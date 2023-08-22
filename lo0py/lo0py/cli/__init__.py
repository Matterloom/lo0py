"""Command-line interface system."""
from __future__ import annotations

import asyncio
import textwrap
import typing
from abc import ABC, abstractmethod

if typing.TYPE_CHECKING:
    import asyncssh


def wrap_preserving_linebreaks(text: str, width: int = 70) -> typing.Iterable[str]:
    """Wrap text to a specific width."""
    # Split the text by line breaks to get individual lines
    lines = text.split("\n")

    # Use textwrap.fill() on each individual line
    return [textwrap.fill(line, width=width) for line in lines]


class Shell(ABC):
    """Base class for all shells that can be run."""

    @abstractmethod
    async def run(
        self,
        process: asyncssh.SSHServerProcess[str],
    ) -> Shell | None:
        """Run the CLI."""


class Cli:
    """CLI."""

    def __init__(self, shell: Shell, termination_signal: asyncio.Event) -> None:
        """Initialize the CLI."""
        super().__init__()
        self._shell: Shell | None = shell
        self._termination_signal = (
            termination_signal  # Event to signal when CLI is done.
        )
        self._shell_task: asyncio.Task[Shell | None] | None = None
        self._process: asyncssh.SSHServerProcess[str] | None = None

    async def run(self, process: asyncssh.SSHServerProcess[str]) -> None:
        """Run the CLI."""
        self._process = process
        while self._shell:
            # assert process
            self._shell_task = asyncio.create_task(self._shell.run(process))
            self._shell = await self._shell_task
        self.close()

    async def wall(self, message: str, *, flush: bool = False) -> None:
        """Write a message to the console."""
        if self._process:
            self._process.stdout.write(message)
            if not flush:
                return
            await self._process.stdout.drain()

    def close(self) -> None:
        """Stop the shell."""
        self._shell = None
        if self._shell_task:
            self._shell_task.cancel()
        if self._process:
            self._process.stdout.close()
        self._termination_signal.set()

    async def wait_closed(self) -> None:
        """Block until the CLI has exited."""
        if self._process:
            await self._process.wait_closed()
