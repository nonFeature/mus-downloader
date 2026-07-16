# /// script
# dependencies = [
#   "pycryptodome",
#   "mutagen",
#   "yandex-music",
#   "httpx",
#   "yt-dlp",
#   "zvuk-music",
# ]
# ///

import argparse
import sys
from core import download_track_by_link

def main():
    # Настройка UTF-8 для вывода в консоль на Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Multi-source FLAC/MP3 Music Downloader")
    parser.add_argument("url", nargs="?", help="URL of the track to download (Spotify, Apple Music, Deezer, etc.)")
    parser.add_argument("-q", "--quality", choices=["FLAC", "MP3"], default="FLAC", help="Preferred download quality (default: FLAC)")
    
    args = parser.parse_args()
    
    if not args.url:
        print("=== Multi-source Music Downloader ===")
        print("Поддерживаемые источники: Spotify, Apple Music, Deezer, Yandex, Zvuk, YouTube, SoundCloud и др.\n")
        
        args.url = input("Введите ссылку на трек: ").strip()
        if not args.url:
            print("[!] Ссылка не может быть пустой.")
            sys.exit(1)
            
        choice = input("Предпочитаемое качество (1 - FLAC (рекомендуется), 2 - MP3 320kbps) [1]: ").strip()
        if choice == "2":
            args.quality = "MP3"
        else:
            args.quality = "FLAC"
            
    try:
        file_path = download_track_by_link(args.url, args.quality)
        if file_path:
            print(f"\n[+] Идеально сохранено: {file_path.resolve()}")
        else:
            print("\n[-] Не удалось скачать трек.")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] Скачивание прервано пользователем.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[!] Возникла ошибка во время работы: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
