"""The bot must never lose a position to a stale balance read.

The exchange fills the BUY, but /v1/balances can still say zero for a minute.
The old code trusted the balance, called the position "unfilled" and dropped
it: the one-position rule then blocked every new trade and the bot sat there
forever. Three things keep that from happening:

  1. a filled order wins over a lagging balance read,
  2. the sell side waits out the lag instead of calling the cycle done,
  3. an idle bot re-adopts balances it is not managing.

Usage:  python tests/test_stuck.py [path-to-monitor.py]
Exit 0 = pass, Exit 1 = fail
"""

import ast
import asyncio
import os
import sys
import textwrap
import time
from decimal import Decimal, ROUND_FLOOR

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "bot", "monitor.py")
src = open(SRC, encoding="utf-8").read()
lines = src.splitlines()
tree = ast.parse(src)

ok = True


def check(label, good, detail=""):
    global ok
    ok = ok and good
    print("%-6s %s%s" % ("PASS" if good else "FAIL", label,
                         (" -> " + detail) if detail else ""))


def seg(nodes):
    return textwrap.dedent("\n".join(lines[nodes[0].lineno - 1:nodes[-1].end_lineno]))


def top_func(name):
    return next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def state_branch(name):
    main_node = top_func("main")
    for node in ast.walk(main_node):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left, comps = node.test.left, node.test.comparators
            if (isinstance(left, ast.Name) and left.id == "state" and len(comps) == 1
                    and isinstance(comps[0], ast.Constant) and comps[0].value == name):
                return node
    raise AssertionError("branch %r not found" % name)


alerts = []
cycle_done_calls = []


def fake_notify(emoji, action, *lines):
    alerts.append(action + " " + " ".join(lines))


tp_placed = []


async def fake_place_tp_trigger(c, pos, m):
    tp_placed.append(pos["symbol"])


async def fake_pick_best_liquid(c, fb, top=4):
    return fb[0]


async def fake_cycle_done(sym, px):
    cycle_done_calls.append(sym)


async def fake_order_state_filled(c, sym, cid, scale):
    return "filled", Decimal("17811.2"), Decimal("0.1673")


async def fake_order_state_open(c, sym, cid, scale):
    return "open", Decimal(0), Decimal(0)


async def fake_base_balance(c, sym, m):
    return Decimal(0)


async def fake_scan_books(c, pairs):
    return []


META = {"ARB-USDT": {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
                     "min_notional": Decimal("5"), "min_qty": Decimal("1"), "scale": 8}}


class FakeTG:
    RUNTIME = {}


def build_ns():
    ns = {
        "Decimal": Decimal, "ROUND_FLOOR": ROUND_FLOOR, "time": time,
        "notify": fake_notify, "cycle_done": fake_cycle_done,
        "alert": lambda text: alerts.append(text),
        "wallet": lambda base=None: "USDT 2.99",
        "tg": FakeTG,
        "order_state": fake_order_state_filled,
        "base_balance": fake_base_balance,
        "rest_asset_balance": lambda base: (Decimal(0), Decimal(0)),
        "rest_balances": lambda: [
            {"symbol": "ARB", "trading": "17811.2", "available": "17811.2"},
            {"symbol": "USDT", "trading": "2.99", "available": "2.99"}],
        "rest_open_orders": lambda: {"orders": []},
        "scan_books": fake_scan_books,
        "pairs": META,
        "carry": {}, "dust": {}, "adopt_q": [],
        "state": "buying", "now": 100.0,
        "sat": {"n": 0},
        "place_tp_trigger": fake_place_tp_trigger,
        "pick_best_liquid": fake_pick_best_liquid,
        "meta": META,
        "QUOTE": "USDT", "GAP_MIN_PCT": Decimal("0.21"),
        "DEPTH_MIN_USD": Decimal("50"), "SCAN_SECONDS": 10.0,
        "FALLBACK_MIN_GAP_PCT": Decimal("0.05"), "FALLBACK_DEPTH_MIN_USD": Decimal("1000"),
        "FALLBACK_SYMBOL": "", "FALLBACK_COOLDOWN": 60.0, "FALLBACK_PENNY": False,
        "BALANCE_GRACE": 120.0, "BUY_TIMEOUT_SECONDS": 180.0,
        "TERMINAL": {"filled", "canceled", "rejected", "expired"},
        "FEE_BUFFER": Decimal("1.0015"), "next_scan": 0.0, "next_fallback": 0.0,
        "next_block_alert": 0.0,
    }
    for fn in ("pdec", "floor_step", "money", "num", "notify"):
        exec(compile(seg([top_func(fn)]), "<%s>" % fn, "exec"), ns)
    return ns


async def run_buying(ns):
    pos = {"symbol": "ARB-USDT", "buy_cid": "cid-1", "buy_px": Decimal("0.1672"),
           "buy_at": 0.0, "qty": None, "sell_cid": None, "better": {}}
    ns["state"] = "buying"
    await ns["buying_step"](None, pos, META, 100.0)
    return pos


async def run_selling(ns, pos, now):
    ns["now"] = now
    await ns["selling_step"](None, pos, META, now)


ns = build_ns()
exec(compile("async def buying_step(c, pos, meta, now):\n    global state\n"
             + textwrap.indent(seg(state_branch("buying").body), "    "),
             "<buying_step>", "exec"), ns)
exec(compile("async def selling_step(c, pos, meta, now):\n"
             + textwrap.indent(seg(state_branch("selling").body), "    "),
             "<selling_step>", "exec"), ns)
exec(compile("async def idle_step(c, pairs):\n    global now, next_scan, next_fallback\n"
             + textwrap.indent(seg(state_branch("idle").body), "    "),
             "<idle_step>", "exec"), ns)

alerts.clear()
pos = asyncio.run(run_buying(ns))
check("a filled buy survives a zero balance read", ns["state"] == "selling",
      "state=" + ns["state"])
check("the filled quantity comes from the order", pos.get("qty") == Decimal("17811.2"),
      str(pos.get("qty")))
check("it is not called unfilled", not any("UNFILLED" in a for a in alerts),
      str(alerts))

cycle_done_calls.clear()
alerts.clear()
ns["rest_asset_balance"] = lambda base: (Decimal(0), Decimal(0))
pos = {"symbol": "ARB-USDT", "qty": Decimal("17811.2"), "qty0": Decimal("17811.2"),
       "buy_px": Decimal("0.1672"), "sell_cid": None, "better": {}, "since": 100.0}
asyncio.run(run_selling(ns, pos, 150.0))
check("the sell side waits for the balance to catch up", not cycle_done_calls,
      str(cycle_done_calls))
asyncio.run(run_selling(ns, pos, 400.0))
check("the cycle closes once the grace is over", cycle_done_calls == ["ARB-USDT"],
      str(cycle_done_calls))

ns["adopt_q"] = []
ns["now"] = 500.0
asyncio.run(ns["idle_step"](None, META))
check("an idle bot re-adopts balances it is not managing",
      ns["adopt_q"] == [("ARB-USDT", Decimal("17811.2"))], str(ns["adopt_q"]))

print()
print("PASS: a lagging balance read cannot strand the bot" if ok else "FAIL")
sys.exit(0 if ok else 1)
