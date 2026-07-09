#!/usr/bin/env python3
"""pair_scan.py — top-100 digital-asset correlation scan (research tool, no trading).

Run on the Larry VM (it has open egress; the Claude sandbox does not):
    /home/msunderji/bot-env/bin/python3 pair_scan.py

Pulls 90 days of daily candles for every USD spot product on Coinbase Exchange
(public API, no auth), ranks by 90-day dollar volume, keeps the top 100, and
prints: the most negative correlation pairs, each asset's correlation to BTC,
and a stability check (first 45d vs last 45d) so transient artifacts are visible.
"""
import json
import math
import time
import urllib.request

BASE = "https://api.exchange.coinbase.com"
DAYS = 90
TOP_N = 100
SKIP_BASES = {  # stablecoins & wrapped duplicates: no alpha or corr ~ 1.0 to base asset
    "USDT", "USDC", "DAI", "PYUSD", "GUSD", "TUSD", "FDUSD", "USDS", "EURC",
    "WBTC", "CBBTC", "WETH", "CBETH", "LSETH", "MSOL", "WAXL",
}


def get_json(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "larry-pair-scan/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def daily_closes(product_id):
    # granularity 86400 -> [[time, low, high, open, close, volume], ...] newest first
    rows = get_json(f"/products/{product_id}/candles?granularity=86400")
    rows = sorted(rows)[-DAYS:]
    closes = [r[4] for r in rows]
    dollar_vol = sum(r[4] * r[5] for r in rows)
    times = [r[0] for r in rows]
    return times, closes, dollar_vol


def log_returns(closes):
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def pearson(x, y):
    n = min(len(x), len(y))
    if n < 30:
        return None
    x, y = x[-n:], y[-n:]
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def main():
    products = get_json("/products")
    usd = [p["id"] for p in products
           if p.get("quote_currency") == "USD"
           and not p.get("trading_disabled")
           and p.get("status") == "online"
           and p.get("base_currency") not in SKIP_BASES]
    print(f"{len(usd)} USD products; fetching {DAYS}d candles (rate-limited)...")

    series, vols = {}, {}
    for i, pid in enumerate(usd):
        try:
            _, closes, dv = daily_closes(pid)
            if len(closes) >= 60:
                series[pid] = closes
                vols[pid] = dv
        except Exception as e:
            print(f"  skip {pid}: {e}")
        time.sleep(0.15)  # stay well under public rate limits
        if (i + 1) % 50 == 0:
            print(f"  ...{i+1}/{len(usd)}")

    top = sorted(vols, key=vols.get, reverse=True)[:TOP_N]
    print(f"\nTop {len(top)} by 90d dollar volume retained. Computing correlations...")
    rets = {p: log_returns(series[p]) for p in top}
    half = DAYS // 2

    # Correlation of everything vs BTC-USD
    btc = rets.get("BTC-USD")
    print("\n=== Correlation to BTC (90d daily log returns) — lowest 15 ===")
    to_btc = []
    for p in top:
        if p == "BTC-USD" or btc is None:
            continue
        c = pearson(rets[p], btc)
        if c is not None:
            to_btc.append((c, p))
    for c, p in sorted(to_btc)[:15]:
        c1 = pearson(rets[p][:half], btc[:half])
        c2 = pearson(rets[p][half:], btc[half:])
        print(f"  {p:14s} corr={c:+.2f}   first-half={c1 if c1 is None else round(c1,2)}  second-half={c2 if c2 is None else round(c2,2)}")

    # Most negative pairs across the whole matrix
    print("\n=== Most negative pairs in the whole top-100 matrix ===")
    pairs = []
    keys = list(top)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            c = pearson(rets[keys[i]], rets[keys[j]])
            if c is not None:
                pairs.append((c, keys[i], keys[j]))
    for c, a, b in sorted(pairs)[:15]:
        print(f"  {a:14s} vs {b:14s} corr={c:+.2f}")

    # Highest-correlation pairs = actual pair-trade (spread) candidates
    print("\n=== Highest-correlation pairs (true spread-trade candidates) ===")
    for c, a, b in sorted(pairs, reverse=True)[:10]:
        print(f"  {a:14s} vs {b:14s} corr={c:+.2f}")

    print("\nInterpretation guide: a dollar-neutral pair trade wants HIGH positive "
          "correlation with a mean-reverting spread. Negative-correlation pairs "
          "held long/short are a doubled directional bet, not a hedge.")


if __name__ == "__main__":
    main()
