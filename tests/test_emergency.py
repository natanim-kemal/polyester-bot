"""A 20% dislocation must trigger the emergency dump, not a quiet idle scan.

The bot tracks each pair's mid against a slow EMA. When the mid jumps 20%+
away, the idle loop must act immediately: sell whatever base it holds at the
best bid (taker), or buy the dislocated pair at the best ask when it holds
nothing. The flagged entry is cleared either way, and the normal scan waits.

Usage:  python tests/test_emergency.py [path-to-monitor.py]
Exit 0 = pass, Exit 1 = fail
"""

import ast
import asyncio
import os
import sys
import textwrap
import time
from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace

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


# ---------------------------------------------------------------- helpers

hns = {
    "Decimal": Decimal, "time": time,
    "MID_EMA": {}, "EMERGENCY": {},
    "EMERGENCY_MIN_MOVE_PCT": 20.0, "EMERGENCY_COOLDOWN": 300.0,
}
for fn in ("emergency_track", "emergency_biggest"):
    exec(compile(seg([top_func(fn)]), "<%s>" % fn, "exec"), hns)

track = hns["emergency_track"]
biggest = hns["emergency_biggest"]

check("first observation only sets the baseline", track("ARB-USDT", Decimal("0.1680"), Decimal("0.1682"), 100.0) is False,
      str(hns["EMERGENCY"]))
check("a 25% jump flags the pair", track("ARB-USDT", Decimal("0.2100"), Decimal("0.2104"), 110.0) is True,
      str(hns["EMERGENCY"]))
check("the flagged pair is the biggest mover", biggest(120.0) == "ARB-USDT")
check("a small drift does not flag", track("HYPE-USDT", Decimal("78.0"), Decimal("78.2"), 130.0) is False
      and track("HYPE-USDT", Decimal("78.4"), Decimal("78.6"), 140.0) is False, str(hns["EMERGENCY"]))
check("the largest move wins", (track("HYPE-USDT", Decimal("130.0"), Decimal("130.2"), 150.0), biggest(160.0))[1] == "HYPE-USDT",
      str(hns["EMERGENCY"]))
check("aged-out entries are dropped", biggest(160.0 + 301.0) is None, str(hns["EMERGENCY"]))

# ---------------------------------------------------------------- idle branch

alerts = []
placed = []


def fake_notify(emoji, action, *lines):
    alerts.append(action + " " + " ".join(lines))


async def fake_book(c, sym, depth=10, max_age=15.0):
    q = BOOKS.get(sym, {})
    return SimpleNamespace(
        bids=[SimpleNamespace(price=Decimal(p)) for p in q.get("bids", [])],
        asks=[SimpleNamespace(price=Decimal(p)) for p in q.get("asks", [])])


async def fake_place(c, sym, side, qty, price, post_only=False):
    placed.append((sym, side, qty, price, post_only))
    return "cid-1", None


BOOKS = {}
META = {
    "ARB-USDT": {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
                 "min_notional": Decimal("5"), "min_qty": Decimal("1"), "scale": 8},
    "HYPE-USDT": {"tick": Decimal("0.001"), "step": Decimal("0.0001"),
                  "min_notional": Decimal("5"), "min_qty": Decimal("0.1"), "scale": 8},
}
BALANCES = []


def build_ns():
    ns = {
        "Decimal": Decimal, "ROUND_FLOOR": ROUND_FLOOR, "time": time, "os": os,
        "notify": fake_notify, "alert": lambda t: alerts.append(t),
        "wallet": lambda base=None: "USDT 2.99",
        "book": fake_book, "place": fake_place,
        "rest_balances": lambda: BALANCES,
        "EMERGENCY": {"ARB-USDT": (25.0, 100.0)},
        "emergency_biggest": lambda now: "ARB-USDT",
        "COST_BASIS": {"ARB-USDT": Decimal("0.1680")},
        "FEES": {}, "DEFAULT_MAKER_PCT": 0.1, "DEFAULT_TAKER_PCT": 0.15,
        "state": "idle", "now": 100.0, "next_scan": 0.0, "next_fallback": 0.0,
        "pos": {}, "carry": {}, "adopt_q": [], "dust": {},
        "QUOTE": "USDT", "meta": META,
    }
    for fn in ("pdec", "floor_step", "num", "taker_fee_pct"):
        exec(compile(seg([top_func(fn)]), "<%s>" % fn, "exec"), ns)
    return ns


