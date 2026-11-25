"""Read-only inventory of the phone's SMS/MMS store: conversations, MMS messages, attachments.

Where the data lives on Android
-------------------------------
* SMS and MMS are kept by the system "Phone and Messaging Storage" provider
  (package com.android.providers.telephony). MMS attachments are files that provider owns;
  the `content` shell tool can list, read and (with permission) delete them.
* RCS ("chat features") messages and their photos/videos live only inside Google Messages'
  private storage. Nothing outside Google Messages can see or delete them without root.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .adb import Device, to_int
from .util import Progress, mms_datetime

MESSAGES_PKG = "com.google.android.apps.messaging"
TELEPHONY_PKG = "com.android.providers.telephony"

MSG_BOX_INBOX = 1
MSG_BOX_SENT = 2
PRUNABLE_BOXES = (MSG_BOX_INBOX, MSG_BOX_SENT)  # never touch drafts, outbox or failed sends

PLACEHOLDER_PREFIX = "msgprune_ph_"  # `cl` (content-location) of the text parts we add

_VCARD_TYPES = {"text/x-vcard", "text/vcard", "text/directory", "text/x-vcalendar", "text/calendar"}
MEDIA_CATEGORIES = ("image", "video", "audio", "other")


def category(ct: str | None) -> str:
    """'text' parts are the message text (or layout) and are never pruned."""
    ct = (ct or "").lower().split(";")[0].strip()
    if ct in ("text/plain", "application/smil"):
        return "text"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("text/") and ct not in _VCARD_TYPES:
        return "text"
    return "other"


@dataclass
class Part:
    id: int
    mid: int
    ct: str
    cl: str | None
    name: str | None
    size: int | None = None

    @property
    def category(self) -> str:
        return category(self.ct)

    @property
    def is_placeholder(self) -> bool:
        return (self.cl or "").startswith(PLACEHOLDER_PREFIX)


@dataclass
class Mms:
    id: int
    thread_id: int
    date: int
    msg_box: int
    parts: list = field(default_factory=list)

    @property
    def when(self):
        return mms_datetime(self.date)

    @property
    def direction(self) -> str:
        return {MSG_BOX_INBOX: "received", MSG_BOX_SENT: "sent"}.get(self.msg_box, f"box{self.msg_box}")

    @property
    def has_placeholder(self) -> bool:
        return any(p.is_placeholder for p in self.parts)


@dataclass
class Thread:
    id: int
    addresses: list
    label: str

    @property
    def is_group(self) -> bool:
        return len(self.addresses) > 1


@dataclass
class Inventory:
    serial: str
    threads: dict
    mms: dict
    sms_count: int
    contacts_used: bool

    def parts(self):
        for m in self.mms.values():
            yield from m.parts

    def thread_label(self, thread_id: int) -> str:
        t = self.threads.get(thread_id)
        return t.label if t else f"Conversation {thread_id}"


# ----- phone numbers and contact names ---------------------------------------------------------

def norm_address(addr: str | None) -> str:
    addr = (addr or "").strip()
    if "@" in addr:
        return addr.lower()
    digits = re.sub(r"\D", "", addr)
    if len(digits) >= 10:
        return digits[-10:]
    return digits or addr.lower()


def pretty_address(addr: str) -> str:
    digits = re.sub(r"\D", "", addr or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10 and not re.search(r"[A-Za-z@]", addr or ""):
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return addr or "unknown"


def load_contacts(dev: Device) -> dict:
    """number (normalized) -> display name. Best effort: returns {} if contacts can't be read."""
    try:
        rows = dev.query("content://com.android.contacts/data/phones", ["data1", "display_name"])
    except Exception:
        return {}
    names = {}
    for r in rows:
        key = norm_address(r.get("data1"))
        if key and r.get("display_name"):
            names.setdefault(key, r["display_name"].strip())
    return names


def make_label(addresses: list, contacts: dict) -> str:
    names = [contacts.get(norm_address(a)) or pretty_address(a) for a in addresses]
    if not names:
        return "Unknown"
    if len(names) <= 4:
        return ", ".join(names)
    return ", ".join(names[:4]) + f" +{len(names) - 4} more"


# ----- gathering ---------------------------------------------------------------------------------

def load_threads(dev: Device, contacts: dict) -> dict:
    canon = {
        to_int(r["_id"]): (r.get("address") or "")
        for r in dev.query("content://mms-sms/canonical-addresses", ["_id", "address"])
    }
    threads = {}
    for r in dev.query("content://mms-sms/conversations?simple=true", ["_id", "recipient_ids"]):
        tid = to_int(r["_id"])
        ids = [to_int(x) for x in (r.get("recipient_ids") or "").split()]
        addrs = [canon[i] for i in ids if i in canon]
        threads[tid] = Thread(tid, addrs, make_label(addrs, contacts))
    return threads


def load_mms(dev: Device) -> dict:
    mms = {}
    for r in dev.query("content://mms", ["_id", "thread_id", "date", "msg_box"], sort="_id ASC"):
        mid = to_int(r["_id"])
        mms[mid] = Mms(mid, to_int(r.get("thread_id"), 0), to_int(r.get("date"), 0),
                       to_int(r.get("msg_box"), 0))
    rows = dev.query("content://mms/part", ["_id", "mid", "ct", "cl", "name"], sort="_id ASC")
    for r in rows:
        mid = to_int(r.get("mid"))
        if mid not in mms:
            continue  # orphaned part, or a draft being composed
        mms[mid].parts.append(Part(to_int(r["_id"]), mid, r.get("ct") or "", r.get("cl"), r.get("name")))
    return mms


