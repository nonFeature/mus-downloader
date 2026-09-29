import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli.ui import RichDownloadProgress
from core import download_track_by_link
import core.sources.youtube as youtube_source
import core.sources.deezer as deezer_source


def test_rich_download_progress_disabled():
    """Проверка работы RichDownloadProgress в отключенном режиме."""
    progress = RichDownloadProgress(disabled=True)
    assert progress.disabled is True

    with progress as p:
        p.update({"stage": "status", "description": "[*] Тестовый статус"})
        p.update({"stage": "download", "downloaded_bytes": 100, "total_bytes": 1000})
        p.update({"stage": "done"})


def test_rich_download_progress_status_formatting():
    """Проверка форматирования различных типов статусов ([*], [+], [!], [-])."""
    p = RichDownloadProgress(disabled=False)
    p._progress = MagicMock()
    p._task_id = 1

    # [*] Обновление описания
    p.update({"stage": "status", "description": "[*] Скачивание трека..."})
    p._progress.update.assert_called_with(1, description="[bold cyan]Скачивание трека...[/bold cyan]")

    # [+] Успешное сообщение в консоль
    p.update({"stage": "status", "description": "[+] Готово 100%"})
    p._progress.console.print.assert_called()

    # [!] Предупреждение
    p.update({"stage": "status", "description": "[!] Фолбек на другой источник"})
    p._progress.console.print.assert_called()

    # [-] Ошибка
    p.update({"stage": "status", "description": "[-] Не удалось скачать"})
    p._progress.console.print.assert_called()


def test_rich_download_progress_byte_and_percent_updates():
    """Проверка обновления байтов и процентов."""
    p = RichDownloadProgress(disabled=False)
    p._progress = MagicMock()
    p._task_id = 1

    # Обновление байтов
    p.update({
        "stage": "download",
        "description": "YouTube: скачивание...",
        "downloaded_bytes": 5000,
        "total_bytes": 10000,
        "speed": 2500,
        "eta": 2,
    })
    p._progress.update.assert_called_with(
        1,
        total=10000,
        completed=5000,
        description="[bold green]YouTube: скачивание...[/bold green]",
    )

    # Обновление процентов
    p._is_byte_mode = False
    p.update({
        "stage": "transcode",
        "description": "Конвертация в MP3...",
        "percent": 90,
    })
    p._progress.update.assert_called_with(
        1,
        total=100,
        completed=90,
        description="[bold magenta]Конвертация в MP3...[/bold magenta]",
    )

    # Завершение
    p.update({
        "stage": "done",
        "description": "Успешно!",
    })
    p._progress.update.assert_called_with(
        1,
        total=100,
        completed=100,
        description="[bold green]Успешно![/bold green]",
    )


def test_download_track_by_link_passes_progress(tmp_path: Path):
    """Проверка вызова progress_callback во время download_track_by_link."""
    events = []

    def on_progress(data: dict):
        events.append(data)

    dummy_meta = {
        "artist": "Test Artist",
        "title": "Test Title",
        "deezer_id": "12345",
        "duration": 200,
    }
    dummy_file = tmp_path / "Test Artist - Test Title.mp3"
    dummy_file.write_bytes(b"dummy mp3 data")

    with patch("core.metadata.resolve_query_metadata", return_value=dummy_meta), \
         patch("core.download_deezer_track", return_value=dummy_file) as mock_dl, \
         patch("core.tagger.apply_metadata", return_value=None):

        res = download_track_by_link(
            "Test Artist - Test Title",
            target_quality="MP3",
            dest_dir=tmp_path,
            progress_callback=on_progress,
        )

        assert res == dummy_file
        assert mock_dl.called
        # Проверяем, что mock_dl получил progress_callback
        _, kwargs = mock_dl.call_args
        assert kwargs.get("progress_callback") == on_progress

        # Проверяем этапы в событиях
        stages = [e.get("stage") for e in events]
        assert "search" in stages
        assert "tagging" in stages
        assert "done" in stages


def test_download_youtube_track_progress_hook(tmp_path: Path):
    """Проверка регистрации progress_hooks в download_youtube_track."""
    events = []

    def on_progress(data: dict):
        events.append(data)

    mock_ydl_instance = MagicMock()
    mock_info = {"id": "vid123"}
    mock_ydl_instance.extract_info.return_value = mock_info
    mock_ydl_instance.prepare_filename.return_value = str(tmp_path / "temp_vid123.webm")

    # Создаем временный файл
    (tmp_path / "temp_vid123.webm").write_bytes(b"dummy webm")

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls, \
         patch("core.sources.youtube._detect_audio_info", return_value=("opus", 160)), \
         patch("core.sources.youtube._transcode_to_mp3", return_value=True):

        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance

        # Создаем целевой файл
        final_mp3 = tmp_path / "Artist - Title.mp3"
        final_mp3.write_bytes(b"dummy mp3")

        res = youtube_source.download_youtube_track(
            artist="Artist",
            title="Title",
            dest_dir=tmp_path,
            direct_url="https://www.youtube.com/watch?v=vid123",
            progress_callback=on_progress,
        )

        assert res is not None
        # Проверяем, что progress_hooks был зарегистрирован в ydl_opts
        ydl_opts_passed = mock_ydl_cls.call_args[0][0]
        assert "progress_hooks" in ydl_opts_passed
        assert len(ydl_opts_passed["progress_hooks"]) == 1

        # Вызываем хук вручную для симуляции загрузки
        hook = ydl_opts_passed["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 4096, "speed": 1000, "eta": 3})
        hook({"status": "finished"})

        stages = [e.get("stage") for e in events]
        assert "download" in stages
        assert "transcode" in stages
        assert "done" in stages
