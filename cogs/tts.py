# -*- coding: utf-8 -*-
"""
텍스트채널에 쓴 글을 그 사람이 있는 음성채널에서 소리로 읽어주는 Cog (카톡 스타일 TTS).
시로냥 봇의 TTS 기능을 가져온 것이다 (합성 엔진은 core/tts_engine.py — edge-tts 무료 /
AWS Polly / 타입캐스트).

시로냥은 TTS 채널/목소리를 웹 설정 페이지에서 골랐지만, 여기서는 슬래시 명령어로 정한다.
  - 관리: /tts채널설정, /tts채널해제, /tts기본목소리
  - 개인: /목소리설정 (자동완성으로 검색 — 타입캐스트 600개도 글자 입력하면 검색됨),
          /내목소리, /목소리초기화, /목소리미리듣기

음악 봇과 같은 봇이라 음성 연결이 서버당 하나뿐이다. 그래서:
  - 음악이 재생/일시정지 중이면 TTS는 읽지 않고 건너뛴다 (노래를 끊지 않음).
  - 음악 세션이 살아있는 동안(대기열 대기 포함)엔 봇을 다른 음성채널로 끌고 가지 않는다.
  - TTS로 읽는 도중 노래가 시작되면, 음악 쪽이 TTS를 끊고 노래를 튼다 (음악 우선).

여러 메시지가 연달아 올라와도 소리가 겹치지 않도록 서버마다 큐에 쌓아두고 하나씩 재생한다.
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import FFMPEG_EXECUTABLE  # 음악과 같은 ffmpeg 경로 규칙(.env의 FFMPEG_PATH 우선)
from core.settings_store import get_guild_settings, update_guild_settings
from core.tts_catalog import find_typecast_voice, search_typecast_voices
from core.tts_engine import synthesize
from core.tts_voices import DEFAULT_VOICE, available_voices

MAX_TEXT_LENGTH = 300  # 너무 긴 메시지가 음성채널을 오래 독점하지 않도록 자른다.


async def _voice_label(voice_id: str) -> str:
    for v in available_voices():
        if v["id"] == voice_id:
            return v["label"]
    if voice_id.startswith("typecast_id:"):
        try:
            found = await find_typecast_voice(voice_id)
        except Exception:
            found = None
        if found:
            return f"{found['label']} (타입캐스트)"
    return voice_id


async def _voice_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    q = current.strip().lower()
    choices = [
        app_commands.Choice(name=v["label"][:100], value=v["id"])
        for v in available_voices()
        if not q or q in v["label"].lower() or q in v["id"].lower()
    ]
    if len(choices) < 25:
        try:
            typecast = await search_typecast_voices(current, limit=25)
        except Exception:
            typecast = []
        choices += [app_commands.Choice(name=f"{v['label']} · 타입캐스트"[:100], value=v["id"]) for v in typecast]
    return choices[:25]


def _is_valid_voice_id(voice_id: str) -> bool:
    if voice_id.startswith("typecast_id:"):
        return bool(os.getenv("TYPECAST_API_KEY"))
    return any(v["id"] == voice_id for v in available_voices())


class Tts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._queues: dict[int, asyncio.Queue] = {}

    def _music_session_active(self, vc: discord.VoiceClient) -> bool:
        """음악 cog가 이 음성 연결로 재생 세션을 이어가고 있는지 (재생 중이거나 다음 곡 대기 중)."""
        music = self.bot.get_cog("MusicPlayer")
        if music is None or music.vc is not vc:
            return False
        return music.current_song is not None or (music.player_task is not None and not music.player_task.done())

    def _queue_for(self, guild_id: int) -> asyncio.Queue:
        """서버별 재생 대기열. 처음 쓰이는 순간 그 큐를 처리할 백그라운드 작업도 같이 띄운다."""
        if guild_id not in self._queues:
            queue: asyncio.Queue = asyncio.Queue()
            self._queues[guild_id] = queue
            self.bot.loop.create_task(self._worker(guild_id, queue))
        return self._queues[guild_id]

    async def _worker(self, guild_id: int, queue: asyncio.Queue) -> None:
        while True:
            voice_channel, text, voice_name = await queue.get()
            try:
                await self._speak(voice_channel, text, voice_name)
            except Exception as e:
                print(f"⚠️ [WARN] TTS 재생 중 오류 (guild={guild_id}): {e}")
            finally:
                queue.task_done()

    async def _speak(self, voice_channel: discord.VoiceChannel, text: str, voice_name: str) -> None:
        guild = voice_channel.guild
        vc = guild.voice_client

        if vc is not None and not vc.is_connected():
            vc = None
        if vc is None:
            vc = await voice_channel.connect()
        else:
            if vc.is_playing() or vc.is_paused():
                return  # 음악 재생 중 — 노래를 끊지 않고 이번 메시지는 건너뛴다.
            if vc.channel.id != voice_channel.id:
                if self._music_session_active(vc):
                    return  # 다른 채널에서 음악 세션이 진행 중이면 봇을 끌고 오지 않는다.
                await vc.move_to(voice_channel)

        audio_bytes = await synthesize(text, voice_name)
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(audio_bytes)

            if vc.is_playing() or vc.is_paused():
                return  # 합성하는 사이에 노래가 시작됐으면 양보한다.

            done = asyncio.Event()

            def _after(_error, loop=self.bot.loop):
                # discord.py는 재생이 끝나면 이 콜백을 별도 스레드에서 부르므로 스레드 안전하게 세팅한다.
                loop.call_soon_threadsafe(done.set)

            vc.play(discord.FFmpegPCMAudio(path, executable=FFMPEG_EXECUTABLE), after=_after)
            await done.wait()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        text = message.content.strip()
        if not text:
            return  # 이미지만 올리거나 빈 메시지면 읽을 게 없다.

        settings = get_guild_settings(message.guild.id)
        tts = settings.get("tts") or {}
        if not tts.get("channel_id") or message.channel.id != tts["channel_id"]:
            return

        voice_state = message.author.voice
        if voice_state is None or voice_state.channel is None:
            return  # 음성채널에 없으면 읽어줄 곳이 없으니 무시.

        # 개인 목소리 설정이 있으면 그걸 우선 쓰고, 없으면 서버 기본값을 쓴다.
        user_voices: dict = settings.get("tts_user_voices", {})
        voice_name = user_voices.get(str(message.author.id)) or tts.get("voice") or DEFAULT_VOICE
        await self._queue_for(message.guild.id).put((voice_state.channel, text[:MAX_TEXT_LENGTH], voice_name))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        """TTS 때문에 들어온 봇이 음성채널에 혼자 남으면 자동으로 나간다 (음악 세션은 음악 쪽에 맡김)."""
        if member.bot:
            return
        vc = member.guild.voice_client
        if vc is None or not before.channel or before.channel.id != vc.channel.id:
            return
        if any(not m.bot for m in vc.channel.members):
            return
        if vc.is_playing() or self._music_session_active(vc):
            return
        await vc.disconnect()

    # ---- 관리 명령 ----

    @app_commands.command(name="tts채널설정", description="현재 채널을 TTS 채널로 지정합니다. 여기 쓴 글을 음성채널에서 읽어줘요.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def set_tts_channel(self, interaction: discord.Interaction):
        tts = dict(get_guild_settings(interaction.guild.id).get("tts") or {})
        tts["channel_id"] = interaction.channel.id
        tts.setdefault("voice", DEFAULT_VOICE)
        update_guild_settings(interaction.guild.id, tts=tts)
        await interaction.response.send_message(
            f"✅ 이제 {interaction.channel.mention} 에 쓴 글을, 쓴 사람이 들어가 있는 음성채널에서 읽어드려요.\n"
            f"서버 기본 목소리: **{await _voice_label(tts['voice'])}** (`/tts기본목소리`로 변경)"
        )

    @app_commands.command(name="tts채널해제", description="TTS 채널 지정을 해제합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def clear_tts_channel(self, interaction: discord.Interaction):
        tts = dict(get_guild_settings(interaction.guild.id).get("tts") or {})
        tts["channel_id"] = None
        update_guild_settings(interaction.guild.id, tts=tts)
        await interaction.response.send_message("✅ TTS 채널 지정을 해제했습니다.")

    @app_commands.command(name="tts기본목소리", description="이 서버의 TTS 기본 목소리를 정합니다 (개인 설정이 없는 사람에게 적용).")
    @app_commands.describe(목소리="목소리 이름을 입력해서 검색")
    @app_commands.autocomplete(목소리=_voice_autocomplete)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def set_default_voice(self, interaction: discord.Interaction, 목소리: str):
        if not _is_valid_voice_id(목소리):
            await interaction.response.send_message("❌ 목록에 있는 목소리를 골라주세요 (자동완성 사용).", ephemeral=True)
            return
        tts = dict(get_guild_settings(interaction.guild.id).get("tts") or {})
        tts["voice"] = 목소리
        update_guild_settings(interaction.guild.id, tts=tts)
        await interaction.response.send_message(f"✅ 서버 기본 목소리를 **{await _voice_label(목소리)}** (으)로 바꿨어요.")

    # ---- 개인 명령 ----

    @app_commands.command(name="목소리설정", description="내 메시지를 읽어줄 TTS 목소리를 고릅니다.")
    @app_commands.describe(목소리="목소리 이름/성별/나이 등으로 검색 (타입캐스트 목소리도 검색됨)")
    @app_commands.autocomplete(목소리=_voice_autocomplete)
    @app_commands.guild_only()
    async def set_my_voice(self, interaction: discord.Interaction, 목소리: str):
        if not _is_valid_voice_id(목소리):
            await interaction.response.send_message("❌ 목록에 있는 목소리를 골라주세요 (자동완성 사용).", ephemeral=True)
            return
        user_voices = dict(get_guild_settings(interaction.guild.id).get("tts_user_voices") or {})
        user_voices[str(interaction.user.id)] = 목소리
        update_guild_settings(interaction.guild.id, tts_user_voices=user_voices)
        await interaction.response.send_message(
            f"🎙️ 앞으로 회원님 메시지는 **{await _voice_label(목소리)}** 목소리로 읽어드릴게요.", ephemeral=True
        )

    @app_commands.command(name="내목소리", description="지금 내 메시지를 읽는 TTS 목소리를 확인합니다.")
    @app_commands.guild_only()
    async def my_voice(self, interaction: discord.Interaction):
        settings = get_guild_settings(interaction.guild.id)
        personal = (settings.get("tts_user_voices") or {}).get(str(interaction.user.id))
        default = (settings.get("tts") or {}).get("voice") or DEFAULT_VOICE
        if personal:
            text = f"🎙️ 개인 설정 목소리: **{await _voice_label(personal)}**"
        else:
            text = f"🎙️ 개인 설정이 없어서 서버 기본 목소리 **{await _voice_label(default)}** 로 읽고 있어요."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="목소리초기화", description="내 개인 TTS 목소리 설정을 지우고 서버 기본 목소리로 돌아갑니다.")
    @app_commands.guild_only()
    async def reset_my_voice(self, interaction: discord.Interaction):
        user_voices = dict(get_guild_settings(interaction.guild.id).get("tts_user_voices") or {})
        user_voices.pop(str(interaction.user.id), None)
        update_guild_settings(interaction.guild.id, tts_user_voices=user_voices)
        await interaction.response.send_message("✅ 개인 목소리 설정을 지웠어요. 이제 서버 기본 목소리로 읽어드려요.", ephemeral=True)

    @app_commands.command(name="목소리미리듣기", description="TTS 목소리를 미리 들어봅니다 (mp3 파일로 보내드려요).")
    @app_commands.describe(목소리="들어볼 목소리", 문장="읽어볼 문장 (비우면 기본 문장)")
    @app_commands.autocomplete(목소리=_voice_autocomplete)
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def preview_voice(self, interaction: discord.Interaction, 목소리: str, 문장: str = "안녕하세요, 이 목소리로 읽어드릴게요."):
        if not _is_valid_voice_id(목소리):
            await interaction.response.send_message("❌ 목록에 있는 목소리를 골라주세요 (자동완성 사용).", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            audio = await synthesize(문장[:100], 목소리)
        except Exception as e:
            await interaction.followup.send(f"❌ 합성 실패: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"🔈 **{await _voice_label(목소리)}**", file=discord.File(io.BytesIO(audio), filename="preview.mp3"), ephemeral=True
        )

    @preview_voice.error
    async def preview_voice_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ {error.retry_after:.0f}초 후 다시 시도하세요.", ephemeral=True)
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Tts(bot))
