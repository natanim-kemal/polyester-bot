"""Telegram slash commands, answered on demand from cached bot state.

The listener runs in its own thread so it still replies when the trading loop
is busy or wedged. It answers only TELEGRAM_CHAT_ID; silence means the bot
process or host is gone. Commands never place orders.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal

VOLUME = {}
RUNTIME = {}
SRC = None
COMMANDS = {}

_started = False
_offset = None


def command(name, usage):
    """Register a handler; each handler returns the reply text."""
    def register(fn):
        COMMANDS[name] = (usage, fn)
        return fn
    return register


def age_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def health(age):
    """Loop freshness for /status: a minute late is slow, five is stuck."""
    if age < 60:
        return "ok"
    if age < 300:
        return "slow"
    return "stuck"


def parse(text):
    """'/volume@Bot now' -> ('volume', 'now'); anything else -> (None, '')."""
    if not text or not text.startswith("/"):
        return None, ""
    head, _, rest = text.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if not name:
        return None, ""
    return name, rest.strip()


def reply(name):
    item = COMMANDS.get(name)
    if item is None:
        return "unknown command" + (" | /help" if "help" in COMMANDS else "")
    try:
        return item[1]()
    except Exception as e:
        print("command failed:", name, repr(e), flush=True)
        return f"command failed: {e}"


@command("volume", "/volume - VIP 30d volume, next milestone, reading age")
def volume():
    v = VOLUME
    if not v.get("vol"):
        return "📊 VIP VOLUME\nno volume reading yet"
    age = age_text(time.time() - v["checked_at"]) if v.get("checked_at") else "?"
    as_of = v.get("as_of")
    stamp = as_of.strftime("%H:%M UTC") if hasattr(as_of, "strftime") else "?"
    tail = f"API {stamp} | checked {age} ago"
    if v.get("tier") is not None:
        tail += f" | tier {v['tier']}"
    return "\n".join([
        "📊 VIP VOLUME",
        f"30d settled {SRC.vol_money(v['vol'])} USDT | "
        f"next {SRC.vol_next(v['vol'], v.get('next')):,}",
        tail,
    ])


@command("chase", "/chase - every tracked chase score and park cooldown")
def chase():
    now = time.time()
    rows = []
    for sym in SRC.CHASE_SCORES:
        score = SRC.chase_score(sym, now)
        if score <= 0:
            continue
        left = SRC.chase_cooldown(sym, now)
        rows.append((score, sym, f"parked {age_text(left)}" if left > 0 else "clear"))
    rows.sort(reverse=True)
    parked = sum(1 for _, _, label in rows if label.startswith("parked"))
    head = f"🎯 CHASE | {len(rows)} tracked | {parked} parked"
    if not rows:
        return head + "\nno chase events"
    return "\n".join([head] + [f"{sym} {score:.2f} | {label}"
                               for score, sym, label in rows])


@command("status", "/status - heartbeat, state, open orders, last cycle")
def status():
    now = time.time()
    tick = RUNTIME.get("tick")
    if not tick:
        return "💓 STATUS\nno loop tick yet"
    age = max(0.0, now - tick)
    lines = [
        "💓 STATUS",
        f"heartbeat {age_text(age)} ago | {health(age)} | "
        f"uptime {age_text(now - RUNTIME.get('started', now))}",
    ]
    state = RUNTIME.get("state") or "?"
    sym = RUNTIME.get("symbol")
    qty = RUNTIME.get("qty")
    where = state + (f" {sym}" if sym else "")
    if qty:
        where += f" {SRC.num(qty)}"
    try:
        orders = len([o for o in SRC.rest_open_orders().get("orders", [])
                      if str(o.get("clientOrderId", "")).startswith("gap1-")])
        open_txt = str(orders)
    except Exception as e:
        print("status order read failed:", repr(e), flush=True)
        open_txt = "?"
    lines.append(f"state {where} | open orders {open_txt}")

    def stamp(t):
        if not t:
            return "none"
        return (f"{time.strftime('%H:%M UTC', time.gmtime(t))} "
                f"({age_text(now - t)} ago)")

    lines.append(f"last cycle {stamp(RUNTIME.get('last_cycle'))} | "
                 f"last fill {stamp(RUNTIME.get('last_fill'))}")
    return "\n".join(lines)


@command("balance", "/balance - USDT and every held asset marked at mid")
def balance():
    try:
        rows = SRC.rest_balances()
    except Exception as e:
        return f"💰 BALANCE\nbalance read failed: {e}"
    quote = SRC.QUOTE
    usdt = Decimal(0)
    held = []
    for row in rows:
        sym = row.get("symbol")
        amt = Decimal(row.get("trading") or 0)
        if amt <= 0:
            continue
        if sym == quote:
            usdt = amt
            continue
        pair = sym + "-" + quote
        bk = SRC.ws_book(pair, 15.0)
        mid = None
        if bk is not None and bk.bids and bk.asks:
            mid = (SRC.pdec(bk.bids[0].price) + SRC.pdec(bk.asks[0].price)) / 2
        held.append((sym, amt, mid))
    lines = ["💰 BALANCE", f"{quote} {SRC.money(usdt)}"]
    total, unmarked = usdt, 0
    for sym, amt, mid in held:
        if mid is None:
            lines.append(f"{sym} {SRC.num(amt)} | no mark")
            unmarked += 1
        else:
            value = amt * mid
            total += value
            lines.append(f"{sym} {SRC.num(amt)} @ {SRC.num(mid)} = "
                         f"{SRC.money(value)}")
    tail = f"total {SRC.money(total)} {quote}"
    if unmarked:
        tail += f" | {unmarked} unmarked"
    lines.append(tail)
    return "\n".join(lines)


def _token_chat():
    return (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def _api(method, data=None):
    tok, _ = _token_chat()
    req = urllib.request.Request(
        "https://api.telegram.org/bot" + tok + "/" + method,
        data=urllib.parse.urlencode(data).encode() if data else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _send(text):
    tok, chat = _token_chat()
    if not tok or not chat:
        return
    try:
        _api("sendMessage", {"chat_id": chat, "text": text})
    except Exception as e:
        print("telegram command reply failed:", repr(e), flush=True)


def _allowed(msg):
    return str((msg.get("chat") or {}).get("id", "")) == _token_chat()[1]


def _handle(update):
    msg = update.get("message") or {}
    if not _allowed(msg):
        return
    name, _ = parse(msg.get("text") or "")
    if name:
        _send(reply(name))


def _drain(offset):
    """Skip commands sent while the bot was down; start from now."""
    while True:
        data = _api("getUpdates", {"offset": offset, "timeout": 0, "limit": 100})
        updates = data.get("result") or []
        if not updates:
            return offset
        offset = updates[-1]["update_id"] + 1


def _poll():
    global _offset
    try:
        _offset = _drain(-1 if _offset is None else _offset)
    except Exception as e:
        print("telegram command drain failed:", repr(e), flush=True)
        _offset = _offset or 0
    while True:
        try:
            data = _api("getUpdates", {"offset": _offset, "timeout": 25,
                                       "limit": 100})
            for update in data.get("result") or []:
                _offset = update["update_id"] + 1
                _handle(update)
        except Exception as e:
            print("telegram command poll failed:", repr(e), flush=True)
            time.sleep(5)


def start(src):
    """Start the listener thread once; no-op without token and chat id."""
    global SRC, _started
    if _started:
        return
    tok, chat = _token_chat()
    if not tok or not chat:
        print("telegram commands off: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset",
              flush=True)
        return
    SRC = src
    _started = True
    threading.Thread(target=_poll, name="telegram-commands", daemon=True).start()
    print("telegram commands on", flush=True)
