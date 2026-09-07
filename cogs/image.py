# -*- coding: utf-8 -*-
"""NovelAI 이미지 생성 기능.

서버(길드) 공용 NAI_TOKEN(.env) 하나로 동작한다. 동시 생성 요청은 mutex로 직렬화하고
(NAI API가 동시 요청을 거부/오류 처리하는 경우가 있어 참고 문서에서 권장하는 방식),
유저별 쿨다운으로 스팸을 막는다.

생성 결과에는 버튼(프롬프트 복사/설정 복사/다시 생성)이 붙어서, 설정을 다시 옮겨 적을
필요 없이 그 자리에서 바로 재생성할 수 있다.
"""

import asyncio
import io
import os
import time
from dataclasses import dataclass, replace
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import image_settings_db, nai_client, prompt_writer, vibe_cache_db
from core.error_notify import notify_error
from core.nai_client import NaiAuthError, NaiError, NaiRateLimitError
from core.prompt_writer import PromptRefused, PromptWriterError

NAI_TOKEN = os.getenv("NAI_TOKEN")

BUTTON_COOLDOWN_SECONDS = 15.0


class ImageChannelRestricted(app_commands.CheckFailure):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        super().__init__(f"이미지 생성은 <#{channel_id}> 채널에서만 가능합니다.")


