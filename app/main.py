"""FastAPI 서버: 인증, 내 설정, 조회 API, 감시 CRUD, 정적 화면."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from app import call_stats, flight_service, nol_service, telegram
from app.config import settings
from app.nol_service import MAX_SCAN_DAYS, NolError
from app.ratelimit import auth_lock, run_now_limit, search_limit
from app.korail_service import TRAIN_TYPE_CODES, SearchError, close_all, drop_service, get_service, load_stations
from app.users import MAX_POLL_INTERVAL, MIN_POLL_INTERVAL, SESSION_DAYS, User, user_store
from app.watcher import watch_store, watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx 는 INFO 에서 요청 URL 을 통째로 찍어 텔레그램 봇 토큰이 로그에 남는다. 경고 이상만 남긴다.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USERNAME = re.compile(r"^[A-Za-z0-9가-힣_.-]{2,20}$")
COOKIE = "session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    watcher.start()
    telegram.link_poller.start()
    yield
    await telegram.link_poller.stop()
    await watcher.stop()
    await asyncio.to_thread(close_all)


# API 문서(/docs, /openapi.json)는 공개하지 않는다.
app = FastAPI(title="빈자리 알리미", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def client_ip(request: Request) -> str:
    """Caddy 뒤에 있으므로 X-Forwarded-For 의 첫 주소를 쓴다."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


# ----------------------------------------------------------------- 인증
def current_user(request: Request) -> User:
    user = user_store.user_for_session(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return user


def approved_user(user: User = Depends(current_user)) -> User:
    """조회·감시 API 는 관리자가 허용한 사용자만."""
    if not user.allowed:
        raise HTTPException(status_code=403, detail="관리자 승인 전에는 조회와 감시를 사용할 수 없습니다")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="관리자만 사용할 수 있습니다")
    return user


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=_is_https(request)
    )


def _check_auth_lock(*keys: str) -> None:
    for k in keys:
        remain = auth_lock.locked_for(k)
        if remain > 0:
            raise HTTPException(status_code=429, detail=f"시도가 너무 많습니다. {int(remain) + 1}초 뒤에 다시 시도하세요.")


def _auth_failed(*keys: str) -> None:
    for k in keys:
        auth_lock.record_failure(k)


class AuthRequest(BaseModel):
    username: str = Field(min_length=2, max_length=20)
    password: str = Field(min_length=1, max_length=100)
    invite_code: str | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME.match(v):
            raise ValueError("사용자명은 2~20자, 한글/영문/숫자/_ . - 만")
        return v


