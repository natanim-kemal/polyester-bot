"""Gap hunter + executor.

Scans every USDT-quoted pair for bid-ask gaps that beat the 0.2% maker
round-trip fee, strikes fast (buy at bid, ask just under the offer), manages
the ask with the bail-out rule, and Telegram-alerts every step.

Bail-out rule (user spec): while our ask rests, if more than 2 better
(lower) ask levels sit above... i.e. below our price for >60s, cancel and
re-ask one tick under the current best ask (maker exit, even at a loss).

Adopted at startup: any asset with a Trading balance that has a USDT pair
(e.g. the BTC from the filled manual order, plus XRP).
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_FLOOR

import nacl.signing
from polyester import POLYESTER_TESTNET_ENVIRONMENT, AsyncPolyester
from polyester.models import ClientOrderId

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if __package__:
    from .paths import HERE, env_files, state_file
else:
    from paths import HERE, env_files, state_file


def load_env():
    for path in env_files():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
        except OSError:
            pass


load_env()

API = "https://api.testnet.polyester.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
KEY = os.environ.get("POLYESTER_API_KEY_ID", "")
_SK = nacl.signing.SigningKey(bytes.fromhex(os.environ.get("POLYESTER_ED25519_SEED", "0" * 64)))
ACCOUNT = os.environ.get("POLYESTER_ACCOUNT_ID", "")

QUOTE = "USDT"
# Real per-pair fees, read from the exchange every 30 min (observed VIP3:
# maker 0.05 / taker 0.105). An explicit GAP_MIN_PCT env still pins the floor.
DEFAULT_MAKER_PCT = Decimal("0.05")
DEFAULT_TAKER_PCT = Decimal("0.105")
GAP_MARGIN_PCT = Decimal(os.environ.get("GAP_MARGIN_PCT", "0.01"))
GAP_MIN_FIXED = (Decimal(os.environ["GAP_MIN_PCT"])
                 if os.environ.get("GAP_MIN_PCT") else None)
FEES = {}
FEES_REFRESH_SECONDS = float(os.environ.get("FEES_REFRESH_SECONDS", "1800"))
DEPTH_MIN_USD = Decimal(os.environ.get("DEPTH_MIN_USD", "50"))
FALLBACK_SYMBOL = os.environ.get("FALLBACK_SYMBOL", "").strip()
FALLBACK_MIN_GAP_PCT = Decimal(os.environ.get("FALLBACK_MIN_GAP_PCT", "0.05"))
FALLBACK_DEPTH_MIN_USD = Decimal(os.environ.get("FALLBACK_DEPTH_MIN_USD", "1000"))
FALLBACK_COOLDOWN = float(os.environ.get("FALLBACK_COOLDOWN", "60"))
FALLBACK_PENNY = os.environ.get("FALLBACK_PENNY", "0") == "1"
SAT_PENNY_AFTER = int(os.environ.get("SAT_PENNY_AFTER", "2"))
SAT_TAKER_AFTER = int(os.environ.get("SAT_TAKER_AFTER", "4"))
FALLBACK_BUY_TIMEOUT = float(os.environ.get("FALLBACK_BUY_TIMEOUT", "60"))
TP_TRIGGER_PCT = Decimal(os.environ.get("TP_TRIGGER_PCT", "0"))
SCAN_SECONDS = float(os.environ.get("SCAN_SECONDS", "10"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
PERSIST_SECONDS = float(os.environ.get("PERSIST_SECONDS", "60"))
BETTER_ASKS_LIMIT = int(os.environ.get("BETTER_ASKS_LIMIT", "1"))
BETTER_BIDS_LIMIT = int(os.environ.get("BETTER_BIDS_LIMIT", "1"))
BUY_TIMEOUT_SECONDS = float(os.environ.get("BUY_TIMEOUT_SECONDS", "180"))
DEADMAN_TIMEOUT = int(os.environ.get("DEADMAN_TIMEOUT", "120"))
DEADMAN_REFRESH = 60
FEE_BUFFER = Decimal("1.0015")
FREEZE_SECONDS = float(os.environ.get("FREEZE_SECONDS", "300"))
STALL_SECONDS = float(os.environ.get("STALL_SECONDS", "900"))
BALANCE_GRACE = float(os.environ.get("BALANCE_GRACE_SECONDS", "120"))
HEARTBEAT = "/var/tmp/polyester.heartbeat"


def maker_fee_pct(fees, sym):
    """Maker fee % for one pair, falling back to the observed VIP3 rate."""
    return (fees.get(sym) or (DEFAULT_MAKER_PCT, DEFAULT_TAKER_PCT))[0]


def taker_fee_pct(fees, sym):
    return (fees.get(sym) or (DEFAULT_MAKER_PCT, DEFAULT_TAKER_PCT))[1]


def rt_fee_pct(fees, sym):
    """Round-trip % for two maker legs."""
    return 2 * maker_fee_pct(fees, sym)


def gap_min_pct(fixed, fees=None, sym=None):
    """Profitable-gap floor: an explicit GAP_MIN_PCT wins, else 2 x maker + margin."""
    if fixed is not None:
        return fixed
    return 2 * maker_fee_pct(fees or FEES, sym) + GAP_MARGIN_PCT


def round_trip_fees(buy_usd, sell_usd, maker_pct):
    """USDT fees for a maker round trip at maker_pct percent per leg."""
    return (buy_usd + sell_usd) * maker_pct / 100


def entry_price(tag, bid, ask, m, penny, taker):
    """Where the BUY rests: gap buys always penny, a calm fallback joins the
    bid, and the saturation ladder pennies then crosses the spread."""
    if taker:
        return ask
    if tag == "GAP" or penny:
        return min(bid + m["tick"], ask - m["tick"])
    return bid


def buy_timeout_for(tag, sat_n):
    """Fallbacks on the saturation ladder stop waiting sooner."""
    if tag != "GAP" and sat_n >= SAT_PENNY_AFTER:
        return FALLBACK_BUY_TIMEOUT
    return BUY_TIMEOUT_SECONDS


async def deadman_rearm(c):
    """Re-arm the exchange-side auto-cancel: if the bot stops re-arming,
    every open order is cancelled once the deadline passes."""
    try:
        await c.orders.cancel_all_after(timeout_sec=DEADMAN_TIMEOUT)
        return True
    except Exception as e:
        print("deadman re-arm failed:", repr(e), flush=True)
        return False


async def refresh_fees(c):
    """Pull the exchange's real per-pair fee rates; keep the gap floor in step."""
    global GAP_MIN_PCT
    try:
        fr = await c.fees.get_spot_fee_rates()
        for r in fr.fee_rates:
            FEES[r.symbol] = (pdec(r.maker_fee_rate_percent),
                              pdec(r.taker_fee_rate_percent))
        GAP_MIN_PCT = gap_min_pct(GAP_MIN_FIXED)
        if FEES:
            s0 = next(iter(FEES))
            print(f"fees refreshed: {len(FEES)} pairs | maker {FEES[s0][0]}% "
                  f"taker {FEES[s0][1]}% | gap floor {GAP_MIN_PCT}%", flush=True)
    except Exception as e:
        print("fee refresh failed:", repr(e), flush=True)