def count_sms(dev: Device) -> int:
    return len(dev.query("content://sms", ["_id"]))


# ----- attachment sizes ----------------------------------------------------------------------------
#
# The provider has no size column, and the files themselves aren't readable from the adb
# shell, so each attachment is streamed through `content read | wc -c` on the phone (no data
# crosses the USB cable). Results are cached on the Mac; attachment ids are never reused.

def _cache_path(data_dir: str) -> str:
    return os.path.join(data_dir, "attachment-sizes.json")


def load_size_cache(data_dir: str) -> dict:
    try:
        with open(_cache_path(data_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_size_cache(data_dir: str, cache: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    tmp = _cache_path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, _cache_path(data_dir))


def measure_sizes(dev: Device, parts: list, data_dir: str, workers: int = 6) -> None:
    """Fill in Part.size for media parts, measuring only those not already cached."""
    cache = load_size_cache(data_dir)
    todo = []
    for p in parts:
        hit = cache.get(str(p.id))
        if hit and hit[0] == p.mid and hit[1] == p.ct:
            p.size = hit[2]
        else:
            todo.append(p)
    if not todo:
        return
    by_id = {p.id: p for p in todo}
    workers = max(1, min(workers, len(todo)))
    chunks = [todo[i::workers] for i in range(workers)]
    scripts = []
    for i, chunk in enumerate(chunks):
        remote = dev.push_text("\n".join(str(p.id) for p in chunk) + "\n", f"sizes_ids_{i}.txt")
        scripts.append(
            f"while read id; do\n"
            f"  n=$(content read --uri content://mms/part/$id 2>/dev/null | wc -c)\n"
            f"  echo \"R $id $n\"\n"
            f"done < {remote}\n"
        )
    progress = Progress("Measuring attachment sizes", len(todo))
    measured = 0

    def on_line(line: str) -> None:
        nonlocal measured
        fields = line.split()
        if len(fields) >= 3:
            pid, size = to_int(fields[1]), to_int(fields[2])
            p = by_id.get(pid)
            if p is not None and size is not None:
                p.size = size
                cache[str(pid)] = [p.mid, p.ct, size]
                measured += 1
                progress.update(measured)
                if measured % 2000 == 0:
                    save_size_cache(data_dir, cache)

    try:
        dev.run_parallel(scripts, on_line, tag="sizes")
    finally:
        progress.finish()
        save_size_cache(data_dir, cache)
        dev.shell("rm -f /data/local/tmp/msgprune/sizes_ids_*", check=False)


def gather(dev: Device, data_dir: str, *, measure: bool = True, workers: int = 6,
           use_contacts: bool = True, quiet: bool = False) -> Inventory:
    def say(msg):
        if not quiet:
            print(msg, flush=True)

    say("Reading conversations…")
    contacts = load_contacts(dev) if use_contacts else {}
    threads = load_threads(dev, contacts)
    say("Reading MMS messages and attachments…")
    mms = load_mms(dev)
    sms_count = count_sms(dev)
    inv = Inventory(dev.serial, threads, mms, sms_count, bool(contacts))
    if measure:
        media = [p for p in inv.parts() if p.category != "text"]
        measure_sizes(dev, media, data_dir, workers=workers)
    return inv


# ----- storage numbers from Android's own daily survey ------------------------------------------

def _json_after(text: str, label: str):
    for line in text.splitlines():
        if line.startswith(label):
            try:
                return json.loads(line[len(label):].strip())
            except ValueError:
                return None
    return None


def storage_survey(dev: Device) -> dict:
    """App sizes from `dumpsys diskstats` (Android refreshes these about once a day).

    Returns {package: {"app": bytes, "data": bytes, "cache": bytes}} for the two packages that
    hold messages, plus "free"/"total" for the data partition when available.
    """
    out = dev.shell("dumpsys diskstats", check=False)
    result: dict = {}
    names = _json_after(out, "Package Names:")
    apps = _json_after(out, "App Sizes:")
    datas = _json_after(out, "App Data Sizes:")
    caches = _json_after(out, "Cache Sizes:")
    if isinstance(names, list) and all(isinstance(x, list) for x in (apps, datas, caches)):
        for pkg in (MESSAGES_PKG, TELEPHONY_PKG):
            if pkg in names:
                i = names.index(pkg)
                try:
                    result[pkg] = {"app": int(apps[i]), "data": int(datas[i]), "cache": int(caches[i])}
                except (IndexError, TypeError, ValueError):
                    pass
    m = re.search(r"Data-Free: (\d+)K / (\d+)K total", out)
    if m:
        result["free"] = int(m.group(1)) * 1024
        result["total"] = int(m.group(2)) * 1024
    return result


def summarize_by_thread(inv: Inventory, parts: list) -> list:
    """[(thread_id, count, bytes)] sorted by bytes desc."""
    acc = defaultdict(lambda: [0, 0])
    for p in parts:
        t = inv.mms[p.mid].thread_id
        acc[t][0] += 1
        acc[t][1] += p.size or 0
    return sorted(((t, c, b) for t, (c, b) in acc.items()), key=lambda x: -x[2])
