import base64
import httpx
from pathlib import Path
from mutagen.flac import FLAC, Picture
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

from . import coverart


def process_cover_art_to_square(raw_bytes: bytes, target_size: int = 0) -> tuple[bytes, str]:
    """
    Обеспечивает квадратный формат (1:1) обложки для всех музыкальных плееров:
    - Обнаруживает и срезает черные/однородные поля (letterbox/pillarbox).
    - Если изображение не квадратное (например, превью YouTube 16:9 или 4:3),
      аккуратно кадрирует его в квадрат 1:1 по центру.
    - При ``target_size`` > 0 дополнительно масштабирует до нужного разрешения
      (1400 для FLAC, 800 для MP3).
    - Сохраняет в высоком качестве (JPEG, quality=95).
    """
    return coverart.fit_cover_art(raw_bytes, target_size)


def download_cover_art(url: str, target_size: int = 0) -> tuple[bytes | None, str | None]:
    """
    Скачивает обложку по URL и приводит её к квадрату нужного разрешения.

    ``target_size`` подставляется прямо в ссылку у CDN, которые это умеют
    (Deezer, iTunes, Cover Art Archive) — это нативный размер, а не апскейл.
    Для YouTube отдельно перебираются превью от maxresdefault вниз.
    """
    if not url:
        return None, None

    primary = coverart.resize_url_for_source(url, target_size) if target_size else url
    candidates = [primary] + [u for u in coverart.upscale_youtube_thumbnail(url) if u != primary]

    best: tuple[int, bytes, str] | None = None
    for target_url in candidates:
        data, mime = coverart.download_image(target_url)
        if not data:
            continue
        fitted, fitted_mime = coverart.fit_cover_art(data, target_size)
        measured = coverart.probe_image_size(fitted)
        score = min(measured) if measured else 0
        # Точное попадание в целевой размер — приоритетнее всего остального.
        score += 100000 if (target_size and measured and min(measured) == target_size) else 0
        if best is None or score > best[0]:
            best = (score, fitted, fitted_mime or mime or "image/jpeg")
        # Точное попадание в целевой размер — дальше искать незачем.
        if target_size and measured and min(measured) == target_size:
            break

    if best is None:
        return None, None
    return best[1], best[2]

