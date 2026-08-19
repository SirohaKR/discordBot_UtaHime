# -*- coding: utf-8 -*-
"""에러 발생 시 디스코드로 알리고 DB에 기록하는 공용 헬퍼.

main.py의 전역 에러 핸들러와 cogs/music.py의 주요 실패 지점에서 함께 사용한다.
"""

import os

import discord

from core import error_log_db

ERROR_LOG_CHANNEL_ID = os.getenv("ERROR_LOG_CHANNEL_ID")
OWNER_ID = os.getenv("OWNER_ID")


async def notify_error(bot, guild_id, message: str) -> None:
    occurred_at = discord.utils.utcnow().isoformat()
    try:
        await error_log_db.log_error(bot.loop, guild_id, message, occurred_at)
    except Exception as e:
        print(f"⚠️ [WARN] 에러 로그 기록 실패: {e}")

    target = None
    if ERROR_LOG_CHANNEL_ID:
        try:
            target = bot.get_channel(int(ERROR_LOG_CHANNEL_ID))
        except (TypeError, ValueError):
            target = None
    if not target and OWNER_ID:
        try:
            target = bot.get_user(int(OWNER_ID)) or await bot.fetch_user(int(OWNER_ID))
        except Exception:
            target = None
    if not target:
        return

    try:
        await target.send(f"⚠️ **에러 발생**\n```{message[:1800]}```")
    except Exception as e:
        print(f"⚠️ [WARN] 에러 알림 전송 실패: {e}")
