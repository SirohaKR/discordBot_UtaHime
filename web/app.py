# -*- coding: utf-8 -*-
"""
랑다AI(우타히메 봇) 관리 웹페이지.

봇 프로세스(main.py)와는 별개로 켜서 브라우저로 접속해 쓰는 "관리자 설정 페이지"다.
두 봇의 웹을 하나로 합친 것이다:
  - 서버 관리 (시로냥 봇에서 가져옴): 입장 안내/캐릭터 인증/역할 선택/공지사항/길드 규칙/
    음성 채널 자동 생성/TTS/길드 직급/역할 부여/채팅 정리/업데이트 로그 채널.
    디스코드 REST API(core/discord_api.py)를 봇 토큰으로 직접 호출하고, 설정은
    core/settings_store.py(data/settings.json)에 저장한다.
  - 음악 (우타히메 원래 웹): 기본 볼륨/셔플/반복, 플레이리스트 북마크, 재생 로그.
    playlists.db(SQLite)를 봇과 공유한다.
  - 공개 페이지: /docs(사용법), /prompt-guide(프롬프트 빌더), /my-voice(디스코드 로그인 후 내 TTS 목소리).

버튼 클릭 -> 인증 스레드, 이모지 -> 역할 부여, 허브 입장 -> 음성방 생성, TTS 읽기 같은
실시간 동작은 main.py(cogs/*)가 담당한다.

인증은 로그인 페이지(비밀번호 = WEB_ADMIN_TOKEN) 방식이다. 예전 우타히메 웹처럼
`?token=<WEB_ADMIN_TOKEN>` 링크로 들어와도 바로 로그인된다.
"""
from __future__ import annotations

import asyncio
import io
import os
import secrets
import sys
import time

from dotenv import load_dotenv
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for

# "python web/app.py"로 실행하면 파이썬이 기본적으로 web/ 폴더만 찾다보니, 한 단계
# 위에 있는 core/ 폴더(core/discord_api.py, core/settings_store.py)를 못 찾아서
# "ModuleNotFoundError: No module named 'core'" 오류가 난다. 아래 줄로 프로젝트
# 최상위 폴더(main.py가 있는 곳)를 검색 경로에 직접 추가해서 해결한다.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import yt_dlp as ytdl  # noqa: E402

from core import discord_api, discord_oauth, settings_store  # noqa: E402
from core import chat_channel_db, prompt_channel_db  # noqa: E402
from core import error_log_db, guild_settings_db, now_playing_db, playback_log_db, playlist_db  # noqa: E402
from core.tts_catalog import list_typecast_voices, search_typecast_voices  # noqa: E402
from core.tts_engine import synthesize  # noqa: E402
from core.tts_voices import available_voices  # noqa: E402

load_dotenv()

WEB_ADMIN_TOKEN = os.getenv("WEB_ADMIN_TOKEN")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

if not WEB_ADMIN_TOKEN:
    print("⚠️ [WARN] WEB_ADMIN_TOKEN이 설정되지 않았습니다. 아무도 로그인할 수 없습니다 (.env 확인).")

# 길드 직급 목록 (위쪽일수록 높은 직급). 이모지로 셀프 지급하면 아무나 "길드마스터"를
# 누를 수 있게 되므로, 직급은 아래 멤버 목록에서 관리자가 직접 눌러서만 부여한다.
GUILD_RANKS = ["길드마스터", "부길드장", "길드원", "신입길드원"]

# 캐릭터 그림 등 미리 준비해둔 이미지를 넣어두는 폴더. 공지/안내 이미지를 고를 때
# 매번 파일을 다시 올리지 않고 여기서 골라 쓸 수 있게 한다 (아래 "내장 이미지" 탭).
IMG_DIR = os.path.join(PROJECT_ROOT, "img")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# TTS 목소리 목록은 core/tts_voices.py에 모아뒀다 (cogs/tts.py의 "/목소리설정"
# 명령어와 같은 목록을 공유해서 쓰기 위함). 목록에 없는 목소리를 쓰고 싶으면
# 페이지의 "직접 입력" 칸에 정확한 이름을 적으면 된다.


def _curated_tts_voices() -> list[dict]:
    """추천 목소리 목록(available_voices())에 타입캐스트 무료 미리듣기 주소를 붙여서
    돌려준다. 이게 없으면 화면에서 "미리듣기"를 누를 때마다 유료 합성 API를 불러서
    크레딧이 나간다 — 타입캐스트 목소리마다 준비된 무료 샘플(preview_url)을 대신 쓰면
    미리듣기는 공짜다."""
    catalog_by_name = {}
    if os.getenv("TYPECAST_API_KEY"):
        catalog_by_name = {v["name"]: v for v in asyncio.run(list_typecast_voices())}

    voices = []
    for v in available_voices():
        entry = dict(v)
        if entry.get("engine") == "typecast":
            match = catalog_by_name.get(entry.get("typecast_name"))
            if match:
                entry["preview_url"] = match.get("preview_url")
        voices.append(entry)
    return voices


def _list_builtin_images() -> list[str]:
    if not os.path.isdir(IMG_DIR):
        return []
    found = []
    for root, _dirs, files in os.walk(IMG_DIR):
        for name in files:
            if name.lower().endswith(IMAGE_EXTENSIONS):
                rel = os.path.relpath(os.path.join(root, name), IMG_DIR)
                found.append(rel.replace(os.sep, "/"))
    return sorted(found)

