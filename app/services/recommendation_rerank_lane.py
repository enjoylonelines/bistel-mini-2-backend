"""Process-local admission lane for the recommendation rerank call.

This deliberately has no distributed semantics.  Each application process owns
its own semaphore, so a multi-worker deployment must not present its capacity
as a global limit.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(frozen=True)
class RerankAdmission:
    capacity: int
    queue_wait_ms: float
    in_flight: int
    max_in_flight: int


class ProcessLocalRerankLane:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("process-local rerank capacity must be at least 1")
        self.capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self._in_flight = 0
        self._max_in_flight = 0

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[RerankAdmission]:
        queued_at = time.perf_counter()
        await self._semaphore.acquire()
        queue_wait_ms = (time.perf_counter() - queued_at) * 1000
        self._in_flight += 1
        self._max_in_flight = max(self._max_in_flight, self._in_flight)
        admission = RerankAdmission(
            capacity=self.capacity,
            queue_wait_ms=round(queue_wait_ms, 3),
            in_flight=self._in_flight,
            max_in_flight=self._max_in_flight,
        )
        try:
            yield admission
        finally:
            self._in_flight -= 1
            self._semaphore.release()


_lanes_by_capacity: dict[int, ProcessLocalRerankLane] = {}


def get_process_local_rerank_lane(capacity: int) -> ProcessLocalRerankLane:
    """Return the shared lane for this Python process and configured capacity."""
    lane = _lanes_by_capacity.get(capacity)
    if lane is None:
        lane = ProcessLocalRerankLane(capacity)
        _lanes_by_capacity[capacity] = lane
    return lane
