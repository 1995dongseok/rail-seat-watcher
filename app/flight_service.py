"""SerpApi Google Flights 조회. 날짜 후보와 실제 귀국편 조회를 분리한다."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import now_kst, settings
from app.ratelimit import SlidingLimit


class FlightError(Exception):
    pass


class FlightRequest(BaseModel):
    start: date
    end: date
    origins: list[Literal["ICN", "GMP", "CJJ"]] = Field(min_length=1, max_length=3)
    destinations: list[str] = Field(default_factory=lambda: ["PVG", "SHA"], min_length=1, max_length=3)
    nights: int = Field(default=3, ge=1, le=14)
    weekend: bool = True
    outbound_from: str = "06:00"
    outbound_to: str = "14:00"
    return_from: str = "12:00"
    return_to: str = "20:00"
    max_stops: int = Field(default=0, ge=0, le=1)
    adults: int = Field(default=1, ge=1, le=9)

    @field_validator("destinations")
    @classmethod
    def airports(cls, values):
        values = list(dict.fromkeys(v.strip().upper() for v in values))
        if any(len(v) != 3 or not v.isascii() or not v.isalpha() for v in values):
            raise ValueError("도착 공항은 PVG, SHA처럼 영문 3자리 코드를 입력하세요")
        return values

    @field_validator("outbound_from", "outbound_to", "return_from", "return_to")
    @classmethod
    def hours(cls, value):
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("시간은 HH:MM 형식이어야 합니다") from exc
        if parsed.strftime("%H:%M") != value:
            raise ValueError("시간은 HH:MM 형식이어야 합니다")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if not 0 <= (self.end - self.start).days <= 30:
            raise ValueError("검색 기간은 시작일부터 최대 31일입니다")
        if self.end < now_kst().date():
            raise ValueError("이미 지난 기간은 검색할 수 없습니다")
        if self.outbound_from > self.outbound_to or self.return_from > self.return_to:
            raise ValueError("종료 시간은 시작 시간보다 빠를 수 없습니다")
        if set(self.origins) & set(self.destinations):
            raise ValueError("출발 공항과 도착 공항은 달라야 합니다")
        return self

    def dates(self):
        day = max(self.start, now_kst().date())
        result = []
        while day + timedelta(days=self.nights) <= self.end:
            back = day + timedelta(days=self.nights)
            weekdays = {(day + timedelta(days=n)).weekday() for n in range(self.nights + 1)}
            if not self.weekend or {5, 6} <= weekdays:
                result.append({"outbound_date": day.isoformat(), "return_date": back.isoformat()})
            day += timedelta(days=1)
        return result


class FlightSearchRequest(FlightRequest):
    outbound_date: date
    departure_token: str | None = Field(default=None, min_length=1, max_length=16000)

    @model_validator(mode="after")
    def valid_day(self):
        if self.outbound_date.isoformat() not in {d["outbound_date"] for d in self.dates()}:
            raise ValueError("출발일이 검색 기간·숙박·주말 조건에 맞지 않습니다")
        return self


_lock = asyncio.Lock()
_cache: dict[tuple, tuple[float, dict, str]] = {}
_budget = SlidingLimit(max_calls=50, window_sec=3600)


def _times(start: str, end: str) -> str:
    # 공급자는 마지막 숫자의 한 시간 전체를 포함한다. 분 단위 경계는 응답에서 재검사한다.
    return f"{int(start[:2])},{int(end[:2])},0,22"


def _offers(data: dict, req: FlightSearchRequest) -> list[dict]:
    returning = bool(req.departure_token)
    day = req.outbound_date + timedelta(days=req.nights if returning else 0)
    earliest, latest = (req.return_from, req.return_to) if returning else (req.outbound_from, req.outbound_to)
    origins, destinations = (req.destinations, req.origins) if returning else (req.origins, req.destinations)
    offers = []
    seen = set()
    for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        try:
            flights = item["flights"]
            if not flights or len(flights) - 1 > req.max_stops:
                continue
            dep, arr = flights[0]["departure_airport"], flights[-1]["arrival_airport"]
            depart, arrive = datetime.fromisoformat(dep["time"]), datetime.fromisoformat(arr["time"])
            if dep["id"] not in origins or arr["id"] not in destinations:
                continue
            if depart.date() != day or arrive.date() != day or arrive.hour >= 23:
                continue
            if not earliest <= depart.strftime("%H:%M") <= latest:
                continue
            if not returning and depart <= now_kst():
                continue
            if any(f.get("overnight") for f in flights) or any(l.get("overnight") for l in item.get("layovers", [])):
                continue
            if any(datetime.fromisoformat(f[k]["time"]).date() != day for f in flights for k in ("departure_airport", "arrival_airport")):
                continue
            price = item["price"]
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                continue
            segments = [{"airline": f.get("airline", ""), "number": f.get("flight_number", ""),
                         "from": f["departure_airport"]["id"], "to": f["arrival_airport"]["id"],
                         "departure": f["departure_airport"]["time"], "arrival": f["arrival_airport"]["time"]} for f in flights]
            key = tuple((f["number"], f["from"], f["to"], f["departure"]) for f in segments) + (price,)
            if key in seen:
                continue
            seen.add(key)
            offers.append({"price": price, "segments": segments, "stops": len(flights) - 1,
                           "departure_token": item.get("departure_token") if not returning else None})
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    return sorted(offers, key=lambda o: o["price"])


async def search(req: FlightSearchRequest) -> dict:
    if not settings.serpapi_key:
        raise FlightError("항공 조회 API 키가 없습니다. 서버의 SERPAPI_API_KEY를 설정하세요.")
    back = req.outbound_date + timedelta(days=req.nights)
    params = {"engine": "google_flights", "type": 1, "departure_id": ",".join(sorted(set(req.origins))),
              "arrival_id": ",".join(sorted(req.destinations)), "outbound_date": req.outbound_date.isoformat(),
              "return_date": back.isoformat(), "outbound_times": _times(req.outbound_from, req.outbound_to),
              "return_times": _times(req.return_from, req.return_to), "stops": req.max_stops + 1,
              "adults": req.adults, "travel_class": 1, "currency": "KRW", "hl": "ko", "gl": "kr",
              "sort_by": 2, "show_hidden": "true"}
    if req.departure_token:
        params["departure_token"] = req.departure_token
    key = tuple(sorted(params.items()))
    async with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 900:
            data, checked = cached[1], cached[2]
        else:
            if _budget.check("server"):
                raise FlightError("항공 API 호출은 서버 전체 시간당 50회까지입니다. 잠시 후 다시 조회하세요.")
            try:
                async with httpx.AsyncClient(timeout=45) as client:
                    account_response = await client.get("https://serpapi.com/account.json", params={"api_key": settings.serpapi_key})
                    account_response.raise_for_status()
                    account = account_response.json()
                    if not isinstance(account, dict) or account.get("plan_monthly_price") != 0 or "free" not in str(account.get("plan_name", "")).lower():
                        raise FlightError("무료 플랜만 사용할 수 있습니다. SerpApi 계정의 Free 플랜을 확인하세요.")
                    if not isinstance(account.get("plan_searches_left"), (int, float)) or account["plan_searches_left"] < 1:
                        raise FlightError("이번 달 무료 항공 조회 횟수를 모두 사용했습니다. 갱신 후 다시 조회하세요.")
                    response = await client.get("https://serpapi.com/search.json", params=params | {"api_key": settings.serpapi_key})
                if response.status_code in (401, 403):
                    raise FlightError("항공 API 인증 실패: 서버의 API 키와 계정 권한을 확인하세요.")
                if response.status_code == 429:
                    raise FlightError("항공 API 사용 한도를 초과했습니다. 제공업체 잔여 할당량을 확인하세요.")
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise FlightError("항공 데이터 조회에 실패했습니다. 잠시 후 다시 시도하세요.") from None
            if not isinstance(data, dict) or data.get("error") or data.get("search_metadata", {}).get("status") != "Success":
                raise FlightError("항공 제공업체가 검색을 완료하지 못했습니다. 조건 또는 API 계정을 확인하세요.")
            checked = now_kst().isoformat(timespec="seconds")
            if len(_cache) >= 128:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic(), data, checked)
    url = data.get("search_metadata", {}).get("google_flights_url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.google.com", "google.com"} or not parsed.path.startswith("/travel/flights"):
        url = ""
    return {"outbound_date": req.outbound_date.isoformat(), "return_date": back.isoformat(),
            "offers": _offers(data, req), "returning": bool(req.departure_token),
            "url": url, "checked_at": checked, "currency": "KRW"}
