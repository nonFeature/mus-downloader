<div align="center"><b>[EN]</b> <a href="ru.md">[RU]</a></div>

<h1>mus-downloader <img align="right" height="40" alt="mus-downloader" src="icon.webp"></h1>

started out as a **cli tool** that was supposed to be the core for other projects, but now it already has a **tg bot**

<div align="center"><h1>what's under the hood?</h1></div>

### for metadata:
1. **song.link/Odesli** - track links across other platforms
2. **Deezer API** - ISRC, track number, year, cover art, and explicit tag
3. **Spotify** - fallback
4. **MusicBrainz** - another fallback
5. **SoundCloud** - for SoundCloud and SoundCloud only
6. **Last.fm** - genres, yep

### for downloading:
1. **Soulseek** - embedded directly in the app via `aioslsk`
2. **Deezer** - direct stream downloads
3. **SoundCloud** - grabs original stream via `yt-dlp`
4. **YouTube Music / YouTube** - fallback matcher using `ytmusicapi` (checks title and duration), then `yt-dlp`
5. **slskd** - optional fallback to an external slskd daemon if you already have one running

### other stuff:
- **mutagen** - writes ID3v2.4 (MP3) and Vorbis comments (FLAC), embeds artwork, year, track number, and explicit tags
- **ffmpeg** - transcodes FLAC to honest 320 kbps CBR MP3 when you ask for MP3 but Soulseek only has FLAC

<div align="center"><h1>how to use?</h1></div>

1. **cli (`cli/main.py`)** - basic terminal runner: pass a link or search query, pick `--flac` or `--mp3`, set output folder
2. **tg bot (`bot/bot.py`)**:
    - built on aiogram 3
    - inline metadata card with quality buttons (FLAC / MP3)
    - whitelist by user ID (`ALLOWED_USERS`)
    - manages local `telegram-bot-api` (via docker or local binary) to bypass Telegram's 50MB limit and send files up to 2GB
    - single-instance file lock so two bot processes never fight over polling

<div align="center"><h1>how to run?</h1></div>

### 0. system requirements
- **Python 3.10+** (uv installs it automatically if missing)
- **ffmpeg** in `PATH` - transcoding FLAC to MP3 and building thumbnails
- **JavaScript runtime** for YouTube: **Deno** (recommended), Node.js, Bun or QuickJS.
  Modern `yt-dlp` needs one for full YouTube support; the project auto-detects what is installed
  and ships the `yt-dlp-ejs` challenge solver
- **Docker** or a `telegram-bot-api` binary - optional, only for uploading files over 50 MB via a local Bot API server

### 1. setup
```bash
# clone and enter
git clone https://github.com/nonFeature/mus-downloader.git
cd mus-downloader

# install dependencies with uv
uv sync
```

### 2. configure `.env`
copy `.env.example` to `.env` and fill in:
- `SLSK_USER` and `SLSK_PASS` - your soulseek account (free, just create one)
- `BOT_TOKEN` - from @BotFather (if using telegram bot)
- `ALLOWED_USERS` - comma-separated telegram user IDs
- `LOCAL_BOT_API=true` + `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` - optional, for sending files over 50MB up to 2GB

### 3. run

**quick commands (installed by `uv sync`):**
```bash
uv run dl "https://open.spotify.com/track/..." --flac   # cli
uv run bot                                              # telegram bot
```

**cli:**
```bash
uv run python -m cli.main "https://open.spotify.com/track/..." --flac
```
or
```bash
uv run python -m cli.main "vince staples big fish" --mp3
```

**telegram bot:**
```bash
uv run python -m bot
```
