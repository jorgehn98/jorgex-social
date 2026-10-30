"""Per-client request limits, kept in memory: the service runs as one process (compose.yaml), so
there is nothing to share. There is deliberately no global limit: anyone could use it up and keep
the operator out."""

import ipaddress
import threading
from collections import deque
from datetime import datetime, timedelta

# Above this many tracked clients, idle ones are swept on the next hit, so addresses that came
# once do not pile up in memory.
SWEEP_ABOVE = 10_000


def client_key(host: str | None) -> str:
    """One key per client address, but one per /64 for IPv6: a single subscriber usually holds
    a whole /64 and could otherwise rotate through it."""
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return host or "unknown"
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


class RateLimiter:
    """At most `limit` hits per `window`, for every (limit, window) pair, per key."""

    def __init__(self, limits: list[tuple[int, timedelta]]) -> None:
        self._limits = limits
        self._longest = max(window for _, window in limits)
        self._hits: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: datetime) -> timedelta | None:
        """Counts a hit and returns None if `key` is within every limit; otherwise counts
        nothing and returns how long until it would be."""
        with self._lock:
            if len(self._hits) > SWEEP_ABOVE:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self._longest:
                hits.popleft()
            wait = timedelta(0)
            for limit, window in self._limits:
                recent = [t for t in hits if t > now - window]
                if len(recent) >= limit:
                    wait = max(wait, recent[-limit] + window - now)
            if wait:
                return wait
            hits.append(now)
            return None

    def _sweep(self, now: datetime) -> None:
        idle = now - self._longest
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= idle]:
            del self._hits[key]
