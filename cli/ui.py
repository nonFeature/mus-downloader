import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    from rich.style import Style
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# Основная консоль для вывода
console = Console() if RICH_AVAILABLE else None


def is_interactive() -> bool:
    """Проверяет, запущен ли CLI в интерактивном терминале (не пайп)."""
    return sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False


def print_banner():
    """Выводит стильный баннер приветствия."""
    if not RICH_AVAILABLE or not is_interactive():
        print("=== Multi-source Music Downloader ===")
        print("Источники: Deezer (320k CBR / FLAC) + Soulseek (P2P Lossless) + YouTube Music + VK Музыка + SoundCloud.")
        print("Поддерживаемые ссылки: YouTube, Spotify, Apple Music, Deezer, SoundCloud, VK, Last.fm, Яндекс.Музыка.\n")
        return

    body = (
        "[bold cyan]Источники:[/bold cyan] "
        "[green]Deezer (320k CBR / FLAC)[/green] • "
        "[magenta]Soulseek (P2P Lossless)[/magenta] • "
        "[red]YouTube Music[/red] • "
        "[blue]VK Музыка[/blue] • "
        "[yellow]SoundCloud[/yellow]\n"
        "[dim]Поддерживаемые ссылки: YouTube, Spotify, Apple Music, Deezer, SoundCloud, VK, Last.fm, Яндекс.Музыка[/dim]\n"
        "[dim]Режим мульти-поиска: [bold]dl search <запрос>[/bold] или флаг [bold]-s[/bold][/dim]"
    )
    console.print(
        Panel(
            body,
            title="[bold yellow]🎵 Multi-source Music Downloader[/bold yellow]",
            border_style="bright_blue",
            padding=(1, 2),
            box=box.ROUNDED,
        )
    )


