"""Removing MMS attachments from the phone while keeping every word of text.

Safety design
-------------
* Dry run unless --apply. Even with --apply you have to type "prune" (or pass --yes).
* Text parts (text/plain, SMIL layout) are never candidates; drafts/outbox are never touched.
* Every write names exactly one row in its URI (content://mms/part/<id>,
  content://mms/<message id>/part). No bulk "WHERE" deletes, so a mistake can't widen.
* Attachments are copied to the computer and checked (byte count + SHA-256) before anything is
  deleted; a part whose copy failed is left alone.
* The first message is done on its own and verified; if Android silently ignored the
  write (it does that instead of erroring), the run stops there.
* The adb shell's permission to write to the SMS/MMS store (app-op WRITE_SMS) is switched on
  only for the duration of the run and put back afterwards, including on Ctrl-C.
* Everything done is appended to a JSON-lines journal as it happens.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .adb import AdbError, Device, insert_args, quote, to_int
from .inventory import PLACEHOLDER_PREFIX, Inventory, Mms, Part, summarize_by_thread
from .selection import CATEGORY_LABELS, Filters, group_by_message, limit_whole_messages, placeholder_text, select
from .util import Progress, fmt_size, plural, safe_name, show_path

SHELL_PKG = "com.android.shell"
MIN_FREE_AFTER_BACKUP = 5 * 10**9  # leave at least 5 GB free on the Mac
VERIFY_CHUNK = 300  # ids per verification query (keeps adb command lines short)
BATCH_MESSAGES = 250


class PruneAborted(Exception):
    pass


@dataclass
class PruneOptions:
    filters: Filters = field(default_factory=Filters)
    limit: int | None = None
    apply: bool = False
    assume_yes: bool = False
    backup: bool = True
    backup_dir: str = ""
    placeholder: bool = True
    workers: int = 6
    download_workers: int = 4


# ----- journal --------------------------------------------------------------------------------

class Journal:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, event: str, **kw) -> None:
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
        self.fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.fh.flush()

    def sync(self) -> None:
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self) -> None:
        self.sync()
        self.fh.close()


# ----- write permission (app-op WRITE_SMS for the adb shell) ------------------------------------

_MODE_LINE = re.compile(r"^(Uid mode:\s*)?WRITE_SMS:\s*(\w+)")


def read_write_sms_mode(dev: Device) -> dict:
    out = dev.shell(quote(["appops", "get", SHELL_PKG, "WRITE_SMS"]), check=False)
    state = {"package": None, "uid": None, "default": None, "raw": out.strip()}
    for line in out.splitlines():
        line = line.strip()
        m = _MODE_LINE.match(line)
        if m:
            state["uid" if m.group(1) else "package"] = m.group(2)
            continue
        m = re.match(r"^Default mode:\s*(\w+)", line)
        if m:
            state["default"] = m.group(1)
    return state


class WriteAccess:
    """Lets the adb shell write to the SMS/MMS store while the `with` block runs.

    Only Android's default SMS app may write there normally; for everyone else Android
    *silently ignores* writes (app-op WRITE_SMS = ignore). `appops set` flips that for the
    shell user, and __exit__ puts back exactly what was there before.
    """

    def __init__(self, dev: Device, journal: Journal | None = None):
        self.dev = dev
        self.journal = journal
        self.before: dict = {}
        self.uid_escalated = False

    def _set(self, mode: str, uid: bool = False) -> None:
        args = ["appops", "set"] + (["--uid"] if uid else []) + [SHELL_PKG, "WRITE_SMS", mode]
        self.dev.shell(quote(args))

    def __enter__(self) -> "WriteAccess":
        self.before = read_write_sms_mode(self.dev)
        if self.journal:
            self.journal.write("appops_before", state=self.before)
        self._set("allow")
        return self

    def escalate(self) -> None:
        """Some builds evaluate the per-uid mode first; allow that too."""
        if not self.uid_escalated:
            self._set("allow", uid=True)
            self.uid_escalated = True

    def __exit__(self, *exc) -> bool:
        fallback = self.before.get("default") or "ignore"
        todo = [(self.before.get("package") or fallback, False)]
        if self.uid_escalated:
            todo.append((self.before.get("uid") or fallback, True))
        for mode, uid in todo:
            try:
                self._set(mode, uid=uid)
            except Exception as e:  # phone unplugged, etc.
                flag = "--uid " if uid else ""
                print(f"\nWARNING: couldn't restore the phone's WRITE_SMS setting ({e}).\n"
                      f"Run this once the phone is connected:\n"
                      f"  adb shell appops set {flag}{SHELL_PKG} WRITE_SMS {mode}", file=sys.stderr)
        if self.journal:
            try:
                self.journal.write("appops_after", state=read_write_sms_mode(self.dev))
            except Exception:
                pass
        return False


@contextlib.contextmanager
def signals_as_interrupt():
    """Make `kill` / closing the terminal unwind like Ctrl-C so cleanup still runs."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            old[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


