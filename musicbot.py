# this is my discord music bot with support links (including spotify) or just general search terms
# current cmds: /play, /skip, /leave, /queue

# obviously im not able to publish these to github but there is also a .env file with the following vars:
# DISCORD_TOKEN, FFMPEG_PATH, GUILD_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, NODE_PATH, DENO_PATH

# there is also a cookies.txt file for yt-dlp

# imports
import os
import asyncio
import logging
import html
import re
import datetime
from typing import Optional, List, Dict

from dotenv import load_dotenv
import discord
from discord import app_commands
import yt_dlp as youtube_dl

# spotify init
SPOTIFY_AVAILABLE = True
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    SPOTIFY_AVAILABLE = False

# setup
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", r"C:\ffmpeg\bin\ffmpeg.exe")
GUILD_ID = os.getenv("GUILD_ID")  # guild-only sync to avoid global Entry Point sync errors

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID") or os.getenv("SPOTIPY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET") or os.getenv("SPOTIPY_CLIENT_SECRET")

NODE_PATH = os.getenv("NODE_PATH", "node")
DENO_PATH = os.getenv("DENO_PATH", "deno")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# spotify client
sp = None
if SPOTIFY_AVAILABLE and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            )
        )
        log.info("Spotify support enabled.")
    except Exception as e:
        log.warning("Spotify init failed: %s", e)

# queue
QueueItem = Dict[str, str]  # {'url', 'title', 'video_id'}
music_queue: List[QueueItem] = []

# yt-dlp & ffmpeg options
YTDLP_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch1",

    "js_runtimes": {
        "node": {"path": NODE_PATH},
        "deno": {"path": DENO_PATH},
    },
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_delay_max 5",
    "options": "-vn",
    "executable": FFMPEG_PATH,
}

SPOTIFY_TRACK_RE = r"^https?://open\.spotify\.com/track/([A-Za-z0-9]+)"

# funcs

# for printing embeds
def music_embed(title: str, description: str, *, color=discord.Color.blurple()) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return e

# grabs voice client
def get_vc(guild: discord.Guild) -> Optional[discord.VoiceClient]:
    return guild.voice_client if guild else None

