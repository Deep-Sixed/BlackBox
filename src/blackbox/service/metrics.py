"""In-process counters, gauges and a latency histogram in Prometheus text format.

Label values are restricted to fixed, code-defined vocabularies (route names,
status codes, reason codes, principal names from the principals file) so a
caller can never inject text into the metrics output.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict

LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.:/{}-]{1,64}$")
BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    for value in labels.values():
        if not LABEL_VALUE.match(value):
            raise ValueError("unsafe metric label")
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = defaultdict(float)
        self._gauges: dict[tuple[str, str], float] = {}
        self._histogram = [0] * (len(BUCKETS) + 1)
        self._histogram_sum = 0.0
        self.started = time.time()

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] += amount

    def set(self, name: str, value: float, **labels: str) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._gauges[key] = value

    def observe(self, seconds: float) -> None:
        with self._lock:
            index = next(
                (i for i, bound in enumerate(BUCKETS) if seconds <= bound),
                len(BUCKETS),
            )
            self._histogram[index] += 1
            self._histogram_sum += seconds

    def counter(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters.get((name, _labels(labels)), 0.0)

    def render(self) -> str:
        with self._lock:
            lines = []
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{labels} {value:g}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"{name}{labels} {value:g}")
            cumulative = 0
            for bound, count in zip(BUCKETS, self._histogram, strict=False):
                cumulative += count
                lines.append(
                    f'blackbox_request_seconds_bucket{{le="{bound:g}"}} {cumulative}'
                )
            cumulative += self._histogram[-1]
            lines.append(f'blackbox_request_seconds_bucket{{le="+Inf"}} {cumulative}')
            lines.append(f"blackbox_request_seconds_sum {self._histogram_sum:g}")
            lines.append(f"blackbox_request_seconds_count {cumulative}")
            lines.append(f"blackbox_process_start_time_seconds {self.started:g}")
            return "\n".join(lines) + "\n"


class RateLimiter:
    """Token buckets per (principal, class); thread-safe."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def allow(self, key: tuple[str, str], rate: tuple[float, int], cost=1) -> bool:
        per_second, burst = rate
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * per_second)
            if tokens < cost:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - cost, now)
            return True
