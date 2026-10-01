# -*- coding: utf-8 -*-
"""
"입장안내 -> 캐릭터 인증" 담당 Cog.

전체 흐름은 이렇다.

1. 웹 설정 페이지(web/app.py)가 입장안내 채널에 "🔑 캐릭터 인증하러 가기" 버튼이 달린
   공지 메시지를 올려둔다. (버튼 자체는 REST API로 올리지만, 눌렸을 때 반응하는 건
   실시간 연결이 필요해서 여기, 켜져 있는 봇(main.py)이 담당한다)
2. 멤버가 그 버튼을 누르면, 이 Cog가 그 멤버만 볼 수 있는 비공개 스레드를 만들어서
   웹에서 써둔 "캐릭터 인증" 안내 문구를 남긴다.
3. 멤버가 그 스레드에 인증용 이미지를 올리면, 이 Cog가 "✅ 승인 / ❌ 거절" 버튼이 달린
   확인 카드를 스레드에 남긴다.
4. "역할 관리" 권한이 있는 관리자가 승인을 누르면 웹에서 지정해둔 역할을 부여하고,
   거절을 누르면 스레드는 그대로 두어 다시 이미지를 올릴 수 있게 한다.

버튼 클릭(컴포넌트 인터랙션)은 discord.py의 View 콜백 대신 on_interaction에서 custom_id를
직접 해석해서 처리한다. custom_id 안에 필요한 정보(대상 유저 ID)를 다 담아두면, 봇이
재시작돼도(뷰를 다시 등록해줄 필요 없이) 그대로 눌러서 쓸 수 있기 때문이다.
"""
from __future__ import annotations

import io

import aiohttp
import discord
from discord.ext import commands

from core.settings_store import get_guild_settings, update_guild_settings

FILES_PER_MESSAGE = 10  # 디스코드 메시지 하나당 첨부파일 최대 개수

