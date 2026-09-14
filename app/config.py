"""환경 변수 로딩. 값은 .env 파일에서 읽는다."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """한국 시간 기준 현재 시각(naive). 서버가 UTC 여도 코레일 시간표와 같은 기준을 쓰기 위함."""
    return datetime.now(KST).replace(tzinfo=None)

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SECRET_FILE = DATA_DIR / "secret.key"

load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    invite_code: str
    admin_username: str
    poll_interval_sec: int
    host: str
    port: int

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


settings = Settings(
    telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    invite_code=os.getenv("INVITE_CODE", "").strip(),
    admin_username=(os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"),
    poll_interval_sec=max(30, _int("POLL_INTERVAL_SEC", 60)),
    host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
    port=_int("PORT", 8000),
)

DATA_DIR.mkdir(exist_ok=True)
try:
    os.chmod(DATA_DIR, 0o700)  # 다른 로컬 계정이 사용자 파일을 읽지 못하게. Windows 에서는 무시됨
except OSError:
    pass


def write_private(path: Path, text: str) -> None:
    """소유자만 읽을 수 있는 권한(600)으로 파일을 쓴다."""
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def secret_key() -> bytes:
    """코레일 비밀번호 암호화에 쓰는 서버 키. 없으면 생성해 data/secret.key 에 보관한다.

    이 파일을 잃으면 저장된 코레일 비밀번호를 복호화할 수 없어 사용자가 다시 입력해야 한다.
    """
    try:
        raw = SECRET_FILE.read_text(encoding="utf-8").strip()
        if len(raw) == 64:
            return bytes.fromhex(raw)
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(32)
    write_private(SECRET_FILE, key.hex())
    return key
