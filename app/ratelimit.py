"""메모리 기반 빈도 제한. 소수 사용자용이라 프로세스 안의 dict 로 충분하다.

- FailureLock: 로그인/가입 실패 횟수를 세고, 한도에 걸리면 잠시 잠근다.
- SlidingLimit: 일정 시간 창 안의 호출 횟수와 최소 간격을 제한한다(조회 API 용).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class FailureLock:
    def __init__(self, max_failures: int, window_sec: float, lock_sec: float) -> None:
        self.max_failures = max_failures
        self.window_sec = window_sec
        self.lock_sec = lock_sec
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        """잠겨 있으면 남은 초, 아니면 0."""
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            remain = until - time.monotonic()
            return remain if remain > 0 else 0.0

    def record_failure(self, key: str) -> float:
        """실패를 기록하고, 한도에 걸리면 잠근 뒤 잠금 시간을 돌려준다."""
        now = time.monotonic()
        with self._lock:
            q = self._fails[key]
            q.append(now)
            while q and q[0] < now - self.window_sec:
                q.popleft()
            if len(q) >= self.max_failures:
                self._locked_until[key] = now + self.lock_sec
                q.clear()
                return self.lock_sec
            return 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)


class SlidingLimit:
    def __init__(self, max_calls: int, window_sec: float, min_gap_sec: float = 0.0) -> None:
        self.max_calls = max_calls
        self.window_sec = window_sec
        self.min_gap_sec = min_gap_sec
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> str | None:
        """허용되면 기록하고 None, 아니면 거부 사유 문자열."""
        now = time.monotonic()
        with self._lock:
            q = self._calls[key]
            while q and q[0] < now - self.window_sec:
                q.popleft()
            if q and self.min_gap_sec and now - q[-1] < self.min_gap_sec:
                wait = self.min_gap_sec - (now - q[-1])
                return f"너무 빠릅니다. {wait:.0f}초 뒤에 다시 시도하세요."
            if len(q) >= self.max_calls:
                wait = self.window_sec - (now - q[0])
                return f"{int(self.window_sec // 60)}분에 {self.max_calls}회까지만 조회할 수 있습니다. {wait / 60:.0f}분 뒤에 다시 시도하세요."
            q.append(now)
            return None


# 로그인/가입: IP 또는 사용자명 기준 5분 안에 5회 실패 -> 1분 잠금
auth_lock = FailureLock(max_failures=5, window_sec=300, lock_sec=60)
# 수동 조회: 사용자당 최소 5초 간격, 10분에 30회
search_limit = SlidingLimit(max_calls=30, window_sec=600, min_gap_sec=5)
# '지금 한 번 점검': 사용자당 30초에 1회
run_now_limit = SlidingLimit(max_calls=1, window_sec=30)