@app.post("/api/auth/register", status_code=201)
async def register(req: AuthRequest, request: Request, response: Response):
    ip = client_ip(request)
    _check_auth_lock(f"ip:{ip}")
    if not settings.invite_code:
        raise HTTPException(status_code=403, detail="가입이 닫혀 있습니다 (.env 의 INVITE_CODE 미설정)")
    if (req.invite_code or "").strip() != settings.invite_code:
        _auth_failed(f"ip:{ip}")
        raise HTTPException(status_code=403, detail="초대코드가 올바르지 않습니다")
    if len(req.password) < 8:
        raise HTTPException(status_code=422, detail="비밀번호는 8자 이상이어야 합니다")
    try:
        user = user_store.create(req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    _set_cookie(request, response, user_store.create_session(user))
    return {"username": user.username}


@app.post("/api/auth/login")
async def login(req: AuthRequest, request: Request, response: Response):
    ip = client_ip(request)
    ukey = f"user:{req.username.lower()}"
    _check_auth_lock(f"ip:{ip}", ukey)
    user = user_store.authenticate(req.username, req.password)
    if user is None:
        _auth_failed(f"ip:{ip}", ukey)
        raise HTTPException(status_code=401, detail="사용자명 또는 비밀번호가 올바르지 않습니다")
    auth_lock.reset(ukey)
    _set_cookie(request, response, user_store.create_session(user))
    return {"username": user.username}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    user_store.delete_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ----------------------------------------------------------------- 내 설정
def _validate_chat_id(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if v and not re.match(r"^-?\d{1,20}$", v):
        raise ValueError("chat_id 는 숫자여야 합니다")
    return v


class SettingsRequest(BaseModel):
    korail_id: str | None = Field(default=None, max_length=100)
    korail_pw: str | None = Field(default=None, max_length=100)


def _me_summary(user: User) -> dict:
    """내 정보. 코레일 비밀번호 값은 절대 돌려주지 않는다(설정 여부만)."""
    svc = get_service(user)
    return {
        "username": user.username,
        "is_admin": user.is_admin,
        "allowed": user.allowed,
        "korail_id": user.korail_id,
        "korail_configured": user.korail_configured,
        "korail_pw_set": bool(user.korail_pw_enc),
        "telegram_chat_id": user.telegram_chat_id,
        "telegram_name": user.telegram_name,
        "telegram_chat_configured": bool(user.telegram_chat_id),
        "telegram_link_code": "" if user.telegram_chat_id else user_store.ensure_link_code(user),
        "telegram_bot_username": telegram.bot_username,
        "korail_logged_in": svc.logged_in,
        "korail_last_error": svc.last_error,
        "watch_limit": user.effective_watch_limit,  # 기차 상한. None = 무제한
        "nol_watch_limit": user.effective_nol_watch_limit,  # 공연 상한. None = 무제한
        "active_watches": watch_store.active_count(user.id),
        "active_train_watches": watch_store.active_count(user.id, "korail"),
        "active_nol_watches": watch_store.active_count(user.id, "nol"),
    }


@app.get("/api/me")
async def me(user: User = Depends(current_user)):
    return _me_summary(user)


@app.post("/api/me/telegram/unlink")
async def telegram_unlink(user: User = Depends(current_user)):
    """연결을 끊고 새 코드를 받는다(다른 텔레그램 계정으로 바꿀 때)."""
    user_store.unlink_telegram(user)
    return _me_summary(user)


@app.put("/api/me/settings")
async def update_settings(req: SettingsRequest, user: User = Depends(current_user)):
    user_store.update_settings(user, req.korail_id, req.korail_pw)
    drop_service(user.id)  # 계정이 바뀌었을 수 있으니 세션을 버린다
    return _me_summary(user)


# ----------------------------------------------------------------- 관리자
class AdminUserUpdate(BaseModel):
    approved: bool | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=30)
    watch_limit: int | None = Field(default=None, ge=0, le=20)
    nol_watch_limit: int | None = Field(default=None, ge=0, le=20)
    poll_interval_sec: int | None = Field(default=None, ge=MIN_POLL_INTERVAL, le=MAX_POLL_INTERVAL)

    @field_validator("telegram_chat_id")
    @classmethod
    def _chat(cls, v: str | None) -> str | None:
        return _validate_chat_id(v)


def _admin_row(u: User) -> dict:
    active = [w for w in watch_store.list(u.id) if w.active]
    calls = call_stats.get(u.id)
    return u.admin_view() | {
        "active_watches": len(active),
        "active_train_watches": sum(1 for w in active if not w.is_nol),
        "active_nol_watches": sum(1 for w in active if w.is_nol),
        "total_korail_calls": calls["korail"],
        "total_nol_calls": calls["nol"],
    }


@app.get("/admin")
async def admin_page(request: Request):
    user = user_store.user_for_session(request.cookies.get(COOKIE))
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/api/admin/users")
async def admin_list_users(_: User = Depends(admin_user)):
    return [_admin_row(u) for u in user_store.list()]


@app.get("/api/admin/stats")
async def admin_stats(_: User = Depends(admin_user)):
    """감시 루프 부하 지표. 관리자가 조회 주기를 정할 때 참고한다."""
    from app import korail_service

    history = list(watcher.cycle_history)
    avg = None
    if history:
        avg = {
            "cycles": len(history),
            "watches": round(sum(h["watches"] for h in history) / len(history), 1),
            "calls": round(sum(h["calls"] for h in history) / len(history), 1),
            "duration_sec": round(sum(h["duration_sec"] for h in history) / len(history), 1),
        }
    calls_10m = korail_service.calls_in_last(600)
    nol_10m = nol_service.calls_in_last(600)
    return {
        "default_poll_interval_sec": settings.poll_interval_sec,
        "active_watches": sum(1 for w in watch_store.list() if w.active),
        "active_train_watches": sum(1 for w in watch_store.list() if w.active and not w.is_nol),
        "active_nol_watches": sum(1 for w in watch_store.list() if w.active and w.is_nol),
        "nol_calls_last_10min": nol_10m,
        "nol_calls_per_min": round(nol_10m / 10, 1),
        "nol_total_calls_since_start": nol_service.total_calls,
        "last_cycle": watcher.last_cycle,
        "average": avg,
        "calls_last_10min": calls_10m,
        "calls_per_min": round(calls_10m / 10, 1),
        "total_calls_since_start": korail_service.total_calls,
        "history": history[-10:],
        "projected": watcher.projected_load(),  # {"per_min", "limit", "users": [...]}
    }


@app.put("/api/admin/users/{user_id}")
async def admin_update_user(user_id: str, req: AdminUserUpdate, admin: User = Depends(admin_user)):
    target = user_store.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="사용자가 없습니다")
    if target.is_admin and req.approved is False:
        raise HTTPException(status_code=400, detail="관리자 계정은 거부할 수 없습니다")
    user_store.admin_update(
        target, req.approved, req.telegram_chat_id, req.watch_limit, req.poll_interval_sec, req.nol_watch_limit
    )
    return _admin_row(target)


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: User = Depends(admin_user)):
    target = user_store.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="사용자가 없습니다")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    removed = watch_store.remove_by_user(target.id)
    drop_service(target.id)
    user_store.delete(target.id)
    call_stats.remove(target.id)
    return {"ok": True, "removed_watches": removed}


