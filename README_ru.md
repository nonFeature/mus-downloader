<div align="center"><a href="README.md">[EN]</a> <b>[RU]</b></div>

<h1>mus-downloader <img align="right" height="40" alt="mus-downloader" src="icon.webp"></h1>

изначально задумывалось как **cli-утилита**, которая должна была стать ядром для других штук, но у нее уже появился **tg-бот**

<div align="center"><h1>что под капотом?</h1></div>

### для метаданных:
1. **song.link/Odesli** - поиск трека на других площадках
2. **Deezer API** - ISRC, номер трека, год, обложка и explicit-метка
3. **Spotify** - резерв
4. **MusicBrainz** - еще один резерв
5. **SoundCloud** - для саундклауда и только для саундклауда
6. **Last.fm** - жанры, ага

### для скачивания:
1. **Soulseek** - встроен прямо в приложение через `aioslsk`
2. **Deezer** - скачивание веб-стрима напрямую
3. **SoundCloud** - вытягивание оригинального стрима через `yt-dlp`
4. **YouTube Music / YouTube** - фолбек через `ytmusicapi` со сверкой по длительности и названию, дальше `yt-dlp`
5. **slskd** - опциональный фолбек на внешний slskd, если уже поднят

### остальное:
- **mutagen** - прописывает ID3v2.4 (MP3) и Vorbis comments (FLAC), вшивает обложку, год, номер трека и explicit-метку
- **ffmpeg** - жмет FLAC в честный MP3 320 kbps CBR, если просили MP3, а в Soulseek нашелся только FLAC

<div align="center"><h1>как пользоваться?</h1></div>

1. **cli (`main.py`)** - простой запуск из терминала: скармливаешь ссылку или поисковый запрос, выбираешь `--flac` или `--mp3`, указываешь папку
2. **tg bot (`bot.py`)**:
    - написан на aiogram 3
    - инлайн-карточка трека с кнопками выбора качества (FLAC / MP3)
    - белый список по user ID (`ALLOWED_USERS`)
    - сам рулит локальным `telegram-bot-api` (через docker или бинарник), чтобы обойти лимит Telegram в 50 МБ и отправлять файлы до 2 ГБ
    - блокировка процесса (`SingleInstanceLock`), чтобы два бота не конфликтовали за получение апдейтов

<div align="center"><h1>как запустить?</h1></div>

### 1. подготовка
```bash
# клонируем и переходим
git clone https://github.com/nonFeature/mus-downloader.git
cd mus-downloader

# ставим зависимости через uv
uv sync
```

### 2. настройка `.env`
копируем `.env.example` в `.env` и заполняем:
- `SLSK_USER` и `SLSK_PASS` - аккаунт soulseek (бесплатный, можно зарегистрировать за минуту)
- `BOT_TOKEN` - токен от @BotFather (если запускаешь бота)
- `ALLOWED_USERS` - Telegram ID через запятую
- `LOCAL_BOT_API=true` + `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` - опционально, для отправки файлов тяжелее 50 МБ (до 2 ГБ)

### 3. запуск

**cli:**
```bash
uv run main.py "https://open.spotify.com/track/4kSCNra5VuD1ZfiwAe8bTD" --flac
```
или
```bash
uv run main.py "vince staples big fish" --mp3
```

**telegram bot:**
```bash
uv run bot.py
```
