"""감시 조건 저장 및 주기적 폴링.

- 루프는 TICK_SEC 마다 깨어나, 각 감시의 마지막 점검 시각 + 소유자의 감시 주기가 지난 것만 점검한다.
  그래서 사용자마다 다른 주기를 가질 수 있다.
- 각 감시마다 '지금 빈자리가 있는 열차 집합'을 기억해 두고, 매진 -> 가능 으로 바뀐 열차가 생기면
  소유자의 텔레그램으로 알린다. 출발일이 지나면 자동으로 감시를 끝낸다.
- 코레일 호출은 korail_service 의 전역 잠금으로 서버 전체에서 하나씩만 나가고, 감시 건 사이에 3초를 쉰다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime

from app import korail_service, telegram
from app.config import DATA_DIR, now_kst, write_private
from app.korail_service import SearchError, TrainSeat, get_service
from app.users import user_store

log = logging.getLogger(__name__)

WATCH_FILE = DATA_DIR / "watches.json"
GAP_BETWEEN_WATCHES_SEC = 3
TICK_SEC = 10  # 루프가 깨어나 '점검할 때가 된 감시'를 찾는 간격
LOAD_LIMIT_PER_MIN = 30  # 서버 IP 하나에서 코레일로 나가는 호출의 권장 상한(분당)


@dataclass
class Watch:
    id: str
    user_id: str
    dep: str
    arr: str
    date: str  # YYYY-MM-DD
    time_from: str | None  # HH:MM 또는 None(전체)
    time_to: str | None
    train_type: str = "ALL"
    seat_pref: str = "ANY"  # ANY / GENERAL / SPECIAL
    active: bool = True
    created_at: str = field(default_factory=lambda: now_kst().isoformat(timespec="seconds"))
    last_checked_at: str | None = None
    last_result: str | None = None
    last_calls: int = 0  # 마지막 점검에 쓴 코레일 호출 수(부하 예측용)
    available_keys: list[str] = field(default_factory=list)
    notified_count: int = 0

    def label(self) -> str:
        when = "전체" if not self.time_from and not self.time_to else f"{self.time_from or '00:00'}~{self.time_to or '23:59'}"
        return f"{self.dep}→{self.arr} {self.date} {when} {self.train_type}"

    def matches(self, seat: TrainSeat) -> bool:
        if self.seat_pref == "GENERAL":
            return seat.general_available
        if self.seat_pref == "SPECIAL":
            return seat.special_available
        return seat.any_available

    def to_dict(self) -> dict:
        return asdict(self) | {"label": self.label()}

    def seconds_since_check(self) -> float | None:
        if not self.last_checked_at:
            return None
        try:
            return (now_kst() - datetime.fromisoformat(self.last_checked_at)).total_seconds()
        except ValueError:
            return None


class WatchStore:
    def __init__(self) -> None:
        self._watches: dict[str, Watch] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for item in raw:
            item.setdefault("user_id", "")
            try:
                w = Watch(**item)
            except TypeError:
                continue
            self._watches[w.id] = w

    def save(self) -> None:
        write_private(WATCH_FILE, json.dumps([asdict(w) for w in self._watches.values()], ensure_ascii=False, indent=2))

    def list(self, user_id: str | None = None) -> list[Watch]:
        items = [w for w in self._watches.values() if user_id is None or w.user_id == user_id]
        return sorted(items, key=lambda w: (not w.active, w.date, w.time_from or ""))

    def get(self, watch_id: str) -> Watch | None:
        return self._watches.get(watch_id)

    def active_count(self, user_id: str) -> int:
        return sum(1 for w in self._watches.values() if w.user_id == user_id and w.active)

    def add(self, **kwargs) -> Watch:
        w = Watch(id=uuid.uuid4().hex[:8], **kwargs)
        self._watches[w.id] = w
        self.save()
        return w

    def remove(self, watch_id: str) -> bool:
        if self._watches.pop(watch_id, None) is None:
            return False
        self.save()
        return True

    def remove_by_user(self, user_id: str) -> int:
        ids = [w.id for w in self._watches.values() if w.user_id == user_id]
        for i in ids:
            self._watches.pop(i, None)
        if ids:
            self.save()
        return len(ids)

    def set_active(self, watch_id: str, active: bool) -> Watch | None:
        w = self._watches.get(watch_id)
        if w is None:
            return None
        w.active = active
        if active:
            w.available_keys = []
            w.last_checked_at = None  # 재개하면 다음 틱에 바로 점검
        self.save()
        return w


class Watcher:
    def __init__(self, store: WatchStore) -> None:
        self.store = store
        self.last_cycle_at: datetime | None = None  # 마지막으로 감시를 1건 이상 점검한 시각
        self.last_cycle_error: str | None = None
        self.last_cycle: dict | None = None  # {"at", "watches", "calls", "duration_sec"}
        self.cycle_history: deque[dict] = deque(maxlen=20)
        self._task: asyncio.Task | None = None
        self._running = asyncio.Lock()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        log.info("감시 루프 시작 (틱 %ds, 주기는 사용자별)", TICK_SEC)
        while True:
            try:
                await self.run_due()
            except Exception as e:  # noqa: BLE001
                log.exception("감시 루프 오류")
                self.last_cycle_error = str(e)
            await asyncio.sleep(TICK_SEC)

    # ------------------------------------------------------------- 실행
    def _is_due(self, w: Watch) -> bool:
        user = user_store.get(w.user_id)
        if user is None:
            return True  # 소유자 없음 처리를 위해 점검 경로로 보낸다
        since = w.seconds_since_check()
        return since is None or since >= user.effective_poll_interval

    async def run_due(self) -> None:
        """점검할 때가 된 활성 감시만 점검한다."""
        await self._run_batch([w for w in self.store.list() if w.active and self._is_due(w)])

    async def run_once(self, user_id: str | None = None) -> None:
        """주기와 무관하게 지금 점검. user_id 를 주면 그 사용자 것만('지금 한 번 점검' 버튼)."""
        await self._run_batch([w for w in self.store.list(user_id) if w.active])

    async def _run_batch(self, watches: list[Watch]) -> None:
        async with self._running:  # 주기 점검과 '지금 점검'이 겹치지 않게
            today = now_kst().date().isoformat()
            started = time.monotonic()
            calls_before = korail_service.total_calls
            checked = 0
            for w in watches:
                if w.date < today:
                    w.active = False
                    w.last_result = "출발일 경과로 감시 종료"
                    self.store.save()
                    continue
                await self._check(w)
                checked += 1
                await asyncio.sleep(GAP_BETWEEN_WATCHES_SEC)
            if checked:
                self.last_cycle_at = now_kst()
                self.last_cycle_error = None
                self.last_cycle = {
                    "at": self.last_cycle_at.isoformat(timespec="seconds"),
                    "watches": checked,
                    "calls": korail_service.total_calls - calls_before,
                    "duration_sec": round(time.monotonic() - started, 1),
                }
                self.cycle_history.append(self.last_cycle)

    async def _check(self, w: Watch) -> None:
        w.last_checked_at = now_kst().isoformat(timespec="seconds")
        user = user_store.get(w.user_id)
        if user is None:
            w.active = False
            w.last_result = "소유자 없음, 감시 종료"
            self.store.save()
            return
        if not user.allowed:
            w.last_result = "사용 거부 상태라 점검 건너뜀 (관리자 승인 필요)"
            self.store.save()
            return
        calls_before = korail_service.total_calls
        try:
            svc = get_service(user)
            seats = await asyncio.to_thread(svc.search, w.dep, w.arr, w.date, w.time_from, w.time_to, w.train_type)
        except SearchError as e:
            w.last_calls = korail_service.total_calls - calls_before
            w.last_result = f"조회 실패: {e}"
            self.store.save()
            return
        w.last_calls = korail_service.total_calls - calls_before

        now_available = {s.key: s for s in seats if w.matches(s)}
        newly = [s for k, s in now_available.items() if k not in set(w.available_keys)]
        w.available_keys = sorted(now_available.keys())
        w.last_result = f"{len(seats)}편 조회, 빈자리 {len(now_available)}편"
        self.store.save()

        if newly:
            if not user.telegram_chat_id:
                w.last_result += " (텔레그램 미연결으로 알림 생략)"
                self.store.save()
                return
            if await telegram.send_message(user.telegram_chat_id, _format_alert(w, newly)):
                w.notified_count += 1
                self.store.save()

    # ------------------------------------------------------------- 부하 예측
    def projected_load(self) -> dict:
        """현재 활성 감시들이 각자의 주기로 돌 때 코레일로 나가는 예상 호출 수(분당)와 사용자별 내역.

        사용자별 행에는 실측(최근 10분, 감시 + 수동 조회 포함)도 함께 붙인다.
        """
        measured = korail_service.calls_by_user_in_last(600)
        per_user: dict[str, dict] = {}
        for user in user_store.list():
            per_user[user.id] = {
                "username": user.username,
                "allowed": user.allowed,
                "interval": user.effective_poll_interval,
                "watches": 0,
                "calls_per_cycle": 0,
                "per_min": 0.0,
                "measured_10min": measured.get(user.id, 0),
            }
        total = 0.0
        for w in self.store.list():
            if not w.active:
                continue
            user = user_store.get(w.user_id)
            if user is None or not user.allowed:
                continue
            calls = max(1, w.last_calls)  # 아직 점검 전이면 최소 1회로 가정
            per_min = calls * 60 / user.effective_poll_interval
            total += per_min
            row = per_user[user.id]
            row["watches"] += 1
            row["calls_per_cycle"] += calls
            row["per_min"] += per_min
        rows = []
        for row in per_user.values():
            row["per_min"] = round(row["per_min"], 1)
            row["measured_per_min"] = round(row["measured_10min"] / 10, 1)
            row["share"] = round(row["per_min"] / total * 100) if total else 0
            if row["watches"] or row["measured_10min"]:
                rows.append(row)
        rows.sort(key=lambda r: (-r["per_min"], -r["measured_10min"]))
        return {"per_min": round(total, 1), "limit": LOAD_LIMIT_PER_MIN, "users": rows}


def _format_alert(w: Watch, seats: list[TrainSeat]) -> str:
    lines = [f"🚄 빈자리 발생: {w.dep} → {w.arr} {w.date}"]
    for s in seats:
        gen = "일반실 O" if s.general_available else "일반실 X"
        spe = "특실 O" if s.special_available else "특실 X"
        lines.append(f"- {s.train_type_name} {s.train_no}  {s.dep_time[:2]}:{s.dep_time[2:4]}→{s.arr_time[:2]}:{s.arr_time[2:4]}  {gen} / {spe}")
    lines.append("예매: https://www.korail.com")
    return "\n".join(lines)


watch_store = WatchStore()
watcher = Watcher(watch_store)
