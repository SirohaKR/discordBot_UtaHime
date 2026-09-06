# 공주님 (우타히메 봇)

디스코드 음악 재생 봇. discord.py 2.x + yt-dlp 기반.

## 기능

- **스마트 셔플**: 셔플을 켜는 순간에만 1회 섞고, 이후 곡을 추가해도 이미 정해진 순서를 흩뜨리지 않고 무작위 위치에만 끼워 넣음 → 몇백 곡이 쌓여도 즉각 반응.
- **반복 모드**: 끔 / 전체 반복 / 한 곡 반복.
- **다음 곡 프리페치**: 현재 곡 재생 중 다음 곡 스트림을 미리 불러와 전환 시 끊김을 줄임.
- **컨트롤러 메시지**: 버튼으로 일시정지/스킵/셔플/반복/대기열 보기/정지 조작, 진행바·볼륨·대기열 곡수 실시간 표시.
- **채널에 아무 텍스트나 입력해도 재생**: `/설정`으로 지정한 전용 채널에서는 `!play` 없이 검색어나 유튜브 링크를 그냥 입력하기만 해도 자동으로 대기열에 추가됨.
- **플레이리스트 북마크**: 자주 듣는 유튜브 플레이리스트 URL을 이름 붙여 저장, 서버별로 관리 (SQLite). 재생할 때 이름 뒤에 `셔플`/`반복`을 붙이면 바로 적용됨.
- **슬래시(/) 명령어**: 기존 `!` 접두사 명령과 동일하게 `/play`, `/join` 등 슬래시 명령어도 지원.
- **자동 정리**: 대기열이 5분간 비어 있으면 자동으로 음성 채널에서 퇴장.
- **웹 설정 페이지**: 볼륨/셔플/반복 기본값과 플레이리스트 북마크를 브라우저에서 관리. 검색어로 곡을 찾아 미리보고 골라서 플레이리스트에 추가 가능. 현재 재생 중인 곡도 실시간(5초 폴링)으로 표시. 봇이 5초 간격으로 폴링해서 거의 즉시 반영.
- **사용 로그/통계**: 최근 재생 기록과 에러 로그를 웹 페이지(`/guild/<id>/logs`)에서 확인 가능.
- **에러 알림**: 재생 실패, 처리되지 않은 예외 등을 지정한 디스코드 채널(또는 오너 DM)로 알림.
- **yt-dlp 자동 업데이트**: 주 1회 자동으로 yt-dlp를 최신 버전으로 업데이트하고, 버전이 바뀌면 안전하게 재시작(Docker의 `restart: unless-stopped`가 재기동). `!업데이트`로 즉시 수동 실행도 가능(봇 오너 전용).

## 기능 — 이미지 생성 (NovelAI + Claude)

- `/그림생성 프롬프트:<설명> [네거티브] [비율] [모델] [등급] [시드] [태그모드] [스타일참조] [스타일강도]` — 원하는 그림을 문장으로 설명하면(한국어 가능) Claude가 NovelAI Danbooru 태그로 자동 변환한 뒤 이미지 1장을 생성해 임베드로 전송. 입력 설명과 변환된 태그를 함께 보여줘서 태그 문법을 몰라도 쓸 수 있음.
  - `태그모드:True`로 켜면 AI 변환 없이 입력한 텍스트를 태그 그대로 사용 (태그 문법을 아는 사람용).
  - `스타일참조`에 이미지를 첨부하면 그 화풍/분위기를 새 그림에 입히는 Vibe Transfer 적용 (`스타일강도`로 세기 조절, 기본 0.6). 같은 이미지+모델 조합은 자동으로 캐싱돼서(`core/vibe_cache_db.py`) 재사용 시 Anlas가 추가로 안 나감 — 처음 인코딩할 때만 2 Anlas 소모.
  - `ANTHROPIC_API_KEY`가 없으면 자동 변환 없이 입력값을 그대로 태그로 사용(경고 메시지와 함께) — 없어도 기본 생성 기능은 동작함.
  - 서버 공용 계정(`.env`의 `NAI_TOKEN`) 하나로 전체 유저 요청을 처리하는 구조. 동시 요청은 자동으로 한 번에 하나씩 순서대로 처리됨.
  - 유저별 15초 쿨다운(스팸 방지).
  - 기본 해상도/스텝(28)은 전부 NovelAI 무료 티어 범위(≤1,048,576px) 안에서만 제공 — Opus 등 구독 플랜의 정액 요금 안에서 해결되고 Anlas가 추가로 빠지지 않음.
