# mus-downloader 🎵

A multi-source, high-quality music downloader written in Python. It supports downloading tracks in lossless (**FLAC**) and high-quality (**MP3**) formats, automatically resolves metadata via song.link and MusicBrainz, tags downloaded tracks, and embeds album art.

[Русское описание находится ниже](#русский)

---

## Features

- **Multi-Source Fallback Chain**: 
  1. **Soulseek (slskd)**: Primary P2P source when configured. The quickest way to get the freshest music releases, unreleased tracks, and scene rips in FLAC Lossless and MP3 320 kbps.
  2. **YouTube Music (CSVMusic Matching Engine)**: Primary streaming source for authentic MP3 320 kbps (LAME CBR). Strict candidate scoring, duration verification within ±10%, filtering out covers, remixes, live performances, and nightcore.
  3. **Deezer**: Authentic lossless FLAC stream extraction and high-speed MP3 320 kbps via on-the-fly Blowfish decryption.
  4. **Direct Stream Fallback**: Direct stream extraction via `yt-dlp` for edge cases.
- **Zero-Key Core**: Downloading tracks requires 0 API keys. Optional keys are strictly for extra metadata enrichment (Last.fm, Discogs).
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
- `SLSKD_URL` / `SLSKD_USER` / `SLSKD_PASS` / `SLSKD_DOWNLOADS_PATH`: Connection and download parameters for your `slskd` instance (Soulseek daemon, optional).
- `LASTFM_API_KEY`: Custom Last.fm API key for genres and tags (optional, built-in key used by default).
- `DISCOGS_TOKEN`: Optional personal access token for Discogs database lookup.
- `DOWNLOAD_DIR`: Path to save downloaded tracks (default: `downloads`).

### Soulseek (slskd) Docker Setup
For headless remote servers, you can run `slskd` using Docker with `docker-compose.yml`:
1. Create a `docker-compose.yml` file:
   ```yml
   services:
     slskd:
       image: ghcr.io/slskd/slskd:latest
       container_name: slskd
       restart: unless-stopped
       ports:
         - "127.0.0.1:5030:5030"
       environment:
         # Your Soulseek credentials
         - SLSKD_SLSK_USERNAME=your_username
         - SLSKD_SLSK_PASSWORD=your_password
         
         # Allow remote configuration via web UI
         - SLSKD_REMOTE_CONFIGURATION=true
         
         # Credentials for slskd web panel (to be used in downloader's .env)
         - SLSKD_USERNAME=admin
         - SLSKD_PASSWORD=admin_password
       volumes:
         # Path to store database and configurations
         - ./slskd/appdata:/app/appdata
         # Path to store downloaded files (script retrieves files from here)
         - ./slskd/downloads:/app/downloads
   ```
2. Start the container:
   ```bash
   docker compose up -d
   ```
3. Configure `.env`, setting `SLSKD_URL=http://localhost:5030` and `SLSKD_DOWNLOADS_PATH=./slskd/downloads`.

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
  1. **Soulseek (демон slskd)**: Первичный P2P-источник (при настроенном `SLSKD_URL`). Идеален для самых свежих релизов, редких треков, микстейпов и оригинальных сцен-рипов в FLAC Lossless и MP3 320kbps.
  2. **YouTube Music (алгоритм CSVMusic)**: Основной стриминговый источник для MP3 320 kbps (LAME CBR). Строгая проверка хронометража (±10%), токенизация, отсев каверов, ремиксов, лайвов и nightcore.
  3. **Deezer**: Скачивание и расшифровка оригинального FLAC Lossless потока и MP3 320 kbps на лету с использованием Blowfish.
  4. **Прямой фолбек**: Прямое скачивание потока через `yt-dlp`.
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
- `SLSKD_URL` / `SLSKD_USER` / `SLSKD_PASS` / `SLSKD_DOWNLOADS_PATH`: Параметры подключения и папка загрузок вашего инстанса `slskd` (демона Soulseek, опционально).
- `LASTFM_API_KEY`: Пользовательский ключ Last.fm для тегов/жанров (опционально, встроен дефолтный ключ).
- `DISCOGS_TOKEN`: Опциональный токен Discogs для расширенного поиска метаданных.
- `DOWNLOAD_DIR`: Директория для сохранения скачанной музыки (по умолчанию: `downloads`).

### Настройка Soulseek (slskd) через Docker Compose
Для работы на удаленных серверах удобнее всего запустить `slskd` через Docker с помощью файла `docker-compose.yml`:
1. Создайте файл:
   ```yml
   services:
      slskd:
         image: ghcr.io/slskd/slskd:latest
         container_name: slskd
         restart: unless-stopped
         ports:
            - "127.0.0.1:5030:5030"
         environment:
            # Ваши учетные данные для входа в сеть Soulseek
            - SLSKD_SLSK_USERNAME=
            - SLSKD_SLSK_PASSWORD=ваш_пароль_в_soulseek
            
            # Разрешить удаленную настройку через веб-интерфейс
            - SLSKD_REMOTE_CONFIGURATION=true
            
            # Логин и пароль для веб-панели управления slskd (будут использоваться в .env скачивателя)
            - SLSKD_USERNAME=admin
            - SLSKD_PASSWORD=admin_password
         volumes:
            # Папка для базы данных и конфигурации slskd
            - ./slskd/appdata:/app/appdata
            # Папка для скачанных файлов (скрипт будет забирать файлы отсюда)
            - ./slskd/downloads:/app/downloads
   ```
2. Запустите контейнер:
   ```bash
   docker compose up -d
   ```
3. Настройте `.env`, указав `SLSKD_URL=http://localhost:5030` и путь к загрузкам `SLSKD_DOWNLOADS_PATH=./slskd/downloads`.

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