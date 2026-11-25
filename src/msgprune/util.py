"""Small helpers: human-readable sizes, dates, safe file names and a progress line."""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time

# Decimal units, the same ones Android's Settings > Storage uses (1 MB = 1,000,000 bytes).
_UNITS = [("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)]


def fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    n = int(n)
    for unit, factor in _UNITS:
        if abs(n) >= factor:
            v = n / factor
            num = f"{v:.1f}" if v < 100 else f"{v:.0f}"
            if num.endswith(".0"):
                num = num[:-2]
            return f"{num} {unit}"
    return f"{n} B"


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_SIZE_FACTORS = {
    "": 1, "b": 1,
    "k": 10**3, "kb": 10**3, "m": 10**6, "mb": 10**6, "g": 10**9, "gb": 10**9,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3,
}


def parse_size(text: str) -> int:
    """'100KB' -> 100000, '2MB' -> 2000000, '1.5GB', '512' (bytes)."""
    m = _SIZE_RE.match(text)
    if not m or m.group(2).lower() not in _SIZE_FACTORS:
        raise ValueError(f"can't understand the size {text!r} (examples: 100KB, 2MB, 1.5GB)")
    return int(float(m.group(1)) * _SIZE_FACTORS[m.group(2).lower()])


_AGE_RE = re.compile(
    r"^\s*(\d+)\s*(d|days?|w|wks?|weeks?|mo|mos|months?|y|yrs?|years?)\s*$", re.IGNORECASE
)


def parse_cutoff(text: str, now: dt.datetime | None = None) -> dt.datetime:
    """'90d', '6mo', '2y' (that long before now) or a date '2023-01-31' -> cutoff datetime."""
    now = now or dt.datetime.now()
    text = text.strip()
    try:
        return dt.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        pass
    m = _AGE_RE.match(text)
    if not m:
        raise ValueError(
            f"can't understand {text!r} (examples: 90d, 12w, 6mo, 2y, or a date like 2023-01-31)"
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("d"):
        days = n
    elif unit.startswith("w"):
        days = 7 * n
    elif unit.startswith("mo"):
        days = 30 * n
    else:
        days = 365 * n
    return now - dt.timedelta(days=days)


def mms_datetime(date_value) -> dt.datetime | None:
    """MMS rows store seconds since the epoch (a few buggy apps wrote milliseconds)."""
    try:
        v = int(date_value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 10**11:  # milliseconds
        v //= 1000
    try:
        return dt.datetime.fromtimestamp(v)
    except (OverflowError, OSError, ValueError):
        return None


_UNSAFE = re.compile(r"[^\w\s.,()\[\]+\-&'@#]", re.UNICODE)


def safe_name(text: str, maxlen: int = 60) -> str:
    """Make text usable as a single file/folder name on macOS, Windows and Linux."""
    text = _UNSAFE.sub("_", text or "")
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    text = text[:maxlen].rstrip()
    return text or "unknown"


def show_path(path: str) -> str:
    """Short path for display: relative when under the current folder, ~ for the home folder."""
    p = os.path.abspath(path)
    cwd = os.getcwd()
    try:
        if os.path.commonpath([p, cwd]) == cwd:
            return os.path.relpath(p, cwd)
    except ValueError:
        pass
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


class Progress:
    """A single status line on stderr. Redraws in place on a terminal, prints every ~10% otherwise."""

    def __init__(self, label: str, total: int, stream=None):
        self.label = label
        self.total = max(int(total), 0)
        self.stream = stream or sys.stderr
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.start = time.monotonic()
        self.done = 0
        self._last_draw = 0.0
        self._last_decile = -1

    def update(self, done: int) -> None:
        self.done = done
        now = time.monotonic()
        if self.tty:
            if now - self._last_draw >= 0.25 or done >= self.total:
                self._last_draw = now
                self.stream.write("\r\033[K" + self._line(now))
                self.stream.flush()
        else:
            decile = int(10 * done / self.total) if self.total else 10
            if decile != self._last_decile:
                self._last_decile = decile
                self.stream.write(self._line(now) + "\n")
                self.stream.flush()

    def advance(self, n: int = 1) -> None:
        self.update(self.done + n)

    def _line(self, now: float) -> str:
        pct = (100 * self.done // self.total) if self.total else 100
        line = f"{self.label}: {self.done:,}/{self.total:,} ({pct}%)"
        elapsed = now - self.start
        if 0 < self.done < self.total and elapsed > 3:
            left = elapsed / self.done * (self.total - self.done)
            line += f", about {fmt_duration(left)} left"
        return line

    def finish(self) -> None:
        if self.tty:
            self.stream.write("\r\033[K" + self._line(time.monotonic()) + "\n")
            self.stream.flush()
        elif self._last_decile != 10:
            self.update(self.total)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"
