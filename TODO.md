# Roadmap & TODO 📋

План развития и улучшений для **mus-downloader**.

---

## 1. Пакетная загрузка: Альбомы и Плейлисты
- [ ] **Парсинг ссылок на альбомы и плейлисты:**
  - Spotify: `open.spotify.com/album/...`, `open.spotify.com/playlist/...`
  - Apple Music: `music.apple.com/.../album/...`, `.../playlist/...`
  - Deezer: `deezer.com/album/...`, `deezer.com/playlist/...`
  - Яндекс.Музыка: `music.yandex.ru/album/...`, `.../users/.../playlists/...`
  - YouTube Music: `music.youtube.com/playlist?list=...`
- [ ] **Очередь и параллелизм:**
  - Пакетное разворачивание ссылок в единую очередь треков с номерами (`track_number/track_total`).
  - Пул воркеров (`ThreadPoolExecutor`) для параллельной загрузки нескольких треков.
- [ ] **Загрузка из текстового файла (`--file` / `-f tracks.txt`):**
  - Чтение ссылок или названий треков построчно.
  - Итоговая статистика загрузки (успешно, пропущено, источники, затраченное время).

---

## 2. Тексты песен и караоке (Lyrics & Synced LRC)
- [ ] **Интеграция с [LRCLIB](https://lrclib.net/):**
  - Публичный бесплатный API, не требующий API-ключей.
  - Поиск по `artist_name`, `track_name`, `album_name` и `duration`.
- [ ] **Караоке `.lrc` файлы:**
  - Сохранение таймированных субтитров рядом с аудиофайлом (`Artist - Title.lrc`).
- [ ] **Вшивание в теги:**
  - ID3v2: фрейм `USLT` (Unsynchronized lyrics) / `SYLT` (Synchronized).
  - FLAC / Vorbis Comments: поля `LYRICS` и `UNSYNCEDLYRICS`.

---

## 3. Шаблоны путей и структура папок (Output Formatting)
- [ ] **Кастомизация структуры папок через шаблоны (`--template` или в `.env`):**
  - По умолчанию: `{artist} - {title}.{ext}`
  - По альбомам: `{artist}/{album}/{track_number:02d} - {title}.{ext}`
  - Для сборников: `Compilations/{album}/{track_number:02d} - {artist} - {title}.{ext}`
- [ ] **Параметр командной строки `--output-dir` / `-o`:**
  - Возможность задать папку назначения на лету без правки `.env`.

---

## 4. Нормализация громкости (ReplayGain / EBU R128)
- [ ] **Расчет ReplayGain через FFmpeg:**
  - Быстрый прогон через аудиофильтр `ebur128` без изменения аудиопотока.
- [ ] **Вшивание тегов:**
  - MP3: `TXXX:REPLAYGAIN_TRACK_GAIN`, `TXXX:REPLAYGAIN_TRACK_PEAK`.
  - FLAC: Vorbis-комментарии `REPLAYGAIN_TRACK_GAIN`, `REPLAYGAIN_TRACK_PEAK`.
  - Обеспечивает одинаковый комфортный уровень громкости в плеерах (foobar2000, Poweramp, Symfonium).

---

## 5. Детектор фейкового Lossless (Cutoff / Spectral Analysis)
- [ ] **Анализ частоты среза (Cutoff Frequency):**
  - Проверка спектра скачанных FLAC из Soulseek (защита от апскейлов MP3 128k/192k в FLAC).
  - Если спектр резко обрезается на 16–18 кГц — отбраковка файла или предупреждение.
  - Автоматический фолбек на Deezer Lossless при обнаружении фейкового FLAC.