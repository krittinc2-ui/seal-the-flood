#!/usr/bin/env python3
"""Seal the Flood — build data.json (run hourly by GitHub Actions, or on KC's Mac).

Fetches ThaiWater (HII) + Open-Meteo, converts to the compact snapshot the page reads,
keeps up to 90 days of daily history, and merges curated notices from notices.json.
Exit code 0 when at least one source succeeded (data.json written), 1 otherwise.
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
NOTICES = os.path.join(ROOT, "notices.json")
TW = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public/"
IDS = [5, 749, 747, 24, 2599, 26, 4, 748, 2676, 39, 2744, 2795]
OM = ("https://api.open-meteo.com/v1/forecast?latitude=13.8119&longitude=100.3412"
      "&hourly=precipitation,precipitation_probability&daily=precipitation_sum,precipitation_probability_max"
      "&past_days=3&forecast_days=7&timezone=Asia%2FBangkok")
MR = ("https://marine-api.open-meteo.com/v1/marine?latitude=13.40&longitude=100.55"
      "&hourly=sea_level_height_msl&past_days=1&forecast_days=7&timezone=Asia%2FBangkok")
BKK = timezone(timedelta(hours=7))
UA = "SealTheFlood/1.0 (personal flood-watch dashboard; hourly)"


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def num(x):
    return None if x in (None, "") else float(x)


def lms(s):
    s = str(s).replace(" ", "T")
    if len(s) == 16:
        s += ":00"
    return int(datetime.fromisoformat(s).replace(tzinfo=BKK).timestamp() * 1000)


def bd(off=0):
    return (datetime.now(BKK) + timedelta(days=off)).strftime("%Y-%m-%d")


def hourly(g, step, key):
    pts = (g.get("data") or {}).get("graph_data") or []
    m = {}
    for p in pts:
        v = p.get(key)
        if v is None:
            continue
        k = (lms(p["datetime"]) // (step * 60000)) * step * 60000
        m[k] = float(v)
    if not m:
        return None
    ks = sorted(m)
    vals, k = [], ks[0]
    while k <= ks[-1]:
        x = m.get(k)
        vals.append(None if x is None else round(x, 2))
        k += step * 60000
    return {"t0": ks[0], "step": step, "v": vals, "bank": g["data"].get("min_bank")}


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        prev = json.load(open(OUT, encoding="utf8"))
    except Exception:
        prev = {}
    psnap = prev.get("snap") or {}
    snap = {"v": 1, "src": dict(psnap.get("src") or {})}
    errs, ok = [], False

    try:
        L = get_json(TW + "waterlevel_load?province_code=10,12,73,14,18,60")
        rows = []
        for r in L["waterlevel_data"]["data"]:
            s = r.get("station") or {}
            if int(s.get("id", -1)) not in IDS:
                continue
            ag = (r.get("agency") or {}).get("agency_shortname") or {}
            rows.append({"id": int(s["id"]), "code": s.get("tele_station_oldcode") or "",
                         "name": (s.get("tele_station_name") or {}).get("th", ""), "river": r.get("river_name") or "",
                         "bank": num(s.get("min_bank")), "wl": num(r.get("waterlevel_msl")),
                         "prev": num(r.get("waterlevel_msl_previous")), "pct": num(r.get("storage_percent")),
                         "q": num(r.get("discharge")), "sit": num(r.get("situation_level")),
                         "t": r.get("waterlevel_datetime") or "", "agency": ag.get("en", "")})
        if not rows:
            raise RuntimeError("no stations")
        snap["stations"] = rows
        snap["mahasawat"] = hourly(get_json(f"{TW}waterlevel_graph?station_type=tele_waterlevel&station_id=5&start_date={bd(-3)}&end_date={bd()}"), 60, "value")
        snap["c13q"] = hourly(get_json(f"{TW}waterlevel_graph?station_type=tele_waterlevel&station_id=2744&start_date={bd(-10)}&end_date={bd()}"), 360, "discharge")
        snap["src"]["thaiwater"] = {"at": now}
        snap["fetchedAt"] = now
        ok = True
    except Exception as e:
        errs.append(f"thaiwater: {e}")
        for k in ("stations", "mahasawat", "c13q", "fetchedAt"):
            if k in psnap:
                snap[k] = psnap[k]

    try:
        m = get_json(OM)
        snap["rain"] = {"daily": {"t": m["daily"]["time"], "p": m["daily"]["precipitation_sum"], "pp": m["daily"]["precipitation_probability_max"]},
                        "hourly": {"t0": m["hourly"]["time"][0], "step": 60, "p": m["hourly"]["precipitation"], "pp": m["hourly"]["precipitation_probability"]}}
        snap["src"]["meteo"] = {"at": now}
        ok = True
    except Exception as e:
        errs.append(f"meteo: {e}")
        if "rain" in psnap:
            snap["rain"] = psnap["rain"]
    try:
        m = get_json(MR)
        snap["tide"] = {"t0": m["hourly"]["time"][0], "step": 60, "v": [None if x is None else round(x, 2) for x in m["hourly"]["sea_level_height_msl"]]}
        snap["src"]["marine"] = {"at": now}
        ok = True
    except Exception as e:
        errs.append(f"marine: {e}")
        if "tide" in psnap:
            snap["tide"] = psnap["tide"]

    # daily history (max of Mahasawat for today, latest C.13 and Tha Chin %)
    hist = {h["date"]: h for h in (prev.get("history") or []) if h.get("date")}
    if "thaiwater" in snap["src"] and snap["src"]["thaiwater"].get("at") == now:
        today = bd()
        mh = snap.get("mahasawat")
        mx = None
        if mh:
            for i, v in enumerate(mh["v"]):
                t = datetime.fromtimestamp((mh["t0"] + i * 3600000) / 1000, BKK).strftime("%Y-%m-%d")
                if v is not None and t == today and (mx is None or v > mx):
                    mx = v
        st = {s["id"]: s for s in snap["stations"]}
        tc = [x for x in (st.get(748, {}).get("pct"), st.get(2676, {}).get("pct")) if x is not None]
        old = hist.get(today, {})
        cand = [x for x in (old.get("mhMax"), mx) if x is not None]
        hist[today] = {"date": today, "mhMax": max(cand) if cand else None, "c13q": st.get(2744, {}).get("q"),
                       "tcPct": max(tc) if tc else None, "at": now}
    history = [hist[k] for k in sorted(hist)][-90:]

    try:
        notices = json.load(open(NOTICES, encoding="utf8"))
    except Exception:
        notices = prev.get("notices") or {"items": []}

    if not ok:
        print("all sources failed:", errs, file=sys.stderr)
        return 1
    out = {"generatedAt": now, "errors": errs, "snap": snap, "notices": notices, "history": history}
    with open(OUT, "w", encoding="utf8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    s5 = next((s for s in snap.get("stations", []) if s["id"] == 5), {})
    print(f"ok {now} mahasawat={s5.get('wl')} errors={errs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
