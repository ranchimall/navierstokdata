"""
Pump.fun token profit tracker
-----------------------------
Reads every transaction of ONE token from the Solana blockchain (via Helius),
works out what each wallet bought / sold, and writes a report.html with:
  - sellers and the profit they made
  - current holders and their unrealised profit / loss

Usage (in Command Prompt, inside this folder):
    python tracker.py                 -> fetch new transactions + build report.html
    python tracker.py check <TX_LINK_OR_SIGNATURE>
                                      -> show how ONE transaction is interpreted
"""
import configparser
import html
import json
import os
import re
import sqlite3
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone

import requests

CFG_FILE = "config.ini"
DB_FILE = "trades.db"
DUST = 1e-9


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CFG_FILE):
        sys.exit("Could not find config.ini. It must be in the same folder as tracker.py.")
    cp = configparser.ConfigParser()
    cp.read(CFG_FILE, encoding="utf-8")
    cfg = dict(cp["settings"])
    if os.environ.get("HELIUS_API_KEY"):  # GitHub Actions secret overrides the file
        cfg["helius_api_key"] = os.environ["HELIUS_API_KEY"].strip()
    if "PASTE" in cfg.get("helius_api_key", "PASTE"):
        sys.exit("Please open config.ini and paste your Helius API key after 'helius_api_key ='.")
    return cfg


# --------------------------------------------------------------------------
# Talking to Solana (through Helius)
# --------------------------------------------------------------------------
def rpc(cfg, method, params, retries=6):
    url = "https://mainnet.helius-rpc.com/?api-key=" + cfg["helius_api_key"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        wait = 1.5 * (attempt + 1)
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:  # too many requests: slow down and retry
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
        except requests.RequestException:
            time.sleep(wait)
            continue
        if "error" in data:
            if "rate" in str(data["error"]).lower():
                time.sleep(wait)
                continue
            raise RuntimeError(f"{method} failed: {data['error']}")
        return data["result"]
    raise RuntimeError(f"{method} kept failing. Check your internet connection and API key.")


TX_VERSION = [1]  # highest transaction version we tell the server we can read; auto-raised if needed


def get_tx(cfg, sig):
    for _ in range(4):
        try:
            return rpc(cfg, "getTransaction", [sig, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": TX_VERSION[0],
                "commitment": "confirmed",
            }])
        except RuntimeError as e:
            m = re.search(r"maxSupportedTransactionVersion\"?: (\d+)", str(e))
            if m and int(m.group(1)) > TX_VERSION[0]:
                TX_VERSION[0] = int(m.group(1))  # server asked for a newer version: retry with it
                continue
            raise
    raise RuntimeError("Could not read transaction " + sig)


def fetch_new_transactions(cfg, db):
    mint = cfg["token_mint"]
    sigs, before = [], None
    while True:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        batch = rpc(cfg, "getSignaturesForAddress", [mint, opts])
        if not batch:
            break
        sigs.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break

    known = {row[0] for row in db.execute("select sig from txs")}
    new = [s["signature"] for s in sigs if s.get("err") is None and s["signature"] not in known]
    print(f"Found {len(sigs)} transactions on the blockchain, {len(new)} are new.")

    for i, sig in enumerate(reversed(new), 1):  # oldest first
        tx = get_tx(cfg, sig)
        if tx is None or tx.get("blockTime") is None:
            continue  # not available yet; picked up on the next run
        db.execute("insert or replace into txs values (?,?,?)",
                   (sig, tx["blockTime"], json.dumps(tx)))
        if i % 20 == 0:
            db.commit()
            print(f"  downloaded {i}/{len(new)}")
        time.sleep(0.12)  # stay under the free-plan speed limit
    db.commit()


# --------------------------------------------------------------------------
# Reading one transaction
# --------------------------------------------------------------------------
def _token_amounts(balances, mint):
    """owner -> amount of `mint` held, from pre/postTokenBalances."""
    out = defaultdict(float)
    for b in balances or []:
        if b.get("mint") == mint and b.get("owner"):
            ui = b["uiTokenAmount"]
            out[b["owner"]] += int(ui["amount"]) / (10 ** ui["decimals"])
    return out