GAP_MIN_PCT = gap_min_pct(GAP_MIN_FIXED)


def frozen_seconds(wall, mono, last_wall, last_mono):
    """Seconds the VM spent frozen: wall clock advanced but the guest did not run.

    A paused sandbox stops the guest kernel, so CLOCK_MONOTONIC freezes while
    the wall clock keeps going. The difference is the freeze duration.
    """
    return (wall - last_wall) - (mono - last_mono)

TERMINAL = {"filled", "canceled", "rejected", "expired"}

COST_BASIS = {"BTC-USDT": Decimal("76227.38"), "ARB-USDT": Decimal("0.1684")}


def pdec(v):
    if v is None or v == "":
        return Decimal(0)
    return v.as_decimal() if hasattr(v, "as_decimal") else Decimal(str(v))


def qdec(v, scale):
    if v is None or v == "":
        return Decimal(0)
    return v.as_decimal(scale) if hasattr(v, "as_decimal") else Decimal(str(v))


def floor_step(x, step):
    step = Decimal(str(step))
    if step <= 0:
        return x
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def alert(text):
    print(text, flush=True)
    import threading

    def send():
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if tok and chat:
            try:
                req = urllib.request.Request(
                    "https://api.telegram.org/bot" + tok + "/sendMessage",
                    data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode())
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print("telegram failed:", repr(e), flush=True)
        hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if hook:
            try:
                req = urllib.request.Request(
                    hook, data=json.dumps({"content": text}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print("discord failed:", repr(e), flush=True)

    threading.Thread(target=send, daemon=True).start()


def money(x):
    return f"{Decimal(x):,.2f}"


def num(x):
    s = f"{Decimal(x):,.8f}".rstrip("0").rstrip(".")
    return s or "0"


def wallet(base=None):
    """The balances that matter right now: 'USDT 3,094.10 | ARB 8,946.35'."""
    try:
        rows = {r.get("symbol"): r for r in rest_balances()}
    except Exception as e:
        print("wallet read failed:", repr(e), flush=True)
        return "balances unavailable"
    parts = [f"{QUOTE} {money(rows.get(QUOTE, {}).get('trading') or 0)}"]
    if base and base in rows:
        parts.append(f"{base} {num(rows[base].get('trading') or 0)}")
    return " | ".join(parts)


def notify(emoji, action, *lines):
    """Telegram layout: emoji + action, then one line per fact."""
    alert("\n".join([f"{emoji} {action}"] + [l for l in lines if l]))


def rest_open_orders():
    """All open orders via signed REST (carries clientOrderId + symbol)."""
    ts = str(int(time.time() * 1000))
    path = "/v1/orders/open"
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", _SK.sign(canon.encode()).signature.hex())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{"orders": []}')


def rest_balances():
    """GET /v1/balances via signed REST (rows carry asset symbols)."""
    ts = str(int(time.time() * 1000))
    path = "/v1/balances"
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", _SK.sign(canon.encode()).signature.hex())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["balances"]


def rest_me():
    """GET /v1/auth/me via signed REST (the account this key belongs to)."""
    ts = str(int(time.time() * 1000))
    path = "/v1/auth/me"
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", _SK.sign(canon.encode()).signature.hex())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


async def place(c, sym, side, qty, price, post_only=False):
    cid = "gap1-" + side[0] + "-" + uuid.uuid4().hex[:12]
    item = {"symbol": sym, "side": side, "order_type": "limit", "tif": "gtc",
            "qty": format(qty, "f"), "price": format(price, "f"),
            "client_order_id": cid}
    if post_only:
        item["post_only"] = True
    res = await c.orders.batch_create(symbol=sym, items=[item],
                                      request_id="gap1-" + uuid.uuid4().hex[:12])
    r = res.results[0]
    if r.status == "rejected":
        return None, str(getattr(r, "code", "") or "REJECTED")
    return cid, None


async def order_state(c, sym, cid, scale):
    """(status_lower, cum_qty, avg_px) for one of our orders."""
    res = await c.orders.get(key=ClientOrderId(cid))
    o = getattr(res, "order", None)
    if o is None:
        return "pending", Decimal(0), Decimal(0)
    return (str(o.status).lower(), qdec(o.cum_qty, scale), pdec(o.avg_px))


async def chase_buy(c, pos, m, now):
    """Mirror of the ask bail-out: when competitors outbid our resting BUY
    for too long, cancel and rebid one tick above the best bid, never into
    the ask. Resets the timeout clock on every chase."""
    sym = pos["symbol"]
    bk = await book(c, sym, depth=10)
    better = {pdec(b.price) for b in (bk.bids or [])
              if pdec(b.price) > pos["buy_px"]}
    track = pos["better"]
    for p in list(track):
        if p not in better:
            del track[p]
    for p in better:
        track.setdefault(p, now)
    aged = sum(1 for p, t0 in track.items() if now - t0 >= PERSIST_SECONDS)
    if aged < BETTER_BIDS_LIMIT:
        return
    best = max(better)
    new_px = best + m["tick"]
    top = min((pdec(a.price) for a in (bk.asks or []) if pdec(a.price) > 0),
              default=None)
    if top is not None:
        new_px = min(new_px, top - m["tick"])
    if new_px <= pos["buy_px"]:
        return
    st, cum, avg = await order_state(c, sym, pos["buy_cid"], m["scale"])
    left = floor_step((pos.get("qty_target") or Decimal(0))
                      - floor_step(cum, m["step"]), m["step"])
    if st not in TERMINAL:
        try:
            await c.orders.modify(key=ClientOrderId(pos["buy_cid"]),
                                  symbol=sym, new_price=new_px)
            pos["buy_px"] = new_px
            pos["buy_at"] = now
            pos["better"] = {}
            notify("🔄", "BID CHASE",
                   f"{sym} | outbid by {aged} bid(s) held {PERSIST_SECONDS:.0f}s "
                   f"| amending to {num(new_px)}",
                   wallet(sym.split("-")[0]))
            return
        except Exception as e:
            print("amend fell back to cancel:", repr(e), flush=True)
    try:
        await c.orders.cancel(key=ClientOrderId(pos["buy_cid"]), symbol=sym)
    except Exception:
        pass
    st, cum, avg = await order_state(c, sym, pos["buy_cid"], m["scale"])
    left = floor_step((pos.get("qty_target") or Decimal(0))
                      - floor_step(cum, m["step"]), m["step"])
    cid = None
    if left > 0 and left >= m["min_qty"] and left * new_px >= m["min_notional"]:
        cid, _ = await place(c, sym, "buy", left, new_px)
    if cid:
        pos["buy_cid"] = cid
        pos["buy_px"] = new_px
        pos["buy_at"] = now
        pos["better"] = {}
        notify("🔄", "BID CHASE",
               f"{sym} | outbid by {aged} bid(s) held {PERSIST_SECONDS:.0f}s "
               f"| re-bidding {num(left)} @ {num(new_px)}",
               wallet(sym.split("-")[0]))
    # a failed or unneeded re-place is reconciled by the terminal/timeout paths


async def place_tp_trigger(c, pos, m):
    """Arm a server-side take-profit sell: the exchange dumps the position
    itself when price rallies TP_TRIGGER_PCT above entry, bot frozen or not."""
    if TP_TRIGGER_PCT <= 0:
        return None
    sym = pos["symbol"]
    qty = floor_step(pos.get("qty") or Decimal(0), m["step"])
    if qty <= 0 or qty < m["min_qty"]:
        return None
    basis = pos.get("buy_px") or Decimal(0)
    ref = basis
    if ref <= 0:
        try:
            bk = await book(c, sym, depth=1)
            ref = pdec(bk.asks[0].price) if bk.asks else Decimal(0)
        except Exception as e:
            print("tp trigger skipped: book unreadable:", repr(e), flush=True)
            return None
    if ref <= 0:
        return None
    tp = ref * (1 + TP_TRIGGER_PCT / 100)
    if qty * tp < m["min_notional"]:
        return None
    cid = "gap1-tp-" + uuid.uuid4().hex[:12]
    try:
        res = await c.triggers.create(symbol=sym, trigger_type="take_profit",
                                      side="sell", qty=format(qty, "f"),
                                      trigger_price=format(tp, "f"),
                                      order_type="market",
                                      client_trigger_id=cid)
        pos["tp_cid"] = cid
        pos["tp_tid"] = getattr(res, "trigger_id", None)
        notify("🎯", "TP ARMED",
               f"{sym} | exchange sells {num(qty)} @ {num(tp)} "
               f"| entry {num(ref)}")
        return res
    except Exception as e:
        print("tp trigger failed:", repr(e), flush=True)
        return None


async def cancel_stale_symbol_orders(c, sym):
    """Cancel our own gap1- orders resting on this symbol. The TP trigger can
    beat our own ask to the fill; a leftover sell would be unbacked."""
    try:
        stale = [o for o in rest_open_orders().get("orders", [])
                 if str(o.get("clientOrderId", "")).startswith("gap1-")
                 and o.get("symbol") == sym]
        for o in stale:
            try:
                await c.orders.cancel(key=ClientOrderId(o["clientOrderId"]),
                                      symbol=sym)
            except Exception:
                pass
        return len(stale)
    except Exception as e:
        print("stale order sweep failed:", repr(e), flush=True)
        return 0


async def cancel_tp_trigger(c, pos):
    """Best-effort disarm once the cycle closes."""
    tid = pos.get("tp_tid")
    if tid is not None:
        try:
            await c.triggers.cancel(trigger_id=tid)
        except Exception:
            pass
        pos["tp_tid"] = None


FALLBACK_CANDIDATES = 4


async def pick_best_liquid(c, fb, top=FALLBACK_CANDIDATES):
    """Among the tightest fallback candidates prefer the one whose tape is
    alive; an unreadable tape counts as zero flow. Ties keep gap order."""
    if not fb:
        return None
    best, best_n = None, -1
    for row in fb[:top]:
        try:
            tr = await c.market_data.get_trades(symbol=row[1], limit=20)
            n = len(tr.trades)
        except Exception as e:
            print("trade count skip", row[1], repr(e), flush=True)
            n = 0
        if n > best_n:
            best, best_n = row, n
    return best or fb[0]


async def preflight_buy(c, sym, side, qty, price):
    """Ask the exchange whether the buy would be admissible. Fail-open: only
    a clear rejection blocks; an outage proceeds."""
    try:
        res = await c.orders.preview_order(symbol=sym, side=side,
                                           order_type="limit", tif="gtc",
                                           qty=format(qty, "f"),
                                           price=format(price, "f"))
        adm = getattr(res, "admissible", None)
        if adm is None or adm:
            return True, None
        rej = getattr(res, "rejection", None)
        code = getattr(rej, "code", None) or getattr(rej, "message", None) or "rejected"
        return False, str(code)
    except Exception as e:
        print("pre-flight failed (proceeding):", repr(e), flush=True)
        return True, None


WS_BOOKS = {}


def ws_book(sym, max_age=15.0):
    """The last WS book snapshot for sym, if fresh enough to trust."""
    rec = WS_BOOKS.get(sym)
    if rec and (time.monotonic() - rec[1]) <= max_age:
        return rec[0]
    return None


async def book(c, sym, depth=10, max_age=15.0):
    """Orderbook for one pair: the WS-fed snapshot when fresh, REST otherwise."""
    cached = ws_book(sym, max_age)
    if cached is not None:
        return cached
    bk = await c.orderbook.get(symbol=sym, depth=depth)
    WS_BOOKS[sym] = (bk, time.monotonic())
    return bk


async def equity_report(c):
    """Daily equity line from real balances x fresh mids; keeps its own snapshot."""
    try:
        total = Decimal(0)
        parts = []
        for row in rest_balances():
            sym0 = row.get("symbol")
            amt = Decimal(row.get("trading") or 0)
            if amt <= 0:
                continue
            if sym0 == QUOTE:
                total += amt
                continue
            psym = sym0 + "-" + QUOTE
            try:
                bk = await book(c, psym, depth=1)
                if bk.bids and bk.asks:
                    mid = (pdec(bk.bids[0].price) + pdec(bk.asks[0].price)) / 2
                    total += amt * mid
                    parts.append(f"{sym0} {money(amt * mid)}")
            except Exception:
                print("equity skip", sym0, flush=True)
        prev = None
        try:
            with open(state_file("equity_state.json"), encoding="utf-8") as f:
                prev = Decimal(json.load(f)["equity"])
        except (OSError, ValueError, KeyError):
            pass
        delta = f" | 24h {total - prev:+.2f} USDT" if prev is not None else " | no prior snapshot"
        notify("📈", "EQUITY",
               f"total {money(total)} USDT{delta}",
               " | ".join(parts) or "all in USDT")
        try:
            with open(state_file("equity_state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"equity": str(total)}, f)
        except OSError:
            pass
    except Exception as e:
        print("equity report failed:", repr(e), flush=True)


async def run_books_ws(c, ids, stop):
    """Feed WS_BOOKS continuously, one subscription per pair; REST stays the
    fallback when a stream goes quiet. ids maps symbol -> symbolId."""
    async def keep_one(sym, symbol_id):
        while not stop.is_set():
            try:
                sub = await c.orderbook.subscribe(symbol=sym,
                                                  symbol_id=symbol_id, depth=10)
                async for data in sub:
                    if getattr(data, "available", True):
                        WS_BOOKS[sym] = (data, time.monotonic())
                    if stop.is_set():
                        break
            except Exception as e:
                print("ws book feed", sym, "down:", repr(e), flush=True)
            try:
                await asyncio.wait_for(stop.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
    await asyncio.gather(*(keep_one(s, i) for s, i in ids.items()))


async def scan_books(c, pairs):
    """Every USDT pair with a two-sided book: (gap_pct, sym, bid, ask, bid$, ask$)."""
    rows = []
    for sym in pairs:
        try:
            bk = await book(c, sym, depth=1)
            if not bk.bids or not bk.asks:
                continue
            bid, ask = pdec(bk.bids[0].price), pdec(bk.asks[0].price)
            if bid <= 0 or ask <= bid:
                continue
            gap = (ask - bid) / ((ask + bid) / 2) * 100
            bid_usd = pdec(bk.bids[0].qty) * bid
            ask_usd = pdec(bk.asks[0].qty) * ask
            rows.append((gap, sym, bid, ask, bid_usd, ask_usd))
        except Exception as e:
            print("scan skip", sym, repr(e), flush=True)
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


async def place_sell(c, pos, meta):
    sym = pos["symbol"]
    base = sym.split("-")[0]
    tick, step, min_not = meta[sym]["tick"], meta[sym]["step"], meta[sym]["min_notional"]
    bk = await book(c, sym, depth=10)
    if not bk.asks:
        return False
    best_ask = pdec(bk.asks[0].price)
    best_bid = pdec(bk.bids[0].price) if bk.bids else Decimal(0)
    spread = ((best_ask - best_bid) / ((best_ask + best_bid) / 2) * 100
              if best_bid else Decimal(0))
    basis = pos.get("buy_px") or Decimal(0)
    if pos.get("bail"):
        target = best_ask - tick
    elif pos.get("join_ask"):
        target = max(best_ask, basis) if basis > 0 else best_ask
    elif basis > 0:
        target = max(best_ask - tick, basis)
    else:
        target = best_ask - tick
    q = floor_step(pos["qty"], step)
    held = None
    try:
        for row in rest_balances():
            if row.get("symbol") == base:
                held = floor_step(Decimal(row.get("trading") or 0), step)
                q = min(q, floor_step(Decimal(row.get("available") or 0), step))
                break
    except Exception:
        held = None
    if q <= 0 or q * best_ask < min_not:
        if held and held * best_ask >= min_not:
            last = pos.get("orphan_noted")
            if last is not None and time.time() - last < 3600:
                return False
            pos["orphan_noted"] = time.time()
            try:
                await c.orders.cancel_all(symbol=sym,
                                          request_id="gap1-ca-" + uuid.uuid4().hex[:10])
                pos["skip_noted"] = False
                notify("⚠️", "ORPHAN CLEARED",
                       f"{sym} | {num(held)} reserved by an orphan order | retrying")
            except Exception as e:
                print("orphan clear failed:", repr(e), flush=True)
        elif not pos.get("skip_noted"):
            pos["too_small"] = True
            pos["skip_noted"] = True
            notify("⚠️", "SELL SKIPPED",
                   f"{sym} | below pair minimums | qty {num(q)}")
        return False
    po = not pos.get("no_postonly")
    for price, post in ((target, po), (best_ask, False)):
        if price <= 0:
            continue
        cid, err = await place(c, sym, "sell", q, price, post_only=post)
        if cid:
            pos["sell_cid"], pos["ask_px"] = cid, price
            pos["better"], pos["ask_at"] = {}, time.time()
            pnl_txt = (f"expected {(price - basis) * q * Decimal('0.998'):+.2f} USDT"
                       if basis > 0 else "expected P&L unknown (no basis)")
            notify("🔵", "ASK",
                   f"{sym} | gap {spread:.3f}% | {num(q)} @ {num(price)} | "
                   f"bought @ {num(basis) if basis else '?'}",
                   f"{pnl_txt} | {wallet(base)}")
            return True
    if err and "insufficient" in err.lower():
        try:
            await c.orders.cancel_all(symbol=sym,
                                      request_id="gap1-ca-" + uuid.uuid4().hex[:10])
            pos["skip_noted"] = False
            notify("⚠️", "INSUFFICIENT FUNDS",
                   f"{sym} | orphaned orders cleared | retrying")
        except Exception as e:
            print("orphan clear failed:", repr(e), flush=True)
    elif pos.get("last_err") != err:
        pos["last_err"] = err
        notify("⚠️", "ASK REJECTED", f"{sym} | {err}")
    return False


def rest_asset_balance(base):
    """(trading, available) for one asset via signed REST. Raises on failure."""
    for row in rest_balances():
        if row.get("symbol") == base:
            return (Decimal(row.get("trading") or 0),
                    Decimal(row.get("available") or 0))
    return Decimal(0), Decimal(0)


async def base_balance(c, sym, m):
    """Total base held (trading) via REST. Raises on failure."""
    base = sym.split("-")[0]
    held, _ = rest_asset_balance(base)
    return held


async def main():
    global ACCOUNT
    last_wall, last_mono = time.time(), time.monotonic()
    c = None
    for attempt in range(12):
        try:
            if not ACCOUNT:
                ACCOUNT = str((rest_me() or {}).get("accountId") or "")
                if not ACCOUNT:
                    raise RuntimeError("the exchange returned no account id")
                print("account:", ACCOUNT, flush=True)
            c = AsyncPolyester(environment=POLYESTER_TESTNET_ENVIRONMENT,
                               api_key_id=KEY,
                               api_private_key=os.environ["POLYESTER_ED25519_SEED"],
                               default_account_id=ACCOUNT)
            await c.wait_for_catalogs()
            break
        except Exception as e:
            print(f"startup retry {attempt + 1}/12: {repr(e)}", flush=True)
            if c is not None:
                try:
                    await c.aclose()
                except Exception:
                    pass
                c = None
            await asyncio.sleep(5)
    if c is None:
        raise SystemExit("API unreachable after 12 retries; supervisor will restart us")
    try:
        await c.wait_for_catalogs()
        cfg = (await c.market_data.get_spot_config()).raw["pairs"]
        pairs = {p["symbol"]: p for p in cfg
                 if p.get("quoteAsset") == QUOTE
                 and p.get("status") in ("PAIR_STATUS_ENABLED", "PAIR_STATUS_POST_ONLY")}
        meta = {s: {"tick": Decimal(p["tickSize"]), "step": Decimal(p["stepSize"]),
                    "min_notional": Decimal(p.get("minNotionalQuote") or "0"),
                    "min_qty": Decimal(p.get("minQtyBase") or "0"),
                    "scale": int(p.get("baseQuantityScale") or 8)}
                for s, p in pairs.items()}
        ws_stop = asyncio.Event()
        ws_ids = {s: p["symbolId"] for s, p in pairs.items() if "symbolId" in p}
        ws_task = asyncio.create_task(run_books_ws(c, ws_ids, ws_stop))

        async def resync(tag):
            """Cancel our stale orders, re-read balances.

            Returns (held, ok). ok=False means the exchange was unreachable, so
            the caller must keep its position instead of forgetting it.
            """
            held, ok = [], False
            for attempt in range(24):
                try:
                    held = []
                    for row in rest_balances():
                        sym = row.get("symbol", "")
                        if sym != QUOTE and Decimal(row.get("trading") or 0) > 0 \
                                and (sym + "-" + QUOTE) in pairs:
                            held.append((sym + "-" + QUOTE, Decimal(row["trading"])))
                    ok = True
                    break
                except Exception as e:
                    print(f"balances read retry {attempt + 1}/24: {repr(e)}", flush=True)
                    await asyncio.sleep(5)
            if not ok:
                notify("⚠️", "RESYNC FAILED",
                       "balances unreadable | keeping the position")
                return [], False

            left = []
            for attempt in range(4):
                try:
                    ours = [o for o in rest_open_orders().get("orders", [])
                            if str(o.get("clientOrderId", "")).startswith("gap1-")]
                    for o in ours:
                        await c.orders.cancel(key=ClientOrderId(o["clientOrderId"]),
                                              symbol=o.get("symbol"))
                    await asyncio.sleep(3)
                    left = [o for o in rest_open_orders().get("orders", [])
                            if str(o.get("clientOrderId", "")).startswith("gap1-")]
                    if not left:
                        break
                except Exception as e:
                    print(f"sweep retry {attempt + 1}/4:", repr(e), flush=True)
                    await asyncio.sleep(3)
            if left:
                notify("⚠️", "ORDERS STILL OPEN",
                       f"{len(left)} of our orders survived the sweep")
            return held, True

        adopt, boot_ok = await resync("gap1-boot")
        await refresh_fees(c)

        notify("🤖", "GAP HUNTER ONLINE",
               f"gap ≥ {GAP_MIN_PCT}% | depth ≥ {money(DEPTH_MIN_USD)} USDT | "
               + (", ".join(f"{s} {num(q)}" for s, q in adopt) or "nothing to adopt"),
               wallet())

        state = "idle"
        adopt_q = list(adopt)
        pos = {}
        carry = {}
        dust = {}
        pending_resync = not boot_ok
        next_scan = 0.0
        next_fallback = 0.0
        next_block_alert = 0.0
        sat = {"n": 0}
        next_fees = 0.0
        fees_seen = False
        last_deadman = 0.0
        last_equity_day = ""

        async def refresh_volume():
            """Pull the canonical 30d VIP volume the site shows."""
            try:
                st = await c.vip.get_vip_status()
                return Decimal(st.settled_volume_30d_usd)
            except Exception as e:
                print("volume refresh failed:", repr(e), flush=True)
                return None

        vol, next_10k = await refresh_volume(), None
        if vol is not None:
            next_10k = (int(vol // 10000) + 1) * 10000
            notify("📊", "VIP VOLUME",
                   f"30d settled volume {money(vol)} USDT | next milestone {next_10k:,}",
                   wallet())
        else:
            vol, next_10k = Decimal(0), 10000

        def beat(now):
            """Heartbeat for the external keepalive: a stale file means we are wedged."""
            try:
                with open(HEARTBEAT, "w", encoding="utf-8") as f:
                    f.write(str(int(now)))
            except OSError:
                pass

        async def cycle_done(sym, sell_px):
            """Close a cycle from balance truth; alert with balances + volume."""
            nonlocal vol, next_10k, pos, state
            await cancel_tp_trigger(c, pos)
            await cancel_stale_symbol_orders(c, sym)
            base_sym = sym.split("-")[0]
            base_held, _ = rest_asset_balance(base_sym)
            qty0 = pos.get("qty0") or pos.get("qty") or Decimal(0)
            sold = max(Decimal(0), qty0 - base_held)
            sell_px = sell_px or pos.get("last_avg") or pos.get("ask_px") or Decimal(0)
            basis = pos.get("buy_px") or Decimal(0)
            sell_usd = sell_px * sold
            buy_usd = basis * sold
            mk = maker_fee_pct(FEES, sym)
            fees = round_trip_fees(buy_usd, sell_usd, mk)
            if pos.get("buy_fee_pct") and pos["buy_fee_pct"] != mk:
                fees += buy_usd * (pos["buy_fee_pct"] - mk) / 100
            net = sell_usd - buy_usd - fees
            if sell_px > 0:
                net_txt = f"net {net:+.2f} USDT"
            else:
                net_txt = "P&L unknown (fill price not observed)"
            notify("✅", "CYCLE DONE",
                   f"{sym} | sold {num(sold)} @ "
                   f"{num(sell_px) if sell_px else '?'} | fee {money(fees)} USDT",
                   f"{net_txt} | {wallet(base_sym)}")
            buy_side = basis if basis > 0 else sell_px
            fresh = await refresh_volume()
            if fresh is not None:
                vol = fresh
            while vol is not None and vol >= next_10k:
                notify("🏆", "MILESTONE",
                       f"{next_10k:,} USDT VIP 30d volume | {money(vol)} USDT",
                       f"{money(vol - next_10k)} USDT over the line | {wallet()}")
                next_10k += 10000
            pos, state = {}, "idle"

        while True:
            now = time.time()
            try:
                mono = time.monotonic()
                gap = frozen_seconds(now, mono, last_wall, last_mono)
                stall = now - last_wall
                last_wall, last_mono = now, mono
                beat(now)
                if (gap > FREEZE_SECONDS or stall > STALL_SECONDS) and not pending_resync:
                    pending_resync = True
                    notify("❄️", "FREEZE DETECTED",
                           f"sandbox paused {max(gap, stall) / 60:.1f} min "
                           f"| re-syncing orders and balances",
                           wallet())
                if pending_resync:
                    held, ok = await resync(f"gap1-freeze-{int(now)}")
                    if ok:
                        pending_resync = False
                        adopt_q = list(held)
                        pos, state = {}, "idle"
                        next_scan = 0.0
                    now = time.time()
                elif state == "idle" and adopt_q:
                    sym, qty = adopt_q.pop(0)
                    basis, qty0 = carry.get(sym, (None, None))
                    pos = {"symbol": sym, "qty": qty, "qty0": qty0 or qty,
                           "buy_px": basis if basis is not None
                           else COST_BASIS.get(sym, Decimal(0)),
                           "sell_cid": None, "better": {}, "since": now}
                    state = "selling"
                    carry[sym] = (pos["buy_px"], pos["qty0"])
                    if sym in meta:
                        await place_tp_trigger(c, pos, meta[sym])
                    notify("♻️", "ADOPT",
                           f"{sym} {num(qty)} | basis "
                           f"{num(pos['buy_px']) if pos['buy_px'] else 'unknown'}",
                           wallet(sym.split("-")[0]))

                elif state == "idle":
                    if now >= next_scan:
                        next_scan = now + SCAN_SECONDS
                        rows = await scan_books(c, pairs)
                        ours = [o for o in rest_open_orders().get("orders", [])
                                if str(o.get("clientOrderId", "")).startswith("gap1-")]
                        held = []
                        for r in rest_balances():
                            psym = r.get("symbol", "") + "-" + QUOTE
                            qty = Decimal(r.get("trading") or 0)
                            if r.get("symbol") == QUOTE or qty <= 0:
                                continue
                            if psym not in pairs or dust.get(psym) == qty:
                                continue
                            held.append(r)
                        if held and not adopt_q:
                            adopt_q.extend((r["symbol"] + "-" + QUOTE,
                                            Decimal(r["trading"])) for r in held)
                            notify("♻️", "ADOPT QUEUED",
                                   f"{len(held)} position(s) to sell back | "
                                   + ", ".join(f"{r['symbol']} {num(r['trading'])}"
                                               for r in held),
                                   wallet())
                        picked, tag = None, ""
                        gaps = [r for r in rows
                                if r[0] >= GAP_MIN_PCT
                                and min(r[4], r[5]) >= DEPTH_MIN_USD]
                        if gaps:
                            picked, tag = gaps[0], "GAP"
                        elif now >= next_fallback:
                            fb = [r for r in rows
                                  if r[0] >= FALLBACK_MIN_GAP_PCT
                                  and min(r[4], r[5]) >= FALLBACK_DEPTH_MIN_USD]
                            if FALLBACK_SYMBOL:
                                fb = [r for r in fb if r[1] == FALLBACK_SYMBOL]
                            if fb:
                                if FALLBACK_SYMBOL:
                                    picked, tag = fb[0], "VOLUME"
                                else:
                                    picked, tag = await pick_best_liquid(c, fb), "VOLUME"
                                next_fallback = now + FALLBACK_COOLDOWN
                        if picked:
                            gap, sym, bid, ask, bid_usd, ask_usd = picked
                            hot, chg = False, Decimal(0)
                            if ours or held:
                                try:
                                    cr = await c.market_data.get_candles(
                                        symbol=sym, timeframe="1h", limit=25)
                                    cs = list(cr.candles)
                                    if len(cs) >= 2 and Decimal(cs[0].close) > 0:
                                        chg = abs((Decimal(cs[-1].close)
                                                   / Decimal(cs[0].close) - 1) * 100)
                                        hot = chg > 20
                                except Exception as e:
                                    print("hot check failed:", repr(e), flush=True)
                            if (ours or held) and not hot:
                                if now >= next_block_alert:
                                    next_block_alert = now + 600
                                    notify("⚠️", "ONE-POSITION",
                                           f"{sym} | {len(ours)} open order(s) | "
                                           f"{len(held)} held position(s) | 24h {chg:.1f}%",
                                           f"not starting a new trade | {wallet()}")
                                picked = None
                            elif hot and (ours or held):
                                try:
                                    await c.orders.cancel_all(
                                        request_id="gap1-hot-" + uuid.uuid4().hex[:10])
                                except Exception:
                                    pass
                                adopt_q.extend((r["symbol"] + "-" + QUOTE,
                                                Decimal(r["trading"])) for r in held)
                                notify("🔥", "HOT OVERRIDE",
                                       f"{sym} | 24h {chg:.1f}% > 20% | "
                                       f"{len(ours)} order(s) | {len(held)} held",
                                       "starting anyway | queued for adoption")
                                picked = None
                        if picked:
                            gap, sym, bid, ask, bid_usd, ask_usd = picked
                            m = meta[sym]
                            if tag == "GAP":
                                sat["n"] = 0
                                penny, take = False, False
                            else:
                                penny = FALLBACK_PENNY or sat["n"] >= SAT_PENNY_AFTER
                                take = sat["n"] >= SAT_TAKER_AFTER
                            price = entry_price(tag, bid, ask, m, penny, take)
                            buy_fee = (taker_fee_pct(FEES, sym) if take
                                       else maker_fee_pct(FEES, sym))
                            rt = maker_fee_pct(FEES, sym) + buy_fee
                            usdt = next((Decimal(r["available"]) for r in rest_balances()
                                         if r.get("symbol") == QUOTE), Decimal(0))
                            q = floor_step(usdt / (price * FEE_BUFFER), m["step"])
                            if q > 0 and q >= m["min_qty"] and q * price >= m["min_notional"]:
                                pv_ok, pv_why = await preflight_buy(c, sym, "buy", q, price)
                                cid = None
                                if not pv_ok:
                                    if now >= next_block_alert:
                                        next_block_alert = now + 600
                                        notify("⚠️", "SKIPPED",
                                               f"{sym} | {tag} | pre-flight: {pv_why}",
                                               wallet(sym.split("-")[0]))
                                else:
                                    cid, err = await place(c, sym, "buy", q, price)
                                if cid:
                                    pos = {"symbol": sym, "buy_cid": cid, "buy_px": price,
                                           "buy_at": now, "qty": None,
                                           "qty_target": q, "tag": tag,
                                           "buy_fee_pct": buy_fee,
                                           "sell_cid": None, "better": {},
                                           "join_ask": tag == "VOLUME"}
                                    state = "buying"
                                    if tag == "GAP":
                                        notify("🟢", "BUY",
                                               f"{sym} | gap {gap:.2f}% "
                                               f"(net +{gap - rt:.2f}%) | "
                                               f"{num(q)} @ {num(price)} | "
                                               f"depth {money(bid_usd)}/{money(ask_usd)}",
                                               f"expected +{q * price * (gap - rt) / 100:.2f}"
                                               f" USDT | {wallet(sym.split('-')[0])}")
                                    else:
                                        loss = q * price * (rt - gap) / 100
                                        stage = ("taker ask" if take else
                                                 "penny bid" if penny else "bid")
                                        notify("🟡", "VOLUME BUY",
                                               f"{sym} | gap {gap:.3f}% "
                                               f"(fee {rt}%) | "
                                               f"{num(q)} @ {num(price)} | "
                                               f"volume {money(q * price * 2)} USDT",
                                               f"{stage} | expected -{loss:.2f} USDT "
                                               f"| {wallet(sym.split('-')[0])}")
                            else:
                                notify("⚠️", "SKIPPED",
                                       f"{sym} | {tag} | below pair minimums")

                elif state == "buying":
                    sym = pos["symbol"]
                    m = meta[sym]
                    st, cum, avg = await order_state(c, sym, pos["buy_cid"],
                                                     m["scale"])
                    if st in TERMINAL:
                        held_qty = floor_step(await base_balance(c, sym, m), m["step"])
                        qty = held_qty or floor_step(cum, m["step"])
                        if st == "filled" or cum > 0:
                            pos["qty"] = qty
                            pos["qty0"] = qty
                            pos["buy_px"] = avg if avg > 0 else pos["buy_px"]
                            pos["since"] = now
                            carry[sym] = (pos["buy_px"], pos["qty0"])
                            sat["n"] = 0
                            notify("✅", "BUY FILLED",
                                   f"{sym} | {num(pos['qty'])} @ {num(pos['buy_px'])}",
                                   wallet(sym.split("-")[0]))
                            await place_tp_trigger(c, pos, m)
                            state = "selling"
                        else:
                            pos["qty"] = held_qty
                            notify("⚠️", "BUY UNFILLED",
                                   f"{sym} | order ended {st}",
                                   wallet(sym.split("-")[0]))
                            state = "idle"
                    else:
                        await chase_buy(c, pos, m, now)
                        tmo = buy_timeout_for(pos.get("tag", "GAP"), sat["n"])
                        if now - pos["buy_at"] > tmo:
                            try:
                                await c.orders.cancel(key=ClientOrderId(pos["buy_cid"]),
                                                      symbol=sym)
                            except Exception:
                                pass
                            st, cum, avg = await order_state(c, sym, pos["buy_cid"],
                                                             m["scale"])
                            notify("⚠️", "BUY TIMEOUT",
                                   f"{sym} | no fill after {tmo:.0f}s "
                                   f"| cancelled",
                                   f"sat {sat['n']} | {wallet(sym.split('-')[0])}")
                            pos["qty"] = floor_step(await base_balance(c, sym, m), m["step"])
                            if pos["qty"] > 0 or cum > 0:
                                pos["qty"] = pos["qty"] or floor_step(cum, m["step"])
                                pos["qty0"] = pos["qty"]
                                pos["buy_px"] = avg if avg > 0 else pos["buy_px"]
                                pos["since"] = now
                                carry[sym] = (pos["buy_px"], pos["qty0"])
                                sat["n"] = 0
                                notify("✅", "BUY FILLED",
                                       f"{sym} | {num(pos['qty'])} @ {num(pos['buy_px'])}",
                                       wallet(sym.split("-")[0]))
                                await place_tp_trigger(c, pos, m)
                                state = "selling"
                            else:
                                state = "idle"
                                if pos.get("tag") == "VOLUME":
                                    sat["n"] += 1

                elif state == "selling":
                    sym = pos["symbol"]
                    m = meta[sym]
                    held, avail = rest_asset_balance(sym.split("-")[0])
                    price_est = pos.get("last_avg") or pos.get("ask_px") or Decimal(0)
                    if held > 0:
                        pos["seen"] = True
                    if held <= 0 and not pos.get("seen"):
                        if now - pos.get("since", 0) < BALANCE_GRACE:
                            pass
                        else:
                            await cycle_done(sym, price_est)
                    elif held < m["min_qty"] or (price_est > 0 and
                                               held * price_est < m["min_notional"]):
                        if held > 0:
                            dust[sym] = held
                        await cycle_done(sym, price_est)
                    elif pos["sell_cid"] is None:
                        pos["qty"] = min(pos.get("qty") or held, held)
                        placed = await place_sell(c, pos, meta)
                        if placed:
                            pos["too_small"] = False
                        elif pos.get("too_small"):
                            dust[sym] = held
                            notify("⚠️", "DUST",
                                   f"{sym} | {num(held)} left | too small to sell",
                                   wallet(sym.split("-")[0]))
                            pos, state = {}, "idle"
                    else:
                        st, cum, avg = await order_state(c, sym, pos["sell_cid"], m["scale"])
                        if avg and avg > 0:
                            pos["last_avg"] = avg
                        if st in TERMINAL:
                            if st == "rejected" and cum <= 0:
                                pos["no_postonly"] = True
                            pos["sell_cid"] = None
                            pos["canceling"] = False
                        elif pos.get("canceling"):
                            if any(o.get("clientOrderId") == pos["sell_cid"]
                                   for o in rest_open_orders().get("orders", [])):
                                try:
                                    await c.orders.cancel(key=ClientOrderId(pos["sell_cid"]),
                                                          symbol=sym)
                                except Exception:
                                    pass
                            else:
                                pos["sell_cid"] = None
                                pos["canceling"] = False
                        else:
                            bk = await book(c, sym, depth=10)
                            better = {pdec(a.price) for a in (bk.asks or [])
                                      if pdec(a.price) < pos["ask_px"]}
                            track = pos["better"]
                            for p in list(track):
                                if p not in better:
                                    del track[p]
                            for p in better:
                                track.setdefault(p, now)
                            aged = sum(1 for p, t0 in track.items()
                                       if now - t0 >= PERSIST_SECONDS)
                            if aged >= BETTER_ASKS_LIMIT:
                                asks2 = [pdec(a.price) for a in (bk.asks or [])
                                         if pdec(a.price) > 0]
                                new_px = min(asks2) - m["tick"] if asks2 else None
                                amended = False
                                if new_px is not None and new_px != pos["ask_px"]:
                                    try:
                                        await c.orders.modify(
                                            key=ClientOrderId(pos["sell_cid"]),
                                            symbol=sym, new_price=new_px)
                                        pos["ask_px"] = new_px
                                        pos["better"] = {}
                                        pos["bail"] = True
                                        notify("🔄", "ASK CHASE",
                                               f"{sym} | {aged} better ask(s) held "
                                               f"{PERSIST_SECONDS:.0f}s | "
                                               f"amending to {num(new_px)}",
                                               wallet(sym.split("-")[0]))
                                        amended = True
                                    except Exception as e:
                                        print("amend fell back to cancel:", repr(e),
                                              flush=True)
                                if not amended and new_px != pos["ask_px"]:
                                    try:
                                        await c.orders.cancel(key=ClientOrderId(pos["sell_cid"]),
                                                              symbol=sym)
                                    except Exception:
                                        pass
                                    notify("⚠️", "ASK BAIL-OUT",
                                           f"{sym} | {aged} better asks held "
                                           f"{PERSIST_SECONDS:.0f}s | re-asking under them",
                                           wallet(sym.split("-")[0]))
                                    pos["canceling"] = True
                                    pos["bail"] = True
                if now >= next_fees:
                    next_fees = now + FEES_REFRESH_SECONDS
                    await refresh_fees(c)
                    if not fees_seen and FEES:
                        fees_seen = True
                        s0 = next(iter(FEES))
                        notify("💱", "FEES LIVE",
                               f"VIP fee {FEES[s0][0]}% maker / "
                               f"{FEES[s0][1]}% taker",
                               f"gap floor {GAP_MIN_PCT}% | {len(FEES)} pairs "
                               f"| {wallet()}")
                if DEADMAN_TIMEOUT > 0 and now - last_deadman >= DEADMAN_REFRESH:
                    last_deadman = now
                    await deadman_rearm(c)
                today = time.strftime("%Y-%m-%d", time.gmtime())
                if today != last_equity_day:
                    last_equity_day = today
                    await equity_report(c)
            except Exception as e:
                print("loop error:", repr(e), flush=True)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        ws_stop.set()
        try:
            await asyncio.wait_for(asyncio.gather(ws_task), 3)
        except Exception:
            ws_task.cancel()
        await c.aclose()


def cli():
    if sys.argv[1:2] == ["setup"]:
        if __package__:
            from .setup import cli as setup_cli
        else:
            from setup import cli as setup_cli
        setup_cli()
        return
    missing = [k for k in ("POLYESTER_API_KEY_ID", "POLYESTER_ED25519_SEED")
               if not os.environ.get(k, "").strip()]
    if missing:
        print("missing credentials: " + ", ".join(missing)
              + " (put them in .env next to the bot or in the environment)")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