ns = build_ns()
exec(compile("async def idle_step(c, pairs):\n    global now, next_scan, next_fallback, state\n"
             + textwrap.indent(seg(state_branch("idle").body), "    "),
             "<idle_step>", "exec"), ns)

alerts.clear()
placed.clear()
BOOKS.clear()
BOOKS["ARB-USDT"] = {"bids": ["0.1670"], "asks": ["0.1672"]}
BALANCES[:] = [{"symbol": "ARB", "trading": "1000", "available": "1000"},
               {"symbol": "USDT", "trading": "2.0", "available": "2.0"}]
asyncio.run(ns["idle_step"](None, META))
check("a held position is dumped at the best bid", ns["state"] == "selling",
      "state=" + ns["state"])
check("the dump is a taker sell at best bid",
      placed == [("ARB-USDT", "sell", Decimal("1000"), Decimal("0.1670"), False)], str(placed))
check("the emergency sell is announced", any("EMERGENCY SELL" in a for a in alerts), str(alerts))
check("carry remembers the dumped basis",
      ns["carry"].get("ARB-USDT") == (Decimal("0.1680"), Decimal("1000")), str(ns["carry"]))
check("the flag is cleared after the dump", ns["EMERGENCY"] == {}, str(ns["EMERGENCY"]))
check("the scan waits after an emergency", ns["next_scan"] == 100.0, str(ns["next_scan"]))

alerts.clear()
placed.clear()
BOOKS.clear()
BOOKS["ARB-USDT"] = {"bids": ["0.1670"], "asks": ["0.1672"]}
BOOKS["HYPE-USDT"] = {"bids": ["78.50"], "asks": ["78.60"]}
ns["emergency_biggest"] = lambda now: "HYPE-USDT"
ns["EMERGENCY"] = {"HYPE-USDT": (22.0, 100.0)}
BALANCES[:] = [{"symbol": "USDT", "trading": "5000", "available": "5000"}]
asyncio.run(ns["idle_step"](None, META))
check("with nothing held it buys the dislocated pair", ns["state"] == "buying",
      "state=" + ns["state"])
check("the buy is a taker at the best ask",
      placed == [("HYPE-USDT", "buy", Decimal("63.6132"), Decimal("78.60"), False)], str(placed))
check("the emergency buy is announced", any("EMERGENCY BUY" in a for a in alerts), str(alerts))

alerts.clear()
placed.clear()
BOOKS.clear()
BOOKS["ARB-USDT"] = {"bids": ["0.1670"], "asks": ["0.1672"]}
BOOKS["HYPE-USDT"] = {"bids": ["78.50"], "asks": ["78.60"]}
ns["emergency_biggest"] = lambda now: "HYPE-USDT"
ns["EMERGENCY"] = {"HYPE-USDT": (22.0, 100.0)}
BALANCES[:] = [{"symbol": "ARB", "trading": "0.5", "available": "0.5"},
               {"symbol": "USDT", "trading": "5000", "available": "5000"}]
asyncio.run(ns["idle_step"](None, META))
check("a holding below the route minimum is skipped, not sold",
      all(p[1] == "buy" for p in placed), str(placed))
check("it still buys the dislocated pair", ns["state"] == "buying", "state=" + ns["state"])

alerts.clear()
placed.clear()
BOOKS.clear()
BOOKS["HYPE-USDT"] = {"bids": ["78.50"], "asks": ["78.60"]}
ns["emergency_biggest"] = lambda now: "HYPE-USDT"
ns["EMERGENCY"] = {"HYPE-USDT": (22.0, 100.0)}
BALANCES[:] = [{"symbol": "USDT", "trading": "0", "available": "0"}]
asyncio.run(ns["idle_step"](None, META))
check("with no funds nothing is placed", placed == [], str(placed))
check("the flag is cleared even when nothing trades", ns["EMERGENCY"] == {}, str(ns["EMERGENCY"]))

print()
print("PASS: a 20% dislocation dumps the book and flags the pair" if ok else "FAIL")
sys.exit(0 if ok else 1)
