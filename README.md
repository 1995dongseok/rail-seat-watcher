# 기차 빈자리 조회 (rail-seat-watcher)

코레일 계정으로 KTX 등 기차 잔여석을 조회하고, 빈자리가 생기면 텔레그램으로 알려주는 소수 지인용 웹 앱입니다.

- 사용자마다 앱 계정을 만들고(초대코드 필요), 각자 코레일 계정과 텔레그램 chat_id 를 등록합니다.
- 조회: 출발역/도착역(드롭다운), 날짜, 시간 범위(또는 하루 전체), 열차 종류. 왕복이면 가는 편·오는 편을 동시에 조회. 조회 결과(빈자리 열차 목록)는 즉시 텔레그램으로도 전송
- 감시: 조건을 등록하면 주기적으로 조회해 매진 → 가능 으로 바뀐 열차를 등록한 사람의 텔레그램으로 알림

## 주의

- 코레일 공식 API가 아니라 코레일 앱 API를 감싼 비공식 라이브러리(pykorail)를 사용합니다. 코레일이 앱을 바꾸면 예고 없이 멈출 수 있습니다.
- 코레일 이용약관은 자동화 도구 사용을 제한합니다. 모든 사용자의 요청이 서버 한 곳에서 나가므로 사용자와 감시 건수가 늘수록 조회 주기를 넉넉히 잡으세요. 서버는 코레일 호출을 한 번에 하나씩만 보내고 감시 건 사이에 3초를 쉽니다.
- 코레일 비밀번호는 `data/secret.key` 로 암호화해 `data/users.json` 에 저장합니다. `data/` 폴더와 `.env` 는 공유하거나 커밋하지 마세요. `secret.key` 를 잃으면 사용자들이 코레일 비밀번호를 다시 입력해야 합니다.
- 통신은 HTTP 입니다. 집이나 사무실 같은 신뢰할 수 있는 네트워크 안에서만 쓰세요. 외부 공개는 권하지 않습니다.

## 설치

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` 를 열어 값을 채웁니다.

| 항목 | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather 에서 만든 봇 토큰. 서버에 하나만 둡니다 |
| `INVITE_CODE` | 가입 초대코드. 지인에게 알려 주면 가입할 수 있습니다 |
| `POLL_INTERVAL_SEC` | 감시 주기(초). 최소 30, 기본 60. 사용자가 여럿이면 120 이상 권장 |
| `HOST` | 같은 네트워크의 다른 기기에서 접속하려면 `0.0.0.0` |

## 실행

```powershell
.\venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000 을 엽니다. 다른 기기에서 접속하려면 `--host 0.0.0.0` 으로 띄우고 서버 PC 의 IP 로 접속합니다.

## 관리자

- `.env` 의 `ADMIN_USERNAME`(기본 `admin`) 이름으로 가입한 계정이 관리자입니다. 관리자는 항상 사용 허용 상태입니다.
- 신규 가입자는 **기본 거부** 상태라 조회·감시 버튼이 비활성화되고 서버도 요청을 거부합니다. 관리자가 `/admin` 에서 **허용**을 눌러야 사용할 수 있습니다.
- 텔레그램은 사용자가 "내 설정"의 6자리 연결 코드를 봇에게 보내면 서버가 자동으로 chat_id 를 연결합니다. 관리자는 `/admin` 에서 연결 상태를 보고 필요하면 chat_id 를 직접 고칠 수 있습니다.
- 서버가 봇의 getUpdates 를 계속 읽고 있으므로, 브라우저에서 getUpdates 주소를 직접 열면 서로 충돌합니다(409). 수동 확인은 더 이상 필요 없습니다.
- 관리자는 사용자를 삭제할 수 있고, 삭제하면 그 사용자의 감시도 함께 지워집니다.
- **감시 상한**: 사용자당 동시에 활성화할 수 있는 감시는 기본 2건입니다(중지·종료된 감시는 제외). 왕복 등록은 2건을 씁니다. 관리자는 `/admin` 에서 사용자별 상한(0~20)을 조정할 수 있고, 관리자 본인은 무제한입니다. 상한을 넘기는 등록과 재개는 서버가 거부합니다.

