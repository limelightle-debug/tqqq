"""Update data/tqqq.json with TQQQ, QQQ and QLD prices from the Nasdaq.com data service.

Runs in GitHub Actions. Nasdaq.com serves the last ten years of daily prices, so the file
keeps everything it has ever received and adds the new days on each run. The older history
(TQQQ from its 2010 listing, QQQ and QLD yearly highs and lows) came with the first version
of the file; QQQ and QLD daily closes before October 2016 come from data/qqq_qld_base.json. Prices are split-adjusted; dividends are not included.
Writes the file only when the data changed, and leaves data/status.json with the outcome.
"""
import json
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "tqqq.json"
STATUS = DATA / "status.json"
BASE = DATA / "qqq_qld_base.json"  # QQQ and QLD daily closes before Nasdaq.com's ten years
SYMBOLS = ("TQQQ", "QQQ", "QLD")
NY = ZoneInfo("America/New_York")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
})


def sig(x, n=5):
    return float(f"{x:.{n}g}")


def num(s):
    return float(str(s).replace("$", "").replace(",", "").strip())


def get_json(url):
    last = None
    for attempt in range(4):
        if attempt:
            time.sleep(5 * attempt)
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:120]!r}"
        except (requests.RequestException, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        print(f"{url.split('?')[0]}: {last}")
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def nasdaq_daily(symbol):
    """Daily [yyyymmdd, open, high, low, close] for about the last ten years, oldest first."""
    today = datetime.now(NY).date()
    start = today - timedelta(days=3700)
    j = get_json(f"https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass=etf"
                 f"&fromdate={start}&todate={today}&limit=10000")
    rows = []
    for r in (j.get("data") or {}).get("tradesTable", {}).get("rows") or []:
        try:
            m, d, y = r["date"].split("/")
            rows.append([int(f"{y}{m}{d}"), num(r["open"]), num(r["high"]), num(r["low"]), num(r["close"])])
        except (KeyError, ValueError):
            continue
    rows.sort()
    if len(rows) < 500:
        raise RuntimeError(f"{symbol}: only {len(rows)} days from Nasdaq")
    return rows


def regular_session(stamp):
    """True for an official close ("Oct 16, 2026") or a trade stamped 9:30-16:00 ET.
    Pre-market and after-hours prices are left out, so the app shows the regular session only."""
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)", stamp or "", re.I)
    if not m:
        return bool(stamp)
    h, mi, ap = int(m.group(1)) % 12, int(m.group(2)), m.group(3).upper()
    t = (h + (12 if ap == "PM" else 0)) * 60 + mi
    return 9 * 60 + 30 <= t <= 16 * 60


