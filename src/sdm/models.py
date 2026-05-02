from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import time


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class SegmentStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELED = "canceled"


@dataclass(slots=True)
class Segment:
    job_id: str
    index: int
    start: int
    end: int | None
    downloaded: int = 0
    status: SegmentStatus = SegmentStatus.PENDING
    temp_path: str = ""
    error: str = ""

    @property
    def size(self) -> int | None:
        if self.end is None:
            return None
        return self.end - self.start + 1

    @property
    def is_complete(self) -> bool:
        return self.status == SegmentStatus.COMPLETED


@dataclass(slots=True)
class DownloadJob:
    id: str
    url: str
    filename: str
    save_dir: str
    total_size: int = 0
    status: DownloadStatus = DownloadStatus.QUEUED
    supports_ranges: bool = False
    segment_count: int = 1
    bytes_downloaded: int = 0
    progress: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    retries: int = 3
    error: str = ""
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)

    @property
    def final_path(self) -> Path:
        return Path(self.save_dir) / self.filename

    @property
    def temp_dir(self) -> Path:
        return Path(self.save_dir) / ".sdm_temp" / self.id
