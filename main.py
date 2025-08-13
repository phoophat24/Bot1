# main.py
# Discord Music Bot (single-file, production-ready)
# Features: Slash commands, queue, pause/resume/skip/stop, nowplaying,
# volume, loop(off/one/all), auto-disconnect, "music-room" text trigger,
# Flask keep-alive server for hosting.

from __future__ import annotations

import os
import asyncio
from typing import Optional, Dict, Literal

import discord
from discord.ext import commands
from discord import app_commands

from myserver import server_on

import yt_dlp

# --------- Keep-alive server (optional but useful on Render/Replit) ----------
from threading import Thread
from flask import Flask

_keep_app = Flask(__name__)

@_keep_app.route("/")
def _home():
    return "OK - Discord Music Bot is running."

def keep_alive(port: int = 8080):
    def _run():
        _keep_app.run(host="0.0.0.0", port=port)
    Thread(target=_run, daemon=True).start()

# ----------------------------- Config ----------------------------------------
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")            # ต้องมี
GUILD_ID_ENV = os.getenv("GUILD_ID")          # ไม่ใส่ก็ได้ (global sync ช้าเล็กน้อย)
GUILD_ID: int | None = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None

MUSIC_CHANNEL_NAME = os.getenv("MUSIC_CHANNEL_NAME", "music-room")  # ห้องข้อความรับชื่อเพลง/ลิงก์
AUTO_DC_IDLE_SECONDS = int(os.getenv("AUTO_DC_IDLE_SECONDS", "180")) # ว่างนานแล้วค่อยตัดสาย
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "0.6"))           # 0.0–2.0 (0–200%)
MAX_VOLUME = float(os.getenv("MAX_VOLUME", "2.0"))

# ---------------------------- yt-dlp / FFmpeg --------------------------------
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS   = "-vn"

# ------------------------------ Song Model -----------------------------------
class Song:
    def __init__(self, title: str, url: str, stream_url: str,
                 thumbnail: Optional[str] = None, requested_by: Optional[str] = None):
        self.title = title
        self.url = url
        self.stream_url = stream_url
        self.thumbnail = thumbnail
        self.requested_by = requested_by

async def search_song(query: str, requested_by: Optional[str] = None) -> Song:
    """ค้นหาเพลงจาก 'ชื่อ' หรือ 'URL' โดยไม่บล็อค event loop (รันใน thread)."""
    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            title = info.get("title", "Unknown")
            url = info.get("webpage_url") or query
            stream_url = info.get("url")
            thumb = info.get("thumbnail")
            return Song(title, url, stream_url, thumb, requested_by)
    return await asyncio.to_thread(_extract)

# ----------------------------- Music Player ----------------------------------
LoopMode = Literal["off", "one", "all"]