# ensure bot is in voice channel
async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    if not interaction.guild:
        return None

    vc = get_vc(interaction.guild)
    if vc and vc.is_connected():
        return vc

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send(
            embed=music_embed(
                "Join a Voice Channel",
                "You need to be in a voice channel first.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return None

    return await interaction.user.voice.channel.connect()

# convert spotify url to yt query
def spotify_to_query(url: str) -> Optional[str]:
    """Spotify track URL -> 'Song Title Artist1, Artist2'"""
    if not sp:
        return None

    m = re.match(SPOTIFY_TRACK_RE, url)
    if not m:
        return None

    track = sp.track(m.group(1))
    name = track.get("name", "")
    artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
    q = f"{name} {artists}".strip()
    return q if q else None

# search yt for first result url
def yt_search(query: str) -> str:
    """Returns the YouTube webpage_url for the first search result."""
    with youtube_dl.YoutubeDL(YTDLP_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    return info["entries"][0]["webpage_url"]

# extract audio stream url and info
def extract_audio(query_or_url: str) -> Dict[str, str]:
    """
    Accepts:
      - YouTube URL
      - search phrase (yt-dlp default_search handles it)
    Returns:
      {'url': direct_audio_url, 'title': title, 'video_id': id}
    """
    with youtube_dl.YoutubeDL(YTDLP_OPTS) as ydl:
        info = ydl.extract_info(query_or_url, download=False)

    if "entries" in info:
        info = info["entries"][0]

    title = html.unescape(info.get("title", "Unknown Title"))
    video_id = info.get("id")

    fmts = info.get("formats", []) or []
    audio_url = None

    # prefer audio only formats
    for f in fmts:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and f.get("url"):
            audio_url = f["url"]
            break

    if not audio_url:
        audio_url = info.get("url")

    if not audio_url:
        raise RuntimeError("No playable audio stream found.")

    return {"url": audio_url, "title": title, "video_id": video_id}

# play next track in queue
async def play_next(guild: discord.Guild):
    vc = get_vc(guild)
    if not vc or not vc.is_connected() or vc.is_playing():
        return
    if not music_queue:
        return

    item = music_queue.pop(0)
    source = discord.FFmpegOpusAudio(item["url"], **FFMPEG_OPTS)

    def after_play(err):
        if err:
            log.warning("Playback error: %s", err)
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    vc.play(source, after=after_play)


# cmds

# plays a track from search or link
@tree.command(name="play", description="Play a YouTube search/link or a Spotify track link.")
@app_commands.describe(query="YouTube URL/search, or Spotify track URL")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    vc = await ensure_voice(interaction)
    if not vc:
        return

    try:
        if re.match(SPOTIFY_TRACK_RE, query):
            yt_query = spotify_to_query(query)
            if not yt_query:
                raise RuntimeError("Spotify support not available (missing spotipy or credentials).")
            yt_url = yt_search(yt_query)
            track = extract_audio(yt_url)
        else:
            track = extract_audio(query)

    except Exception as e:
        await interaction.followup.send(
            embed=music_embed("Playback Error", str(e), color=discord.Color.red()),
            ephemeral=True,
        )
        return

    # if playing then queue
    if vc.is_playing() or vc.is_paused():
        music_queue.append(track)
        await interaction.followup.send(
            embed=music_embed("Queued", f"**{track['title']}**")
        )
        return

    # play
    source = discord.FFmpegOpusAudio(track["url"], **FFMPEG_OPTS)

    def after_play(err):
        if err:
            log.warning("Playback error: %s", err)
        asyncio.run_coroutine_threadsafe(play_next(interaction.guild), bot.loop)

    vc.play(source, after=after_play)

    embed = music_embed("Now Playing", f"**{track['title']}**")
    if track.get("video_id"):
        embed.set_thumbnail(url=f"https://img.youtube.com/vi/{track['video_id']}/hqdefault.jpg")

    await interaction.followup.send(embed=embed)

# skips current track
@tree.command(name="skip", description="Skip the current track.")
async def skip(interaction: discord.Interaction):
    vc = get_vc(interaction.guild)
    if not vc or not vc.is_connected() or not vc.is_playing():
        await interaction.response.send_message(
            embed=music_embed(
                "Nothing Playing",
                "There is nothing to skip.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    vc.stop()
    await interaction.response.send_message(
        embed=music_embed("Skipped", "Playing the next track.")
    )

# leaves voice channel and clears queue
@tree.command(name="leave", description="Disconnect and clear the queue.")
async def leave(interaction: discord.Interaction):
    vc = get_vc(interaction.guild)
    if not vc or not vc.is_connected():
        await interaction.response.send_message(
            embed=music_embed(
                "Not Connected",
                "I am not in a voice channel.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    music_queue.clear()
    await vc.disconnect()
    await interaction.response.send_message(
        embed=music_embed("Disconnected", "Cleared the queue and left the channel.")
    )

# shows the queue
@tree.command(name="queue", description="Show the music queue.")
async def queue_cmd(interaction: discord.Interaction):
    if not music_queue:
        await interaction.response.send_message(
            embed=music_embed("Queue", "The queue is empty.")
        )
        return

    lines = "\n".join(f"**{i+1}.** {item['title']}" for i, item in enumerate(music_queue))
    await interaction.response.send_message(
        embed=music_embed("Queue", lines)
    )


# get bot ready
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    if GUILD_ID:
        try:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            log.info("Commands synced to guild %s.", GUILD_ID)
        except Exception:
            log.exception("Command sync failed")
    else:
        log.warning("GUILD_ID not set; skipping command sync.")


# main
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN not set in your environment/.env")
    bot.run(DISCORD_TOKEN)
