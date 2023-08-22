"""Handle the SSH connection lifecycle."""
from __future__ import annotations

import asyncio
import typing
from pathlib import Path

import asyncssh
from loguru import logger

from lo0py.socket_listener import SocketListener


class Server(asyncssh.SSHServer):
    """Class for handling an SSH connection lifecycle."""

    do_terminate = property(lambda self: self._do_terminate)
    start_forwarding = property(lambda self: self._start_forwarding)
    requested_forwards = property(lambda self: tuple(self._requested_forwards))

    def __init__(self, domain: str, remote: tuple[str, int]) -> None:
        """Initialize a new Server object."""
        super().__init__()
        self._domain = domain
        self._remote = remote
        self._eula_accepted = asyncio.Event()
        self._do_terminate = asyncio.Event()
        self._start_forwarding = asyncio.Event()
        self._conn: asyncssh.SSHServerConnection | None = None

        self._requested_forwards: list[tuple[str, int]] = []

        self._forwarders: list[SocketListener] = []

    def public_key_auth_supported(self) -> bool:
        """Return that public key auth is supported."""
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        """Accept any provided public key."""
        return True

    def password_auth_supported(self) -> bool:
        """Return that password auth is supported."""
        return True

    def validate_password(self, username: str, password: str) -> bool:
        """Accept incoming password."""
        return True

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        """Handle incoming ssh connection."""
        logger.info(f"{self._remote}: Connected")
        self._conn = conn

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle remote ssh disconnection."""
        logger.info(f"{self._remote}: Disconnected: {exc}")
        self.do_terminate.set()

    async def _get_forwarder(
        self,
        host: str,
        port: int,
    ) -> SocketListener | bool:
        if self._eula_accepted.is_set():
            return False
        self._requested_forwards.append((host, port))

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(self._start_forwarding.wait()),
                asyncio.create_task(self._do_terminate.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        # Check if eula has already been accepted to prevent late/slow
        # attempts to forward.
        if self._do_terminate.is_set() or self._eula_accepted.is_set():
            return False

        mapped_address = f"{host}.{self._domain}"
        logger.info(
            f"Forwarding {mapped_address} at "
            f"/var/run/lo0app/forward-{mapped_address}.sock",
        )
        socket_path = Path(f"/var/run/lo0app/forward-{mapped_address}.sock")

        if not self._conn:
            raise AssertionError

        forwarder = SocketListener(self._conn, host, port, mapped_address, socket_path)
        self._forwarders.append(forwarder)
        return forwarder

    def server_requested(
        self,
        listen_host: str,
        listen_port: int,
    ) -> typing.Coroutine[typing.Any, typing.Any, SocketListener | bool]:
        """Return a future that will handle any requests to forward a port."""
        logger.info(
            f"{self._remote}: Port forward request: {listen_host} {listen_port}",
        )
        return self._get_forwarder(listen_host, listen_port)

    def close(self) -> None:
        """Signal all forwarders to stop."""
        for forwarder in self._forwarders:
            forwarder.close()

    async def wait_closed(self) -> None:
        """Block until all forwarders are stopped."""
        # This method is not part of the library, so must be called explicitly.
        for forwarder in self._forwarders:
            await forwarder.wait_closed()
