"""USDC mode: full balance, join the best bid exactly, 3x patience, no other pairs.

Usage:  python tests/test_usdc_mode.py [path-to-monitor.py]
Exit 0 = pass, Exit 1 = fail
"""

import ast
import asyncio
import os
import sys
import textwrap
import time
from decimal import Decimal

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


placed = []
alerts = []
BID = Decimal("1.00091")
ASK = Decimal("1.00119")
USDT = Decimal("9351.84")
META = {"USDC-USDT": {"tick": Decimal("0.00001"), "step": Decimal("0.01"),
                      "min_notional": Decimal("5"), "min_qty": Decimal("1"),
                      "scale": 8}}


def level(px, q):
    class L:
        price = type("P", (), {"as_decimal": lambda self: px})()
        qty = type("Q", (), {"as_decimal": lambda self: q})()
    return L()


async def fake_book(c, sym, depth=10, max_age=15.0):
    class BK:
        bids = [level(BID, Decimal("57290"))]
        asks = [level(ASK, Decimal("49"))]
    return BK()


async def fake_place(c, sym, side, qty, price, post_only=False):
    placed.append((sym, side, qty, price, post_only))
    return "cid-usdc", None


async def fake_scan_books(c, pairs):
    return []


def notify(emoji, action, *lines):
    alerts.append(action + " " + " ".join(lines))


def build_ns(on):
    ns = {
        "Decimal": Decimal, "ROUND_FLOOR": __import__("decimal").ROUND_FLOOR,
        "time": time, "os": os,
        "USDC_MODE": on, "meta": META, "QUOTE": "USDT",
        "SCAN_SECONDS": 10.0, "next_scan": 0.0, "next_fallback": 0.0,
        "next_block_alert": 0.0, "state": "idle", "pos": {},
        "now": 100.0,
        "carry": {}, "dust": {}, "adopt_q": [], "sat": {"n": 0},
        "GAP_MIN_PCT": Decimal("0.11"), "DEPTH_MIN_USD": Decimal("50"),
        "FALLBACK_MIN_GAP_PCT": Decimal("0.05"),
        "FALLBACK_DEPTH_MIN_USD": Decimal("1000"), "FALLBACK_SYMBOL": "",
        "FALLBACK_COOLDOWN": 60.0, "FALLBACK_PENNY": False,
        "BALANCE_GRACE": 120.0, "BUY_TIMEOUT_SECONDS": 180.0,
        "GAP_TIMEOUT_MULT": 3.0, "FEES": {},
        "DEFAULT_MAKER_PCT": 0.03, "DEFAULT_TAKER_PCT": 0.08,
        "TERMINAL": {"filled", "canceled", "rejected", "expired"},
        "FEE_BUFFER": Decimal("1.0015"),
        "book": fake_book, "place": fake_place, "notify": notify,
        "scan_books": fake_scan_books,
        "wallet": lambda base=None: "USDT 9,351",
        "rest_balances": lambda: [{"symbol": "USDT", "available": str(USDT)}],
        "rest_open_orders": lambda: {"orders": []},
        "emergency_biggest": lambda now: None,
        "chase_parked": lambda sym, now: False,
        "calmest": lambda rows, now, limit=3: rows[:limit],
        "least_lossy": lambda rows, now, limit=3: rows[:limit],
        "calm_streak_update": lambda rows, now, limit=3: None,
        "alert": lambda text: alerts.append(text),
    }
    for fn in ("pdec", "floor_step", "money", "num", "maker_fee_pct",
               "taker_fee_pct"):
        exec(compile(seg([top_func(fn)]), "<%s>" % fn, "exec"), ns)
    return ns


def run(on):
    ns = build_ns(on)
    exec(compile("async def idle_step(c, pairs):\n"
                 "    global now, next_scan, next_fallback, state, pos\n"
                 + textwrap.indent(seg(state_branch("idle").body), "    "),
                 "<idle_step>", "exec"), ns)
    asyncio.run(ns["idle_step"](None, META))
    return ns


placed.clear()
alerts.clear()
ns = run(True)
check("usdc mode places exactly one buy", len(placed) == 1, str(placed))
if placed:
    sym, side, qty, price, po = placed[0]
    check("it trades only USDC-USDT", sym == "USDC-USDT", sym)
    check("it buys at the exact best bid, no extra tick", price == BID, str(price))
    check("it uses the full USDT balance", qty == ns["floor_step"](USDT / BID, META[sym]["step"]),
          str(qty))
    check("the buy is post-only", po is True, str(po))
check("the position carries 3x patience", ns["pos"].get("time_mult") == 3.0,
      str(ns["pos"].get("time_mult")))
check("the position is tagged usdc", ns["pos"].get("usdc_mode") is True,
      str(ns["pos"].get("usdc_mode")))
check("a usdc buy is announced", any("USDC BUY" in a for a in alerts), str(alerts))

placed.clear()
alerts.clear()
ns2 = run(False)
check("with the mode off nothing is placed", placed == [], str(placed))

print()
print("PASS: usdc mode joins the best bid with full balance" if ok else "FAIL")
sys.exit(0 if ok else 1)
