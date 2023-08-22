"""SSH connection handler."""
from __future__ import annotations

import asyncio
import typing
from typing import (
    TypeVar,
)

import asyncssh
from loguru import logger

from lo0py.cli import Cli
from lo0py.cli.eula import Eula
from lo0py.domain_manager import (
    DomainForwardEmptyError,
    DomainInUseError,
    DomainManager,
    DomainNameDuplicateError,
    DomainNameInvalidError,
    DomainReservationError,
)

if typing.TYPE_CHECKING:
    import socket

    from lo0py.server import Server

# Create a type variable for the Exception type
ExcType = TypeVar("ExcType", bound=BaseException)


class SshHandler:
    """Class that reads/writes data from a socket to handle client requests."""

    def __init__(
        self,
        domain_manager: DomainManager,
        server_cls: type[Server],
        domain: str,
        client_socket: socket.socket,
        conn: tuple[str, int],
    ) -> None:
        """Construct a new SshHandler."""
        self._domain_manager = domain_manager
        self._server_cls = server_cls
        self._domain = domain
        self._client_socket = client_socket
        self._conn = conn

        super().__init__()

    # C901 waived because the complexity is from the number of branches of
    # error-handling.
    async def run(self) -> None:
        """Handle all incoming connection."""
        try:
            result = await self._accept()
        # To prevent any locking up of this thread due to an unanticipated exception,
        # catch any uncaught exceptions, log, and then disconnect the client to
        # limit the blast-radius.
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{exc}")
            result = None

        if not result:
            logger.info(f"{self._conn} Disconnected")
            return

        server, server_connection, cli = result

        try:
            with self._domain_manager.reserve(
                server.requested_forwards,
            ):
                server.start_forwarding.set()

                for forward in server.requested_forwards:
                    wall_task = asyncio.create_task(
                        cli.wall(
                            f"\r\nForwarding https://{forward[0]}.{self._domain}\r\n",
                        ),
                    )
                    try:
                        # Limit amount of time willing to be waited to send a message.
                        await asyncio.wait_for(wall_task, timeout=2)
                    except asyncio.TimeoutError:
                        logger.info(f"{self._conn} wall too slow")
                        server.do_terminate.set()
                        break

                await server.do_terminate.wait()
        except DomainForwardEmptyError:
            logger.info(f"{self._conn} No forwarding requested")
            await cli.wall("\r\nNo forwarding requested\r\n", flush=True)
        except DomainReservationError as exc:
            message = {
                DomainInUseError: "Domains already in use",
                DomainNameInvalidError: "Domains not alphanumeric",
                DomainNameDuplicateError: "Domain names not unique",
            }[type(exc)]
            domain_string = ", ".join([domain for domain, _ in exc.args[0]])
            logger.info(f"{self._conn} {message}")
            await cli.wall(f"\r\n{message}: {domain_string}\r\n")
        # To prevent any locking up of this thread due to an unanticipated exception,
        # catch any uncaught exceptions, log, and then disconnect the client to
        # limit the blast-radius.
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{self._conn} Unhandled exception {exc}")
            await cli.wall("\r\nDisconnecting due to error\r\n", flush=True)

        finally:
            # If we didn't reserve successfully, we need to tell the
            # server to stop waiting.
            server.do_terminate.set()

            cli.close()
            await cli.wait_closed()

            server.close()
            await server.wait_closed()

            server_connection.close()
            await server_connection.wait_closed()

        logger.info(f"{self._conn} disconnection")

    async def _accept(
        self,
    ) -> tuple[Server, asyncssh.SSHServerConnection, Cli] | None:
        # Generate SSH key pair for server (you can use ssh-keygen)
        # For example:
        # ssh-keygen -t rsa -f server_rsa_key
        eula = Eula()
        server = self._server_cls(self._domain, self._conn)

        cli = Cli(eula, server.do_terminate)

        server_options = asyncssh.SSHServerConnectionOptions(
            server_factory=lambda: server,
            process_factory=cli.run,
            server_host_keys="server_rsa_key",  # From file.
            server_version="SSH-2.0-lo",
            line_editor=False,
            line_echo=False,
        )
        server_connection = await asyncssh.run_server(
            self._client_socket,
            options=server_options,
        )

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(eula.read.wait()),
                asyncio.create_task(server.do_terminate.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        if not eula.accepted:
            server.do_terminate.set()
            return None

        return server, server_connection, cli
