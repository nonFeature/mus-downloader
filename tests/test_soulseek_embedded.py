import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import config
from sources.soulseek import parse_slskd_quality, search_soulseek, download_soulseek_track, EmbeddedSoulseek
from aioslsk.transfer.state import TransferState
from core import download_track_by_link


def test_parse_slskd_quality_snake_case_flac():
    # Test snake_case (aioslsk) format
    file_info = {
        "bit_depth": 24,
        "sample_rate": 96000,
        "bitrate": 3000,
        "duration": 210,
        "size": 50000000,
        "is_vbr": False,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.flac",
        file_info=file_info,
        target_quality="FLAC",
        expected_duration=210
    )
    assert "FLAC Hi-Res 24bit/96kHz" in label
    assert score > 2000


def test_parse_slskd_quality_snake_case_mp3():
    file_info = {
        "bitrate": 320,
        "duration": 180,
        "size": 7500000,
        "is_vbr": False,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.mp3",
        file_info=file_info,
        target_quality="MP3",
        expected_duration=180
    )
    assert "MP3 320 kbps CBR" in label
    assert score >= 450


def test_parse_slskd_quality_rejects_lossy_in_flac_mode():
    file_info = {
        "bitrate": 320,
        "duration": 180,
        "size": 7500000,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.mp3",
        file_info=file_info,
        target_quality="FLAC",
        expected_duration=180
    )
    assert label == "Lossy"
    assert score == 0.0


def test_parse_slskd_quality_duration_mismatch_penalty():
    file_info = {
        "bitrate": 320,
        "duration": 300,  # 100s difference
        "size": 7500000,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.mp3",
        file_info=file_info,
        target_quality="MP3",
        expected_duration=200
    )
    assert label == "Invalid Duration"
    assert score < 0


def test_search_soulseek_uses_embedded_instance():
    mock_es = MagicMock()
    mock_es.search.return_value = [
        {
            "username": "peer1",
            "filename": "Artist - Song.flac",
            "size": 30000000,
            "bit_depth": 16,
            "sample_rate": 44100,
            "bitrate": 900,
            "duration": 200,
            "has_free_slot": True,
            "upload_speed": 1000000,
            "queue_length": 0,
        }
    ]

    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es):
        results = search_soulseek(
            artist="Artist",
            title="Song",
            limit=5,
            target_quality="FLAC",
            duration=200
        )

        assert mock_es.search.called
        assert len(results) == 1
        assert results[0]["slskd_username"] == "peer1"
        assert results[0]["slskd_filename"] == "Artist - Song.flac"
        assert "FLAC 16bit/44kHz" in results[0]["quality"]


def test_download_soulseek_track_embedded_copies_file(tmp_path):
    mock_es = MagicMock()
    source_flac = tmp_path / "cache" / "Artist - Song.flac"
    source_flac.parent.mkdir(parents=True, exist_ok=True)
    source_flac.write_bytes(b"FLACDATA" * 50)

    mock_es.download.return_value = source_flac

    dest_dir = tmp_path / "downloads"
    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es):
        result = download_soulseek_track(
            username="peer1",
            filename="folder\\Artist - Song.flac",
            size=1000,
            dest_dir=dest_dir,
            target_quality="FLAC"
        )

        assert mock_es.download.called
        assert result == dest_dir / "Artist - Song.flac"
        assert result.exists()
        assert result.read_bytes() == b"FLACDATA" * 50


def test_download_soulseek_track_transcodes_flac_in_mp3_mode(tmp_path):
    mock_es = MagicMock()
    source_flac = tmp_path / "cache" / "Artist - Song.flac"
    source_flac.parent.mkdir(parents=True, exist_ok=True)
    source_flac.write_bytes(b"FLACDATA" * 50)

    mock_es.download.return_value = source_flac
    dest_dir = tmp_path / "downloads"

    def fake_ffmpeg(cmd, **kwargs):
        mp3_target = Path(cmd[-1])
        mp3_target.write_bytes(b"MP3DATA" * 2000)
        mock_res = MagicMock()
        mock_res.returncode = 0
        return mock_res

    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es), \
         patch("subprocess.run", side_effect=fake_ffmpeg):
        result = download_soulseek_track(
            username="peer1",
            filename="Artist - Song.flac",
            size=1000,
            dest_dir=dest_dir,
            target_quality="MP3"
        )

        assert result == dest_dir / "Artist - Song.mp3"
        assert result.exists()


