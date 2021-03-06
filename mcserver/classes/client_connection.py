# Future patches
from __future__ import annotations

# Stdlib
from traceback import format_exc
from typing import TYPE_CHECKING
from uuid import UUID

# External Libraries
from anyio import sleep, create_event, create_task_group
from anyio.exceptions import TLSRequired
from quarry.data import packets
from quarry.net.crypto import Cipher, make_server_id, make_verify_token
from quarry.types.buffer import BufferUnderrun

# MCServer
from mcserver.classes.client_message import ClientMessage
from mcserver.objects.event_handler import EventHandler
from mcserver.objects.packet_handler import PacketHandler
from mcserver.objects.player_registry import PlayerRegistry
from mcserver.utils.logger import warn, debug, error

if TYPE_CHECKING:
    from typing import List, Dict, Union, Optional
    from anyio import SocketStream, Event
    from mcserver.classes.player import Player
    from mcserver.utils.misc import AnyBuffer


class ClientConnection:
    def __init__(self, client: SocketStream):
        self.client = client
        self.do_loop = True
        self.protocol_state = "init"
        self.protocol_version = packets.default_protocol_version
        self.messages: List[bytes] = []
        self._locks: List[
            Dict[str,
                 Union[
                     str,
                     Event,
                     Optional[AnyBuffer]
                 ]]
        ] = []
        self.server_id = make_server_id()
        self.verify_token = make_verify_token()
        self.cipher = Cipher()
        self.display_name = ""
        self.uuid: UUID = None

    @property
    def player(self) -> Player:
        return PlayerRegistry.get_player(self.uuid)

    def __repr__(self):
        return (f"ClientConnection(loop={self.do_loop}, "
                f"message_queue={len(self.messages)}, "
                f"lock_queue={len(self._locks)})")

    async def serve(self):
        async with create_task_group() as tg:
            await tg.spawn(self.serve_loop)
            await tg.spawn(self.write_loop)

    async def serve_loop(self):
        data = b""
        run_again = False
        async with create_task_group() as tg:
            while self.do_loop:
                if not run_again:
                    try:
                        line = await self.client.receive_some(1024)
                    except ConnectionError:
                        line = b""

                    if line == b"":
                        try:
                            warn(f"Closing connection to {self.client.server_hostname}")
                        except TLSRequired:
                            pass

                        self.do_loop = False
                        break

                    data += self.cipher.decrypt(line)

                try:
                    msg = ClientMessage(self, data, self.protocol_version)
                except BufferUnderrun:
                    run_again = False
                    continue
                else:
                    data = data[msg.old_len:]
                    if data != b"":
                        run_again = True

                for lock in self._locks:
                    if lock["name"] == msg.name:
                        self._locks.remove(lock)
                        lock["result"] = msg.buffer
                        await lock["lock"].set()
                        break

                if msg.name == "handshake":
                    await self.handle_msg(msg)
                else:
                    await tg.spawn(self.handle_msg, msg)

            for lock in self._locks:
                await lock["lock"].set()
            if self.protocol_state == "play":
                # User was logged in
                debug("Player left, removing from game...")
                # TODO: Fix EventHandler
                # Requires: client_message.py:22
                # EventHandler.event_player_leave(self.player)
                PlayerRegistry.players.remove(self.player)

    async def handle_msg(self, msg: ClientMessage):
        try:
            coro = PacketHandler.decode(msg)
            if coro:
                args = await coro
                coro2 = EventHandler.handle_event(msg, args)
                if coro2:
                    await coro2
        except Exception:  # pylint: disable=broad-except
            error(f"Exception occurred:\n{format_exc()}")

    async def write_loop(self):
        while self.do_loop:
            if self.messages:
                msg = self.messages.pop(0)
                debug(f"Sending to client: {msg}")
                await self.client.send_all(msg)
            else:
                await sleep(0.00001)  # Allow other tasks to run

    async def wait_for_packet(self, packet_name: str) -> AnyBuffer:
        lock = {
            "name": packet_name,
            "lock": create_event(),
            "result": None
        }

        self._locks.append(lock)
        await lock["lock"].wait()

        res: AnyBuffer = lock["result"]
        return res

    def send_packet(self, packet: bytes):
        self.messages.append(self.cipher.encrypt(packet))