def parse_tx(tx, token_mint, tsmx_mint, pool):
    meta = tx.get("meta")
    if not meta or meta.get("err") or tx.get("blockTime") is None:
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in tx["transaction"]["message"]["accountKeys"]]
    signer = keys[0]

    pre_t = _token_amounts(meta.get("preTokenBalances"), token_mint)
    post_t = _token_amounts(meta.get("postTokenBalances"), token_mint)
    token_delta = {o: post_t.get(o, 0.0) - pre_t.get(o, 0.0) for o in set(pre_t) | set(post_t)}

    pre_q = _token_amounts(meta.get("preTokenBalances"), tsmx_mint)
    post_q = _token_amounts(meta.get("postTokenBalances"), tsmx_mint)
    tsmx_delta = post_q.get(signer, 0.0) - pre_q.get(signer, 0.0)

    # SOL change of the signer, with the network fee added back (fee is not a trade cost)
    sol_delta = (meta["postBalances"][0] - meta["preBalances"][0] + meta.get("fee", 0)) / 1e9

    return {
        "sig": tx["transaction"]["signatures"][0],
        "time": tx["blockTime"],
        "signer": signer,
        "phase": "pool" if pool in keys else "curve",
        "token_delta": token_delta,
        "sol_delta": sol_delta,
        "tsmx_delta": tsmx_delta,
    }


def classify(rec, sol_usd, tsmx_usd):
    td = rec["token_delta"].get(rec["signer"], 0.0)
    usd = rec["sol_delta"] * sol_usd + rec["tsmx_delta"] * tsmx_usd
    if abs(td) < DUST:
        return "none", td, usd
    if td > 0 and usd < -0.0001:
        return "buy", td, usd
    if td < 0 and usd > 0.0001:
        return "sell", td, usd
    return "transfer", td, usd