ENTRANCE_BUTTON_COMPONENTS = [
    {
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 1,
                "label": "🔑 캐릭터 인증하러 가기",
                "custom_id": "siro:verify_start",
            }
        ],
    }
]


# 관리자 비밀번호 없이 들어올 수 있는 곳들. "내 목소리 설정"은 누구나 주소를 알면
# 열 수 있는 오픈 페이지고, 그 안에서 본인 확인은 비밀번호 대신 디스코드 로그인
# (oauth_*)으로 한다 — 그래서 관리자 세션(session["authed"])이 없어도 통과시킨다.
PUBLIC_ENDPOINTS = (
    "static",
    "login",
    "oauth_login",
    "oauth_callback",
    "oauth_logout",
    "my_voice",
    "my_voice_search_voices",
    "my_voice_preview",
    "docs",
    "prompt_guide",
)


@app.before_request
def require_auth():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("authed"):
        return None
    # 예전 우타히메 웹 방식(?token=...) 링크도 그대로 받아준다.
    token = request.args.get("token")
    if WEB_ADMIN_TOKEN and token == WEB_ADMIN_TOKEN:
        session["authed"] = True
        remaining = {k: v for k, v in request.args.items() if k != "token"}
        return redirect(request.path + (f"?{urlencode(remaining)}" if remaining else ""))
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if WEB_ADMIN_TOKEN and request.form.get("password") == WEB_ADMIN_TOKEN:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("index"))
        return render_template("login.html", error=True)
    return render_template("login.html", error=False)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def _my_guild_id() -> int | None:
    raw = os.getenv("MY_GUILD_ID")
    return int(raw) if raw else None


@app.route("/oauth/login")
def oauth_login():
    """"디스코드로 로그인" 버튼 → 디스코드 자체 로그인 화면으로 보낸다.

    state 값은 CSRF 방지용 임시 토큰이다 — 세션에 저장해뒀다가, 콜백에서 돌아온
    값이랑 같은지 확인해서, 이 로그인 시도가 내가 방금 시작한 게 맞는지 확인한다.
    """
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    session["oauth_next"] = request.args.get("next") or url_for("my_voice")
    return redirect(discord_oauth.build_authorize_url(state))


@app.route("/oauth/callback")
def oauth_callback():
    if not request.args.get("code") or request.args.get("state") != session.get("oauth_state"):
        return "⚠️ 로그인 요청이 올바르지 않아요. 다시 시도해주세요.", 400

    try:
        user = discord_oauth.exchange_code_for_user(request.args["code"])
    except Exception as e:
        return f"⚠️ 디스코드 로그인에 실패했어요: {e}", 500

    session.pop("oauth_state", None)
    session["discord_user_id"] = int(user["id"])
    return redirect(session.pop("oauth_next", None) or url_for("my_voice"))


@app.route("/oauth/logout", methods=["POST"])
def oauth_logout():
    session.pop("discord_user_id", None)
    return redirect(url_for("my_voice"))


@app.route("/my-voice", methods=["GET", "POST"])
def my_voice():
    """누구나 주소를 알면 열 수 있는 "내 목소리 설정" 페이지. 디스코드로 로그인해야
    본인 확인이 되고, 그 다음에도 실제로 이 길드 멤버여야만 목소리를 바꿀 수 있다."""
    guild_id = _my_guild_id()
    if not guild_id:
        return "⚠️ 아직 서버가 설정되지 않았어요. (.env의 MY_GUILD_ID를 확인해주세요)", 500

    user_id = session.get("discord_user_id")
    if not user_id:
        return render_template("my_voice.html", logged_in=False)

    member = discord_api.get_member(guild_id, user_id)
    if member is None:
        return render_template("my_voice.html", logged_in=True, not_member=True)

    settings = settings_store.get_guild_settings(guild_id)
    user_voices: dict = settings.get("tts_user_voices", {})

    if request.method == "POST":
        voice = (request.form.get("voice") or "").strip()
        if voice:
            user_voices[str(user_id)] = voice
        else:
            user_voices.pop(str(user_id), None)  # 빈 값 = "서버 기본값 쓰기"로 되돌림
        settings_store.update_guild_settings(guild_id, tts_user_voices=user_voices)
        return redirect(url_for("my_voice"))

    display_name = member.get("nick") or member["user"]["username"]

    return render_template(
        "my_voice.html",
        logged_in=True,
        not_member=False,
        display_name=display_name,
        tts_voices=_curated_tts_voices(),
        current_voice=user_voices.get(str(user_id)),
    )


@app.route("/my-voice/search-voices")
def my_voice_search_voices():
    return _search_voices_response(request.args.get("q", ""))


@app.route("/my-voice/preview")
def my_voice_preview():
    voice = request.args.get("voice") or "ko-KR-SunHiNeural"
    sample_text = request.args.get("text") or "안녕하세요! 이 목소리로 채팅 내용을 읽어드릴게요."
    return _tts_preview_response(voice, sample_text)


