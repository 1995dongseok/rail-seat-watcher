"""감시 조건 저장 및 주기적 폴링.

각 감시 조건마다 '지금 빈자리가 있는 열차 집합'을 기억해 두고,
매진 -> 가능 으로 바뀐 열차가 생기면 소유자의 텔레그램으로 알린다.
출발일이 지나면 자동으로 감시를 끝낸다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from app import telegram
from app.config import DATA_DIR, now_kst, settings
from app.korail_service import SearchError, TrainSeat, get_service
from app.users import user_store

log = logging.getLogger(__name__)

WATCH_FILE = DATA_DIR / "watches.json"
GAP_BETWEEN_WATCHES_SEC = 3


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
        WATCH_FILE.write_text(
            json.dumps([asdict(w) for w in self._watches.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
        self.save()
        return w


class Watcher:
    def __init__(self, store: WatchStore) -> None:
        self.store = store
        self.last_cycle_at: datetime | None = None
        self.last_cycle_error: str | None = None
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
        log.info("감시 루프 시작 (주기 %ds)", settings.poll_interval_sec)
        while True:
            try:
                await self.run_once()
            except Exception as e:  # noqa: BLE001
                log.exception("감시 루프 오류")
                self.last_cycle_error = str(e)
            await asyncio.sleep(settings.poll_interval_sec)

    async def run_once(self, user_id: str | None = None) -> None:
        """활성 감시를 한 바퀴 점검. user_id 를 주면 그 사용자 것만."""
        async with self._running:  # 주기 점검과 '지금 점검'이 겹치지 않게
            today = now_kst().date().isoformat()
            for w in self.store.list(user_id):
                if not w.active:
                    continue
                if w.date < today:
                    w.active = False
                    w.last_result = "출발일 경과로 감시 종료"
                    self.store.save()
                    continue
                await self._check(w)
                await asyncio.sleep(GAP_BETWEEN_WATCHES_SEC)
            if user_id is None:
                self.last_cycle_at = now_kst()
                self.last_cycle_error = None

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
        try:
            svc = get_service(user)
            seats = await asyncio.to_thread(svc.search, w.dep, w.arr, w.date, w.time_from, w.time_to, w.train_type)
        except SearchError as e:
            w.last_result = f"조회 실패: {e}"
            self.store.save()
            return

        now_available = {s.key: s for s in seats if w.matches(s)}
        newly = [s for k, s in now_available.items() if k not in set(w.available_keys)]
        w.available_keys = sorted(now_available.keys())
        w.last_result = f"{len(seats)}편 조회, 빈자리 {len(now_available)}편"
        self.store.save()

        if newly:
            if not user.telegram_chat_id:
                w.last_result += " (텔레그램 chat_id 미설정으로 알림 생략)"
                self.store.save()
                return
            if await telegram.send_message(user.telegram_chat_id, _format_alert(w, newly)):
                w.notified_count += 1
                self.store.save()


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
