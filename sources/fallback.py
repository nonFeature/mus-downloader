import yt_dlp
import re
from pathlib import Path
from typing import Optional

def download_fallback_track(url: str, dest_dir: Path) -> Optional[Path]:
    """
    Скачивает аудио с YouTube Music, YouTube или SoundCloud через yt-dlp.
    Конвертирует в MP3 с качеством 320kbps (или лучшим доступным).
    Исключает аудиопотоки с битрейтом менее 96kbps.
    """
    print(f"[*] Fallback: Скачивание по ссылке через yt-dlp: {url}")
    
    # Шаблон имени временного файла
    temp_template = str(dest_dir / "temp_fallback_%(id)s.%(ext)s")
    
    ydl_opts = {
        # Ищем лучший аудиопоток с битрейтом не менее 96kbps, при отсутствии берем любой лучший
        'format': 'bestaudio[abr>=96]/bestaudio/best',
        'outtmpl': temp_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 1. Извлекаем информацию о треке
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
                
            # 2. Определяем путь к скачанному файлу
            # Метод prepare_filename возвращает путь до постпроцессинга
            temp_path = Path(ydl.prepare_filename(info))
            
            # После FFmpegExtractAudio расширение меняется на .mp3
            final_temp_path = temp_path.with_suffix(".mp3")
            
            if not final_temp_path.exists():
                # Пробуем найти любой .mp3 файл с тем же префиксом temp_fallback_
                temp_files = list(dest_dir.glob(f"temp_fallback_{info['id']}.mp3"))
                if temp_files:
                    final_temp_path = temp_files[0]
                else:
                    print("[!] Fallback: Не удалось найти скачанный файл после конвертации")
                    return None
            
            # 3. Переименовываем файл в красивое имя
            title = info.get("title", "Unknown Track")
            uploader = info.get("uploader", "Unknown Artist")
            
            # Убираем лишние символы для имени файла
            clean_title = re.sub(r'[\\/*?:"<>|]', "_", title)
            clean_uploader = re.sub(r'[\\/*?:"<>|]', "_", uploader)
            
            output_path = dest_dir / f"{clean_uploader} - {clean_title}.mp3"
            
            # Перемещаем
            final_temp_path.replace(output_path)
            print(f"[+] Fallback: Скачивание завершено: {output_path}")
            return output_path
            
    except Exception as e:
        print(f"[!] Fallback: Ошибка при скачивании по ссылке: {e}")
        
    return None