@app.route("/")
def index():
    guilds = discord_api.get_bot_guilds()
    if len(guilds) == 1:
        return redirect(url_for("guild_page", guild_id=guilds[0]["id"]))
    return render_template("index.html", guilds=guilds)


@app.route("/assets/<path:filename>")
def serve_asset(filename):
    """img/ 폴더의 내장 이미지를 브라우저에 보여준다. (로그인 세션이 있어야 접근 가능)"""
    return send_from_directory(IMG_DIR, filename)


def _channels(guild_id: int) -> dict:
    channels = discord_api.get_channels(guild_id)
    return {
        "text_channels": [c for c in channels if c["type"] == 0],
        "voice_channels": [c for c in channels if c["type"] == 2],
        "categories": [c for c in channels if c["type"] == 4],
        "channels_by_id": {int(c["id"]): c["name"] for c in channels},
    }


def _roles(guild_id: int) -> tuple[list, dict]:
    roles = [r for r in discord_api.get_roles(guild_id) if r["name"] != "@everyone"]
    roles_by_id = {int(r["id"]): r["name"] for r in roles}
    return roles, roles_by_id


def _post_embed(channel_id: int, title: str, body: str, image_url: str, image_file, components=None) -> dict:
    """공지성 임베드 하나를 채널에 올린다. 파일 첨부가 있으면 그걸 우선한다."""
    embed = {"title": title, "description": body, "color": 0x5B8CFF}
    if image_file and image_file.filename:
        filename = image_file.filename
        embed["image"] = {"url": f"attachment://{filename}"}
        return discord_api.send_message_with_file(channel_id, embed, filename, image_file.read(), components)
    if image_url:
        embed["image"] = {"url": image_url}
    return discord_api.send_message(channel_id, embed, components)


@app.route("/guild/<int:guild_id>")
def guild_page(guild_id):
    settings = settings_store.get_guild_settings(guild_id)
    ch = _channels(guild_id)
    _, roles_by_id = _roles(guild_id)

    return render_template(
        "dashboard.html",
        guild_id=guild_id,
        active="dashboard",
        page_title="🏠 대시보드",
        page_desc="랑다가 지금 어떻게 설정되어 있는지 한눈에 볼 수 있어요.",
        settings=settings,
        entrance=settings.get("entrance", {}),
        verification=settings.get("verification", {}),
        announcement=settings.get("announcement", {}),
        rules=settings.get("guild_rules", {}),
        job_list=settings.get("job_list", []),
        rank_role_ids=settings.get("rank_role_ids", {}),
        guild_ranks=GUILD_RANKS,
        channels_by_id=ch["channels_by_id"],
        text_channels=ch["text_channels"],
        roles_by_id=roles_by_id,
    )


