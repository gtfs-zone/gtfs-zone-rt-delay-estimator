"""A small in-process cache whose entries expire after a fixed time."""

import time
from collections.abc import Callable, Hashable


class TTLCache[K: Hashable, V]:
    """Maps keys to values for `ttl` seconds, holding at most `maxsize` entries.

    Misses are cached like hits, so a lookup that found nothing is not repeated
    every poll.
    """

    def __init__(
        self,
        ttl: float,
        maxsize: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._clock = clock
        self._data: dict[K, tuple[float, V]] = {}

    def get_or_load(self, key: K, load: Callable[[], V]) -> V:
        now = self._clock()
        hit = self._data.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = load()
        if len(self._data) >= self.maxsize:
            self._data = {k: v for k, v in self._data.items() if v[0] > now}
            if len(self._data) >= self.maxsize:
                self._data.pop(next(iter(self._data)))
        self._data[key] = (now + self.ttl, value)
        return value
