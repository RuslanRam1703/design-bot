"""Tiny bounded in-process short-burst protection, not a DDoS firewall.

No day quotas, no user bans, no Redis writes on every incoming request.
Render/Cloudflare protects the edge; this only reduces cheap bot/API floods.
Counts reset on process restart and are not shared between instances.
"""
import time
from collections import OrderedDict, deque


class BurstLimiter:
    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 4096):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._seen = OrderedDict()

    def allow(self, identity, *, now=None) -> bool:
        stamp = time.monotonic() if now is None else now
        queue = self._seen.pop(identity, None)
        if queue is None:
            queue = deque()
        cutoff = stamp - self.window_seconds
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if len(queue) >= self.limit:
            # Preserve its existing expiry without extending a ban on each hit.
            self._seen[identity] = queue
            return False
        queue.append(stamp)
        self._seen[identity] = queue
        if len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return True
