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

from core.changelog import get_latest_entry
from core.error_notify import notify_error
from core.errors import HandledCommandError
from core.settings_store import get_guild_settings, update_guild_settings

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
    "cogs.prompt_suggest",
    "cogs.chat",  # 자유 채팅 (일상 대화 + 프롬프트 추천)
    "cogs.tts",  # 시로냥에서 가져온 TTS (텍스트채널 글을 음성채널에서 읽어줌)
    "cogs.channels",  # 시로냥에서 가져온 음성 허브 (자동 음성방 생성/삭제)
    "cogs.verification",  # 시로냥: 입장안내 버튼 -> 비공개 인증 스레드 -> 관리자 승인/거절
    "cogs.roles",  # 시로냥: 공지 이모지 반응으로 역할 셀프 선택
    "cogs.info",  # 시로냥: /관리페이지 (웹 관리 페이지 주소 안내)
]

# yt-dlp는 유튜브 정책 변화에 맞춰 자주 패치되므로, 주기적으로 자동 업데이트한다.
# 버전이 바뀌면 프로세스를 정상 종료하고, Docker의 restart:unless-stopped(또는 운영 중인
# 프로세스 매니저)가 새 site-packages 상태 그대로 재기동하도록 한다.
AUTO_UPDATE_INTERVAL_SECONDS = 7 * 24 * 3600  # 1주일

# voice_states(기본 포함): 음악 + TTS + 음성 허브가 음성채널 입퇴장을 감지하는 데 필요.
# members: 캐릭터 인증(관리자 찾기/역할 부여)과 이모지 역할 선택에 필요 — 디스코드 개발자
#          포털에서 "SERVER MEMBERS INTENT"도 켜져 있어야 한다 (안 켜면 봇이 접속 자체를 못 함).
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

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


@bot.command(name="동기화", aliases=["sync"])
@commands.is_owner()
async def sync_command(ctx):
    """이 서버에 슬래시 명령어를 즉시 다시 동기화한다 (봇 오너 전용).

    봇이 켜질 때(on_ready)와 새 서버에 들어갈 때(on_guild_join) 자동으로 서버별 동기화가 되므로
    보통은 쓸 일이 없다. 그래도 명령어가 안 보이면 그 서버에서 `!동기화`를 치면 바로 다시 등록된다.
    """
    if not ctx.guild:
        await ctx.send("❌ 서버 안에서만 사용할 수 있습니다.")
        return
    guild_obj = discord.Object(id=ctx.guild.id)
    bot.tree.copy_global_to(guild=guild_obj)
    synced = await bot.tree.sync(guild=guild_obj)
    await ctx.send(f"✅ 이 서버에 슬래시 명령어 {len(synced)}개를 즉시 동기화했습니다.")


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


async def _sync_guild_commands(guild_id: int) -> None:
    guild_obj = discord.Object(id=guild_id)
    bot.tree.copy_global_to(guild=guild_obj)
    synced = await bot.tree.sync(guild=guild_obj)
    print(f"🌳 [LOG] 슬래시 명령어 동기화 완료 (guild={guild_id}, {len(synced)}개)")


async def _sync_commands_to_all_guilds() -> None:
    """봇이 들어가 있는 모든 서버에 슬래시 명령어를 "서버 전용"으로 바로 등록한다.

    전역(global) 등록은 디스코드 쪽 캐시 때문에 반영까지 최대 1시간이 걸리고, 전역 + 서버 전용을
    같이 등록하면 명령어가 두 번씩 보인다. 그래서 서버마다 즉시 반영되는 서버 전용으로만 등록하고,
    예전에 전역으로 올려둔 명령어는 디스코드에서 지운다 (코드 쪽 명령어 목록은 그대로 둬서
    나중에 들어간 서버(on_guild_join)에도 똑같이 복사할 수 있게 한다).
    """
    for guild in bot.guilds:
        try:
            await _sync_guild_commands(guild.id)
        except Exception as e:
            print(f"⚠️ [WARN] 슬래시 명령어 동기화 실패 (guild={guild.id}): {e}")
    try:
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        print("🌳 [LOG] 예전 전역 명령어 정리 완료 (중복 표시 방지)")
    except Exception as e:
        print(f"⚠️ [WARN] 전역 명령어 정리 실패: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await _sync_guild_commands(guild.id)
    except Exception as e:
        print(f"⚠️ [WARN] 새 서버 슬래시 명령어 동기화 실패 (guild={guild.id}): {e}")


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

    await _sync_commands_to_all_guilds()

    print("=" * 40 + "\n")

    await _announce_update()


async def _announce_update():
    """CHANGELOG.md의 최신 항목을, 아직 못 본 서버들의 '업데이트 로그' 채널에 알려준다.

    서버별로 settings_store의 last_announced_version과 비교해서, 이미 알려준 버전이면
    건너뛴다 — 재접속으로 on_ready가 여러 번 불려도 중복으로 올라가지 않는다.
    채널은 웹 관리 페이지 대시보드에서 지정한다.
    """
    entry = get_latest_entry()
    if entry is None:
        return
    version, body = entry

    for guild in bot.guilds:
        settings = get_guild_settings(guild.id)
        log_channel_id = settings.get("bot_log_channel_id")
        if not log_channel_id or settings.get("last_announced_version") == version:
            continue

        channel = guild.get_channel(log_channel_id)
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(title=f"🛠️ 랑다 업데이트 ({version})", description=body[:4000], color=0x5B8CFF)
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as e:
                print(f"⚠️ [WARN] 업데이트 로그 전송 실패 (guild={guild.id}): {e}")

        update_guild_settings(guild.id, last_announced_version=version)


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
