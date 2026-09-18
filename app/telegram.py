"""텔레그램 봇 알림 발송과 연결 코드 수신 폴링.

봇은 서버에 하나, 받는 사람은 chat_id 로 구분한다.
LinkPoller 는 getUpdates 를 롱폴링하다가 사용자가 봇에게 보낸 6자리 연결 코드를 발견하면
그 사용자 계정에 chat_id 를 자동으로 저장한다.

주의: getUpdates 는 소비자가 하나여야 한다. 서버가 폴링하는 동안 브라우저에서 getUpdates 주소를
열면 서로 업데이트를 가로채거나 409 Conflict 가 난다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from app.config import DATA_DIR, settings, write_private
from app.ratelimit import FailureLock

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_FILE = DATA_DIR / "telegram_offset.json"
CODE_RE = re.compile(r"\b(\d{6})\b")
# 연결 코드 무차별 대입 방지: 한 채팅에서 10분 안에 5회 틀리면 1시간 동안 무시
code_lock = FailureLock(max_failures=5, window_sec=600, lock_sec=3600)

bot_username: str = ""  # getMe 로 채움. 화면에서 t.me 링크를 만들 때 사용


def _url(method: str) -> str:
    return API.format(token=settings.telegram_bot_token, method=method)


async def send_message(chat_id: str, text: str) -> bool:
    if not settings.telegram_configured:
        log.warning("텔레그램 봇 토큰 미설정: 메시지 생략")
        return False
    if not chat_id:
        log.warning("chat_id 없음: 메시지 생략")
        return False
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(_url("sendMessage"), json=payload)
            if r.status_code != 200:
                log.error("텔레그램 발송 실패 %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except httpx.HTTPError as e:
        log.error("텔레그램 통신 오류: %s", e)
        return False


async def fetch_bot_username() -> str:
    global bot_username
    if not settings.telegram_configured:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(_url("getMe"))
            if r.status_code == 200:
                bot_username = r.json().get("result", {}).get("username", "") or ""
    except httpx.HTTPError as e:
        log.warning("getMe 실패: %s", e)
    return bot_username


class LinkPoller:
    """봇 수신 메시지를 롱폴링하며 연결 코드를 처리한다."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._offset = self._load_offset()
        self.last_error: str | None = None

    @staticmethod
    def _load_offset() -> int:
        try:
            return int(json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0))
        except (FileNotFoundError, ValueError, json.JSONDecodeError, AttributeError):
            return 0

    def _save_offset(self) -> None:
        try:
            write_private(OFFSET_FILE, json.dumps({"offset": self._offset}))
        except OSError:
            pass

    def start(self) -> None:
        if settings.telegram_configured and self._task is None:
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
        await fetch_bot_username()
        log.info("텔레그램 연결 코드 폴링 시작 (봇 @%s)", bot_username or "?")
        backoff = 5
        while True:
            try:
                await self._poll_once()
                backoff = 5
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.warning("텔레그램 폴링 오류: %s (%.0f초 후 재시도)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _poll_once(self) -> None:
        params = {"offset": self._offset, "timeout": 25, "allowed_updates": json.dumps(["message"])}
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.get(_url("getUpdates"), params=params)
        if r.status_code == 409:
            raise RuntimeError("getUpdates 충돌: 다른 곳에서 같은 봇의 getUpdates 를 사용 중입니다")
        if r.status_code != 200:
            raise RuntimeError(f"getUpdates {r.status_code}: {r.text[:120]}")
        for upd in r.json().get("result", []):
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or {}
            try:
                await self._handle(msg)
            except Exception as e:  # noqa: BLE001
                log.exception("연결 코드 처리 실패: %s", e)
        self._save_offset()

    async def _handle(self, msg: dict) -> None:
        from app.users import user_store  # 순환 import 회피

        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return
        sender = msg.get("from") or {}
        name = " ".join(x for x in [sender.get("first_name", ""), sender.get("last_name", "")] if x).strip()
        if sender.get("username"):
            name = f"{name} (@{sender['username']})".strip()

        key = str(chat_id)
        if code_lock.locked_for(key) > 0:
            return  # 틀린 코드를 너무 많이 보낸 채팅은 답장 없이 무시
        if text.startswith("/start"):
            await send_message(key, "빈자리 알리미 봇입니다.\n사이트의 '내 설정'에 표시된 6자리 연결 코드를 이 대화에 보내 주세요.")
            return
        m = CODE_RE.search(text)
        if not m:
            await send_message(key, "6자리 연결 코드를 보내 주세요. 코드는 사이트의 '내 설정'에서 확인할 수 있습니다.")
            return
        user = user_store.by_link_code(m.group(1))
        if user is None:
            locked = code_lock.record_failure(key)
            if locked:
                log.warning("연결 코드 실패 누적으로 chat %s 를 %d초 동안 무시", key, int(locked))
                await send_message(key, "잘못된 코드가 반복되어 잠시 후에 다시 시도할 수 있습니다.")
            else:
                await send_message(key, "코드를 찾을 수 없거나 만료됐습니다. 사이트의 '내 설정'에서 코드를 다시 확인해 주세요. 코드는 10분마다 바뀝니다.")
            return
        code_lock.reset(key)
        user_store.link_telegram(user, key, name)
        log.info("텔레그램 연결: %s <- chat %s (%s)", user.username, chat_id, name)
        await send_message(str(chat_id), f"✅ {user.username}님 계정과 연결되었습니다. 이제 이 대화로 알림이 옵니다.")


link_poller = LinkPoller()
