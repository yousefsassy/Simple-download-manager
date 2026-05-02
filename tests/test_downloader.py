from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sdm.downloader import DownloadManager
from sdm.models import DownloadStatus, SegmentStatus
from sdm.storage import Storage


DATA = (b"0123456789abcdef" * 65536)[:1024 * 1024]


class DownloadTestHandler(BaseHTTPRequestHandler):
    data = DATA
    supports_ranges = True
    delay = 0.0
    fail_first_get = False
    fail_all_get = False
    failed_once = False

    def log_message(self, format: str, *args) -> None:
        return

    def do_HEAD(self) -> None:
        self._send_headers(200, len(self.data))

    def do_GET(self) -> None:
        if self.fail_all_get:
            self.send_response(503)
            self.end_headers()
            return

        if self.fail_first_get and not self.__class__.failed_once:
            self.__class__.failed_once = True
            self.send_response(503)
            self.end_headers()
            return

        range_header = self.headers.get("Range")
        if self.supports_ranges and range_header:
            start, end = self._parse_range(range_header)
            chunk = self.data[start : end + 1]
            self._send_headers(206, len(chunk), start, end)
            self._write_body(chunk)
            return

        self._send_headers(200, len(self.data))
        self._write_body(self.data)

    def _parse_range(self, value: str) -> tuple[int, int]:
        unit, bounds = value.split("=", 1)
        assert unit == "bytes"
        start_text, end_text = bounds.split("-", 1)
        start = int(start_text)
        end = int(end_text) if end_text else len(self.data) - 1
        return start, min(end, len(self.data) - 1)

    def _send_headers(
        self,
        status: int,
        length: int,
        start: int | None = None,
        end: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        if self.supports_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if start is not None and end is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.data)}")
        self.end_headers()

    def _write_body(self, body: bytes) -> None:
        for index in range(0, len(body), 8192):
            self.wfile.write(body[index : index + 8192])
            if self.delay:
                time.sleep(self.delay)


