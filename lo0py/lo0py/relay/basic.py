"""Transparent relay."""
from __future__ import annotations

import asyncio
import typing

import asyncssh


class Relay(asyncssh.SSHTCPSession[bytes]):
    """A transparent relay class."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        id_: int,
    ) -> None:
        """Initialize a new relay."""
        super().__init__()
        self._unix_reader = reader
        self._unix_writer = writer
        self.id = id_
        self._channel: asyncssh.SSHTCPChannel[bytes] | None = None
        self._channel_available = asyncio.Event()

    def connection_made(self, channel: asyncssh.SSHTCPChannel[bytes]) -> None:
        """Handle a successful connection to the upstream."""
        self._channel = channel
        self._channel_available.set()

    async def relay_unix_socket(self) -> None:
        """Copy data from the client to the upstream."""
        await self._channel_available.wait()
        if not self._channel:
            raise AssertionError
        while True:
            data = await self._unix_reader.read(1024 * 16)
            if not data:
                self.close()
                return
            try:
                self._channel.write(data)
            except:  # noqa: E722
                self.close()
                return

    def data_received(self, data: typing.ByteString, _: asyncssh.DataType) -> None:
        """Handle data coming from the upstream server."""
        try:
            self._unix_writer.write(data)
        except:  # noqa: E722
            self.close()

    def eof_received(self) -> bool:
        """Handle an EOF from the upstream."""
        self.close()
        return False

    def close(self) -> None:
        """Close the client connection."""
        self._unix_writer.close()
        if not self._unix_reader.at_eof():
            self._unix_reader.feed_eof()

        if self._channel:
            self._channel.close()

    async def wait_closed(self) -> None:
        """Block until the client connection is closed."""
        await self._unix_writer.wait_closed()

        if self._channel:
            await self._channel.wait_closed()
