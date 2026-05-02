from __future__ import annotations

import re
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlparse


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def filename_from_headers(url: str, headers: dict[str, str]) -> str:
    disposition = headers.get("content-disposition") or headers.get("Content-Disposition", "")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return sanitize_filename(filename)

    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).name
    if path_name:
        return sanitize_filename(path_name)
    return "download.bin"


def sanitize_filename(filename: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", filename).strip().strip(".")
    return cleaned or "download.bin"


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m"
    if minutes:
        return f"{minutes:d}m {secs:02d}s"
    return f"{secs:d}s"
