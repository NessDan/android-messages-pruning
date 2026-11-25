"""Which attachments a prune would remove. Shared by `scan` (estimates) and `prune` (the real list)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .inventory import MEDIA_CATEGORIES, PRUNABLE_BOXES, Inventory, Part, Thread, norm_address

CATEGORY_LABELS = {"image": "Photos", "video": "Videos", "audio": "Audio", "other": "Other files"}
NOUNS = {
    "image": ("photo", "photos"),
    "video": ("video", "videos"),
    "audio": ("audio clip", "audio clips"),
    "other": ("file", "files"),
}


@dataclass
class Filters:
    min_size: int = 100_000
    categories: tuple = MEDIA_CATEGORIES
    cutoff: dt.datetime | None = None  # only messages older than this
    only: tuple = ()  # conversation names/numbers to include (empty = all)
    skip: tuple = ()  # conversation names/numbers to leave alone


def thread_matches(thread: Thread | None, needles) -> bool:
    if thread is None:
        return False
    hay = [thread.label.lower()] + [a.lower() for a in thread.addresses]
    norms = {norm_address(a) for a in thread.addresses}
    for needle in needles:
        n = needle.strip().lower()
        if not n:
            continue
        if any(n in h for h in hay):
            return True
        nn = norm_address(needle)
        if len(nn) >= 7 and nn in norms:
            return True
    return False


def select(inv: Inventory, f: Filters) -> list:
    """Attachments to remove, oldest message first. Text parts are never candidates."""
    out = []
    for m in sorted(inv.mms.values(), key=lambda m: (m.date, m.id)):
        if m.msg_box not in PRUNABLE_BOXES:
            continue
        if f.cutoff is not None:
            when = m.when
            if when is None or when >= f.cutoff:
                continue
        thread = inv.threads.get(m.thread_id)
        if f.only and not thread_matches(thread, f.only):
            continue
        if f.skip and thread_matches(thread, f.skip):
            continue
        for p in m.parts:
            if p.category == "text" or p.is_placeholder or p.category not in f.categories:
                continue
            if not p.size or p.size < f.min_size:
                continue
            out.append(p)
    return out


def group_by_message(parts: list) -> list:
    """[(mid, [parts])] preserving order."""
    groups: dict = {}
    for p in parts:
        groups.setdefault(p.mid, []).append(p)
    return list(groups.items())


def placeholder_text(parts: list) -> str:
    """'[Photo removed to save space - 2.3 MB]' / '[2 photos and 1 video removed …]'."""
    from .util import fmt_size

    counts: dict = {}
    for p in parts:
        counts[p.category] = counts.get(p.category, 0) + 1
    total = sum(p.size or 0 for p in parts)
    if len(parts) == 1:
        desc = NOUNS[parts[0].category][0].capitalize()
    else:
        bits = [f"{n} {NOUNS[c][0] if n == 1 else NOUNS[c][1]}"
                for c in MEDIA_CATEGORIES if (n := counts.get(c))]
        desc = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
    return f"[{desc} removed to save space - {fmt_size(total)}]"


def limit_whole_messages(parts: list, limit: int | None) -> list:
    """Cut the list after ~limit attachments without splitting a message."""
    if not limit or limit <= 0:
        return parts
    out: list = []
    for _mid, group in group_by_message(parts):
        if len(out) >= limit:
            break
        out.extend(group)
    return out