## 사용자 안내

1. 로그인 화면에서 **가입** 탭을 눌러 사용자명, 비밀번호, 초대코드를 입력합니다.
2. 로그인 후 **내 설정**을 펼쳐 코레일 아이디/비밀번호를 저장합니다.
3. "내 설정"의 텔레그램 항목에서 봇 링크를 눌러 봇을 열고, 표시된 6자리 연결 코드를 보냅니다. 몇 초 안에 "연결됨"으로 바뀝니다.
4. 관리자가 `/admin` 에서 사용을 허용하면 **코레일 로그인 테스트**와 **텔레그램 테스트**로 확인합니다.
5. 조건을 조회하고 **이 조건 감시 등록**을 누르면 빈자리가 생길 때 텔레그램으로 알림이 옵니다.

## Lightsail 배포

서울 리전 Ubuntu 인스턴스에 systemd 서비스로 올리고, 기존 Caddy 가 HTTPS 를 붙입니다. 설정 파일은 `deploy/` 에 있습니다.

- 최초 1회 (인스턴스에서): `sudo bash deploy/setup-server.sh <도메인>`. 이미 Caddy 가 있으면 이 스크립트 대신 `/etc/caddy/Caddyfile` 에 아래 블록만 추가하고 `sudo systemctl reload caddy`.

```
rail-watcher.duckdns.org {
    reverse_proxy 127.0.0.1:8000
    encode gzip
}
```

- 코드 갱신 (이 PC 에서): `.\deploy\deploy.ps1 -HostName <인스턴스 IP>`. 서버의 `.env` 와 `data/` 는 유지됩니다.
- 서비스는 `TZ=Asia/Seoul` 로 실행되며, 코드도 한국 시간 기준으로 동작합니다.
- 로그: `sudo journalctl -u rail-seat-watcher -f`

## 구조

```
app/
  config.py          .env 로딩, 서버 암호화 키
  crypto.py          코레일 비밀번호 암복호화, 앱 비밀번호 해시
  users.py           사용자/세션 저장(data/users.json, data/sessions.json)
  korail_service.py  pykorail 래퍼 (사용자별 세션, 서버 전체 직렬화, 하루 전체 페이징 조회)
  telegram.py        텔레그램 발송(chat_id 별) + 연결 코드 수신 폴링(getUpdates)
  watcher.py         감시 조건 저장(data/watches.json) + 폴링 루프
  main.py            FastAPI 라우트
  static/login.html  로그인/가입 화면
  static/index.html  조회/감시 화면
```

## API

모든 `/api/*` 는 로그인 쿠키가 필요합니다(인증 API 제외).

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/auth/register` | 가입 (username, password, invite_code) |
| POST | `/api/auth/login` / `/api/auth/logout` | 로그인 / 로그아웃 |
| GET | `/api/me` | 내 정보 |
| PUT | `/api/me/settings` | 코레일 아이디/비밀번호 저장 |
| POST | `/api/me/korail/test` | 코레일 로그인 테스트 |
| POST | `/api/me/telegram/unlink` | 텔레그램 연결 해제(새 코드 발급) |
| POST | `/api/telegram/test` | 내 텔레그램으로 테스트 발송 |
| GET | `/api/admin/users` | (관리자) 사용자 목록 |
| PUT | `/api/admin/users/{id}` | (관리자) 허용/거부, chat_id, 감시 상한 변경 |
| DELETE | `/api/admin/users/{id}` | (관리자) 사용자와 감시 삭제 |
| GET | `/api/stations` | 역 목록(주요역/전체) |
| POST | `/api/search` | 즉시 조회(왕복은 화면에서 2회 호출) |
| GET/POST | `/api/watches` | 내 감시 목록 / 등록 |
| POST | `/api/watches/{id}/toggle` | 감시 중지/재개 |
| DELETE | `/api/watches/{id}` | 감시 삭제 |
| POST | `/api/watches/run-now` | 내 감시를 지금 한 번 점검 |
| GET | `/api/status` | 로그인/설정/마지막 점검 상태 |
