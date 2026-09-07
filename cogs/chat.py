# -*- coding: utf-8 -*-
"""자유 채팅 채널.

지정한 채널에서는 명령어 없이 그냥 편하게 말을 걸면 Claude가 답장한다. 어떤 그림을 그리고
싶은지 감이 안 잡힐 때 대화로 아이디어를 다듬으면, 대화 내용이 충분히 구체화됐다고 Claude가
판단한 순간에만(IMAGE_READY_MARKER) 답장에 "🎨 이 대화로 그림 만들기" 버튼이 붙는다 — 매번
붙어있으면 일상 대화 중엔 방해만 되므로, 필요할 때만 나타나게 했다. 버튼을 누르면 지금까지
나눈 대화 내용을 그대로 /그림생성과 같은 경로(ImageGen.run_generation)로 넘겨서 이미지를 만든다.

대화 기록은 채널별로 메모리에만 들고 있는다 (봇 재시작 시 초기화 — 가벼운 잡담 기능이라
DB에 영구 저장할 필요는 없다고 판단). 여러 명이 같은 채널에서 동시에 말해도 순서가 꼬이지
않도록 채널별 락으로 한 번에 하나씩만 처리한다.
"""

import asyncio
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.image import GenAttempt
from core import chat_channel_db, nai_client, prompt_writer, vibe_cache_db
from core.nai_client import NaiError
from core.prompt_writer import PromptRefused, PromptWriterError

NAI_TOKEN = os.getenv("NAI_TOKEN")

MAX_HISTORY_MESSAGES = 20  # 최근 10턴(사용자+봇) 정도만 유지 — 토큰/비용 억제
CHAT_COOLDOWN_SECONDS = 5.0
VIBE_MODEL = "nai-diffusion-4-5-full"  # 채팅에서 첨부한 이미지를 인코딩할 때 쓰는 고정 모델


class ChatToImageView(discord.ui.View):
    def __init__(self, cog: "ChatChannel", transcript: str):
        super().__init__(timeout=1800)  # 30분 후 버튼 비활성화
        self.cog = cog
        self.transcript = transcript
        self.message: Optional[discord.Message] = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="이 대화로 그림 만들기", emoji="🎨", style=discord.ButtonStyle.primary)
    async def make_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.make_image_from_chat(interaction, self.transcript)


