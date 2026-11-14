"""Pre-flight check before a buy hits the exchange.

preview_order tells us the order would be admissible before we spend the
round trip. The fail-safe is fail-open: an unreachable pre-flight never
blocks a trade, only a clear rejection does.

  1. admissible=true -> the trade proceeds,
  2. admissible=false with a rejection -> blocked, with the reason,
  3. admissible=none -> proceeds,
  4. a broken pre-flight -> proceeds, never a crash,
  5. after a rejection, the idle branch skips the placement and alerts.

Usage:  python tests/test_preflight.py
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
sys.path.insert(0, os.path.join(HERE, "..", "bot"))

ok = True


def check(label, good, detail=""):
    global ok
    ok = ok and good
    print("%-6s %s%s" % ("PASS" if good else "FAIL", label,
                         (" -> " + detail) if detail else ""))


try:
    import monitor
except Exception as e:
    print("FAIL cannot import monitor:", repr(e))
    sys.exit(1)

META = {"ARB-USDT": {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
                     "min_notional": Decimal("5"), "min_qty": Decimal("1"),
                     "scale": 8}}


class FakePreview:
    def __init__(self, admissible, code=""):
        self.admissible = admissible
        self.rejection = (type("R", (), {"code": code})() if code else None)


class FakeOrders:
    def __init__(self, preview=None, raise_preview=False):
        self.calls = []
        self.preview = preview
        self.raise_preview = raise_preview

    async def preview_order(self, **kwargs):
        if self.raise_preview:
            raise RuntimeError("pre-flight down")
        self.calls.append(kwargs)
        return self.preview


class FakeC:
    def __init__(self, preview=None, raise_preview=False):
        self.orders = FakeOrders(preview, raise_preview)


async def call_pre(c):
    return await monitor.preflight_buy(c, "ARB-USDT", "buy",
                                       Decimal("1000"), Decimal("0.1670"))


ok_flag, why = asyncio.run(call_pre(FakeC(FakePreview(True))))
check("admissible=true lets the trade through", ok_flag is True, f"why={why}")

ok_flag, why = asyncio.run(call_pre(FakeC(FakePreview(False, "post_only_cross"))))
check("a clear rejection blocks with the reason",
      ok_flag is False and "post_only_cross" in str(why), f"ok={ok_flag} why={why}")

ok_flag, why = asyncio.run(call_pre(FakeC(FakePreview(None))))
check("admissible=none proceeds", ok_flag is True)

ok_flag, why = asyncio.run(call_pre(FakeC(raise_preview=True)))
check("a broken pre-flight proceeds, never a crash", ok_flag is True)

src = open(os.path.join(HERE, "..", "bot", "monitor.py"), encoding="utf-8").read()
srclines = src.splitlines()
tree = ast.parse(src)


def seg(nodes):
    return textwrap.dedent("\n".join(srclines[nodes[0].lineno - 1:nodes[-1].end_lineno]))


def state_branch(name):
    main_node = next(n for n in tree.body
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    for node in ast.walk(main_node):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left, comps = node.test.left, node.test.comparators
            if (isinstance(left, ast.Name) and left.id == "state" and len(comps) == 1
                    and isinstance(comps[0], ast.Constant) and comps[0].value == name):
                return node
    raise AssertionError("branch %r not found" % name)


alerts, placed, preflight_calls = [], [], []


async def fake_scan_books(c, pairs):
    return [(Decimal("0.30"), "ARB-USDT", Decimal("0.1670"), Decimal("0.1675"),
             Decimal("10000"), Decimal("10000"))]


async def fake_preflight_reject(c, sym, side, qty, price):
    preflight_calls.append((sym, side, qty, price))
    return False, "reserve_exceeds_available"


async def fake_place(c, sym, side, qty, price, post_only=False):
    placed.append((side, qty, price))
    return "cid-new", None


ns = {
    "Decimal": Decimal, "ROUND_FLOOR": ROUND_FLOOR, "time": time,
    "notify": lambda emoji, action, *lines: alerts.append(action),
    "wallet": lambda base=None: "USDT 2.99",
    "scan_books": fake_scan_books,
    "rest_balances": lambda: [{"symbol": "USDT", "trading": "10000", "available": "10000"}],
    "rest_open_orders": lambda: {"orders": []},
    "pick_best_liquid": None,
    "place": fake_place,
    "preflight_buy": fake_preflight_reject,
    "pdec": monitor.pdec, "floor_step": monitor.floor_step,
    "money": monitor.money, "num": monitor.num,
    "entry_price": monitor.entry_price,
    "maker_fee_pct": monitor.maker_fee_pct,
    "taker_fee_pct": monitor.taker_fee_pct, "FEES": {},
    "QUOTE": "USDT", "GAP_MIN_PCT": Decimal("0.11"),
    "DEPTH_MIN_USD": Decimal("50"), "SCAN_SECONDS": 10.0,
    "FALLBACK_MIN_GAP_PCT": Decimal("0.05"), "FALLBACK_DEPTH_MIN_USD": Decimal("1000"),
    "FALLBACK_SYMBOL": "", "FALLBACK_COOLDOWN": 60.0, "FALLBACK_PENNY": False,
    "SAT_PENNY_AFTER": 2, "SAT_TAKER_AFTER": 4,
    "TERMINAL": {"filled", "canceled", "rejected", "expired"},
    "FEE_BUFFER": Decimal("1.0015"),
    "SOFT": None,
    "meta": META,
    "now": 100.0, "next_scan": 0.0, "next_fallback": 0.0,
    "next_block_alert": 0.0, "sat": {"n": 0},
    "adopt_q": [],
    "chase_parked": lambda sym, now: False,
}
exec(compile("async def idle_step(c, pairs):\n"
             "    global now, next_scan, next_fallback, next_block_alert\n"
             + textwrap.indent(seg(state_branch("idle").body), "    "),
             "<idle_step>", "exec"), ns)
asyncio.run(ns["idle_step"](None, META))
check("a rejected pre-flight skips the placement entirely",
      placed == [] and len(preflight_calls) == 1,
      f"placed={placed} preflight_calls={preflight_calls}")
check("the rejection is announced", "SKIPPED" in alerts, str(alerts))

print()
print("PASS: a clear rejection stops the order, an outage passes it through" if ok else "FAIL")
sys.exit(0 if ok else 1)
