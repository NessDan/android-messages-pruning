"""A bigger, realistic-looking fake phone for demos and README screenshots.

Everything is invented: names are made up, numbers are in the 555-01xx range reserved for
fiction, and attachments are "virtual" (the fake phone streams zeros of the right size, so no
gigabytes land on your disk).

    python3 tests/fakephone/demo.py /tmp/demo-phone
    FAKEPHONE_ROOT=/tmp/demo-phone MSGPRUNE_ADB=tests/fakephone/adb \\
        FAKEPHONE_MODEL=Pixel_9_Pro FAKEPHONE_SERIAL=DEMO9PRO ./msgprune scan
"""
import datetime as dt
import json
import math
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fixture import SCHEMA  # noqa: E402

KB, MB, GB = 1000, 1000**2, 1000**3

CONTACTS = {
    "Mom": "0101", "Dad": "0102", "Jordan Lee": "0103", "Priya Shah": "0104", "Alex Kim": "0105",
    "Sam Rivera": "0106", "Taylor Brooks": "0107", "Chris Park": "0108", "Morgan Diaz": "0109",
    "Riley Chen": "0110", "Casey Nguyen": "0111", "Avery Johnson": "0112", "Drew Patel": "0113",
    "Quinn Murphy": "0114", "Harper Wells": "0115", "Rowan Ellis": "0116", "Skyler Gray": "0117",
    "Logan Reyes": "0118", "Emerson Hall": "0119",
}

# (participants, how many attachments); a participant is a contact name or a bare 4-digit suffix
THREADS = [
    (["Mom", "Dad", "Jordan Lee"], 2300),
    (["Mom"], 1150),
    (["Sam Rivera", "Taylor Brooks", "Chris Park", "Priya Shah", "Morgan Diaz", "Riley Chen"], 1000),
    (["Priya Shah"], 850),
    (["Alex Kim"], 610),
    (["Dad"], 430),
    (["Morgan Diaz"], 350),
    (["Avery Johnson", "Drew Patel", "Quinn Murphy"], 320),
    (["Riley Chen"], 220),
    (["Jordan Lee"], 190),
    (["Casey Nguyen"], 140),
    (["0147"], 95),
    (["Harper Wells"], 110),
    (["Rowan Ellis"], 80),
    (["Skyler Gray"], 60),
    (["Logan Reyes"], 45),
    (["Emerson Hall"], 40),
]
THREADS += [([f"01{n}"], (n * 7) % 30 + 1) for n in range(20, 46)]  # odds and ends

YEARS = {2013: 2, 2014: 4, 2015: 7, 2016: 10, 2017: 13, 2018: 15, 2019: 14, 2020: 13, 2021: 10,
         2022: 7, 2023: 4, 2024: 1}

# (content type, weight, median size, spread, min, max)
KINDS = [
    ("image/jpeg", 76, 520 * KB, 0.55, 35 * KB, 1600 * KB),
    ("image/png", 4, 300 * KB, 0.7, 40 * KB, 1200 * KB),
    ("image/gif", 3, 450 * KB, 0.6, 90 * KB, 1100 * KB),
    ("image/heic", 3, 1300 * KB, 0.3, 600 * KB, 2600 * KB),
    ("video/mp4", 6, 1500 * KB, 0.4, 400 * KB, 3500 * KB),
    ("video/3gpp", 3, 900 * KB, 0.4, 300 * KB, 1800 * KB),
    ("audio/amr", 3, 90 * KB, 0.6, 15 * KB, 400 * KB),
    ("text/x-vcard", 2, 3 * KB, 0.4, 1 * KB, 8 * KB),
]
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/heic": "heic",
       "video/mp4": "mp4", "video/3gpp": "3gp", "audio/amr": "amr", "text/x-vcard": "vcf"}


def number(suffix):
    return f"+1415555{suffix}"


def random_date(rng):
    year = rng.choices(list(YEARS), weights=list(YEARS.values()))[0]
    start = dt.datetime(year, 1, 1).timestamp()
    end = min(dt.datetime(year + 1, 1, 1).timestamp(), dt.datetime(2024, 10, 1).timestamp())
    return int(rng.uniform(start, end))


