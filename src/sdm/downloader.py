from __future__ import annotations

import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .models import DownloadJob, DownloadStatus, Segment, SegmentStatus
from .storage import Storage
from .utils import ensure_unique_path, filename_from_headers


ProgressCallback = Callable[[DownloadJob], None]


@dataclass(slots=True)
class _ActiveDownload:
    job_id: str
    stop_event: threading.Event
    target_status: DownloadStatus
    thread: threading.Thread


class DownloadManager:
    def __init__(
        self,
        storage: Storage | None = None,
        default_save_dir: str | Path = "downloads",
        default_segments: int = 4,
        max_active_downloads: int = 2,
        chunk_size: int = 64 * 1024,
        timeout: int = 20,
    ) -> None:
        self.storage = storage or Storage()
        self.default_save_dir = Path(default_save_dir)
        self.default_segments = default_segments
        self.max_active_downloads = max(1, max_active_downloads)
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.storage.mark_interrupted_downloads_paused()
        self.default_save_dir.mkdir(parents=True, exist_ok=True)
        self._callbacks: list[ProgressCallback] = []
        self._active: dict[str, _ActiveDownload] = {}
        self._queued_job_ids: list[str] = []
        self._progress: dict[str, dict[int, int]] = {}
        self._starts: dict[str, tuple[float, int]] = {}
        self._lock = threading.RLock()

    def add_callback(self, callback: ProgressCallback) -> None:
        self._callbacks.append(callback)

    def add_download(
        self,
        url: str,
        save_dir: str | Path | None = None,
        segments: int | None = None,
        retries: int = 3,
    ) -> DownloadJob:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a valid http:// or https:// URL.")

        save_path = Path(save_dir or self.default_save_dir)
        save_path.mkdir(parents=True, exist_ok=True)
        headers = self._read_headers(url)
        filename = filename_from_headers(url, headers)
        final_path = ensure_unique_path(save_path / filename)
        total_size = int(headers.get("content-length") or 0)
        supports_ranges = (
            headers.get("accept-ranges", "").lower() == "bytes" and total_size > 0
        )
        segment_count = max(1, int(segments or self.default_segments))
        if not supports_ranges:
            segment_count = 1

        job = DownloadJob(
            id=uuid.uuid4().hex,
            url=url,
            filename=final_path.name,
            save_dir=str(save_path),
            total_size=total_size,
            supports_ranges=supports_ranges,
            segment_count=segment_count,
            retries=retries,
        )
        segment_rows = self._build_segments(job)
        self.storage.save_job(job)
        self.storage.save_segments(segment_rows)
        self._notify(job)
        return job

    def start(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._active:
                return

            job = self._require_job(job_id)
            if job.status == DownloadStatus.COMPLETED:
                return

            if job.status == DownloadStatus.CANCELED:
                self._reset_segments(job)

            if len(self._active) >= self.max_active_downloads:
                self._queue_job_locked(job)
                return

            self._start_job_locked(job)

    def set_max_active_downloads(self, value: int) -> None:
        with self._lock:
            self.max_active_downloads = max(1, value)
            self._start_queued_jobs_locked()

    def _start_job_locked(self, job: DownloadJob) -> None:
        if job.id in self._queued_job_ids:
            self._queued_job_ids.remove(job.id)

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_job,
            args=(job.id, stop_event),
            name=f"sdm-job-{job.id[:8]}",
            daemon=True,
        )
        self._active[job.id] = _ActiveDownload(
            job_id=job.id,
            stop_event=stop_event,
            target_status=DownloadStatus.PAUSED,
            thread=thread,
        )
        thread.start()

    def _queue_job_locked(self, job: DownloadJob) -> None:
        if job.id not in self._queued_job_ids:
            self._queued_job_ids.append(job.id)
        job.status = DownloadStatus.QUEUED
        job.speed_bps = 0
        job.eta_seconds = None
        self.storage.save_job(job)
        self._notify(job)

    def _start_queued_jobs_locked(self) -> None:
        while self._queued_job_ids and len(self._active) < self.max_active_downloads:
            job_id = self._queued_job_ids.pop(0)
            job = self.storage.get_job(job_id)
            if not job or job.status in {
                DownloadStatus.COMPLETED,
                DownloadStatus.CANCELED,
            }:
                continue
            self._start_job_locked(job)

    def resume(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job.status in {
            DownloadStatus.PAUSED,
            DownloadStatus.FAILED,
            DownloadStatus.QUEUED,
            DownloadStatus.DOWNLOADING,
        }:
            self.start(job_id)

    def retry(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job.status != DownloadStatus.COMPLETED:
            self.start(job_id)

    def pause(self, job_id: str) -> None:
        active = self._active.get(job_id)
        if active:
            active.target_status = DownloadStatus.PAUSED
            active.stop_event.set()
            return

        with self._lock:
            if job_id in self._queued_job_ids:
                self._queued_job_ids.remove(job_id)
            job = self.storage.get_job(job_id)
            if job and job.status == DownloadStatus.QUEUED:
                job.status = DownloadStatus.PAUSED
                job.speed_bps = 0
                job.eta_seconds = None
                self.storage.save_job(job)
                self._notify(job)

    def cancel(self, job_id: str) -> None:
        active = self._active.get(job_id)
        if active:
            active.target_status = DownloadStatus.CANCELED
            active.stop_event.set()
            return

        with self._lock:
            if job_id in self._queued_job_ids:
                self._queued_job_ids.remove(job_id)

        job = self._require_job(job_id)
        job.status = DownloadStatus.CANCELED
        job.speed_bps = 0
        job.eta_seconds = None
        self.storage.save_job(job)
        self._cleanup_temp(job)
        self._notify(job)

    def list_jobs(self) -> list[DownloadJob]:
        return self.storage.list_jobs()

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self.storage.get_job(job_id)

    def wait_for(self, job_id: str, timeout: float | None = None) -> None:
        active = self._active.get(job_id)
        if active:
            active.thread.join(timeout)

    def _read_headers(self, url: str) -> dict[str, str]:
        try:
            response = requests.head(
                url,
                allow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "SimpleDownloadManager/0.1"},
            )
            response.raise_for_status()
            return {key.lower(): value for key, value in response.headers.items()}
        except requests.RequestException:
            response = requests.get(
                url,
                stream=True,
                allow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "SimpleDownloadManager/0.1"},
            )
            try:
                response.raise_for_status()
                return {key.lower(): value for key, value in response.headers.items()}
            finally:
                response.close()

    def _build_segments(self, job: DownloadJob) -> list[Segment]:
        job.temp_dir.mkdir(parents=True, exist_ok=True)
        if not job.supports_ranges or job.total_size <= 0:
            return [
                Segment(
                    job_id=job.id,
                    index=0,
                    start=0,
                    end=None if job.total_size <= 0 else job.total_size - 1,
                    temp_path=str(job.temp_dir / "segment-0.part"),
                )
            ]

        segment_size = job.total_size // job.segment_count
        segments: list[Segment] = []
        start = 0
        for index in range(job.segment_count):
            end = (
                job.total_size - 1
                if index == job.segment_count - 1
                else start + segment_size - 1
            )
            segments.append(
                Segment(
                    job_id=job.id,
                    index=index,
                    start=start,
                    end=end,
                    temp_path=str(job.temp_dir / f"segment-{index}.part"),
                )
            )
            start = end + 1
        return segments

    def _run_job(self, job_id: str, stop_event: threading.Event) -> None:
        job = self._require_job(job_id)
        segments = self.storage.get_segments(job_id)
        try:
            self._prepare_for_download(job, segments)
            workers = max(1, min(job.segment_count, len(segments)))
            failed_error = ""

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._download_segment, job, segment, stop_event): segment
                    for segment in segments
                    if segment.status != SegmentStatus.COMPLETED
                }
                for future in as_completed(futures):
                    segment = future.result()
                    if segment.status == SegmentStatus.FAILED:
                        failed_error = segment.error
                        stop_event.set()

            active = self._active.get(job_id)
            target = active.target_status if active else DownloadStatus.PAUSED
            fresh_segments = self.storage.get_segments(job_id)

            if target == DownloadStatus.CANCELED and stop_event.is_set():
                self._mark_job(job, DownloadStatus.CANCELED)
                self._cleanup_temp(job)
                self._reset_progress_runtime(job_id)
                return

            if failed_error:
                self._mark_job(job, DownloadStatus.FAILED, failed_error)
                self._reset_progress_runtime(job_id)
                return

            if stop_event.is_set():
                self._mark_job(job, DownloadStatus.PAUSED)
                self._reset_progress_runtime(job_id)
                return

            if all(segment.status == SegmentStatus.COMPLETED for segment in fresh_segments):
                self._assemble_file(job, fresh_segments)
                job.bytes_downloaded = job.total_size or sum(
                    Path(segment.temp_path).stat().st_size for segment in fresh_segments
                )
                job.progress = 100.0
                job.speed_bps = 0
                job.eta_seconds = 0
                job.status = DownloadStatus.COMPLETED
                job.error = ""
                self.storage.save_job(job)
                self._cleanup_temp(job)
                self._notify(job)
            else:
                self._mark_job(job, DownloadStatus.FAILED, failed_error or "Download failed.")
        except Exception as exc:
            self._mark_job(job, DownloadStatus.FAILED, str(exc))
        finally:
            with self._lock:
                self._active.pop(job_id, None)
                self._start_queued_jobs_locked()

    def _prepare_for_download(self, job: DownloadJob, segments: list[Segment]) -> None:
        job.temp_dir.mkdir(parents=True, exist_ok=True)
        downloaded: dict[int, int] = {}
        for segment in segments:
            path = Path(segment.temp_path)
            existing = path.stat().st_size if path.exists() else 0
            if segment.size is not None:
                existing = min(existing, segment.size)
            if existing and not job.supports_ranges:
                path.unlink(missing_ok=True)
                existing = 0
            segment.downloaded = existing
            segment.status = (
                SegmentStatus.COMPLETED
                if segment.size is not None and existing >= segment.size
                else SegmentStatus.PENDING
            )
            segment.error = ""
            downloaded[segment.index] = existing

        with self._lock:
            self._progress[job.id] = downloaded
            self._starts[job.id] = (time.monotonic(), sum(downloaded.values()))

        job.status = DownloadStatus.DOWNLOADING
        job.error = ""
        self._recalculate_job(job)
        self.storage.save_segments(segments)
        self.storage.save_job(job)
        self._notify(job)

    def _download_segment(
        self,
        job: DownloadJob,
        segment: Segment,
        stop_event: threading.Event,
    ) -> Segment:
        path = Path(segment.temp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        last_error = ""

        for attempt in range(1, job.retries + 2):
            if stop_event.is_set():
                segment.status = SegmentStatus.PAUSED
                self.storage.update_segment(segment)
                return segment

            try:
                self._download_segment_once(job, segment, stop_event)
                if segment.status == SegmentStatus.COMPLETED:
                    return segment
            except Exception as exc:
                last_error = str(exc)
                segment.error = last_error
                if attempt <= job.retries and not stop_event.is_set():
                    time.sleep(min(3.0, 0.5 * attempt))
                    continue
                break

        if stop_event.is_set():
            segment.status = SegmentStatus.PAUSED
        else:
            segment.status = SegmentStatus.FAILED
            segment.error = last_error or "Segment failed."
        self.storage.update_segment(segment)
        return segment

    def _download_segment_once(
        self,
        job: DownloadJob,
        segment: Segment,
        stop_event: threading.Event,
    ) -> None:
        path = Path(segment.temp_path)
        existing = path.stat().st_size if path.exists() else 0
        if segment.size is not None:
            existing = min(existing, segment.size)

        if segment.size is not None and existing >= segment.size:
            segment.downloaded = segment.size
            segment.status = SegmentStatus.COMPLETED
            self.storage.update_segment(segment)
            self._record_progress(job, segment)
            return

        headers = {"User-Agent": "SimpleDownloadManager/0.1"}
        mode = "ab"
        if job.supports_ranges and segment.end is not None:
            range_start = segment.start + existing
            headers["Range"] = f"bytes={range_start}-{segment.end}"
        else:
            existing = 0
            mode = "wb"

        segment.downloaded = existing
        segment.status = SegmentStatus.DOWNLOADING
        self.storage.update_segment(segment)
        self._record_progress(job, segment)

        with requests.get(
            job.url,
            stream=True,
            timeout=self.timeout,
            headers=headers,
            allow_redirects=True,
        ) as response:
            expected_status = 206 if job.supports_ranges and segment.end is not None else 200
            if response.status_code != expected_status:
                raise RuntimeError(
                    f"Expected HTTP {expected_status}, got {response.status_code}."
                )

            with path.open(mode) as output:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if stop_event.is_set():
                        segment.status = SegmentStatus.PAUSED
                        self.storage.update_segment(segment)
                        return
                    if not chunk:
                        continue
                    output.write(chunk)
                    segment.downloaded += len(chunk)
                    self._record_progress(job, segment)

        if segment.size is not None and segment.downloaded < segment.size:
            raise RuntimeError("Connection closed before the segment finished.")

        segment.status = SegmentStatus.COMPLETED
        segment.error = ""
        self.storage.update_segment(segment)
        self._record_progress(job, segment)

    def _record_progress(self, job: DownloadJob, segment: Segment) -> None:
        with self._lock:
            self._progress.setdefault(job.id, {})[segment.index] = segment.downloaded
            self._recalculate_job(job)
            self.storage.save_job(job)
        self._notify(job)

    def _recalculate_job(self, job: DownloadJob) -> None:
        progress = self._progress.get(job.id, {})
        downloaded = sum(progress.values())
        job.bytes_downloaded = downloaded
        if job.total_size > 0:
            job.progress = min(100.0, downloaded / job.total_size * 100)
        else:
            job.progress = 0.0

        start_time, start_bytes = self._starts.get(job.id, (time.monotonic(), downloaded))
        elapsed = max(0.001, time.monotonic() - start_time)
        speed = max(0.0, (downloaded - start_bytes) / elapsed)
        job.speed_bps = speed
        remaining = max(0, job.total_size - downloaded)
        job.eta_seconds = remaining / speed if speed > 0 and job.total_size > 0 else None

    def _assemble_file(self, job: DownloadJob, segments: list[Segment]) -> None:
        final_path = job.final_path
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with final_path.open("wb") as output:
            for segment in sorted(segments, key=lambda item: item.index):
                with Path(segment.temp_path).open("rb") as part:
                    shutil.copyfileobj(part, output)

        if job.total_size > 0 and final_path.stat().st_size != job.total_size:
            raise RuntimeError("Final file size does not match the expected Content-Length.")

    def _mark_job(
        self,
        job: DownloadJob,
        status: DownloadStatus,
        error: str = "",
    ) -> None:
        job.status = status
        job.error = error
        job.speed_bps = 0
        job.eta_seconds = None
        self.storage.save_job(job)
        self._notify(job)

    def _reset_segments(self, job: DownloadJob) -> None:
        self._cleanup_temp(job)
        segments = self._build_segments(job)
        self.storage.save_segments(segments)
        job.status = DownloadStatus.QUEUED
        job.bytes_downloaded = 0
        job.progress = 0
        job.error = ""
        self.storage.save_job(job)

    def _cleanup_temp(self, job: DownloadJob) -> None:
        shutil.rmtree(job.temp_dir, ignore_errors=True)

    def _reset_progress_runtime(self, job_id: str) -> None:
        with self._lock:
            self._progress.pop(job_id, None)
            self._starts.pop(job_id, None)

    def _require_job(self, job_id: str) -> DownloadJob:
        job = self.storage.get_job(job_id)
        if job is None:
            raise KeyError(f"Download job {job_id} does not exist.")
        return job

    def _notify(self, job: DownloadJob) -> None:
        for callback in list(self._callbacks):
            try:
                callback(job)
            except Exception:
                pass
