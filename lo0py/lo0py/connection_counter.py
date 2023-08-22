"""Connection lifecycle tracking."""
from __future__ import annotations

import asyncio
import contextlib
import enum
import errno
import typing

import asyncssh.misc
from loguru import logger

MAX_CONNECTIONS_INITIAL = 1000


class _OpeningStatus(enum.IntEnum):
    OPENING = enum.auto()
    OPEN_COMPLETE = enum.auto()
    OPENED = enum.auto()
    CLOSED = enum.auto()


class ConnectionCounter:
    """Connection lifecycle state tracker.

    Used to prevent excessive numbers of connections from being opened at once.
    """

    def __init__(self) -> None:
        """Initialize ConnectionCounter."""
        self._max_connections = MAX_CONNECTIONS_INITIAL
        self._lock = asyncio.Condition()

        # Maximum number of connections that can be in the middle of being opened.
        self._max_opening = 100

        # List of what's opening/opened.
        self._connection_record: list[tuple[int, _OpeningStatus]] = []
        self._attempt = 0

    @property
    def _num_opens_in_progress(self) -> int:
        opens = filter(
            lambda x: x[1] == _OpeningStatus.OPENING,
            self._connection_record,
        )
        completed_opens = filter(
            lambda x: x[1] == _OpeningStatus.OPEN_COMPLETE,
            self._connection_record,
        )

        return len(tuple(opens)) - len(tuple(completed_opens))

    @property
    def _num_connections(self) -> int:
        """Number of fully open connections."""
        opened = filter(
            lambda x: x[1] == _OpeningStatus.OPENED,
            self._connection_record,
        )
        closed = filter(
            lambda x: x[1] == _OpeningStatus.CLOSED,
            self._connection_record,
        )

        return len(tuple(opened)) - len(tuple(closed))

    @property
    def _opens_in_progress(self) -> typing.Iterator[int]:
        connection_record = self._connection_record[:]
        open_complete = set()
        # Work backwards; yield anything that has an opening status without a
        # corresponding open_complete status. Since we're working backwards,
        # we'll have seen the completion status if it exists. Otherwise, we can
        # immediately yield the value.
        for attempt_id, status in reversed(connection_record):
            if status == _OpeningStatus.OPEN_COMPLETE:
                open_complete.add(attempt_id)
            elif status == _OpeningStatus.OPENING and attempt_id not in open_complete:
                yield attempt_id

    def _record_opened(self, attempt_id: int) -> None:
        """Record that a connection has been successfully opened.

        Requires lock already be held. Connection is opened.
        """
        self._connection_record.append((attempt_id, _OpeningStatus.OPENED))

    def _record_closed(self, attempt_id: int) -> None:
        """Record that a connection has been closed.

        Requires lock already be held.
        """
        self._connection_record.append((attempt_id, _OpeningStatus.CLOSED))

        self._clean_up_records()

    def _clean_up_records(self) -> None:
        """Remove irrelevant records."""
        self._lock.notify()

        # Remove any stale opening records: anything where the open attempt has been
        # completed before any currently in-progress openings began.
        opens_in_progress = set(self._opens_in_progress)

        ## Find the index of the first opening that is in progress.
        first_open_index = 0
        for idx, item in enumerate(self._connection_record):
            if item[0] in opens_in_progress:
                first_open_index = idx
                break

        ## Find the set of openings that were complete before the identified record
        ## above and remove all related records.
        completed = set()
        current_index = first_open_index - 1
        while current_index >= 0:
            attempt_id, status = self._connection_record[current_index]
            if status == _OpeningStatus.OPEN_COMPLETE:
                completed.add(attempt_id)
            if attempt_id in completed and status in {
                _OpeningStatus.OPENING,
                _OpeningStatus.OPEN_COMPLETE,
            }:
                self._connection_record.pop(current_index)
                # The list has shrunk!
                first_open_index -= 1
            else:
                current_index -= 1

        # Remove any stale connection records: anything where the connection has been
        # closed before any currently in-progress openings began.
        ## Find the set of connections that were closed before the identified record
        ## above and remove all related records.
        closed = set()
        current_index = first_open_index - 1
        while current_index >= 0:
            attempt_id, status = self._connection_record[current_index]
            if status == _OpeningStatus.CLOSED:
                closed.add(attempt_id)
            if attempt_id in closed:
                self._connection_record.pop(current_index)
            else:
                current_index -= 1

    def _record_open_complete(self, attempt_id: int) -> None:
        """Record an attempt to open is complete.

        Requires lock already be held.
        """
        self._connection_record.append((attempt_id, _OpeningStatus.OPEN_COMPLETE))

        self._clean_up_records()

    def _record_open(self, attempt_id: int) -> None:
        """Record an attempt to open a connection.

        Requires lock already be held.
        """
        self._connection_record.append((attempt_id, _OpeningStatus.OPENING))

    def _over_open_limit(self) -> bool:
        """Return True iff now connections are allowed.

        New connections are not permitted if too many existing connections exist
        or too many are in the process of being opened.
        """
        too_many_connections = (
            self._num_connections + self._num_opens_in_progress
        ) >= self._max_connections

        too_many_opening = self._num_opens_in_progress >= self._max_opening
        return too_many_connections or too_many_opening

    @contextlib.asynccontextmanager
    # Suppressing A003 because the open/close pairing of names is clean.
    async def open(self) -> typing.AsyncIterator[int]:  # noqa: A003
        """Block until a connection can be made and handle the attempt."""
        async with self._lock:
            while self._over_open_limit():
                await self._lock.wait()

            self._attempt += 1
            attempt_id = self._attempt
            self._record_open(attempt_id)

        try:
            yield attempt_id
        except OSError as e:
            async with self._lock:
                self._record_open_complete(attempt_id)
            if e.errno == errno.EMFILE:
                logger.error("Too many files. Pausing.")
                await asyncio.sleep(0.01)
                return

            # EPIPE is closed pipe.
            elif e.errno in {errno.EBADF, errno.EPIPE}:
                return
            logger.error(f"{e}")
            raise
        except asyncssh.misc.ChannelOpenError as exc:
            async with self._lock:
                self._record_open_complete(attempt_id)

                if exc.reason == "Connection refused":
                    await asyncio.sleep(1)
                else:
                    # Can't drop below 32 -- Caddy keeps those connections open.
                    self._max_connections = max(
                        min(
                            self._num_connections + self._num_opens_in_progress,
                            self._max_connections,
                        ),
                        36,
                    )
        except Exception as e:
            async with self._lock:
                self._record_open_complete(attempt_id)
            logger.error(f"{e} {type(e)}")
            raise
        else:
            async with self._lock:
                self._record_open_complete(attempt_id)
                self._record_opened(attempt_id)

    async def close(self, attempt_id: int) -> None:
        """Handle connection is closed."""
        async with self._lock:
            self._record_closed(attempt_id)