def apply_metadata(
    file_path: Path,
    artist: str,
    title: str,
    album: str = "",
    year: str = None,
    track_number: int | None = None,
    track_total: int | None = None,
    album_art_url: str | None = None,
    album_artist: str | None = None,
    source: str | None = None,
    source_quality: str | None = None,
    compilation: bool = False,
    genre: str | None = None,
    cover_size: int = 0,
):
    """
    Записывает метаданные в аудиофайл (FLAC, MP3 или M4A).
    Если передан album_art_url, обложка скачивается и вшивается в файл.
    ``cover_size`` — целевое разрешение обложки в пикселях (0 = без ресайза).
    """
    if not file_path.exists():
        print(f"[!] Файл не найден: {file_path}")
        return

    suffix = file_path.suffix.lower()
    art_bytes, art_mime = (
        download_cover_art(album_art_url, cover_size) if album_art_url else (None, None)
    )
    has_art = art_bytes is not None

    try:
        if suffix == '.flac':
            audio = FLAC(str(file_path))
            audio["ARTIST"] = artist
            audio["TITLE"] = title
            if album:
                audio["ALBUM"] = album
            if album_artist:
                audio["ALBUMARTIST"] = album_artist
                audio["ALBUM ARTIST"] = album_artist
            if compilation:
                audio["COMPILATION"] = "1"
            if year:
                audio["DATE"] = year
            if track_number:
                audio["TRACKNUMBER"] = str(track_number)
                if track_total:
                    audio["TRACKTOTAL"] = str(track_total)
                    audio["TOTALTRACKS"] = str(track_total)
            if source:
                audio["SOURCE"] = source
            if source_quality:
                audio["SOURCE_QUALITY"] = source_quality
            if genre:
                audio["GENRE"] = genre
            if has_art:
                pic = Picture()
                pic.type = 3  # Front cover
                pic.mime = art_mime
                pic.data = art_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()

        elif suffix == '.mp3':
            try:
                audio = EasyID3(str(file_path))
            except Exception:
                # Если тегов нет, инициализируем пустой ID3
                mp3 = MP3(str(file_path))
                mp3.add_tags()
                mp3.save()
                audio = EasyID3(str(file_path))

            audio["artist"] = artist
            audio["title"] = title
            if album:
                audio["album"] = album
            if album_artist:
                audio["albumartist"] = [album_artist]
            if compilation:
                try:
                    EasyID3.RegisterTextKey("compilation", "TCMP")
                    audio["compilation"] = ["1"]
                except Exception:
                    pass
            if year:
                audio["date"] = year
            if track_number:
                tn = f"{track_number}/{track_total}" if track_total else str(track_number)
                audio["tracknumber"] = [tn]
            if genre:
                audio["genre"] = genre
            
            # Регистрируем кастомные TXXX фреймы для источника и качества
            try:
                EasyID3.RegisterTXXXKey("source", "SOURCE")
                EasyID3.RegisterTXXXKey("source_quality", "SOURCE_QUALITY")
                if source:
                    audio["source"] = source
                if source_quality:
                    audio["source_quality"] = source_quality
            except Exception:
                pass
            audio.save()

            if has_art:
                mp3 = MP3(str(file_path), ID3=ID3)
                if mp3.tags is None:
                    mp3.add_tags()
                mp3.tags.delall("APIC")
                mp3.tags.add(
                    APIC(
                        encoding=3,
                        mime=art_mime,
                        type=3,  # Front cover
                        desc="Cover",
                        data=art_bytes
                    )
                )
                mp3.save(v2_version=3)

        elif suffix in ['.m4a', '.mp4']:
            audio = MP4(str(file_path))
            audio["\xa9ART"] = [artist]
            audio["\xa9nam"] = [title]
            if album:
                audio["\xa9alb"] = [album]
            if album_artist:
                audio["aART"] = [album_artist]
            if compilation:
                audio["cpil"] = True
            if year:
                audio["\xa9day"] = [year]
            if track_number:
                audio["trkn"] = [(track_number, track_total or 0)]
            if source:
                audio["----:com.musicgrabber:SOURCE"] = [source.encode("utf-8")]
            if source_quality:
                audio["----:com.musicgrabber:SOURCE_QUALITY"] = [source_quality.encode("utf-8")]
            if genre:
                audio["\xa9gen"] = [genre]
            
            if has_art:
                cover_format = MP4Cover.FORMAT_PNG if "png" in art_mime else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(art_bytes, imageformat=cover_format)]
            audio.save()

        elif suffix in ['.opus', '.ogg']:
            audio = OggOpus(str(file_path))
            audio["ARTIST"] = artist
            audio["TITLE"] = title
            if album:
                audio["ALBUM"] = album
            if album_artist:
                audio["ALBUMARTIST"] = album_artist
            if compilation:
                audio["COMPILATION"] = "1"
            if year:
                audio["DATE"] = year
            if track_number:
                audio["TRACKNUMBER"] = str(track_number)
                if track_total:
                    audio["TRACKTOTAL"] = str(track_total)
            if source:
                audio["SOURCE"] = source
            if source_quality:
                audio["SOURCE_QUALITY"] = source_quality
            if genre:
                audio["GENRE"] = genre
            if has_art:
                try:
                    pic = Picture()
                    pic.type = 3
                    pic.mime = art_mime
                    pic.data = art_bytes
                    audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
                except Exception:
                    pass
            audio.save()
            
        else:
            print(f"[!] Неподдерживаемый формат файла для теггирования: {suffix}")

    except Exception as e:
        print(f"[!] Ошибка при записи тегов: {e}")


