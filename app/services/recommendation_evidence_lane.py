"""Process-local admission for candidate-level RAG evidence searches."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(frozen=True)
class EvidenceAdmission:
    capacity: int
    queue_wait_ms: float
    in_flight: int
    max_in_flight: int


class ProcessLocalEvidenceLane:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("process-local evidence capacity must be at least 1")
        self.capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self._in_flight = 0
        self._max_in_flight = 0

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[EvidenceAdmission]:
        queued_at = time.perf_counter()
        await self._semaphore.acquire()
        self._in_flight += 1
        self._max_in_flight = max(self._max_in_flight, self._in_flight)
        admission = EvidenceAdmission(
            capacity=self.capacity,
            queue_wait_ms=round((time.perf_counter() - queued_at) * 1000, 3),
            in_flight=self._in_flight,
            max_in_flight=self._max_in_flight,
        )
        try:
            yield admission
        finally:
            self._in_flight -= 1
            self._semaphore.release()


_lanes: dict[int, ProcessLocalEvidenceLane] = {}


def get_process_local_evidence_lane(capacity: int) -> ProcessLocalEvidenceLane:
    """Return the shared evidence lane for this Python process and capacity."""
    lane = _lanes.get(capacity)
    if lane is None:
        lane = ProcessLocalEvidenceLane(capacity)
        _lanes[capacity] = lane
    return lane
