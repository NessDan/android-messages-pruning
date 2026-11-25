"""Fake versions of the Android shell tools msgprune uses, backed by a SQLite file.

Behaves like the real ones where it matters for safety:
* `content insert/delete` on the SMS/MMS store are *silently ignored* (exit 0, no output)
  unless the shell's WRITE_SMS app-op is "allow" -- exactly what Android does.
* `content query` prints "Row: N col=value, ..." with NULL for nulls, or "No result found."
* `content read` streams the attachment file; text parts have no file and fail.

Knobs (environment variables):
  FAKEPHONE_IGNORE_APPOPS=1   appops set "works" but writes stay ignored (a phone that won't cooperate)
  FAKEPHONE_NEEDS_UID_MODE=1  only the per-uid app-op mode grants writes
"""
import json
import os
import sqlite3
import sys
import time

ROOT = os.environ["FAKEPHONE_ROOT"]
DB = os.path.join(ROOT, "telephony.db")
STATE = os.path.join(ROOT, "state.json")


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"appops": {}}


def save_state(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh)


def write_allowed():
    if os.environ.get("FAKEPHONE_IGNORE_APPOPS") == "1":
        return False
    ops = load_state()["appops"]
    if os.environ.get("FAKEPHONE_NEEDS_UID_MODE") == "1":
        return ops.get("uid") == "allow"
    mode = ops.get("uid") or ops.get("package") or "ignore"
    return mode == "allow"


def provider_error(msg):
    sys.stderr.write(f"Error while accessing provider:mms\n{msg}\n")
    return 1


# ----- content ----------------------------------------------------------------------------------