class MusicPlayer:
    """ตัวเล่นเพลงต่อกิลด์: จัดคิว, loop, volume, auto disconnect"""
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: asyncio.Queue[Song] = asyncio.Queue()
        self.current: Optional[Song] = None
        self.volume: float = DEFAULT_VOLUME
        self.loop_mode: LoopMode = "off"
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

    def start(self):
        if not self._task or self._task.done():
            self._stop_evt.clear()
            self._task = asyncio.create_task(self._player_loop())

    async def stop(self):
        self._stop_evt.set()
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
        # drain queue
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except:  # noqa: E722
                break

    async def _player_loop(self):
        while not self._stop_evt.is_set():
            try:
                # รอเพลงถัดไปจากคิว (ถ้าเกินเวลา → ตัดสาย/ออก)
                try:
                    song: Song = await asyncio.wait_for(self.queue.get(), timeout=AUTO_DC_IDLE_SECONDS)
                except asyncio.TimeoutError:
                    vc = self.guild.voice_client
                    if vc and vc.is_connected():
                        await vc.disconnect(force=True)
                    break

                self.current = song
                vc = self.guild.voice_client
                if not vc or not vc.is_connected():
                    self.current = None
                    continue

                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        song.stream_url,
                        before_options=FFMPEG_BEFORE,
                        options=FFMPEG_OPTS
                    ),
                    volume=self.volume
                )

                done = asyncio.Event()
                def _after(err: Exception | None):
                    done.set()

                vc.play(source, after=_after)

                # ส่ง now playing
                text_ch = self.guild.system_channel or discord.utils.get(
                    self.guild.text_channels, name=MUSIC_CHANNEL_NAME
                )
                if text_ch:
                    embed = discord.Embed(
                        title="🎵 Now Playing",
                        description=f"[{song.title}]({song.url})",
                        color=0x1DB954
                    )
                    if song.thumbnail:
                        embed.set_thumbnail(url=song.thumbnail)
                    if song.requested_by:
                        embed.set_footer(text=f"Requested by {song.requested_by}")
                    try:
                        await text_ch.send(embed=embed)
                    except discord.Forbidden:
                        pass

                await done.wait()

                # จัดการ loop
                if self.loop_mode == "one":
                    await self.queue.put(song)   # เล่นซ้ำเพลงเดิม
                elif self.loop_mode == "all":
                    await self.queue.put(song)   # วนคิวทั้งกอง
                else:
                    self.current = None

            except Exception as e:  # ป้องกันลูปแตก
                text_ch = self.guild.system_channel or discord.utils.get(
                    self.guild.text_channels, name=MUSIC_CHANNEL_NAME
                )
                if text_ch:
                    try:
                        await text_ch.send(f"⚠️ เกิดข้อผิดพลาดระหว่างเล่นเพลง: `{e}`")
                    except:
                        pass

        # ออกจากช่องเสียงเมื่อจบลูป
        vc = self.guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect(force=True)

