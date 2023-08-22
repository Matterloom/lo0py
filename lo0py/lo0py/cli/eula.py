"""A shell that presents a EULA."""
from __future__ import annotations

import asyncio
import typing
from pathlib import Path

from loguru import logger

from lo0py.cli import Shell, wrap_preserving_linebreaks
from lo0py.cli.quitwaiter import QuitWaiter

if typing.TYPE_CHECKING:
    import asyncssh


class Eula(Shell):
    """Shell that waits for a EULA to be accepted/rejected."""

    read = property(lambda self: self._read)
    accepted = property(lambda self: self._accepted)

    def __init__(self) -> None:
        """Initialize the Shell."""
        super().__init__()
        self._read = asyncio.Event()
        self._accepted = False

    async def run(
        self,
        process: asyncssh.SSHServerProcess[str],
    ) -> Shell | None:
        """Run the shell."""
        with Path("eula.txt").open() as eula_file:
            file_contents_str: str = eula_file.read().replace("\r", "")

        # Word wrap text
        width, _, _, _ = process.term_size
        max_width = max(min(width - 1, 120), 30)
        word_wrapped: str = "\n".join(
            wrap_preserving_linebreaks(file_contents_str, max_width),
        )

        process.stdout.write(word_wrapped.replace("\n", "\r\n"))

        try:
            # Don't wait too long for EULA to be accepted.
            char = await asyncio.wait_for(process.stdin.read(1), 30)
        except TimeoutError:
            process.stdout.write(
                "Timed out waiting for Terms of Service to be accepted.\r\n",
            )
            process.stdout.close()
            await process.stdout.wait_closed()
            self._read.set()
            logger.info("Timed out waiting for EULA")
            return None

        # Handle user response.
        if char != "y":
            process.stdout.write("Terms of Service declined.\r\n")
            process.stdout.close()
            await process.stdout.wait_closed()
            self._read.set()
            logger.info("EULA declined")
            return None

        process.stdout.write("Terms of Service accepted\r\n\r\n")
        self._accepted = True
        self._read.set()
        return QuitWaiter()