@app.route("/guild/<int:guild_id>/bot-log", methods=["POST"])
def set_bot_log_channel(guild_id):
    """업데이트 내역을 자동으로 남길 채널을 지정한다. main.py가 시작할 때(또는 새 버전이
    감지될 때) CHANGELOG.md의 최신 항목을 이 채널에 올려준다."""
    channel_id = request.form.get("channel_id")
    settings_store.update_guild_settings(guild_id, bot_log_channel_id=int(channel_id) if channel_id else None)
    return redirect(url_for("guild_page", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/entrance", methods=["GET", "POST"])
def guild_entrance(guild_id):
    """입장안내 채널에 "캐릭터 인증하러 가기" 버튼이 달린 안내 메시지를 올린다.

    이 버튼 자체는 REST API로 바로 만들 수 있지만, 눌렸을 때 비공개 인증 스레드를
    만드는 실제 동작은 실시간으로 켜져 있는 main.py(cogs/verification.py)가 처리한다.
    """
    if request.method == "POST":
        channel_id = int(request.form["channel_id"])
        title = (request.form.get("title") or "").strip() or "입장 안내"
        body = (request.form.get("body") or "").strip()
        image_url = (request.form.get("image_url") or "").strip()
        image_file = request.files.get("image_file")

        message = _post_embed(channel_id, title, body, image_url, image_file, ENTRANCE_BUTTON_COMPONENTS)

        settings_store.update_guild_settings(
            guild_id,
            entrance={"channel_id": channel_id, "title": title, "body": body, "message_id": int(message["id"])},
        )
        return redirect(url_for("guild_entrance", guild_id=guild_id))

    entrance = settings_store.get_guild_settings(guild_id).get("entrance", {})
    return render_template(
        "entrance.html",
        guild_id=guild_id,
        active="entrance",
        page_title="🚪 입장 안내",
        page_desc="새로 들어온 길드원에게 보여줄 안내 메시지를 만들어요.",
        text_channels=_channels(guild_id)["text_channels"],
        entrance=entrance,
        builtin_images=_list_builtin_images(),
    )


@app.route("/guild/<int:guild_id>/verification", methods=["GET", "POST"])
def guild_verification(guild_id):
    """캐릭터 인증 안내 문구 + 인증 스레드를 만들 채널 + 승인 시 부여할 역할을 저장한다.

    여기서 저장한 값은 바로 채널에 게시되는 게 아니라, 멤버가 입장안내의 버튼을 눌러
    비공개 인증 스레드가 만들어질 때마다 그 스레드 안에 안내 문구로 쓰인다.
    """
    if request.method == "POST":
        channel_id = int(request.form["channel_id"])
        role_id = int(request.form["role_id"])
        title = (request.form.get("title") or "").strip() or "캐릭터 인증"
        body = (request.form.get("body") or "").strip()
        log_channel_id = request.form.get("log_channel_id")

        settings = settings_store.get_guild_settings(guild_id)
        existing_threads = (settings.get("verification") or {}).get("threads", {})

        settings_store.update_guild_settings(
            guild_id,
            verification={
                "channel_id": channel_id,
                "role_id": role_id,
                "title": title,
                "body": body,
                "threads": existing_threads,
                "log_channel_id": int(log_channel_id) if log_channel_id else None,
            },
        )
        return redirect(url_for("guild_verification", guild_id=guild_id))

    ch = _channels(guild_id)
    roles, _ = _roles(guild_id)
    verification = settings_store.get_guild_settings(guild_id).get("verification", {})
    return render_template(
        "verification.html",
        guild_id=guild_id,
        active="verification",
        page_title="🔑 캐릭터 인증",
        page_desc="입장 안내 버튼을 누르면 만들어지는 비공개 채널의 안내와, 승인 시 부여할 역할을 설정해요.",
        text_channels=ch["text_channels"],
        roles=roles,
        verification=verification,
    )


@app.route("/guild/<int:guild_id>/announcements", methods=["GET", "POST"])
def guild_announcements(guild_id):
    """이벤트/소식처럼 그때그때 알릴 공지사항을 원하는 채널에 올린다. 누를 때마다 새
    메시지로 게시된다 (계속 남겨둘 내용은 "길드 규칙" 페이지를 쓴다)."""
    if request.method == "POST":
        channel_id = int(request.form["channel_id"])
        title = (request.form.get("title") or "").strip() or "공지"
        body = (request.form.get("body") or "").strip()
        image_url = (request.form.get("image_url") or "").strip()
        image_file = request.files.get("image_file")

        message = _post_embed(channel_id, title, body, image_url, image_file)

        settings_store.update_guild_settings(
            guild_id,
            announcement={"channel_id": channel_id, "title": title, "body": body, "message_id": int(message["id"])},
        )
        return redirect(url_for("guild_announcements", guild_id=guild_id))

    announcement = settings_store.get_guild_settings(guild_id).get("announcement", {})
    return render_template(
        "announcements.html",
        guild_id=guild_id,
        active="announcements",
        page_title="📢 공지사항",
        page_desc="이벤트, 소식 등 그때그때 알릴 내용을 채널에 올려요. 누를 때마다 새 메시지로 게시돼요.",
        text_channels=_channels(guild_id)["text_channels"],
        announcement=announcement,
        builtin_images=_list_builtin_images(),
    )


@app.route("/guild/<int:guild_id>/rules", methods=["GET", "POST"])
def guild_rules(guild_id):
    """길드 규칙처럼 계속 남겨둘 안내를 채널에 올린다. 공지사항과 저장 위치만 다를 뿐
    동작은 동일하다 (구조를 나눈 이유는 07-14 업데이트로 두 기능을 분리했기 때문)."""
    if request.method == "POST":
        channel_id = int(request.form["channel_id"])
        title = (request.form.get("title") or "").strip() or "길드 규칙"
        body = (request.form.get("body") or "").strip()
        image_url = (request.form.get("image_url") or "").strip()
        image_file = request.files.get("image_file")

        message = _post_embed(channel_id, title, body, image_url, image_file)

        settings_store.update_guild_settings(
            guild_id,
            guild_rules={"channel_id": channel_id, "title": title, "body": body, "message_id": int(message["id"])},
        )
        return redirect(url_for("guild_rules", guild_id=guild_id))

    rules = settings_store.get_guild_settings(guild_id).get("guild_rules", {})
    return render_template(
        "rules.html",
        guild_id=guild_id,
        active="rules",
        page_title="📜 길드 규칙",
        page_desc="계속 남겨둘 길드 규칙을 채널에 올려요. 다시 게시하면 새 메시지로 올라가니, 이전 메시지는 직접 지워주세요.",
        text_channels=_channels(guild_id)["text_channels"],
        rules=rules,
        builtin_images=_list_builtin_images(),
    )


def _save_job_draft(guild_id: int) -> None:
    """공지 제목/본문 입력값을 저장해둔다. (이미지는 파일 첨부라 새로고침 후 되살릴 수
    없으므로 드래프트로 관리하지 않고, 게시할 때만 그 자리에서 첨부받는다)

    "목록에 추가"/"삭제"를 누를 때마다 페이지가 새로고침되는데, 그때 지금까지
    입력해둔 공지 내용이 날아가지 않도록 매번 같이 저장해서 다시 채워 넣는다.
    """
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    settings_store.update_guild_settings(guild_id, job_announcement_draft={"title": title, "body": body})


@app.route("/guild/<int:guild_id>/jobs")
def guild_jobs(guild_id):
    """채팅채널에 공지를 올리고 이모지 반응으로 역할을 셀프 선택하게 하는 페이지.

    캐릭터 인증(관리자 승인이 필요한 방식)과는 별개로, 직업처럼 "여러 개 중 하나를
    본인이 바로 고르면 되는" 역할에 쓰는 셀프 선택형 기능이다. 실제 반응 감지/역할
    부여는 cogs/roles.py가 담당한다.
    """
    settings = settings_store.get_guild_settings(guild_id)
    roles, roles_by_id = _roles(guild_id)
    return render_template(
        "jobs.html",
        guild_id=guild_id,
        active="jobs",
        page_title="🎭 역할 선택 (이모지 역할)",
        page_desc="채팅 채널에 공지를 올리고, 길드원이 이모티콘을 눌러 역할을 스스로 고르게 해요.",
        text_channels=_channels(guild_id)["text_channels"],
        roles=roles,
        roles_by_id=roles_by_id,
        job_list=settings.get("job_list", []),
        announcement_draft=settings.get("job_announcement_draft", {}),
        settings=settings,
        builtin_images=_list_builtin_images(),
    )


@app.route("/guild/<int:guild_id>/jobs/add", methods=["POST"])
def add_job(guild_id):
    """직업 목록에 한 줄 추가한다 (직업 이름 + 이모지 + 역할). 개수 제한 없음."""
    _save_job_draft(guild_id)

    label = (request.form.get("label") or "").strip()
    emoji = (request.form.get("emoji") or "").strip()
    role_id = request.form.get("role_id")

    if label and emoji and role_id:
        settings = settings_store.get_guild_settings(guild_id)
        job_list = settings.get("job_list", [])
        job_list.append({"label": label, "emoji": emoji, "role_id": int(role_id)})
        settings_store.update_guild_settings(guild_id, job_list=job_list)

    return redirect(url_for("guild_jobs", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/jobs/delete", methods=["POST"])
def delete_job(guild_id):
    """직업 목록에서 한 줄을 뺀다."""
    _save_job_draft(guild_id)

    index = request.form.get("index", type=int)
    settings = settings_store.get_guild_settings(guild_id)
    job_list = settings.get("job_list", [])

    if index is not None and 0 <= index < len(job_list):
        job_list.pop(index)
        settings_store.update_guild_settings(guild_id, job_list=job_list)

    return redirect(url_for("guild_jobs", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/jobs/post", methods=["POST"])
def post_job_roles(guild_id):
    """직접 쓴 공지 제목/본문/이미지 + 지금까지 추가해둔 직업 목록으로 실제 공지
    메시지를 올리고, 그 밑에 이모지를 붙인다."""
    channel_id = int(request.form["channel_id"])
    title = (request.form.get("title") or "").strip() or "공지"
    body = (request.form.get("body") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    image_file = request.files.get("image_file")
    _save_job_draft(guild_id)

    settings = settings_store.get_guild_settings(guild_id)
    job_list = settings.get("job_list", [])

    if not job_list:
        return redirect(url_for("guild_jobs", guild_id=guild_id))

    emoji_to_role = {}
    lines = []
    for job in job_list:
        emoji_to_role[job["emoji"]] = {"role_id": job["role_id"], "label": job["label"]}
        lines.append(f"{job['emoji']}  {job['label']}")

    # 관리자가 직접 쓴 공지 내용 밑에, 어떤 이모지가 어떤 역할인지 목록을 이어 붙인다.
    description = body
    if lines:
        description += ("\n\n" if description else "") + "\n".join(lines)

    message = _post_embed(channel_id, title, description, image_url, image_file)

    for emoji in emoji_to_role:
        discord_api.add_reaction(channel_id, message["id"], emoji)

    settings_store.update_guild_settings(
        guild_id,
        job_roles={
            "message_id": int(message["id"]),
            "channel_id": channel_id,
            "emoji_to_role": emoji_to_role,
        },
    )
    return redirect(url_for("guild_jobs", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/hub")
def guild_hub(guild_id):
    ch = _channels(guild_id)
    settings = settings_store.get_guild_settings(guild_id)
    return render_template(
        "hub.html",
        guild_id=guild_id,
        active="hub",
        page_title="🔊 음성 채널 자동 생성",
        page_desc="채널에 들어가면 개인/자유 음성방이 자동으로 생기게 해요. 허브는 원하는 만큼 추가할 수 있어요.",
        voice_channels=ch["voice_channels"],
        categories=ch["categories"],
        channels_by_id=ch["channels_by_id"],
        voice_hubs=settings.get("voice_hubs", []),
    )


@app.route("/guild/<int:guild_id>/hub/add", methods=["POST"])
def add_voice_hub(guild_id):
    """음성 허브를 하나 추가한다. 기존 채널을 고르거나, 이름을 입력해 새로 만들 수 있다.

    name_template 안의 {user}는 입장한 사람 이름, {n}은 번호로 바뀐다 (실제 치환은
    cogs/channels.py가 한다). user_limit을 정해두면 새로 만드는 채널마다 디스코드
    자체 인원 제한이 걸린다 (예: 1인 개인방은 1로 설정). 화면에는 자주 쓰는 형태
    (이름별/번호별/자유) 버튼을 미리 만들어뒀지만, 직접 원하는 값으로 바꿔도 된다.
    """
    existing_id = request.form.get("existing_channel_id")
    new_name = (request.form.get("new_channel_name") or "").strip()
    parent_id = request.form.get("parent_id") or None
    name_template = (request.form.get("name_template") or "").strip() or "{user}의 방"
    user_limit = request.form.get("user_limit", type=int) or 0

    if existing_id:
        hub_channel_id = int(existing_id)
    elif new_name:
        channel = discord_api.create_voice_channel(guild_id, new_name, int(parent_id) if parent_id else None)
        hub_channel_id = int(channel["id"])
    else:
        return redirect(url_for("guild_hub", guild_id=guild_id))

    settings = settings_store.get_guild_settings(guild_id)
    voice_hubs = settings.get("voice_hubs", [])
    voice_hubs.append(
        {"id": hub_channel_id, "name_template": name_template, "user_limit": max(0, min(99, user_limit))}
    )
    settings_store.update_guild_settings(guild_id, voice_hubs=voice_hubs)
    return redirect(url_for("guild_hub", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/hub/delete", methods=["POST"])
def delete_voice_hub(guild_id):
    """음성 허브 설정을 하나 뺀다. (허브로 쓰던 디스코드 채널 자체는 지우지 않는다)"""
    index = request.form.get("index", type=int)
    settings = settings_store.get_guild_settings(guild_id)
    voice_hubs = settings.get("voice_hubs", [])

    if index is not None and 0 <= index < len(voice_hubs):
        voice_hubs.pop(index)
        settings_store.update_guild_settings(guild_id, voice_hubs=voice_hubs)

    return redirect(url_for("guild_hub", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/tts", methods=["GET", "POST"])
def guild_tts(guild_id):
    """카톡 스타일 TTS 설정. 지정한 텍스트채널에 쓴 글을, 글쓴이가 있는 음성채널에서
    읽어준다 (실제 읽기는 main.py의 cogs/tts.py가 실시간으로 감시해서 처리한다)."""
    if request.method == "POST":
        channel_id = request.form.get("channel_id")
        voice = (request.form.get("custom_voice") or "").strip() or request.form.get("voice") or ""
        settings_store.update_guild_settings(
            guild_id,
            tts={"channel_id": int(channel_id) if channel_id else None, "voice": voice or None},
        )
        return redirect(url_for("guild_tts", guild_id=guild_id))

    tts = settings_store.get_guild_settings(guild_id).get("tts", {})

    return render_template(
        "tts.html",
        guild_id=guild_id,
        active="tts",
        page_title="🗣️ TTS",
        page_desc="지정한 채널에 쓴 글을, 글쓴이가 들어가있는 음성채널에서 읽어줘요 (카톡 스타일).",
        text_channels=_channels(guild_id)["text_channels"],
        tts=tts,
        tts_voices=_curated_tts_voices(),
    )


def _search_voices_response(query: str):
    """타입캐스트 목소리를 이름/성별/나이/용도로 검색한다. 관리자 페이지("🗣️ TTS")와
    "내 목소리 설정" 오픈 페이지가 같이 쓴다. 검색어가 비어있으면(아직 아무것도 안
    입력했으면) 전체 목록을 돌려줘서, 뭘 찾을지 몰라도 쭉 훑어보며 고를 수 있다."""
    results = asyncio.run(search_typecast_voices(query))
    return jsonify(results)


def _tts_preview_response(voice: str, sample_text: str):
    """목소리를 미리 들어볼 수 있게, 짧은 예시 문장을 그 자리에서 mp3로 만들어 돌려준다.
    디스코드와는 상관없이 브라우저에서 바로 재생해볼 수 있다."""
    try:
        audio_bytes = asyncio.run(synthesize(sample_text, voice))
    except Exception as e:
        return f"미리듣기를 만들지 못했어요: {e}", 500
    return Response(io.BytesIO(audio_bytes), mimetype="audio/mpeg")


@app.route("/guild/<int:guild_id>/tts/search-voices")
def tts_search_voices(guild_id):
    return _search_voices_response(request.args.get("q", ""))


@app.route("/guild/<int:guild_id>/tts/preview")
def tts_preview(guild_id):
    voice = request.args.get("voice") or "ko-KR-SunHiNeural"
    sample_text = request.args.get("text") or "안녕하세요! 이 목소리로 채팅 내용을 읽어드릴게요."
    return _tts_preview_response(voice, sample_text)


@app.route("/guild/<int:guild_id>/ranks", methods=["GET", "POST"])
def guild_ranks(guild_id):
    if request.method == "POST":
        rank_role_ids = {}
        for rank in GUILD_RANKS:
            role_id = request.form.get(f"role_{rank}")
            if role_id:
                rank_role_ids[rank] = int(role_id)
        settings_store.update_guild_settings(guild_id, rank_role_ids=rank_role_ids)
        return redirect(url_for("guild_ranks", guild_id=guild_id))

    roles, _ = _roles(guild_id)
    rank_role_ids = settings_store.get_guild_settings(guild_id).get("rank_role_ids", {})
    return render_template(
        "ranks.html",
        guild_id=guild_id,
        active="ranks",
        page_title="👑 길드 직급",
        page_desc="길드마스터/부길드장 같은 직급에 서버 역할을 연결해요.",
        roles=roles,
        guild_ranks=GUILD_RANKS,
        rank_role_ids=rank_role_ids,
    )


@app.route("/guild/<int:guild_id>/members", methods=["GET", "POST"])
def guild_members(guild_id):
    if request.method == "POST":
        member_id = int(request.form["member_id"])
        rank = request.form["rank"]

        settings = settings_store.get_guild_settings(guild_id)
        rank_role_ids: dict = settings.get("rank_role_ids", {})
        target_role_id = rank_role_ids.get(rank)

        if target_role_id:
            # 직급은 한 사람당 하나만 갖도록, 다른 직급 역할은 제거하고 새 직급만 부여한다.
            for other_rank, role_id in rank_role_ids.items():
                if role_id != target_role_id:
                    try:
                        discord_api.remove_role(guild_id, member_id, role_id)
                    except Exception:
                        pass  # 원래 그 직급이 아니었을 수도 있으니 실패해도 무시.
            discord_api.add_role(guild_id, member_id, target_role_id)

        return redirect(url_for("guild_members", guild_id=guild_id))

    settings = settings_store.get_guild_settings(guild_id)
    rank_role_ids: dict = settings.get("rank_role_ids", {})
    role_id_to_rank = {str(role_id): rank for rank, role_id in rank_role_ids.items()}

    members_raw = discord_api.get_members(guild_id)
    members = []
    for m in members_raw:
        if m["user"].get("bot"):
            continue
        member_role_ids = m.get("roles", [])
        current_rank = next((role_id_to_rank[rid] for rid in member_role_ids if rid in role_id_to_rank), None)
        members.append(
            {
                "id": m["user"]["id"],
                "name": m.get("nick") or m["user"]["username"],
                "current_rank": current_rank,
            }
        )

    return render_template(
        "members.html",
        guild_id=guild_id,
        active="members",
        page_title="🧑‍🤝‍🧑 역할 부여",
        page_desc="멤버를 골라서 직급 역할을 직접 부여해요 (셀프 지급 아님).",
        members=sorted(members, key=lambda m: m["name"].lower()),
        guild_ranks=GUILD_RANKS,
    )


def _cleanup_channel_messages(channel_id: int, limit: int | None) -> int:
    """channel_id의 메시지를 최근 것부터 limit개(None이면 채널이 빌 때까지 전부) 지운다.

    디스코드 자체 제한 때문에 14일 이내 메시지는 한 번에 최대 100개씩 묶어(bulk) 지우고,
    그보다 오래된 메시지는 하나씩 지운다 — 오래된 메시지가 많으면 그만큼 오래 걸린다.
    """
    deleted = 0
    fourteen_days_ago_snowflake = discord_api.snowflake_from_timestamp_ms(
        int((time.time() - 14 * 24 * 3600) * 1000)
    )
    before = None

    while limit is None or deleted < limit:
        batch_size = 100 if limit is None else min(100, limit - deleted)
        messages = discord_api.get_channel_messages(channel_id, limit=batch_size, before=before)
        if not messages:
            break

        recent_ids = [m["id"] for m in messages if int(m["id"]) > fourteen_days_ago_snowflake]
        old_ids = [m["id"] for m in messages if int(m["id"]) <= fourteen_days_ago_snowflake]

        if len(recent_ids) >= 2:
            discord_api.bulk_delete_messages(channel_id, recent_ids)
            deleted += len(recent_ids)
        elif len(recent_ids) == 1:
            discord_api.delete_message(channel_id, int(recent_ids[0]))
            deleted += 1

        for mid in old_ids:
            discord_api.delete_message(channel_id, int(mid))
            deleted += 1

        before = messages[-1]["id"]

    return deleted


@app.route("/guild/<int:guild_id>/cleanup", methods=["GET", "POST"])
def guild_cleanup(guild_id):
    """관리자가 지정한 채널의 메시지를 정리(대량 삭제)하는 페이지.

    되돌릴 수 없는 작업이라, "정말 지울게요" 체크박스에 직접 체크해야만 실제로
    삭제가 실행된다 (셀렉트에서 실수로 다른 채널을 고르고 그대로 제출하는 것을
    막기 위함 — 체크박스는 채널을 바꾸면 프론트엔드에서 자동으로 풀린다).
    """
    ch = _channels(guild_id)
    result = None

    if request.method == "POST":
        channel_id = int(request.form["channel_id"])
        amount = request.form.get("amount", "100")
        confirmed = request.form.get("confirm") == "on"
        channel_name = ch["channels_by_id"].get(channel_id)

        if channel_name is None or not confirmed:
            result = {"error": "삭제 확인 체크박스에 체크해주세요."}
        else:
            limit = None if amount == "all" else int(amount)
            try:
                deleted = _cleanup_channel_messages(channel_id, limit)
                result = {"deleted": deleted, "channel_name": channel_name}
            except Exception as e:
                result = {"error": f"삭제 중 오류가 발생했어요: {e}"}

    return render_template(
        "cleanup.html",
        guild_id=guild_id,
        active="cleanup",
        page_title="🧹 채팅 정리",
        page_desc="채널을 고르고 메시지를 한꺼번에 지워서 깨끗하게 정리해요. 지운 메시지는 복구할 수 없어요.",
        text_channels=ch["text_channels"],
        result=result,
    )



@app.route("/guild/<int:guild_id>/ai-chat", methods=["GET", "POST"])
def guild_ai_chat(guild_id):
    """랑다 자유 채팅 채널과 프롬프트 추천 채널을 지정한다 (예전 /채팅채널설정, /프롬프트채널설정).
    playlists.db에 저장되고, 봇은 메시지가 올 때마다 DB를 읽으므로 바로 반영된다."""
    if request.method == "POST":
        for key, db in (("chat_channel_id", chat_channel_db), ("prompt_channel_id", prompt_channel_db)):
            value = request.form.get(key)
            if value:
                db.set_channel(guild_id, int(value))
            else:
                db.clear_channel(guild_id)
        return redirect(url_for("guild_ai_chat", guild_id=guild_id))

    return render_template(
        "ai_chat.html",
        guild_id=guild_id,
        active="ai_chat",
        page_title="💬 AI 채팅",
        page_desc="랑다와 자유롭게 대화하는 채널, 문장/이미지를 올리면 그림 태그를 추천해주는 채널을 정해요.",
        text_channels=_channels(guild_id)["text_channels"],
        chat_channel_id=chat_channel_db.get_channel(guild_id),
        prompt_channel_id=prompt_channel_db.get_channel(guild_id),
    )


# =====================================================================
# 🎵 음악 설정 (우타히메 원래 웹) — playlists.db를 봇과 공유한다.
# 봇은 5초 간격으로 폴링해서 여기서 바뀐 값(볼륨/셔플/반복)을 반영한다.
# =====================================================================

ytdl.utils.bug_reports_message = lambda *args, **kwargs: ""
SEARCH_YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "default_search": "auto",
    # cogs/music.py의 YTDL_OPTIONS와 동일한 클라이언트 목록(403/DRM 회피용)을 사용한다.
    "extractor_args": {"youtube": {"player_client": ["web", "android", "tv", "ios"]}},
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


@app.route("/docs")
def docs():
    """봇 사용법 문서. 링크만 있으면 누구나 볼 수 있게 인증 없이 공개한다 (설정 변경 기능 없음)."""
    return render_template("docs.html")


@app.route("/prompt-guide")
def prompt_guide():
    """태그 카테고리를 클릭/입력하면 긍정/네거티브 프롬프트를 조합해주는 참고용 페이지.
    전부 클라이언트 사이드 JS로 동작 — 서버는 정적 페이지만 내려준다. 인증 불필요."""
    return render_template("prompt_guide.html")


@app.route("/guild/<int:guild_id>/music")
def guild_music(guild_id):
    settings = guild_settings_db.get_settings(guild_id)
    playlists = playlist_db.list_playlists_sync(guild_id)
    return render_template(
        "settings.html",
        guild_id=guild_id,
        settings=settings,
        volume_percent=round(settings["default_volume"] * 100),
        playlists=playlists,
        text_channels=_channels(guild_id)["text_channels"],
    )


@app.route("/guild/<int:guild_id>/settings", methods=["POST"])
def update_settings(guild_id):
    volume_percent = request.form.get("volume", type=int) or 30
    volume_percent = max(1, min(200, volume_percent))
    shuffle = request.form.get("shuffle") == "on"
    loop_mode = request.form.get("loop_mode", "off")
    if loop_mode not in ("off", "all", "one"):
        loop_mode = "off"

    # 음악 전용 채널 (예전 /설정 명령어). 바꾸면 봇이 몇 초 안에 감지해서 컨트롤러 메시지를 옮긴다.
    music_channel_id = request.form.get("music_channel_id")

    guild_settings_db.upsert_settings(
        guild_id,
        default_volume=volume_percent / 100.0,
        shuffle=shuffle,
        loop_mode=loop_mode,
        music_channel_id=int(music_channel_id) if music_channel_id else None,
    )
    return redirect(url_for("guild_music", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/playlists", methods=["POST"])
def add_playlist(guild_id):
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    if name and (url.startswith("http://") or url.startswith("https://")):
        playlist_db.add_playlist_sync(guild_id, name, url, "web", _now_iso())
    return redirect(url_for("guild_music", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/playlists/<path:name>/delete", methods=["POST"])
def delete_playlist(guild_id, name):
    playlist_db.delete_playlist_sync(guild_id, name)
    return redirect(url_for("guild_music", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/playlists/search")
def search_songs(guild_id):
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify([])

    try:
        with ytdl.YoutubeDL(SEARCH_YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        video_id = entry.get("id")
        url = entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
        if not url:
            continue
        thumbnails = entry.get("thumbnails") or []
        thumbnail = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
        results.append(
            {
                "title": entry.get("title") or "제목 없음",
                "url": url,
                "thumbnail": thumbnail,
                "duration": entry.get("duration"),
            }
        )
    return jsonify(results)


@app.route("/guild/<int:guild_id>/now_playing.json")
def now_playing_json(guild_id):
    data = now_playing_db.get_now_playing_sync(guild_id)
    return jsonify(data or {})


@app.route("/guild/<int:guild_id>/logs")
def guild_logs(guild_id):
    plays = playback_log_db.list_recent_playback_sync(guild_id, limit=50)
    errors = error_log_db.list_recent_errors_sync(guild_id, limit=20)
    return render_template("logs.html", guild_id=guild_id, plays=plays, errors=errors)


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
