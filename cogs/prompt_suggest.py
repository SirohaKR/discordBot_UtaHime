# -*- coding: utf-8 -*-
"""프롬프트 추천 전용 채널.

지정한 채널에 문장(+선택적으로 이미지 첨부)을 그냥 입력하면, Claude가 NovelAI
Danbooru 태그로 변환해서 답장해준다. 이미지만 첨부하고 텍스트가 없으면 이미지를
보고 태그를 뽑아준다 (예: "이런 느낌으로 그리고 싶은데 태그 뭐 써야 돼?").
"""

import time

import discord
from discord import app_commands
from discord.ext import commands

from core import prompt_channel_db
from core.prompt_writer import PromptRefused, PromptWriterError, write_tags

_SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_COOLDOWN_SECONDS = 5.0


class PromptSuggest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_used: dict[int, float] = {}

    @commands.Cog.listener("on_message")
    async def on_message_suggest(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        channel_id = await prompt_channel_db.async_get_channel(self.bot.loop, message.guild.id)
        if not channel_id or message.channel.id != channel_id:
            return

        text = message.content.strip()
        if text.startswith(self.bot.command_prefix):
            return  # 실제 명령어는 기존 명령 처리에 맡긴다.

        image_attachment = next(
            (a for a in message.attachments if (a.content_type or "").split(";")[0] in _SUPPORTED_IMAGE_TYPES),
            None,
        )
        if not text and not image_attachment:
            return  # 빈 메시지나 지원 안 하는 첨부파일만 있으면 무시.

        now = time.monotonic()
        last = self._last_used.get(message.author.id, 0.0)
        if now - last < _COOLDOWN_SECONDS:
            return
        self._last_used[message.author.id] = now

        async with message.channel.typing():
            image_bytes = None
            media_type = None
            if image_attachment:
                image_bytes = await image_attachment.read()
                media_type = image_attachment.content_type.split(";")[0]

            try:
                tags = await write_tags(text, image_bytes=image_bytes, image_media_type=media_type)
            except PromptRefused as e:
                await message.reply(f"🚫 요청이 거부되었습니다: {e}", mention_author=False)
                return
            except PromptWriterError as e:
                await message.reply(f"❌ {e}", mention_author=False)
                return

        embed = discord.Embed(
            title="🎨 추천 태그",
            description=f"```{tags[:1900]}```",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="이 태그를 /그림생성에 태그모드:True로 붙여넣으면 바로 생성할 수 있어요.")
        await message.reply(embed=embed, mention_author=False)

    @app_commands.command(name="프롬프트채널설정", description="현재 채널을 프롬프트 추천 전용 채널로 지정합니다.")
    @app_commands.guild_only()
    async def set_prompt_channel(self, interaction: discord.Interaction):
        await prompt_channel_db.async_set_channel(self.bot.loop, interaction.guild.id, interaction.channel.id)
        await interaction.response.send_message(
            f"✅ 이제 {interaction.channel.mention} 에 문장(또는 이미지)을 적으면 AI가 태그를 추천해줍니다."
        )

    @app_commands.command(name="프롬프트채널설정해제", description="프롬프트 추천 채널 지정을 해제합니다.")
    @app_commands.guild_only()
    async def clear_prompt_channel(self, interaction: discord.Interaction):
        await prompt_channel_db.async_clear_channel(self.bot.loop, interaction.guild.id)
        await interaction.response.send_message("✅ 프롬프트 추천 채널 지정을 해제했습니다.")


async def setup(bot: commands.Bot):
    await bot.add_cog(PromptSuggest(bot))
