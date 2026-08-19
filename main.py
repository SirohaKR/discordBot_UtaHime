# -*- coding: utf-8 -*-
# 우타히메 봇 부트스트랩

import asyncio
import logging
import os
import subprocess
import sys
import traceback

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.error_notify import notify_error
from core.errors import HandledCommandError

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# 음성/재생 문제 진단용. DAVE 핸드셰이크 이슈는 확인 후 해결되었으므로
# 게이트웨이 원시 이벤트(JSON)는 다시 조용히 하고, 음성 연결/ffmpeg 재생 상태만 남긴다.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("discord.voice_client").setLevel(logging.INFO)
logging.getLogger("discord.player").setLevel(logging.INFO)

# 새 기능(cog)을 추가할 때 여기에 모듈 경로만 추가하면 됨.
INITIAL_EXTENSIONS = [
    "cogs.music",
]

# yt-dlp는 유튜브 정책 변화에 맞춰 자주 패치되므로, 주기적으로 자동 업데이트한다.
# 버전이 바뀌면 프로세스를 정상 종료하고, Docker의 restart:unless-stopped(또는 운영 중인
# 프로세스 매니저)가 새 site-packages 상태 그대로 재기동하도록 한다.
AUTO_UPDATE_INTERVAL_SECONDS = 7 * 24 * 3600  # 1주일

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def _run_yt_dlp_update(auto_restart: bool) -> bool:
    """yt-dlp를 최신 버전으로 업데이트한다. 버전이 바뀌었으면 True를 반환."""
    from importlib.metadata import version as pkg_version

    try:
        before = pkg_version("yt-dlp")
    except Exception:
        before = None

    print("🔄 [LOG] yt-dlp 업데이트 확인 중...")
    proc = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "-q", "yt-dlp"],
            capture_output=True,
            text=True,
        ),
    )
    if proc.returncode != 0:
        print(f"⚠️ [WARN] yt-dlp 업데이트 실패: {proc.stderr[-500:]}")
        return False

    try:
        after = pkg_version("yt-dlp")
    except Exception:
        after = None

    if before == after:
        print(f"ℹ️ [LOG] yt-dlp 이미 최신 버전({after})")
        return False

    print(f"✅ [LOG] yt-dlp 업데이트됨: {before} → {after}")
    if auto_restart:
        print("♻️ [LOG] 새 버전 적용을 위해 봇을 재시작합니다.")
        os._exit(0)
    return True


async def _auto_update_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(AUTO_UPDATE_INTERVAL_SECONDS)
        try:
            await _run_yt_dlp_update(auto_restart=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️ [WARN] 자동 업데이트 중 오류: {e}")


@bot.command(name="업데이트", aliases=["update"])
@commands.is_owner()
async def update_command(ctx):
    await ctx.send("🔄 yt-dlp 업데이트를 확인하는 중...")
    updated = await _run_yt_dlp_update(auto_restart=False)
    if updated:
        await ctx.send("✅ yt-dlp가 업데이트되었습니다. 적용하려면 봇을 재시작해주세요.")
    else:
        await ctx.send("ℹ️ 이미 최신 버전이거나 업데이트에 실패했습니다 (콘솔 로그 확인).")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure, HandledCommandError)):
        return
    # 슬래시(하이브리드) 경로에서는 HybridCommandError로 감싸져 올 수 있어 원본도 함께 확인한다.
    if isinstance(getattr(error, "original", None), HandledCommandError):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ 필요한 값이 빠졌습니다: `{error.param.name}`", delete_after=6)
        return

    print(f"❌ [ERROR] 명령 처리 중 오류: {error}")
    guild_id = ctx.guild.id if ctx.guild else None
    await notify_error(bot, guild_id, f"명령 `{ctx.command}` 처리 중 오류: {error}")


@bot.event
async def on_error(event_method, *args, **kwargs):
    tb = traceback.format_exc()
    print(f"❌ [ERROR] 처리되지 않은 예외 ({event_method}):\n{tb}")
    await notify_error(bot, None, f"이벤트 `{event_method}` 처리 중 예외:\n{tb[-1500:]}")


@bot.event
async def on_ready():
    print("\n" + "=" * 40)
    print(f"봇 이름: {bot.user.name}")
    print(f"봇 ID: {bot.user.id}")
    print("✅ 봇 실행/연결 완료")

    music_cog = bot.get_cog("MusicPlayer")
    if music_cog:
        await music_cog.migrate_legacy_settings()
        await music_cog.reconnect_controller_message()

    try:
        synced = await bot.tree.sync()
        print(f"🌳 [LOG] 슬래시 명령어 글로벌 동기화 완료 ({len(synced)}개)")
    except Exception as e:
        print(f"⚠️ [WARN] 슬래시 명령어 글로벌 동기화 실패: {e}")

    if music_cog and music_cog.guild_id:
        try:
            guild_obj = discord.Object(id=music_cog.guild_id)
            bot.tree.copy_global_to(guild=guild_obj)
            synced_guild = await bot.tree.sync(guild=guild_obj)
            print(f"🌳 [LOG] 슬래시 명령어 길드 동기화 완료 (guild={music_cog.guild_id}, {len(synced_guild)}개)")
        except Exception as e:
            print(f"⚠️ [WARN] 슬래시 명령어 길드 동기화 실패: {e}")

    print("=" * 40 + "\n")


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")

    async with bot:
        for extension in INITIAL_EXTENSIONS:
            await bot.load_extension(extension)
        bot.loop.create_task(_auto_update_loop())
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 봇 종료")
