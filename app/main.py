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

from app import telegram
from app.config import settings
from app.korail_service import TRAIN_TYPE_CODES, SearchError, close_all, drop_service, get_service, load_stations
from app.users import SESSION_DAYS, User, user_store
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


app = FastAPI(title="기차 빈자리 조회", lifespan=lifespan)


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


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")


class AuthRequest(BaseModel):
    username: str = Field(min_length=2, max_length=20)
    password: str = Field(min_length=4, max_length=100)
    invite_code: str | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME.match(v):
            raise ValueError("사용자명은 2~20자, 한글/영문/숫자/_ . - 만")
        return v


@app.post("/api/auth/register", status_code=201)
async def register(req: AuthRequest, response: Response):
    if not settings.invite_code:
        raise HTTPException(status_code=403, detail="가입이 닫혀 있습니다 (.env 의 INVITE_CODE 미설정)")
    if (req.invite_code or "").strip() != settings.invite_code:
        raise HTTPException(status_code=403, detail="초대코드가 올바르지 않습니다")
    try:
        user = user_store.create(req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    _set_cookie(response, user_store.create_session(user))
    return {"username": user.username}


@app.post("/api/auth/login")
async def login(req: AuthRequest, response: Response):
    user = user_store.authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="사용자명 또는 비밀번호가 올바르지 않습니다")
    _set_cookie(response, user_store.create_session(user))
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
        "watch_limit": user.effective_watch_limit,  # None = 무제한
        "active_watches": watch_store.active_count(user.id),
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

    @field_validator("telegram_chat_id")
    @classmethod
    def _chat(cls, v: str | None) -> str | None:
        return _validate_chat_id(v)


def _admin_row(u: User) -> dict:
    return u.admin_view() | {"active_watches": sum(1 for w in watch_store.list(u.id) if w.active)}


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
    return {
        "poll_interval_sec": settings.poll_interval_sec,
        "active_watches": sum(1 for w in watch_store.list() if w.active),
        "last_cycle": watcher.last_cycle,
        "average": avg,
        "calls_last_10min": calls_10m,
        "calls_per_min": round(calls_10m / 10, 1),
        "total_calls_since_start": korail_service.total_calls,
        "history": history[-10:],
    }


@app.put("/api/admin/users/{user_id}")
async def admin_update_user(user_id: str, req: AdminUserUpdate, admin: User = Depends(admin_user)):
    target = user_store.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="사용자가 없습니다")
    if target.is_admin and req.approved is False:
        raise HTTPException(status_code=400, detail="관리자 계정은 거부할 수 없습니다")
    user_store.admin_update(target, req.approved, req.telegram_chat_id, req.watch_limit)
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
    ok = await telegram.send_message(user.telegram_chat_id, f"✅ {user.username}님, 기차 빈자리 조회 텔레그램 연결 테스트")
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
        "poll_interval_sec": settings.poll_interval_sec,
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


def _check_watch_limit(user: User, adding: int = 1) -> None:
    limit = user.effective_watch_limit
    if limit is None:
        return
    active = watch_store.active_count(user.id)
    if active + adding > limit:
        raise HTTPException(
            status_code=400,
            detail=f"활성 감시는 최대 {limit}건입니다 (현재 {active}건). 기존 감시를 중지하거나 삭제하세요.",
        )


@app.post("/api/watches", status_code=201)
async def add_watch(req: WatchRequest, user: User = Depends(approved_user)):
    _check_watch_limit(user)
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


def _own_watch(watch_id: str, user: User):
    w = watch_store.get(watch_id)
    if w is None or w.user_id != user.id:
        raise HTTPException(status_code=404, detail="감시 항목이 없습니다")
    return w


@app.post("/api/watches/run-now")
async def run_now(user: User = Depends(approved_user)):
    await watcher.run_once(user.id)
    return {"ok": True}


@app.post("/api/watches/{watch_id}/toggle")
async def toggle_watch(watch_id: str, user: User = Depends(approved_user)):
    current = _own_watch(watch_id, user)
    if not current.active:  # 재개도 상한에 포함
        _check_watch_limit(user)
    w = watch_store.set_active(watch_id, not current.active)
    return w.to_dict()


@app.delete("/api/watches/{watch_id}")
async def delete_watch(watch_id: str, user: User = Depends(current_user)):
    _own_watch(watch_id, user)
    watch_store.remove(watch_id)
    return {"ok": True}
