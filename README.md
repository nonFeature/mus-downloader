# mus-downloader 🎵

A multi-source, high-quality music downloader written in Python. It supports downloading tracks in lossless (**FLAC**) and high-quality (**MP3**) formats, automatically resolves metadata via song.link and MusicBrainz, tags downloaded tracks, and embeds album art.

[Русское описание находится ниже](#русский)

---

## Features

- **Multi-Source Fallback Chain**: 
  1. **Soulseek (Native Embedded P2P / aioslsk)**: Primary P2P source when credentials (`SLSK_USER`, `SLSK_PASS`) are configured in `.env`. Connects directly to the Soulseek network with zero external daemons or Docker needed. Ideal for original scene rips, FLAC Lossless, and authentic MP3 320 kbps.
  2. **YouTube Music (CSVMusic Matching Engine)**: Primary streaming source for MP3 (Opus transcode to 256k LAME CBR). Strict candidate scoring, duration verification within ±10%, filtering out covers, remixes, live performances, and nightcore.
  3. **Deezer**: Lossless FLAC stream extraction and high-speed MP3 320 kbps via on-the-fly Blowfish decryption.
  4. **SoundCloud**: Original stream preservation (MP3 or AAC converted without inflation).
  5. **Direct Stream Fallback**: Direct stream extraction via `yt-dlp` for edge cases.
- **Zero External Daemons**: Soulseek P2P runs directly inside the Python process. No Docker or `slskd` daemon required.
- **Metadata Resolving**: Accepts any song link (Spotify, Apple Music, Deezer, YouTube Music, etc.) or text query. Resolves cross-platform links via song.link, fetches clean ISRC codes, and queries MusicBrainz / iTunes / Last.fm to fetch accurate metadata and high-res cover art.
- **Metadata Tagging**: Automatically writes complete metadata tags (Title, Artist, Album, Year, Track number, Genre) and embeds high-resolution cover art into FLAC and MP3 files using `mutagen`.

---

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for fast and reliable dependency management.

1. Clone the repository.
2. Install dependencies and create a virtual environment:
   ```bash
   uv sync
   ```

Make sure you have [FFmpeg](https://ffmpeg.org/) installed and added to your system's PATH.

---

## Configuration

Copy `.env.example` to `.env` and fill in the configuration values:
```bash
cp .env.example .env
```

Available variables:
- `SLSK_USER` / `SLSK_PASS`: Your Soulseek username and password for embedded P2P downloads (no Docker required!).
- `SLSKD_URL` / `SLSKD_USER` / `SLSKD_PASS` / `SLSKD_DOWNLOADS_PATH`: Optional legacy `slskd` daemon connection parameters.
- `LASTFM_API_KEY`: Custom Last.fm API key for genres and tags (optional, built-in key used by default).
- `DISCOGS_TOKEN`: Optional personal access token for Discogs database lookup.
- `DOWNLOAD_DIR`: Path to save downloaded tracks (default: `downloads`).

---

## Usage

Run the script using `uv run`:

```bash
# Download a track by Spotify/Deezer/Apple Music link in FLAC (default)
uv run main.py https://open.spotify.com/track/2qwjVyRjKzownq7ggOcgj8

# Download a track in MP3 format
uv run main.py -q MP3 https://open.spotify.com/track/2qwjVyRjKzownq7ggOcgj8

# Search and download by query (Artist - Title)
uv run main.py "MGMT - Kids"
```

### Arguments

- `query`: The URL (Spotify, Apple Music, Deezer, etc.) or search query.
- `-q`, `--quality`: Target quality format (`FLAC` or `MP3`). Default is `FLAC`.

---

<a name="русский"></a>
# mus-downloader (Русский) 🎵

Мультиплатформенный загрузчик музыки в высоком качестве на Python. Позволяет скачивать треки во **FLAC** (lossless) и **MP3** (320kbps) форматах, автоматически сопоставляет метаданные через song.link и MusicBrainz, прописывает теги и вшивает обложки альбомов.

---

## Возможности

- **Интеллектуальная цепочка источников**:
  1. **Soulseek (встроенный нативный P2P / aioslsk)**: Первичный P2P-источник (при указании `SLSK_USER` и `SLSK_PASS` в `.env`). Подключается напрямую к сети Soulseek внутри процесса без Docker и внешних демонов. Идеален для самых свежих релизов, редких треков, микстейпов и оригинальных сцен-рипов в FLAC Lossless и честном MP3 320kbps.
  2. **YouTube Music (алгоритм CSVMusic)**: Основной стриминговый источник для MP3 (транскодирование Opus в честный 256k LAME CBR). Строгая проверка хронометража (±10%), токенизация, отсев каверов, ремиксов, лайвов и nightcore.
  3. **Deezer**: Скачивание и расшифровка оригинального FLAC Lossless потока и MP3 320 kbps на лету с использованием Blowfish.
  4. **SoundCloud**: Сохранение оригинального потока (MP3 или AAC без раздувания битрейта).
  5. **Прямой фолбек**: Прямое скачивание потока через `yt-dlp`.
- **Никаких внешних демонов**: Soulseek P2P полностью встроен в приложение на чистом Python (`aioslsk`). Docker и `slskd` больше не нужны.
- **Полная независимость от обязательных API-ключей**: Скачивание работает сразу "из коробки" без каких-либо платных ключей или токенов. Опциональные ключи используются исключительно для расширенных баз метаданных (Last.fm, Discogs).
- **Разрешение метаданных**: Принимает ссылки любых музыкальных сервисов (Spotify, Apple Music, Deezer, YouTube Music и др.) или текстовые поисковые запросы. Определяет ISRC с помощью song.link и извлекает чистые альбомные метаданные из баз MusicBrainz / iTunes / Last.fm (игнорирует нежелательные ремиксы и сборники).
- **Автоматическое теггирование**: Записывает теги (название, артист, альбом, год, номер трека, жанр) и вшивает обложки высокого разрешения (до 1000x1000) в результирующие FLAC и MP3 файлы с помощью библиотеки `mutagen`.

---

## Установка

Для управления зависимостями используется утилита [uv](https://github.com/astral-sh/uv).

1. Клонируйте репозиторий.
2. Установите зависимости и создайте виртуальное окружение:
   ```bash
   uv sync
   ```

Для корректной работы фолбеков и конвертации аудио убедитесь, что в вашей системе установлен [FFmpeg](https://ffmpeg.org/) и его путь добавлен в системный PATH.

---

## Настройка

Скопируйте файл `.env.example` в `.env` и при необходимости укажите настройки:
```bash
cp .env.example .env
```

Основные переменные:
- `SLSK_USER` / `SLSK_PASS`: Логин и пароль вашего аккаунта Soulseek (работает автономно прямо в приложении!).
- `SLSKD_URL` / `SLSKD_USER` / `SLSKD_PASS` / `SLSKD_DOWNLOADS_PATH`: Опциональные параметры для подключения к внешнему демону `slskd` (для обратной совместимости).
- `LASTFM_API_KEY`: Пользовательский ключ Last.fm для тегов/жанров (опционально, встроен дефолтный ключ).
- `DISCOGS_TOKEN`: Опциональный токен Discogs для расширенного поиска метаданных.
- `DOWNLOAD_DIR`: Директория для сохранения скачанной музыки (по умолчанию: `downloads`).

---

## Использование

Запуск программы осуществляется через команду `uv run`:

```bash
# Скачать трек по ссылке Spotify в качестве FLAC (по умолчанию)
uv run main.py https://open.spotify.com/track/2qwjVyRjKzownq7ggOcgj8

# Скачать трек в формате MP3
uv run main.py -q MP3 https://open.spotify.com/track/2qwjVyRjKzownq7ggOcgj8

# Найти и скачать трек по текстовому запросу
uv run main.py "MGMT - Kids"
```

### Параметры CLI

- `query`: Ссылка на трек (Spotify, Apple Music, Deezer и т. д.) или текстовый запрос.
- `-q`, `--quality`: Желаемое качество скачивания (`FLAC` или `MP3`). По умолчанию `FLAC`.