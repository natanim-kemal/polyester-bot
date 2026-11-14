"""The buy side chases: an outbid resting BUY re-bids above competitors.

Mirror of the ask bail-out. Covers:
  1. a fresh outbid does not chase yet, one better bid held long enough does
     (BETTER_BIDS_LIMIT = 1, down from the old fixed >2),
  2. the chase re-bids one tick above the best bid, capped under the ask,
  3. the timeout clock restarts after a chase,
  4. a competitor that flickers (never persists) never triggers a chase,
  5. the sell side bail-out now fires on ONE better ask (>= not >).

Usage:  python tests/test_chase.py
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

M = {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
     "min_notional": Decimal("5"), "min_qty": Decimal("1"), "scale": 8}
PERSIST = monitor.PERSIST_SECONDS

alerts = []
placed = []
cancel_calls = []


class FakePrice:
    def __init__(self, price):
        self.price = price


class FakeBook:
    def __init__(self, bids, asks):
        self.bids = [FakePrice(p) for p in bids]
        self.asks = [FakePrice(p) for p in asks]


class FakeOrders:
    async def cancel(self, key=None, symbol=None):
        cancel_calls.append(symbol)


class FakeBookAPI:
    def __init__(self, bids, asks):
        self._bids, self._asks = list(bids), list(asks)

    async def get(self, symbol=None, depth=None):
        return FakeBook(self._bids, self._asks)


class FakeC:
    def __init__(self, bids, asks):
        self.orderbook = FakeBookAPI(bids, asks)
        self.orders = FakeOrders()


async def fake_place(c, sym, side, qty, price, post_only=False):
    placed.append(price)
    return "cid-new", None


async def open_state(c, sym, cid, scale):
    return "open", Decimal(0), Decimal(0)


async def fake_cycle_done2(sym, px):
    pass


monitor.place = fake_place
monitor.order_state = open_state
monitor.notify = lambda emoji, action, *lines: alerts.append(action)
monitor.wallet = lambda base=None: "USDT 2.99"


def new_pos(buy_px):
    return {"symbol": "ARB-USDT", "buy_cid": "cid-1", "buy_px": buy_px,
            "buy_at": 0.0, "qty": None, "qty_target": Decimal("100"),
            "sell_cid": None, "better": {}, "tag": "GAP",
            "buy_fee_pct": Decimal("0.05")}


async def step(c, pos, t):
    await monitor.chase_buy(c, pos, M, t)


c = FakeC(["0.1671"], ["0.1690"])
pos = new_pos(Decimal("0.1670"))
asyncio.run(step(c, pos, 100.0))
check("a fresh outbid does not chase yet",
      len(cancel_calls) == 0 and len(placed) == 0)
asyncio.run(step(c, pos, 100.0 + PERSIST))
check("one better bid held long enough fires the chase",
      len(cancel_calls) == 1 and len(placed) == 1,
      f"cancels={len(cancel_calls)} placed={len(placed)}")
check("it re-bids one tick above the competitor",
      placed and placed[0] == Decimal("0.1672"),
      str(placed[-1] if placed else None))
check("the position follows the new order",
      pos["buy_cid"] == "cid-new" and pos["buy_px"] == Decimal("0.1672"))
check("the timeout clock restarts after a chase",
      pos["buy_at"] == 100.0 + PERSIST, str(pos["buy_at"]))

cancel_calls.clear()
placed.clear()
alerts.clear()
monitor.WS_BOOKS.clear()
c2 = FakeC(["0.1689"], ["0.1690"])
p2 = new_pos(Decimal("0.1688"))
asyncio.run(step(c2, p2, 100.0))
asyncio.run(step(c2, p2, 100.0 + PERSIST))
check("a chase never crosses into the ask",
      placed and placed[0] == Decimal("0.1689"),
      str(placed[-1] if placed else None))

cancel_calls.clear()
placed.clear()
c = FakeC(["0.1671"], ["0.1690"])
pos = new_pos(Decimal("0.1670"))
asyncio.run(step(c, pos, 100.0))                    # competitor appears
monitor.WS_BOOKS.clear()
c.orderbook._bids = []
asyncio.run(step(c, pos, 130.0))                    # leaves before persisting
c.orderbook._bids = ["0.1671"]
monitor.WS_BOOKS.clear()
asyncio.run(step(c, pos, 140.0))                    # returns, clock restarts
monitor.WS_BOOKS.clear()
asyncio.run(step(c, pos, 155.0))                    # only 15s old again
check("a flickering competitor never triggers a chase",
      cancel_calls == [] and placed == [],
      f"cancels={cancel_calls} placed={placed}")

# The sell side: the bail-out must fire on ONE better ask now.
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


monitor.WS_BOOKS.clear()
alerts2 = []
ns = {
    "Decimal": Decimal, "ROUND_FLOOR": ROUND_FLOOR, "time": time,
    "notify": lambda emoji, action, *lines: alerts2.append(action),
    "cycle_done": fake_cycle_done2,
    "wallet": lambda base=None: "USDT 2.99",
    "order_state": open_state,
    "rest_asset_balance": lambda base: (Decimal("100"), Decimal("100")),
    "rest_open_orders": lambda: {"orders": []},
    "ClientOrderId": monitor.ClientOrderId,
    "book": monitor.book,
    "pdec": monitor.pdec, "floor_step": monitor.floor_step,
    "money": monitor.money, "num": monitor.num,
    "dust": {}, "carry": {}, "TERMINAL": monitor.TERMINAL,
    "PERSIST_SECONDS": PERSIST, "BETTER_ASKS_LIMIT": monitor.BETTER_ASKS_LIMIT,
    "BALANCE_GRACE": 120.0, "meta": {"ARB-USDT": M},
    "chase_note": lambda sym, weight, now: None,
    "CHASE_ASK_WEIGHT": monitor.CHASE_ASK_WEIGHT,
    "CHASE_BAIL_WEIGHT": monitor.CHASE_BAIL_WEIGHT,
}
exec(compile("async def selling_step(c, pos, meta, now):\n"
             + textwrap.indent(seg(state_branch("selling").body), "    "),
             "<selling_step>", "exec"), ns)

spos = {"symbol": "ARB-USDT", "sell_cid": "cid-9", "ask_px": Decimal("0.1690"),
        "better": {}, "seen": True, "since": 0.0}
cancel_calls.clear()
asyncio.run(ns["selling_step"](FakeC([], ["0.1689"]), spos, ns["meta"], 200.0))
check("a fresh better ask does not bail out yet", spos.get("canceling") is None)
asyncio.run(ns["selling_step"](FakeC([], ["0.1689"]), spos, ns["meta"],
                               200.0 + PERSIST))
check("one better ask held long enough bails out",
      spos.get("canceling") is True and len(cancel_calls) == 1,
      f"canceling={spos.get('canceling')} cancels={len(cancel_calls)}")
check("the bail-out is announced", "ASK BAIL-OUT" in alerts2, str(alerts2))

print()
print("PASS: both sides chase the price now" if ok else "FAIL")
sys.exit(0 if ok else 1)
