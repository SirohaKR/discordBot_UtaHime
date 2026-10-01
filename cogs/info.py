# -*- coding: utf-8 -*-
"""
잡다한 편의 명령어를 담는 Cog (시로냥 봇에서 가져옴). 지금은 "/설정" 하나뿐이다.
(우타히메의 예전 "/설정"(음악 채널 지정)은 웹 관리 페이지로 옮겨졌다 — 채널 설정은 전부 웹에서 한다)

/설정 명령어는 웹 설정 페이지(web/app.py) 주소를 디스코드에서 바로 받아볼 수 있게
해주는 지름길이다. 페이지 자체는 로그인(비밀번호 입력)이 앞을 막고 있지만, 그래도
"이런 명령어가 있다"는 것조차 서버 소유자 말고는 몰라도 되므로 이 명령어는 서버를
만든 사람(길드 소유자) 한 명만 쓸 수 있게 제한한다. 부관리자 등 "역할 관리" 권한이
있는 다른 사람도 여기서는 제외된다 — 그런 사람에게도 페이지를 열어주고 싶으면
비밀번호를 직접 알려주면 된다.
"""
from __future__ import annotations

import os

import discord
from discord import app_commands
from discord.ext import commands


def is_guild_owner():
    """서버를 만든 사람(길드 소유자)만 통과시키는 슬래시 명령어 체크."""

    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id

    return app_commands.check(predicate)


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="설정", description="[서버 소유자 전용] 랑다 관리 웹페이지 주소를 알려줍니다.")
    @app_commands.guild_only()
    # 관리자가 아닌 사람의 명령어 목록에서는 아예 숨긴다 (실제 사용은 아래 체크로 서버 소유자만).
    @app_commands.default_permissions(administrator=True)
    @is_guild_owner()
    async def settings_url(self, interaction: discord.Interaction):
        base_url = os.getenv("WEB_PUBLIC_URL")

        if not base_url:
            await interaction.response.send_message(
                "⚠️ 아직 설정 페이지 주소가 등록되지 않았습니다. .env의 WEB_PUBLIC_URL 값을 채워주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🔧 랑다 관리 페이지\n{base_url.rstrip('/')}/\n비밀번호를 입력하면 들어갑니다. (다른 사람에게 공유하지 마세요)",
            ephemeral=True,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "⚠️ 이 명령어는 서버 소유자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