# --------------------------------------------------------------------------
# Prices (all free, no key needed)
# --------------------------------------------------------------------------
def get_sol_hourly_prices(t0, t1):
    prices = {}
    start = int(t0 // 3600 * 3600) * 1000
    end = int((t1 // 3600 + 1) * 3600) * 1000
    hosts = ["https://data-api.binance.vision", "https://api.binance.com"]
    while start <= end:
        rows = None
        for host in hosts:
            try:
                r = requests.get(host + "/api/v3/klines", params={
                    "symbol": "SOLUSDT", "interval": "1h",
                    "startTime": start, "endTime": end, "limit": 1000}, timeout=30)
                r.raise_for_status()
                rows = r.json()
                break
            except requests.RequestException:
                continue
        if rows is None:
            return prices, False
        if not rows:
            break
        for row in rows:
            prices[row[0] // 1000] = (float(row[1]) + float(row[4])) / 2
        start = rows[-1][0] + 3600 * 1000
        if len(rows) < 1000:
            break
    return prices, True


def make_sol_lookup(t0, t1, fallback):
    prices, ok = get_sol_hourly_prices(t0, t1)
    if not ok or not prices:
        print("WARNING: could not download SOL prices. Using the fixed fallback price from config.ini.")
    hours = sorted(prices)

    def lookup(t):
        if not hours:
            return fallback
        h = t // 3600 * 3600
        if h in prices:
            return prices[h]
        earlier = [x for x in hours if x <= h]
        return prices[earlier[-1]] if earlier else prices[hours[0]]
    lookup.source = "Binance hourly price" if hours else "fixed fallback from config.ini"
    return lookup


def dex_price(mint):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=20)
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "solana" and p.get("priceUsd")]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        return float(best["priceUsd"])
    except Exception:
        return None


def pool_reserve_price(cfg, tsmx_usd):
    """Token price in USD from the pool's own reserves: (TSMx in pool / tokens in pool) x TSMx price."""
    def reserve(mint):
        res = rpc(cfg, "getTokenAccountsByOwner",
                  [cfg["pool_address"], {"mint": mint}, {"encoding": "jsonParsed"}])
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                   for a in res["value"])
    try:
        tokens, tsmx = reserve(cfg["token_mint"]), reserve(cfg["tsmx_mint"])
        if tokens > 0 and tsmx > 0:
            return tsmx / tokens * tsmx_usd
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# The profit calculation
# --------------------------------------------------------------------------
def new_wallet():
    return dict(balance=0.0, cost=0.0, realized=0.0, bought=0.0, sold=0.0,
                buys=0, sells=0, t_in=0, t_out=0, incomplete=False)


def analyse(recs, pool, sol_at, tsmx_usd):
    # Pass 1: classify every transaction and discover the bonding-curve / pool accounts
    # (the "other side" of every trade). Those are not real traders, so we exclude them.
    infra = {pool}
    items = []
    for r in recs:
        action, td, usd = classify(r, sol_at(r["time"]), tsmx_usd)
        items.append((r, action, td, usd))
        if action in ("buy", "sell"):
            for owner, d in r["token_delta"].items():
                if owner != r["signer"] and abs(d) > DUST and d * td < 0:
                    infra.add(owner)

    # Pass 2: walk through history in order, wallet by wallet (average-cost method)
    W = defaultdict(new_wallet)
    total_all, last_unit = 0.0, None
    for r, action, td, usd in items:
        total_all += sum(r["token_delta"].values())
        for owner, d in r["token_delta"].items():
            if abs(d) < DUST or owner in infra:
                continue
            w = W[owner]
            is_trade = owner == r["signer"] and action in ("buy", "sell")
            if is_trade and action == "buy":
                w["balance"] += d
                w["cost"] += -usd
                w["bought"] += -usd
                w["buys"] += 1
                last_unit = -usd / d
            elif is_trade and action == "sell":
                sold = -d
                avg = w["cost"] / w["balance"] if w["balance"] > DUST else 0.0
                covered = min(sold, max(w["balance"], 0.0))
                basis = avg * covered
                if sold > covered + 1e-6:
                    w["incomplete"] = True
                w["realized"] += usd - basis
                w["cost"] -= basis
                w["balance"] = max(w["balance"] - sold, 0.0)
                w["sold"] += usd
                w["sells"] += 1
                last_unit = usd / sold
            elif d > 0:  # tokens received without paying (transfer in)
                w["balance"] += d
                w["t_in"] += 1
            else:  # tokens sent away without being paid (transfer out)
                out = -d
                avg = w["cost"] / w["balance"] if w["balance"] > DUST else 0.0
                moved = min(out, max(w["balance"], 0.0))
                w["cost"] -= avg * moved
                w["balance"] = max(w["balance"] - out, 0.0)
                w["t_out"] += 1
            if w["balance"] < 1e-6:
                w["balance"], w["cost"] = 0.0, 0.0
    return W, infra, total_all, last_unit


# --------------------------------------------------------------------------
# Report (report.html)
# --------------------------------------------------------------------------
def money(x):
    s = f"${abs(x):,.2f}" if abs(x) >= 1 else f"${abs(x):,.4f}"
    return ("-" if x < 0 else "") + s


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}"


