# -*- coding: utf-8 -*-
"""자유 채팅 채널.

지정한 채널에서는 명령어 없이 그냥 편하게 말을 걸면 Claude가 답장한다. 하는 일은 일상 대화와
그림 프롬프트 추천 두 가지뿐이다 (봇이 그림을 직접 그리지는 않음).

대화 기록은 채널별로 메모리에만 들고 있는다 (봇 재시작 시 초기화 — 가벼운 잡담 기능이라
DB에 영구 저장할 필요는 없다고 판단). 여러 명이 같은 채널에서 동시에 말해도 순서가 꼬이지
않도록 채널별 락으로 한 번에 하나씩만 처리한다.
"""

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import chat_channel_db, prompt_writer
from core.prompt_writer import PromptWriterError

MAX_HISTORY_MESSAGES = 60  # 최근 30턴(사용자+봇) 정도 유지 — 대화 맥락을 더 오래 기억
CHAT_COOLDOWN_SECONDS = 5.0


class ChatChannel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._history: dict[int, list[dict]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_used: dict[int, float] = {}

    def _get_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[channel_id] = lock
        return lock

    @commands.Cog.listener("on_message")
    async def on_message_chat(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        channel_id = await chat_channel_db.async_get_channel(self.bot.loop, message.guild.id)
        if not channel_id or message.channel.id != channel_id:
            return

        text = message.content.strip()
        if text.startswith(self.bot.command_prefix):
            return  # 다른 명령어는 무시.

        image_attachment = next(
            (a for a in message.attachments if (a.content_type or "").split(";")[0].startswith("image/")),
            None,
        )
        if not text and not image_attachment:
            return  # 빈 메시지나 지원 안 하는 첨부파일만 있으면 무시.

        now = time.monotonic()
        last = self._last_used.get(message.author.id, 0.0)
        if now - last < CHAT_COOLDOWN_SECONDS:
            return
        self._last_used[message.author.id] = now

        image_bytes = None
        image_media_type = None
        if image_attachment:
            image_bytes = await image_attachment.read()
            image_media_type = (image_attachment.content_type or "image/png").split(";")[0]

        async with self._get_lock(message.channel.id):
            history = self._history.setdefault(message.channel.id, [])
            labeled_message = f"{message.author.display_name}: {text or '(이미지를 첨부함)'}"

            async with message.channel.typing():
                try:
                    reply = await prompt_writer.chat_reply(
                        history, labeled_message, image_bytes=image_bytes, image_media_type=image_media_type
                    )
                except PromptWriterError as e:
                    await message.reply(f"❌ {e}", mention_author=False)
                    return

            # history에는 이미지 원본 대신 첨부됐다는 표시만 남긴다 (매 턴 다시 보내면 토큰 낭비).
            history.append({"role": "user", "content": labeled_message + (" [이미지 첨부됨]" if image_attachment else "")})
            history.append({"role": "assistant", "content": reply})
            del history[:-MAX_HISTORY_MESSAGES]

            # 디스코드 메시지 한 개는 2000자 제한이라, 길면 나눠서 보낸다.
            chunks = [reply[i : i + 1900] for i in range(0, len(reply), 1900)] or ["..."]
            await message.reply(chunks[0], mention_author=False)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)

    @app_commands.command(name="채팅초기화", description="이 채널에서 저와 나눈 대화 기억을 초기화합니다.")
    @app_commands.guild_only()
    async def reset_chat(self, interaction: discord.Interaction):
        self._history.pop(interaction.channel.id, None)
        await interaction.response.send_message("🔄 대화 기억을 초기화했어요, 회원님.", ephemeral=True)

    @app_commands.command(name="가이드", description="이 봇이 할 수 있는 걸 전부 자세히 안내합니다.")
    async def guide(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📖 시로챤넬 서포트 랑다 — 기능 가이드",
            description="이 서버 봇이 할 수 있는 걸 전부 정리해드릴게요, 회원님.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="🎵 음악 재생",
            value=(
                "`/play` 검색어·유튜브 링크를 대기열에 추가\n"
                "`/join` `/leave` 음성 채널 입장/퇴장\n"
                "`/volume` 볼륨 조회·설정\n"
                "`/플레이리스트추가` `/플레이리스트목록` `/플레이리스트삭제`\n"
                "전용 채널의 컨트롤러 버튼: 일시정지/스킵/셔플/반복/대기열 보기/정지"
            ),
            inline=False,
        )
        embed.add_field(
            name="💬 프롬프트 추천 채널",
            value="지정된 채널에 문장이나 이미지를 올리면 어울리는 Danbooru 태그를 추천해드려요.",
            inline=False,
        )
        embed.add_field(
            name="🗨️ 자유 채팅 (저예요!)",
            value="지정된 채널에서 명령어 없이 편하게 말 걸어주세요. 일상 대화와 그림 프롬프트 다듬기를 도와드려요.",
            inline=False,
        )
        embed.add_field(
            name="🗣️ TTS",
            value=(
                "TTS 채널에 글을 쓰면, 내가 들어가 있는 음성채널에서 읽어드려요.\n"
                "`/목소리설정` 내 목소리 고르기 · `/내목소리` 확인 · `/목소리초기화`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔊 음성 허브 (자동 음성방)",
            value="허브 음성채널에 들어가면 내 방이 자동으로 만들어지고, 다들 나가면 자동으로 지워져요.",
            inline=False,
        )
        embed.add_field(
            name="🛡️ 서버 관리 (웹 관리 페이지)",
            value=(
                "입장 안내 + 🔑 캐릭터 인증 버튼(비공개 스레드 → 관리자 승인 → 역할 부여)\n"
                "🎭 이모지로 역할 셀프 선택 · 📢 공지사항 · 📜 길드 규칙 · 👑 직급/역할 부여 · 🧹 채팅 정리\n"
                "`/설정` 관리 웹페이지 주소 받기 (서버 소유자 전용)"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ 채널 설정 (관리용)",
            value=(
                "음악 채널 · 채팅/프롬프트 채널 · TTS 채널/기본 목소리 · 음성 허브는\n"
                "전부 웹 관리 페이지에서 설정해요 (서버 소유자가 `/설정`으로 주소 확인)"
            ),
            inline=False,
        )
        embed.set_footer(text="더 궁금한 게 있으면 채팅 채널에서 저한테 편하게 물어보세요.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatChannel(bot))
