#!/usr/bin/env python3
"""Seal the Flood — merge the public data.json (GitHub Actions) into the Claude artifact's db.

Run by the hourly Claude scheduled task (cloud, no Mac needed):
  python3 artifact_sync.py WORKDIR
Expects (written beforehand by ArtifactData get ... out_dir=WORKDIR/db):
  WORKDIR/db/snap/latest.json            current snap doc (optional)
  WORKDIR/db/history/<YYYY-MM-DD>.json   today's / yesterday's history docs (optional)
Writes WORKDIR/out/*.json and prints one JSON line:
  {"status": "OK"|"STALE"|"FAIL", "problems": [...], "writes": [{op, collection, doc_id, file_path}], "summary": {...}}
Merge rule: each dataset (stations / mahasawat / c13q / rain / tide) is replaced only when the
incoming copy has NEWER content (measurement time, last graph point, or fetch time) — never older.
"""
import json, os, sys, subprocess, datetime as dt

URL = "https://raw.githubusercontent.com/krittinc2-ui/seal-the-flood/main/data.json"
BKK = dt.timezone(dt.timedelta(hours=7))
KEYS = ["stations", "mahasawat", "c13q", "rain", "tide"]
STALE_H = 3


def local_ms(s):
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(" ", "T")
    if len(s) == 16:
        s += ":00"
    return dt.datetime.fromisoformat(s).replace(tzinfo=BKK).timestamp() * 1000


def iso_ms(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def last_t(o):
    if not o or o.get("t0") is None or not o.get("v"):
        return None
    t0, step = local_ms(o["t0"]), (o.get("step") or 60) * 6e4
    for i in range(len(o["v"]) - 1, -1, -1):
        if o["v"][i] is not None:
            return t0 + i * step
    return None


def st_t(arr):
    ts = [local_ms(s["t"]) for s in (arr or []) if s.get("t")]
    return max(ts) if ts else None


def src_at(snap, g):
    v = (snap.get("src") or {}).get(g)
    return iso_ms(v.get("at") if isinstance(v, dict) else v) if v else None


def content_t(snap, k):
    if not snap or not snap.get(k):
        return None
    if k == "stations":
        return st_t(snap[k])
    if k in ("mahasawat", "c13q"):
        return last_t(snap[k])
    return src_at(snap, "meteo" if k == "rain" else "marine")


def merge(cur, new):
    if not cur:
        return json.loads(json.dumps(new)), list(KEYS)
    out = json.loads(json.dumps(cur))
    out.setdefault("src", {})
    taken = []
    for k in KEYS:
        tc, tn = content_t(cur, k), content_t(new, k)
        if new.get(k) and (tc is None or (tn is not None and tn > tc)):
            out[k] = new[k]
            taken.append(k)
    for g in ("thaiwater", "meteo", "marine"):
        if (src_at(new, g) or 0) > (src_at(cur, g) or 0):
            out["src"][g] = new["src"][g]
    if taken:
        out["fetchedAt"] = max([cur.get("fetchedAt") or "", new.get("fetchedAt") or ""])
    out["v"] = new.get("v", out.get("v", 1))
    return out, taken


def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def main(work):
    os.makedirs(os.path.join(work, "out"), exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    problems, writes = [], []
    raw = os.path.join(work, "out", "data.json")
    r = subprocess.run(["curl", "-sS", "-m", "30", "-o", raw, "-w", "%{http_code}", URL], capture_output=True, text=True)
    d = load(raw) if r.stdout.strip() == "200" else None
    if not d or not d.get("snap"):
        print(json.dumps({"status": "FAIL", "problems": ["โหลด data.json ไม่ได้ (HTTP %s %s)" % (r.stdout.strip(), r.stderr.strip()[:120])], "writes": []}, ensure_ascii=False))
        return
    gen = iso_ms(d.get("generatedAt"))
    if gen is None or now - gen > 2 * 3600e3:
        problems.append("data.json เก่า: generatedAt %s (GitHub Actions อาจหยุดทำงาน)" % d.get("generatedAt"))
    if d.get("errors"):
        problems.append("GitHub Actions ดึงบางแหล่งไม่สำเร็จ: %s" % "; ".join(map(str, d["errors"]))[:300])

    cur = load(os.path.join(work, "db", "snap", "latest.json"))
    merged, taken = merge(cur, d["snap"])
    if taken:
        p = os.path.join(work, "out", "snap.json")
        json.dump(merged, open(p, "w"), ensure_ascii=False)
        writes.append({"op": "set" if cur else "set_new", "collection": "snap", "doc_id": "latest", "file_path": p})

    today = dt.datetime.now(BKK).date()
    for day in (today - dt.timedelta(days=1), today):
        key = day.isoformat()
        h = next((x for x in d.get("history", []) if x.get("date") == key), None)
        if not h:
            continue
        old = load(os.path.join(work, "db", "history", key + ".json"))
        body = dict(old or {})
        body["date"] = key
        mx = [x for x in ((old or {}).get("mhMax"), h.get("mhMax")) if x is not None]
        body["mhMax"] = max(mx) if mx else None
        if not old or (iso_ms(h.get("at")) or 0) >= (iso_ms(old.get("at")) or 0):
            body["c13q"], body["tcPct"], body["at"] = h.get("c13q"), h.get("tcPct"), h.get("at")
        if body != (old or {}):
            p = os.path.join(work, "out", "history-%s.json" % key)
            json.dump(body, open(p, "w"), ensure_ascii=False)
            writes.append({"op": "set" if old else "set_new", "collection": "history", "doc_id": key, "file_path": p})

    tm, ts = last_t(merged.get("mahasawat")), st_t(merged.get("stations"))
    for name, t in (("กราฟคลองมหาสวัสดิ์", tm), ("ระดับน้ำสถานี", ts)):
        if t is None or now - t > STALE_H * 3600e3:
            problems.append("%s เก่ากว่า %d ชม. (จุดล่าสุด %s)" % (name, STALE_H, dt.datetime.fromtimestamp(t / 1000, BKK).strftime("%d/%m %H:%M") if t else "-"))
    s5 = next((s for s in merged.get("stations", []) if s.get("id") == 5), {})
    c13 = next((s for s in merged.get("stations", []) if s.get("id") == 2744), {})
    tc = [s.get("pct") for s in merged.get("stations", []) if s.get("id") in (748, 2676) and s.get("pct") is not None]
    print(json.dumps({
        "status": "OK" if not problems else "STALE",
        "problems": problems,
        "writes": writes,
        "updated_keys": taken,
        "summary": {"dataGeneratedAt": d.get("generatedAt"), "mahasawat_wl": s5.get("wl"), "mahasawat_t": s5.get("t"),
                    "c13_q": c13.get("q"), "thachin_pct": max(tc) if tc else None},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
