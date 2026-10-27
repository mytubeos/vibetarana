"""Bounded, process-local playback observations; no invented load benchmarks."""
import math
import os
import resource
import time
from collections import Counter, deque

class Metrics:
    def __init__(self):
        self.started = time.monotonic()
        self.counts = Counter()
        self.latency = deque(maxlen=1000)

    def record(self, outcome, seconds):
        self.counts[outcome] += 1
        if outcome == 'success':
            self.latency.append(seconds)

    def snapshot(self):
        values = sorted(self.latency)
        def percentile(p):
            return round(values[max(0, math.ceil(len(values) * p) - 1)], 3) if values else None
        total = self.counts['success'] + self.counts['failed']
        return {'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'host_load_1m': os.getloadavg()[0], 'uptime_seconds': int(time.monotonic() - self.started),
                'play_attempts': dict(self.counts), 'success_rate': self.counts['success'] / total if total else None,
                'successful_start_seconds_p50': percentile(.5), 'successful_start_seconds_p95': percentile(.95),
                'latency_samples': len(values), 'window': 'current process; latest 1000 successes'}

metrics = Metrics()
