"""Nasdaq-100 constituents for the Nasdaq 100 page of this app: data/ndx.json (stocks, periods' change, weights,
the index), data/ndx_daily.json (the index's daily bars) and data/rank_history.json (market-cap rank by month).

Sources: the weights and the list of stocks from Invesco QQQ's published holdings (which replicate the index), the
prices and market values from CNBC's quotes (a few requests for all the stocks), each stock's daily closes from
Nasdaq.com once a day after the close (for the week to the year), the index level from Nasdaq.com, and each stock's
sector from the kept list (Nasdaq.com's summary for a stock new to the index). One pass, run by the update workflow.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA = Path(__file__).resolve().parent.parent / "data"
OUT, RANKS, INDEX_DAILY, HIST, SEED = (DATA / f for f in ("ndx.json", "rank_history.json", "ndx_daily.json",
                                                          "ndx_hist.json", "ndx_sectors.json"))
NY = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"}
session = requests.Session()
session.headers.update(HEADERS)
# trading days back from the latest close for each period (ytd: the last close of the previous year)
BACK = {"w1": 5, "w4": 21, "w13": 63, "w26": 126, "w52": 252}
PERIODS = ["d1", "w1", "w4", "w13", "w26", "ytd", "w52"]


def get(url, **kw):
    last = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


NASDAQ_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def num(x):
    return float(str(x).replace("$", "").replace(",", "").strip())


def regular_session(stamp):
    """True for an official close ("Oct 16, 2026") or a value stamped 9:30-16:00 ET."""
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)", stamp or "", re.I)
    if not m:
        return bool(stamp)
    t = (int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)) * 60 + int(m.group(2))
    return 9 * 60 + 30 <= t <= 16 * 60


def nasdaq_json(path, query=""):
    r = requests.get(f"https://api.nasdaq.com/api/quote/NDX/{path}?assetclass=index{query}",
                     headers=NASDAQ_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("data") or {}


def fetch_index():
    """The Nasdaq-100 index level from Nasdaq.com: price, previous close and the day's high and low."""
    info = nasdaq_json("info")
    p = None
    for key in ("primaryData", "secondaryData"):
        c = info.get(key) or {}
        if c.get("lastSalePrice") and regular_session(c.get("lastTradeTimestamp")):
            p = c
            break
    if p is None:
        return None
    price = num(p["lastSalePrice"])
    q = {"price": round(price, 2), "prev": round(price - num(p.get("netChange") or 0), 2),
         "time": p.get("lastTradeTimestamp")}
    try:
        summ = nasdaq_json("summary").get("summaryData") or {}
        print("index summary fields:", sorted(summ))
        vals = {k.lower(): str((v or {}).get("value", "")) for k, v in summ.items() if isinstance(v, dict)}
        for k, v in vals.items():
            if "high" in k and "low" in k and "/" in v and "52" not in k:
                hi, lo = (num(x) for x in v.split("/"))
                q.update(high=round(hi, 2), low=round(lo, 2))
        hi = next((v for k, v in vals.items() if k in ("todayshigh", "todayhigh", "dayhigh")), None)
        lo = next((v for k, v in vals.items() if k in ("todayslow", "todaylow", "daylow")), None)
        if hi and lo and "high" not in q:
            q.update(high=round(num(hi), 2), low=round(num(lo), 2))
        pc = vals.get("previousclose")
        if pc:
            q["prev"] = round(num(pc), 2)
    except (requests.RequestException, ValueError) as e:
        print(f"warning: index summary: {e}", file=sys.stderr)
    return q


