"""Price chases amend in place instead of cancel + re-place.

A chase changes one thing: the price. Amending is one atomic call, keeps the
remaining quantity, and leaves no window where the bot holds no order. Only
when the amend itself is impossible or fails does the bot cancel and re-place
the remainder, and when the order is dying the cancel path stays.

  1. a chase amends the price; no cancel, no re-place
  2. a partially filled order amends its remainder's price, qty untouched
  3. an amend failure falls back to cancel + place of the remainder
  4. the sell side amends under one better ask; no cancel, no re-ask
  5. an amend failure on the sell side falls back to the legacy bail-out

Usage:  python tests/test_amend.py
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
MONEY0 = Decimal("0")

alerts, cancels, modifies, placed = [], [], [], []
codescript = {}


class FakePrice:
    def __init__(self, price):
        self.price = price


class FakeBook:
    def __init__(self, bids, asks):
        self.bids = [FakePrice(b) for b in bids]
        self.asks = [FakePrice(p) for p in asks]


class FakeBookAPI:
    def __init__(self, bids, asks):
        self._bids, self._asks = bids, asks

    async def get(self, symbol=None, depth=None):
        return FakeBook(self._bids, self._asks)


class FakeOrders:
    def __init__(self, fail_modify=False):
        self.fail_modify = fail_modify

    async def cancel(self, key=None, symbol=None):
        cancels.append(symbol)

    async def modify(self, key=None, symbol=None, new_price=None):
        if self.fail_modify:
            raise RuntimeError("amend rejected")
        modifies.append((symbol, new_price))


class FakeC:
    def __init__(self, bids, asks, fail_modify=False):
        self.orderbook = FakeBookAPI(bids, asks)
        self.orders = FakeOrders(fail_modify)


async def fake_place(c, sym, side, qty, price, post_only=False):
    placed.append((side, qty, price))
    return "cid-new", None


async def clear_place(c, sym, side, qty, price, post_only=False):
    placed.append((side, qty, price))
    return "cid-new", None


monitor.place = fake_place
monitor.notify = lambda emoji, action, *lines: alerts.append(action)
monitor.wallet = lambda base=None: "USDT 2.99"


def buy_pos():
    return {"symbol": "ARB-USDT", "buy_cid": "cid-1", "buy_px": Decimal("0.1670"),
            "buy_at": 0.0, "qty": None, "qty_target": Decimal("1000"),
            "sell_cid": None, "better": {}, "tag": "GAP",
            "buy_fee_pct": Decimal("0.05")}


def outbid(c, pos):
    asyncio.run(monitor.chase_buy(c, pos, M, 100.0))
    asyncio.run(monitor.chase_buy(c, pos, M, 100.0 + PERSIST))
    return pos


async def open_state_zero(c, sym, cid, scale):
    return "open", MONEY0, MONEY0


async def open_state_filled(c, sym, cid, scale):
    return "open", Decimal("100"), Decimal("0.1671")


check("--- chase amends instead of cancelling", True)
monitor.order_state = open_state_zero
c = FakeC(["0.1671"], ["0.1690"])
pos = outbid(c, buy_pos())
check("the amend is one atomic call, no cancel",
      modifies == [("ARB-USDT", Decimal("0.1672"))] and cancels == [],
      f"modifies={modifies} cancels={cancels}")
check("the order is not re-placed", placed == [])
check("the position keeps its order id and moves to the new price",
      pos["buy_cid"] == "cid-1" and pos["buy_px"] == Decimal("0.1672"),
      f"cid={pos['buy_cid']} px={pos['buy_px']}")
check("the timeout clock and the better-price tracker reset",
      pos["buy_at"] == 100.0 + PERSIST and pos["better"] == {})

modifies.clear()
monitor.order_state = open_state_filled
c = FakeC(["0.1671"], ["0.1690"])
pos = outbid(c, buy_pos())
check("a partially filled order amends, never cancels",
      modifies == [("ARB-USDT", Decimal("0.1672"))] and cancels == [] and placed == [],
      f"modifies={modifies} cancels={cancels} placed={placed}")
check("the ordered quantity is untouched by an amend",
      pos["qty_target"] == Decimal("1000"))

modifies.clear()
cancels.clear()
placed.clear()
states = [("open", MONEY0, MONEY0), ("open", Decimal("100"), Decimal("0.1671"))]


async def half_then_more(c, sym, cid, scale):
    return states.pop(0) if states else ("open", Decimal("100"), Decimal("0.1671"))


monitor.order_state = half_then_more
c = FakeC(["0.1671"], ["0.1690"], fail_modify=True)
pos = outbid(c, buy_pos())
check("an amend failure falls back to cancel + re-place of the remainder",
      len(cancels) == 1 and len(placed) == 1 and placed[0][2] == Decimal("0.1672"),
      f"cancels={cancels} placed={placed}")
check("the remainder is the unfilled quantity",
      placed and placed[0][1] == Decimal("900"), str(placed))

# The sell side: extract the selling branch like test_stuck/test_chase do.
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


async def open_state(c, sym, cid, scale):
    return "open", MONEY0, Decimal("0")


async def fake_cycle_done3(sym, px):
    pass


def run_sell(fail_modify):
    alerts.clear()
    cancels.clear()
    modifies.clear()
    monitor.WS_BOOKS.clear()
    ns = {
        "Decimal": Decimal, "ROUND_FLOOR": ROUND_FLOOR, "time": time,
        "notify": lambda emoji, action, *lines: alerts.append(action),
        "cycle_done": fake_cycle_done3,
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
    pos = {"symbol": "ARB-USDT", "sell_cid": "cid-9", "ask_px": Decimal("0.1690"),
           "better": {}, "seen": True, "since": 0.0, "qty": Decimal("100")}
    c = FakeC([], ["0.1695", "0.1689"], fail_modify=fail_modify)
    asyncio.run(ns["selling_step"](c, pos, ns["meta"], 200.0))
    asyncio.run(ns["selling_step"](c, pos, ns["meta"], 200.0 + PERSIST))
    return pos


pos = run_sell(fail_modify=False)
check("the sell side amends under one better ask",
      modifies == [("ARB-USDT", Decimal("0.1688"))] and cancels == [],
      f"modifies={modifies} cancels={cancels}")
check("no cancel, no re-ask, no canceling flag",
      pos["ask_px"] == Decimal("0.1688") and pos.get("canceling") is None,
      f"ask_px={pos['ask_px']} canceling={pos.get('canceling')}")
check("the amend is announced", "ASK CHASE" in alerts, str(alerts))

pos = run_sell(fail_modify=True)
check("an amend failure falls back to the legacy cancel + re-ask",
      len(cancels) == 1 and pos.get("canceling") is True,
      f"cancels={cancels} canceling={pos.get('canceling')}")
check("the legacy path keeps its alert", "ASK BAIL-OUT" in alerts, str(alerts))

print()
print("PASS: chases amend in place, cancel only when truly needed" if ok else "FAIL")
sys.exit(0 if ok else 1)
