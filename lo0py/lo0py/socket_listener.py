"""Listener for remote client connections trying to connect to proxied server."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import asyncssh
from loguru import logger

from lo0py.caddy.forward_manager import ForwardManager
from lo0py.connection_counter import ConnectionCounter
from lo0py.relay.basic import Relay

if TYPE_CHECKING:
    from pathlib import Path


class SocketListener(asyncssh.SSHListener):
    """Class that spawns relays for each remote client."""

    def __init__(
        self,
        conn: asyncssh.SSHServerConnection,
        remote_host: str,
        remote_port: int,
        mapped_address: str,
        socket_path: Path,
    ) -> None:
        """Construct new SocketListener."""
        super().__init__()
        self._conn = conn
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._mapped_address = mapped_address
        self._socket_path = socket_path

        self._close = asyncio.Event()
        self._closed = asyncio.Event()

        self._forward_manager = ForwardManager(self._mapped_address, self._socket_path)

        self._counter = ConnectionCounter()

        self._relays: set[Relay] = set()
        self._task = asyncio.create_task(self._start_server())

    async def _start_server(self) -> None:
        # Remove socket file if it exists to reuse.
        if self._socket_path.exists():
            self._socket_path.unlink()
        # Create the socket
        # Keep spawning relays until the server is closed.
        server = await asyncio.start_unix_server(
            self._create_relay,
            path=self._socket_path,
        )
        self._forward_manager.start()

        await self._close.wait()

        server.close()
        await server.wait_closed()
        self._closed.set()

    # Suppressing C901, PLR0912 because all the branching is from exception handling.
    async def _create_relay(  # noqa: PLR0912
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        relay: Relay

        try:
            # TODO @lungj: Determine how to switch on HTTPS tunnelling.
            # https://gitlab.wyre.link/matterloom/lo0py/-/issues/14
            relay = Relay(reader, writer, 0)
        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"{e}")
        self._relays.add(relay)

        while not self._close.is_set():
            try:
                async with self._counter.open() as attempt_id:
                    await self._conn.create_connection(
                        lambda: relay,
                        self._remote_host,
                        self._remote_port,
                    )
                    break
            # Limit blast radius of any exceptions.
            # No point in logging; this is very common.
            except Exception:  # noqa: BLE001, S112
                # TODO @lungj: limit exception types ignored.
                # https://gitlab.wyre.link/matterloom/lo0py/-/issues/13
                continue  # nosec B112

        if not self._close.is_set():
            try:
                await relay.relay_unix_socket()
            # Limit blast radius of any exceptions.
            except Exception as e:  # noqa: BLE001
                logger.error(f"{e}")

        try:
            await relay.wait_closed()
        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"{e}")

        try:
            writer.close()
        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"{e}")
        try:
            await writer.wait_closed()
        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"{e}")

        try:
            await self._counter.close(attempt_id)
        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"{e}")
        self._relays.remove(relay)

    def close(self) -> None:
        """Tell all relays to close."""
        # TODO @lungj: Check if lock needed with async.
        # https://gitlab.wyre.link/matterloom/lo0py/-/issues/12
        if self._close.is_set():
            return

        self._forward_manager.stop()

        self._close.set()
        self._socket_path.unlink()
        for relay in self._relays.copy():
            relay.close()

    async def wait_closed(self) -> None:
        """Block until all child relays are closed."""
        for relay in self._relays.copy():
            await relay.wait_closed()
        self._closed.set()
