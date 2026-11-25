"""Talking to the phone: adb discovery, device selection, and the `content` shell tool.

Everything the phone-side runs is a stock Android shell command (`content`, `appops`,
`dumpsys`, `getprop`, `sh`). Nothing gets installed on the phone.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterable

REMOTE_DIR = "/data/local/tmp/msgprune"

_COMMON_ADB_PATHS = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
]


class AdbError(RuntimeError):
    pass


class QueryParseError(AdbError):
    pass


def quote(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in args)


def find_adb(explicit: str | None = None) -> str:
    for cand in [explicit, os.environ.get("MSGPRUNE_ADB"), shutil.which("adb"), *_COMMON_ADB_PATHS]:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise AdbError(
        "adb (Android platform-tools) isn't installed.\n"
        "  Install it with:  brew install --cask android-platform-tools"
    )


@dataclass
class DeviceEntry:
    serial: str
    state: str
    model: str


def list_devices(adb: str) -> list[DeviceEntry]:
    cp = subprocess.run(
        [adb, "devices", "-l"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
    )
    if cp.returncode != 0:
        raise AdbError(f"`adb devices` failed: {cp.stderr.strip()}")
    entries = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[0], fields[1]
        model = ""
        for f in fields[2:]:
            if f.startswith("model:"):
                model = f[len("model:"):].replace("_", " ")
        entries.append(DeviceEntry(serial, state, model))
    return entries


_NO_DEVICE_HELP = """No phone found. To connect one:
  1. On the phone: Settings > About phone > tap "Build number" 7 times (unlocks Developer options).
  2. Settings > System > Developer options > turn on "USB debugging".
  3. Plug the phone into this computer with a USB cable that carries data.
  4. Unlock the phone and tap "Allow" on the "Allow USB debugging?" prompt.