def apply_extended_metadata(file_path: Path, extras: dict) -> bool:
    """Дописывает «мелочи» из web-API (TIDAL / Apple Music) в готовый файл.

    Базовые поля (исполнитель, название, альбом, год, обложка) пишет
    :func:`apply_metadata`; здесь только то, чего в обычном источнике нет:
    ISRC, композитор, лейбл, копирайт, номера диска/трека с общими,
    полная дата релиза, жанр, BPM, тональность, ReplayGain, UPC.

    Отсутствующие ключи пропускаются, поэтому вызывать можно с любым
    словарём. Возвращает True, если файл перезаписан.
    """
    if not extras or not file_path.exists():
        return False

    def text(key: str) -> str:
        value = extras.get(key)
        if value is None or value == "" or value == []:
            return ""
        return str(value).strip()

    def number(key: str) -> str:
        value = extras.get(key)
        if value is None or value == "":
            return ""
        try:
            as_float = float(value)
        except (TypeError, ValueError):
            return str(value).strip()
        return str(int(as_float)) if as_float.is_integer() else str(as_float)

    isrc = text("isrc")
    composer = text("composer")
    label = text("label")
    copyright_ = text("copyright")
    release_date = text("release_date") or text("year")
    genre = text("genre")
    bpm = number("bpm")
    initial_key = text("initial_key")
    upc = text("upc")
    disc_number = number("disc_number")
    disc_total = number("disc_total")
    track_number = number("track_number")
    track_total = number("track_total")
    gain = number("replaygain_track_gain")
    peak = number("replaygain_track_peak")

    suffix = file_path.suffix.lower()
    try:
        if suffix in (".flac", ".opus", ".ogg"):
            audio = FLAC(str(file_path)) if suffix == ".flac" else OggOpus(str(file_path))

            def put(tag: str, value: str) -> None:
                if value:
                    audio[tag] = value

            put("ISRC", isrc)
            put("COMPOSER", composer)
            put("LABEL", label)
            put("ORGANIZATION", label)
            put("COPYRIGHT", copyright_)
            put("DATE", release_date)
            put("GENRE", genre)
            put("BPM", bpm)
            put("INITIALKEY", initial_key)
            put("UPC", upc)
            put("DISCNUMBER", disc_number)
            put("DISCTOTAL", disc_total)
            put("TRACKTOTAL", track_total)
            put("TOTALTRACKS", track_total)
            put("REPLAYGAIN_TRACK_GAIN", f"{gain} dB" if gain else "")
            put("REPLAYGAIN_TRACK_PEAK", peak)
            audio.save()

        elif suffix == ".mp3":
            from mutagen.id3 import TALB, TBPM, TCOM, TCOP, TDRC, TKEY, TPOS, TPUB, TRCK, TSRC, TXXX, TCON

            audio = ID3(str(file_path))
            if isrc:
                audio.delall("TSRC")
                audio.add(TSRC(encoding=3, text=[isrc]))
            if composer:
                audio.delall("TCOM")
                audio.add(TCOM(encoding=3, text=[composer]))
            if label:
                audio.delall("TPUB")
                audio.add(TPUB(encoding=3, text=[label]))
            if copyright_:
                audio.delall("TCOP")
                audio.add(TCOP(encoding=3, text=[copyright_]))
            if release_date:
                audio.delall("TDRC")
                audio.add(TDRC(encoding=3, text=[release_date]))
            if genre:
                audio.delall("TCON")
                audio.add(TCON(encoding=3, text=[genre]))
            if bpm:
                audio.delall("TBPM")
                audio.add(TBPM(encoding=3, text=[bpm]))
            if initial_key:
                audio.delall("TKEY")
                audio.add(TKEY(encoding=3, text=[initial_key]))
            if disc_number:
                audio.delall("TPOS")
                audio.add(TPOS(encoding=3, text=[f"{disc_number}/{disc_total}" if disc_total else disc_number]))
            if track_number and track_total:
                audio.delall("TRCK")
                audio.add(TRCK(encoding=3, text=[f"{track_number}/{track_total}"]))
            for desc, value in (
                ("ISRC", isrc),
                ("LABEL", label),
                ("UPC", upc),
                ("INITIALKEY", initial_key),
                ("REPLAYGAIN_TRACK_GAIN", f"{gain} dB" if gain else ""),
                ("REPLAYGAIN_TRACK_PEAK", peak),
            ):
                if not value:
                    continue
                audio.delall(f"TXXX:{desc}")
                audio.add(TXXX(encoding=3, desc=desc, text=[value]))
            audio.save(v2_version=3)

        elif suffix in (".m4a", ".mp4"):
            audio = MP4(str(file_path))

            def atom(name: str, value: str) -> None:
                if value:
                    audio[name] = [value.encode("utf-8")]

            atom("----:com.apple.iTunes:ISRC", isrc)
            atom("----:com.apple.iTunes:LABEL", label)
            atom("----:com.apple.iTunes:UPC", upc)
            atom("----:com.apple.iTunes:initialkey", initial_key)
            atom("----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN", f"{gain} dB" if gain else "")
            atom("----:com.apple.iTunes:REPLAYGAIN_TRACK_PEAK", peak)
            if composer:
                audio["\xa9wrt"] = [composer]
            if copyright_:
                audio["cprt"] = [copyright_]
            if release_date:
                audio["\xa9day"] = [release_date]
            if genre:
                audio["\xa9gen"] = [genre]
            if bpm:
                try:
                    audio["tmpo"] = [int(float(bpm))]
                except (TypeError, ValueError):
                    pass
            if disc_number:
                audio["disk"] = [(int(float(disc_number)), int(float(disc_total or 0)))]
            if track_number and track_total:
                audio["trkn"] = [(int(float(track_number)), int(float(track_total)))]
            audio.save()
        else:
            return False
    except Exception as exc:
        print(f"[!] Ошибка при записи дополнительных тегов: {exc}")
        return False
    return True