def when(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def flags_for(w, wallet, creator):
    f = []
    if wallet == creator:
        f.append("CREATOR / DEV")
    if w["t_in"]:
        f.append("received tokens by transfer")
    if w["t_out"]:
        f.append("sent tokens away by transfer")
    if w["incomplete"]:
        f.append("history incomplete, profit unreliable")
    return "; ".join(f)


def build_report(cfg, W, price_now, tsmx_usd, n_tx, supply_warning, price_source=""):
    creator = cfg["creator_wallet"]

    def cell(v, kind=""):
        if kind == "money":
            c = "pos" if v > 0 else "neg" if v < 0 else ""
            return f'<td class="num {c}">{money(v)}</td>'
        if kind == "num":
            return f'<td class="num">{v:,.2f}</td>'
        return f"<td>{v}</td>"

    def wallet_cell(a):
        return (f'<td><a href="https://solscan.io/account/{a}" target="_blank" '
                f'title="{a}">{short(a)}</a></td>')

    sellers = sorted([(a, w) for a, w in W.items() if w["sells"] > 0],
                     key=lambda x: x[1]["realized"], reverse=True)
    holders = sorted([(a, w) for a, w in W.items() if w["balance"] > 0.01],
                     key=lambda x: x[1]["balance"], reverse=True)

    srows = "".join(
        "<tr>" + wallet_cell(a) + cell(w["bought"], "money") + cell(w["sold"], "money")
        + cell(w["realized"], "money") + cell(w["balance"], "num")
        + cell(f'{w["buys"]} / {w["sells"]}') + cell(html.escape(flags_for(w, a, creator))) + "</tr>"
        for a, w in sellers)
    hrows = ""
    tot_unreal = 0.0
    unreal_transfer = 0.0  # P/L sitting on wallets that received tokens by transfer (no purchase price)
    for a, w in holders:
        value = w["balance"] * price_now
        unreal = value - w["cost"]
        tot_unreal += unreal
        if w["t_in"]:
            unreal_transfer += unreal
        hrows += ("<tr>" + wallet_cell(a) + cell(w["balance"], "num") + cell(value, "money")
                  + cell(w["cost"], "money") + cell(unreal, "money") + cell(w["realized"], "money")
                  + cell(html.escape(flags_for(w, a, creator))) + "</tr>")

    tot_real = sum(w["realized"] for _, w in sellers)
    warn = f'<p class="warn">{html.escape(supply_warning)}</p>' if supply_warning else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Token profit tracker</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1a1a1a;background:#fafafa}}
h1{{margin-bottom:4px}} h2{{margin-top:32px}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #ddd;padding:6px 10px;font-size:14px;text-align:left}}
th{{background:#f0f0f0}} td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:#0a7a2f}} .neg{{color:#c0281c}} .warn{{background:#fff3cd;padding:8px 12px;border:1px solid #e6c200}}
.box{{display:flex;gap:16px;flex-wrap:wrap}} .card{{background:#fff;border:1px solid #ddd;padding:10px 16px}}
.small{{color:#666;font-size:13px}}
</style></head><body>
<h1>Token profit tracker</h1>
<div class="small">Token {html.escape(cfg["token_mint"])} &middot; generated {when(int(time.time()))}
&middot; {n_tx} transactions analysed &middot; all values in USD</div>
{warn}
<div class="box" style="margin-top:12px">
<div class="card">Token price now<br><b>${price_now:.8f}</b><br><span class="small">{html.escape(price_source)}</span></div>
<div class="card">TSMx price used<br><b>{money(tsmx_usd)}</b></div>
<div class="card">Realised profit (all sellers)<br><b class="{'pos' if tot_real>0 else 'neg'}">{money(tot_real)}</b></div>
<div class="card">Unrealised P/L (all holders)<br><b class="{'pos' if tot_unreal>0 else 'neg'}">{money(tot_unreal)}</b><br><span class="small">of which {money(unreal_transfer)} is in wallets that received<br>tokens by transfer (no purchase price known)</span></div>
</div>
<h2>Sellers: profit already made</h2>
<table><tr><th>Wallet</th><th>Total bought</th><th>Total sold</th><th>Realised profit</th>
<th>Tokens still held</th><th>Buys / Sells</th><th>Notes</th></tr>{srows or '<tr><td colspan="7">No sells yet.</td></tr>'}</table>
<h2>Current holders: profit / loss on paper</h2>
<table><tr><th>Wallet</th><th>Tokens held</th><th>Value now</th><th>Cost of tokens held</th>
<th>Unrealised P/L</th><th>Realised so far</th><th>Notes</th></tr>{hrows or '<tr><td colspan="7">No holders found.</td></tr>'}</table>
<p class="small">Method: average cost per wallet. SOL amounts are converted to USD at the hourly SOL price of each trade;
TSMx amounts use the current TSMx price (approximation). Pool and bonding-curve accounts are excluded.
Network fees are excluded from trade values.</p>
</body></html>"""


def write_json(cfg, W, price_now, tsmx_usd, n_tx, warning, price_source, path="data.json"):
    creator = cfg["creator_wallet"]
    sellers, holders = [], []
    for a, w in W.items():
        flags = flags_for(w, a, creator)
        if w["sells"] > 0:
            sellers.append(dict(wallet=a, bought=w["bought"], sold=w["sold"], realized=w["realized"],
                                balance=w["balance"], buys=w["buys"], sells=w["sells"], notes=flags))
        if w["balance"] > 0.01:
            value = w["balance"] * price_now
            holders.append(dict(wallet=a, balance=w["balance"], value=value, cost=w["cost"],
                                unrealized=value - w["cost"], realized=w["realized"],
                                from_transfer=bool(w["t_in"]), notes=flags))
    sellers.sort(key=lambda x: x["realized"], reverse=True)
    holders.sort(key=lambda x: x["balance"], reverse=True)
    out = dict(
        generated=int(time.time()), token=cfg["token_mint"], n_tx=n_tx, n_wallets=len(W),
        price_now=price_now, price_source=price_source, tsmx_usd=tsmx_usd, warning=warning,
        total_realized=sum(x["realized"] for x in sellers),
        total_unrealized=sum(x["unrealized"] for x in holders),
        unrealized_transfer=sum(x["unrealized"] for x in holders if x["from_transfer"]),
        sellers=sellers, holders=holders)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_check(cfg, arg):
    sig = arg.rstrip("/").split("/")[-1].split("?")[0]
    tx = get_tx(cfg, sig)
    if tx is None:
        sys.exit("Transaction not found.")
    rec = parse_tx(tx, cfg["token_mint"], cfg["tsmx_mint"], cfg["pool_address"])
    if rec is None:
        sys.exit("This transaction failed on-chain or has no time, so it is ignored.")
    sol_at = make_sol_lookup(rec["time"], rec["time"], float(cfg["sol_price_fallback"]))
    tsmx_usd = dex_price(cfg["tsmx_mint"]) or float(cfg["tsmx_price_fallback"])
    sol_usd = sol_at(rec["time"])
    action, td, usd = classify(rec, sol_usd, tsmx_usd)
    print(f"\nTime:               {when(rec['time'])}")
    print(f"Phase:              {rec['phase']}")
    print(f"Signer (trader):    {rec['signer']}")
    print(f"Token change:       {td:+,.6f}")
    print(f"SOL change:         {rec['sol_delta']:+.6f} SOL  (fee excluded)  @ ${sol_usd:,.2f}  [{sol_at.source}]")
    print(f"TSMx change:        {rec['tsmx_delta']:+.8f} TSMx  @ ${tsmx_usd:,.2f}")
    print(f"Value for trader:   {usd:+,.4f} USD")
    print(f"Interpreted as:     {action.upper()}")
    print("\nCompare these numbers with what Solscan shows for this transaction.")


def main():
    cfg = load_config()
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        return cmd_check(cfg, sys.argv[2])

    db = sqlite3.connect(DB_FILE)
    db.execute("create table if not exists txs (sig text primary key, block_time integer, raw text)")
    fetch_new_transactions(cfg, db)

    raw = db.execute("select raw from txs order by block_time, sig").fetchall()
    recs = [r for r in (parse_tx(json.loads(x[0]), cfg["token_mint"], cfg["tsmx_mint"],
                                 cfg["pool_address"]) for x in raw) if r]
    if not recs:
        sys.exit("No transactions found for this token.")
    recs.sort(key=lambda r: r["time"])

    sol_at = make_sol_lookup(recs[0]["time"], recs[-1]["time"], float(cfg["sol_price_fallback"]))
    tsmx_usd = dex_price(cfg["tsmx_mint"]) or float(cfg["tsmx_price_fallback"])
    W, infra, total_all, last_unit = analyse(recs, cfg["pool_address"], sol_at, tsmx_usd)

    price_source = "DexScreener"
    price_now = dex_price(cfg["token_mint"])
    if price_now is None:
        price_now = pool_reserve_price(cfg, tsmx_usd)
        price_source = "pool reserves (TSMx in pool / tokens in pool)"
    if price_now is None:
        price_now = last_unit or 0.0
        price_source = "price of the last trade (no pool price available)"
        print("NOTE: could not read a pool price; using the price of the last trade.")
    print(f"Token price now: ${price_now:.8f}  [{price_source}]")

    warning = ""
    try:
        supply = float(rpc(cfg, "getTokenSupply", [cfg["token_mint"]])["value"]["uiAmount"])
        if supply > 0 and abs(total_all - supply) / supply > 0.005:
            warning = ("Heads-up: the transactions found add up to a token supply that differs from the real "
                       "supply, so some transfers may be missing and a few wallet balances could be off.")
    except Exception:
        pass

    report = build_report(cfg, W, price_now, tsmx_usd, len(recs), warning, price_source)
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(report)
    write_json(cfg, W, price_now, tsmx_usd, len(recs), warning, price_source)
    print(f"\nDone. {len(recs)} transactions, {len(W)} wallets. Wrote report.html and data.json.")
    if not os.environ.get("CI"):
        webbrowser.open("file://" + os.path.abspath("report.html"))


if __name__ == "__main__":
    main()
