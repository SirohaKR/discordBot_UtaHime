# -*- coding: utf-8 -*-
# 우타히메 - 음악 재생 Cog (discord.py 2.x / yt-dlp)

import os
import json
import shutil
import contextlib
import functools
import asyncio

import discord
from discord.ext import commands
import yt_dlp as ytdl

from core.song_queue import SongQueue
from core import playlist_db
from core import guild_settings_db
from core import now_playing_db
from core import playback_log_db
from core.error_notify import notify_error
from core.errors import HandledCommandError

LEGACY_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot_settings.json")

# =========================
# 설정
# =========================
def _resolve_ffmpeg_path() -> str:
    env_path = os.getenv("FFMPEG_PATH")
    if env_path:
        return env_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 로컬 Windows 개발 환경 기본값 (Docker/Linux에서는 PATH의 ffmpeg가 우선 사용됨)
    return r"C:\ffmpeg\bin\ffmpeg.exe"


FFMPEG_EXECUTABLE = _resolve_ffmpeg_path()

# 재연결 옵션은 유지하되, probesize/analyzeduration/디버그 로그 등
# 시작 지연과 CPU 부담만 키우는 옵션은 제거해서 곡 전환을 더 빠르게 만든다.
# -reconnect_initial_delay는 일부 ffmpeg 빌드(예: gyan.dev 최신 빌드)에서 인식하지 못해
# 시작하자마자 곧바로 종료되는 원인이 될 수 있어 제외한다.
FFMPEG_BEFORE_OPTIONS = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_DEFAULT_OPTIONS = "-vn -loglevel warning"

ytdl.utils.bug_reports_message = lambda *args, **kwargs: ""
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "force_ipv4": True,
    # android_vr 클라이언트로 뽑힌 스트림 URL이 PO 토큰 요구 정책 때문에
    # ffmpeg에서 403 Forbidden으로 막히는 사례가 잦아 제외한다.
    # tv 클라이언트 단독으로는 일부 영상에서 DRM 전용 포맷만 내려주는
    # 알려진 버그가 있어, 다른 클라이언트도 함께 시도해 DRM이 아닌
    # 포맷을 고를 수 있게 폭을 넓힌다.
    "extractor_args": {"youtube": {"player_client": ["web", "android", "tv", "ios"]}},
}
ytdl_client = ytdl.YoutubeDL(YTDL_OPTIONS)

VOLUME_DEFAULT = 0.30


# =========================
# YTDLSource
# =========================
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        duration_sec = int(data.get("duration", 0) or 0)
        self.duration = self.parse_duration(duration_sec)
        self.title = data.get("title")
        self.url = data.get("webpage_url")
        self.thumbnail = data.get("thumbnail")

    @classmethod
    def parse_duration(cls, duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02}:{minutes:02}:{seconds:02}"
        return f"{minutes:02}:{seconds:02}"

    @classmethod
    def _build(cls, data, volume):
        stream_url = data.get("url")
        if not stream_url:
            raise ValueError("스트리밍 URL을 찾을 수 없습니다. (재생 불가)")

        # 유튜브는 스트림 URL을 발급한 요청과 동일한 헤더(User-Agent 등)로
        # 접근하지 않으면 403 Forbidden을 반환한다. yt-dlp가 추출한
        # http_headers를 ffmpeg 요청에도 그대로 실어 보내야 한다.
        before_options = FFMPEG_BEFORE_OPTIONS
        http_headers = data.get("http_headers")
        if http_headers:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
            before_options = f'{before_options} -headers "{headers_str}"'

        return cls(
            discord.FFmpegPCMAudio(
                stream_url,
                executable=FFMPEG_EXECUTABLE,
                before_options=before_options,
                options=FFMPEG_DEFAULT_OPTIONS,
            ),
            data=data,
            volume=volume,
        )

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, volume=VOLUME_DEFAULT):
        flat_options = YTDL_OPTIONS.copy()
        flat_options["extract_flat"] = "in_playlist"
        ytdl_flat_client = ytdl.YoutubeDL(flat_options)
        try:
            partial = functools.partial(ytdl_flat_client.extract_info, search, download=False, process=True)
            data = await loop.run_in_executor(None, partial)
        except ytdl.utils.DownloadError as e:
            raise ValueError(f"❌ yt-dlp 다운로드 오류: {e}")
        except Exception as e:
            raise ValueError(f"❌ 노래 메타데이터 로딩 중 알 수 없는 오류: {e}")

        if "entries" in data:
            if data.get("_type") == "playlist":
                return data["entries"]
            if len(data["entries"]) > 0:
                data = data["entries"][0]
            else:
                raise ValueError("검색 결과가 없습니다.")

        return cls._build(data, volume)


