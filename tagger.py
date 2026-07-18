import httpx
from pathlib import Path
from mutagen.flac import FLAC, Picture
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.mp4 import MP4, MP4Cover

def download_cover_art(url: str) -> tuple[bytes | None, str | None]:
    """Скачивает обложку по URL и возвращает (bytes, mime_type)"""
    if not url:
        return None, None
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code == 200:
            mime = resp.headers.get("content-type", "image/jpeg")
            return resp.content, mime
    except Exception as e:
        print(f"[!] Не удалось скачать обложку: {e}")
    return None, None

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
):
    """
    Записывает метаданные в аудиофайл (FLAC, MP3 или M4A).
    Если передан album_art_url, обложка скачивается и вшивается в файл.
    """
    if not file_path.exists():
        print(f"[!] Файл не найден: {file_path}")
        return

    suffix = file_path.suffix.lower()
    art_bytes, art_mime = download_cover_art(album_art_url) if album_art_url else (None, None)
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
            print(f"[+] Теги FLAC записаны успешно.")

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
            print(f"[+] Теги MP3 записаны успешно.")

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
            print(f"[+] Теги M4A записаны успешно.")
            
        else:
            print(f"[!] Неподдерживаемый формат файла для теггирования: {suffix}")

    except Exception as e:
        print(f"[!] Ошибка при записи тегов: {e}")