def test_core_flac_uses_slsk_user_without_slskd_url(tmp_path):
    dummy_flac = tmp_path / "Artist - Track.flac"
    dummy_flac.write_bytes(b"flac_bytes" * 20)

    mock_meta = {
        "artist": "Artist",
        "title": "Track",
        "duration": 200,
    }

    slsk_cand = [{
        "slskd_username": "peer1",
        "slskd_filename": "Artist - Track.flac",
        "slskd_size": 20000000,
        "quality": "FLAC 16bit/44kHz",
    }]

    # SLSKD_URL is empty, but SLSK_USER is set
    with patch.object(config, "SLSKD_URL", ""), \
         patch.object(config, "SLSK_USER", "test_user"), \
         patch("core.metadata.resolve_query_metadata", return_value=mock_meta), \
         patch("core.search_soulseek", return_value=slsk_cand) as mock_search, \
         patch("core.download_soulseek_track", return_value=dummy_flac) as mock_dl, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("Artist - Track", target_quality="FLAC")
        assert mock_search.called
        assert mock_dl.called
        assert res == dummy_flac


def test_download_soulseek_track_concurrent_threads(tmp_path):
    """Проверяет параллельное скачивание нескольких треков в разных потоках."""
    mock_es = MagicMock()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = tmp_path / "downloads"

    def fake_download(username, filename, timeout=120.0):
        time.sleep(0.05)
        name = Path(filename).name
        file_path = cache_dir / f"{username}_{name}"
        file_path.write_bytes(f"DATA_{username}_{name}".encode())
        return file_path

    mock_es.download.side_effect = fake_download
    mock_es.staging_dir = None

    tracks = [
        ("peer1", "folder1/Track 01.flac", 1000),
        ("peer2", "folder2/Track 02.flac", 2000),
        ("peer3", "folder3/Track 03.flac", 3000),
        ("peer4", "folder4/Track 04.flac", 4000),
    ]

    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    download_soulseek_track,
                    username=u,
                    filename=fn,
                    size=s,
                    dest_dir=dest_dir,
                    target_quality="FLAC"
                )
                for u, fn, s in tracks
            ]
            results = [f.result() for f in futures]

    assert len(results) == 4
    for idx, (u, fn, _) in enumerate(tracks):
        name = Path(fn).name
        assert results[idx] == dest_dir / name
        assert results[idx].exists()
        assert results[idx].read_bytes() == f"DATA_{u}_{name}".encode()


