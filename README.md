# mus-downloader 🎵

A multi-source, high-quality music downloader written in Python. It supports downloading tracks in lossless (**FLAC**) and high-quality (**MP3**) formats, automatically resolves metadata via song.link and MusicBrainz, tags downloaded tracks, and embeds album art.

[Русское описание находится ниже](#русский)

---

## Features

- **Multi-Source Fallback Chain**: 
  1. **Monochrome**: Resolves tracks via Qobuz proxies (using ISRC or text query search) and Tidal hifi-api (using Tidal ID). Supports Qobuz and Tidal lossless streaming (up to Hi-Res 24-bit).
  2. **Deezer**: Streams and decrypts lossless streams using Echo API Proxy.
  3. **Yandex Music**: Downloads tracks via `yandex-music` API (FLAC & MP3 320kbps).
  4. **Zvuk (Sber Zvuk)**: Downloads tracks via `zvuk-music` library (FLAC & MP3).
  5. **Soulseek**: Searches peer-to-peer sharing network using `slskd` API for rare and high-quality tracks.
  6. **Fallback**: SoundCloud and YouTube Music using `yt-dlp` (filtering low quality, encoding to 320kbps MP3 via FFmpeg).
- **Metadata Resolving**: Accepts any song link (Spotify, Apple Music, Deezer, YouTube Music, etc.). Resolves it via the song.link API, fetches clean ISRC codes, and queries MusicBrainz to fetch accurate album metadata and original release dates (skipping compilations/live editions).
- **Metadata Tagging**: Automatically writes complete metadata tags (Title, Artist, Album, Year, Track number) and embeds high-resolution cover art into FLAC, MP3, and M4A files using `mutagen`.

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
- `SLSKD_URL` / `SLSKD_USER` / `SLSKD_PASS` / `SLSKD_DOWNLOADS_PATH`: Connection and download parameters for your `slskd` instance (Soulseek daemon).
- `YANDEX_TOKEN`: Access token for Yandex Music API.
- `ZVUK_TOKEN`: Authentication token for Zvuk API.
- `MONOCHROME_AMAZON_API_URL`: Custom Amazon Music proxy API URL (default: `https://amz.geeked.wtf`).
- `MONOCHROME_AMAZON_JWT`: Cloudflare Turnstile JWT for Amazon Music authentication (valid for 1 hour).
- `MONOCHROME_AMAZON_BYPASS_TOKEN`: Optional bypass token for Amazon Music.
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
  1. **Monochrome**: Поиск по ISRC и тексту на прокси Qobuz и скачивание потоков через Tidal hifi-api (поддержка Hi-Res 24-бит и CD FLAC).
  2. **Deezer**: Скачивание и расшифровка оригинального потока с использованием прокси Echo API.
  3. **Яндекс Музыка**: Скачивание через официальное API (FLAC и MP3 320кбит/с).
  4. **Сбер Звук**: Скачивание через библиотеку `zvuk-music` (FLAC и MP3).
  5. **Soulseek**: Поиск редких записей и FLAC-файлов в P2P-сети через API демона `slskd`.
  6. **Резервный фолбек**: SoundCloud и YouTube Music через `yt-dlp` (с фильтрацией низкого битрейта и перекодированием в MP3 320kbps через FFmpeg).
- **Разрешение метаданных**: Принимает ссылки любых музыкальных сервисов (Spotify, Apple Music, Deezer, YouTube Music и др.). Определяет ISRC с помощью song.link API и извлекает чистые альбомные метаданные из базы MusicBrainz (игнорирует плейлисты, синглы и сборники).
- **Автоматическое теггирование**: Записывает теги (название, артист, альбом, год, номер трека) и вшивает обложки высокого разрешения в результирующие FLAC, MP3 и M4A файлы с помощью библиотеки `mutagen`.

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

Скопируйте файл `.env.example` в `.env` и укажите необходимые токены:
```bash
cp .env.example .env
```

Основные переменные:
- `SLSKD_URL` / `SLSKD_USER` / `SLSKD_PASS` / `SLSKD_DOWNLOADS_PATH`: Параметры подключения и папка загрузок вашего инстанса `slskd` (демона Soulseek).
- `YANDEX_TOKEN`: Токен доступа Яндекс Музыки.
- `ZVUK_TOKEN`: Авторизационный токен Сбер Звука.
- `MONOCHROME_AMAZON_API_URL`: URL прокси-сервера Amazon Music (по умолчанию: `https://amz.geeked.wtf`).
- `MONOCHROME_AMAZON_JWT`: Временный JWT-токен Cloudflare Turnstile для Amazon Music (действует 1 час).
- `MONOCHROME_AMAZON_BYPASS_TOKEN`: Опциональный токен обхода капчи для Amazon Music.
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