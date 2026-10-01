# -*- coding: utf-8 -*-
"""
음성 허브(자동 음성방) Cog — 시로냥 봇의 채널 관리 기능을 가져온 것.

허브로 지정한 음성채널에 누가 들어오면, 그 허브의 이름 규칙(name_template)대로 새 음성채널을
만들고 그쪽으로 옮겨준다. 봇이 만들어준 채널이 비면 자동으로 삭제한다.
   - {user} 는 입장한 사람의 이름으로 바뀐다.
   - {n} 은 "지금 그 허브에서 살아있는 방들 중 비어있는 가장 작은 번호"로 바뀐다
     (개인방 1이 지워지면 다음 방은 다시 개인방 1부터 채워짐).
   - user_limit(최대 인원)이 있으면 새로 만드는 채널에도 그대로 적용한다 (0 = 무제한).

허브는 웹 관리 페이지의 "🔊 음성 채널 자동 생성"에서 추가/삭제한다. 설정은
core/settings_store.py(data/settings.json)에 저장된다.

봇 권한: 채널 관리(Manage Channels) + 멤버 이동(Move Members)이 필요하다.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from core.settings_store import get_guild_settings, update_guild_settings


def _next_available_number(hub_id: int, temp_channels: list[dict]) -> int:
    """그 허브에서 지금 살아있는 채널들이 쓰고 있는 번호를 피해서, 빈 번호 중 가장 작은 걸 돌려준다."""
    used_numbers = {
        tc["number"] for tc in temp_channels if tc.get("hub_id") == hub_id and tc.get("number") is not None
    }
    n = 1
    while n in used_numbers:
        n += 1
    return n


class Channels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.bot:
            return
        guild = member.guild
        settings = get_guild_settings(guild.id)
        voice_hubs: list = settings.get("voice_hubs", [])
        temp_channels: list = settings.get("temp_voice_channels", [])
        if not voice_hubs and not temp_channels:
            return

        # 1) 허브 채널 중 하나에 새로 들어온 경우 -> 그 허브의 이름 규칙대로 채널을 만든다.
        if after.channel and (before.channel is None or before.channel.id != after.channel.id):
            hub = next((h for h in voice_hubs if h.get("id") == after.channel.id), None)
            if hub:
                template = hub.get("name_template") or "{user}의 방"
                number = _next_available_number(hub["id"], temp_channels) if "{n}" in template else None
                # format() 대신 replace를 써서 이름규칙에 다른 중괄호가 섞여 있어도 터지지 않게 한다.
                name = template.replace("{user}", member.display_name).replace("{n}", str(number or ""))[:100]

                try:
                    new_channel = await guild.create_voice_channel(
                        name=name,
                        category=after.channel.category,
                        user_limit=hub.get("user_limit") or 0,  # 0 = 디스코드 기준 무제한
                        reason="음성 허브 입장으로 자동 생성",
                    )
                    await member.move_to(new_channel, reason="자동 생성된 채널로 이동")
                except discord.HTTPException as e:
                    print(f"⚠️ [WARN] 음성 허브 채널 생성/이동 실패 (guild={guild.id}): {e}")
                else:
                    temp_channels.append({"channel_id": new_channel.id, "hub_id": hub["id"], "number": number})
                    update_guild_settings(guild.id, temp_voice_channels=temp_channels)

        # 2) 봇이 만들어준 임시 채널에서 사람이 빠져나가 완전히 비었으면 -> 삭제.
        if before.channel and (after.channel is None or after.channel.id != before.channel.id):
            entry = next((tc for tc in temp_channels if tc["channel_id"] == before.channel.id), None)
            if entry and not any(not m.bot for m in before.channel.members):
                try:
                    await before.channel.delete(reason="빈 채널 자동 삭제")
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    print(f"⚠️ [WARN] 빈 음성채널 삭제 실패 (guild={guild.id}): {e}")
                    return
                temp_channels.remove(entry)
                update_guild_settings(guild.id, temp_voice_channels=temp_channels)



async def setup(bot: commands.Bot):
    await bot.add_cog(Channels(bot))
