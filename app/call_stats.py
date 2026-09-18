"""사용자별 누적 조회(호출) 횟수. 관리자 페이지에 "이 사람이 지금까지 몇 번 조회했는지" 보여 주기 위한 것이다.

- korail_service / nol_service 가 실제로 코레일·NOL 로 호출을 보낼 때마다 record() 를 호출해 1씩 늘린다.
  감시 점검이든 수동 조회든, 로그인이든 페이지 조회든 나간 호출은 모두 센다.
- korail_service/nol_service 의 _call_log(deque, maxlen=5000)는 최근 부하 계산용이라 오래된 기록이 밀려나지만,
  이 모듈은 data/call_stats.json 에 저장해 서버를 재배포·재시작해도 누적치가 유지된다.
- 시스템이 사용자 없이 부르는 호출(예: 역 목록 조회)은 owner_id 가 비어 있어 세지 않는다.
"""

from __future__ import annotations

import json
import threading

from app.config import DATA_DIR, write_private

STATS_FILE = DATA_DIR / "call_stats.json"

_lock = threading.Lock()
_counts: dict[str, dict[str, int]] = {}  # user_id -> {"korail": n, "nol": n}


def _load() -> None:
    global _counts
    try:
        _counts = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        _counts = {}


def _save() -> None:
    write_private(STATS_FILE, json.dumps(_counts, ensure_ascii=False, indent=2))


def record(owner_id: str, kind: str) -> None:
    """호출 1건을 사용자 누적치에 더한다. kind 는 'korail' 또는 'nol'."""
    if not owner_id:
        return
    with _lock:
        row = _counts.setdefault(owner_id, {"korail": 0, "nol": 0})
        row[kind] = row.get(kind, 0) + 1
        _save()


def get(owner_id: str) -> dict[str, int]:
    row = _counts.get(owner_id, {})
    return {"korail": row.get("korail", 0), "nol": row.get("nol", 0)}


def remove(owner_id: str) -> None:
    """사용자 삭제 시 누적치도 함께 지운다."""
    with _lock:
        if _counts.pop(owner_id, None) is not None:
            _save()


_load()