def test_embedded_soulseek_concurrent_downloads_different_files(tmp_path):
    """Проверяет работу EmbeddedSoulseek._async_download при параллельных скачиваниях разных файлов."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._connected = threading.Event()
        es._connected.set()

        mock_client = MagicMock()
        transfers_map = {}

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 1000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.QUEUED

            async def abort(self):
                self.state.VALUE = TransferState.State.ABORTED

        async def fake_download(username, filename):
            t = FakeTransfer(username, filename)
            transfers_map[(username, filename)] = t
            return t

        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        def run_dl(u, fn):
            return es.download(u, fn, timeout=5.0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(run_dl, "peer1", "folder1\\song1.flac")
            f2 = executor.submit(run_dl, "peer2", "folder2\\song2.flac")

            for _ in range(50):
                if len(transfers_map) >= 2:
                    break
                time.sleep(0.05)

            t1 = transfers_map[("peer1", "folder1\\song1.flac")]
            t2 = transfers_map[("peer2", "folder2\\song2.flac")]

            # Убедимся, что local_path изолированы в разных staging поддиректориях
            assert t1.local_path != t2.local_path
            assert ".staging" in t1.local_path
            assert ".staging" in t2.local_path

            Path(t1.local_path).write_bytes(b"SONG1")
            Path(t2.local_path).write_bytes(b"SONG2")

            t1.state.VALUE = TransferState.State.COMPLETE
            t2.state.VALUE = TransferState.State.COMPLETE

            res1 = f1.result(timeout=5)
            res2 = f2.result(timeout=5)

            assert res1 is not None and res1.exists()
            assert res1.read_bytes() == b"SONG1"
            assert res2 is not None and res2.exists()
            assert res2.read_bytes() == b"SONG2"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_concurrent_downloads_same_file(tmp_path):
    """Проверяет, что два параллельных запроса на один и тот же файл используют одну активную загрузку."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._connected = threading.Event()
        es._connected.set()

        mock_client = MagicMock()
        fake_transfer = None

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 1000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.QUEUED

            async def abort(self):
                self.state.VALUE = TransferState.State.ABORTED

        async def fake_download(username, filename):
            nonlocal fake_transfer
            fake_transfer = FakeTransfer(username, filename)
            return fake_transfer

        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        def run_dl():
            return es.download("peer1", "folder\\same_song.flac", timeout=5.0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(run_dl)
            time.sleep(0.05)
            f2 = executor.submit(run_dl)

            for _ in range(50):
                if fake_transfer is not None and fake_transfer.local_path:
                    break
                time.sleep(0.05)

            # download у клиента должен быть вызван ровно 1 раз
            assert mock_client.transfers.download.call_count == 1

            Path(fake_transfer.local_path).write_bytes(b"SAME_SONG_DATA")
            fake_transfer.state.VALUE = TransferState.State.COMPLETE

            res1 = f1.result(timeout=5)
            res2 = f2.result(timeout=5)

            assert res1 is not None and res1.exists()
            assert res2 is not None and res2.exists()
            assert res1.read_bytes() == b"SAME_SONG_DATA"
            assert res2.read_bytes() == b"SAME_SONG_DATA"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_peer_busy_does_not_timeout_queue(tmp_path):
    """Проверяет, что если пир активно передает нам другой трек, очередь для следующего трека не сбрасывается через 15 сек."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._connected = threading.Event()
        es._connected.set()

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 1000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.QUEUED

            async def abort(self):
                self.state.VALUE = TransferState.State.ABORTED

        transfers = {}
        async def fake_download(username, filename):
            t = FakeTransfer(username, filename)
            transfers[filename] = t
            return t

        mock_client = MagicMock()
        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        future1 = asyncio.run_coroutine_threadsafe(
            es._async_download("busy_peer", "track1.flac", timeout=5.0),
            loop
        )
        time.sleep(0.05)
        future2 = asyncio.run_coroutine_threadsafe(
            es._async_download("busy_peer", "track2.flac", timeout=5.0),
            loop
        )

        for _ in range(50):
            if len(transfers) >= 2:
                break
            time.sleep(0.05)

        t1 = transfers["track1.flac"]
        t2 = transfers["track2.flac"]

        # track1 качается (DOWNLOADING), track2 ждет в очереди (QUEUED)
        t1.state.VALUE = TransferState.State.DOWNLOADING
        t2.state.VALUE = TransferState.State.QUEUED

        time.sleep(1.5)

        # track2 НЕ должен быть отменен/абортирован, так как busy_peer занят отдачей track1
        assert t2.state.VALUE == TransferState.State.QUEUED

        # Завершаем track1 и track2
        Path(t1.local_path).write_bytes(b"TRACK1")
        Path(t2.local_path).write_bytes(b"TRACK2")
        t1.state.VALUE = TransferState.State.COMPLETE
        t2.state.VALUE = TransferState.State.COMPLETE

        res1 = future1.result(timeout=5)
        res2 = future2.result(timeout=5)
        assert res1 is not None and res1.exists()
        assert res2 is not None and res2.exists()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_concurrent_searches():
    """Проверяет изоляцию результатов одновременных поисков по разным тикетам."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.loop = loop
        es._connected = threading.Event()
        es._connected.set()

        registered_listeners = []

        class FakeEvents:
            def register(self, ev_cls, cb):
                registered_listeners.append(cb)
            def unregister(self, ev_cls, cb):
                if cb in registered_listeners:
                    registered_listeners.remove(cb)

        class FakeReq:
            def __init__(self, ticket):
                self.ticket = ticket

        class FakeSearches:
            def __init__(self):
                self._cur = 100
            async def search(self, query):
                self._cur += 1
                return FakeReq(self._cur)
            def remove_request(self, req):
                pass

        mock_client = MagicMock()
        mock_client.events = FakeEvents()
        mock_client.searches = FakeSearches()
        es.client = mock_client

        def do_search1():
            return es.search("query1", timeout=1.0)
        def do_search2():
            return es.search("query2", timeout=1.0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(do_search1)
            fut2 = executor.submit(do_search2)

            time.sleep(0.1)

            # Создаем два события с разными тикетами:
            item1 = MagicMock()
            item1.filename = "song_query1.flac"
            item1.filesize = 123
            item1.get_attribute_map.return_value = {}
            res1 = MagicMock()
            res1.username = "peer1"
            res1.shared_items = [item1]
            res1.has_free_slots = True
            res1.avg_speed = 100
            res1.queue_size = 0
            ev1 = MagicMock()
            ev1.query.ticket = 101
            ev1.result = res1

            item2 = MagicMock()
            item2.filename = "song_query2.flac"
            item2.filesize = 456
            item2.get_attribute_map.return_value = {}
            res2 = MagicMock()
            res2.username = "peer2"
            res2.shared_items = [item2]
            res2.has_free_slots = True
            res2.avg_speed = 200
            res2.queue_size = 0
            ev2 = MagicMock()
            ev2.query.ticket = 102
            ev2.result = res2

            for listener in list(registered_listeners):
                asyncio.run_coroutine_threadsafe(listener(ev1), loop)
                asyncio.run_coroutine_threadsafe(listener(ev2), loop)

            r1 = fut1.result(timeout=3)
            r2 = fut2.result(timeout=3)

            assert len(r1) == 1
            assert r1[0]["filename"] == "song_query1.flac"

            assert len(r2) == 1
            assert r2[0]["filename"] == "song_query2.flac"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_caller_short_timeout_does_not_abort_longer_subscriber(tmp_path):
    """
    Проверяет, что таймаут первого подписчика НЕ прерывает загрузку для второго подписчика с большим таймаутом.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._staging_refs = {}
        es._staging_lock = threading.Lock()
        es._connected = threading.Event()
        es._connected.set()

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 5000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.DOWNLOADING
            async def abort(self):
                self.state.VALUE = TransferState.State.ABORTED

        fake_transfer = None
        async def fake_download(u, f):
            nonlocal fake_transfer
            fake_transfer = FakeTransfer(u, f)
            return fake_transfer

        mock_client = MagicMock()
        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            # Первый поток с коротким таймаутом (0.2s)
            f1 = executor.submit(es.download, "peer1", "album\\song.flac", timeout=0.2)
            time.sleep(0.05)
            # Второй поток с длинным таймаутом (4.0s)
            f2 = executor.submit(es.download, "peer1", "album\\song.flac", timeout=4.0)

            # Ждем истечения таймаута первого потока
            res1 = f1.result(timeout=2.0)
            assert res1 is None  # Первый поток отвалился по таймауту

            # Убеждаемся, что transfer НЕ был абортирован
            assert fake_transfer is not None
            assert fake_transfer.state.VALUE == TransferState.State.DOWNLOADING

            # Завершаем загрузку для оставшегося второго потока
            Path(fake_transfer.local_path).write_bytes(b"COMPLETED_DATA")
            fake_transfer.state.VALUE = TransferState.State.COMPLETE

            res2 = f2.result(timeout=4.0)
            assert res2 is not None
            assert res2.exists()
            assert res2.read_bytes() == b"COMPLETED_DATA"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_download_soulseek_track_concurrent_same_file_staging_refcount(tmp_path):
    """
    Проверяет, что при одновременном скачивании одного трека двумя потоками
    очистка staging файла первым потоком не удаляет файл у второго потока (refcounting).
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._staging_refs = {}
        es._staging_lock = threading.Lock()
        es._connected = threading.Event()
        es._connected.set()

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 1000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.QUEUED
            async def abort(self):
                self.state.VALUE = TransferState.State.ABORTED

        fake_transfer = None
        async def fake_download(u, f):
            nonlocal fake_transfer
            fake_transfer = FakeTransfer(u, f)
            return fake_transfer

        mock_client = MagicMock()
        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        dest_dir = tmp_path / "final_dest"
        dest_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(EmbeddedSoulseek, "get_instance", return_value=es):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(
                    download_soulseek_track,
                    username="peer1",
                    filename="song.flac",
                    size=1000,
                    dest_dir=dest_dir,
                    target_quality="FLAC"
                )
                time.sleep(0.05)
                f2 = executor.submit(
                    download_soulseek_track,
                    username="peer1",
                    filename="song.flac",
                    size=1000,
                    dest_dir=dest_dir,
                    target_quality="FLAC"
                )

                while fake_transfer is None or not fake_transfer.local_path:
                    time.sleep(0.01)

                # Записываем данные в staging
                staging_path = Path(fake_transfer.local_path)
                staging_path.write_bytes(b"FLAC_AUDIO_CONTENT")
                fake_transfer.state.VALUE = TransferState.State.COMPLETE

                res1 = f1.result(timeout=5)
                res2 = f2.result(timeout=5)

                assert res1 == dest_dir / "song.flac"
                assert res2 == dest_dir / "song.flac"
                assert res1.exists()
                assert res1.read_bytes() == b"FLAC_AUDIO_CONTENT"

                # После завершения обоих потоков staging файл должен быть удален (refcount = 0)
                assert not staging_path.exists()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_preserves_remote_path_forward_slashes(tmp_path):
    """
    Проверяет, что remote_path с прямыми слешами (Linux/macOS пиры)
    передается в aioslsk без искажения (не заменяется на обратные слеши).
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._staging_refs = {}
        es._staging_lock = threading.Lock()
        es._connected = threading.Event()
        es._connected.set()

        requested_remote_paths = []
        created_transfer = None

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 1000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.QUEUED
            async def abort(self):
                pass

        async def fake_download(u, f):
            nonlocal created_transfer
            requested_remote_paths.append(f)
            created_transfer = FakeTransfer(u, f)
            return created_transfer

        mock_client = MagicMock()
        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        linux_path = "LinuxUser/Music/Rock/01 - Song.flac"

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            fut = executor.submit(es.download, "linux_peer", linux_path, timeout=5.0)

            while created_transfer is None or not created_transfer.local_path:
                time.sleep(0.01)

            # В aioslsk сетевой поток пишет в transfer.local_path
            Path(created_transfer.local_path).write_bytes(b"LINUX_PEER_DATA")
            created_transfer.state.VALUE = TransferState.State.COMPLETE

            res = fut.result(timeout=5.0)

        assert len(requested_remote_paths) == 1
        # Исходный remote_path с прямыми слешами сохранен для Soulseek protocol
        assert requested_remote_paths[0] == linux_path
        assert res is not None
        assert res.name == "01 - Song.flac"
        assert res.read_bytes() == b"LINUX_PEER_DATA"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_concurrent_searches_strict_ticket_isolation():
    """
    Проверяет, что при одновременных поисках чужие тикеты и результаты,
    пришедшие до назначения target_ticket, строго игнорируются.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.loop = loop
        es._connected = threading.Event()
        es._connected.set()

        registered_listeners = []
        class FakeEvents:
            def register(self, ev_cls, cb):
                registered_listeners.append(cb)
            def unregister(self, ev_cls, cb):
                if cb in registered_listeners:
                    registered_listeners.remove(cb)

        class FakeReq:
            def __init__(self, ticket):
                self.ticket = ticket
                self.results = []

        class FakeSearches:
            def __init__(self):
                self.cur = 500
            async def search(self, query):
                # Имитируем небольшую задержку регистрации поиска в сети
                await asyncio.sleep(0.05)
                self.cur += 1
                return FakeReq(self.cur)
            def remove_request(self, req):
                pass

        mock_client = MagicMock()
        mock_client.events = FakeEvents()
        mock_client.searches = FakeSearches()
        es.client = mock_client

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            fut = executor.submit(es.search, "artist song", timeout=1.0)

            # Пока search еще выполняется и target_ticket не установлен:
            # шлем чужое событие с ticket=999
            time.sleep(0.01)
            alien_item = MagicMock()
            alien_item.filename = "alien_song.flac"
            alien_item.filesize = 999
            alien_item.get_attribute_map.return_value = {}
            alien_res = MagicMock()
            alien_res.username = "alien_peer"
            alien_res.shared_items = [alien_item]
            alien_res.has_free_slots = True
            alien_res.avg_speed = 100
            alien_res.queue_size = 0
            alien_ev = MagicMock()
            alien_ev.query.ticket = 999
            alien_ev.result = alien_res

            for listener in list(registered_listeners):
                asyncio.run_coroutine_threadsafe(listener(alien_ev), loop)

            # Теперь отправляем легитимный ответ с ticket=501
            time.sleep(0.1)
            valid_item = MagicMock()
            valid_item.filename = "correct_song.flac"
            valid_item.filesize = 777
            valid_item.get_attribute_map.return_value = {}
            valid_res = MagicMock()
            valid_res.username = "good_peer"
            valid_res.shared_items = [valid_item]
            valid_res.has_free_slots = True
            valid_res.avg_speed = 500
            valid_res.queue_size = 0
            valid_ev = MagicMock()
            valid_ev.query.ticket = 501
            valid_ev.result = valid_res

            for listener in list(registered_listeners):
                asyncio.run_coroutine_threadsafe(listener(valid_ev), loop)

            results = fut.result(timeout=3)
            # Чужой результат не должен был попасть в результаты
            assert len(results) == 1
            assert results[0]["filename"] == "correct_song.flac"
            assert results[0]["username"] == "good_peer"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_embedded_soulseek_peer_busy_initializing_state_prevents_timeout(tmp_path):
    """
    Проверяет, что состояние INITIALIZING на параллельном треке того же пира
    также считается занятостью пира и не сбрасывает очередь по таймауту.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        es = EmbeddedSoulseek.__new__(EmbeddedSoulseek)
        es.download_dir = tmp_path / "downloads"
        es.staging_dir = es.download_dir / ".staging"
        es.staging_dir.mkdir(parents=True, exist_ok=True)
        es.loop = loop
        es._active_downloads = {}
        es._staging_refs = {}
        es._staging_lock = threading.Lock()
        es._connected = threading.Event()
        es._connected.set()

        class FakeTransfer:
            def __init__(self, username, filename):
                self.username = username
                self.filename = filename
                self.local_path = None
                self.filesize = 1000
                self.bytes_transfered = 0
                self.state = MagicMock()
                self.state.VALUE = TransferState.State.QUEUED
            async def abort(self):
                self.state.VALUE = TransferState.State.ABORTED

        transfers = {}
        async def fake_download(username, filename):
            t = FakeTransfer(username, filename)
            transfers[filename] = t
            return t

        mock_client = MagicMock()
        mock_client.transfers.download = AsyncMock(side_effect=fake_download)
        mock_client.transfers.remove = AsyncMock()
        es.client = mock_client

        future1 = asyncio.run_coroutine_threadsafe(
            es._async_download("busy_peer", "track1.flac", timeout=5.0),
            loop
        )
        time.sleep(0.05)
        future2 = asyncio.run_coroutine_threadsafe(
            es._async_download("busy_peer", "track2.flac", timeout=5.0),
            loop
        )

        for _ in range(50):
            if len(transfers) >= 2:
                break
            time.sleep(0.05)

        t1 = transfers["track1.flac"]
        t2 = transfers["track2.flac"]

        # track1 инициализирует соединение (INITIALIZING), track2 ждет в очереди (QUEUED)
        t1.state.VALUE = TransferState.State.INITIALIZING
        t2.state.VALUE = TransferState.State.QUEUED

        time.sleep(1.2)
        # Очередь не должна быть отменена
        assert t2.state.VALUE == TransferState.State.QUEUED

        Path(t1.local_path).write_bytes(b"DATA1")
        Path(t2.local_path).write_bytes(b"DATA2")
        t1.state.VALUE = TransferState.State.COMPLETE
        t2.state.VALUE = TransferState.State.COMPLETE

        res1 = future1.result(timeout=5)
        res2 = future2.result(timeout=5)
        assert res1 is not None and res1.exists()
        assert res2 is not None and res2.exists()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
