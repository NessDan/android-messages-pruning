#!/usr/bin/env python3
"""Regenerate the README screenshots from the simulated demo phone.

Runs the real ./msgprune commands against tests/fakephone (sample data, nothing real), turns
each transcript into a terminal-window image, and saves PNGs to docs/images/.
Needs Google Chrome or Chromium for rendering.

    python3 docs/screenshots/make_screenshots.py
"""
import fcntl
import html
import math
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(PROJECT, "docs", "images")
sys.path.insert(0, os.path.join(PROJECT, "tests", "fakephone"))
import demo  # noqa: E402

SERIAL, MODEL = "DEMO9PRO", "Pixel_9_Pro"
DEMO_DIR = "Pixel-9-Pro-DEMO9PRO"  # what msgprune names this phone's data/backups folders
CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "",
]

SHOTS = [
    ("doctor", "./msgprune doctor", None),
    ("scan", "./msgprune scan", None),
    ("preview", "./msgprune prune --min-size 250KB --older-than 2y", None),
    ("apply", "./msgprune prune --min-size 250KB --older-than 2y --apply --limit 5", "prune\n"),
    ("backups", "find backups -type f | sort", None),
]

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROGRESS = re.compile(r"^(Measuring attachment sizes|Backing up|Pruning): [\d,]+/[\d,]+ \(\d+%\)")


def run(cmd, env, typed=None):
    if typed is None:
        cp = subprocess.run(["sh", "-c", cmd], cwd=PROJECT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=1800)
        return cp.stdout.decode("utf-8", "replace")
    return run_in_terminal(cmd, env, expect="anything else to cancel: ", reply=typed)


def run_in_terminal(cmd, env, expect, reply):
    """Run cmd on a pseudo-terminal and type `reply` once `expect` shows up, like a person would."""
    master, slave = pty.openpty()

    def make_controlling_tty():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(["sh", "-c", cmd], cwd=PROJECT, env=env, stdin=slave, stdout=slave,
                            stderr=slave, preexec_fn=make_controlling_tty)
    os.close(slave)
    out, sent = b"", False
    while True:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO: the program finished and closed the terminal
                break
            if not chunk:
                break
            out += chunk
            if not sent and expect.encode() in out:
                time.sleep(0.4)
                os.write(master, reply.replace("\n", "\r").encode())
                sent = True
        elif proc.poll() is not None:
            break
    proc.wait(timeout=60)
    os.close(master)
    return out.decode("utf-8", "replace")


def clean(text):
    """Show what a terminal would: carriage returns overwrite, progress lines update in place."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        segments = [ANSI.sub("", s) for s in raw.split("\r")]
        segments = [s for s in segments if s.strip()] or [""]
        line = segments[-1].replace("\x04", "").replace("^D\b\b", "").rstrip()
        m = PROGRESS.match(line)
        if m and lines and lines[-1].startswith(m.group(1) + ":"):
            lines[-1] = line
        else:
            lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


SUBHEADS = ("Attachments by type", "Attachment sizes", "What `msgprune prune` would free",
            "Biggest conversations", "MMS attachments by year", "Conversations most affected")
RULES = [  # (pattern, css class); earlier rules win where they overlap
    (r"\[OK  \]", "ok"), (r"\[FAIL\]", "bad"), (r"\[info\]", "info"),
    (r"^[A-Z][A-Z/ ]{5,}[A-Z](?=\s|$)", "h"),
    (r"out of msgprune's reach", "bad"), (r"msgprune can prune these", "good"),
    (r"<- default", "warn"), (r"^Removed .*", "good"), (r"Nothing has been changed\.", "good"),
    (r"^STOPPED.*", "bad"), (r"^Type 'prune'.*", "warn"),
    (r"\b\d[\d,]*(?:\.\d+)? (?:B|KB|MB|GB|TB)\b", "sz"),
]


def colorize(line):
    if line.startswith("$ "):
        return f'<span class="p">$</span> <span class="cmd">{html.escape(line[2:])}</span>'
    if line.strip().startswith(SUBHEADS):
        return f'<span class="sub">{html.escape(line)}</span>'
    taken = [None] * len(line)
    for pattern, css in RULES:
        for m in re.finditer(pattern, line):
            if all(t is None for t in taken[m.start():m.end()]):
                for i in range(m.start(), m.end()):
                    taken[i] = css
    out, i = [], 0
    while i < len(line):
        css, j = taken[i], i
        while j < len(line) and taken[j] == css:
            j += 1
        chunk = html.escape(line[i:j])
        out.append(f'<span class="{css}">{chunk}</span>' if css else chunk)
        i = j
    return "".join(out)


CSS = """
html, body { margin: 0; background: transparent; }
.wrap { padding: 18px 26px 34px 26px; }
.win { display: inline-block; background: #1e1e2e; border-radius: 11px; overflow: hidden;
       box-shadow: 0 14px 34px rgba(0,0,0,.30), 0 0 0 1px rgba(0,0,0,.25); }
