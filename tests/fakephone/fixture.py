"""Builds a small fake SMS/MMS store for the tests (dates in local time, sizes in bytes)."""
import datetime as dt
import os
import sqlite3

SCHEMA = """
CREATE TABLE sms(_id INTEGER PRIMARY KEY, thread_id INTEGER, address TEXT, date INTEGER, type INTEGER, body TEXT);
CREATE TABLE pdu(_id INTEGER PRIMARY KEY, thread_id INTEGER, date INTEGER, msg_box INTEGER, m_type INTEGER, m_size INTEGER, read INTEGER);
CREATE TABLE part(_id INTEGER PRIMARY KEY AUTOINCREMENT, mid INTEGER, seq INTEGER, ct TEXT, name TEXT, chset INTEGER, cid TEXT, cl TEXT, _data TEXT, text TEXT);
CREATE TABLE threads(_id INTEGER PRIMARY KEY, recipient_ids TEXT, message_count INTEGER, date INTEGER);
CREATE TABLE canonical_addresses(_id INTEGER PRIMARY KEY, address TEXT);
CREATE TABLE phones(_id INTEGER PRIMARY KEY, data1 TEXT, display_name TEXT);
"""

KB, MB = 1000, 1000 * 1000

# (thread, date, msg_box, [(content type, size or text, name)])  msg_box: 1 inbox, 2 sent, 3 draft
MESSAGES = [
    (1, "2016-03-01", 1, [("application/smil", "<smil/>", None), ("image/jpeg", 250 * KB, "IMG_0001.jpg"),
                          ("text/plain", "Look at this, Mom!\nRow: 9 _id=fake", None)]),
    (1, "2017-06-10", 2, [("application/smil", "<smil/>", None), ("image/jpeg", 1200 * KB, "photo, beach.jpg")]),
    (1, "2018-01-01", 1, [("video/mp4", 3500 * KB, None), ("text/plain", "birthday video", None)]),
    (1, "2019-05-05", 2, [("application/smil", "<smil/>", None), ("text/plain", "text-only MMS, keep me", None)]),
    (1, "2020-02-02", 1, [("image/png", 40 * KB, "tiny.png"), ("text/plain", "small pic", None)]),
    (1, "2025-12-01", 1, [("image/jpeg", 600 * KB, "recent.jpg")]),
    (1, "2021-07-07", 1, [("image/jpeg", 2 * MB, "a.jpg"), ("image/jpeg", 150 * KB, "b.jpg"),
                          ("text/plain", "two pics", None)]),
    (1, "2015-01-01", 3, [("image/jpeg", 5 * MB, "draft.jpg")]),
    (2, "2016-08-08", 1, [("image/jpeg", 120 * KB, None), ("text/plain", "group pic 1", None)]),
    (2, "2017-08-08", 2, [("image/jpeg", 900 * KB, None)]),
    (2, "2018-08-08", 1, [("audio/amr", 300 * KB, "voice.amr")]),
    (2, "2019-08-08", 1, [("image/jpeg", 450 * KB, None), ("text/plain", "group pic 2", None)]),
    (2, "2022-08-08", 1, [("image/jpeg", 99 * KB, None)]),
    (2, "2023-08-08", 2, [("text/plain", "group text only", None)]),
    (3, "2018-02-02", 1, [("text/x-vcard", 3 * KB, "contact.vcf")]),
    (3, "2018-03-03", 1, [("image/gif", 500 * KB, "funny.gif")]),
    (3, "2018-04-04", 1, [("image/jpeg", "MISSING", "gone.jpg")]),  # file missing on the phone
    (4, "2024-01-01", 1, [("image/heic", 2500 * KB, "IMG_9.heic")]),
    (4, "2024-02-02", 2, [("video/3gpp", 800 * KB, "clip.3gp"), ("text/plain", "clip", None)]),
]


def ts(day):
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp())


def build(root):
    os.makedirs(os.path.join(root, "parts"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "local", "tmp"), exist_ok=True)
    con = sqlite3.connect(os.path.join(root, "telephony.db"))
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO canonical_addresses VALUES (?,?)", [
        (1, "+15551230001"), (2, "+15551230002"), (3, "+15551230003"), (4, "+15559870000"), (5, "5551230005"),
    ])
    con.executemany("INSERT INTO phones VALUES (?,?,?)", [
        (1, "(555) 123-0001", "Mom"), (2, "555-123-0002", "Dad"), (3, "+1 555 123 0003", "Sis"),
        (4, "+1 (555) 123-0005", "Alex, Jr."),
    ])
    con.executemany("INSERT INTO threads VALUES (?,?,?,?)", [
        (1, "1", 0, 0), (2, "1 2 3", 0, 0), (3, "4", 0, 0), (4, "5", 0, 0),
    ])
    for i in range(25):
        con.execute("INSERT INTO sms (thread_id, address, date, type, body) VALUES (?,?,?,?,?)",
                    (1 + i % 4, "+15551230001", ts("2020-01-01") * 1000, 1, f"hi, number {i}\nsecond line"))
    for mid, (thread, day, box, parts) in enumerate(MESSAGES, start=1):
        con.execute("INSERT INTO pdu VALUES (?,?,?,?,?,?,?)", (mid, thread, ts(day), box, 132, 0, 1))
        for seq, (ct, payload, name) in enumerate(parts):
            if ct in ("text/plain", "application/smil"):
                con.execute("INSERT INTO part (mid, seq, ct, name, chset, cid, cl, _data, text) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", (mid, seq, ct, name, 106, f"<t{seq}>", name, None, payload))
                continue
            path = os.path.join(root, "parts", f"PART_{mid}_{seq}")
            if payload != "MISSING":
                with open(path, "wb") as fh:
                    fh.write(os.urandom(payload))
            con.execute("INSERT INTO part (mid, seq, ct, name, chset, cid, cl, _data, text) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (mid, seq, ct, name, None, f"<m{seq}>", name, path, None))
    con.commit()
    con.close()
