"""In-memory ring buffer of notable runtime events for diagnostics.

Pure observability: the coordinator records events here and never reads them
back to make control decisions. The diagnostics download dumps the buffer so a
bug report shows what the integration actually did (405 writes, reconnects,
recovery attempts, phase restores, baseline restores) with timestamps, in the
order they happened.

Two buckets: phase events (405 writes, recovery) are kept apart from system
noise (reconnects, restores), so a flapping connection can never evict the
phase evidence that matters in a bug report.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import count

MAX_PHASE_EVENTS = 25
MAX_SYSTEM_EVENTS = 25
# Backwards-compatible alias: the historic single-bucket capacity.
MAX_EVENTS = MAX_SYSTEM_EVENTS

# Event kinds that carry phase evidence and must survive system noise.
_PHASE_KINDS = frozenset({"405_write", "recovery_attempt", "recovery_done"})


def _is_phase_event(kind: str) -> bool:
    return kind in _PHASE_KINDS or kind.startswith("recovery")


@dataclass(frozen=True, slots=True)
class Event:
    at: str
    kind: str
    detail: str
    seq: int = field(compare=True)


class EventLog:
    """Two fixed-capacity, newest-wins event buckets dumped chronologically."""

    def __init__(
        self,
        maxlen: int = MAX_SYSTEM_EVENTS,
        maxlen_phase: int = MAX_PHASE_EVENTS,
        maxlen_system: int | None = None,
    ) -> None:
        if maxlen_system is None:
            maxlen_system = maxlen
            if maxlen_phase == MAX_PHASE_EVENTS and maxlen != MAX_SYSTEM_EVENTS:
                maxlen_phase = maxlen
        self._phase: deque[Event] = deque(maxlen=maxlen_phase)
        self._system: deque[Event] = deque(maxlen=maxlen_system)
        self._seq = count()

    def record(self, kind: str, detail: str) -> None:
        event = Event(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            kind=kind,
            detail=detail,
            seq=next(self._seq),
        )
        if _is_phase_event(kind):
            self._phase.append(event)
        else:
            self._system.append(event)

    def as_list(self) -> list[dict[str, str]]:
        merged = sorted(
            list(self._phase) + list(self._system),
            key=lambda event: (event.at, event.seq),
        )
        return [
            {"at": event.at, "kind": event.kind, "detail": event.detail}
            for event in merged
        ]

    def __len__(self) -> int:
        return len(self._phase) + len(self._system)

    def __iter__(self):
        return iter(list(self._phase) + list(self._system))