# ----- backups on the Mac ---------------------------------------------------------------------

_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/heic": ".heic", "image/heif": ".heif", "image/webp": ".webp", "image/bmp": ".bmp",
    "video/mp4": ".mp4", "video/3gpp": ".3gp", "video/3gpp2": ".3g2", "video/quicktime": ".mov",
    "video/webm": ".webm", "audio/amr": ".amr", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/3gpp": ".3gp", "audio/x-wav": ".wav",
    "audio/wav": ".wav", "text/x-vcard": ".vcf", "text/vcard": ".vcf", "application/pdf": ".pdf",
}
INDEX_FIELDS = ["part_id", "message_id", "conversation", "date", "direction", "type", "size",
                "sha256", "file"]


def _ext_for(p: Part) -> str:
    for cand in (p.name, p.cl):
        if cand:
            m = re.search(r"\.([A-Za-z0-9]{1,5})$", cand.strip())
            if m:
                return "." + m.group(1).lower()
    ct = (p.ct or "").lower().split(";")[0].strip()
    return _EXT.get(ct) or mimetypes.guess_extension(ct) or ".bin"


def backup_relpath(inv: Inventory, p: Part) -> str:
    m = inv.mms[p.mid]
    folder = safe_name(f"{inv.thread_label(m.thread_id)} [{m.thread_id}]", 90)
    when = m.when
    stamp = when.strftime("%Y-%m-%d_%H-%M-%S") if when else "unknown-date"
    return os.path.join("attachments", folder, f"{stamp}_{m.direction}_p{p.id}{_ext_for(p)}")


class BackupStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "index.csv")
        self.known: dict = {}
        if os.path.exists(self.index_path):
            with open(self.index_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    pid = to_int(row.get("part_id"))
                    if pid is not None:
                        self.known[pid] = row

    def has_good_copy(self, p: Part) -> bool:
        row = self.known.get(p.id)
        if not row or to_int(row.get("size")) != p.size:
            return False
        path = os.path.join(self.root, row["file"])
        return os.path.isfile(path) and os.path.getsize(path) == p.size

    def record(self, row: dict) -> None:
        new = not os.path.exists(self.index_path)
        with open(self.index_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        self.known[int(row["part_id"])] = row


def _fetch_one(dev: Device, inv: Inventory, store: BackupStore, p: Part):
    try:
        data, err, rc = dev.read(f"content://mms/part/{p.id}")
    except Exception as e:  # timeout, adb vanished
        return p, None, str(e)
    if rc != 0 or len(data) != p.size:
        return p, None, f"expected {p.size} bytes, got {len(data)} (exit {rc}) {err.strip()[:200]}"
    rel = backup_relpath(inv, p)
    path = os.path.join(store.root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".partial"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    m = inv.mms[p.mid]
    when = m.when
    if when:
        ts = when.timestamp()
        os.utime(path, (ts, ts))
    row = {
        "part_id": p.id, "message_id": p.mid, "conversation": inv.thread_label(m.thread_id),
        "date": when.isoformat(sep=" ") if when else "", "direction": m.direction, "type": p.ct,
        "size": p.size, "sha256": hashlib.sha256(data).hexdigest(), "file": rel,
    }
    return p, row, None


def backup_all(dev: Device, inv: Inventory, parts: list, store: BackupStore, workers: int,
               journal: Journal) -> tuple:
    todo = [p for p in parts if not store.has_good_copy(p)]
    ok = {p.id for p in parts} - {p.id for p in todo}
    need = sum(p.size or 0 for p in todo)
    free = shutil.disk_usage(store.root).free
    if free - need < MIN_FREE_AFTER_BACKUP:
        raise PruneAborted(
            f"Not enough space on this computer for the backup copies: they need {fmt_size(need)}, "
            f"{fmt_size(free)} is free, and msgprune keeps {fmt_size(MIN_FREE_AFTER_BACKUP)} spare.\n"
            "Options: --backup-dir /Volumes/<external drive>/msgprune, a narrower selection "
            "(--min-size, --older-than, --only), or --no-backup if your old phone still has everything."
        )
    failed = []
    if ok:
        print(f"{plural(len(ok), 'attachment')} already backed up on an earlier run.")
    if not todo:
        return ok, failed
    print(f"Copying {plural(len(todo), 'attachment')} ({fmt_size(need)}) to {show_path(store.root)} …")
    progress = Progress("Backing up", len(todo))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_fetch_one, dev, inv, store, p) for p in todo]
        try:
            for fut in as_completed(futures):
                p, row, err = fut.result()
                if row:
                    ok.add(p.id)
                    store.record(row)
                    journal.write("backed_up", part=p.id, mid=p.mid, size=p.size,
                                  sha256=row["sha256"], file=row["file"])
                else:
                    failed.append((p, err))
                    journal.write("backup_failed", part=p.id, mid=p.mid, error=err)
                progress.advance()
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise
    progress.finish()
    return ok, failed


# ----- deleting on the phone ------------------------------------------------------------------

_PH_RE = re.compile(re.escape(PLACEHOLDER_PREFIX) + r"(\d+)_([\d-]+)\.txt$")


def _coverage(rows: list) -> dict:
    """mid -> set of attachment ids already described by one of our placeholder notes."""
    cov: dict = {}
    for r in rows:
        m = _PH_RE.match(r.get("cl") or "")
        if m:
            cov.setdefault(int(m.group(1)), set()).update(int(x) for x in m.group(2).split("-") if x)
    return cov


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _parts_of_messages(dev: Device, mids) -> list:
    rows: list = []
    for chunk in _chunks(sorted(set(mids)), VERIFY_CHUNK):
        rows += dev.query("content://mms/part", ["_id", "mid", "cl"],
                          where=f"mid IN ({','.join(map(str, chunk))})")
    return rows


def _still_present(dev: Device, part_ids) -> set:
    ids = sorted(set(part_ids))
    present: set = set()
    for chunk in _chunks(ids, VERIFY_CHUNK):
        rows = dev.query("content://mms/part", ["_id"], where=f"_id IN ({','.join(map(str, chunk))})")
        present |= {to_int(r["_id"]) for r in rows}
    return present & set(ids)


def _insert_line(m: Mms, parts: list) -> str:
    ids = sorted(p.id for p in parts)
    binds = [
        ("ct", "s", "text/plain"),
        ("chset", "i", 106),  # UTF-8
        ("seq", "i", 0),
        ("cid", "s", f"<msgprune_{m.id}_{ids[0]}>"),
        ("cl", "s", f"{PLACEHOLDER_PREFIX}{m.id}_{'-'.join(map(str, ids))}.txt"),
        ("text", "s", placeholder_text(parts)),
    ]
    return quote(insert_args(f"content://mms/{m.id}/part", binds)) + f'; echo "R ins {m.id} $?"'


def _delete_line(p: Part) -> str:
    return quote(["content", "delete", "--uri", f"content://mms/part/{p.id}"]) + f'; echo "R del {p.id} $?"'


def _run_lines(dev: Device, lines: list, workers: int, tag: str, journal: Journal) -> None:
    if not lines:
        return
    workers = max(1, min(workers, len(lines)))
    scripts = ["".join(line + "\n" for line in lines[i::workers]) for i in range(workers)]
    stray = dev.run_parallel(scripts, lambda line: None, tag=tag)
    if stray:
        journal.write("phone_output", tag=tag, lines=stray[:50])


def process_batch(dev: Device, inv: Inventory, batch: list, opts: PruneOptions, journal: Journal,
                  sequential: bool = False) -> tuple:
    """batch: [(Mms, [Part])]. Returns (deleted parts, parts that couldn't be removed)."""
    workers = 1 if sequential else opts.workers
    rows = _parts_of_messages(dev, [m.id for m, _ in batch])
    present = {to_int(r["_id"]) for r in rows}
    cov = _coverage(rows)
    # Drop anything already gone (e.g. a re-run after an interruption).
    batch = [(m, [p for p in ps if p.id in present]) for m, ps in batch]
    batch = [(m, ps) for m, ps in batch if ps]
    blocked: list = []

    if opts.placeholder:
        need = [(m, ps) for m, ps in batch if not {p.id for p in ps} <= cov.get(m.id, set())]
        for attempt in range(2):
            if not need:
                break
            _run_lines(dev, [_insert_line(m, ps) for m, ps in need], 1 if attempt else workers,
                       "ins", journal)
            cov = _coverage(_parts_of_messages(dev, [m.id for m, _ in need]))
            done = [(m, ps) for m, ps in need if {p.id for p in ps} <= cov.get(m.id, set())]
            for m, ps in done:
                journal.write("placeholder", mid=m.id, covers=sorted(p.id for p in ps),
                              text=placeholder_text(ps))
            need = [(m, ps) for m, ps in need if not {p.id for p in ps} <= cov.get(m.id, set())]
            if sequential:
                break
        blocked = need
        for m, ps in blocked:
            journal.write("placeholder_failed", mid=m.id, parts=sorted(p.id for p in ps))

    blocked_mids = {m.id for m, _ in blocked}
    targets = [p for m, ps in batch if m.id not in blocked_mids for p in ps]
    remaining: set = set()
    if targets:
        _run_lines(dev, [_delete_line(p) for p in targets], workers, "del", journal)
        remaining = _still_present(dev, [p.id for p in targets])
        if remaining and not sequential:
            _run_lines(dev, [_delete_line(p) for p in targets if p.id in remaining], 1, "del", journal)
            remaining = _still_present(dev, remaining)
    deleted = [p for p in targets if p.id not in remaining]
    failed = [p for p in targets if p.id in remaining] + [p for _, ps in blocked for p in ps]
    for p in deleted:
        m = inv.mms[p.mid]
        journal.write("deleted", part=p.id, mid=p.mid, thread=m.thread_id, ct=p.ct, size=p.size,
                      date=m.when.isoformat() if m.when else None)
    for p in failed:
        journal.write("not_deleted", part=p.id, mid=p.mid)
    journal.sync()
    return deleted, failed


# ----- the command ------------------------------------------------------------------------------

def _describe_rules(opts: PruneOptions) -> str:
    f = opts.filters
    kinds = ", ".join(CATEGORY_LABELS[c].lower() for c in f.categories)
    bits = [f"{kinds} of {fmt_size(f.min_size)} or more", "sent and received"]
    bits.append(f"only messages before {f.cutoff:%Y-%m-%d}" if f.cutoff else "any age")
    if f.only:
        bits.append("only conversations matching " + ", ".join(repr(x) for x in f.only))
    if f.skip:
        bits.append("skipping conversations matching " + ", ".join(repr(x) for x in f.skip))
    return "; ".join(bits)


def print_plan(inv: Inventory, targets: list, opts: PruneOptions, device_name: str) -> None:
    total = sum(p.size for p in targets)
    msgs = len({p.mid for p in targets})
    print()
    print(f"PRUNE {'PLAN' if opts.apply else 'PREVIEW'} for {device_name}")
    print(f"  Rules: {_describe_rules(opts)}")
    if opts.limit:
        print(f"  Limited to the oldest ~{opts.limit} attachment(s) (whole messages).")
    print(f"  Matches {plural(len(targets), 'attachment')} in {plural(msgs, 'message')}: {fmt_size(total)}")
    if not targets:
        return
    by_cat: dict = {}
    for p in targets:
        n, b = by_cat.get(p.category, (0, 0))
        by_cat[p.category] = (n + 1, b + p.size)
    for c, (n, b) in sorted(by_cat.items(), key=lambda kv: -kv[1][1]):
        print(f"    {CATEGORY_LABELS[c]:<12}{n:>10,}{fmt_size(b):>11}")
    ranked = summarize_by_thread(inv, targets)
    print("  Conversations most affected:")
    for tid, n, b in ranked[:8]:
        print(f"    {fmt_size(b):>9}  {n:>7,}  {inv.thread_label(tid)[:70]}")
    if len(ranked) > 8:
        print(f"    … and {len(ranked) - 8:,} more conversations")
    dates = [inv.mms[p.mid].when for p in targets if inv.mms[p.mid].when]
    if dates:
        print(f"  Message dates: {min(dates):%Y-%m-%d} to {max(dates):%Y-%m-%d}")
    print("  The text of every message stays.", end=" ")
    if opts.placeholder:
        print('Each pruned message gets a short note such as\n  "[Photo removed to save space - 2.3 MB]" (turn off with --no-placeholder).')
    else:
        print()
    if opts.backup:
        print(f"  Copies of the attachments are saved to {show_path(opts.backup_dir)} first.")
    else:
        print("  NO backup copies will be made (--no-backup).")


def _confirm(targets: list) -> bool:
    if not sys.stdin.isatty():
        print("Refusing to continue without a terminal to confirm in; add --yes if you're sure.")
        return False
    total = fmt_size(sum(p.size for p in targets))
    try:
        answer = input(f"\nType 'prune' to remove {plural(len(targets), 'attachment')} ({total}) "
                       f"from the phone, anything else to cancel: ")
    except EOFError:  # Ctrl-D / closed input counts as "no"
        print()
        return False
    return answer.strip().lower() == "prune"


def run_prune(dev: Device, inv: Inventory, opts: PruneOptions, data_dir: str, info: dict) -> int:
    targets = limit_whole_messages(select(inv, opts.filters), opts.limit)
    device_name = f"{info.get('model') or dev.model} ({dev.serial})"
    print_plan(inv, targets, opts, device_name)
    if not targets:
        print("\nNothing to do.")
        return 0
    if not opts.apply:
        print("\nNothing has been changed. To do this for real, add --apply "
              "(try it on a few first: --apply --limit 5).")
        return 0
    if not opts.assume_yes and not _confirm(targets):
        print("Cancelled; nothing was changed.")
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    journal = Journal(os.path.join(data_dir, "journals", f"{stamp}-prune.jsonl"))
    journal.write("run_start", device=info, rules=_describe_rules(opts), limit=opts.limit,
                  placeholder=opts.placeholder, backup_dir=opts.backup_dir if opts.backup else None,
                  candidates=len(targets), bytes=sum(p.size for p in targets))
    deleted: list = []
    failed: list = []
    backup_failed: list = []
    try:
        if opts.backup:
            store = BackupStore(opts.backup_dir)
            ok, backup_failed = backup_all(dev, inv, targets, store, opts.download_workers, journal)
            if backup_failed:
                print(f"WARNING: {plural(len(backup_failed), 'attachment')} couldn't be copied and "
                      f"will be left on the phone. Details in {show_path(journal.path)}")
            targets = [p for p in targets if p.id in ok]
            journal.sync()
        groups = [(inv.mms[mid], ps) for mid, ps in group_by_message(targets)]
        if not groups:
            print("Nothing left to remove.")
            return 1 if backup_failed else 0

        with signals_as_interrupt(), WriteAccess(dev, journal) as access:
            progress = Progress("Pruning", len(targets))
            # Canary: first message alone, fully verified, before anything else is touched.
            d, f = process_batch(dev, inv, groups[:1], opts, journal, sequential=True)
            if f:
                access.escalate()
                d2, f = process_batch(dev, inv, groups[:1], opts, journal, sequential=True)
                d += d2
            deleted += d
            if f:
                raise PruneAborted(
                    "The phone didn't accept the change on the first message, so msgprune stopped "
                    "there.\nAndroid ignores writes to the SMS/MMS store from anything but the default "
                    "SMS app unless the WRITE_SMS app-op is allowed, and this phone didn't honour it.\n"
                    + ("If only the placeholder note failed, retry with --no-placeholder.\n"
                       if opts.placeholder else "")
                    + f"Journal: {show_path(journal.path)}"
                )
            progress.update(len(deleted))
            for batch in _chunks(groups[1:], BATCH_MESSAGES):
                d, f = process_batch(dev, inv, batch, opts, journal)
                deleted += d
                failed += f
                progress.update(len(deleted) + len(failed))
                if f and not d:
                    raise PruneAborted("The phone stopped accepting changes; stopping. Re-run the same "
                                       f"command to pick up where it left off. Journal: {show_path(journal.path)}")
            progress.finish()
    except KeyboardInterrupt:
        print("\nStopped. Everything already removed is listed in the journal; re-run the same "
              "command to carry on.", file=sys.stderr)
        journal.write("interrupted", deleted=len(deleted))
        return 130
    except (PruneAborted, AdbError) as e:
        journal.write("aborted", error=str(e), deleted=len(deleted))
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        return 2
    finally:
        journal.write("run_end", deleted=len(deleted), bytes=sum(p.size for p in deleted),
                      failed=len(failed), backup_failed=len(backup_failed))
        journal.close()
        dev.cleanup_remote()
        if deleted or failed:
            print(f"\nRemoved {plural(len(deleted), 'attachment')} ({fmt_size(sum(p.size for p in deleted))}) "
                  f"from {plural(len({p.mid for p in deleted}), 'message')}. No text was removed.")
            if failed:
                print(f"{plural(len(failed), 'attachment')} couldn't be removed and are still on the phone.")
            if opts.backup:
                print(f"Backup copies: {show_path(opts.backup_dir)}  (index.csv lists every file)")
            print(f"Journal: {show_path(journal.path)}")
    return 0 if not failed and not backup_failed else 1