@app.post("/api/me/korail/test")
async def korail_test(user: User = Depends(approved_user)):
    svc = get_service(user)
    try:
        name = await asyncio.to_thread(svc.test_login)
    except SearchError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "name": name}


@app.post("/api/telegram/test")
async def telegram_test(user: User = Depends(current_user)):
    if not settings.telegram_configured:
        raise HTTPException(status_code=400, detail="서버에 텔레그램 봇 토큰이 설정되지 않았습니다 (.env TELEGRAM_BOT_TOKEN)")
    if not user.telegram_chat_id:
        raise HTTPException(status_code=400, detail="내 설정에서 텔레그램 chat_id 를 먼저 등록하세요")
    ok = await telegram.send_message(user.telegram_chat_id, f"✅ {user.username}님, 빈자리 알리미 텔레그램 연결 테스트")
    if not ok:
        raise HTTPException(status_code=400, detail="텔레그램 발송 실패. chat_id 를 확인하고 봇에게 먼저 메시지를 보냈는지 확인하세요.")
    return {"ok": True}


# ----------------------------------------------------------------- 화면
@app.get("/")
async def index(request: Request):
    if user_store.user_for_session(request.cookies.get(COOKIE)) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
async def login_page(request: Request):
    if user_store.user_for_session(request.cookies.get(COOKIE)) is not None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/api/status")
async def status(user: User = Depends(current_user)):
    svc = get_service(user)
    return {
        "username": user.username,
        "allowed": user.allowed,
        "is_admin": user.is_admin,
        "korail_configured": user.korail_configured,
        "korail_logged_in": svc.logged_in,
        "korail_last_error": svc.last_error,
        "telegram_bot_configured": settings.telegram_configured,
        "telegram_bot_username": telegram.bot_username,
        "telegram_poll_error": telegram.link_poller.last_error,
        "telegram_chat_configured": bool(user.telegram_chat_id),
        "poll_interval_sec": user.effective_poll_interval,
        "last_cycle_at": watcher.last_cycle_at.isoformat(timespec="seconds") if watcher.last_cycle_at else None,
        "last_cycle_error": watcher.last_cycle_error,
        "active_watches": sum(1 for w in watch_store.list(user.id) if w.active),
        "total_active_watches": sum(1 for w in watch_store.list() if w.active),
        "users": user_store.count(),
    }


