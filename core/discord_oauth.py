# -*- coding: utf-8 -*-
"""
"디스코드로 로그인"(OAuth2) 코드.

관리자 페이지는 비밀번호 하나로 누구나 같은 자격으로 들어가지만, "내 목소리 설정"
페이지(web/app.py의 /my-voice)는 주소만 알면 누구나 열 수 있는 "오픈 페이지"라서
"지금 이 요청을 보낸 사람이 실제로 어떤 디스코드 유저인지" 확인이 따로 필요하다.
그래서 비밀번호 대신 디스코드 로그인을 붙였다 — 로그인하면 그 사람 본인 계정으로만
자기 TTS 목소리를 바꿀 수 있다.

동작하려면 디스코드 개발자 포털의 OAuth2 탭에서:
  1. Redirect URL에 "{WEB_PUBLIC_URL}/oauth/callback"을 등록하고
  2. Client ID/Secret을 발급받아 .env의 DISCORD_CLIENT_ID/DISCORD_CLIENT_SECRET에 넣어야 한다.
(README의 "내 목소리 설정 페이지(디스코드 로그인) 켜기" 참고)
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
USER_URL = "https://discord.com/api/users/@me"


def get_redirect_uri() -> str:
    explicit = os.getenv("DISCORD_REDIRECT_URI")
    if explicit:
        return explicit
    base = (os.getenv("WEB_PUBLIC_URL") or "").rstrip("/")
    return f"{base}/oauth/callback"


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": os.getenv("DISCORD_CLIENT_ID", ""),
        "redirect_uri": get_redirect_uri(),
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_user(code: str) -> dict:
    """인가 코드를 액세스 토큰으로 바꾸고, 바로 유저 정보(/users/@me)까지 받아온다.

    토큰 자체는 저장하지 않는다 — 필요한 건 "이 사람이 누구인지"뿐이라, 확인이
    끝나면 액세스 토큰은 그냥 버린다(세션에는 유저 id만 남긴다).
    """
    data = {
        "client_id": os.getenv("DISCORD_CLIENT_ID", ""),
        "client_secret": os.getenv("DISCORD_CLIENT_SECRET", ""),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": get_redirect_uri(),
    }
    r = requests.post(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10
    )
    r.raise_for_status()
    access_token = r.json()["access_token"]

    r2 = requests.get(USER_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    r2.raise_for_status()
    return r2.json()