def nasdaq_quote(symbol):
    """Latest regular-session price, or None outside it (the caller then uses the last daily close)."""
    j = get_json(f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass=etf")
    data = j.get("data") or {}
    p = None
    for key in ("primaryData", "secondaryData"):
        c = data.get(key) or {}
        if c.get("lastSalePrice") and regular_session(c.get("lastTradeTimestamp")):
            p = c
            break
    if p is None:
        return None
    price = num(p["lastSalePrice"])
    chg = num(p.get("netChange") or 0)
    q = {"price": sig(price, 6), "prev": sig(price - chg, 6), "time": p.get("lastTradeTimestamp")}
    # today's high and low while the market is open ("$73.13/$67.43"; "N/A" when closed)
    if re.search(r"\d:\d", q["time"] or ""):
        try:
            s = get_json(f"https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=etf")
            hl = (((s.get("data") or {}).get("summaryData") or {}).get("TodayHighLow") or {}).get("value", "")
            if "/" in hl:
                hi, lo = (num(x) for x in hl.split("/"))
                q.update(high=sig(hi, 6), low=sig(lo, 6))
        except (RuntimeError, ValueError):
            pass
    return q


def quote_day(stamp):
    """yyyymmdd of a Nasdaq timestamp such as "Oct 19, 2026 9:36 AM ET", or None."""
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", stamp or "")
    if not m:
        return None
    mon = "JanFebMarAprMayJunJulAugSepOctNovDec".find(m.group(1))
    return None if mon < 0 else int(m.group(3)) * 10000 + (mon // 3 + 1) * 100 + int(m.group(2))


def year_stats(rows):
    out = {}
    for d, o, h, l, c in rows:
        y = str(d // 10000)
        e = out.setdefault(y, {"lo": l, "hi": h, "end": c, "first": d, "last": d})
        e["lo"], e["hi"] = min(e["lo"], l), max(e["hi"], h)
        if d >= e["last"]:
            e["end"], e["last"] = c, d
        e["first"] = min(e["first"], d)
    return out


def merge_years(old, new):
    out = {y: dict(v) for y, v in old.items()}
    for y, n in new.items():
        o = out.get(y)
        if o is None:
            out[y] = dict(n)
            continue
        o["lo"], o["hi"] = min(o["lo"], n["lo"]), max(o["hi"], n["hi"])
        o["first"] = min(o["first"], n["first"])
        if n["last"] >= o["last"]:
            o["end"], o["last"] = n["end"], n["last"]
    return out


def scale_years(years, f):
    for v in years.values():
        for k in ("lo", "hi", "end"):
            v[k] *= f


def split_factor(old_by_date, new_rows):
    """If the source re-based its history for a split, old stored prices need the same factor."""
    ratios = [r[4] / old_by_date[r[0]] for r in new_rows[:-5] if old_by_date.get(r[0])]
    if len(ratios) < 5:
        return 1.0
    f = statistics.median(ratios)
    return f if abs(f - 1) > 0.02 else 1.0


def update():
    store = json.loads(OUT.read_text(encoding="utf-8"))
    daily = {r[0]: r[1:5] for r in store["daily"]}
    # QQQ and QLD closes kept on the same dates as TQQQ (columns 7 and 8 of each daily row)
    closes = {"QQQ": {r[0]: r[6] for r in store["daily"] if len(r) > 6 and r[6] is not None},
              "QLD": {r[0]: r[7] for r in store["daily"] if len(r) > 7 and r[7] is not None}}
    if BASE.exists():
        base = json.loads(BASE.read_text(encoding="utf-8"))
        for s in ("QQQ", "QLD"):
            for d, c in base.get(s, []):
                closes[s].setdefault(d, c)
    years = {s: {y: {k: v[k] for k in ("lo", "hi", "end", "first", "last")} for y, v in store["years"][s].items()}
             for s in SYMBOLS}

    quotes, last_hist = {}, {}
    for s in SYMBOLS:
        rows = nasdaq_daily(s)
        last_hist[s] = rows[-1][0]
        time.sleep(1)
        if s == "TQQQ":
            f = split_factor({d: v[3] for d, v in daily.items()}, rows)
            if f != 1.0:
                print(f"TQQQ: rescaling stored history by {f:.4f}")
                daily = {d: [x * f for x in v] for d, v in daily.items()}
            for d, o, h, l, c in rows:
                daily[d] = [o, h, l, c]
            years[s] = year_stats([[d, *v] for d, v in sorted(daily.items())])
        else:
            new = year_stats(rows)
            done = [y for y in new if y in years[s] and new[y]["first"] % 10000 <= 110
                    and years[s][y]["first"] % 10000 <= 110 and new[y]["last"] % 10000 >= 1224
                    and years[s][y]["last"] % 10000 >= 1224]
            if done:
                f = statistics.median(new[y]["end"] / years[s][y]["end"] for y in done)
                if abs(f - 1) > 0.02:
                    print(f"{s}: rescaling stored years by {f:.4f}")
                    scale_years(years[s], f)
            years[s] = merge_years(years[s], new)
            f = split_factor(closes[s], rows)
            if f != 1.0:
                print(f"{s}: rescaling stored closes by {f:.4f}")
                closes[s] = {d: c * f for d, c in closes[s].items()}
            for d, o, h, l, c in rows:
                closes[s][d] = c
        try:
            quotes[s] = nasdaq_quote(s)
        except RuntimeError as e:
            print(f"warning: quote {s}: {e}", file=sys.stderr)
            quotes[s] = None
        if quotes[s] is None:  # outside the regular session: the last daily close
            last = rows[-1]
            quotes[s] = {"price": last[4], "prev": rows[-2][4], "time": str(last[0])}
        time.sleep(1)

    # Today's bar from the regular-session quote, until Nasdaq.com publishes the official daily row
    # (which then replaces it). The open is the first price seen today, so it is approximate.
    q = quotes["TQQQ"]
    qd = quote_day(q.get("time"))
    if qd and qd > last_hist["TQQQ"]:
        before = daily.get(qd)
        o = before[0] if before else q["price"]
        hs = [x for x in (q.get("high"), q["price"], before and before[1]) if x]
        ls = [x for x in (q.get("low"), q["price"], before and before[2]) if x]
        daily[qd] = [o, max(hs), min(ls), q["price"]]
        for s in ("QQQ", "QLD"):
            if quote_day(quotes[s].get("time")) == qd and qd > last_hist[s]:
                closes[s][qd] = quotes[s]["price"]

    def cl(sym, d):
        v = closes[sym].get(d)
        return None if v is None else sig(v)
    out_rows = [[d, sig(o), sig(h), sig(l), sig(c), sig(c), cl("QQQ", d), cl("QLD", d)]
                for d, (o, h, l, c) in sorted(daily.items())]
    out_years = {s: {y: {"lo": sig(v["lo"]), "hi": sig(v["hi"]), "end": sig(v["end"]),
                         "first": v["first"], "last": v["last"], "full": v["first"] % 10000 <= 110}
                     for y, v in sorted(years[s].items())} for s in SYMBOLS}
    data = {
        "source": "Nasdaq.com（2016年10月より前は Yahoo Finance）",
        "quotes": quotes,
        "latest": {"price": quotes["TQQQ"]["price"]},
        "daily": out_rows,
        "years": out_years,
    }
    prev = dict(store)
    prev.pop("updatedAt", None)
    if json.dumps(prev, sort_keys=True) == json.dumps(data, sort_keys=True):
        print("no change")
        return
    data = {"updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **data}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"updated: {len(out_rows)} days, last {out_rows[-1][0]} close {out_rows[-1][4]}, "
          f"TQQQ {quotes['TQQQ']['price']}")


if __name__ == "__main__":
    try:
        update()
        result = {"ok": True}
    except Exception as e:  # leave the reason in the repo so it can be read without signing in
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500],
                  "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    old = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else None
    if old != result:
        STATUS.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
