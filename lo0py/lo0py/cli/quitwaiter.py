"""Shell that waits until Ctrl+C."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import aiofiles
import asyncinotify

from lo0py.cli import Shell, wrap_preserving_linebreaks

if TYPE_CHECKING:
    import asyncssh


class QuitWaiter(Shell):
    """Shell that blocks until client sends Ctrl+C."""

    def _write_wrapped_message(
        self,
        process: asyncssh.SSHServerProcess[str],
        message: str,
    ) -> None:
        """Write word-wrapped message to the shell's stdout."""
        width, _, _, _ = process.term_size
        max_width = max(min(width - 1, 120), 30)
        word_wrapped: str = "\n".join(wrap_preserving_linebreaks(message, max_width))

        process.stdout.write(word_wrapped.replace("\n", "\r\n"))

    async def run(
        self,
        process: asyncssh.SSHServerProcess[str],
    ) -> Shell | None:
        """Run the shell."""
        self._write_wrapped_message(
            process,
            (
                "Press Ctrl+C to exit.\n"
                "This service is provided as-is and may restart "
                "for periodic upgrades, "
                "so please make sure to monitor the tunnel!\n"
            ),
        )

        try:
            while True:
                # Wait for messages pushed to the broadcast directory.
                message_path = "/var/run/lo0app/messages"
                with asyncinotify.Inotify() as inotify:
                    inotify.add_watch(message_path, asyncinotify.Mask.MOVED_TO)
                    tasks = [
                        asyncio.create_task(process.stdin.read(1)),
                        asyncio.create_task(inotify.get()),
                    ]
                    done, pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()

                    for task in done:
                        if task == tasks[0]:
                            char = cast(str, task.result())
                            # Exit if Ctrl+C or session is dead
                            if char.encode() == b"\x03" or not char:
                                return None
                        elif task == tasks[1]:
                            result = cast(asyncinotify.Event, task.result())
                            msg_path = message_path + "/" + str(result.name)
                            async with aiofiles.open(msg_path) as msg_file:
                                self._write_wrapped_message(
                                    process,
                                    await msg_file.read(),
                                )

        except:  # noqa: E722
            return None
