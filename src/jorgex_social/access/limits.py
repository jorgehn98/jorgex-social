"""Per-client request limits, kept in memory: the service runs as one process (compose.yaml), so
there is nothing to share. There is deliberately no global limit: anyone could use it up and keep
the operator out."""

import ipaddress
import threading
from collections import deque
from datetime import datetime, timedelta

# Clients tracked at most. Past it, the one heard from least recently is forgotten, so memory
# stays bounded however many addresses an attacker has.
MAX_KEYS = 50_000


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


def is_listed(host: str | None, addresses: str) -> bool:
    """Whether `host` is one of `addresses`, comma-separated addresses or networks in the form
    uvicorn's forwarded_allow_ips takes."""
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    for entry in addresses.split(","):
        try:
            if address in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


class RateLimiter:
    """At most `limit` hits per `window`, for every (limit, window) pair, per key.

    Keys are kept in the order they were last hit, so idle ones are always at the front: each
    hit drops the idle keys there, and the cap drops the oldest, without scanning the rest."""

    def __init__(self, limits: list[tuple[int, timedelta]], max_keys: int = MAX_KEYS) -> None:
        self._limits = limits
        self._longest = max(window for _, window in limits)
        self._max_keys = max_keys
        self._hits: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: datetime) -> timedelta | None:
        """Counts a hit and returns None if `key` is within every limit; otherwise counts
        nothing and returns how long until it would be."""
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            self._hits[key] = hits  # now the most recent
            while hits and hits[0] <= now - self._longest:
                hits.popleft()
            wait = timedelta(0)
            for limit, window in self._limits:
                recent = [t for t in hits if t > now - window]
                if len(recent) >= limit:
                    wait = max(wait, recent[-limit] + window - now)
            if not wait:
                hits.append(now)
            self._forget_idle(now)
            return wait or None

    def __len__(self) -> int:
        return len(self._hits)

    def _forget_idle(self, now: datetime) -> None:
        idle = now - self._longest
        while self._hits:
            oldest_key = next(iter(self._hits))
            oldest = self._hits[oldest_key]
            if len(self._hits) <= self._max_keys and oldest and oldest[-1] > idle:
                return
            del self._hits[oldest_key]
