"""Manager for domain names."""
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    Iterable,
    MutableMapping,
    MutableSet,
)

if TYPE_CHECKING:
    import types
    from multiprocessing.managers import DictProxy
    from threading import Lock


class DomainReservationError(Exception):
    """Generic error from reserving a domain."""


class DomainForwardEmptyError(DomainReservationError):
    """Error when no domain forwards are requested."""


class DomainInUseError(DomainReservationError):
    """Error when a domain has already been reserved."""


class DomainNameInvalidError(DomainReservationError):
    """Error when an invalid domain name is requested."""


class DomainNameDuplicateError(DomainReservationError):
    """Error when the same domain forward is requested more than once."""


class DomainReserver:
    """Context manager that reserves domains."""

    def __init__(
        self,
        domain_lock: Lock,
        in_use_domains: MutableMapping[tuple[str, int], bool],
        requested_forwards: Iterable[tuple[str, int]],
    ) -> None:
        """Initialize a DomainReserver."""
        self._domain_lock = domain_lock
        self._in_use_domains = in_use_domains
        # Ignore requested port; set to 443.
        self._requested_forwards = [(forward[0], 443) for forward in requested_forwards]
        self._reservations: MutableSet[tuple[str, int]] = set()

    def _check_overlap(
        self,
        requested_forwards: list[tuple[str, int]],
    ) -> list[tuple[str, int]]:
        return [
            forward for forward in requested_forwards if forward in self._in_use_domains
        ]

    def _check_names(
        self,
        requested_forwards: list[tuple[str, int]],
    ) -> list[tuple[str, int]]:
        return [forward for forward in requested_forwards if not forward[0].isalnum()]

    def _check_unique(
        self,
        requested_forwards: list[tuple[str, int]],
    ) -> list[tuple[str, int]]:
        domains = [domain for domain, _ in requested_forwards]
        return list({(domain, 443) for domain in domains if domains.count(domain) > 1})

    def _verify_rule(
        self,
        rule: Callable[[list[tuple[str, int]]], list[tuple[str, int]]],
        exc_type: type[Exception],
    ) -> None:
        violations = rule(self._requested_forwards)
        if violations:
            raise exc_type(violations)

    def __enter__(self) -> None:
        """Validate the request and reserve requested domains."""
        with self._domain_lock:
            # Validate rules.
            self._verify_rule(self._check_names, DomainNameInvalidError)
            self._verify_rule(self._check_overlap, DomainInUseError)
            self._verify_rule(self._check_unique, DomainNameDuplicateError)

            if not len(self._requested_forwards):
                raise DomainForwardEmptyError

            for forward in self._requested_forwards:
                sanitized_forward = (forward[0], 443)
                self._in_use_domains[sanitized_forward] = True
                self._reservations.add(sanitized_forward)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        """Release locks on reservations."""
        with self._domain_lock:
            for forward in self._reservations:
                del self._in_use_domains[forward]


class DomainManager:
    """Class that manages making domain reservations."""

    def __init__(
        self,
        domain_lock: Lock,
        in_use_domains: DictProxy,  # type: ignore [type-arg]
    ) -> None:
        """Initialize a new DomainManager."""
        self._domain_lock = domain_lock
        self._in_use_domains = in_use_domains

    def reserve(
        self,
        requested_forwards: Iterable[tuple[str, int]],
    ) -> DomainReserver:
        """Reserve the forwards requested."""
        return DomainReserver(
            self._domain_lock,
            self._in_use_domains,
            requested_forwards,
        )
