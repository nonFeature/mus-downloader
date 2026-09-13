# /// script
# dependencies = [
#   "pycryptodome",
#   "mutagen",
#   "httpx",
#   "yt-dlp",
#   "ytmusicapi",
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

    parser = argparse.ArgumentParser(description="Multi-source Music Downloader (YouTube Music / Deezer / Soulseek)")
    parser.add_argument("url", nargs="?", help="URL of the track to download or search query")
    parser.add_argument("-q", "--quality", choices=["MP3", "FLAC"], default="MP3", help="Preferred download quality (default: MP3 320kbps)")
    
    args = parser.parse_args()
    
    if not args.url:
        print("=== Multi-source Music Downloader ===")
        print("Источники: Soulseek (P2P свежие релизы) + YouTube Music (MP3 320kbps) + Deezer FLAC.")
        print("Поддерживаемые ссылки: Spotify, Apple Music, Deezer, YouTube Music, SoundCloud, Яндекс.Музыка и текстовые запросы.\n")
        
        args.url = input("Введите ссылку на трек или название: ").strip()
        if not args.url:
            print("[!] Ссылка или запрос не могут быть пустыми.")
            sys.exit(1)
            
        choice = input("Предпочитаемое качество (1 - MP3 320kbps (рекомендуется), 2 - FLAC (Deezer / Soulseek)) [1]: ").strip()
        if choice == "2":
            args.quality = "FLAC"
        else:
            args.quality = "MP3"
            
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
