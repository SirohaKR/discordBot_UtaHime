# -*- coding: utf-8 -*-
"""공주님 봇 설정용 Flask 웹페이지.

봇 프로세스와 완전히 분리된 별도 프로세스로 실행되며, 같은 SQLite 파일
(playlists.db)을 공유해서 읽고 쓴다. 봇은 5초 간격으로 폴링해서 여기서
바뀐 값(볼륨/셔플/반복)을 반영한다.

인증은 OAuth 없이 비밀 토큰 링크 방식: `?token=<WEB_ADMIN_TOKEN>`으로 처음
접속하면 세션 쿠키가 발급되고, 이후에는 토큰 없이도 접근 가능하다.
"""

import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

import yt_dlp as ytdl
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import error_log_db, guild_settings_db, now_playing_db, playback_log_db, playlist_db  # noqa: E402

load_dotenv()

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

WEB_ADMIN_TOKEN = os.getenv("WEB_ADMIN_TOKEN")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

if not WEB_ADMIN_TOKEN:
    print("⚠️ [WARN] WEB_ADMIN_TOKEN이 설정되지 않았습니다. 아무도 로그인할 수 없습니다 (.env 확인).")


@app.before_request
def require_auth():
    if request.endpoint == "static":
        return None

    if session.get("authed"):
        return None

    token = request.args.get("token")
    if WEB_ADMIN_TOKEN and token == WEB_ADMIN_TOKEN:
        session["authed"] = True
        remaining = {k: v for k, v in request.args.items() if k != "token"}
        query = f"?{urlencode(remaining)}" if remaining else ""
        return redirect(request.path + query)

    abort(403)


@app.errorhandler(403)
def forbidden(_e):
    return "접근 권한이 없습니다. 올바른 링크(토큰 포함)로 접속해주세요.", 403


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


@app.route("/")
def index():
    guild_ids = guild_settings_db.list_guild_ids()
    if len(guild_ids) == 1:
        return redirect(url_for("guild_page", guild_id=guild_ids[0]))
    return render_template("index.html", guild_ids=guild_ids)


@app.route("/guild/<int:guild_id>")
def guild_page(guild_id):
    settings = guild_settings_db.get_settings(guild_id)
    playlists = playlist_db.list_playlists_sync(guild_id)
    return render_template(
        "settings.html",
        guild_id=guild_id,
        settings=settings,
        volume_percent=round(settings["default_volume"] * 100),
        playlists=playlists,
    )


@app.route("/guild/<int:guild_id>/settings", methods=["POST"])
def update_settings(guild_id):
    volume_percent = request.form.get("volume", type=int) or 30
    volume_percent = max(1, min(200, volume_percent))
    shuffle = request.form.get("shuffle") == "on"
    loop_mode = request.form.get("loop_mode", "off")
    if loop_mode not in ("off", "all", "one"):
        loop_mode = "off"

    guild_settings_db.upsert_settings(
        guild_id,
        default_volume=volume_percent / 100.0,
        shuffle=shuffle,
        loop_mode=loop_mode,
    )
    return redirect(url_for("guild_page", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/playlists", methods=["POST"])
def add_playlist(guild_id):
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    if name and (url.startswith("http://") or url.startswith("https://")):
        playlist_db.add_playlist_sync(guild_id, name, url, "web", _now_iso())
    return redirect(url_for("guild_page", guild_id=guild_id))


@app.route("/guild/<int:guild_id>/playlists/<path:name>/delete", methods=["POST"])
def delete_playlist(guild_id, name):
    playlist_db.delete_playlist_sync(guild_id, name)
    return redirect(url_for("guild_page", guild_id=guild_id))


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