def quote_day(stamp):
    """yyyymmdd of a Nasdaq timestamp such as "Oct 19, 2026 9:36 AM ET", or None."""
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", stamp or "")
    mon = "JanFebMarAprMayJunJulAugSepOctNovDec".find(m.group(1)) if m else -1
    return None if mon < 0 else int(m.group(3)) * 10000 + (mon // 3 + 1) * 100 + int(m.group(2))


def update_index_daily(index):
    """Daily open/high/low/close of the index in data/ndx_daily.json, for the candle chart.

    The official history comes from Nasdaq.com (about ten years, kept and extended run by run;
    fetched at most every 30 minutes). Today's bar is built from the live quote until the
    official row appears; its open is the first value seen today, so it is approximate."""
    store = json.loads(INDEX_DAILY.read_text(encoding="utf-8")) if INDEX_DAILY.exists() else {}
    rows = {r[0]: r[1:5] for r in store.get("daily", [])}
    now = datetime.now(timezone.utc)
    fetched = store.get("fetchedAt")
    if not rows or not fetched or (now - datetime.fromisoformat(fetched)).total_seconds() > 1800:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        j = nasdaq_json("historical", f"&fromdate={today.replace(year=today.year - 10)}&todate={today}&limit=10000")
        got = 0
        for r in ((j.get("tradesTable") or {}).get("rows") or []):
            try:
                mo, d, y = r["date"].split("/")
                c = num(r["close"])
                o, h, l = (num(r[k]) if str(r.get(k, "")).strip() not in ("", "N/A") else c for k in ("open", "high", "low"))
                rows[int(f"{y}{mo}{d}")] = [round(o, 2), round(h, 2), round(l, 2), round(c, 2)]
                got += 1
            except (KeyError, ValueError):
                continue
        print(f"index history: {got} days from Nasdaq.com")
        if not got and not rows:
            raise RuntimeError(f"no index history rows; keys: {sorted(j)[:8]}")
        fetched = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    out = {"fetchedAt": fetched, "daily": [[d, *v] for d, v in sorted(rows.items())]}
    qd = quote_day((index or {}).get("time"))
    if qd and rows and qd > max(rows):
        before = store.get("today")
        o = before[1] if before and before[0] == qd else index["price"]
        hs = [x for x in (index.get("high"), index["price"]) if x]
        ls = [x for x in (index.get("low"), index["price"]) if x]
        out["today"] = [qd, o, max(hs), min(ls), index["price"]]
    if out != store:
        INDEX_DAILY.write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")



QQQ_HOLDINGS = ("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund"
                "?idType=ticker&interval=monthly&productType=ETF")


def fetch_holdings():
    """Index weights and names from the holdings of Invesco QQQ, which fully replicates the Nasdaq-100.
    GOOG and GOOGL are merged into one Alphabet line (GOOGL); weights are rescaled to sum to 100 over the stocks."""
    import urllib.request
    j = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(QQQ_HOLDINGS, headers={"User-Agent": UA, "Accept": "application/json"})
            j = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
            break
        except Exception as e:  # noqa: BLE001
            print(f"warning: QQQ holdings: {e}", file=sys.stderr)
            time.sleep(3)
    if j is None:
        raise RuntimeError("QQQ holdings unavailable")
    w, names, total = {}, {}, 0.0
    for h in j.get("holdings", []):
        pct, ticker = h.get("percentageOfTotalNetAssets"), h.get("ticker")
        kind = h.get("securityTypeName") or ""
        if not ticker or not isinstance(pct, (int, float)):
            continue
        if "Common Stock" not in kind and "Depository Receipt" not in kind:
            continue
        t = "GOOGL" if ticker == "GOOG" else ticker
        w[t] = w.get(t, 0.0) + pct
        names.setdefault(t, h.get("issuerName") or t)
        total += pct
    if len(w) < 90 or total <= 0:
        raise RuntimeError(f"QQQ holdings: only {len(w)} stocks")
    return ({"asOf": j.get("effectiveDate"), "source": "Invesco QQQ",
             "w": {k: round(v / total * 100, 4) for k, v in w.items()}}, names)


CNBC = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols={}"
        "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1")


def cap_millions(text):
    m = re.match(r"^([\d.]+)([TBMK])$", (text or "").strip())
    if not m:
        return None
    return round(float(m.group(1)) * {"T": 1_000_000, "B": 1_000, "M": 1, "K": 0.001}[m.group(2)])


def fetch_quotes(tickers):
    """Regular-session last price, previous close, change and market value of every stock (30 to a request)."""
    out = {}
    for i in range(0, len(tickers), 30):
        part = tickers[i:i + 30]
        j = get(CNBC.format("|".join(part))).json()
        rows = ((j.get("FormattedQuoteResult") or {}).get("FormattedQuote")) or []
        for q in rows:
            try:
                last, prev = num(q["last"]), num(q["previous_day_closing"])
            except (KeyError, ValueError):
                continue
            day = None
            if q.get("last_time"):
                day = int(datetime.fromisoformat(q["last_time"][:19]).strftime("%Y%m%d"))
            out[q["symbol"]] = {"last": last, "prev": prev, "day": day, "cap": cap_millions(q.get("mktcapView")),
                                "name": q.get("name")}
        time.sleep(0.5)
    return out


