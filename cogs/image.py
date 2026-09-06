# -*- coding: utf-8 -*-
"""NovelAI 이미지 생성 기능.

서버(길드) 공용 NAI_TOKEN(.env) 하나로 동작한다. 동시 생성 요청은 mutex로 직렬화하고
(NAI API가 동시 요청을 거부/오류 처리하는 경우가 있어 참고 문서에서 권장하는 방식),
유저별 쿨다운으로 스팸을 막는다.
"""

import asyncio
import io
import os

import discord
from discord import app_commands
from discord.ext import commands

from core import nai_client, prompt_writer
from core.error_notify import notify_error
from core.nai_client import NaiAuthError, NaiError, NaiRateLimitError
from core.prompt_writer import PromptRefused, PromptWriterError

NAI_TOKEN = os.getenv("NAI_TOKEN")

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


class ImageGen(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = discord.utils.MISSING  # cog_load에서 이벤트 루프에 맞춰 생성

    async def cog_load(self):
        self._lock = asyncio.Lock()

    @app_commands.command(name="그림생성", description="원하는 그림을 문장으로 설명하면 AI가 태그를 짜서 NovelAI로 그려줍니다.")
    @app_commands.describe(
        프롬프트="그리고 싶은 이미지를 자유롭게 설명 (한국어 가능. 예: 파란 머리에 웃고 있는 여자아이). 태그모드 켜면 영문 태그로 직접 입력",
        네거티브="제외하고 싶은 요소 (선택)",
        비율="이미지 비율/크기",
        모델="사용할 NovelAI 모델",
        등급="이미지 수위",
        시드="재현하고 싶은 시드값 (비우면 랜덤)",
        태그모드="켜면 AI 변환 없이 프롬프트를 Danbooru 태그 그대로 사용 (태그를 아는 사람용)",
    )
    @app_commands.choices(비율=SIZE_CHOICES, 모델=MODEL_CHOICES, 등급=RATING_CHOICES)
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
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

        if self._lock.locked():
            await interaction.followup.send("⏳ 다른 요청을 처리 중이라 순서를 기다립니다...")

        async with self._lock:
            try:
                result = await nai_client.generate_image(
                    NAI_TOKEN,
                    prompt=final_prompt,
                    negative_prompt=네거티브,
                    model=model,
                    width=width,
                    height=height,
                    rating=rating,
                    seed=시드,
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
        embed = discord.Embed(title="🎨 이미지 생성 완료", color=discord.Color.purple())
        if not 태그모드 and final_prompt != 프롬프트:
            embed.add_field(name="입력한 설명", value=f"```{프롬프트[:300]}```", inline=False)
            embed.add_field(name="변환된 태그", value=f"```{final_prompt[:500]}```", inline=False)
        else:
            embed.add_field(name="태그", value=f"```{final_prompt[:500]}```", inline=False)
        if prompt_note:
            embed.add_field(name="안내", value=prompt_note, inline=False)
        embed.add_field(name="비율", value=f"{width}x{height}", inline=True)
        embed.add_field(name="모델", value=nai_client.MODELS.get(model, model), inline=True)
        embed.add_field(name="등급", value=등급.name if 등급 else "약한 선정성 (기본)", inline=True)
        embed.add_field(name="시드", value=str(시드) if 시드 else "랜덤", inline=True)
        embed.set_image(url="attachment://nai_image.png")
        embed.set_footer(text=f"요청: {interaction.user.display_name}")

        await interaction.followup.send(embed=embed, file=file)

    @generate.error
    async def generate_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ 너무 자주 요청했습니다. {error.retry_after:.0f}초 후 다시 시도하세요.", ephemeral=True
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


async def setup(bot: commands.Bot):
    await bot.add_cog(ImageGen(bot))
