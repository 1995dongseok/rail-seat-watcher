"""앱 사용자 저장소와 세션.

- data/users.json : 사용자 목록. 코레일 비밀번호는 암호화해 저장한다.
- data/sessions.json : 로그인 세션 토큰 -> 사용자 id
"""

from __future__ import annotations

import json
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from pykorail.device import random_profile

from app import crypto
from app.config import DATA_DIR, settings, write_private

USERS_FILE = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
SESSION_DAYS = 30
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 3600
LINK_CODE_TTL_SEC = 600  # 텔레그램 연결 코드 유효 시간(10분)


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    korail_id: str = ""
    korail_pw_enc: str = ""
    telegram_chat_id: str = ""
    telegram_name: str = ""  # 연결된 텔레그램 표시 이름(참고용)
    telegram_link_code: str = ""  # 봇에게 보내면 chat_id 가 자동 연결되는 1회용 코드
    telegram_link_code_at: str = ""  # 코드 발급 시각(ISO). LINK_CODE_TTL_SEC 지나면 무효
    device_profile_id: str = ""
    approved: bool = False  # 관리자가 허용해야 조회/감시 가능. 기본 거부
    flight_approved: bool = False
    watch_limit: int = 2  # 동시에 활성화할 수 있는 기차 감시 수. 관리자는 무제한
    nol_watch_limit: int = 2  # 동시에 활성화할 수 있는 공연(NOL) 감시 수. 관리자는 무제한
    poll_interval_sec: int = 0  # 이 사용자의 감시 주기(초). 0 이면 서버 기본값(.env)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def is_admin(self) -> bool:
        return self.username.lower() == settings.admin_username.lower()

    @property
    def allowed(self) -> bool:
        """조회·감시 사용 가능 여부. 관리자는 항상 허용."""
        return self.approved or self.is_admin

    @property
    def flight_allowed(self) -> bool:
        return self.is_admin or (self.approved and self.flight_approved)

    @property
    def effective_watch_limit(self) -> int | None:
        """기차 감시 상한. None 이면 무제한(관리자)."""
        return None if self.is_admin else max(0, int(self.watch_limit))

    @property
    def effective_nol_watch_limit(self) -> int | None:
        """공연(NOL) 감시 상한. None 이면 무제한(관리자)."""
        return None if self.is_admin else max(0, int(self.nol_watch_limit))

    def limit_for(self, kind: str) -> int | None:
        return self.effective_nol_watch_limit if kind == "nol" else self.effective_watch_limit

    @property
    def effective_poll_interval(self) -> int:
        v = int(self.poll_interval_sec or 0) or settings.poll_interval_sec
        return max(MIN_POLL_INTERVAL, v)

    @property
    def korail_pw(self) -> str:
        try:
            return crypto.decrypt(self.korail_pw_enc)
        except Exception:  # noqa: BLE001  키 변경 등으로 복호화 실패
            return ""

    @property
    def korail_configured(self) -> bool:
        return bool(self.korail_id and self.korail_pw_enc)

    def admin_view(self) -> dict:
        """관리자 페이지용. 코레일 아이디/비밀번호 값은 제외하고 등록 여부만."""
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "approved": self.approved,
            "allowed": self.allowed,
            "flight_approved": self.flight_approved,
            "flight_allowed": self.flight_allowed,
            "korail_configured": self.korail_configured,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_name": self.telegram_name,
            "watch_limit": self.effective_watch_limit,
            "nol_watch_limit": self.effective_nol_watch_limit,
            "poll_interval_sec": self.effective_poll_interval,
            "created_at": self.created_at,
        }


class UserStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            for item in json.loads(USERS_FILE.read_text(encoding="utf-8")):
                try:
                    u = User(**item)
                    self._users[u.id] = u
                except TypeError:
                    continue
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            self._sessions = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._sessions = {}

    def _save_users(self) -> None:
        write_private(USERS_FILE, json.dumps([asdict(u) for u in self._users.values()], ensure_ascii=False, indent=2))

    def _save_sessions(self) -> None:
        write_private(SESSIONS_FILE, json.dumps(self._sessions, indent=2))

    # ------------------------------------------------------------- 사용자
    def count(self) -> int:
        return len(self._users)

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def by_username(self, username: str) -> User | None:
        key = username.strip().lower()
        return next((u for u in self._users.values() if u.username.lower() == key), None)

    def create(self, username: str, password: str) -> User:
        with self._lock:
            if self.by_username(username) is not None:
                raise ValueError("이미 사용 중인 사용자명입니다")
            u = User(
                id=uuid.uuid4().hex[:10],
                username=username.strip(),
                password_hash=crypto.hash_password(password),
                device_profile_id=random_profile().id,
            )
            self._users[u.id] = u
            self._save_users()
            return u

    def authenticate(self, username: str, password: str) -> User | None:
        u = self.by_username(username)
        if u is None or not crypto.verify_password(password, u.password_hash):
            return None
        return u

    def list(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: (not u.is_admin, u.created_at))

    def update_settings(self, user: User, korail_id: str | None, korail_pw: str | None) -> User:
        with self._lock:
            if korail_id is not None:
                user.korail_id = korail_id.strip()
            if korail_pw:  # 빈 값이면 기존 비밀번호 유지
                user.korail_pw_enc = crypto.encrypt(korail_pw)
            if not user.device_profile_id:
                user.device_profile_id = random_profile().id
            self._save_users()
            return user

    def admin_update(
        self,
        user: User,
        approved: bool | None,
        telegram_chat_id: str | None,
        watch_limit: int | None = None,
        poll_interval_sec: int | None = None,
        nol_watch_limit: int | None = None,
        flight_approved: bool | None = None,
    ) -> User:
        """관리자가 허용/거부, chat_id, 감시 상한(기차/공연), 감시 주기를 바꾼다."""
        with self._lock:
            if approved is not None:
                user.approved = approved
            if flight_approved is not None:
                user.flight_approved = flight_approved
            if telegram_chat_id is not None:
                user.telegram_chat_id = telegram_chat_id.strip()
                if not user.telegram_chat_id:
                    user.telegram_name = ""
            if watch_limit is not None:
                user.watch_limit = max(0, int(watch_limit))
            if nol_watch_limit is not None:
                user.nol_watch_limit = max(0, int(nol_watch_limit))
            if poll_interval_sec is not None:
                user.poll_interval_sec = min(MAX_POLL_INTERVAL, max(MIN_POLL_INTERVAL, int(poll_interval_sec)))
            self._save_users()
            return user

    # ------------------------------------------------------------- 텔레그램 자동 연결
    @staticmethod
    def _code_valid(user: User) -> bool:
        if not user.telegram_link_code or not user.telegram_link_code_at:
            return False
        try:
            issued = datetime.fromisoformat(user.telegram_link_code_at)
        except ValueError:
            return False
        return (datetime.now() - issued).total_seconds() < LINK_CODE_TTL_SEC

    def ensure_link_code(self, user: User) -> str:
        """유효한 연결 코드가 없거나 만료됐으면 6자리 숫자 코드를 새로 만든다. 다른 사용자와 겹치지 않게."""
        if self._code_valid(user):
            return user.telegram_link_code
        with self._lock:
            taken = {u.telegram_link_code for u in self._users.values() if u.telegram_link_code}
            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                if code not in taken:
                    break
            user.telegram_link_code = code
            user.telegram_link_code_at = datetime.now().isoformat(timespec="seconds")
            self._save_users()
            return code

    def by_link_code(self, code: str) -> User | None:
        code = code.strip()
        if not code:
            return None
        u = next((u for u in self._users.values() if u.telegram_link_code == code), None)
        return u if u is not None and self._code_valid(u) else None

    def link_telegram(self, user: User, chat_id: str, name: str) -> None:
        with self._lock:
            user.telegram_chat_id = str(chat_id)
            user.telegram_name = name[:60]
            user.telegram_link_code = ""
            user.telegram_link_code_at = ""
            self._save_users()

    def unlink_telegram(self, user: User) -> None:
        with self._lock:
            user.telegram_chat_id = ""
            user.telegram_name = ""
            self._save_users()

    def delete(self, user_id: str) -> bool:
        with self._lock:
            if self._users.pop(user_id, None) is None:
                return False
            stale = [tok for tok, s in self._sessions.items() if s.get("user_id") == user_id]
            for tok in stale:
                self._sessions.pop(tok, None)
            self._save_users()
            self._save_sessions()
            return True

    # ------------------------------------------------------------- 세션
    def create_session(self, user: User) -> str:
        with self._lock:
            self._purge_sessions()
            token = secrets.token_urlsafe(32)
            self._sessions[token] = {"user_id": user.id, "created_at": datetime.now().isoformat(timespec="seconds")}
            self._save_sessions()
            return token

    def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return None
        try:
            created = datetime.fromisoformat(s["created_at"])
        except (KeyError, ValueError):
            return None
        if datetime.now() - created > timedelta(days=SESSION_DAYS):
            return None
        return self._users.get(s.get("user_id", ""))

    def delete_session(self, token: str | None) -> None:
        if token and token in self._sessions:
            with self._lock:
                self._sessions.pop(token, None)
                self._save_sessions()

    def _purge_sessions(self) -> None:
        cutoff = datetime.now() - timedelta(days=SESSION_DAYS)
        stale = []
        for tok, s in self._sessions.items():
            try:
                if datetime.fromisoformat(s["created_at"]) < cutoff:
                    stale.append(tok)
            except (KeyError, ValueError):
                stale.append(tok)
        for tok in stale:
            self._sessions.pop(tok, None)


user_store = UserStore()