def parse_opts(args):
    opts = {"bind": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--bind":
            opts["bind"].append(args[i + 1])
            i += 2
        elif a.startswith("--"):
            opts[a[2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    return opts


def route(uri):
    rest = uri.split("://", 1)[1]
    path, _, query = rest.partition("?")
    segs = [s for s in path.split("/") if s]
    auth = segs[0]
    if auth == "sms" and len(segs) == 1:
        return "sms", None
    if auth == "mms":
        if len(segs) == 1:
            return "pdu", None
        if segs[1] == "part":
            return ("part", None) if len(segs) == 2 else ("part", f"_id={int(segs[2])}")
        if segs[1].isdigit():
            if len(segs) == 2:
                return "pdu", f"_id={int(segs[1])}"
            if segs[2] == "part":
                return "part", f"mid={int(segs[1])}"
    if auth == "mms-sms":
        if segs[1] == "conversations" and "simple=true" in query:
            return "threads", None
        if segs[1] == "canonical-addresses":
            return "canonical_addresses", None
    if auth == "com.android.contacts" and segs[1:] == ["data", "phones"]:
        return "phones", None
    raise ValueError(f"Unknown URI {uri}")


def fmt_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return "BLOB"
    return str(v)


def content(args):
    sub, opts = args[0], parse_opts(args[1:])
    try:
        table, implicit = route(opts["uri"])
    except (ValueError, KeyError, IndexError) as e:
        return provider_error(f"java.lang.IllegalArgumentException: {e}")
    con = sqlite3.connect(DB)
    where = " AND ".join(f"({w})" for w in (implicit, opts.get("where")) if w)
    if sub == "query":
        proj = opts.get("projection", "*").replace(":", ", ")
        sql = f"SELECT {proj} FROM {table}" + (f" WHERE {where}" if where else "")
        if opts.get("sort"):
            sql += f" ORDER BY {opts['sort']}"
        try:
            cur = con.execute(sql)
        except sqlite3.Error as e:
            return provider_error(f"android.database.sqlite.SQLiteException: {e}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            print("No result found.")
        for i, r in enumerate(rows):
            print(f"Row: {i} " + ", ".join(f"{c}={fmt_value(v)}" for c, v in zip(cols, r)))
        return 0
    if sub == "read":
        row = con.execute(f"SELECT _data FROM {table} WHERE {where}").fetchone()
        if row and row[0] and row[0].startswith("virtual:"):
            left, chunk = int(row[0].split(":")[1]), b"\0" * (1 << 20)
            while left > 0:
                sys.stdout.buffer.write(chunk[:min(left, len(chunk))])
                left -= len(chunk)
            return 0
        if not row or not row[0] or not os.path.exists(row[0]):
            sys.stderr.write(f"java.io.FileNotFoundException: No entry for {opts['uri']}\n")
            return 1
        with open(row[0], "rb") as fh:
            sys.stdout.buffer.write(fh.read())
        return 0
    if sub == "insert":
        if not write_allowed():
            return 0  # Android hands back a fake URI and does nothing
        if table != "part" or not implicit or not implicit.startswith("mid="):
            return provider_error("only content://mms/<id>/part inserts are faked")
        values = {"mid": int(implicit.split("=")[1])}
        for b in opts["bind"]:
            col, typ, val = b.split(":", 2)
            values[col] = int(val) if typ in ("i", "l") else (None if typ == "n" else val)
        if values.get("ct") not in ("text/plain", "application/smil"):
            path = os.path.join(ROOT, "parts", f"PART_{time.time_ns()}")
            open(path, "wb").close()
            values["_data"] = path
        cols = ", ".join(values)
        con.execute(f"INSERT INTO part ({cols}) VALUES ({', '.join('?' * len(values))})",
                    list(values.values()))
        con.commit()
        return 0
    if sub == "delete":
        if not write_allowed():
            return 0  # silently ignored
        if table != "part":
            return provider_error("only part deletes are faked")
        sel = f" WHERE {where}" if where else ""
        for (data,) in con.execute(f"SELECT _data FROM part{sel}").fetchall():
            if data and os.path.exists(data):
                os.unlink(data)
        con.execute(f"DELETE FROM part{sel}")
        con.commit()
        return 0
    return provider_error(f"unsupported content subcommand {sub}")


# ----- appops ---------------------------------------------------------------------------------

def appops(args):
    state = load_state()
    ops = state["appops"]
    if args[0] == "get":
        lines = []
        if ops.get("uid"):
            lines.append(f"Uid mode: WRITE_SMS: {ops['uid']}")
        if ops.get("package"):
            lines.append(f"WRITE_SMS: {ops['package']}; time=+3m1s123ms ago")
        print("\n".join(lines) if lines else "No operations.\nDefault mode: ignore")
        return 0
    if args[0] == "set":
        rest = args[1:]
        key = "package"
        if rest[0] == "--uid":
            key, rest = "uid", rest[1:]
        pkg, op, mode = rest
        assert pkg == "com.android.shell" and op == "WRITE_SMS", rest
        if mode == "ignore":  # the op's default: Android drops the entry
            ops.pop(key, None)
        else:
            ops[key] = mode
        save_state(state)
        return 0
    return 1


# ----- odds and ends --------------------------------------------------------------------------

PROPS = {
    "ro.product.model": os.environ.get("FAKEPHONE_MODEL", "Pixel_11_Pro").replace("_", " "),
    "ro.build.version.release": os.environ.get("FAKEPHONE_ANDROID", "17"),
    "ro.build.version.sdk": os.environ.get("FAKEPHONE_SDK", "37"),
}


def dumpsys(args):
    if args[:1] == ["diskstats"]:
        stats = {"messages": [104857600, 64424509440, 402653184], "telephony": [1048576, 17179869184, 0],
                 "free_k": 314572800, "total_k": 499122176}
        try:
            with open(os.path.join(ROOT, "diskstats.json")) as fh:
                stats.update(json.load(fh))
        except OSError:
            pass
        names = ["com.google.android.apps.messaging", "com.android.providers.telephony", "com.example"]
        cols = list(zip(stats["messages"], stats["telephony"], [1000, 2000, 0]))
        print("Latency: 2ms [512B Data Write]")
        print(f"Data-Free: {stats['free_k']}K / {stats['total_k']}K total = "
              f"{100 * stats['free_k'] // stats['total_k']}% free")
        print("App Size: 1000")
        print(f"Package Names: {json.dumps(names)}")
        print(f"App Sizes: {json.dumps(list(cols[0]))}")
        print(f"App Data Sizes: {json.dumps(list(cols[1]))}")
        print(f"Cache Sizes: {json.dumps(list(cols[2]))}")
        return 0
    if args[:1] == ["package"]:
        print("Packages:\n  Package [com.google.android.apps.messaging] (abc):\n"
              "    versionCode=1 minSdk=31 targetSdk=36\n"
              "    versionName=messages.android_20261104_00_RC00.phone_dynamic")
        return 0
    return 0


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    if tool == "content":
        return content(args)
    if tool == "appops":
        return appops(args)
    if tool == "getprop":
        print(PROPS.get(args[0], ""))
        return 0
    if tool == "dumpsys":
        return dumpsys(args)
    if tool == "cmd":
        if args[:2] == ["role", "get-role-holders"]:
            print("com.google.android.apps.messaging")
        return 0
    if tool == "settings":
        print("com.google.android.apps.messaging")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