.bar { height: 34px; background: #2a2b3d; position: relative; display: flex; align-items: center;
       padding-left: 14px; }
.dot { width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; }
.title { position: absolute; left: 0; right: 0; text-align: center; pointer-events: none;
         color: #a6adc8; font: 12.5px -apple-system, "Helvetica Neue", Arial, sans-serif; }
pre { margin: 0; padding: 14px 20px 18px 20px; color: #cdd6f4; white-space: pre;
      font: 13px/19px Menlo, "DejaVu Sans Mono", monospace; }
.p { color: #a6e3a1; } .cmd { color: #ffffff; font-weight: bold; }
.ok, .good { color: #a6e3a1; } .bad { color: #f38ba8; } .info { color: #89b4fa; }
.h { color: #89b4fa; font-weight: bold; } .sub { color: #bac2de; font-weight: bold; }
.sz { color: #fab387; } .warn { color: #f9e2af; }
"""
CHAR_W, LINE_H = 7.83, 19  # Menlo 13px


def render(lines, png, chrome, title):
    body = "\n".join(colorize(line) for line in lines)
    cols = max(len(line) for line in lines)
    width = math.ceil(cols * CHAR_W + 40 + 52 + 4)
    height = 34 + 14 + 18 + len(lines) * LINE_H + 52 + 2
    page = (f"<!doctype html><meta charset=utf-8><style>{CSS}</style>"
            f'<div class="wrap"><div class="win"><div class="bar">'
            f'<span class="dot" style="background:#ff5f57"></span>'
            f'<span class="dot" style="background:#febc2e"></span>'
            f'<span class="dot" style="background:#28c840"></span>'
            f'<span class="title">{html.escape(title)}</span></div><pre>{body}</pre></div></div>')
    with tempfile.TemporaryDirectory() as tmp:
        page_path = os.path.join(tmp, "shot.html")
        with open(page_path, "w", encoding="utf-8") as fh:
            fh.write(page)
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={os.path.join(tmp, 'profile')}", "--no-first-run",
                        "--force-device-scale-factor=2", "--default-background-color=00000000",
                        f"--window-size={width},{height}", f"--screenshot={png}",
                        "file://" + page_path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def main():
    chrome = next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), None)
    if not chrome:
        sys.exit("Chrome/Chromium not found (set CHROME=/path/to/chrome)")
    os.makedirs(OUT, exist_ok=True)
    phone = tempfile.mkdtemp(prefix="msgprune-demo-phone-")
    demo_paths = [os.path.join(PROJECT, d, DEMO_DIR) for d in ("data", "backups")]
    for p in demo_paths:  # only ever the demo phone's folders
        shutil.rmtree(p, ignore_errors=True)
    try:
        demo.build(phone)
        env = dict(os.environ, FAKEPHONE_ROOT=phone, FAKEPHONE_SERIAL=SERIAL, FAKEPHONE_MODEL=MODEL,
                   FAKEPHONE_ANDROID="16", FAKEPHONE_SDK="36",
                   MSGPRUNE_ADB=os.path.join(PROJECT, "tests", "fakephone", "adb"))
        for name, cmd, typed in SHOTS:
            print(f"running: {cmd}", flush=True)
            lines = ["$ " + cmd] + clean(run(cmd, env, typed))
            render(lines, os.path.join(OUT, f"{name}.png"), chrome, "Terminal — msgprune")
            print(f"  saved docs/images/{name}.png ({len(lines)} lines)")
            if name == "scan":  # the top of the report, for the README's opening image
                end = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Other files"))
                render(lines[:end + 1], os.path.join(OUT, "scan-summary.png"), chrome,
                       "Terminal — msgprune")
                print("  saved docs/images/scan-summary.png")
    finally:
        shutil.rmtree(phone, ignore_errors=True)
        if "--keep" not in sys.argv:
            for p in demo_paths:
                shutil.rmtree(p, ignore_errors=True)
            for d in ("data", "backups"):
                try:
                    os.rmdir(os.path.join(PROJECT, d))  # only if now empty
                except OSError:
                    pass


if __name__ == "__main__":
    main()