VERIFY_START = "siro:verify_start"
VERIFY_APPROVE_PREFIX = "siro:verify_approve:"
VERIFY_REJECT_PREFIX = "siro:verify_reject:"

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def _is_verify_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # 1) 입장안내 채널의 "캐릭터 인증하러 가기" 버튼 -> 비공개 스레드 생성
    # ------------------------------------------------------------------
    async def _start_verification(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return

        settings = get_guild_settings(guild.id)
        verification = settings.get("verification") or {}
        channel_id = verification.get("channel_id")
        role_id = verification.get("role_id")

        if not channel_id or not role_id:
            await interaction.response.send_message(
                "⚠️ 아직 관리자가 캐릭터 인증 설정을 끝내지 않았어요. 웹 설정 페이지에서 먼저 설정해주세요.",
                ephemeral=True,
            )
            return

        role = guild.get_role(role_id)
        if role is not None and role in member.roles:
            await interaction.response.send_message("이미 인증이 완료된 멤버예요! 🎉", ephemeral=True)
            return

        parent = guild.get_channel(channel_id)
        if not isinstance(parent, discord.TextChannel):
            await interaction.response.send_message(
                "⚠️ 인증 채널 설정이 올바르지 않아요. 웹 설정 페이지에서 다시 확인해주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        thread = await self._get_or_create_thread(guild, parent, member, verification)
        if thread is None:
            await interaction.followup.send(
                "⚠️ 인증용 비공개 채널을 만들지 못했어요. 봇에게 '비공개 스레드 만들기' 권한이 있는지 확인해주세요.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🔑 비공개 인증 채널을 만들었어요: {thread.mention}\n거기에 안내를 남겨뒀으니 확인해주세요!",
            ephemeral=True,
        )

    async def _get_or_create_thread(
        self,
        guild: discord.Guild,
        parent: discord.TextChannel,
        member: discord.Member,
        verification: dict,
    ) -> discord.Thread | None:
        threads_map: dict = verification.get("threads") or {}
        existing_id = threads_map.get(str(member.id))

        if existing_id:
            thread = guild.get_thread(existing_id)
            if thread is None:
                try:
                    thread = await guild.fetch_channel(existing_id)
                except (discord.NotFound, discord.Forbidden):
                    thread = None
            if isinstance(thread, discord.Thread):
                if thread.archived or thread.locked:
                    # 승인 처리 때 잠가둔 스레드일 수 있으니(재인증 등), 다시 쓸 수 있게 풀어준다.
                    try:
                        await thread.edit(archived=False, locked=False)
                    except discord.HTTPException:
                        thread = None
                if thread is not None:
                    return thread

        try:
            thread = await parent.create_thread(
                name=f"인증-{member.display_name}"[:100],
                type=discord.ChannelType.private_thread,
                invitable=False,
                reason="캐릭터 인증 요청으로 생성",
            )
            await thread.add_user(member)
        except discord.HTTPException:
            return None

        title = verification.get("title") or "캐릭터 인증"
        body = verification.get("body") or "인증할 캐릭터 이미지를 이 채널에 업로드해주세요."
        embed = discord.Embed(title=title, description=body, color=0x5B8CFF)
        await thread.send(content=member.mention, embed=embed)

        # 비공개 스레드는 초대된 사람만 볼 수 있어서, 만들어둬도 관리자가 못 보고 지나칠 수
        # 있다. '역할 관리' 권한이 있는 사람은 자동으로 스레드에 넣고 멘션까지 남겨서
        # 새 인증 요청이 왔다는 걸 바로 알 수 있게 한다.
        admins = [m for m in guild.members if not m.bot and m.id != member.id and _is_verify_admin(m)]
        for admin in admins:
            try:
                await thread.add_user(admin)
            except discord.HTTPException:
                pass
        if admins:
            mentions = " ".join(a.mention for a in admins)
            await thread.send(f"🔔 {mentions} 새 캐릭터 인증 요청이 도착했어요!")

        threads_map[str(member.id)] = thread.id
        verification["threads"] = threads_map
        update_guild_settings(guild.id, verification=verification)
        return thread

    # ------------------------------------------------------------------
    # 2) 인증 스레드에 이미지가 올라오면 -> 승인/거절 버튼이 달린 확인 카드 게시
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if not message.attachments:
            return

        settings = get_guild_settings(message.guild.id)
        verification = settings.get("verification") or {}
        threads_map: dict = verification.get("threads") or {}
        if threads_map.get(str(message.author.id)) != message.channel.id:
            return  # 이 사람이 인증용으로 만든 스레드가 아니면 무시.

        # 한 메시지에 이미지를 여러 장 첨부했을 수 있으니, 첫 장만 고르지 않고 전부 모은다.
        images = [a for a in message.attachments if _is_image_attachment(a)]
        if not images:
            return

        count_note = f" (이미지 {len(images)}장)" if len(images) > 1 else ""
        embeds = [
            discord.Embed(
                title=f"📥 {message.author.display_name}님의 인증 이미지{count_note}",
                description="관리자님, 확인 후 아래 버튼으로 승인/거절해주세요.",
                color=0x5B8CFF,
            )
        ]
        embeds[0].set_image(url=images[0].url)
        # 임베드 하나에는 이미지를 하나만 넣을 수 있어서, 두 번째 장부터는 이미지만 있는
        # 임베드를 추가로 붙인다 (디스코드는 메시지 하나에 임베드를 최대 10개까지 허용).
        for extra in images[1:]:
            embeds.append(discord.Embed(color=0x5B8CFF).set_image(url=extra.url))

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="✅ 승인", style=discord.ButtonStyle.success,
                custom_id=f"{VERIFY_APPROVE_PREFIX}{message.author.id}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="❌ 거절", style=discord.ButtonStyle.danger,
                custom_id=f"{VERIFY_REJECT_PREFIX}{message.author.id}",
            )
        )
        await message.channel.send(embeds=embeds, view=view)

    # ------------------------------------------------------------------
    # 3) 버튼 클릭 처리 (컴포넌트 인터랙션은 View 콜백이 아니라 여기서 전부 해석한다)
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")

        if custom_id == VERIFY_START:
            await self._start_verification(interaction)
        elif custom_id.startswith(VERIFY_APPROVE_PREFIX):
            await self._resolve_verification(interaction, int(custom_id[len(VERIFY_APPROVE_PREFIX):]), approve=True)
        elif custom_id.startswith(VERIFY_REJECT_PREFIX):
            await self._resolve_verification(interaction, int(custom_id[len(VERIFY_REJECT_PREFIX):]), approve=False)

    async def _resolve_verification(self, interaction: discord.Interaction, target_id: int, approve: bool) -> None:
        guild = interaction.guild
        admin = interaction.user
        if guild is None or not isinstance(admin, discord.Member):
            return

        if not _is_verify_admin(admin):
            await interaction.response.send_message(
                "⚠️ 이 버튼은 '역할 관리' 권한이 있는 관리자만 사용할 수 있어요.", ephemeral=True
            )
            return

        thread = interaction.channel
        # 이미지가 여러 장이면 확인 카드에 임베드가 여러 개 붙어있으니, 전부 가져온다.
        original_embeds = interaction.message.embeds or [discord.Embed()]

        if approve:
            settings = get_guild_settings(guild.id)
            verification = settings.get("verification") or {}
            role = guild.get_role(verification.get("role_id")) if verification.get("role_id") else None
            member = guild.get_member(target_id)

            if role is None:
                await interaction.response.send_message(
                    "⚠️ 인증 완료 시 부여할 역할이 설정되어 있지 않아요. 웹 설정 페이지에서 먼저 지정해주세요.",
                    ephemeral=True,
                )
                return
            if member is None:
                await interaction.response.send_message("⚠️ 해당 멤버를 서버에서 찾을 수 없어요.", ephemeral=True)
                return

            await interaction.response.defer()
            try:
                await member.add_roles(role, reason=f"캐릭터 인증 승인 (관리자: {admin})")
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ 역할을 부여할 권한이 없어요. 서버 설정 → 역할에서 봇 역할을 "
                    f"'{role.name}' 역할보다 위로 옮겨주세요.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.followup.send(f"⚠️ 역할 부여 중 오류가 났어요: {e}", ephemeral=True)
                return

            resolved_embeds = [e.copy() for e in original_embeds]
            for e in resolved_embeds:
                e.color = 0x57F287
            resolved_embeds[0].set_footer(text=f"✅ {admin.display_name}님이 승인함")
            await interaction.edit_original_response(embeds=resolved_embeds, view=None)

            # 승인 버튼을 누른 카드에 붙어있던 이미지만이 아니라, 이 스레드에 그동안
            # 이 사람이 올린 이미지를 전부 훑어서 로그에 남긴다 (여러 메시지로 나눠
            # 올렸어도 빠짐없이 모으려고).
            image_urls: list[str] = []
            if isinstance(thread, discord.Thread):
                image_urls = await self._collect_thread_images(thread, target_id)

            if isinstance(thread, discord.Thread):
                await thread.send(f"🎉 {member.mention}님의 인증이 승인되어 역할이 부여됐어요!")
                try:
                    await thread.edit(archived=True, locked=True)
                except discord.HTTPException:
                    pass

            await self._log_verification(guild, verification, member, role, admin, image_urls)
        else:
            await interaction.response.defer()

            resolved_embeds = [e.copy() for e in original_embeds]
            for e in resolved_embeds:
                e.color = 0xE2574C
            resolved_embeds[0].set_footer(text=f"❌ {admin.display_name}님이 거절함")
            await interaction.edit_original_response(embeds=resolved_embeds, view=None)

            if isinstance(thread, discord.Thread):
                await thread.send(f"❌ 인증 이미지가 거절됐어요. <@{target_id}>님, 다른 이미지로 다시 업로드해주세요.")

    async def _collect_thread_images(self, thread: discord.Thread, member_id: int) -> list[str]:
        """인증 스레드의 대화 기록 전체를 훑어서, 그 멤버가 올린 이미지 URL을 전부 모은다.

        여러 메시지로 나눠 올렸어도(예: 사진1 먼저 보내고, 나중에 사진2 추가로 보내고)
        승인 버튼을 한 번만 눌러도 그동안 올라온 이미지를 빠짐없이 로그에 남기기 위함.
        """
        urls: list[str] = []
        try:
            async for msg in thread.history(limit=200, oldest_first=True):
                if msg.author.id != member_id:
                    continue
                for attachment in msg.attachments:
                    if _is_image_attachment(attachment):
                        urls.append(attachment.url)
        except discord.HTTPException:
            pass
        return urls

    async def _download_image(self, url: str) -> discord.File | None:
        """이미지 주소에서 실제 파일 내용을 받아와 디스코드에 다시 첨부할 수 있는
        형태(discord.File)로 만든다. 그냥 embed에 URL만 넣으면 이미지 하나당 임베드가
        하나씩 필요해서 세로로 길게 늘어지는데, 진짜 첨부파일로 여러 장을 같이 보내면
        디스코드가 알아서 작은 바둑판(그리드) 미리보기로 묶어서 보여준다."""
        filename = url.split("?")[0].rsplit("/", 1)[-1] or "image.png"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
        except aiohttp.ClientError:
            return None
        return discord.File(io.BytesIO(data), filename=filename)

    async def _log_verification(
        self,
        guild: discord.Guild,
        verification: dict,
        member: discord.Member,
        role: discord.Role,
        admin: discord.Member,
        image_urls: list[str],
    ) -> None:
        """웹 설정에서 '인증 로그 채널'을 지정해뒀으면, 승인된 인증 내역을 거기 정리해서 남긴다.

        image_urls는 스레드 전체에서 모은 이미지라 여러 장일 수 있다. 첨부파일로 같이
        보내서 바둑판 형태의 작은 미리보기로 묶이게 하고, 10장을 넘으면(디스코드 메시지
        하나당 첨부파일 제한) 메시지를 나눠 보낸다.
        """
        log_channel_id = verification.get("log_channel_id")
        if not log_channel_id:
            return
        log_channel = guild.get_channel(log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            return

        log_embed = discord.Embed(
            title="✅ 캐릭터 인증 완료",
            description=f"{member.mention}님이 인증되어 {role.mention} 역할이 부여됐어요.",
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        footer = f"승인: {admin.display_name}"
        if image_urls:
            footer += f" · 이미지 {len(image_urls)}장"
        log_embed.set_footer(text=footer)

        files = [f for f in [await self._download_image(url) for url in image_urls] if f is not None]
        # 여러 장을 한 메시지에 같이 보낼 때 이름이 겹치면 디스코드가 거부하므로,
        # 번호를 붙여서 겹치지 않게 한다.
        for i, f in enumerate(files):
            f.filename = f"{i}_{f.filename}"

        try:
            if not files:
                await log_channel.send(embed=log_embed)
                return
            await log_channel.send(embed=log_embed, files=files[:FILES_PER_MESSAGE])
            for i in range(FILES_PER_MESSAGE, len(files), FILES_PER_MESSAGE):
                await log_channel.send(files=files[i : i + FILES_PER_MESSAGE])
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
