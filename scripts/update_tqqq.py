"""Fetch TQQQ, QQQ and QLD prices from Yahoo Finance and write data/tqqq.json.

Runs in GitHub Actions. Writes the file only when the data changed.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TQQQ_OUT = Path(__file__).resolve().parent.parent / "data" / "tqqq.json"
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


def sig(x, n=5):
    """Round to n significant digits (keeps the JSON small)."""
    if x is None:
        return None
    return float(f"{x:.{n}g}")


CRUMB = None


def yahoo_crumb():
    """Yahoo answers cloud servers more reliably with its consent cookie and a crumb token."""
    global CRUMB
    if CRUMB is not None:
        return CRUMB
    CRUMB = ""
    try:
        session.get("https://fc.yahoo.com", timeout=15, allow_redirects=True)
    except requests.RequestException:
        pass
    try:
        r = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15)
        if r.status_code == 200 and r.text and "<" not in r.text and len(r.text) < 40:
            CRUMB = r.text.strip()
    except requests.RequestException:
        pass
    print(f"yahoo crumb: {'yes' if CRUMB else 'no'}")
    return CRUMB


def yahoo_daily(symbol):
    """Full daily history from Yahoo Finance: split-adjusted OHLC plus dividend-adjusted close."""
    now = int(time.time())
    last = None
    crumb = yahoo_crumb()
    for attempt, host in enumerate(("query1", "query2", "query1", "query2")):
        if attempt:
            time.sleep(5 * attempt)
        try:
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
                   f"?period1=0&period2={now}&interval=1d&events=div%2Csplit")
            if crumb:
                url += f"&crumb={crumb}"
            r = session.get(url, timeout=30)
            if r.status_code != 200:
                last = f"HTTP {r.status_code} from {host}: {r.text[:120]!r}"
                print(f"{symbol}: {last}")
                continue
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = res["indicators"]["adjclose"][0]["adjclose"]
            rows = []
            for i, ts in enumerate(res["timestamp"]):
                o, h, l, c, a = q["open"][i], q["high"][i], q["low"][i], q["close"][i], adj[i]
                if None in (o, h, l, c, a):
                    continue
                d = datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime("%Y%m%d")
                rows.append([int(d), o, h, l, c, a])
            if len(rows) < 1000:
                raise RuntimeError(f"{symbol}: only {len(rows)} days")
            return rows, res.get("meta", {})
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
            print(f"{symbol}: {last}")
    raise RuntimeError(f"yahoo {symbol}: {last}")


def yearly_table(rows):
    """Per calendar year: dividend-adjusted low, high and year-end close; 'full' = data from the first January days."""
    out = {}
    for d, o, h, l, c, a in rows:
        y = d // 10000
        f = a / c
        e = out.setdefault(y, {"lo": l * f, "hi": h * f, "end": a, "first": d})
        e["lo"] = min(e["lo"], l * f)
        e["hi"] = max(e["hi"], h * f)
        e["end"] = a
    return {str(y): {"lo": sig(v["lo"]), "hi": sig(v["hi"]), "end": sig(v["end"]),
                     "full": v["first"] % 10000 <= 110} for y, v in sorted(out.items())}


def quote(rows, meta):
    """Latest price and the previous day's close (for the day change)."""
    return {"price": sig(meta.get("regularMarketPrice") or rows[-1][4], 6),
            "prev": sig(rows[-2][4], 6), "date": rows[-1][0]}


def update_tqqq():
    tq, meta = yahoo_daily("TQQQ")
    years = {"TQQQ": yearly_table(tq)}
    quotes = {"TQQQ": quote(tq, meta)}
    for sym in ("QQQ", "QLD"):
        time.sleep(1)
        rows, m = yahoo_daily(sym)
        years[sym] = yearly_table(rows)
        quotes[sym] = quote(rows, m)
    data = {
        "source": "Yahoo Finance",
        "latest": {
            "price": meta.get("regularMarketPrice"),
            "time": meta.get("regularMarketTime"),
            "high": meta.get("regularMarketDayHigh"),
            "low": meta.get("regularMarketDayLow"),
        },
        # [yyyymmdd, open, high, low, close, adjusted close]; prices are split-adjusted
        "quotes": quotes,
        "daily": [[d, sig(o), sig(h), sig(l), sig(c), sig(a)] for d, o, h, l, c, a in tq],
        "years": years,
    }
    old = TQQQ_OUT.read_text(encoding="utf-8") if TQQQ_OUT.exists() else ""
    body = json.dumps(data, separators=(",", ":"))
    if old:
        try:
            prev = json.loads(old)
            prev.pop("updatedAt", None)
            if json.dumps(prev, separators=(",", ":")) == body:
                print("tqqq: no change")
                return
        except ValueError:
            pass
    data = {"updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **data}
    TQQQ_OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"tqqq: {len(tq)} days, last {tq[-1][0]} close {tq[-1][4]:.2f}")


if __name__ == "__main__":
    TQQQ_OUT.parent.mkdir(exist_ok=True)
    status = TQQQ_OUT.parent / "status.json"
    try:
        update_tqqq()
        result = {"ok": True}
    except Exception as e:  # leave the reason in the repo so it can be read without signing in
        print(f"error: {e}", file=sys.stderr)
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500],
                  "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    old = json.loads(status.read_text(encoding="utf-8")) if status.exists() else None
    if result["ok"] and old == {"ok": True}:
        pass
    else:
        status.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
