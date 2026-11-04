"""Protections for running the server where strangers can reach it."""
import threading
import time

MAX_TRACKED = 10000


class FailureLimiter:
    """Locks a client out after too many wrong admin tokens, so the token can't be guessed by brute force.
    Clients are told apart by address; behind a reverse proxy see TRUST_FORWARDED_FOR."""

    def __init__(self, max_failures=5, window=600):
        self.max_failures, self.window = max_failures, window
        self._failures = {}
        self._lock = threading.Lock()

    def _recent(self, client, now):
        recent = [t for t in self._failures.get(client, ()) if now - t < self.window]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return recent

    def retry_after(self, client, now=None):
        """Seconds until this client may try again, or 0 if it isn't locked out."""
        now = time.time() if now is None else now
        with self._lock:
            recent = self._recent(client, now)
            if len(recent) >= self.max_failures:
                return max(1, int(recent[-self.max_failures] + self.window - now) + 1)
            return 0

    def record_failure(self, client, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if len(self._failures) >= MAX_TRACKED:  # bound memory: forget clients whose failures have all expired
                for other in list(self._failures):
                    self._recent(other, now)
                if len(self._failures) >= MAX_TRACKED:
                    self._failures.pop(next(iter(self._failures)))
            self._failures.setdefault(client, []).append(now)

    def clear(self, client):
        with self._lock:
            self._failures.pop(client, None)