def nasdaq_quote(t):
    p = (nasdaq_stock(t, "info").get("primaryData")) or {}
    last = num(p["lastSalePrice"])
    prev = round(last - num(p.get("netChange") or 0), 4)
    summ = nasdaq_stock(t, "summary").get("summaryData") or {}
    cap = (summ.get("MarketCap") or {}).get("value")
    pc = (summ.get("PreviousClose") or {}).get("value")
    if pc:
        prev = num(pc)
    return {"last": last, "prev": prev, "day": quote_day(p.get("lastTradeTimestamp")),
            "cap": round(num(cap) / 1e6) if cap else None, "name": None}


def nasdaq_stock(t, path, query=""):
    r = requests.get(f"https://api.nasdaq.com/api/quote/{t}/{path}?assetclass=stocks{query}",
                     headers=NASDAQ_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("data") or {}


def stock_closes(t, today):
    """About 14 months of a stock's daily closes from Nasdaq.com: {yyyymmdd: close}."""
    j = nasdaq_stock(t, "historical", f"&fromdate={today - timedelta(days=430)}&todate={today}&limit=400")
    rows = {}
    for r in ((j.get("tradesTable") or {}).get("rows") or []):
        try:
            mo, d, y = r["date"].split("/")
            rows[int(f"{y}{mo}{d}")] = round(num(r["close"]), 4)
        except (KeyError, ValueError):
            continue
    if len(rows) < 30:
        raise RuntimeError(f"{t}: only {len(rows)} closes")
    return rows


def update_hist(tickers):
    """Each stock's daily closes, refreshed once a day after the close (and for a stock not yet kept).
    Kept as one list of days and, per stock, the closes on those days (None where missing): {t: {yyyymmdd: close}}."""
    hist = json.loads(HIST.read_text(encoding="utf-8")) if HIST.exists() else {}
    days = hist.get("d") or []
    have = {t: {d: v for d, v in zip(days, c) if v is not None} for t, c in (hist.get("c") or {}).items()}
    now = datetime.now(NY)
    today = now.date()
    # due when the kept closes stop before the index's latest official day (Nasdaq.com posts a day's closes some
    # time after the close); tried at most once an hour
    try:
        target = max(r[0] for r in json.loads(INDEX_DAILY.read_text(encoding="utf-8"))["daily"])
    except Exception:  # noqa: BLE001
        target = None
    tried = hist.get("tried")
    due = bool(target and (not days or days[-1] < target)
               and (not tried or (datetime.now(timezone.utc) - datetime.fromisoformat(tried)).total_seconds() > 3600))
    want = [t for t in tickers if due or t not in have]
    got = 0
    for t in want:
        try:
            have[t] = stock_closes(t, today)
            got += 1
        except Exception as e:  # noqa: BLE001  (one stock must not stop the rest)
            print(f"warning: closes {t}: {e}"[:200], file=sys.stderr)
        time.sleep(0.4)
    have = {t: v for t, v in have.items() if t in tickers}
    if want:
        days = sorted({d for v in have.values() for d in v})[-300:]
        out = {"tried": datetime.now(timezone.utc).isoformat(timespec="seconds") if due else tried, "d": days,
               "c": {t: [round(v[d], 2) if d in v else None for d in days] for t, v in sorted(have.items())}}
        HIST.write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"closes: {got}/{len(want)} stocks")
    return have