def print_search_table(candidates: List[Dict[str, Any]], query: str):
    """Выводит форматированную таблицу кандидатов поиска."""
    if not RICH_AVAILABLE or not is_interactive():
        print(f"\nНайдено треков: {len(candidates)}")
        for i, c in enumerate(candidates, 1):
            artist = c.get("artist") or "Unknown Artist"
            title = c.get("title") or "Unknown Track"
            alb = c.get("album") or ""
            yr = f" ({c.get('year')})" if c.get("year") else ""
            dur = f" [{c.get('duration')}s]" if c.get("duration") else ""
            sq = c.get("source_quality") or c.get("source") or "Web"
            print(f"  [{i}] {artist} - {title}{yr}{dur} [{sq}]")
        print("  [0] Отмена\n")
        return

    table = Table(
        title=f"🔎 [bold cyan]Результаты поиска для:[/bold cyan] [bold yellow]\"{query}\"[/bold yellow]",
        box=box.ROUNDED,
        header_style="bold magenta",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("#", justify="center", style="bold cyan", width=4)
    table.add_column("Исполнитель и Трек", style="bold white", min_width=26)
    table.add_column("Альбом", style="dim italic", min_width=18)
    table.add_column("Год", justify="center", style="yellow", width=6)
    table.add_column("Длит.", justify="center", style="green", width=7)
    table.add_column("Качество / Источник", style="bold", min_width=20)

    for idx, cand in enumerate(candidates, 1):
        artist = cand.get("artist") or "Unknown Artist"
        title = cand.get("title") or "Unknown Track"
        explicit = " [bold red][E][/bold red]" if cand.get("explicit") else ""
        track_display = f"{artist} — [bold]{title}[/bold]{explicit}"

        album = cand.get("album") or "—"
        year = str(cand.get("year") or "—")

        duration_sec = cand.get("duration")
        if duration_sec:
            m = int(duration_sec) // 60
            s = int(duration_sec) % 60
            dur_str = f"{m}:{s:02d}"
        else:
            dur_str = "—"

        sq = cand.get("source_quality") or cand.get("source") or "Web"
        if "FLAC" in sq or "Lossless" in sq:
            quality_display = f"[bold green]{sq}[/bold green]"
        elif "320" in sq or "Apple" in sq:
            quality_display = f"[bold cyan]{sq}[/bold cyan]"
        elif "YouTube" in sq:
            quality_display = f"[red]{sq}[/red]"
        else:
            quality_display = sq

        table.add_row(str(idx), track_display, album, year, dur_str, quality_display)

    console.print(table)
    console.print("[dim]  [bold red]0[/bold red] — Отмена[/dim]\n")


def print_track_panel(artist: str, title: str, quality: str, album: Optional[str] = None, year: Optional[str] = None, duration: Optional[float] = None):
    """Выводит информационную карточку выбранного трека перед скачиванием."""
    if not RICH_AVAILABLE or not is_interactive():
        parts = [f"[*] Трек: {artist} - {title}"]
        if year:
            parts.append(f"({year})")
        if duration:
            parts.append(f"[{round(duration)}s]")
        parts.append(f"[{quality}]")
        print(" ".join(parts))
        return

    details = []
    if album:
        details.append(f"[dim]Альбом:[/dim] [italic]{album}[/italic]")
    if year:
        details.append(f"[dim]Год:[/dim] [yellow]{year}[/yellow]")
    if duration:
        m = int(duration) // 60
        s = int(duration) % 60
        details.append(f"[dim]Хронометраж:[/dim] [green]{m}:{s:02d}[/green]")

    details_line = "  •  ".join(details) if details else ""
    content = f"[bold white]{artist}[/bold white] — [bold cyan]{title}[/bold cyan]\n"
    if details_line:
        content += f"{details_line}\n"
    content += f"[dim]Целевой формат:[/dim] [bold {'green' if quality == 'FLAC' else 'yellow'}]{quality}[/bold {'green' if quality == 'FLAC' else 'yellow'}]"

    console.print(
        Panel(
            content,
            title="[bold green]🎧 Выбранный трек[/bold green]",
            border_style="green",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )


def print_success_panel(file_path: Path):
    """Выводит плашку успешного скачивания и сохранения трека."""
    if not RICH_AVAILABLE or not is_interactive():
        print(f"[+] Сохранено: {file_path.resolve()}")
        return

    size_mb = f"{file_path.stat().st_size / (1024 * 1024):.1f} MB" if file_path.exists() else ""
    body = (
        f"[bold green]✔ Файл успешно сохранен![/bold green]\n"
        f"[bold white]{file_path.name}[/bold white] [dim]({size_mb})[/dim]\n"
        f"[dim]Путь:[/dim] [underline]{file_path.resolve()}[/underline]"
    )
    console.print(
        Panel(
            body,
            title="[bold green]Готово[/bold green]",
            border_style="bright_green",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )


def print_error(msg: str):
    """Выводит форматированное сообщение об ошибке."""
    if not RICH_AVAILABLE or not is_interactive():
        print(f"[!] {msg}")
        return
    console.print(f"[bold red][!][/bold red] [red]{msg}[/red]")


def print_info(msg: str):
    """Выводит информационное сообщение."""
    if not RICH_AVAILABLE or not is_interactive():
        print(f"[*] {msg}")
        return
    console.print(f"[bold cyan][*][/bold cyan] {msg}")


class RichDownloadProgress:
    """Управляет динамическим прогресс-баром Rich при скачивании в CLI."""

    def __init__(self, disabled: Optional[bool] = None):
        if disabled is not None:
            self.disabled = disabled
        else:
            self.disabled = not RICH_AVAILABLE or not is_interactive()
        self._progress = None
        self._task_id = None
        self._is_byte_mode = False

    def __enter__(self):
        if not self.disabled:
            from rich.progress import (
                Progress,
                SpinnerColumn,
                TextColumn,
                BarColumn,
                TaskProgressColumn,
                DownloadColumn,
                TransferSpeedColumn,
                TimeRemainingColumn,
            )
            self._progress = Progress(
                SpinnerColumn("dots", style="bold cyan"),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(bar_width=None, complete_style="green", finished_style="bold green"),
                TaskProgressColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=False,
            )
            self._progress.start()
            self._task_id = self._progress.add_task("[dim]Подготовка к скачиванию...[/dim]", total=None)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._progress:
            try:
                self._progress.stop()
            except Exception:
                pass

    def update(self, data: Dict[str, Any]):
        """Принимает словарь с событиями процесса загрузки и обновляет прогресс."""
        if self.disabled or not self._progress or self._task_id is None:
            return

        desc = data.get("description")
        stage = data.get("stage")
        total = data.get("total_bytes")
        downloaded = data.get("downloaded_bytes")
        percent = data.get("percent")

        if stage == "status":
            if desc:
                stripped = desc.strip()
                if not stripped:
                    return
                if stripped.startswith("[*]"):
                    clean = stripped[3:].strip()
                    self._progress.update(self._task_id, description=f"[bold cyan]{clean}[/bold cyan]")
                elif stripped.startswith("[+]"):
                    clean = stripped[3:].strip()
                    self._progress.console.print(f"[bold green][+][/bold green] [green]{clean}[/green]")
                elif stripped.startswith("[!]"):
                    clean = stripped[3:].strip()
                    self._progress.console.print(f"[bold yellow][!][/bold yellow] [yellow]{clean}[/yellow]")
                elif stripped.startswith("[-]"):
                    clean = stripped[3:].strip()
                    self._progress.console.print(f"[bold red][-][/bold red] [red]{clean}[/red]")
                else:
                    self._progress.update(self._task_id, description=f"[bold cyan]{stripped}[/bold cyan]")
            return

        if total and total > 0 and downloaded is not None:
            self._is_byte_mode = True
            clean_desc = desc or "Скачивание аудиопотока..."
            self._progress.update(
                self._task_id,
                total=total,
                completed=downloaded,
                description=f"[bold green]{clean_desc}[/bold green]",
            )
        elif percent is not None and not self._is_byte_mode:
            clean_desc = desc or "Обработка трека..."
            self._progress.update(
                self._task_id,
                total=100,
                completed=percent,
                description=f"[bold magenta]{clean_desc}[/bold magenta]",
            )
        elif stage == "done":
            clean_desc = desc or "Готово!"
            if self._is_byte_mode and total:
                self._progress.update(
                    self._task_id,
                    completed=total,
                    description=f"[bold green]{clean_desc}[/bold green]",
                )
            else:
                self._progress.update(
                    self._task_id,
                    total=100,
                    completed=100,
                    description=f"[bold green]{clean_desc}[/bold green]",
                )
        elif desc:
            self._progress.update(
                self._task_id,
                description=f"[bold cyan]{desc}[/bold cyan]",
            )

