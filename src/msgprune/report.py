"""Turning an inventory into something a person can read."""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from .adb import Device
from .inventory import MEDIA_CATEGORIES, MESSAGES_PKG, PRUNABLE_BOXES, TELEPHONY_PKG, Inventory
from .selection import CATEGORY_LABELS, Filters, select
from .util import fmt_size, plural


def device_info(dev: Device) -> dict:
    info = {
        "serial": dev.serial,
        "model": dev.getprop("ro.product.model") or dev.model,
        "android": dev.getprop("ro.build.version.release"),
        "sdk": dev.getprop("ro.build.version.sdk"),
    }
    pkg = dev.shell(f"dumpsys package {MESSAGES_PKG}", check=False)
    m = re.search(r"versionName=(\S+)", pkg)
    info["messages_version"] = m.group(1) if m else None
    holders = dev.shell("cmd role get-role-holders android.app.role.SMS", check=False).strip()
    if not re.fullmatch(r"[\w.;\s]+", holders or ""):
        holders = dev.shell("settings get secure sms_default_application", check=False).strip()
    info["default_sms"] = holders.split(";")[0].strip() if holders else None
    return info


def _row(label: str, value: str, note: str = "", width: int = 34) -> str:
    dots = "." * max(2, width - len(label))
    line = f"  {label} {dots} {value:>9}"
    return line + (f"   {note}" if note else "")


