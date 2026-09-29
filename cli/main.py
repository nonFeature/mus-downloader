# /// script
# dependencies = [
#   "pycryptodome",
#   "mutagen",
#   "httpx",
#   "yt-dlp",
#   "ytmusicapi",
#   "aioslsk>=1.5.0",
#   "rich>=13.0.0",
# ]
# ///

import argparse
import sys
from pathlib import Path
from typing import Optional

from core import download_track_by_link, search_tracks

try:
    from .ui import (
        print_banner,
        print_search_table,
        print_track_panel,
        print_success_panel,
        print_error,
        print_info,
        is_interactive,
        RICH_AVAILABLE,
        RichDownloadProgress,
    )
except (ImportError, ValueError):
    try:
        from cli.ui import (
            print_banner,
            print_search_table,
            print_track_panel,
            print_success_panel,
            print_error,
            print_info,
            is_interactive,
            RICH_AVAILABLE,
            RichDownloadProgress,
        )
    except ImportError:
        from ui import (
            print_banner,
            print_search_table,
            print_track_panel,
            print_success_panel,
            print_error,
            print_info,
            is_interactive,
            RICH_AVAILABLE,
            RichDownloadProgress,
        )


def format_search_result_item(index: int, item: dict) -> str:
    """Форматирует один элемент поисковой выдачи для отображения в терминале."""
    artist = item.get("artist") or "Unknown Artist"
    title = item.get("title") or "Unknown Track"
    album = item.get("album")
    year = item.get("year")
    duration = item.get("duration")
    source_quality = item.get("source_quality") or item.get("source") or "Web"
    explicit = " [E]" if item.get("explicit") else ""

    details = []
    if album:
        details.append(album)
    if year:
        details.append(str(year))
    if duration:
        mins = int(duration) // 60
        secs = int(duration) % 60
        details.append(f"{mins}:{secs:02d}")

    details_str = f" ({', '.join(details)})" if details else ""
    return f"[{index}] {artist} - {title}{explicit}{details_str} [{source_quality}]"


def select_candidate_interactive(candidates: list[dict], query: str = "") -> Optional[dict]:
    """Выводит интерактивный список результатов и запрашивает выбор пользователя."""
    if not candidates:
        return None

    # Отрисовка таблицы (через Rich или обычный текст)
    print_search_table(candidates, query)

    while True:
        try:
            choice = input(f"Выберите номер трека [1-{len(candidates)}] (Enter=1, 0=отмена): ").strip()
        except EOFError:
            # При запуске через пайп или закрытом stdin берем первый результат
            choice = "1"
        if not choice:
            return candidates[0]
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        print_error(f"Неверный выбор. Введите число от 1 до {len(candidates)} или 0.")


def cli_search_and_download(
    query: str,
    target_quality: str = "MP3",
    dest_dir: Optional[Path] = None,
    reuse_cached_file: bool = True,
) -> bool:
    """Интерактивный поиск по мульти-источникам и скачивание выбранного трека."""
    print_info(f"Поиск по мульти-источникам (Deezer, Apple Music, YouTube Music): '{query}'...")
    candidates = search_tracks(query, limit=8)
    if not candidates:
        print_error("Ничего не найдено.")
        return False

    selected = select_candidate_interactive(candidates, query=query)
    if not selected:
        print_info("Поиск отменен пользователем.")
        return False

    artist = selected.get("artist") or ""
    title = selected.get("title") or ""
    album = selected.get("album")
    year = selected.get("year")
    duration = selected.get("duration")
    source_url = selected.get("url") or f"{artist} - {title}"

    print_track_panel(artist, title, target_quality, album=album, year=year, duration=duration)

    with RichDownloadProgress() as progress_bar:
        file_path = download_track_by_link(
            source_url,
            target_quality=target_quality,
            dest_dir=dest_dir,
            track_meta=selected,
            progress_callback=progress_bar.update if is_interactive() else None,
            reuse_cached_file=reuse_cached_file,
        )
    if file_path:
        print_success_panel(file_path)
        return True
    else:
        print_error("Не удалось скачать выбранный трек ни с одного источника.")
        return False