def perf_of(closes, q):
    """Change over each period, from the latest price and the closes before its day."""
    if not q:
        return {}
    days = sorted((int(k), v) for k, v in (closes or {}).items() if not q["day"] or int(k) < q["day"])
    out = {"d1": round((q["last"] / q["prev"] - 1) * 100, 2) if q["prev"] else None}
    if days:
        for p, n in BACK.items():
            if len(days) >= n:
                out[p] = round((q["last"] / days[-n][1] - 1) * 100, 2)
        y = (q["day"] or days[-1][0]) // 10000
        prior = [v for d, v in days if d // 10000 < y]
        if prior:
            out["ytd"] = round((q["last"] / prior[-1] - 1) * 100, 2)
    return out


def sector_of(t, kept):
    if t in kept:
        return kept[t]
    try:
        s = nasdaq_stock(t, "summary").get("summaryData") or {}
        return {"sec": (s.get("Sector") or {}).get("value") or "", "ind": (s.get("Industry") or {}).get("value") or ""}
    except Exception as e:  # noqa: BLE001
        print(f"warning: sector {t}: {e}"[:200], file=sys.stderr)
        return {"sec": "", "ind": ""}


def next_month(key):
    y, m = map(int, key.split("-"))
    return f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"


def update_rank_history(stocks):
    """Keep the market-cap rank of every current constituent for the current month (New York time).

    Each run overwrites the current month, so the last run of a month leaves the month-end rank.
    """
    if not RANKS.exists():
        return
    hist = json.loads(RANKS.read_text(encoding="utf-8"))
    months, ranks = hist["months"], hist["ranks"]
    cur = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m")
    if cur < months[-1]:
        return
    while months[-1] < cur:
        months.append(next_month(months[-1]))
        for t in ranks:
            ranks[t].append(None)
    live = {s["t"]: i + 1 for i, s in enumerate(sorted(stocks, key=lambda s: -s["cap"]))}
    for t in ranks:  # stocks that left the index get no rank for this month
        ranks[t][-1] = None
    for t, r in live.items():
        if t not in ranks:
            ranks[t] = [None] * len(months)
        ranks[t][-1] = r
    text = json.dumps(hist, ensure_ascii=False, separators=(",", ":"))
    if text != RANKS.read_text(encoding="utf-8"):
        RANKS.write_text(text, encoding="utf-8")
        print(f"rank history: {cur} updated")



def main():
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    # sectors: the kept list (data/ndx_sectors.json), then the last run's stocks; a new stock is looked up once
    kept = json.loads(SEED.read_text(encoding="utf-8")) if SEED.exists() else {}
    kept.update({s["t"]: {"sec": s.get("sec", ""), "ind": s.get("ind", "")} for s in old.get("stocks") or [] if s.get("sec")})
    now = datetime.now(NY)
    weights, names = old.get("weights"), {}
    if not weights or weights.get("fetched") != str(now.date()):
        try:
            weights, names = fetch_holdings()
            weights["fetched"] = str(now.date())
        except Exception as e:  # keep the last known weights if Invesco is unavailable
            print(f"warning: {e}; keeping previous weights", file=sys.stderr)
    if not weights:
        sys.exit("no holdings")
    tickers = sorted(weights["w"])
    quotes = fetch_quotes(tickers)
    for t in [t for t in tickers if t not in quotes]:
        try:
            quotes[t] = nasdaq_quote(t)
        except Exception as e:  # noqa: BLE001
            print(f"warning: quote {t}: {e}"[:200], file=sys.stderr)
    if len(quotes) < 90:
        print(f"warning: only {len(quotes)} quotes; keeping the previous data", file=sys.stderr)
        quotes = {}
    try:
        index = fetch_index() or old.get("index")
    except Exception as e:  # noqa: BLE001
        print(f"warning: index: {e}", file=sys.stderr)
        index = old.get("index")
    try:
        update_index_daily(index)
    except Exception as e:  # noqa: BLE001
        print(f"warning: index history: {e}", file=sys.stderr)
    closes = update_hist(tickers)
    prev_names = {s["t"]: s["name"] for s in old.get("stocks") or []}
    stocks, perf = [], {p: {} for p in PERIODS}
    if quotes:
        for t in tickers:
            q = quotes.get(t)
            if not q or not q["cap"]:
                continue
            stocks.append({"t": t, "name": names.get(t) or prev_names.get(t) or q["name"] or t, **sector_of(t, kept),
                           "cap": q["cap"]})
            for p, v in perf_of(closes.get(t), q).items():
                if v is not None:
                    perf[p][t] = v
        stocks.sort(key=lambda s: -s["cap"])
    else:
        stocks, perf = old.get("stocks") or [], old.get("perf") or perf
    if quotes:
        try:
            update_rank_history(stocks)
        except Exception as e:  # noqa: BLE001
            print(f"warning: rank history: {e}", file=sys.stderr)
    if (old.get("perf") == perf and old.get("stocks") == stocks and old.get("weights") == weights
            and old.get("index") == index):
        print("no change")
        return
    data = {"updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "CNBC, Nasdaq.com, Invesco", "stocks": stocks, "perf": perf, "weights": weights}
    if index:
        data["index"] = index
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"updated: {len(stocks)} stocks, NVDA d1 {perf['d1'].get('NVDA')} w1 {perf['w1'].get('NVDA')}")


if __name__ == "__main__":
    main()