- `/애나니스` — Anlas(크레딧) 잔액과 Opus 구독 여부 확인.
- `/그림채널설정` — 현재 채널을 이미지 생성 전용 채널로 지정. 지정 후에는 그 채널에서만 `/그림생성` 사용 가능(다른 채널에서 시도하면 안내 메시지).
- `/그림채널설정해제` — 채널 제한 해제 (아무 채널에서나 다시 사용 가능).
- NovelAI 토큰 발급: NovelAI 로그인 → 좌측 톱니바퀴(User Settings) → Account 탭 → **Get Persistent API Token** → 복사해서 `.env`의 `NAI_TOKEN`에 붙여넣기.
- Claude API 키 발급(선택, 자동 프롬프트 변환용): [console.anthropic.com](https://console.anthropic.com) 가입 → 결제수단 등록 → API Keys에서 발급 → `.env`의 `ANTHROPIC_API_KEY`에 붙여넣기. Haiku 4.5 기준 이미지 1장당 약 $0.001~0.002 추가 비용(NovelAI 구독료와 별개, Anthropic 쪽에 종량제로 청구).
- 참고: NovelAI 연동은 아카라이브에 공개된 "Novel AI 이미지 생성 도구 개발용 API 레퍼런스"(DNT-LAB/NAIA_novel_ai_entrypoint)의 비공식 API 스펙을 기반으로 구현됨 (`core/nai_client.py`). 프롬프트 자동 변환은 `core/prompt_writer.py`.

## 명령어

접두사는 `!` 이며, 명령어 메시지는 실행 후 자동 삭제됩니다. 아래 명령어들은 모두 `/`(슬래시) 명령어로도 동일하게 사용할 수 있습니다.

### 재생

| 명령어 | 별칭 | 설명 |
|---|---|---|
| `!play <검색어\|URL>` | `!실행`, `!p` | 검색어/유튜브 링크/플레이리스트 링크를 대기열에 추가 |
| `!join` | `!입장` | (전용 채널 지정 후) 봇을 음성 채널에 연결 |
| `!leave` | `!퇴장`, `!stop` | (전용 채널에서만) 대기열을 비우고 음성 채널에서 퇴장 |
| `!설정` | `!setup`, `!set_music_channel` | **처음 한 번** 현재 채널을 전용 채널로 지정 (컨트롤러 메시지 생성). 이후 `join`/`leave`/`play`는 이 채널에서만 동작 |
| `!volume [값]` | `!vol`, `!볼륨` | 볼륨 조회(`!vol`) / 설정(`!vol 30`) / 상대 조절(`!vol +10`, `!vol -20`) |

### 플레이리스트 북마크

| 명령어 | 별칭 | 설명 |
|---|---|---|
| `!플레이리스트추가 <이름> <url>` | `!pladd`, `!playlist_add` | 유튜브 링크를 이름으로 저장 |
| `!플레이리스트재생 <이름> [셔플\|반복]` | `!plplay`, `!playlist_play` | 저장된 링크를 대기열에 추가 (내부적으로 `!play`와 동일 경로). 뒤에 `셔플`을 붙이면 즉시 셔플 ON, `반복`을 붙이면 즉시 전체 반복 ON |
| `!플레이리스트목록` | `!pllist`, `!playlist_list` | 이 서버에 저장된 플레이리스트 목록 확인 |
| `!플레이리스트삭제 <이름>` | `!pldel`, `!playlist_delete` | 저장된 플레이리스트 삭제 |

### 컨트롤러 버튼 (전용 채널의 임베드 메시지)

| 버튼 | 기능 |
|---|---|
| ⏸️ 일시정지 / ▶️ 재개 | 재생 일시정지/재개 |
| ⏭️ 스킵 | 현재 곡 건너뛰기 |
| 🔀 셔플 ON/OFF | 셔플 토글 |
| 🔁 반복: 끔 → 전체 → 한 곡 | 반복 모드 순환 |
| 📄 대기열 보기 | 현재 대기열 목록 표시 (최대 15곡 + 나머지 개수) |
| 🔇 정지/퇴장 | 대기열 비우고 음성 채널 퇴장 |

### 운영 (봇 오너 전용)

| 명령어 | 별칭 | 설명 |
|---|---|---|
| `!업데이트` | `!update` | yt-dlp를 즉시 최신 버전으로 업데이트 (버전이 바뀌면 적용을 위해 봇을 직접 재시작해야 함) |

## 웹 설정 페이지

`web/app.py`는 봇과 완전히 분리된 Flask 프로세스로, 같은 `playlists.db`를 공유해서 읽고 쓴다.

- 접속: `http://<서버주소>:5000/?token=<.env의 WEB_ADMIN_TOKEN>` — 한 번 접속하면 세션 쿠키가 발급되어 이후엔 토큰 없이 접근 가능
- 서버(길드) 단위로 기본 볼륨/셔플/반복 모드를 수정하고, 플레이리스트 북마크를 추가/삭제
- 플레이리스트 추가 시 검색어를 입력하면 유튜브 검색 결과(썸네일 포함)를 보여주고 클릭해서 선택 가능 (유튜브 링크를 그대로 붙여넣어도 동작)
- 현재 재생 중인 곡을 5초 간격으로 폴링해서 실시간 표시
- `/guild/<id>/logs`에서 최근 재생 기록/에러 로그 확인 가능
- OAuth 로그인은 없음 — 비밀 토큰 링크 하나로 접근 제어 (개인/소규모 운영 기준)
- 여기서 저장한 값은 봇이 5초 간격으로 폴링해서 재시작 없이 반영

```powershell
python web/app.py
```

## 설정 저장 구조

`bot_settings.json`(레거시 파일)은 더 이상 쓰지 않고, 모든 서버별 설정은 `playlists.db`의 `guild_settings` 테이블에 저장됩니다.
기존 `bot_settings.json`이 남아 있으면 봇이 처음 켜질 때 자동으로 DB로 1회 마이그레이션하고 `bot_settings.json.migrated`로 이름을 바꿔둡니다.

같은 `playlists.db` 파일 안에 `now_playing`(현재 재생 상태), `playback_log`(재생 기록), `error_log`(에러 기록) 테이블도 함께 저장되며, 모두 봇/웹 페이지 첫 실행 시 자동 생성됩니다.

## 프로젝트 구조

```
main.py                     # 부트스트랩 (.env 로드 → cog 로드 → 슬래시 동기화 → 자동 업데이트 태스크 → 봇 시작)
cogs/music.py                # 음악 재생 + 플레이리스트 북마크 기능
cogs/image.py                # NovelAI 이미지 생성 (/그림생성, /애나니스)
core/nai_client.py            # NovelAI 이미지 생성 API 클라이언트 (aiohttp)
core/prompt_writer.py          # 자연어 설명 -> Danbooru 태그 변환 (Claude Haiku 4.5)
core/image_settings_db.py      # 이미지 생성 전용 채널 설정 저장소 (SQLite, playlists.db 공유)
core/vibe_cache_db.py          # Vibe Transfer 인코딩 결과 캐시 (SQLite, playlists.db 공유)
core/guild_settings_db.py     # 서버별 설정 저장소 (SQLite, 웹페이지와 공유)
core/song_queue.py            # 스마트 셔플 큐
core/playlist_db.py           # 플레이리스트 북마크 저장소 (SQLite, playlists.db)
core/now_playing_db.py        # 현재 재생 상태 저장소 (웹페이지 실시간 표시용)
core/playback_log_db.py       # 재생 기록 저장소
core/error_log_db.py          # 에러 기록 저장소
core/error_notify.py          # 에러 발생 시 디스코드 알림 + error_log 기록 공용 헬퍼
web/app.py                    # 설정용 Flask 웹페이지 (별도 프로세스)
web/templates/                # 웹페이지 HTML (settings.html, logs.html)
playlists.db                   # 서버별 설정 + 플레이리스트 북마크 + 로그 DB (자동 생성)
```

새 기능(음악 외)을 추가할 때는 `cogs/`에 새 파일을 만들고 `main.py`의 `INITIAL_EXTENSIONS`에 경로만 추가하면 됩니다.

## 실행

### 로컬

```powershell
cd C:\botPython
pip install -r requirements.txt
python main.py          # 봇
python web/app.py        # 웹 설정 페이지 (선택, 별도 터미널)
```

`.env`에 `DISCORD_TOKEN`이 설정되어 있어야 하며, FFmpeg는 `FFMPEG_PATH` 환경 변수 또는 PATH에서 자동으로 찾습니다.
웹페이지를 쓰려면 `.env`의 `WEB_ADMIN_TOKEN`/`FLASK_SECRET_KEY`도 설정하세요 (기본값이 이미 랜덤하게 채워져 있습니다. 운영 시 원하면 교체하세요).
에러를 디스코드로 받고 싶으면 `.env`의 `ERROR_LOG_CHANNEL_ID`(채널 ID) 또는 `OWNER_ID`(내 유저 ID)를 채워주세요. 둘 다 비워두면 콘솔에만 출력됩니다.

### 시놀로지 NAS (Docker / Container Manager) — 처음 설정하는 경우

1. **Container Manager 설치**: NAS의 패키지 센터(Package Center)에서 `Container Manager`(구 Docker) 검색 후 설치. DSM 7.2 이상 필요.
2. **프로젝트 폴더 복사**: File Station 또는 SMB 공유 폴더로 `C:\botPython` 전체(단, `.env`는 실제 토큰이 들어있으니 직접 확인 후 복사)를 NAS의 원하는 공유 폴더(예: `docker/utahime-bot`)로 복사.
3. **`.env` 확인**: NAS에 올라간 폴더의 `.env`가 로컬과 같은 값(DISCORD_TOKEN 등)인지 확인. `FFMPEG_PATH`는 비워두면 됨(컨테이너 안에 Dockerfile이 ffmpeg를 설치함).
4. **Container Manager → 프로젝트(Project) → 생성**: 경로에 방금 복사한 폴더 지정 → `docker-compose.yml`을 자동 인식 → 소스가 `docker-compose.yml`로 지정되어 있는지 확인 후 다음.
5. **빌드 및 실행**: 빌드가 끝나면 `utahime-bot`, `utahime-web` 두 컨테이너가 뜸. `bot_settings.json`, `playlists.db`가 볼륨 마운트되어 컨테이너를 재생성해도 데이터가 유지됨.
6. **동작 확인**:
   - Container Manager → 컨테이너 → `utahime-bot` → 로그 탭에서 `✅ 봇 실행/연결 완료`가 보이면 정상.
   - 웹페이지는 `http://<NAS IP>:5050/?token=<WEB_ADMIN_TOKEN>`으로 접속 (NAS 자체 관리 페이지가 5000번 포트를 쓰기 때문에 `docker-compose.yml`에서 호스트 쪽 포트를 5050으로 매핑해뒀습니다. 컨테이너 내부 포트는 그대로 5000).
7. **자동 재시작/자동 업데이트**: `restart: unless-stopped`로 NAS 재부팅이나 봇 크래시 시에도 자동 재기동. yt-dlp도 주 1회 자동으로 최신 버전으로 업데이트되고 자동 재시작됨(코드 재배포 불필요).
8. (선택) 외부에서 웹페이지에 접속하려면 NAS의 리버스 프록시/DDNS 설정이 별도로 필요 — 필요하면 그때 추가로 도와드릴 수 있음.