Then run:  ./msgprune devices"""


def pick_device(adb: str, wanted: str | None = None) -> "Device":
    entries = list_devices(adb)
    if wanted:
        w = wanted.lower()
        matches = [e for e in entries if e.serial == wanted] or [
            e for e in entries if w in e.model.lower()
        ]
        if not matches:
            raise AdbError(f"No connected phone matches {wanted!r}. Connected: {_describe(entries)}")
        if len(matches) > 1:
            raise AdbError(f"{wanted!r} matches more than one phone; use the serial instead: {_describe(matches)}")
        entry = matches[0]
    else:
        ready = [e for e in entries if e.state == "device"]
        if not entries:
            raise AdbError(_NO_DEVICE_HELP)
        if not ready:
            raise AdbError(_state_help(entries[0]))
        if len(ready) > 1:
            raise AdbError(
                "More than one phone is connected. Pick one with --device, e.g.\n"
                + "\n".join(f"  ./msgprune --device {shlex.quote(e.model or e.serial)} ..." for e in ready)
            )
        entry = ready[0]
    if entry.state != "device":
        raise AdbError(_state_help(entry))
    return Device(adb, entry.serial, entry.model)


def _describe(entries: list[DeviceEntry]) -> str:
    if not entries:
        return "none"
    return ", ".join(f"{e.model or '?'} ({e.serial}, {e.state})" for e in entries)


def _state_help(e: DeviceEntry) -> str:
    if e.state == "unauthorized":
        return (
            f"{e.model or e.serial} is connected but hasn't trusted this computer yet.\n"
            "Unlock the phone and tap \"Allow\" on the \"Allow USB debugging?\" prompt "
            "(unplug and replug if you don't see it)."
        )
    if e.state == "offline":
        return f"{e.model or e.serial} is offline. Unplug and replug the cable, then try again."
    return f"{e.model or e.serial} is in state {e.state!r}; it needs to be 'device'."


def _provider_error(out: str, err: str) -> str | None:
    blob = err + "\n" + out[:2000]
    for marker in ("Error while accessing provider", "Permission Denial", "SecurityException",
                   "IllegalArgumentException", "SQLiteException", "Unknown URI", "Unsupported"):
        if marker in blob:
            lines = [ln for ln in blob.strip().splitlines() if ln.strip()]
            return "\n".join(lines[:6])
    return None


class Device:
    def __init__(self, adb: str, serial: str, model: str = ""):
        self.adb = adb
        self.serial = serial
        self.model = model or serial
        self._remote_ready = False

    # ----- raw adb -------------------------------------------------------------------------

    def _adb(self, *args: str) -> list[str]:
        return [self.adb, "-s", self.serial, *args]

    def shell(self, cmd: str, *, timeout: float = 900, check: bool = True) -> str:
        cp = subprocess.run(
            self._adb("shell", "-T", cmd),
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        out = cp.stdout.decode("utf-8", errors="replace")
        err = cp.stderr.decode("utf-8", errors="replace")
        if check:
            problem = _provider_error(out, err)
            if problem or cp.returncode != 0:
                raise AdbError(f"Phone command failed: {cmd[:200]}\n{problem or err.strip() or out.strip()}")
        return out

    def shell_bytes(self, cmd: str, *, timeout: float = 900) -> tuple[bytes, str, int]:
        """Binary-safe: stdout bytes, stderr text, exit code (adb's shell protocol, no pty)."""
        cp = subprocess.run(
            self._adb("shell", "-T", cmd),
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return cp.stdout, cp.stderr.decode("utf-8", errors="replace"), cp.returncode

    def push(self, local: str, remote: str) -> None:
        cp = subprocess.run(
            self._adb("push", local, remote), capture_output=True, text=True,
            timeout=600, stdin=subprocess.DEVNULL,
        )
        if cp.returncode != 0:
            raise AdbError(f"adb push failed: {cp.stderr.strip() or cp.stdout.strip()}")

    def getprop(self, name: str) -> str:
        return self.shell(quote(["getprop", name]), check=False).strip()

    # ----- the `content` tool --------------------------------------------------------------

    def query(self, uri: str, projection: list[str], where: str | None = None,
              sort: str | None = None, timeout: float = 1800) -> list[dict]:
        args = ["content", "query", "--uri", uri, "--projection", ":".join(projection)]
        if where:
            args += ["--where", where]
        if sort:
            args += ["--sort", sort]
        out = self.shell(quote(args), timeout=timeout)
        return parse_query_output(out, projection)

    def insert(self, uri: str, binds: list[tuple[str, str, object]]) -> None:
        self.shell(quote(insert_args(uri, binds)))

    def delete(self, uri: str) -> None:
        self.shell(quote(["content", "delete", "--uri", uri]))

    def read(self, uri: str, timeout: float = 900) -> tuple[bytes, str, int]:
        return self.shell_bytes(quote(["content", "read", "--uri", uri]), timeout=timeout)

    # ----- running a batch of commands on the phone, several at a time ----------------------

    def _ensure_remote_dir(self) -> None:
        if not self._remote_ready:
            self.shell(quote(["mkdir", "-p", REMOTE_DIR]))
            self._remote_ready = True

    def run_parallel(self, worker_scripts: list[str], on_line: Callable[[str], None],
                     tag: str = "job", timeout: float | None = None) -> list[str]:
        """Push one shell script per worker, run them all at once on the phone, stream output.

        Lines starting with 'R ' are results and go to on_line(); anything else (error text
        from `content`) is returned so the caller can report it.
        """
        self._ensure_remote_dir()
        stray: list[str] = []
        with tempfile.TemporaryDirectory(prefix="msgprune-") as tmp:
            names = []
            for i, body in enumerate(worker_scripts):
                name = f"{tag}_w{i}.sh"
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
                names.append(name)
            main = "".join(f"sh {REMOTE_DIR}/{n} &\n" for n in names) + "wait\n"
            with open(os.path.join(tmp, f"{tag}_main.sh"), "w", encoding="utf-8") as fh:
                fh.write(main)
            for name in names + [f"{tag}_main.sh"]:
                self.push(os.path.join(tmp, name), f"{REMOTE_DIR}/{name}")
        proc = subprocess.Popen(
            self._adb("shell", "-T", f"sh {REMOTE_DIR}/{tag}_main.sh 2>&1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("R "):
                    on_line(line)
                elif line.strip():
                    stray.append(line)
            proc.wait(timeout=timeout)
        except BaseException:
            proc.kill()
            raise
        finally:
            if proc.stdout:
                proc.stdout.close()
            self.shell(f"rm -f {REMOTE_DIR}/{tag}_*", check=False)
        return stray

    def push_text(self, text: str, remote_name: str) -> str:
        self._ensure_remote_dir()
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as fh:
            fh.write(text)
            local = fh.name
        try:
            remote = f"{REMOTE_DIR}/{remote_name}"
            self.push(local, remote)
            return remote
        finally:
            os.unlink(local)

    def cleanup_remote(self) -> None:
        self.shell(f"rm -rf {REMOTE_DIR}", check=False)
        self._remote_ready = False


def insert_args(uri: str, binds: list[tuple[str, str, object]]) -> list[str]:
    args = ["content", "insert", "--uri", uri]
    for col, typ, val in binds:
        args += ["--bind", f"{col}:{typ}:{val}"]
    return args


# ----- parsing `content query` output ---------------------------------------------------------
#
# Android prints one line per row:   Row: 0 _id=12, mid=4, ct=image/jpeg, name=NULL
# Values aren't escaped, so we parse using the known column order: each value runs until
# ", <next column>=". Put free-text columns last; a multi-line value continues on the
# following lines until the next "Row: ".

_ROW_RE = re.compile(r"^Row: \d+ ")


def parse_query_output(text: str, columns: list[str]) -> list[dict]:
    if text.endswith("\n"):
        text = text[:-1]
    if not text.strip() or text.strip() == "No result found.":
        return []
    raw_rows: list[str] = []
    for line in text.split("\n"):
        if _ROW_RE.match(line):
            raw_rows.append(line)
        elif raw_rows:
            raw_rows[-1] += "\n" + line
    return [_parse_row(r, columns) for r in raw_rows]


def _parse_row(raw: str, columns: list[str]) -> dict:
    m = _ROW_RE.match(raw)
    assert m
    body = raw[m.end():]
    row: dict = {}
    pos = 0
    for i, col in enumerate(columns):
        prefix = col + "="
        if not body.startswith(prefix, pos):
            raise QueryParseError(f"Unexpected `content query` output near {body[pos:pos + 60]!r}")
        start = pos + len(prefix)
        if i + 1 < len(columns):
            end = body.find(", " + columns[i + 1] + "=", start)
            if end < 0:
                raise QueryParseError(f"Couldn't find column {columns[i + 1]!r} in {body[:120]!r}")
            value = body[start:end]
            pos = end + 2
        else:
            value = body[start:]
        row[col] = None if value == "NULL" else value
    return row


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
