"""NOL 티켓(구 인터파크) 공연 잔여석 조회.

- 공식 API 가 아니라 NOL 웹/앱이 쓰는 비공개 JSON 엔드포인트를 그대로 호출한다. 예고 없이 바뀔 수 있다.
- 로그인, 쿠키, 토큰이 필요 없다. 사용자 계정 등록 없이 상품코드와 날짜만 있으면 된다.
- 호출은 서버 전체에서 GLOBAL_LOCK 으로 직렬화하고, 호출 사이에 잠깐 쉰다(레이트리밋 대응).
- 좌석 수는 '예매 가능한 잔여석 수'로 오며, 회차(playSeq)와 등급(seatGrade)별로 나온다.

엔드포인트
- 검색: POST https://nol.yanolja.com/discovery/api/list/universal-search/v2/list
- 상품 요약: GET https://api-ticketfront.interpark.com/v1/goods/{code}/summary
- 날짜별 잔여석: GET https://api-ticketfront.interpark.com/v1/goods/{code}/playSeq/PlayDate/{YYYYMMDD}/REMAINSEAT
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

import httpx

from app import call_stats
from app.config import now_kst

log = logging.getLogger(__name__)

SEARCH_URL = "https://nol.yanolja.com/discovery/api/list/universal-search/v2/list"
FRONT_API = "https://api-ticketfront.interpark.com/v1/goods/{code}"
PRODUCT_URL = "https://nol.yanolja.com/ticket/products/{code}"

GLOBAL_LOCK = threading.Lock()
GAP_SEC = 0.4  # 연속 호출 사이 간격
TIMEOUT = 15
MAX_SCAN_DAYS = 14  # '남은 기간 전체 조회' 시 하루 1회 호출이므로 상한을 둔다
CODE_RE = re.compile(r"^\d{5,12}$")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json",
    "Referer": "https://tickets.interpark.com/",
}

# ----------------------------------------------------------------- 호출 통계(관리자 부하 표시용)
_call_log: deque[tuple[float, str]] = deque(maxlen=5000)
_call_lock = threading.Lock()
total_calls = 0
_last_call_at = 0.0


def record_call(owner_id: str = "") -> None:
    global total_calls
    with _call_lock:
        _call_log.append((time.monotonic(), owner_id))
        total_calls += 1
    call_stats.record(owner_id, "nol")


def calls_in_last(seconds: float) -> int:
    cutoff = time.monotonic() - seconds
    with _call_lock:
        return sum(1 for t, _ in _call_log if t >= cutoff)


def calls_by_user_in_last(seconds: float) -> dict[str, int]:
    cutoff = time.monotonic() - seconds
    out: dict[str, int] = {}
    with _call_lock:
        for t, owner in _call_log:
            if t >= cutoff:
                out[owner] = out.get(owner, 0) + 1
    return out


class NolError(Exception):
    """화면에 그대로 보여줄 수 있는 실패 사유."""


@dataclass(frozen=True)
class Goods:
    code: str
    name: str
    place: str
    play_start: str  # YYYY-MM-DD
    play_end: str
    booking_end: str  # 'YYYY-MM-DD HH:MM' 또는 ''
    show_times: dict[str, list[str]] = field(default_factory=dict)  # {YYYY-MM-DD: ['20:00', ...]} 안내문에서 파싱
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SeatGrade:
    play_date: str  # YYYY-MM-DD
    play_seq: str
    grade_code: str
    grade_name: str
    remain: int

    @property
    def key(self) -> str:
        return f"{self.play_date}-{self.play_seq}-{self.grade_code}"

    @property
    def available(self) -> bool:
        return self.remain > 0

    def to_dict(self) -> dict:
        return asdict(self) | {"key": self.key, "available": self.available}


# ----------------------------------------------------------------- HTTP
def _get(url: str, owner_id: str = "", **kwargs) -> httpx.Response:
    global _last_call_at
    with GLOBAL_LOCK:
        wait = GAP_SEC - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        record_call(owner_id)
        try:
            r = httpx.get(url, headers=_HEADERS, timeout=TIMEOUT, **kwargs)
        except httpx.HTTPError as e:
            raise NolError(f"NOL 통신 오류: {e}") from e
        finally:
            _last_call_at = time.monotonic()
    if r.status_code != 200:
        raise NolError(f"NOL 응답 오류 (HTTP {r.status_code})")
    return r


def _front(code: str, path: str, owner_id: str = "") -> dict:
    r = _get(FRONT_API.format(code=code) + path, owner_id)
    try:
        body = r.json()
    except ValueError as e:
        raise NolError("NOL 응답을 해석할 수 없습니다 (API 가 바뀌었을 수 있음)") from e
    common = body.get("common") or {}
    if common.get("internalHttpStatusCode", 200) != 200 or body.get("data") is None:
        raise NolError(f"NOL 응답 실패: {common.get('message') or '데이터 없음'}")
    return body["data"]


# ----------------------------------------------------------------- 검색
def normalize_code(text: str) -> str | None:
    """상품코드 숫자, 또는 NOL/인터파크 상품 URL 에서 코드를 뽑는다. 아니면 None."""
    text = text.strip()
    if CODE_RE.match(text):
        return text
    m = re.search(r"/(?:products|goods)/(\d{5,12})", text)
    return m.group(1) if m else None


def search(keyword: str, owner_id: str = "", include_upcoming: bool = False) -> list[dict]:
    """공연명·아티스트로 검색. [{code, title, date_info, place}].

    기본은 지금 판매 중인 공연만(취소표는 판매 중인 공연에서만 나온다). include_upcoming 이면 판매 예정도 포함.
    NOL 검색은 빈 검색어를 받지 않고 한 번에 20건 정도만 준다.
    """
    global _last_call_at
    keyword = keyword.strip()
    if not keyword:
        return []
    statuses = ["ENTERTAINMENT_SALE_STATUS_CODE_ACTIVE"]
    if include_upcoming:
        statuses.insert(0, "ENTERTAINMENT_SALE_STATUS_CODE_UPCOMING")
    today = date.today()
    ymd = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731
    # NOL 통합검색은 숙박 검색과 요청 형식을 공유해 필수 필드가 많다. 브라우저가 보내는 형식 그대로.
    payload = {
        "keyword": keyword,
        "filter": {
            "codeFilter": {
                "reservationTypeCodes": [], "starRatingCodes": [], "accommodationCategoryCodes": [],
                "amenitiesCodes": [], "accommodationLocationCodes": [], "maxRentHourCodes": [],
                "accommodationPromotionCodes": [], "leisureLocationCodes": [], "leisureCategoryCodes": [],
                "leisureBrandCodes": [], "leisurePromotionCodes": [], "entertainmentCategoryCodes": [],
                "entertainmentRegionCodes": [],
                "saleStatusCodes": statuses,
                "entertainmentPropertyCodes": [], "entertainmentTopingPaidMemberDiscount": False,
                "entertainmentFutureShowDateCount": 0,
                "nolWorldDomesticStay": {"starRatingCodes": [], "facilityCodes": []},
            },
            "rangeFilter": {"priceRange": {"from": 0, "to": 0}, "entertainmentShowDateRanges": []},
            "productStatusFilter": {"availableOnly": False},
            "quickFilters": [],
            "useDynamicFilter": False,
            "globalAccommodationCodeFilter": {"rateAmenityCodes": [], "propertyBadgeCodes": [], "propertyAmenityCodes": []},
        },
        "category": "PRODUCT_CATEGORY_ENTERTAINMENT",
        "sort": "SORT_DEFAULT",
        "localAccommodation": {"checkInDate": ymd(today), "checkOutDate": ymd(today + timedelta(days=1)), "capacityAdults": 2, "childrenAges": []},
        "globalAccommodation": {"checkInDate": ymd(today), "checkOutDate": ymd(today + timedelta(days=1)), "rooms": [{"capacityAdults": 2, "childrenAges": []}]},
        "disableSpellCorrection": False,
    }
    with GLOBAL_LOCK:
        wait = GAP_SEC - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        record_call(owner_id)
        try:
            r = httpx.post(SEARCH_URL, json=payload, headers=_HEADERS | {"Referer": "https://nol.yanolja.com/ticket"}, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise NolError(f"NOL 통신 오류: {e}") from e
        finally:
            _last_call_at = time.monotonic()
    if r.status_code != 200:
        raise NolError(f"NOL 검색 실패 (HTTP {r.status_code})")
    try:
        body = r.json()
    except ValueError as e:
        raise NolError("NOL 검색 응답을 해석할 수 없습니다") from e
    items: list[dict] = []
    seen: set[str] = set()
    for p in _walk_product_items(body):
        code = str(p.get("id") or "")
        if not CODE_RE.match(code) or code in seen:
            continue
        seen.add(code)
        loc = p.get("locationDetails")
        if isinstance(loc, list):
            loc = " ".join(str(x) for x in loc if x)
        items.append({
            "code": code,
            "title": str(p.get("title") or ""),
            "date_info": str(p.get("dateInfo") or ""),
            "place": str(loc or ""),
            "url": PRODUCT_URL.format(code=code),
        })
    return items


def _walk_product_items(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("productItem"), dict):
            yield obj["productItem"]
        for v in obj.values():
            yield from _walk_product_items(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_product_items(v)


# ----------------------------------------------------------------- 상품 요약
_goods_cache: dict[str, tuple[float, Goods]] = {}
GOODS_CACHE_SEC = 600


def get_goods(code: str, owner_id: str = "") -> Goods:
    """상품 요약. 공연명, 장소, 공연 기간, 예매 마감. 10분 캐시."""
    cached = _goods_cache.get(code)
    if cached and time.monotonic() - cached[0] < GOODS_CACHE_SEC:
        return cached[1]
    d = _front(code, "/summary", owner_id)
    name = str(d.get("goodsName") or "")
    if not name or d.get("goodsStatus") not in (None, "Y"):
        raise NolError("판매 중인 상품이 아니거나 상품코드가 잘못되었습니다")
    g = Goods(
        code=code,
        name=name,
        place=str(d.get("placeName") or ""),
        play_start=_ymd(d.get("playStartDate")),
        play_end=_ymd(d.get("playEndDate")),
        booking_end=_ymdhm(d.get("bookingEndDate")),
        show_times=_parse_show_times(str(d.get("playTime") or "")),
        url=PRODUCT_URL.format(code=code),
    )
    _goods_cache[code] = (time.monotonic(), g)
    return g


def _ymd(v) -> str:
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else ""


def _ymdhm(v) -> str:
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}" if len(s) >= 12 and s[:12].isdigit() else ""


_SHOW_RE = re.compile(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일(?:\([^)]*\))?\s*(오전|오후)?\s*(\d{1,2})시(?:\s*(\d{1,2})분)?")


def _parse_show_times(text: str) -> dict[str, list[str]]:
    """'2026년 9월 11일(금) 오후 8시 | 9월 12일(토) 오후 5시' 같은 안내문에서 날짜별 시각을 뽑는다. 실패해도 빈 dict."""
    out: dict[str, list[str]] = {}
    year = None
    for m in _SHOW_RE.finditer(text):
        y, mo, d, ampm, h, mi = m.groups()
        if y:
            year = int(y)
        if year is None:
            continue
        hour = int(h) % 12 + (12 if ampm == "오후" else 0)
        if ampm is None and int(h) >= 12:
            hour = int(h)
        try:
            key = date(year, int(mo), int(d)).isoformat()
        except ValueError:
            continue
        out.setdefault(key, []).append(f"{hour:02d}:{int(mi or 0):02d}")
    return out


# ----------------------------------------------------------------- 잔여석
def remaining_by_date(code: str, play_date: str, owner_id: str = "") -> list[SeatGrade]:
    """특정 날짜(YYYY-MM-DD)의 모든 회차 × 등급 잔여석. 호출 1회."""
    d = _front(code, f"/playSeq/PlayDate/{play_date.replace('-', '')}/REMAINSEAT", owner_id)
    rows = d.get("remainSeat") or []
    out = []
    for r in rows:
        try:
            remain = int(r.get("remainCnt") or 0)
        except (TypeError, ValueError):
            remain = 0
        out.append(SeatGrade(
            play_date=play_date,
            play_seq=str(r.get("playSeq") or ""),
            grade_code=str(r.get("seatGrade") or ""),
            grade_name=str(r.get("seatGradeName") or r.get("seatGrade") or ""),
            remain=remain,
        ))
    return sorted(out, key=lambda s: (s.play_seq, s.grade_code))


def scan_dates(goods: Goods, date_from: str | None = None, date_to: str | None = None, owner_id: str = "") -> list[SeatGrade]:
    """남은 공연 기간을 하루씩 조회해 이어 붙인다. 하루 1회 호출이므로 MAX_SCAN_DAYS 로 자른다."""
    today = now_kst().date()
    start = max(_to_date(date_from) or today, _to_date(goods.play_start) or today, today)
    end = min(_to_date(date_to) or date.max, _to_date(goods.play_end) or date.max)
    if end < start:
        raise NolError("조회 날짜가 공연 기간 밖입니다")
    if (end - start).days + 1 > MAX_SCAN_DAYS:
        end = start + timedelta(days=MAX_SCAN_DAYS - 1)
    out: list[SeatGrade] = []
    cur = start
    while cur <= end:
        out.extend(remaining_by_date(goods.code, cur.isoformat(), owner_id))
        cur += timedelta(days=1)
    return out


def _to_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