def main():
    # Настройка UTF-8 для вывода в консоль на Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Multi-source Music Downloader (Deezer / Soulseek / YouTube Music / VK Музыка)"
    )
    parser.add_argument("url", nargs="*", help="URL of the track to download, search query or 'search <query>'")
    parser.add_argument("-s", "--search", action="store_true", help="Interactive multi-source search mode with selection")
    parser.add_argument(
        "-q", "--quality", choices=["MP3", "FLAC", "mp3", "flac"], default="MP3",
        help="Preferred download quality (default: MP3 320kbps)"
    )
    parser.add_argument(
        "-o", "--output", dest="dest_dir", type=str, default=None,
        help="Destination directory (default: ./downloads)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore the local cache and download the track again"
    )
    parser.add_argument(
        "--cache", dest="cache_action", choices=["info", "clear", "prune"], default=None,
        help="Show local cache statistics, clear it, or drop files unused for too long"
    )

    args = parser.parse_args()

    # Служебные команды работы с кэшем: показываем статистику или чистим.
    if args.cache_action:
        from core.filecache import get_file_cache

        fc = get_file_cache()
        if args.cache_action == "clear":
            removed = fc.clear()
            print_info(f"Кэш очищен, удалено файлов: {removed}")
        elif args.cache_action == "prune":
            days = fc.max_age_days()
            removed = fc.prune_expired()
            if days <= 0:
                print_info("Очистка по возрасту отключена (FILE_CACHE_MAX_AGE_DAYS=0).")
            else:
                print_info(f"Удалено файлов без запросов дольше {days} дн.: {removed}")
        else:
            stats = fc.stats()
            size_mb = stats.get("bytes", 0) / (1024 * 1024)
            limit_mb = fc.max_bytes() / (1024 * 1024) if fc.max_bytes() else 0
            print_info(
                f"Кэш: {stats.get('entries', 0)} треков, {size_mb:.1f} МБ"
                + (f" из {limit_mb:.0f} МБ" if limit_mb else "")
                + f"\nСтарее {fc.max_age_days()} дн. удаляются автоматически"
                + ("" if fc.max_age_days() else " (отключено)")
                + f"\nПуть: {stats.get('path', '?')}"
            )
        return

    reuse_cached_file = not args.force

    # Определение режима поиска и запроса
    is_search = bool(args.search)
    target_quality = args.quality.upper()
    dest_dir = Path(args.dest_dir) if args.dest_dir else None

    raw_args = list(args.url) if args.url else []
    if raw_args and raw_args[0].lower() in ("search", "s"):
        is_search = True
        raw_args = raw_args[1:]

    query = " ".join(raw_args).strip().strip("'\"")

    if not query:
        if is_search:
            try:
                query = input("Введите поисковый запрос: ").strip().strip("'\"")
            except EOFError:
                query = ""
            if not query:
                print_error("Запрос не может быть пустым.")
                sys.exit(1)
        else:
            print_banner()

            try:
                raw_input = input("Введите ссылку, поисковый запрос или 's <запрос>' для поиска: ").strip().strip("'\"")
            except EOFError:
                raw_input = ""

            if not raw_input:
                print_error("Ссылка или запрос не могут быть пустыми.")
                sys.exit(1)

            if raw_input.lower().startswith("s ") or raw_input.lower().startswith("search "):
                is_search = True
                query = raw_input.split(maxsplit=1)[1].strip().strip("'\"")
            else:
                query = raw_input

            try:
                choice = input("Предпочитаемое качество (1 - MP3 320kbps (рекомендуется), 2 - FLAC (Deezer / Soulseek)) [1]: ").strip().lower()
            except EOFError:
                choice = "1"
            if choice in ("2", "flac"):
                target_quality = "FLAC"
            else:
                target_quality = "MP3"

    try:
        if is_search:
            success = cli_search_and_download(
                query,
                target_quality=target_quality,
                dest_dir=dest_dir,
                reuse_cached_file=reuse_cached_file,
            )
            if not success:
                sys.exit(1)
        else:
            with RichDownloadProgress() as progress_bar:
                file_path = download_track_by_link(
                    query,
                    target_quality,
                    dest_dir=dest_dir,
                    progress_callback=progress_bar.update if is_interactive() else None,
                    reuse_cached_file=reuse_cached_file,
                )
            if file_path:
                print_success_panel(file_path)
            else:
                print_error("Не удалось скачать трек ни с одного источника.")
                sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем.")
        sys.exit(0)
    except Exception as e:
        print_error(f"Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
