"""pykorail 을 감싼 조회 서비스. 사용자마다 인스턴스 하나.

- 로그인은 최초 조회 시 지연 수행하고, 세션 만료(NeedToLoginError)면 한 번 재로그인한다.
- 코레일 호출은 서버 전체에서 하나의 잠금(GLOBAL_LOCK)으로 직렬화한다. 사용자가 여러 명이어도
  한 서버 IP 에서 동시에 여러 요청이 나가지 않게 하기 위해서다.
- 기기 프로필은 사용자별로 고정한다. 여러 계정이 같은 기기 프로필을 쓰면 탐지 신호가 된다.
- 코레일 조회 API 는 한 번에 최대 10편 정도만 주므로, 하루 전체 조회는
  마지막 열차 출발시각 +1분 으로 반복 호출해 이어 붙인다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from pykorail import Korail
from pykorail.device import profile_by_id, random_profile
from pykorail.exceptions import (
    KorailError,
    NeedToLoginError,
    NoResultsError,
    PastDepartureError,
    PykorailError,
    StationNotFoundError,
)
from pykorail.options import TrainType

from app import call_stats
from app.config import now_kst

log = logging.getLogger(__name__)

TRAIN_TYPE_CODES: dict[str, str] = {
    "ALL": TrainType.ALL,
    "KTX": TrainType.KTX,
    "ITX_SAEMAEUL": TrainType.ITX_SAEMAEUL,
    "MUGUNGHWA": TrainType.MUGUNGHWA,
    "ITX_CHEONGCHUN": TrainType.ITX_CHEONGCHUN,
}

MAX_PAGES = 12  # 하루 전체 조회 시 최대 반복 호출 횟수(안전장치)
GLOBAL_LOCK = threading.Lock()

# ----------------------------------------------------------------- 호출 통계
# 코레일로 나간 호출(로그인 + 조회 페이지)의 시각과 사용자를 기록한다. 관리자 페이지의 부하 표시용.
_call_log: deque[tuple[float, str]] = deque(maxlen=5000)
_call_lock = threading.Lock()
total_calls = 0


def record_call(owner_id: str = "") -> None:
    global total_calls
    with _call_lock:
        _call_log.append((time.monotonic(), owner_id))
        total_calls += 1
    call_stats.record(owner_id, "korail")


def calls_in_last(seconds: float) -> int:
    cutoff = time.monotonic() - seconds
    with _call_lock:
        return sum(1 for t, _ in _call_log if t >= cutoff)


def calls_by_user_in_last(seconds: float) -> dict[str, int]:
    """최근 N초 동안 사용자별 코레일 호출 수(감시 + 수동 조회 모두 포함). 키는 user id."""
    cutoff = time.monotonic() - seconds
    out: dict[str, int] = {}
    with _call_lock:
        for t, owner in _call_log:
            if t >= cutoff:
                out[owner] = out.get(owner, 0) + 1
    return out

FALLBACK_STATIONS = [
    "서울", "용산", "광명", "수서", "영등포", "수원", "평택", "천안아산", "천안", "오송", "조치원", "대전", "서대전",
    "김천구미", "구미", "동대구", "대구", "경주", "울산(통도사)", "포항", "경산", "밀양", "부산", "구포", "창원중앙",
    "평창", "진부(오대산)", "강릉", "익산", "전주", "광주송정", "목포", "순천", "청량리", "여수EXPO", "동해", "정동진",
    "안동", "서원주", "원주", "행신", "인천공항T1", "인천공항T2", "검암", "춘천", "남춘천",
]


class SearchError(Exception):
    """화면에 그대로 보여줄 수 있는 조회 실패 사유."""


@dataclass(frozen=True)
class TrainSeat:
    train_type_name: str
    train_no: str
    dep_name: str
    arr_name: str
    dep_date: str  # YYYYMMDD
    dep_time: str  # HHMMSS
    arr_time: str  # HHMMSS
    general_available: bool
    special_available: bool
    waiting_list: bool

    @property
    def key(self) -> str:
        return f"{self.dep_date}-{self.train_no}"

    @property
    def any_available(self) -> bool:
        return self.general_available or self.special_available

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["any_available"] = self.any_available
        d["dep_hhmm"] = self.dep_time[:2] + ":" + self.dep_time[2:4]
        d["arr_hhmm"] = self.arr_time[:2] + ":" + self.arr_time[2:4]
        return d


# ----------------------------------------------------------------- 역 목록(공용)
_stations_cache: list[dict] | None = None


def load_stations() -> list[dict]:
    """역 목록 [{name, major}]. 로그인 없이 받아오며, 실패 시 내장 목록을 준다."""
    global _stations_cache
    if _stations_cache is not None:
        return _stations_cache
    with GLOBAL_LOCK:
        if _stations_cache is not None:
            return _stations_cache
        temp = None
        try:
            temp = Korail(device_profile=random_profile(), validate_stations=False)
            items = []
            for s in temp.stations.all():
                try:
                    major = int(s.major) if s.major else 0
                except ValueError:
                    major = 0
                items.append({"name": s.name, "major": major})
            if not items:
                raise ValueError("빈 역 목록")
            _stations_cache = items
        except Exception as e:  # noqa: BLE001
            log.warning("역 목록 조회 실패, 내장 목록 사용: %s", e)
            return [{"name": n, "major": i + 1} for i, n in enumerate(FALLBACK_STATIONS)]
        finally:
            if temp is not None:
                try:
                    temp.close()
                except Exception:  # noqa: BLE001
                    pass
    return _stations_cache


# ----------------------------------------------------------------- 사용자별 서비스
class KorailService:
    def __init__(
        self, korail_id: str, korail_pw: str, device_profile_id: str, owner: str = "", owner_id: str = ""
    ) -> None:
        self.korail_id = korail_id
        self.korail_pw = korail_pw
        self.owner = owner
        self.owner_id = owner_id
        self._device_profile = profile_by_id(device_profile_id) or random_profile()
        self._korail: Korail | None = None
        self.last_error: str | None = None
        self.last_login_at: datetime | None = None

    # ------------------------------------------------------------- 세션
    @property
    def logged_in(self) -> bool:
        return self._korail is not None and self._korail.logined

    def _ensure_login(self) -> Korail:
        if self._korail is not None and self._korail.logined:
            return self._korail
        if not (self.korail_id and self.korail_pw):
            raise SearchError("내 설정에서 코레일 아이디와 비밀번호를 먼저 등록하세요.")
        self.close()
        log.info("[%s] 코레일 로그인 시도", self.owner)
        record_call(self.owner_id)
        korail = Korail.logged_in(
            self.korail_id, self.korail_pw, device_profile=self._device_profile, validate_stations=True
        )
        self._korail = korail
        self.last_login_at = now_kst()
        log.info("[%s] 코레일 로그인 성공", self.owner)
        return korail

    def test_login(self) -> str:
        """로그인만 시도하고 코레일 회원 이름을 돌려준다."""
        with GLOBAL_LOCK:
            self._korail = None
            korail = self._call(self._ensure_login)
            self.last_error = None
            return korail.name or ""

    def close(self) -> None:
        if self._korail is not None:
            try:
                self._korail.close()
            except Exception:  # noqa: BLE001
                pass
            self._korail = None

    def logout(self) -> None:
        with GLOBAL_LOCK:
            if self._korail is not None:
                try:
                    self._korail.logout()
                except Exception:  # noqa: BLE001
                    pass
            self.close()

    # ------------------------------------------------------------- 조회
    def search(
        self,
        dep: str,
        arr: str,
        date: str,
        time_from: str | None,
        time_to: str | None,
        train_type: str = "ALL",
    ) -> list[TrainSeat]:
        """date: YYYY-MM-DD, time_from/time_to: HH:MM 또는 None(전체).

        time_from 이 None 이면 00:00 부터, time_to 가 None 이면 23:59 까지.
        오늘 날짜면 지금 시각 이전은 자동으로 잘라낸다.
        """
        code = TRAIN_TYPE_CODES.get(train_type.upper(), TrainType.ALL)
        start = _combine(date, time_from or "00:00")
        end = _combine(date, time_to or "23:59")
        now = now_kst()
        if start < now:
            start = now + timedelta(minutes=1)
        if end <= start:
            raise SearchError("조회 시간 범위가 이미 지났거나 잘못되었습니다.")

        with GLOBAL_LOCK:
            try:
                return self._search_range(dep, arr, start, end, code)
            except NeedToLoginError:
                log.info("[%s] 세션 만료, 재로그인", self.owner)
                self._korail = None
                return self._search_range(dep, arr, start, end, code)

    def _search_range(self, dep, arr, start: datetime, end: datetime, code: str) -> list[TrainSeat]:
        korail = self._call(self._ensure_login)
        results: dict[str, TrainSeat] = {}
        cursor = start
        for _ in range(MAX_PAGES):
            record_call(self.owner_id)
            try:
                trains = self._call(
                    korail.trains.search,
                    dep,
                    arr,
                    depart_after=cursor,
                    train_type=code,
                    include_no_seats=True,
                    include_waiting_list=True,
                )
            except NoResultsError:
                break
            except PastDepartureError:
                cursor = now_kst() + timedelta(minutes=1)
                continue
            new_found = False
            last_dep: datetime | None = None
            for t in trains:
                seat = TrainSeat(
                    train_type_name=t.train_type_name,
                    train_no=t.train_no,
                    dep_name=t.dep_name,
                    arr_name=t.arr_name,
                    dep_date=t.dep_date,
                    dep_time=t.dep_time,
                    arr_time=t.arr_time,
                    general_available=t.has_general_seat(),
                    special_available=t.has_special_seat(),
                    waiting_list=t.has_waiting_list(),
                )
                dep_dt = _parse(t.dep_date, t.dep_time)
                if dep_dt is None or dep_dt.date() != start.date():
                    continue
                last_dep = dep_dt if last_dep is None or dep_dt > last_dep else last_dep
                if dep_dt > end:
                    continue
                if seat.key not in results:
                    results[seat.key] = seat
                    new_found = True
            if not trains or last_dep is None or not new_found or last_dep >= end:
                break
            cursor = last_dep + timedelta(minutes=1)
        self.last_error = None
        return sorted(results.values(), key=lambda s: s.dep_time)

    def _call(self, fn, *args, **kwargs):
        """pykorail 예외를 화면용 SearchError 로 번역한다. NeedToLogin/NoResults 는 통과."""
        try:
            return fn(*args, **kwargs)
        except (NeedToLoginError, NoResultsError, PastDepartureError):
            raise
        except SearchError as e:
            self.last_error = str(e)
            raise
        except StationNotFoundError as e:
            self.last_error = f"역 이름 오류: {e}"
            raise SearchError(self.last_error) from e
        except KorailError as e:
            self.last_error = f"코레일 응답 오류: {e}"
            raise SearchError(self.last_error) from e
        except PykorailError as e:
            self.last_error = f"통신 오류: {e}"
            raise SearchError(self.last_error) from e


# ----------------------------------------------------------------- 레지스트리
_registry: dict[str, KorailService] = {}
_registry_lock = threading.Lock()


def get_service(user) -> KorailService:
    """사용자별 서비스. 코레일 계정이 바뀌었으면 새로 만든다."""
    pw = user.korail_pw
    with _registry_lock:
        svc = _registry.get(user.id)
        if svc is not None and (svc.korail_id != user.korail_id or svc.korail_pw != pw):
            svc.close()
            svc = None
        if svc is None:
            svc = KorailService(user.korail_id, pw, user.device_profile_id, owner=user.username, owner_id=user.id)
            _registry[user.id] = svc
        return svc


def drop_service(user_id: str) -> None:
    with _registry_lock:
        svc = _registry.pop(user_id, None)
    if svc is not None:
        svc.close()


def close_all() -> None:
    with _registry_lock:
        services = list(_registry.values())
        _registry.clear()
    for svc in services:
        svc.logout()


def _combine(date: str, hhmm: str) -> datetime:
    try:
        return datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M")
    except ValueError as e:
        raise SearchError(f"날짜/시간 형식 오류: {date} {hhmm}") from e


def _parse(yyyymmdd: str, hhmmss: str) -> datetime | None:
    try:
        return datetime.strptime(f"{yyyymmdd} {hhmmss[:6]}", "%Y%m%d %H%M%S")
    except ValueError:
        return None
