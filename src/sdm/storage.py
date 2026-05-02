from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from time import time

from .models import DownloadJob, DownloadStatus, Segment, SegmentStatus


class Storage:
    def __init__(self, db_path: str | Path = "sdm.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    save_dir TEXT NOT NULL,
                    total_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    supports_ranges INTEGER NOT NULL,
                    segment_count INTEGER NOT NULL,
                    bytes_downloaded INTEGER NOT NULL,
                    progress REAL NOT NULL,
                    speed_bps REAL NOT NULL,
                    eta_seconds REAL,
                    retries INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS segments (
                    job_id TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER,
                    downloaded INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    temp_path TEXT NOT NULL,
                    error TEXT NOT NULL,
                    PRIMARY KEY (job_id, segment_index),
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                """
            )

    def mark_interrupted_downloads_paused(self) -> None:
        """Recover cleanly when the app was closed while workers were active."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, speed_bps = 0, eta_seconds = NULL, updated_at = ?
                WHERE status = ?
                """,
                (DownloadStatus.PAUSED.value, time(), DownloadStatus.DOWNLOADING.value),
            )
            conn.execute(
                """
                UPDATE segments
                SET status = ?
                WHERE status = ?
                """,
                (SegmentStatus.PAUSED.value, SegmentStatus.DOWNLOADING.value),
            )

    def save_job(self, job: DownloadJob) -> None:
        job.updated_at = time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, url, filename, save_dir, total_size, status, supports_ranges,
                    segment_count, bytes_downloaded, progress, speed_bps, eta_seconds,
                    retries, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,
                    filename=excluded.filename,
                    save_dir=excluded.save_dir,
                    total_size=excluded.total_size,
                    status=excluded.status,
                    supports_ranges=excluded.supports_ranges,
                    segment_count=excluded.segment_count,
                    bytes_downloaded=excluded.bytes_downloaded,
                    progress=excluded.progress,
                    speed_bps=excluded.speed_bps,
                    eta_seconds=excluded.eta_seconds,
                    retries=excluded.retries,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    job.id,
                    job.url,
                    job.filename,
                    job.save_dir,
                    job.total_size,
                    job.status.value,
                    int(job.supports_ranges),
                    job.segment_count,
                    job.bytes_downloaded,
                    job.progress,
                    job.speed_bps,
                    job.eta_seconds,
                    job.retries,
                    job.error,
                    job.created_at,
                    job.updated_at,
                ),
            )

    def save_segments(self, segments: list[Segment]) -> None:
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO segments (
                    job_id, segment_index, start_byte, end_byte, downloaded,
                    status, temp_path, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, segment_index) DO UPDATE SET
                    start_byte=excluded.start_byte,
                    end_byte=excluded.end_byte,
                    downloaded=excluded.downloaded,
                    status=excluded.status,
                    temp_path=excluded.temp_path,
                    error=excluded.error
                """,
                [
                    (
                        segment.job_id,
                        segment.index,
                        segment.start,
                        segment.end,
                        segment.downloaded,
                        segment.status.value,
                        segment.temp_path,
                        segment.error,
                    )
                    for segment in segments
                ],
            )

    def update_segment(self, segment: Segment) -> None:
        self.save_segments([segment])

    def get_job(self, job_id: str) -> DownloadJob | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def get_segments(self, job_id: str) -> list[Segment]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM segments WHERE job_id = ? ORDER BY segment_index",
                (job_id,),
            ).fetchall()
        return [self._row_to_segment(row) for row in rows]

    def list_jobs(self) -> list[DownloadJob]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._row_to_job(row) for row in rows]

    def delete_job(self, job_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> DownloadJob:
        return DownloadJob(
            id=row["id"],
            url=row["url"],
            filename=row["filename"],
            save_dir=row["save_dir"],
            total_size=row["total_size"],
            status=DownloadStatus(row["status"]),
            supports_ranges=bool(row["supports_ranges"]),
            segment_count=row["segment_count"],
            bytes_downloaded=row["bytes_downloaded"],
            progress=row["progress"],
            speed_bps=row["speed_bps"],
            eta_seconds=row["eta_seconds"],
            retries=row["retries"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> Segment:
        return Segment(
            job_id=row["job_id"],
            index=row["segment_index"],
            start=row["start_byte"],
            end=row["end_byte"],
            downloaded=row["downloaded"],
            status=SegmentStatus(row["status"]),
            temp_path=row["temp_path"],
            error=row["error"],
        )
