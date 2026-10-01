# -*- coding: utf-8 -*-
"""
이모지 반응으로 역할을 부여하는 Cog (예: 채팅채널에 공지 올리고 이모티콘으로 직업 선택).

"어떤 공지 메시지의 어떤 이모티콘이 어떤 역할과 연결되는지"는 명령어가 아니라
웹 설정 페이지(web/app.py)에서 관리자가 정한다. 이 파일은 그렇게 만들어진 공지
메시지에 이모티콘을 누르고 떼는 것만 감시해서 실제로 역할을 부여/회수하는
역할만 한다. (설정 따로, 실행 따로 — main.py로 켜둔 봇이 이 파일을 계속 감시한다)

cogs/verification.py(입장안내 버튼 -> 비공개 인증 -> 관리자 승인)와는 별개의
기능이다. 둘 다 "역할을 준다"는 점은 같지만, 이건 셀프 선택형(직업 등 여러 개 중
하나를 스스로 고르는 것)이고 verification.py는 관리자 확인이 필요한 인증형이다.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from core.settings_store import get_guild_settings


class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload, added=False)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent, added: bool) -> None:
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return  # DM이거나, 봇 스스로 붙인 이모지에는 반응하지 않는다.

        settings = get_guild_settings(payload.guild_id)
        job_roles = settings.get("job_roles") or {}
        if not job_roles or job_roles.get("message_id") != payload.message_id:
            return  # 우리가 관리하는 "직업 선택" 메시지가 아니면 무시.

        emoji_key = str(payload.emoji)
        entry = (job_roles.get("emoji_to_role") or {}).get(emoji_key)
        if not entry:
            return  # 등록되지 않은 이모지(누가 장난으로 다른 이모지를 눌렀을 때)는 무시.

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        role = guild.get_role(entry["role_id"])
        if role is None:
            return

        if added:
            # 직업은 한 사람당 하나만 갖도록, 이 메시지에 연결된 다른 직업 역할은 제거한다.
            other_role_ids = [e["role_id"] for e in job_roles["emoji_to_role"].values() if e["role_id"] != role.id]
            other_roles = [guild.get_role(rid) for rid in other_role_ids if guild.get_role(rid)]
            roles_to_remove = [r for r in other_roles if r in member.roles]
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="직업 변경(이모지 재선택)")
            await member.add_roles(role, reason="이모지 반응으로 직업 선택")

            # 같은 메시지에 다른 이모지도 눌러놨다면 헷갈리지 않도록 그 반응은 지워준다.
            # (봇에게 "메시지 관리" 권한이 없으면 조용히 실패하고 넘어간다)
            channel = guild.get_channel(payload.channel_id)
            if channel is not None:
                try:
                    message = await channel.fetch_message(payload.message_id)
                    for reaction in message.reactions:
                        if str(reaction.emoji) != emoji_key:
                            await reaction.remove(member)
                except discord.HTTPException:
                    pass
        else:
            await member.remove_roles(role, reason="이모지 반응 취소")


async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