@contextmanager
def download_server(**options):
    handler = type("Handler", (DownloadTestHandler,), options | {"failed_once": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture.bin"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def make_manager(tmp_path: Path, max_active_downloads: int = 2) -> DownloadManager:
    return DownloadManager(
        Storage(tmp_path / "sdm.sqlite3"),
        default_save_dir=tmp_path / "downloads",
        default_segments=4,
        max_active_downloads=max_active_downloads,
        chunk_size=8192,
        timeout=5,
    )


def wait_for_status(
    manager: DownloadManager,
    job_id: str,
    status: DownloadStatus,
    timeout: float = 5,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        if job and job.status == status:
            return
        time.sleep(0.02)
    job = manager.get_job(job_id)
    raise AssertionError(f"expected {status}, got {job.status if job else None}")


def test_segmented_download_merges_expected_file(tmp_path: Path) -> None:
    with download_server(supports_ranges=True) as url:
        manager = make_manager(tmp_path)
        job = manager.add_download(url, segments=4)

        assert job.supports_ranges is True
        assert job.segment_count == 4

        manager.start(job.id)
        wait_for_status(manager, job.id, DownloadStatus.COMPLETED)

        final_path = tmp_path / "downloads" / "fixture.bin"
        assert final_path.read_bytes() == DATA


def test_server_without_ranges_uses_single_worker(tmp_path: Path) -> None:
    with download_server(supports_ranges=False) as url:
        manager = make_manager(tmp_path)
        job = manager.add_download(url, segments=8)

        assert job.supports_ranges is False
        assert job.segment_count == 1

        manager.start(job.id)
        wait_for_status(manager, job.id, DownloadStatus.COMPLETED)
        assert (tmp_path / "downloads" / "fixture.bin").read_bytes() == DATA


def test_pause_and_resume_keeps_partial_segments(tmp_path: Path) -> None:
    with download_server(supports_ranges=True, delay=0.002) as url:
        manager = make_manager(tmp_path)
        job = manager.add_download(url, segments=4)

        manager.start(job.id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = manager.get_job(job.id)
            if current and 0 < current.progress < 100:
                break
            time.sleep(0.01)

        manager.pause(job.id)
        wait_for_status(manager, job.id, DownloadStatus.PAUSED)

        paused = manager.get_job(job.id)
        assert paused is not None
        assert 0 < paused.bytes_downloaded < len(DATA)

        manager.resume(job.id)
        wait_for_status(manager, job.id, DownloadStatus.COMPLETED)
        assert (tmp_path / "downloads" / "fixture.bin").read_bytes() == DATA


def test_retry_recovers_from_transient_http_failure(tmp_path: Path) -> None:
    with download_server(supports_ranges=True, fail_first_get=True) as url:
        manager = make_manager(tmp_path)
        job = manager.add_download(url, segments=4, retries=2)

        manager.start(job.id)
        wait_for_status(manager, job.id, DownloadStatus.COMPLETED)

        assert (tmp_path / "downloads" / "fixture.bin").read_bytes() == DATA


def test_permanent_http_failure_marks_job_failed(tmp_path: Path) -> None:
    with download_server(supports_ranges=True, fail_all_get=True) as url:
        manager = make_manager(tmp_path)
        job = manager.add_download(url, segments=4, retries=1)

        manager.start(job.id)
        wait_for_status(manager, job.id, DownloadStatus.FAILED)

        failed = manager.get_job(job.id)
        assert failed is not None
        assert failed.error


def test_restart_marks_interrupted_download_paused(tmp_path: Path) -> None:
    with download_server(supports_ranges=True) as url:
        storage = Storage(tmp_path / "sdm.sqlite3")
        manager = DownloadManager(storage, default_save_dir=tmp_path / "downloads")
        job = manager.add_download(url, segments=4)
        segments = storage.get_segments(job.id)
        job.status = DownloadStatus.DOWNLOADING
        segments[0].status = SegmentStatus.DOWNLOADING
        storage.save_job(job)
        storage.update_segment(segments[0])

        restarted = DownloadManager(
            Storage(tmp_path / "sdm.sqlite3"),
            default_save_dir=tmp_path / "downloads",
        )

        recovered_job = restarted.get_job(job.id)
        recovered_segments = restarted.storage.get_segments(job.id)
        assert recovered_job is not None
        assert recovered_job.status == DownloadStatus.PAUSED
        assert recovered_segments[0].status == SegmentStatus.PAUSED


def test_resume_after_restart_completes_partial_download(tmp_path: Path) -> None:
    with download_server(supports_ranges=True, delay=0.002) as url:
        first_manager = make_manager(tmp_path)
        job = first_manager.add_download(url, segments=4)

        first_manager.start(job.id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = first_manager.get_job(job.id)
            if current and 0 < current.progress < 100:
                break
            time.sleep(0.01)

        first_manager.pause(job.id)
        wait_for_status(first_manager, job.id, DownloadStatus.PAUSED)

        restarted_manager = make_manager(tmp_path)
        restarted_manager.resume(job.id)
        wait_for_status(restarted_manager, job.id, DownloadStatus.COMPLETED)

        assert (tmp_path / "downloads" / "fixture.bin").read_bytes() == DATA


def test_queue_waits_for_active_slot_then_starts(tmp_path: Path) -> None:
    with download_server(supports_ranges=True, delay=0.002) as url:
        manager = make_manager(tmp_path, max_active_downloads=1)
        first = manager.add_download(url, segments=4)
        second = manager.add_download(url.replace("fixture.bin", "second.bin"), segments=4)

        manager.start(first.id)
        manager.start(second.id)

        queued = manager.get_job(second.id)
        assert queued is not None
        assert queued.status == DownloadStatus.QUEUED

        wait_for_status(manager, first.id, DownloadStatus.COMPLETED)
        wait_for_status(manager, second.id, DownloadStatus.COMPLETED)

        assert (tmp_path / "downloads" / "fixture.bin").read_bytes() == DATA
        assert (tmp_path / "downloads" / "second.bin").read_bytes() == DATA
