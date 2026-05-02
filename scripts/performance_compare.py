from __future__ import annotations

import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sdm.downloader import DownloadManager
from sdm.models import DownloadStatus
from sdm.storage import Storage
from sdm.utils import format_bytes


DATA = (b"SimpleDownloadManagerPerformanceData" * 300_000)[:8 * 1024 * 1024]


class RangeHandler(BaseHTTPRequestHandler):
    delay = 0.0015

    def log_message(self, format: str, *args) -> None:
        return

    def do_HEAD(self) -> None:
        self._send_headers(200, len(DATA))

    def do_GET(self) -> None:
        range_header = self.headers.get("Range")
        if range_header:
            start, end = self._parse_range(range_header)
            body = DATA[start : end + 1]
            self._send_headers(206, len(body), start, end)
            self._write_body(body)
            return

        self._send_headers(200, len(DATA))
        self._write_body(DATA)

    def _parse_range(self, value: str) -> tuple[int, int]:
        _, bounds = value.split("=", 1)
        start_text, end_text = bounds.split("-", 1)
        start = int(start_text)
        end = int(end_text) if end_text else len(DATA) - 1
        return start, min(end, len(DATA) - 1)

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
        self.send_header("Accept-Ranges", "bytes")
        if start is not None and end is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.end_headers()

    def _write_body(self, body: bytes) -> None:
        for index in range(0, len(body), 8192):
            self.wfile.write(body[index : index + 8192])
            time.sleep(self.delay)


@contextmanager
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/performance.bin"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def wait_for_completion(manager: DownloadManager, job_id: str) -> None:
    while True:
        job = manager.get_job(job_id)
        if job and job.status in {
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELED,
        }:
            if job.status != DownloadStatus.COMPLETED:
                raise RuntimeError(job.error or f"Job ended as {job.status.value}")
            return
        time.sleep(0.02)


def measure(url: str, segments: int) -> tuple[float, float]:
    workdir = Path(tempfile.mkdtemp(prefix=f"sdm-perf-{segments}-"))
    try:
        manager = DownloadManager(
            Storage(workdir / "sdm.sqlite3"),
            default_save_dir=workdir / "downloads",
            max_active_downloads=1,
            chunk_size=64 * 1024,
        )
        job = manager.add_download(url, segments=segments)
        start = time.perf_counter()
        manager.start(job.id)
        wait_for_completion(manager, job.id)
        elapsed = time.perf_counter() - start
        speed = len(DATA) / elapsed
        return elapsed, speed
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    with server_url() as url:
        single_time, single_speed = measure(url, 1)
        multi_time, multi_speed = measure(url, 4)

    improvement = single_time / multi_time if multi_time else 0
    print("Simple Download Manager performance comparison")
    print(f"File size: {format_bytes(len(DATA))}")
    print("| Mode | Segments | Time | Average Speed |")
    print("| --- | ---: | ---: | ---: |")
    print(f"| Single-threaded | 1 | {single_time:.2f}s | {format_bytes(single_speed)}/s |")
    print(f"| Multithreaded | 4 | {multi_time:.2f}s | {format_bytes(multi_speed)}/s |")
    print(f"Speedup: {improvement:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