# =========================
# Song
# =========================
class Song:
    def __init__(self, source, requester, initial_data: dict = None):
        self.source = source
        self.requester = requester
        self.initial_data = initial_data if initial_data is not None else {}
        self._load_lock = asyncio.Lock()

        if source and isinstance(source, YTDLSource):
            self.title = source.title
            self.url = source.url
            self.duration = source.duration
        elif initial_data:
            self.title = initial_data.get("title", "제목 없음")
            self.url = initial_data.get("webpage_url", "URL 없음")
            duration_sec = int(initial_data.get("duration", 0) or 0)
            self.duration = YTDLSource.parse_duration(duration_sec)
        else:
            self.title = "제목 없음"
            self.url = "URL 없음"
            self.duration = "00:00"

    async def load_stream_source(self, loop, volume):
        # 이미 로드됐거나(프리페치 포함) 로딩 중이면 중복 요청하지 않는다.
        async with self._load_lock:
            if self.source:
                try:
                    self.source.volume = volume
                except Exception:
                    pass
                return self.source

            search_term = self.url if isinstance(self.url, str) and self.url.startswith("http") else self.initial_data.get("id")
            if not search_term:
                raise ValueError("스트림을 로드할 유효한 검색어가 없습니다.")

            try:
                partial = functools.partial(ytdl_client.extract_info, search_term, download=False, process=True)
                data = await loop.run_in_executor(None, partial)
            except ytdl.utils.DownloadError as e:
                raise ValueError(f"❌ 스트림 로딩 실패 (DownloadError): {e}")
            except Exception as e:
                raise ValueError(f"❌ 스트림 로딩 중 알 수 없는 오류: {e}")

            if "entries" in data and len(data["entries"]) > 0:
                data = data["entries"][0]

            self.source = YTDLSource._build(data, volume)
            self.title = data.get("title", self.title)
            self.url = data.get("webpage_url", self.url)
            self.duration = YTDLSource.parse_duration(int(data.get("duration", 0) or 0))

            return self.source

    def create_embed(self, title_prefix="🎶 현재 재생 중", current_progress: str = "00:00", progress_bar: str = ""):
        description_text = f"**{progress_bar}**\n[{self.title}]({self.url})"
        embed = (
            discord.Embed(title=title_prefix, description=description_text, color=discord.Color.blue())
            .add_field(name="길이", value=f"**{current_progress}** / {self.duration}")
            .add_field(name="요청자", value=self.requester.mention if self.requester else "알 수 없음")
            .set_footer(text="버튼을 사용하여 제어하세요.")
        )
        thumb = None
        if self.source:
            thumb = self.source.data.get("thumbnail")
        if not thumb and self.initial_data:
            thumb = self.initial_data.get("thumbnail")
        if thumb:
            embed.set_thumbnail(url=thumb)
        return embed