def scan_report(inv: Inventory, info: dict, survey: dict, top: int = 15) -> tuple:
    lines: list = []
    data: dict = {"device": info, "generated": dt.datetime.now().isoformat(timespec="seconds")}
    add = lines.append

    title = f"{info.get('model')} (Android {info.get('android')})"
    add(f"msgprune scan: {title}, {dt.datetime.now():%Y-%m-%d %H:%M}")
    default_sms = info.get("default_sms") or "unknown"
    if default_sms == MESSAGES_PKG:
        default_sms = "Google Messages"
    add(f"Default SMS app: {default_sms}" + (f"  (Messages {info['messages_version']})"
                                            if info.get("messages_version") else ""))
    add("")

    # --- where the space goes ------------------------------------------------------------
    gm, tel = survey.get(MESSAGES_PKG), survey.get(TELEPHONY_PKG)
    data["storage_survey"] = survey
    add("WHERE THE MESSAGES STORAGE IS  (Android's own storage survey, refreshed about daily)")
    if gm or tel:
        if gm:
            add(_row("Google Messages app storage", fmt_size(gm["data"]),
                     "RCS photos/videos + its database: out of msgprune's reach"))
        if tel:
            add(_row("SMS/MMS system store", fmt_size(tel["data"]),
                     "SMS + MMS incl. attachments: msgprune can prune these"))
    else:
        add("  (Android hasn't published its storage survey yet; a new phone does this within a day.)")
        add("  Live numbers: Settings > Apps > Messages > Storage & cache.")
    if survey.get("free") and survey.get("total"):
        add(_row("Phone free space", fmt_size(survey["free"]), f"of {fmt_size(survey['total'])}"))
    add("")

    # --- the SMS/MMS store -----------------------------------------------------------------
    all_media = [p for p in inv.parts() if p.category != "text" and not p.is_placeholder]
    prunable_media = [p for p in all_media if inv.mms[p.mid].msg_box in PRUNABLE_BOXES]
    unknown = [p for p in all_media if not p.size]  # `content read` failed: file missing/empty
    total_media = sum(p.size or 0 for p in all_media)
    add("SMS/MMS SYSTEM STORE")
    add(f"  {plural(inv.sms_count, 'SMS message')} (text only, nothing to prune)")
    add(f"  {plural(len(inv.mms), 'MMS message')} with {plural(len(all_media), 'attachment')} "
        f"totalling {fmt_size(total_media)}")
    placeholders = sum(1 for p in inv.parts() if p.is_placeholder)
    if placeholders:
        add(f"  {plural(placeholders, 'message')} already pruned by msgprune")
    if unknown:
        add(f"  ({plural(len(unknown), 'attachment')} couldn't be measured: file missing on the phone)")
    add("")

    by_cat: dict = defaultdict(lambda: [0, 0])
    for p in all_media:
        by_cat[p.category][0] += 1
        by_cat[p.category][1] += p.size or 0
    add(f"  {'Attachments by type':<24}{'count':>10}{'size':>12}")
    for c in MEDIA_CATEGORIES:
        n, b = by_cat.get(c, [0, 0])
        add(f"    {CATEGORY_LABELS[c]:<22}{n:>10,}{fmt_size(b):>12}")
    data["by_type"] = {c: {"count": v[0], "bytes": v[1]} for c, v in by_cat.items()}
    add("")

    buckets = [(0, 100_000, "under 100 KB"), (100_000, 500_000, "100-500 KB"),
               (500_000, 1_000_000, "500 KB-1 MB"), (1_000_000, 5_000_000, "1-5 MB"),
               (5_000_000, 20_000_000, "5-20 MB"), (20_000_000, None, "20 MB and up")]
    add(f"  {'Attachment sizes':<24}{'count':>10}{'size':>12}")
    data["size_buckets"] = []
    for lo, hi, label in buckets:
        sel = [p for p in all_media if p.size is not None and p.size >= lo and (hi is None or p.size < hi)]
        b = sum(p.size for p in sel)
        add(f"    {label:<22}{len(sel):>10,}{fmt_size(b):>12}")
        data["size_buckets"].append({"label": label, "count": len(sel), "bytes": b})
    add("")

    add("  What `msgprune prune` would free (photos, videos, audio, files; sent + received):")
    data["prune_estimates"] = []
    for threshold in (50_000, 100_000, 250_000, 500_000, 1_000_000, 5_000_000):
        sel = select(inv, Filters(min_size=threshold))
        b = sum(p.size for p in sel)
        mark = "   <- default" if threshold == Filters().min_size else ""
        add(f"    --min-size {fmt_size(threshold):<9}{len(sel):>10,} attachments{fmt_size(b):>12}{mark}")
        data["prune_estimates"].append({"min_size": threshold, "count": len(sel), "bytes": b})
    add("")

    # --- biggest conversations ---------------------------------------------------------------
    per_thread: dict = defaultdict(lambda: [0, 0])
    for p in prunable_media:
        t = inv.mms[p.mid].thread_id
        per_thread[t][0] += 1
        per_thread[t][1] += p.size or 0
    ranked = sorted(per_thread.items(), key=lambda kv: -kv[1][1])
    if ranked:
        add(f"  Biggest conversations by MMS attachments (top {min(top, len(ranked))} of {len(ranked):,})")
        for tid, (n, b) in ranked[:top]:
            t = inv.threads.get(tid)
            label = inv.thread_label(tid) + ("  (group)" if t and t.is_group else "")
            add(f"    {fmt_size(b):>9}  {n:>7,} attachments  {label[:70]}")
        add("")
    data["threads"] = [
        {"thread_id": tid, "label": inv.thread_label(tid), "count": n, "bytes": b} for tid, (n, b) in ranked
    ]

    # --- by year -------------------------------------------------------------------------------
    per_year: dict = defaultdict(lambda: [0, 0])
    for p in prunable_media:
        when = inv.mms[p.mid].when
        y = when.year if when else 0
        per_year[y][0] += 1
        per_year[y][1] += p.size or 0
    if per_year:
        add("  MMS attachments by year")
        for y in sorted(per_year):
            n, b = per_year[y]
            add(f"    {y or 'unknown':<8}{n:>9,}{fmt_size(b):>12}")
        add("")
    data["by_year"] = {str(y): {"count": v[0], "bytes": v[1]} for y, v in per_year.items()}

    # --- interpretation ------------------------------------------------------------------------
    add("WHAT THIS MEANS")
    if gm and gm["data"] > 2 * max(total_media, 1):
        add(f"  Most of the space ({fmt_size(gm['data'])}) is inside Google Messages itself: RCS chat")
        add("  photos and videos. msgprune can't reach those (no app can, without rooting the phone).")
        add("  See README.md, section \"RCS media\", for how to clear them.")
    elif gm:
        add("  A good share of the space is MMS attachments, which msgprune can remove.")
        add(f"  Google Messages' own storage ({fmt_size(gm['data'])}) is RCS media + its database.")
    add(f"  MMS attachments msgprune can remove: {fmt_size(total_media)}.")
    add("  Next: preview a prune (changes nothing):   ./msgprune prune --min-size 100KB")
    return "\n".join(lines) + "\n", data
