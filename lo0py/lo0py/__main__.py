#!/usr/bin/env python3
"""Main lo0py entrypoint."""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import re
import signal
import socket
import sys
import time
import typing
from pathlib import Path

from loguru import logger

from lo0py.domain_manager import DomainManager
from lo0py.server import Server
from lo0py.ssh_handler import SshHandler

if typing.TYPE_CHECKING:
    import types
    from multiprocessing.managers import DictProxy
    from threading import Lock


class Listener(mp.Process):
    """Class that listens on a port."""

    # Allow extra args to avoid globals.
    def __init__(  # noqa: PLR0913
        self,
        server_socket: socket.socket,
        connection_counts_lock: Lock,
        connection_counts: DictProxy[int, int],
        process_id: int,
        domain_manager: DomainManager,
        server_cls: type[Server],
        domain: str,
    ) -> None:
        """Initialize Listener."""
        super().__init__()
        self._server_socket = server_socket
        self._connection_counts_lock = connection_counts_lock
        self._connection_counts = connection_counts
        self._process_id = process_id
        self._domain_manager = domain_manager
        self._server_cls = server_cls
        self._domain = domain
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def _signal_handler(
        self,
        _: int,
        __: types.FrameType | None = None,
    ) -> None:
        self._stop_event.set()
        logger.info(f"{self._process_id} stopping")

    def run(self) -> None:
        """Run instance of server.

        Set up signal handlers.
        """
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        asyncio.run(self._run())

    async def _run(self) -> None:
        await asyncio.gather(self._listen())

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        # Wait for tasks to complete.
        await asyncio.gather(*tasks)

    def _decrement_connection_count(
        self,
        task: asyncio.Task[None],
    ) -> typing.Callable[[asyncio.Future[None]], None]:
        def _inner(future: asyncio.Future[None]) -> None:
            with self._connection_counts_lock:
                self._connection_counts[self._process_id] -= 1
                self._tasks.remove(task)
            _ = future.result()

        return _inner

    async def _listen(self) -> None:
        loop = asyncio.get_event_loop()
        while not self._stop_event.is_set():
            # Only accept connections on the least-busy processes.
            with self._connection_counts_lock:
                is_min = self._connection_counts[self._process_id] == min(
                    self._connection_counts.values(),
                )
            if not is_min:
                await asyncio.sleep(0.5)
                continue

            # Wrap the coroutine in a task.
            accept_task = loop.create_task(loop.sock_accept(self._server_socket))

            try:
                # Try to accept a connection with a short timeout.
                done, pending = await asyncio.wait(
                    (accept_task,),
                    timeout=0.5,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()  # Cancel incomplete tasks.

                if done:
                    logger.info(f"P{self._process_id} Accepted connection")
                    with self._connection_counts_lock:
                        self._connection_counts[self._process_id] += 1

                    client_socket, addr = done.pop().result()
                    handler = SshHandler(
                        self._domain_manager,
                        self._server_cls,
                        self._domain,
                        client_socket,
                        addr,
                    )
                    handler_task = loop.create_task(handler.run())

                    self._tasks.append(handler_task)
                    handler_task.add_done_callback(
                        self._decrement_connection_count(handler_task),
                    )

            except asyncio.TimeoutError:
                continue  # No connection accepted before timeout.
        logger.info(f"P{self._process_id} no longer accepting connections")


def _terminate_processes(
    signal_number: int,
    _: types.FrameType | None = None,
) -> None:
    """Signal handler for main program."""
    for listener in listeners:
        os.kill(listener.pid or 0, signal_number)
    logger.info("Waiting for all child processes to finish.")
    for listener in listeners:
        listener.join()
    sys.exit()


def _bind_server_socket(
    bind_tuple: tuple[str, int],
) -> socket.socket:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    server_socket.bind(bind_tuple)
    server_socket.listen(100)
    # Suppressing FBT003 because there is no parameter name for the C builtin.
    server_socket.setblocking(False)  # noqa: FBT003
    return server_socket


def main() -> None:
    """Run main program."""
    interface: str = os.environ.get(
        "LO0APP_INTERFACE",
        "0.0.0.0",  # nosec: B104. # noqa: S104
    )
    port: int = int(os.environ.get("LO0APP_PORT", 22))
    # Suppress B104, S014: This is a public service. Open the port on all interfaces.
    bind_tuple: tuple[str, int] = (interface, port)
    domain: str = os.environ.get("LO0APP_DOMAIN", "free.lo.app")
    num_processes: int = int(os.environ.get("LO0APP_PROCESSES", 8))

    logger.info(f"Listening on port {port} for {domain}...")
    signal.signal(signal.SIGTERM, _terminate_processes)
    signal.signal(signal.SIGINT, _terminate_processes)

    # Find sessions from older (hopefully still-running) sessions.
    existing_sockets = Path("/var/run/lo0app/").glob("forward-*.sock")

    domain_escaped = domain.replace(".", "\\.")
    in_use = {}

    connection_counts = {idx: 0 for idx in range(num_processes)}
    for path in existing_sockets:
        match = re.match(
            rf"forward-(.*)\\.{domain_escaped}\\.sock",
            path.name,
        )
        if match:
            in_use[(match.group(1), 443)] = True

    # Create a shared socket for all subprocesses.
    server_socket = _bind_server_socket(bind_tuple)

    with mp.Manager() as manager:
        domain_manager = DomainManager(manager.Lock(), manager.dict(in_use))
        listener_lock = manager.Lock()

        for count in range(num_processes):
            handler = Listener(
                server_socket,
                listener_lock,
                manager.dict(connection_counts),
                count,
                domain_manager,
                Server,
                domain,
            )

            # Spawn a new process up to the limit.
            handler.start()
            listeners.append(handler)

        # Must stay in manager context or else child processes lose managed objects.
        while True:
            time.sleep(1000)


def handle_signal_parent(
    signal_number: int,
    _: types.FrameType | None = None,
) -> None:
    """Signal handler for parent process."""
    os.kill(child_pid, signal_number)
    sys.exit(0)


if __name__ == "__main__":
    child_pid = os.fork()

    if child_pid == 0:  # Spawn listener in child process.
        listeners: list[Listener] = []
        main()
    else:
        # Set up signal handlers in the parent to catch SIGTERM and SIGINT.
        signal.signal(signal.SIGTERM, handle_signal_parent)
        signal.signal(signal.SIGINT, handle_signal_parent)

        # Keep the parent process running.
        while True:
            time.sleep(10)