def build(root, seed=7):
    rng = random.Random(seed)
    os.makedirs(os.path.join(root, "data", "local", "tmp"), exist_ok=True)
    path = os.path.join(root, "telephony.db")
    if os.path.exists(path):
        os.unlink(path)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    canon, next_canon = {}, [1]

    def canon_id(addr):
        if addr not in canon:
            canon[addr] = next_canon[0]
            next_canon[0] += 1
        return canon[addr]

    con.executemany("INSERT INTO phones (data1, display_name) VALUES (?,?)",
                    [(f"(415) 555-{s}", name) for name, s in CONTACTS.items()])
    mid, total, kinds_w = 0, 0, [k[1] for k in KINDS]
    pdus, parts = [], []
    for tid, (people, n_attach) in enumerate(THREADS, start=1):
        addrs = [number(CONTACTS.get(p, p)) for p in people]
        con.execute("INSERT INTO threads VALUES (?,?,?,?)",
                    (tid, " ".join(str(canon_id(a)) for a in addrs), 0, 0))
        left = n_attach
        while left > 0:
            mid += 1
            date = random_date(rng)
            box = 1 if rng.random() < 0.62 else 2
            pdus.append((mid, tid, date, box, 132, 0, 1))
            parts.append((mid, -1, "application/smil", None, 106, "<smil>", "smil.xml", None, "<smil/>"))
            n = 1 if rng.random() < 0.85 else min(left, rng.randint(2, 4))
            for seq in range(n):
                ct, _, median, spread, lo, hi = rng.choices(KINDS, weights=kinds_w)[0]
                size = int(min(hi, max(lo, rng.lognormvariate(math.log(median), spread))))
                total += size
                data = f"virtual:{size}" if rng.random() > 0.0004 else "/nonexistent/PART_gone"
                parts.append((mid, seq, ct, f"IMG_{mid}{seq}.{EXT[ct]}", None, f"<p{seq}>",
                              f"part{seq}.{EXT[ct]}", data, None))
            if rng.random() < 0.3:
                parts.append((mid, n, "text/plain", None, 106, "<text>", "text.txt", None, "(caption)"))
            left -= n
        for _ in range(int(n_attach * 1.4)):  # group/long texts that went out as MMS, no media
            mid += 1
            pdus.append((mid, tid, random_date(rng), 1 if rng.random() < 0.6 else 2, 132, 0, 1))
            parts.append((mid, -1, "application/smil", None, 106, "<smil>", "smil.xml", None, "<smil/>"))
            parts.append((mid, 0, "text/plain", None, 106, "<text>", "text.txt", None, "(text)"))
    for _ in range(3):  # drafts are never touched
        mid += 1
        pdus.append((mid, 2, random_date(rng), 3, 128, 0, 1))
        parts.append((mid, 0, "image/jpeg", "draft.jpg", None, "<d>", "draft.jpg", "virtual:800000", None))
    con.executemany("INSERT INTO canonical_addresses VALUES (?,?)", [(i, a) for a, i in canon.items()])
    con.executemany("INSERT INTO pdu VALUES (?,?,?,?,?,?,?)", pdus)
    con.executemany("INSERT INTO part (mid, seq, ct, name, chset, cid, cl, _data, text) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", parts)
    con.executemany("INSERT INTO sms (thread_id, address, date, type, body) VALUES (?,?,?,?,?)",
                    [(1 + i % len(THREADS), "", 0, 1, "") for i in range(61482)])
    con.commit()
    con.close()
    with open(os.path.join(root, "diskstats.json"), "w") as fh:
        json.dump({"messages": [118 * MB, int(31.6 * GB), 412 * MB],
                   "telephony": [2 * MB, total + int(0.9 * GB), 0],
                   "free_k": 288 * GB // 1024, "total_k": 512 * GB // 1024}, fh)
    return {"messages": mid, "attachment_bytes": total}


if __name__ == "__main__":
    print(build(sys.argv[1]))
