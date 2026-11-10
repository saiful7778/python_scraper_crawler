import asyncio
import time


class RateLimiter:
    def __init__(self, delay=1.0, clock=time.monotonic):
        self.delay = max(0.0, float(delay))
        self.clock = clock
        self._last = {}

    async def acquire(self, domain, cancel_event=None):
        remaining = self._last.get(domain, 0.0) + self.delay - self.clock()
        while remaining > 0:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                await asyncio.wait_for(
                    asyncio.sleep(min(remaining, 0.2)),
                    timeout=min(remaining, 0.2) + 0.05,
                )
            except TimeoutError:
                pass
            remaining = self._last.get(domain, 0.0) + self.delay - self.clock()
        self._last[domain] = self.clock()