class ChatChannel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._history: dict[int, list[dict]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_used: dict[int, float] = {}
        # 채널별로 가장 최근에 첨부된 이미지 (스타일 참조용). 새 이미지가 오면 덮어쓰고,
        # /채팅초기화로 지울 수 있다.
        self._reference_images: dict[int, tuple[bytes, str]] = {}

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
            self._reference_images[message.channel.id] = (image_bytes, image_media_type)

        async with self._get_lock(message.channel.id):
            history = self._history.setdefault(message.channel.id, [])
            spoken_text = text or "(이미지를 첨부함)"
            labeled_message = f"{message.author.display_name}: {spoken_text}"

            async with message.channel.typing():
                try:
                    raw_reply = await prompt_writer.chat_reply(
                        history, labeled_message, image_bytes=image_bytes, image_media_type=image_media_type
                    )
                except PromptWriterError as e:
                    await message.reply(f"❌ {e}", mention_author=False)
                    return

            image_ready = prompt_writer.IMAGE_READY_MARKER in raw_reply
            reply = raw_reply.replace(prompt_writer.IMAGE_READY_MARKER, "").strip()

            # history에는 이미지 원본 대신 첨부됐다는 표시만 남긴다 (매 턴 다시 보내면 토큰 낭비).
            # 마커도 다음 턴에 다시 보이면 Claude가 헷갈릴 수 있으니 제거한 텍스트만 기록한다.
            history_message = labeled_message + (" [이미지 첨부됨 — 스타일 참조로 저장됨]" if image_attachment else "")
            history.append({"role": "user", "content": history_message})
            history.append({"role": "assistant", "content": reply})
            del history[:-MAX_HISTORY_MESSAGES]

            if image_ready:
                # 그림으로 만들어도 될 만큼 구체화됐다고 판단한 경우에만 버튼을 붙인다.
                transcript = "\n".join(
                    ("(나) " if h["role"] == "assistant" else "") + h["content"] for h in history
                )
                view = ChatToImageView(self, transcript)
                sent = await message.reply(reply[:1900] or "...", mention_author=False, view=view)
                view.message = sent
            else:
                await message.reply(reply[:1900] or "...", mention_author=False)

    async def make_image_from_chat(self, interaction: discord.Interaction, transcript: str) -> None:
        image_cog = self.bot.get_cog("ImageGen")
        if image_cog is None:
            await interaction.response.send_message("❌ 이미지 생성 기능을 찾을 수 없습니다.", ephemeral=True)
            return
        if not image_cog.check_and_hit_button_cooldown(interaction.user.id):
            await interaction.response.send_message(
                "⏳ 너무 자주 요청했습니다. 잠시 후 다시 시도하세요.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            tags = await prompt_writer.write_tags(
                "다음은 사용자와 나눈 대화입니다. 이 대화에서 그리고 싶어하는 이미지의 내용만 골라 "
                f"태그로 만들어주세요:\n\n{transcript}"
            )
        except PromptRefused as e:
            await interaction.followup.send(f"🚫 요청이 거부되었습니다: {e}")
            return
        except PromptWriterError as e:
            await interaction.followup.send(f"❌ 프롬프트 변환 실패: {e}")
            return

        vibe_encoded = None
        vibe_note = None
        reference = self._reference_images.get(interaction.channel.id)
        if reference and NAI_TOKEN:
            ref_bytes, _ref_media_type = reference
            try:
                image_hash = vibe_cache_db.image_hash(ref_bytes)
                cached = await vibe_cache_db.async_get_cached(self.bot.loop, image_hash, VIBE_MODEL, 1.0)
                if cached:
                    vibe_encoded = cached
                    vibe_note = "🖼️ 채팅에 첨부된 이미지로 스타일 참조 적용 (캐시된 인코딩 재사용, Anlas 소모 없음)"
                else:
                    vibe_encoded = await nai_client.encode_vibe(NAI_TOKEN, ref_bytes, model=VIBE_MODEL)
                    await vibe_cache_db.async_save_cached(self.bot.loop, image_hash, VIBE_MODEL, 1.0, vibe_encoded)
                    vibe_note = "🖼️ 채팅에 첨부된 이미지로 스타일 참조 적용 (새로 인코딩, Anlas 2 소모)"
            except NaiError as e:
                # 스타일 참조가 실패해도 그림 생성 자체는 참조 없이 계속 진행한다.
                await interaction.followup.send(f"⚠️ 스타일 참조 인코딩에 실패해서 참조 없이 만들게요: {e}", ephemeral=True)

        attempt = GenAttempt(
            requester_id=interaction.user.id,
            description="",
            tag_mode=True,
            used_prompt=tags,
            negative="",
            width=832,
            height=1216,
            size_label="세로 832x1216 (기본)",
            model="nai-diffusion-4-5-full",
            model_label="풀 모델",
            rating="sensitive",
            rating_label="약한 선정성 (기본)",
            seed=0,
            vibe_encoded=vibe_encoded,
            vibe_strength=0.6,
            vibe_information_extracted=1.0,
            vibe_note=vibe_note,
        )
        await image_cog.run_generation(interaction, attempt)

    @app_commands.command(name="채팅채널설정", description="현재 채널을 봇과 자유롭게 대화하는 채널로 지정합니다.")
    @app_commands.guild_only()
    async def set_chat_channel(self, interaction: discord.Interaction):
        await chat_channel_db.async_set_channel(self.bot.loop, interaction.guild.id, interaction.channel.id)
        await interaction.response.send_message(
            f"✅ 이제 {interaction.channel.mention} 에서 저와 자유롭게 대화하실 수 있어요, 회원님. "
            "그림 아이디어를 얘기하다가 답장에 달린 🎨 버튼을 누르시면 바로 그림도 만들어드려요. "
            "이미지를 첨부하시면 그 화풍을 스타일 참조로 기억해뒀다가 그림 만들 때 반영해드립니다."
        )

    @app_commands.command(name="채팅채널설정해제", description="자유 채팅 채널 지정을 해제합니다.")
    @app_commands.guild_only()
    async def clear_chat_channel(self, interaction: discord.Interaction):
        await chat_channel_db.async_clear_channel(self.bot.loop, interaction.guild.id)
        await interaction.response.send_message("✅ 채팅 채널 지정을 해제했습니다.")

    @app_commands.command(name="채팅초기화", description="이 채널에서 저와 나눈 대화 기억(+저장된 스타일 참조 이미지)을 초기화합니다.")
    @app_commands.guild_only()
    async def reset_chat(self, interaction: discord.Interaction):
        self._history.pop(interaction.channel.id, None)
        self._reference_images.pop(interaction.channel.id, None)
        await interaction.response.send_message("🔄 대화 기억과 스타일 참조 이미지를 초기화했어요, 회원님.", ephemeral=True)

    @app_commands.command(name="가이드", description="이 봇이 할 수 있는 걸 전부 자세히 안내합니다.")
    async def guide(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📖 시로챤넬 서포트 란다 — 기능 가이드",
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
            name="🎨 이미지 생성",
            value=(
                "`/그림생성` 문장으로 설명하면 태그로 자동 변환해서 NovelAI로 그림 생성\n"
                "비율·모델·등급·시드·태그모드·스타일참조(Vibe Transfer) 등 옵션 지원\n"
                "결과에 프롬프트 복사·설정 복사·다시 생성·수정하기 버튼이 붙고, 결과별로 스레드가 자동 생성됨\n"
                "`/애나니스` NovelAI Anlas(크레딧) 잔액 확인"
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
            value=(
                "지정된 채널에서 명령어 없이 그냥 편하게 말 걸어주시면 돼요.\n"
                "그림 아이디어를 나누다가 준비됐다 싶으면 답장에 그림 생성 버튼이 자동으로 붙어요.\n"
                "이미지를 첨부하시면 그 화풍을 기억해뒀다가 그림 만들 때 스타일 참조로 반영해드려요.\n"
                "URL을 보여주시거나 최신 정보가 필요한 질문도 직접 찾아서 답해드릴 수 있어요."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ 채널 설정 (관리용)",
            value=(
                "`/그림채널설정` `/그림채널설정해제`\n"
                "`/프롬프트채널설정` `/프롬프트채널설정해제`\n"
                "`/채팅채널설정` `/채팅채널설정해제` `/채팅초기화`\n"
                "`/설정` 음악 전용 채널 지정"
            ),
            inline=False,
        )
        embed.set_footer(text="더 궁금한 게 있으면 채팅 채널에서 저한테 편하게 물어보세요.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatChannel(bot))