async def _check_image_channel(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    channel_id = await image_settings_db.async_get_channel(interaction.client.loop, interaction.guild.id)
    if channel_id is None or interaction.channel.id == channel_id:
        return True
    raise ImageChannelRestricted(channel_id)

SIZE_CHOICES = [
    app_commands.Choice(name="세로 832x1216 (기본)", value="portrait"),
    app_commands.Choice(name="가로 1216x832", value="landscape"),
    app_commands.Choice(name="정사각 1024x1024", value="square"),
    app_commands.Choice(name="긴세로 704x1472", value="tall"),
    app_commands.Choice(name="긴가로 1472x704", value="wide"),
]

MODEL_CHOICES = [
    app_commands.Choice(name="풀 모델 (기본)", value="nai-diffusion-4-5-full"),
    app_commands.Choice(name="큐레이트 모델 (정제된 화풍)", value="nai-diffusion-4-5-curated"),
]

RATING_CHOICES = [
    app_commands.Choice(name="일반", value="general"),
    app_commands.Choice(name="약한 선정성 (기본)", value="sensitive"),
    app_commands.Choice(name="성인", value="questionable"),
    app_commands.Choice(name="노출", value="explicit"),
]


@dataclass
class GenAttempt:
    """한 번의 생성 요청 상태. 결과 메시지의 버튼(복사/재생성/수정)이 이 값을 재사용한다."""

    requester_id: int
    description: str  # 사용자가 입력한 자연어 설명 (태그모드면 빈 문자열)
    tag_mode: bool
    used_prompt: str  # 실제로 NAI에 넣은 태그 (품질 태그/등급 태그 제외)
    negative: str  # 사용자가 입력한 네거티브 (프리셋 제외)
    width: int
    height: int
    size_label: str
    model: str
    model_label: str
    rating: str
    rating_label: str
    seed: int
    vibe_encoded: Optional[str]
    vibe_strength: float
    vibe_information_extracted: float
    vibe_note: Optional[str]
    thread_id: Optional[int] = None  # 결과 스레드가 만들어진 뒤에는 후속 요청(재생성/수정)이 이 스레드로 감


def _build_result_embed(attempt: GenAttempt, requester_display_name: str) -> discord.Embed:
    embed = discord.Embed(title="🎨 이미지 생성 완료", color=discord.Color.purple())
    if not attempt.tag_mode and attempt.description:
        embed.add_field(name="입력한 설명", value=f"```{attempt.description[:300]}```", inline=False)
        embed.add_field(name="변환된 태그", value=f"```{attempt.used_prompt[:500]}```", inline=False)
    else:
        embed.add_field(name="태그", value=f"```{attempt.used_prompt[:500]}```", inline=False)
    if attempt.negative:
        embed.add_field(name="네거티브", value=f"```{attempt.negative[:300]}```", inline=False)
    if attempt.vibe_note:
        embed.add_field(
            name="스타일참조",
            value=f"{attempt.vibe_note} (강도 {attempt.vibe_strength} / 정보추출량 {attempt.vibe_information_extracted})",
            inline=False,
        )
    embed.add_field(name="비율", value=attempt.size_label, inline=True)
    embed.add_field(name="모델", value=attempt.model_label, inline=True)
    embed.add_field(name="등급", value=attempt.rating_label, inline=True)
    embed.add_field(name="시드", value=str(attempt.seed) if attempt.seed else "랜덤", inline=True)
    embed.set_image(url="attachment://nai_image.png")
    embed.set_footer(text=f"요청: {requester_display_name}")
    return embed


class ImageResultView(discord.ui.View):
    def __init__(self, cog: "ImageGen", attempt: GenAttempt):
        super().__init__(timeout=1800)  # 30분 후 버튼 비활성화
        self.cog = cog
        self.attempt = attempt
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.attempt.requester_id:
            await interaction.response.send_message("❌ 요청자만 사용할 수 있는 버튼입니다.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="프롬프트 복사", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def copy_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="📋 사용된 프롬프트", color=discord.Color.blurple())
        if not self.attempt.tag_mode and self.attempt.description:
            embed.add_field(name="입력한 설명", value=f"```{self.attempt.description[:1000]}```", inline=False)
        embed.add_field(name="태그", value=f"```{self.attempt.used_prompt[:1000]}```", inline=False)
        embed.add_field(
            name="네거티브", value=f"```{self.attempt.negative[:1000] or '(없음)'}```", inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="설정 복사", emoji="⚙️", style=discord.ButtonStyle.secondary, row=0)
    async def copy_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        lines = [
            f"비율: {self.attempt.size_label} ({self.attempt.width}x{self.attempt.height})",
            f"모델: {self.attempt.model_label}",
            f"등급: {self.attempt.rating_label}",
            f"시드: {self.attempt.seed if self.attempt.seed else '랜덤'}",
            f"태그모드: {'예' if self.attempt.tag_mode else '아니오'}",
        ]
        if self.attempt.vibe_note:
            lines.append(f"스타일참조 강도: {self.attempt.vibe_strength}")
            lines.append(f"스타일참조 정보추출량: {self.attempt.vibe_information_extracted}")
        embed = discord.Embed(
            title="⚙️ 사용된 설정",
            description="```\n" + "\n".join(lines) + "\n```",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="다시 생성", emoji="🔁", style=discord.ButtonStyle.primary, row=1)
    async def regenerate(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.check_and_hit_button_cooldown(interaction.user.id):
            await interaction.response.send_message(
                "⏳ 너무 자주 요청했습니다. 잠시 후 다시 시도하세요.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        # 같은 프롬프트/설정 그대로, 시드만 새로 뽑아 다른 결과를 받는다.
        new_attempt = replace(self.attempt, requester_id=interaction.user.id, seed=0)
        await self.cog.run_generation(interaction, new_attempt)


class ImageGen(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = discord.utils.MISSING  # cog_load에서 이벤트 루프에 맞춰 생성
        self._button_cooldowns: dict[int, float] = {}

    async def cog_load(self):
        self._lock = asyncio.Lock()

    def check_and_hit_button_cooldown(self, user_id: int) -> bool:
        """버튼/모달 경로는 슬래시 명령 쿨다운을 안 타므로 별도로 스팸을 막는다."""
        now = time.monotonic()
        last = self._button_cooldowns.get(user_id, 0.0)
        if now - last < BUTTON_COOLDOWN_SECONDS:
            return False
        self._button_cooldowns[user_id] = now
        return True

    async def run_generation(self, interaction: discord.Interaction, attempt: GenAttempt) -> None:
        """attempt 내용으로 NAI 생성을 수행하고 결과(임베드+버튼)를 followup으로 보낸다.

        호출 시점에 interaction은 이미 defer(thinking=True)된 상태여야 한다.
        슬래시 명령(/그림생성)과 버튼(다시 생성)이 모두 이 메서드를 공유한다.
        """
        if not NAI_TOKEN:
            await interaction.followup.send("⚠️ NAI_TOKEN이 설정되지 않았습니다.", ephemeral=True)
            return

        if self._lock.locked():
            await interaction.followup.send("⏳ 다른 요청을 처리 중이라 순서를 기다립니다...", ephemeral=True)

        async with self._lock:
            try:
                result = await nai_client.generate_image(
                    NAI_TOKEN,
                    prompt=attempt.used_prompt,
                    negative_prompt=attempt.negative,
                    model=attempt.model,
                    width=attempt.width,
                    height=attempt.height,
                    rating=attempt.rating,
                    seed=attempt.seed,
                    vibe_encoded=attempt.vibe_encoded,
                    vibe_strength=attempt.vibe_strength,
                    vibe_information_extracted=attempt.vibe_information_extracted,
                )
            except NaiAuthError as e:
                await interaction.followup.send(f"❌ {e}")
                return
            except NaiRateLimitError as e:
                await interaction.followup.send(f"⏳ {e}")
                return
            except NaiError as e:
                await interaction.followup.send(f"❌ 이미지 생성 실패: {e}")
                return
            except Exception as e:
                print(f"❌ [ERROR] 이미지 생성 중 알 수 없는 오류: {e}")
                await notify_error(self.bot, interaction.guild_id, f"이미지 생성 중 오류: {e}")
                await interaction.followup.send("❌ 알 수 없는 오류로 이미지 생성에 실패했습니다.")
                return

        file = discord.File(io.BytesIO(result.png_bytes), filename="nai_image.png")
        embed = _build_result_embed(attempt, interaction.user.display_name)
        view = ImageResultView(self, attempt)

        if attempt.thread_id and interaction.channel.id != attempt.thread_id:
            # 결과 스레드가 이미 있는데 지금 버튼은 스레드 시작 메시지(원본 채널) 쪽에서 눌린 경우 —
            # followup은 원본 채널로 가버리므로, 스레드를 직접 찾아서 그 안에 새 결과를 올린다.
            thread = self.bot.get_channel(attempt.thread_id)
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(attempt.thread_id)
                except Exception:
                    thread = None
            message = None
            if thread is not None:
                try:
                    message = await thread.send(embed=embed, file=file, view=view)
                    await interaction.followup.send(f"✅ 스레드에 새 결과를 올렸어요 → {thread.mention}", ephemeral=True)
                except Exception as e:
                    print(f"⚠️ [WARN] 스레드에 결과 전송 실패, 원래 채널로 대체: {e}")
                    message = None
            if message is None:
                message = await interaction.followup.send(embed=embed, file=file, view=view)
        else:
            message = await interaction.followup.send(embed=embed, file=file, view=view)
            # 최초 생성(스레드가 아직 없을 때)이면 결과 메시지에서 스레드를 새로 판다.
            # 이 블록은 어디까지나 부가 기능이라, 실패해도 이미 보낸 결과 메시지에는 영향이
            # 없어야 한다 — 그래서 discord.py가 던질 수 있는 예외를 넓게 잡아서 조용히 넘어간다
            # (좁게 HTTPException만 잡으면 ClientException 등 다른 예외가 새어나가 슬래시 명령
            # 자체가 실패한 것처럼 "❌ 처리 중 오류가 발생했습니다"가 뜨는 문제가 있었음).
            if attempt.thread_id is None and isinstance(interaction.channel, discord.TextChannel):
                try:
                    thread_title = (attempt.description.strip() if attempt.description else "") or attempt.used_prompt
                    thread = await message.create_thread(name=thread_title[:80] or "그림 생성 결과", auto_archive_duration=1440)
                    attempt = replace(attempt, thread_id=thread.id)
                    view.attempt = attempt
                    embed.add_field(
                        name="🧵 스레드", value=f"다시 생성/수정 결과는 {thread.mention} 안에서 이어집니다.", inline=False
                    )
                    await message.edit(embed=embed, view=view)
                except Exception as e:
                    print(f"⚠️ [WARN] 결과 스레드 생성 실패 (이미지 전송 자체는 정상): {e}")

        view.message = message

    @app_commands.command(name="그림생성", description="원하는 그림을 문장으로 설명하면 AI가 태그를 짜서 NovelAI로 그려줍니다.")
    @app_commands.describe(
        프롬프트="그리고 싶은 이미지를 자유롭게 설명 (한국어 가능. 예: 파란 머리에 웃고 있는 여자아이). 태그모드 켜면 영문 태그로 직접 입력",
        네거티브="제외하고 싶은 요소 (선택)",
        비율="이미지 비율/크기",
        모델="사용할 NovelAI 모델",
        등급="이미지 수위",
        시드="재현하고 싶은 시드값 (비우면 랜덤)",
        태그모드="켜면 AI 변환 없이 프롬프트를 Danbooru 태그 그대로 사용 (태그를 아는 사람용)",
        스타일참조="화풍/분위기를 참고할 이미지 (Vibe Transfer, 선택). 같은 이미지+정보추출량 조합 재사용 시 캐시돼서 Anlas 안 나감",
        스타일강도="레퍼런스가 결과에 얼마나 세게 섞일지 (0.0~1.0, 기본 0.6). 높을수록 화풍이 강해지지만 프롬프트 내용과 충돌 가능",
        정보추출량="레퍼런스에서 얼마나 구체적으로 뽑아낼지 (0.0~1.0, 기본 1.0). 낮으면 색감/분위기만, 높으면 선화·구도까지 상세히 반영",
    )
    @app_commands.choices(비율=SIZE_CHOICES, 모델=MODEL_CHOICES, 등급=RATING_CHOICES)
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    @app_commands.check(_check_image_channel)
    async def generate(
        self,
        interaction: discord.Interaction,
        프롬프트: str,
        네거티브: str = "",
        비율: app_commands.Choice[str] = None,
        모델: app_commands.Choice[str] = None,
        등급: app_commands.Choice[str] = None,
        시드: int = 0,
        태그모드: bool = False,
        스타일참조: discord.Attachment = None,
        스타일강도: app_commands.Range[float, 0.0, 1.0] = 0.6,
        정보추출량: app_commands.Range[float, 0.0, 1.0] = 1.0,
    ):
        if not NAI_TOKEN:
            await interaction.response.send_message(
                "⚠️ NAI_TOKEN이 설정되지 않았습니다. `.env`에 NovelAI Persistent API Token을 추가하고 봇을 재시작하세요.",
                ephemeral=True,
            )
            return

        size_key = 비율.value if 비율 else "portrait"
        width, height = nai_client.SIZE_PRESETS[size_key]
        model = 모델.value if 모델 else "nai-diffusion-4-5-full"
        rating = 등급.value if 등급 else "sensitive"

        await interaction.response.defer(thinking=True)

        final_prompt = 프롬프트
        prompt_note = None
        if not 태그모드:
            try:
                final_prompt = await prompt_writer.write_tags(프롬프트)
            except PromptRefused as e:
                await interaction.followup.send(f"🚫 요청이 거부되었습니다: {e}")
                return
            except PromptWriterError as e:
                prompt_note = f"⚠️ 프롬프트 자동 변환 실패({e}) — 입력값을 태그로 그대로 사용합니다."

        vibe_encoded = None
        vibe_note = None
        if 스타일참조:
            if not (스타일참조.content_type or "").startswith("image/"):
                await interaction.followup.send("❌ 스타일참조는 이미지 파일만 가능합니다.")
                return
            try:
                image_bytes = await 스타일참조.read()
                image_hash = vibe_cache_db.image_hash(image_bytes)
                cached = await vibe_cache_db.async_get_cached(self.bot.loop, image_hash, model, 정보추출량)
                if cached:
                    vibe_encoded = cached
                    vibe_note = "🖼️ 스타일 참조 적용 (캐시된 인코딩 재사용, Anlas 소모 없음)"
                else:
                    vibe_encoded = await nai_client.encode_vibe(
                        NAI_TOKEN, image_bytes, model=model, information_extracted=정보추출량
                    )
                    await vibe_cache_db.async_save_cached(self.bot.loop, image_hash, model, 정보추출량, vibe_encoded)
                    vibe_note = "🖼️ 스타일 참조 적용 (새로 인코딩, Anlas 2 소모)"
            except NaiError as e:
                await interaction.followup.send(f"❌ 스타일참조 인코딩 실패: {e}")
                return

        if prompt_note:
            await interaction.followup.send(prompt_note)

        attempt = GenAttempt(
            requester_id=interaction.user.id,
            description="" if 태그모드 else 프롬프트,
            tag_mode=태그모드,
            used_prompt=final_prompt,
            negative=네거티브,
            width=width,
            height=height,
            size_label=비율.name if 비율 else "세로 832x1216 (기본)",
            model=model,
            model_label=nai_client.MODELS.get(model, model),
            rating=rating,
            rating_label=등급.name if 등급 else "약한 선정성 (기본)",
            seed=시드,
            vibe_encoded=vibe_encoded,
            vibe_strength=스타일강도,
            vibe_information_extracted=정보추출량,
            vibe_note=vibe_note,
        )
        await self.run_generation(interaction, attempt)

    @generate.error
    async def generate_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ 너무 자주 요청했습니다. {error.retry_after:.0f}초 후 다시 시도하세요.", ephemeral=True
            )
            return
        if isinstance(error, ImageChannelRestricted):
            await interaction.response.send_message(
                f"⚠️ 이미지 생성은 <#{error.channel_id}> 채널에서만 가능합니다.", ephemeral=True
            )
            return
        print(f"❌ [ERROR] /그림생성 처리 중 오류: {error}")
        await notify_error(self.bot, interaction.guild_id, f"/그림생성 처리 중 오류: {error}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 처리 중 오류가 발생했습니다.")
        else:
            await interaction.response.send_message("❌ 처리 중 오류가 발생했습니다.", ephemeral=True)

    @app_commands.command(name="애나니스", description="NovelAI Anlas(크레딧) 잔액을 확인합니다.")
    async def anlas(self, interaction: discord.Interaction):
        if not NAI_TOKEN:
            await interaction.response.send_message("⚠️ NAI_TOKEN이 설정되지 않았습니다.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            info = await nai_client.get_anlas(NAI_TOKEN)
        except NaiError as e:
            await interaction.followup.send(f"❌ {e}")
            return
        opus_text = "Opus 구독 (기본 프리셋 범위 내 무제한 생성)" if info["opus"] else "구독 없음/일반 플랜 (Anlas 소모)"
        await interaction.followup.send(
            f"💰 Anlas 잔액: **{info['total']}** (고정 {info['fixed']} + 구매 {info['purchased']})\n{opus_text}"
        )

    @app_commands.command(name="그림채널설정", description="현재 채널을 이미지 생성 전용 채널로 지정합니다.")
    @app_commands.guild_only()
    async def set_image_channel(self, interaction: discord.Interaction):
        await image_settings_db.async_set_channel(self.bot.loop, interaction.guild.id, interaction.channel.id)
        await interaction.response.send_message(
            f"✅ 이제 이 서버에서는 {interaction.channel.mention} 에서만 `/그림생성`을 사용할 수 있습니다.",
        )

    @app_commands.command(name="그림채널설정해제", description="이미지 생성 채널 제한을 해제합니다 (아무 채널에서나 사용 가능).")
    @app_commands.guild_only()
    async def clear_image_channel(self, interaction: discord.Interaction):
        await image_settings_db.async_clear_channel(self.bot.loop, interaction.guild.id)
        await interaction.response.send_message("✅ 채널 제한을 해제했습니다. 이제 아무 채널에서나 `/그림생성`을 사용할 수 있습니다.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ImageGen(bot))