# ------------------------------- Cog -----------------------------------------
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, MusicPlayer] = {}

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = MusicPlayer(self.bot, guild)
            self.players[guild.id] = player
        return player

    async def ensure_voice(self, interaction: Optional[discord.Interaction] = None,
                           message: Optional[discord.Message] = None,
                           connect_to: Optional[discord.VoiceChannel] = None) -> Optional[discord.VoiceClient]:
        """ให้บอทเข้าช่องเสียงเดียวกับผู้ใช้ ถ้ายังไม่อยู่"""
        guild = (interaction.guild if interaction else message.guild)
        user = (interaction.user if interaction else message.author)

        if not isinstance(user, discord.Member):
            return None

        vc = guild.voice_client
        if vc and vc.channel:
            return vc

        target = connect_to or (user.voice.channel if user.voice else None)
        if not target:
            if interaction:
                await interaction.response.send_message("❌ เข้าช่องเสียงก่อนนะ", ephemeral=True)
            else:
                await message.channel.send("❌ เข้าช่องเสียงก่อนนะ")
            return None

        try:
            return await target.connect()
        except discord.Forbidden:
            if interaction:
                await interaction.response.send_message("❌ บอทไม่มีสิทธิ์เข้าช่องเสียงนั้น", ephemeral=True)
            else:
                await message.channel.send("❌ บอทไม่มีสิทธิ์เข้าช่องเสียงนั้น")
            return None

    # -------- Slash Commands --------
    @app_commands.command(name="play", description="เล่นเพลงจากชื่อเพลงหรือ URL")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        vc = await self.ensure_voice(interaction=interaction)
        if not vc:
            return
        player = self.get_player(interaction.guild)
        song = await search_song(query, requested_by=interaction.user.display_name)
        await player.queue.put(song)
        player.start()
        await interaction.followup.send(f"➕ เพิ่มในคิว: **{song.title}**")

    @app_commands.command(name="skip", description="ข้ามเพลง")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭ ข้ามเพลงแล้ว")
        else:
            await interaction.response.send_message("❌ ไม่มีเพลงที่กำลังเล่น", ephemeral=True)

    @app_commands.command(name="pause", description="หยุดชั่วคราว")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ หยุดชั่วคราวแล้ว")
        else:
            await interaction.response.send_message("❌ ไม่มีเพลงที่กำลังเล่น", ephemeral=True)

    @app_commands.command(name="resume", description="เล่นต่อ")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ เล่นต่อแล้ว")
        else:
            await interaction.response.send_message("❌ เพลงไม่ได้หยุดอยู่", ephemeral=True)

    @app_commands.command(name="stop", description="หยุดและล้างคิว")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        await player.stop()
        await interaction.response.send_message("⏹️ หยุดและล้างคิวแล้ว")

    @app_commands.command(name="queue", description="ดูคิวเพลง")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        items = list(player.queue._queue)  # แสดง snapshot คิวปัจจุบัน
        if not items:
            await interaction.response.send_message("📭 คิวว่าง")
            return
        desc = "\n".join(f"{i+1}. {s.title}" for i, s in enumerate(items))
        embed = discord.Embed(title="🎶 คิวเพลง", description=desc, color=0x5865F2)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="ตอนนี้กำลังเล่นอะไร")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        s = player.current
        if not s:
            await interaction.response.send_message("⏹️ ยังไม่มีเพลงกำลังเล่น")
            return
        embed = discord.Embed(title="🎵 Now Playing", description=f"[{s.title}]({s.url})", color=0x1DB954)
        if s.thumbnail:
            embed.set_thumbnail(url=s.thumbnail)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="ปรับเสียง 0–200% (เริ่มต้น 60%)")
    @app_commands.describe(percent="เปอร์เซ็นต์เสียง เช่น 100")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
        player = self.get_player(interaction.guild)
        new_vol = max(0.0, min(MAX_VOLUME, percent / 100.0))
        player.volume = new_vol
        await interaction.response.send_message(f"🔊 ตั้งเสียงเป็น {percent}% (มีผลกับเพลงใหม่ทันที)")

    @app_commands.command(name="loop", description="โหมดเล่นซ้ำ: off / one / all")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="one", value="one"),
    app_commands.Choice(name="all", value="all")
])
async def loop(self, interaction: discord.Interaction, mode: str):
    player = self.get_player(interaction.guild)
    player.loop_mode = mode
    await interaction.response.send_message(f"🔁 ตั้ง loop: **{mode}**")

    @app_commands.command(name="leave", description="ให้ออกจากช่องเสียง")
    async def leave(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        await player.stop()
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
        await interaction.response.send_message("👋 ออกจากช่องเสียงแล้ว")

    # -------- Text-channel trigger ("music-room") --------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.channel.name != MUSIC_CHANNEL_NAME:
            return
        content = message.content.strip()
        if not content or content.startswith(("/", "!", ".")):
            return  # ข้ามกรณีเป็นคำสั่งอื่น

        # ให้บอทเข้าช่องเสียงเดียวกับผู้พิมพ์
        vc = await self.ensure_voice(message=message)
        if not vc:
            return

        guild = message.guild
        player = self.get_player(guild)

        try:
            song = await search_song(content, requested_by=message.author.display_name)
        except Exception as e:
            await message.channel.send(f"❌ หาเพลงไม่สำเร็จ: `{e}`")
            return

        await player.queue.put(song)
        player.start()
        await message.channel.send(f"🎶 เพิ่มในคิว: **{song.title}**")

# ------------------------------- Bot setup -----------------------------------
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # เปิดใน Dev Portal ด้วย
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(Music(self))

        # Sync slash commands (เร็วขึ้นมากถ้าระบุ GUILD_ID)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} global commands (อาจหน่วงเล็กน้อยกว่าจะขึ้น)")

server_on

bot.run(os.getenv('TOKEN'))  # เริ่มบอทด้วย Token ที่ตั้งไว้ใน .env



