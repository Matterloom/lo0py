"""Relay for HTTPS upstream."""
import asyncio
import ssl
import typing

import asyncssh
from loguru import logger

from lo0py.relay.basic import Relay as BasicRelay


class Relay(BasicRelay):
    """Relay HTTPS server data."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        id_: int,
        hostname: str,
    ) -> None:
        """Initialize new HTTPS relay."""
        super().__init__(reader, writer, id_)

        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # The client presumably doesn't have a valid cert.
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE
        self._ssl_incoming, self._ssl_outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()

        self._ssl_object = self._ssl_context.wrap_bio(
            self._ssl_incoming,
            self._ssl_outgoing,
            server_hostname=hostname,
        )

        self._data_received = asyncio.Event()
        self._data_written = asyncio.Event()
        self._doing_handshake = True

    async def _handshake(self) -> None:
        if not self._channel:
            raise AssertionError

        while self._doing_handshake:
            try:
                # Try to perform the handshake
                self._data_received.clear()
                self._ssl_object.do_handshake()

                data = self._ssl_outgoing.read()
                self._channel.write(data)

                self._doing_handshake = False
            # Any response that is undertaken must be repeated, so try/except
            # must be performed inside a loop.
            except ssl.SSLWantWriteError:  # noqa: PERF203
                await self._data_received.wait()
                # The SSL object wants data written to it; wait for data from channel.

            except ssl.SSLWantReadError:
                # The SSL object has produced data to be read; write data to channel.
                data = self._ssl_outgoing.read()
                self._channel.write(data)
                await self._data_received.wait()

            except ssl.SSLError:
                # Handle other SSL errors
                raise

    async def relay_unix_socket(self) -> None:
        """Copy data from the client to the upstream server."""
        await self._channel_available.wait()

        if not self._channel:
            raise AssertionError

        try:
            await self._handshake()
        except:  # noqa: E722
            self.close()
            return

        while True:
            data = await self._unix_reader.read(1024 * 16)
            if not data:
                self.close()
                return
            try:
                while data:
                    bytes_written = self._ssl_object.write(data)
                    data = data[bytes_written:]

                    encrypted_data = self._ssl_outgoing.read()
                    self._channel.write(encrypted_data)
            except:  # noqa: E722
                self.close()
                return

    def _write_upstream_to_ssl_incoming(self, data: typing.ByteString) -> None:
        """Write all data from `data` to the ssl-translator."""
        while data:
            bytes_written = self._ssl_incoming.write(data)
            data = data[bytes_written:]
        self._data_received.set()

    def _write_decrypted_upstream_to_client(self) -> None:
        """Write incoming data the ssl-translator has decrypted back to the client."""
        while True:
            try:
                decrypted_data = self._ssl_object.read()
            except ssl.SSLWantReadError:
                # This means the read is done.
                break
            self._unix_writer.write(decrypted_data)

    def data_received(self, data: typing.ByteString, _: asyncssh.DataType) -> None:
        """Handle data coming from the upstream server."""
        try:
            self._write_upstream_to_ssl_incoming(data)

            # Do not pass any handshaking data back to the client.
            if self._doing_handshake:
                return

            self._write_decrypted_upstream_to_client()

        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Err {e} {type(e)}")
            self.close()

        # `self._channel` should already be set.
        if not self._channel:
            raise AssertionError

        # Reading from an SSL object can result in renegotation,
        # so send any outgoing data that was generated as part of
        # renegotiation.
        try:
            outgoing = self._ssl_outgoing.read()
            self._channel.write(outgoing)

        # TODO @lungj: This may require suppressing further relayed data.
        # https://gitlab.wyre.link/matterloom/lo0py/-/issues/14

        # Limit blast radius of any exceptions.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Err {e} {type(e)}")
            self.close()