@app.get("/api/stations")
async def stations(_: User = Depends(current_user)):
    items = await asyncio.to_thread(load_stations)
    major = sorted((s for s in items if s["major"]), key=lambda s: s["major"])
    others = sorted((s for s in items if not s["major"]), key=lambda s: s["name"])
    return {"major": [s["name"] for s in major], "others": [s["name"] for s in others]}


# ----------------------------------------------------------------- 조회 / 감시
@app.post("/api/flights/plan")
async def flight_plan(req: flight_service.FlightRequest, _: User = Depends(admin_user)):
    return {"dates": req.dates(), "configured": bool(settings.serpapi_key)}


@app.post("/api/flights/search")
async def flight_search(req: flight_service.FlightSearchRequest, _: User = Depends(admin_user)):
    try:
        return await flight_service.search(req)
    except flight_service.FlightError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


class SearchRequest(BaseModel):
    dep: str = Field(min_length=1, max_length=20)
    arr: str = Field(min_length=1, max_length=20)
    date: str
    time_from: str | None = None
    time_to: str | None = None
    train_type: str = "ALL"

    @field_validator("dep", "arr")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not YMD.match(v):
            raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
        return v

    @field_validator("time_from", "time_to")
    @classmethod
    def _time(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v is not None and not HHMM.match(v):
            raise ValueError("시간은 HH:MM 형식이어야 합니다")
        return v

    @field_validator("train_type")
    @classmethod
    def _train_type(cls, v: str) -> str:
        v = v.upper()
        if v not in TRAIN_TYPE_CODES:
            raise ValueError(f"열차 종류는 {', '.join(TRAIN_TYPE_CODES)} 중 하나")
        return v


class WatchRequest(SearchRequest):
    seat_pref: str = "ANY"

    @field_validator("seat_pref")
    @classmethod
    def _pref(cls, v: str) -> str:
        v = v.upper()
        if v not in {"ANY", "GENERAL", "SPECIAL"}:
            raise ValueError("seat_pref 는 ANY/GENERAL/SPECIAL")
        return v


MAX_TELEGRAM_LINES = 40


def _format_search_result(req: "SearchRequest", seats: list) -> str:
    when = "전체" if not req.time_from and not req.time_to else f"{req.time_from or '00:00'}~{req.time_to or '23:59'}"
    available = [s for s in seats if s.any_available]
    lines = [f"🔍 조회: {req.dep} → {req.arr} {req.date} {when}", f"{len(seats)}편 중 빈자리 {len(available)}편"]
    if not available:
        lines.append("빈자리 없음")
    for s in available[:MAX_TELEGRAM_LINES]:
        gen = "일반 O" if s.general_available else "일반 X"
        spe = "특실 O" if s.special_available else "특실 X"
        lines.append(f"- {s.train_type_name} {s.train_no}  {s.dep_time[:2]}:{s.dep_time[2:4]}→{s.arr_time[:2]}:{s.arr_time[2:4]}  {gen} / {spe}")
    if len(available) > MAX_TELEGRAM_LINES:
        lines.append(f"… 외 {len(available) - MAX_TELEGRAM_LINES}편")
    return "\n".join(lines)


@app.post("/api/search")
async def search(req: SearchRequest, user: User = Depends(approved_user)):
    denied = search_limit.check(user.id)
    if denied:
        raise HTTPException(status_code=429, detail=denied)
    svc = get_service(user)
    try:
        seats = await asyncio.to_thread(svc.search, req.dep, req.arr, req.date, req.time_from, req.time_to, req.train_type)
    except SearchError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    sent = False
    if user.telegram_chat_id and settings.telegram_configured:
        sent = await telegram.send_message(user.telegram_chat_id, _format_search_result(req, seats))
    return {"count": len(seats), "trains": [s.to_dict() for s in seats], "telegram_sent": sent}


@app.get("/api/watches")
async def list_watches(user: User = Depends(current_user)):
    return [w.to_dict() for w in watch_store.list(user.id)]


def _check_watch_limit(user: User, kind: str = "korail", adding: int = 1) -> None:
    """종류(기차/공연)별 활성 감시 상한 검사. 관리자는 무제한."""
    limit = user.limit_for(kind)
    if limit is None:
        return
    active = watch_store.active_count(user.id, kind)
    name = "공연" if kind == "nol" else "기차"
    if active + adding > limit:
        raise HTTPException(
            status_code=400,
            detail=f"활성 {name} 감시는 최대 {limit}건입니다 (현재 {active}건). 기존 {name} 감시를 중지하거나 삭제하세요.",
        )


@app.post("/api/watches", status_code=201)
async def add_watch(req: WatchRequest, user: User = Depends(approved_user)):
    _check_watch_limit(user, "korail")
    w = watch_store.add(
        user_id=user.id,
        dep=req.dep,
        arr=req.arr,
        date=req.date,
        time_from=req.time_from,
        time_to=req.time_to,
        train_type=req.train_type,
        seat_pref=req.seat_pref,
    )
    return w.to_dict()


# ----------------------------------------------------------------- NOL 티켓(공연)
GOODS_CODE = re.compile(r"^\d{5,12}$")


def _nol_goods(code: str) -> str:
    """상품코드 또는 상품 URL 을 코드로 정규화한다."""
    normalized = nol_service.normalize_code(code)
    if normalized is None:
        raise HTTPException(status_code=422, detail="상품코드(숫자) 또는 NOL 상품 주소를 입력하세요")
    return normalized


async def _nol(fn, *args):
    """NOL 호출을 스레드에서 실행하고, 실패를 화면용 JSON 오류로 바꾼다.

    NolError(통신·응답 오류)는 400, 그 밖의 예외는 로그를 남기고 502. 어떤 경우에도 연결이 끊기지 않게 한다.
    """
    try:
        return await asyncio.to_thread(fn, *args)
    except NolError as e:
        log.warning("NOL %s 실패: %s", fn.__name__, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.exception("NOL %s 중 예상치 못한 오류", fn.__name__)
        raise HTTPException(status_code=502, detail=f"NOL 조회 중 서버 오류: {type(e).__name__}: {e}") from e


def _nol_date(v: str | None) -> str | None:
    v = (v or "").strip() or None
    if v is not None and not YMD.match(v):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
    return v


class NolRemainingRequest(BaseModel):
    goods_code: str = Field(min_length=1, max_length=200)
    date: str | None = None  # 비우면 남은 공연 기간을 하루씩 조회(최대 MAX_SCAN_DAYS 일)
    date_to: str | None = None

    @field_validator("date", "date_to")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        return _nol_date(v)


class NolWatchRequest(BaseModel):
    goods_code: str = Field(min_length=1, max_length=200)
    date: str
    play_seq: str = ""
    grades: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not YMD.match(v):
            raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
        return v

    @field_validator("play_seq")
    @classmethod
    def _seq(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^\d{1,4}$", v):
            raise ValueError("회차는 숫자여야 합니다")
        return v

    @field_validator("grades")
    @classmethod
    def _grades(cls, v: list[str]) -> list[str]:
        return [g.strip()[:40] for g in v if g and g.strip()]


def _format_nol_result(goods, seats: list) -> str:
    available = [s for s in seats if s.available]
    dates = sorted({s.play_date for s in seats})
    when = f"{dates[0]}~{dates[-1]}" if len(dates) > 1 else (dates[0] if dates else "-")
    lines = [f"🔍 공연 조회: {goods.name} {when}", f"{len(seats)}개 회차·등급 중 잔여 있는 것 {len(available)}개"]
    if not available:
        lines.append("잔여석 없음")
    for s in available[:MAX_TELEGRAM_LINES]:
        lines.append(f"- {s.play_date} {s.play_seq}회차 {s.grade_name}  잔여 {s.remain}석")
    lines.append(f"예매: {goods.url}")
    return "\n".join(lines)


@app.get("/api/nol/search")
async def nol_search(q: str, upcoming: bool = False, user: User = Depends(approved_user)):
    """공연명·아티스트로 검색(기본은 판매 중인 공연만, upcoming=1 이면 판매 예정 포함).

    상품코드나 상품 URL 을 넣으면 그 상품을 바로 돌려준다.
    """
    q = q.strip()
    if len(q) < 2 or len(q) > 100:
        raise HTTPException(status_code=422, detail="검색어는 2~100자")
    denied = search_limit.check(user.id)
    if denied:
        raise HTTPException(status_code=429, detail=denied)
    code = nol_service.normalize_code(q)
    if code:
        g = await _nol(nol_service.get_goods, code, user.id)
        return {"items": [{"code": g.code, "title": g.name, "date_info": f"{g.play_start}~{g.play_end}", "place": g.place, "url": g.url}]}
    return {"items": await _nol(nol_service.search, q, user.id, upcoming)}


@app.get("/api/nol/goods/{code}")
async def nol_goods(code: str, user: User = Depends(approved_user)):
    code = _nol_goods(code)
    g = await _nol(nol_service.get_goods, code, user.id)
    return g.to_dict() | {"max_scan_days": MAX_SCAN_DAYS}


@app.post("/api/nol/remaining")
async def nol_remaining(req: NolRemainingRequest, user: User = Depends(approved_user)):
    denied = search_limit.check(user.id)
    if denied:
        raise HTTPException(status_code=429, detail=denied)
    code = _nol_goods(req.goods_code)
    goods = await _nol(nol_service.get_goods, code, user.id)
    if req.date and not req.date_to:
        seats = await _nol(nol_service.remaining_by_date, code, req.date, user.id)
    else:
        seats = await _nol(nol_service.scan_dates, goods, req.date, req.date_to, user.id)
    sent = False
    if user.telegram_chat_id and settings.telegram_configured:
        sent = await telegram.send_message(user.telegram_chat_id, _format_nol_result(goods, seats))
    return {"goods": goods.to_dict(), "count": len(seats), "seats": [s.to_dict() for s in seats], "telegram_sent": sent}


@app.post("/api/watches/nol", status_code=201)
async def add_nol_watch(req: NolWatchRequest, user: User = Depends(approved_user)):
    _check_watch_limit(user, "nol")
    code = _nol_goods(req.goods_code)
    goods = await _nol(nol_service.get_goods, code, user.id)
    if goods.play_start and goods.play_end and not (goods.play_start <= req.date <= goods.play_end):
        raise HTTPException(status_code=400, detail=f"관람일은 공연 기간({goods.play_start}~{goods.play_end}) 안이어야 합니다")
    w = watch_store.add(
        user_id=user.id,
        kind="nol",
        date=req.date,
        goods_code=code,
        goods_name=goods.name,
        play_seq=req.play_seq,
        grades=req.grades,
    )
    return w.to_dict()


def _own_watch(watch_id: str, user: User):
    w = watch_store.get(watch_id)
    if w is None or w.user_id != user.id:
        raise HTTPException(status_code=404, detail="감시 항목이 없습니다")
    return w


@app.post("/api/watches/run-now")
async def run_now(user: User = Depends(approved_user)):
    denied = run_now_limit.check(user.id)
    if denied:
        raise HTTPException(status_code=429, detail="지금 점검은 30초에 한 번만 할 수 있습니다.")
    await watcher.run_once(user.id)
    return {"ok": True}


@app.post("/api/watches/{watch_id}/toggle")
async def toggle_watch(watch_id: str, user: User = Depends(approved_user)):
    current = _own_watch(watch_id, user)
    if not current.active:  # 재개도 상한에 포함
        _check_watch_limit(user, current.kind)
    w = watch_store.set_active(watch_id, not current.active)
    return w.to_dict()


@app.delete("/api/watches/{watch_id}")
async def delete_watch(watch_id: str, user: User = Depends(current_user)):
    _own_watch(watch_id, user)
    watch_store.remove(watch_id)
    return {"ok": True}
