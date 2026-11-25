"""Command line: ./msgprune {devices,doctor,scan,prune}"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from . import __version__
from .adb import AdbError, find_adb, list_devices, pick_device
from .inventory import MEDIA_CATEGORIES, MESSAGES_PKG, gather, storage_survey
from .prune import PruneOptions, read_write_sms_mode, run_prune
from .report import device_info, scan_report
from .selection import Filters
from .util import fmt_size, parse_cutoff, parse_size, safe_name, show_path

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _device_dir(base: str, dev, info: dict | None = None) -> str:
    model = (info or {}).get("model") or dev.model
    return os.path.join(base, safe_name(f"{model}-{dev.serial}", 80).replace(" ", "-"))


def cmd_devices(args) -> int:
    adb = find_adb(args.adb)
    entries = list_devices(adb)
    if not entries:
        print("No phones connected. Run `./msgprune doctor` for setup steps.")
        return 1
    for e in entries:
        print(f"{e.serial:<24} {e.state:<13} {e.model}")
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def line(good, text):
        nonlocal ok
        mark = {True: "OK  ", False: "FAIL", None: "info"}[good]
        if good is False:
            ok = False
        print(f"[{mark}] {text}")

    try:
        adb = find_adb(args.adb)
        ver = subprocess.run([adb, "version"], capture_output=True, text=True).stdout.splitlines()
        line(True, f"adb: {show_path(adb)} ({ver[1].strip() if len(ver) > 1 else ver[0] if ver else '?'})")
    except AdbError as e:
        line(False, str(e))
        return 1
    try:
        dev = pick_device(adb, args.device)
    except AdbError as e:
        line(False, str(e))
        return 1
    info = device_info(dev)
    line(True, f"Phone: {info['model']}, Android {info['android']} (serial {dev.serial})")
    if info.get("default_sms") == MESSAGES_PKG:
        line(True, f"Default SMS app: Google Messages {info.get('messages_version') or ''}".rstrip())
    else:
        line(None, f"Default SMS app: {info.get('default_sms') or 'unknown'}")
    for uri, what in (("content://sms", "SMS"), ("content://mms", "MMS"), ("content://mms/part", "MMS attachments")):
        try:
            dev.query(uri, ["_id"], where="_id<0")
            line(True, f"Can read the {what} store")
        except AdbError as e:
            line(False, f"Can't read the {what} store: {str(e).splitlines()[-1]}")
    try:
        media = [r for r in dev.query("content://mms/part", ["_id", "ct"])
                 if (r.get("ct") or "").startswith(("image/", "video/"))]
        if media:
            data, err, rc = dev.read(f"content://mms/part/{media[-1]['_id']}")
            if rc == 0 and data:
                line(True, f"Can read attachment files ({len(media):,} photos/videos found)")
            else:
                line(False, f"Can't read attachment files: {err.strip()[:200] or 'empty result'}")
        else:
            line(None, "No MMS photos/videos on this phone")
    except AdbError as e:
        line(False, f"Can't list attachments: {str(e).splitlines()[-1]}")
    try:
        dev.query("content://com.android.contacts/data/phones", ["_id"], where="_id<0")
        line(True, "Can read contacts (used only to name conversations)")
    except AdbError:
        line(None, "Can't read contacts; conversations will be shown by phone number")
    state = read_write_sms_mode(dev)
    current = state["package"] or state["uid"] or (state["default"] and f"{state['default']} (default)") or "?"
    line(None, f"adb shell's WRITE_SMS app-op: {current}. `prune --apply` allows it only while it runs.")
    line(None, f"Free space on this computer for backups: {fmt_size(shutil.disk_usage(PROJECT_DIR).free)}")
    print("\nAll set." if ok else "\nFix the FAIL lines above, then run doctor again.")
    return 0 if ok else 1


def cmd_scan(args) -> int:
    dev = pick_device(find_adb(args.adb), args.device)
    info = device_info(dev)
    data_dir = args.data_dir or _device_dir(os.path.join(PROJECT_DIR, "data"), dev, info)
    print(f"Scanning {info['model']} ({dev.serial}). This only reads; nothing on the phone changes.")
    survey = storage_survey(dev)
    inv = gather(dev, data_dir, workers=args.workers, use_contacts=not args.no_contacts)
    text, data = scan_report(inv, info, survey, top=args.top)
    print()
    print(text, end="")
    os.makedirs(data_dir, exist_ok=True)
    stamp = data["generated"].replace(":", "").replace("-", "")
    with open(os.path.join(data_dir, f"scan-{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(data_dir, f"scan-{stamp}.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    print(f"\n(Saved to {show_path(data_dir)})")
    dev.cleanup_remote()
    return 0


def cmd_prune(args) -> int:
    dev = pick_device(find_adb(args.adb), args.device)
    info = device_info(dev)
    data_dir = args.data_dir or _device_dir(os.path.join(PROJECT_DIR, "data"), dev, info)
    categories = tuple(c.strip() for c in args.types.split(",") if c.strip())
    bad = [c for c in categories if c not in MEDIA_CATEGORIES]
    if bad or not categories:
        print(f"--types takes a comma-separated list from: {', '.join(MEDIA_CATEGORIES)}", file=sys.stderr)
        return 2
    filters = Filters(
        min_size=args.min_size,
        categories=categories,
        cutoff=args.older_than,
        only=tuple(args.only or ()),
        skip=tuple(args.skip or ()),
    )
    opts = PruneOptions(
        filters=filters,
        limit=args.limit,
        apply=args.apply,
        assume_yes=args.yes,
        backup=not args.no_backup,
        backup_dir=args.backup_dir or _device_dir(os.path.join(PROJECT_DIR, "backups"), dev, info),
        placeholder=not args.no_placeholder,
        workers=args.workers,
        download_workers=args.download_workers,
    )
    print(f"Reading the SMS/MMS store on {info['model']} ({dev.serial}) …")
    inv = gather(dev, data_dir, workers=args.workers, use_contacts=not args.no_contacts)
    return run_prune(dev, inv, opts, data_dir, info)


def _size_arg(text: str) -> int:
    try:
        return parse_size(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _cutoff_arg(text: str):
    try:
        return parse_cutoff(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="msgprune",
        description="Shrink an Android phone's SMS/MMS history by removing large attachments, "
                    "keeping all the text. Read-only unless you run `prune --apply`.",
    )
    p.add_argument("--version", action="version", version=f"msgprune {__version__}")
    p.add_argument("-d", "--device", help="which phone (serial, or part of the model name like 'Pixel 11')")
    p.add_argument("--adb", help="path to adb (default: found automatically)")
    p.add_argument("--data-dir", help="where scans, the size cache and journals go "
                                      "(default: ./data/<phone>)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sp = sub.add_parser("devices", help="list connected phones")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("doctor", help="check the setup (read-only)")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("scan", help="measure where the messages storage goes (read-only)")
    sp.add_argument("--workers", type=int, default=6, help="parallel jobs on the phone (default 6)")
    sp.add_argument("--top", type=int, default=15, help="how many conversations to list (default 15)")
    sp.add_argument("--no-contacts", action="store_true", help="don't read contact names")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser(
        "prune", help="remove large MMS attachments, keep the text (preview unless --apply)",
        description="Preview (default) or remove MMS attachments. Text is never removed.",
    )
    sp.add_argument("--min-size", type=_size_arg, default=Filters().min_size,
                    help="only attachments at least this big (default 100KB; e.g. 500KB, 2MB)")
    sp.add_argument("--types", default=",".join(MEDIA_CATEGORIES),
                    help="which kinds: image,video,audio,other (default: all four)")
    sp.add_argument("--older-than", type=_cutoff_arg, metavar="AGE|DATE",
                    help="only messages older than this: 90d, 12w, 6mo, 2y or 2023-01-31")
    sp.add_argument("--only", action="append", metavar="NAME|NUMBER",
                    help="only conversations matching this name or number (repeatable)")
    sp.add_argument("--skip", action="append", metavar="NAME|NUMBER",
                    help="leave conversations matching this name or number alone (repeatable)")
    sp.add_argument("--limit", type=int, help="stop after about this many attachments, oldest first")
    sp.add_argument("--apply", action="store_true", help="actually remove them (otherwise: preview)")
    sp.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    sp.add_argument("--backup-dir", help="where to copy attachments before removing them "
                                         "(default: ./backups/<phone>)")
    sp.add_argument("--no-backup", action="store_true", help="don't copy attachments to this computer first")
    sp.add_argument("--no-placeholder", action="store_true",
                    help="don't add a '[Photo removed …]' note to pruned messages")
    sp.add_argument("--no-contacts", action="store_true", help="don't read contact names")
    sp.add_argument("--workers", type=int, default=6, help="parallel jobs on the phone (default 6)")
    sp.add_argument("--download-workers", type=int, default=4,
                    help="parallel copies to this computer (default 4)")
    sp.set_defaults(func=cmd_prune)
    return p


def main(argv=None) -> int:
    try:  # keep stdout and the stderr progress lines in order when piped to a file
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AdbError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