# =========================
# ControllerView
# =========================
class ControllerView(discord.ui.View):
    def __init__(self, cog: "MusicPlayer", *args, **kwargs):
        super().__init__(*args, **kwargs, timeout=None)
        self.cog = cog

    def _update_button_labels(self):
        shuffle_button = [c for c in self.children if c.custom_id == "shuffle_button"][0]
        shuffle_button.style = discord.ButtonStyle.green if self.cog.queue.shuffle else discord.ButtonStyle.grey
        shuffle_button.label = "셔플 ON" if self.cog.queue.shuffle else "셔플 OFF"

        loop_button = [c for c in self.children if c.custom_id == "loop_button"][0]
        if self.cog.loop_mode == 'all':
            loop_button.style = discord.ButtonStyle.green
            loop_button.label = "🔁 반복: 전체"
        elif self.cog.loop_mode == 'one':
            loop_button.style = discord.ButtonStyle.red
            loop_button.label = "🔂 반복: 한 곡"
        else:
            loop_button.style = discord.ButtonStyle.grey
            loop_button.label = "🔁 반복: 끔"

    async def update_message(self, song: "Song" = None, title_prefix="🎶 현재 재생 중"):
        self._update_button_labels()
        current_progress, progress_bar = self.cog._get_playback_info()

        if song:
            render_key = (song.title, current_progress, progress_bar, int(self.cog.volume * 100), self.cog.loop_mode, self.cog.queue.shuffle)
        else:
            render_key = ("empty",)
        if self.cog._last_render_key == render_key:
            return
        self.cog._last_render_key = render_key

        if self.cog.controller_message_created_at:
            age = (discord.utils.utcnow() - self.cog.controller_message_created_at).total_seconds()
            if age >= 55 * 60:
                await self.cog._rotate_controller_message(self.cog.controller_message.channel)

        if self.cog.controller_message:
            if song:
                embed = song.create_embed(title_prefix, current_progress, progress_bar)
                embed.add_field(name="볼륨", value=f"{int(self.cog.volume*100)}%")
                loop_text = "전체" if self.cog.loop_mode == "all" else ("한 곡" if self.cog.loop_mode == "one" else "끔")
                embed.add_field(name="반복", value=loop_text)
                embed.add_field(name="셔플", value="ON" if self.cog.queue.shuffle else "OFF")
                queue_len = len(self.cog.queue)
                if queue_len:
                    embed.add_field(name="대기열", value=f"{queue_len}곡 대기 중")
            else:
                embed = discord.Embed(
                    title="대기열이 비었습니다.",
                    description="`!play [검색어]` 또는 `!실행 [검색어]`로 음악을 추가하세요.",
                    color=discord.Color.light_grey(),
                ).set_footer(text="봇이 5분 동안 활동이 없으면 자동 퇴장합니다.")

            try:
                await self.cog.controller_message.edit(embed=embed, view=self)
            except discord.HTTPException as e:
                if getattr(e, "code", None) == 30046 or "Maximum number of edits" in str(e):
                    await self.cog._rotate_controller_message(self.cog.controller_message.channel)
                    with contextlib.suppress(Exception):
                        await self.cog.controller_message.edit(embed=embed, view=self)
                else:
                    print(f"❌ [ERROR] 메시지 업데이트 중 오류: {e}")
            except Exception as e:
                print(f"❌ [ERROR] 메시지 업데이트 중 오류: {e}")

    @discord.ui.button(label="⏸️ 일시정지", style=discord.ButtonStyle.primary, custom_id="pause_button", row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.cog.vc and self.cog.vc.is_playing():
            self.cog.vc.pause()
            self.cog.paused_at = discord.utils.utcnow()
            button.label = "▶️ 재개"
            await self.update_message(self.cog.current_song, "⏸️ 일시정지됨")
        elif self.cog.vc and self.cog.vc.is_paused():
            self.cog.vc.resume()
            if self.cog.paused_at:
                self.cog.paused_total += (discord.utils.utcnow() - self.cog.paused_at).total_seconds()
                self.cog.paused_at = None
            button.label = "⏸️ 일시정지"
            await self.update_message(self.cog.current_song, "🎶 현재 재생 중")
        else:
            await self.cog._send_temporary_message(interaction.channel, "재생 중인 곡이 없습니다.")
        await interaction.message.edit(view=self)

    @discord.ui.button(label="⏭️ 스킵", style=discord.ButtonStyle.secondary, custom_id="skip_button", row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.cog.vc and (self.cog.vc.is_playing() or self.cog.vc.is_paused()):
            self.cog.vc.stop()
            await self.cog._send_temporary_message(
                interaction.channel, f"⏭️ **{self.cog.current_song.title}**을(를) 건너뛰었습니다.", delay=3
            )
        else:
            await self.cog._send_temporary_message(interaction.channel, "건너뛸 곡이 없습니다.")

    @discord.ui.button(label="🔀 셔플 OFF", style=discord.ButtonStyle.grey, custom_id="shuffle_button", row=0)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.cog.queue.set_shuffle(not self.cog.queue.shuffle)
        self.cog._save_guild_settings()

        if self.cog.queue.shuffle:
            await self.cog._send_temporary_message(interaction.channel, "🔀 대기열 셔플 **ON**", delay=3)
        else:
            await self.cog._send_temporary_message(interaction.channel, "🔀 대기열 셔플 **OFF**", delay=3)

        self._update_button_labels()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="🔁 반복: 끔", style=discord.ButtonStyle.grey, custom_id="loop_button", row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.cog.loop_mode == 'off':
            self.cog.loop_mode = 'all'
            msg = "🔁 전체 반복 **ON**"
        elif self.cog.loop_mode == 'all':
            self.cog.loop_mode = 'one'
            msg = "🔂 한 곡 반복 **ON**"
        else:
            self.cog.loop_mode = 'off'
            msg = "🔁 반복 **OFF**"

        self.cog._save_guild_settings()

        await self.cog._send_temporary_message(interaction.channel, msg, delay=3)
        self._update_button_labels()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="📄 대기열 보기", style=discord.ButtonStyle.secondary, custom_id="queue_button", row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.cog.queue.is_empty() and not self.cog.current_song:
            await self.cog._send_temporary_message(interaction.channel, "⚠️ 대기열이 비어 있습니다.", delay=5)
            return

        queue_list = []
        if self.cog.current_song:
            queue_list.append(
                f"**▶️ 현재 재생 중:** [{self.cog.current_song.title}]({self.cog.current_song.url}) "
                f"(`{self.cog.current_song.duration}`)"
            )
        queue_content = self.cog.queue.to_list()
        for i, song in enumerate(queue_content):
            if len(queue_list) < 15:
                queue_list.append(f"`{i+1}.` [{song.title}]({song.url}) (`{song.duration}`)")
            else:
                queue_list.append(f"`... 그리고 {len(queue_content) - i}개 곡 더 ...`")
                break

        embed = discord.Embed(
            title="🎵 현재 재생 대기열",
            description="\n".join(queue_list) if queue_list else "대기열이 비어 있습니다.",
            color=discord.Color.gold(),
        )
        await interaction.channel.send(embed=embed, delete_after=30)

    @discord.ui.button(label="🔇 정지/퇴장", style=discord.ButtonStyle.danger, custom_id="stop_button", row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.cog.vc:
            self.cog.queue.clear()
            self.cog.vc.stop()
            await self.cog.vc.disconnect()
            self.cog.vc = None
            self.cog.current_song = None
            self.cog.player_task = None

            if self.cog.progress_task and not self.cog.progress_task.done():
                self.cog.progress_task.cancel()
            self.cog.progress_task = None

            if self.cog.prefetch_task and not self.cog.prefetch_task.done():
                self.cog.prefetch_task.cancel()
            self.cog.prefetch_task = None

            await self.update_message(None)
            await self.cog._clear_now_playing()
            await self.cog._send_temporary_message(interaction.channel, "🔇 음악 재생 중지 및 퇴장", delay=5)


# =========================
# MusicPlayer Cog
# =========================
class MusicPlayer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = SongQueue()
        self._queue_updated = asyncio.Event()
        self.vc = None
        self.current_song = None
        self.controller_message = None
        self.player_task = None
        self.prefetch_task = None
        self.settings_poll_task = None
        self.guild_id = None

        # 반복 모드: 'off' | 'all' | 'one'
        self.loop_mode = 'off'

        self.start_time = None
        self.paused_at = None
        self.paused_total = 0.0

        self.progress_task = None

        self.controller_message_created_at = None
        self._last_render_key = None

        self.controller_view = ControllerView(self)

        # 저장된 길드가 정확히 하나뿐이면(개인/소규모 운영 기준) 그 설정을 이어서 사용.
        # 여러 길드가 저장돼 있으면 명령 실행 시점(cog_before_invoke)에 guild_id가 잡힌다.
        existing_guild_ids = guild_settings_db.list_guild_ids()
        if len(existing_guild_ids) == 1:
            self.guild_id = existing_guild_ids[0]

        settings = guild_settings_db.get_settings(self.guild_id) if self.guild_id else dict(guild_settings_db.DEFAULTS)
        self.music_channel_id = settings.get("music_channel_id")
        self.controller_message_id = settings.get("controller_message_id")
        self.volume = float(settings.get("default_volume", VOLUME_DEFAULT))
        self.queue.set_shuffle(bool(settings.get("shuffle", False)))
        lm = settings.get("loop_mode", "off")
        self.loop_mode = lm if lm in ("off", "all", "one") else "off"

        print(f"⚙️ [LOG] 전용 채널 ID: {self.music_channel_id}")
        print(f"⚙️ [LOG] 컨트롤러 메시지 ID: {self.controller_message_id}")
        print(f"🔊 [LOG] 기본 볼륨: {int(self.volume*100)}%")
        print(f"🔁 [LOG] 반복 모드: {self.loop_mode}")
        print(f"🔀 [LOG] 셔플 상태: {'ON' if self.queue.shuffle else 'OFF'}")

    async def cog_load(self):
        if not os.path.exists(FFMPEG_EXECUTABLE) and not shutil.which("ffmpeg"):
            print(f"❌ [ERROR] FFmpeg 실행 파일을 찾을 수 없습니다: {FFMPEG_EXECUTABLE}")
            print("    FFMPEG_PATH 환경 변수를 설정하거나 FFmpeg를 설치하세요.")
        self.settings_poll_task = self.bot.loop.create_task(self._settings_poll_loop())

    async def cog_unload(self):
        if self.settings_poll_task and not self.settings_poll_task.done():
            self.settings_poll_task.cancel()

    def _save_guild_settings(self):
        if not self.guild_id:
            return
        guild_settings_db.upsert_settings(
            self.guild_id,
            music_channel_id=self.music_channel_id,
            controller_message_id=self.controller_message_id,
            default_volume=self.volume,
            shuffle=self.queue.shuffle,
            loop_mode=self.loop_mode,
        )

    async def _record_now_playing(self, song):
        if not self.guild_id:
            return
        thumbnail = song.source.data.get("thumbnail") if song.source else None
        requester_name = song.requester.name if song.requester else None
        now_iso = discord.utils.utcnow().isoformat()
        try:
            await now_playing_db.set_now_playing(
                self.bot.loop,
                self.guild_id,
                title=song.title,
                url=song.url,
                thumbnail=thumbnail,
                requester=requester_name,
                duration=song.duration,
                queue_length=len(self.queue),
                updated_at=now_iso,
            )
            await playback_log_db.log_playback(self.bot.loop, self.guild_id, song.title, song.url, requester_name, now_iso)
        except Exception as e:
            print(f"⚠️ [WARN] 재생 상태/로그 기록 실패: {e}")

    async def _clear_now_playing(self):
        if not self.guild_id:
            return
        try:
            await now_playing_db.clear_now_playing(self.bot.loop, self.guild_id, discord.utils.utcnow().isoformat())
        except Exception as e:
            print(f"⚠️ [WARN] 재생 상태 초기화 실패: {e}")

    async def _settings_poll_loop(self):
        """웹 설정 페이지에서 바뀐 값을 몇 초 안에 봇에 반영한다."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(5)
            if not self.guild_id:
                continue
            try:
                row = await guild_settings_db.async_get_settings(self.bot.loop, self.guild_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"⚠️ [WARN] 설정 폴링 오류: {e}")
                continue

            changed = False
            if abs(row["default_volume"] - self.volume) > 1e-6:
                self.volume = row["default_volume"]
                if self.current_song and self.current_song.source:
                    self.current_song.source.volume = self.volume
                changed = True
            if row["shuffle"] != self.queue.shuffle:
                self.queue.set_shuffle(row["shuffle"])
                changed = True
            if row["loop_mode"] in ("off", "all", "one") and row["loop_mode"] != self.loop_mode:
                self.loop_mode = row["loop_mode"]
                changed = True

            if changed:
                print("🌐 [LOG] 웹에서 변경된 설정을 반영했습니다.")
                if self.controller_message and self.current_song:
                    await self.controller_view.update_message(self.current_song)

    async def migrate_legacy_settings(self):
        """구버전 bot_settings.json → guild_settings DB 1회성 마이그레이션 (on_ready에서 호출)."""
        if guild_settings_db.list_guild_ids():
            return  # 이미 DB에 설정이 있으면 건드리지 않음
        if not os.path.exists(LEGACY_SETTINGS_FILE):
            return

        try:
            with open(LEGACY_SETTINGS_FILE, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except Exception as e:
            print(f"⚠️ [WARN] 레거시 설정 파일 읽기 실패: {e}")
            return

        channel_id = legacy.get("music_channel_id")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not channel or not channel.guild:
            print("⚠️ [WARN] 레거시 채널을 찾을 수 없어 마이그레이션을 건너뜁니다.")
            return

        guild_id = channel.guild.id
        volume = float(legacy.get("default_volume", VOLUME_DEFAULT))
        shuffle = bool(legacy.get("shuffle", False))
        loop_mode = legacy.get("loop_mode", "off")

        guild_settings_db.upsert_settings(
            guild_id,
            music_channel_id=channel_id,
            controller_message_id=legacy.get("controller_message_id"),
            default_volume=volume,
            shuffle=shuffle,
            loop_mode=loop_mode,
        )

        self.guild_id = guild_id
        self.music_channel_id = channel_id
        self.controller_message_id = legacy.get("controller_message_id")
        self.volume = volume
        self.queue.set_shuffle(shuffle)
        self.loop_mode = loop_mode if loop_mode in ("off", "all", "one") else "off"

        with contextlib.suppress(Exception):
            os.rename(LEGACY_SETTINGS_FILE, LEGACY_SETTINGS_FILE + ".migrated")
        print(f"✅ [LOG] 레거시 bot_settings.json → DB 마이그레이션 완료 (guild_id={guild_id})")

    async def reconnect_controller_message(self):
        """봇 재시작 후 저장된 컨트롤러 메시지를 다시 연결(on_ready에서 호출)."""
        if not (self.music_channel_id and self.controller_message_id):
            return
        try:
            channel = self.bot.get_channel(self.music_channel_id)
            if not channel:
                print(f"❌ [LOG] 저장된 채널 ID({self.music_channel_id})를 찾을 수 없음")
                return
            message = await channel.fetch_message(self.controller_message_id)
            self.controller_message = message
            await message.edit(view=self.controller_view)
            self.controller_message_created_at = discord.utils.utcnow()
            self._last_render_key = None
            print(f"🖼️ [LOG] 컨트롤러 메시지 재연결 성공 (ID: {self.controller_message_id})")
        except discord.NotFound:
            print(f"❌ [LOG] 저장된 메시지 ID({self.controller_message_id}) 없음 → 새로 생성 필요")
            self.controller_message = None
        except Exception as e:
            print(f"❌ [LOG] 컨트롤러 메시지 재연결 중 오류: {e}")
            self.controller_message = None

    async def _send_temporary_message(self, channel, content: str, delay=5):
        if not channel:
            print(f"❌ [WARN] 메시지를 보낼 채널이 없음: {content}")
            return
        try:
            message = await channel.send(content)
            await message.delete(delay=delay)
        except discord.errors.NotFound:
            pass
        except Exception as e:
            print(f"❌ [ERROR] 임시 메시지 전송/삭제 실패: {e}")

    def _get_playback_info(self):
        if not self.vc or not self.current_song or not self.current_song.source or not self.start_time:
            return "00:00", ""
        total_duration_sec = int(self.current_song.source.data.get("duration", 0) or 0)
        if total_duration_sec == 0:
            return self.current_song.duration, "▶️ 라이브 스트림 또는 길이 알 수 없음"

        paused_dur = 0.0
        if self.paused_at:
            paused_dur = (discord.utils.utcnow() - self.paused_at).total_seconds()
        effective_paused = self.paused_total + paused_dur
        time_diff = (discord.utils.utcnow() - self.start_time).total_seconds() - effective_paused
        current_time_sec = max(0, int(time_diff))
        current_progress_str = YTDLSource.parse_duration(min(current_time_sec, total_duration_sec))

        progress_ratio = min(current_time_sec / total_duration_sec, 1.0)
        BAR_LENGTH = 20
        filled = int(BAR_LENGTH * progress_ratio)
        empty = BAR_LENGTH - filled
        progress_bar = "—" * filled + "🔴" + "—" * empty
        return current_progress_str, progress_bar

    def _requeue(self, song, front: bool):
        """after_playback(오디오 스레드)에서 call_soon_threadsafe로만 호출."""
        if front:
            self.queue.push_front(song)
        else:
            self.queue.push_back(song)
        self._queue_updated.set()

    def _schedule_prefetch(self):
        """현재 곡 재생 중 다음 곡 스트림 URL을 미리 받아 곡 전환 끊김을 줄인다."""
        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()
        next_song = self.queue.peek_next()
        if next_song and next_song.source is None:
            self.prefetch_task = self.bot.loop.create_task(self._prefetch_song(next_song))

    async def _prefetch_song(self, song):
        try:
            await song.load_stream_source(self.bot.loop, self.volume)
            print(f"⏳ [LOG] 다음 곡 프리페치 완료: {song.title}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"⚠️ [WARN] 프리페치 실패(재생 시점에 재시도): {e}")

    async def _progress_updater(self):
        try:
            while self.vc and self.current_song:
                await asyncio.sleep(10)
                if self.controller_message:
                    await self.controller_view.update_message(self.current_song, "🎶 현재 재생 중")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"❌ [ERROR] 진행 정보 갱신 오류: {e}")

    async def _rotate_controller_message(self, channel):
        try:
            old = self.controller_message
            embed = discord.Embed(
                title="대기열이 비었습니다.",
                description="`!play [검색어]` 또는 버튼을 사용하여 음악을 추가하세요.",
                color=discord.Color.light_grey(),
            ).set_footer(text="이 채널은 음악 전용 채널로 지정되었습니다.")
            new_msg = await channel.send(embed=embed, view=self.controller_view)
            self.controller_message = new_msg
            self.controller_message_id = new_msg.id
            self.controller_message_created_at = discord.utils.utcnow()
            self._last_render_key = None

            self.music_channel_id = self.music_channel_id or channel.id
            self._save_guild_settings()

            if old:
                with contextlib.suppress(Exception):
                    await old.delete()
            print("♻️ [LOG] 컨트롤러 메시지 rotate 완료")
        except Exception as e:
            print(f"❌ [ERROR] 컨트롤러 메시지 rotate 실패: {e}")

    async def player_loop(self):
        await self.bot.wait_until_ready()
        print("💡 [LOG] 음악 재생 루프 시작.")

        while not self.bot.is_closed():
            if self.queue.is_empty():
                self._queue_updated.clear()
                try:
                    await asyncio.wait_for(self._queue_updated.wait(), timeout=300)
                except asyncio.TimeoutError:
                    print("🛑 [LOG] 5분 대기열 비어있음 → 종료/퇴장")
                    if self.vc:
                        await self.vc.disconnect()
                    if self.controller_message:
                        await self.controller_view.update_message(None)
                    await self._clear_now_playing()
                    self.vc = None
                    self.current_song = None
                    self.player_task = None
                    return

            song = self.queue.pop_next()
            if song is None:
                continue
            self.current_song = song

            try:
                source = await song.load_stream_source(self.bot.loop, self.volume)
            except ValueError as e:
                error_msg = str(e)
                print(f"❌ [ERROR] 스트림 로딩 실패: {error_msg}")
                requester_name = song.requester.name if song.requester else "알 수 없음"
                music_channel = self.bot.get_channel(self.music_channel_id) if self.music_channel_id else None
                await self._send_temporary_message(
                    music_channel, f"❌ **{requester_name}** 님 요청 곡 로딩 실패: **{song.title}** (오류: {error_msg})", delay=10
                )
                await notify_error(self.bot, self.guild_id, f"스트림 로딩 실패: **{song.title}**\n{error_msg}")
                self.current_song = None
                continue
            except Exception as e:
                print(f"❌ [ERROR] 일반 오류: {e}")
                self.current_song = None
                continue

            if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
                self.vc.stop()
                await asyncio.sleep(0.2)

            self.start_time = discord.utils.utcnow()
            self.paused_at = None
            self.paused_total = 0.0

            if self.current_song is None:
                continue

            print(f"▶️ [LOG] 재생 시작: {self.current_song.title} (요청: {self.current_song.requester.name if self.current_song.requester else '알 수 없음'})")

            await self._record_now_playing(self.current_song)
            await self.controller_view.update_message(self.current_song, "🎶 현재 재생 중")

            self.play_next_song = asyncio.Event()

            def after_playback(error):
                if error:
                    print(f"❌ [ERROR] 재생 오류: {error}")

                if self.loop_mode == 'one' and self.current_song:
                    self.bot.loop.call_soon_threadsafe(self._requeue, self.current_song, True)
                    print(f"🔂 [LOG] 한 곡 반복: {self.current_song.title}")
                elif self.loop_mode == 'all' and self.current_song:
                    self.bot.loop.call_soon_threadsafe(self._requeue, self.current_song, False)
                    print(f"🔁 [LOG] 전체 반복: {self.current_song.title} 큐 뒤 재추가")
                else:
                    print("✅ [LOG] 곡 종료")

                self.bot.loop.call_soon_threadsafe(self.play_next_song.set)

            if not self.vc:
                print("❌ [WARN] VC 없음, 재생 불가")
                self.current_song = None
                continue

            try:
                self.vc.play(source, after=after_playback)
            except discord.ClientException as e:
                print(f"❌ [ERROR] 재생 시작 실패(음성 연결 끊김): {e}")
                self.vc = None
                self.current_song = None
                continue

            # 다음 곡을 미리 준비해서 전환 시 끊김을 줄인다.
            self._schedule_prefetch()

            if self.progress_task and not self.progress_task.done():
                self.progress_task.cancel()
            self.progress_task = self.bot.loop.create_task(self._progress_updater())

            await self.play_next_song.wait()

        if self.progress_task and not self.progress_task.done():
            self.progress_task.cancel()
        self.progress_task = None

        self.current_song = None
        self.start_time = None
        self.paused_at = None
        self.paused_total = 0.0

        if self.queue.is_empty() and self.controller_message:
            await self.controller_view.update_message(None)

    # 전용 채널이 이미 지정된 뒤부터 정상 동작하는 명령들.
    # "설정"으로 채널을 지정하기 전까지는 사용할 수 없다(전용 채널이 없다는 안내만 나감).
    _CHANNEL_GATED_COMMANDS = {"play", "플레이리스트재생", "join", "leave"}
    # 이 중 음성 채널 자동 연결이 필요한 명령.
    _VOICE_CONNECT_COMMANDS = {"play", "플레이리스트재생", "join"}

    async def _check_dedicated_channel(self, ctx) -> bool:
        """전용 채널이 지정돼 있고, 현재 채널이 그 채널인지 확인. 아니면 안내 메시지를 보내고 False."""
        if not self.music_channel_id:
            await self._send_temporary_message(
                ctx.channel, "⚠️ 아직 전용 채널이 지정되지 않았습니다. `/설정`으로 이 채널을 먼저 지정해주세요.", delay=6
            )
            return False
        if ctx.channel.id != self.music_channel_id:
            await self._send_temporary_message(
                ctx.channel, f"⚠️ 음악 명령은 전용 채널(<#{self.music_channel_id}>)에서 사용해주세요.", delay=5
            )
            return False
        return True

    async def _ensure_voice_connected(self, channel, author, guild) -> bool:
        """음성 채널 연결을 보장한다. 실패/미접속이면 안내 메시지를 보내고 False를 반환."""
        voice_client = guild.voice_client if guild else None
        if voice_client and not voice_client.is_connected():
            # 좀비 연결(끊겼는데 객체만 남아있는 상태) 정리 후 재연결
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)
            voice_client = None

        if not voice_client:
            if author.voice:
                try:
                    self.vc = await author.voice.channel.connect()
                    print(f"🔊 [LOG] {author.name} 요청으로 음성 채널 연결: {author.voice.channel.name}")
                except Exception as e:
                    print(f"❌ [ERROR] 음성 채널 연결 실패: {e}")
                    await self._send_temporary_message(channel, f"❌ 음성 채널 연결 실패: {e}", delay=6)
                    await notify_error(self.bot, guild.id if guild else None, f"음성 채널 연결 실패: {e}")
                    return False
            else:
                await self._send_temporary_message(channel, "⚠️ 음성 채널에 먼저 접속해주세요.", delay=5)
                return False
        else:
            self.vc = voice_client
        return True

    async def cog_before_invoke(self, ctx):
        # 슬래시(인터랙션)로 호출된 경우 3초 안에 반드시 응답해야 "애플리케이션이 응답하지 않음" 오류가
        # 뜨지 않는다. 실제 답장은 기존처럼 채널 메시지(_send_temporary_message)로 보내므로,
        # 여기서는 조용히 defer한 뒤 빈 응답을 바로 지워서 "생각 중..." 표시가 남지 않게 한다.
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer(ephemeral=True)
            with contextlib.suppress(Exception):
                await ctx.interaction.delete_original_response()

        try:
            await ctx.message.delete()
        except Exception:
            pass

        if ctx.guild:
            self.guild_id = ctx.guild.id

        if ctx.command.name in self._CHANNEL_GATED_COMMANDS:
            if not await self._check_dedicated_channel(ctx):
                raise HandledCommandError("Not in dedicated channel.")

        if ctx.command.name in self._VOICE_CONNECT_COMMANDS:
            if not await self._ensure_voice_connected(ctx.channel, ctx.author, ctx.guild):
                raise HandledCommandError("Voice connect failed.")

    @commands.Cog.listener("on_message")
    async def on_message_auto_play(self, message: discord.Message):
        """전용 채널에서는 `!play` 없이 아무 텍스트나 입력해도 검색해서 대기열에 추가한다."""
        if message.author.bot or not message.guild:
            return
        if not self.music_channel_id or message.channel.id != self.music_channel_id:
            return

        content = message.content.strip()
        if not content or content.startswith(self.bot.command_prefix):
            return  # 빈 메시지(첨부파일만 등)나 실제 명령어는 기존 명령 처리에 맡긴다.

        self.guild_id = message.guild.id

        with contextlib.suppress(Exception):
            await message.delete()

        if not await self._ensure_voice_connected(message.channel, message.author, message.guild):
            return

        await self._enqueue_search(message, content)

    # -------------------------
    # 명령어
    # -------------------------
    async def _enqueue_search(self, ctx, search: str):
        """검색어/URL을 대기열에 추가. !play와 플레이리스트 북마크 재생, 전용 채널 자동 재생이 공유하는 로직.
        전용 채널/음성 연결 확인은 호출하는 쪽(cog_before_invoke 또는 on_message)에서 이미 끝낸 상태로 호출된다."""
        try:
            if not self.vc:
                return await self._send_temporary_message(ctx.channel, "⚠️ 봇이 음성 채널에 연결되지 않았습니다. `/입장` 먼저.", delay=5)

            await self._send_temporary_message(ctx.channel, f"🔍 **{search}** 검색 중...", delay=5)
            result = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, volume=self.volume)

            if isinstance(result, list):
                songs = [Song(source=None, requester=ctx.author, initial_data=item_data) for item_data in result]
                self.queue.add_many(songs)
                self._queue_updated.set()
                await self._send_temporary_message(ctx.channel, f"✅ 플레이리스트 **{len(songs)}곡** 추가", delay=7)
            elif isinstance(result, YTDLSource):
                song = Song(source=result, requester=ctx.author)
                self.queue.add(song)
                self._queue_updated.set()
                await self._send_temporary_message(ctx.channel, f"✅ **{song.title}** 대기열 추가", delay=7)
            else:
                return await self._send_temporary_message(ctx.channel, "❌ 유효한 검색 결과가 없습니다.", delay=5)

            if not self.vc.is_playing() and not self.vc.is_paused():
                if not self.player_task or self.player_task.done():
                    self.player_task = self.bot.loop.create_task(self.player_loop())

            await self.controller_view.update_message(self.current_song)

        except ValueError as e:
            await self._send_temporary_message(ctx.channel, f"❌ 오류: {e}", delay=10)
        except Exception as e:
            await self._send_temporary_message(ctx.channel, f"❌ 알 수 없는 오류: {e}", delay=10)
            print(f"❌ [ERROR] 대기열 추가 처리 오류: {e}")

    @commands.hybrid_command(name="play", aliases=["실행", "p"], description="검색어/유튜브 링크를 대기열에 추가합니다.")
    async def play_(self, ctx, *, search: str):
        await self._enqueue_search(ctx, search)

    @commands.hybrid_command(name="플레이리스트추가", aliases=["pladd", "playlist_add"], description="유튜브 링크를 이름으로 저장합니다.")
    async def playlist_add_(self, ctx, name: str, url: str):
        if not (url.startswith("http://") or url.startswith("https://")):
            return await self._send_temporary_message(
                ctx.channel, "❌ URL 형식이 아닙니다. 예) `!플레이리스트추가 즐겨듣기 https://youtube.com/playlist?list=...`", delay=8
            )
        await playlist_db.add_playlist(
            self.bot.loop, ctx.guild.id, name, url, str(ctx.author), discord.utils.utcnow().isoformat()
        )
        await self._send_temporary_message(ctx.channel, f"✅ 플레이리스트 **{name}** 저장 완료", delay=6)

    @commands.hybrid_command(
        name="플레이리스트재생",
        aliases=["plplay", "playlist_play"],
        description="저장된 플레이리스트를 대기열에 추가합니다. option에 '셔플' 또는 '반복'을 붙이면 즉시 적용됩니다.",
    )
    async def playlist_play_(self, ctx, name: str, option: str = None):
        url = await playlist_db.get_playlist(self.bot.loop, ctx.guild.id, name)
        if not url:
            return await self._send_temporary_message(
                ctx.channel, f"❌ **{name}** 플레이리스트를 찾을 수 없습니다. `!플레이리스트목록`으로 확인해주세요.", delay=6
            )
        await self._enqueue_search(ctx, url)

        if option == "셔플":
            self.queue.set_shuffle(True)
            self._save_guild_settings()
            await self._send_temporary_message(ctx.channel, "🔀 대기열 셔플 **ON**", delay=3)
        elif option == "반복":
            self.loop_mode = "all"
            self._save_guild_settings()
            await self._send_temporary_message(ctx.channel, "🔁 전체 반복 **ON**", delay=3)

        if option in ("셔플", "반복") and self.controller_message:
            self._last_render_key = None
            await self.controller_view.update_message(self.current_song)

    @commands.hybrid_command(name="플레이리스트목록", aliases=["pllist", "playlist_list"], description="저장된 플레이리스트 목록을 확인합니다.")
    async def playlist_list_(self, ctx):
        rows = await playlist_db.list_playlists(self.bot.loop, ctx.guild.id)
        if not rows:
            return await self._send_temporary_message(ctx.channel, "⚠️ 저장된 플레이리스트가 없습니다.", delay=6)

        lines = [f"`{name}` — [링크]({url}) (등록: {added_by})" for name, url, added_by in rows]
        embed = discord.Embed(
            title="📂 저장된 플레이리스트", description="\n".join(lines), color=discord.Color.gold()
        )
        await ctx.send(embed=embed, delete_after=30)

    @commands.hybrid_command(name="플레이리스트삭제", aliases=["pldel", "playlist_delete"], description="저장된 플레이리스트를 삭제합니다.")
    async def playlist_delete_(self, ctx, name: str):
        deleted = await playlist_db.delete_playlist(self.bot.loop, ctx.guild.id, name)
        if deleted:
            await self._send_temporary_message(ctx.channel, f"🗑️ **{name}** 삭제 완료", delay=6)
        else:
            await self._send_temporary_message(ctx.channel, f"❌ **{name}** 플레이리스트를 찾을 수 없습니다.", delay=6)

    @commands.hybrid_command(name="join", aliases=["입장"], description="전용 채널에서 봇을 음성 채널에 연결합니다 (전용 채널 지정은 /설정).")
    async def join_(self, ctx):
        # 전용 채널 확인, 음성 채널 연결은 cog_before_invoke에서 이미 처리됨.
        if self.controller_message is None:
            # 컨트롤러 메시지가 유실된 경우(수동 삭제 등) 안내만 하고, 복구는 /설정으로.
            await self._send_temporary_message(
                ctx.channel, "⚠️ 컨트롤러 메시지가 없습니다. `/설정`을 다시 실행해 복구해주세요.", delay=6
            )
        voice_channel_name = ctx.author.voice.channel.name if ctx.author.voice else "음성 채널"
        await self._send_temporary_message(ctx.channel, f"✅ **{voice_channel_name}**에 연결했습니다.", delay=5)

    @commands.hybrid_command(name="leave", aliases=["퇴장", "stop"], description="전용 채널에서 대기열을 비우고 음성 채널에서 퇴장합니다.")
    async def leave_(self, ctx):
        if self.vc:
            self.queue.clear()
            self.vc.stop()
            await self.vc.disconnect()
            self.vc = None
            self.current_song = None

            if self.player_task:
                self.player_task.cancel()
                self.player_task = None

            if self.progress_task and not self.progress_task.done():
                self.progress_task.cancel()
            self.progress_task = None

            if self.prefetch_task and not self.prefetch_task.done():
                self.prefetch_task.cancel()
            self.prefetch_task = None

            if self.controller_message:
                await self.controller_view.update_message(None)
            await self._clear_now_playing()

            await self._send_temporary_message(ctx.channel, "✅ 음성 채널에서 퇴장합니다.", delay=5)
        else:
            await self._send_temporary_message(ctx.channel, "⚠️ 현재 음성 채널에 연결되어 있지 않습니다.", delay=5)

    @commands.hybrid_command(name="설정", aliases=["set_music_channel", "setup"], description="현재 채널을 음악 전용 채널로 지정합니다.")
    async def set_music_channel(self, ctx):
        music_channel = ctx.channel
        self.music_channel_id = music_channel.id

        embed_to_send = discord.Embed(
            title="대기열이 비었습니다.",
            description="`!play [검색어]` 또는 버튼을 사용하여 음악을 추가하세요.",
            color=discord.Color.light_grey(),
        ).set_footer(text="이 채널은 음악 전용 채널로 지정되었습니다.")

        message_to_edit = None
        if self.controller_message_id:
            try:
                if self.controller_message and self.controller_message.channel.id == self.music_channel_id:
                    message_to_edit = self.controller_message
                else:
                    message_to_edit = await music_channel.fetch_message(self.controller_message_id)
                    if message_to_edit.channel.id != self.music_channel_id:
                        await message_to_edit.delete()
                        message_to_edit = None
            except (discord.NotFound, discord.HTTPException):
                message_to_edit = None
                self.controller_message = None

        if message_to_edit:
            self.controller_message = message_to_edit
            await self.controller_message.edit(embed=embed_to_send, view=self.controller_view)
            print("🖼️ [LOG] 기존 컨트롤러 메시지 업데이트")
        else:
            self.controller_message = await music_channel.send(embed=embed_to_send, view=self.controller_view)
            print("🖼️ [LOG] 새 컨트롤러 메시지 생성")

        self.controller_message_created_at = discord.utils.utcnow()
        self._last_render_key = None

        self.controller_message_id = self.controller_message.id
        self._save_guild_settings()

        await self._send_temporary_message(
            ctx.channel, f"✅ 이 채널(<#{self.music_channel_id}>)을 음악 전용 채널로 설정했습니다.", delay=10
        )

    @commands.hybrid_command(name="volume", aliases=["vol", "볼륨"], description="볼륨을 조회하거나 설정합니다. 예: 30, +10, -20")
    async def volume_(self, ctx, value: str = None):
        """
        !vol           -> 현재 볼륨 표시
        !vol 35        -> 35%로 설정
        !vol +10       -> 현재값에서 +10%
        !vol -20       -> 현재값에서 -20%
        """
        if value is None:
            return await self._send_temporary_message(ctx.channel, f"🔊 현재 볼륨: **{int(self.volume*100)}%**", delay=5)

        cur = int(round(self.volume * 100))
        if value.startswith(("+", "-")):
            try:
                delta = int(value)
            except ValueError:
                return await self._send_temporary_message(ctx.channel, "❌ 잘못된 값. 예) `!vol 30`, `!vol +10`, `!vol -20`", delay=6)
            new_percent = cur + delta
        else:
            try:
                new_percent = int(value)
            except ValueError:
                return await self._send_temporary_message(ctx.channel, "❌ 숫자를 입력하세요. 예) `!vol 30`", delay=6)

        new_percent = max(1, min(200, new_percent))
        self.volume = new_percent / 100.0

        if self.current_song and self.current_song.source:
            self.current_song.source.volume = self.volume

        self._save_guild_settings()

        await self._send_temporary_message(ctx.channel, f"🔊 볼륨을 **{new_percent}%**로 설정했습니다.", delay=5)

        if self.controller_message and self.current_song:
            await self.controller_view.update_message(self.current_song, "🎶 현재 재생 중")


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
