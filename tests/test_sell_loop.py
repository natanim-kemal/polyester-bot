"""Deterministic repro of the ask -> cancel -> ask storm in monitor.py.

Executes the REAL source of place_sell() and the REAL `state == "selling"`
branch out of monitor.py against a fake exchange, so the signal is tight and
repeatable (no network, no exchange, no timing luck).

Scenario: one ask rests, 3 better asks appear and persist >PERSIST_SECONDS,
so the bail-out rule fires once.  Expected behaviour: ONE cancel, ONE re-ask,
then the ask rests.  Buggy behaviour: the re-ask is canceled too, forever.

Usage:  python tests/test_sell_loop.py [path-to-monitor.py]
Exit 0 = pass (ask rests after a single bail-out)
Exit 1 = fail (cancel storm)
"""

import ast
import asyncio
import os
import sys
import textwrap
import time
import uuid
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


def seg(nodes):
    return textwrap.dedent("\n".join(lines[nodes[0].lineno - 1:nodes[-1].end_lineno]))


def top_func(name):
    return next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


main_node = top_func("main")
selling = None
for node in ast.walk(main_node):
    if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
        left, comps = node.test.left, node.test.comparators
        if (isinstance(left, ast.Name) and left.id == "state" and len(comps) == 1
                and isinstance(comps[0], ast.Constant) and comps[0].value == "selling"):
            selling = node
            break
assert selling, "could not locate the state == 'selling' branch"

TOTAL = Decimal("100")
TICK = Decimal("0.1")
STEP = Decimal("0.0001")
ASKS = ["9.7", "9.8", "9.9", "10.0"]
BASIS = Decimal("10.0")
CANCEL_LAG = 6.0


class Exchange:
    def __init__(self):
        self.orders = {}
        self.events = []
        self.now = 0.0
        self.n = 0

    def reserved(self):
        return sum(o["qty"] for o in self.orders.values()
                   if o["status"] in ("open", "canceling"))

    def available(self):
        return TOTAL - self.reserved()

    def place(self, sym, side, qty, price):
        self.n += 1
        cid = "cid-%d" % self.n
        self.orders[cid] = {"status": "open", "qty": qty, "price": price, "release_at": None}
        self.events.append(("ASK", cid, str(qty), str(price)))
        return cid

    def cancel(self, cid):
        o = self.orders.get(cid)
        if o is None or o["status"] != "open":
            return
        o["status"] = "canceling"
        o["release_at"] = self.now + CANCEL_LAG
        self.events.append(("CANCEL", cid))

    def poll_state(self, cid):
        o = self.orders.get(cid)
        if o is None:
            return "pending", Decimal(0), Decimal(0)
        if o["status"] == "canceling" and self.now >= o["release_at"]:
            o["status"] = "canceled"
            self.events.append(("GONE", cid))
        return o["status"], Decimal(0), Decimal(0)


ex = Exchange()
alerts = []


async def fake_place(c, sym, side, qty, price, post_only=False):
    return ex.place(sym, side, qty, price), None


async def fake_order_state(c, sym, cid, scale):
    return ex.poll_state(cid)


def fake_rest_open_orders():
    return {"orders": [{"clientOrderId": cid} for cid, o in ex.orders.items()
                        if o["status"] in ("open", "canceling")]}


def fake_rest_balances():
    return [{"symbol": "TEST", "available": str(ex.available()), "trading": str(TOTAL)}]


def fake_rest_asset_balance(base):
    return (TOTAL, ex.available())


def fake_alert(text, priority=False):
    alerts.append(text)
    ex.events.append(("ALERT", text[:60], "priority" if priority else "routine"))


def fake_cycle_done(sym, px):
    ex.events.append(("CYCLE_DONE", sym, str(px)))


class ClientOrderId:
    def __init__(self, v):
        self.value = v


class FakeOrders:
    async def cancel(self, key, symbol):
        ex.cancel(key.value)


class Px:
    def __init__(self, v):
        self.price = Decimal(str(v))


class FakeBook:
    def __init__(self):
        self.asks = [Px(a) for a in ASKS]
        self.bids = [Px("9.5")]


class FakeOrderbook:
    async def get(self, symbol, depth=10):
        return FakeBook()


class FakeClient:
    def __init__(self):
        self.orders = FakeOrders()
        self.orderbook = FakeOrderbook()


async def fake_book(c, sym, depth=10, max_age=15.0):
    return await c.orderbook.get(symbol=sym, depth=depth)


ns = {
    "Decimal": Decimal, "time": time, "uuid": uuid, "ROUND_FLOOR": ROUND_FLOOR,
    "place": fake_place, "order_state": fake_order_state,
    "rest_open_orders": fake_rest_open_orders, "rest_balances": fake_rest_balances,
    "rest_asset_balance": fake_rest_asset_balance,
    "alert": fake_alert, "cycle_done": fake_cycle_done,
    "wallet": lambda base=None: "USDT 100",
    "ClientOrderId": ClientOrderId,
    "book": fake_book,
    "PERSIST_SECONDS": 60.0, "BETTER_ASKS_LIMIT": 2,
    "TERMINAL": {"filled", "canceled", "rejected", "expired"},
    "chase_note": lambda sym, weight, now: None,
    "CHASE_ASK_WEIGHT": 1.0,
    "CHASE_BAIL_WEIGHT": 2.0,
}

for fn in ("pdec", "floor_step", "money", "num", "notify"):
    exec(compile(seg([top_func(fn)]), "<%s>" % fn, "exec"), ns)

exec(compile(seg([top_func("place_sell")]), "<place_sell>", "exec"), ns)
exec(compile("async def selling_step(c, pos, meta, now):\n"
             + textwrap.indent(seg(selling.body), "    "), "<selling_step>", "exec"), ns)

meta = {"TEST-USDT": {"tick": TICK, "step": STEP, "min_notional": Decimal("1"),
                      "min_qty": Decimal("0.0001"), "scale": 8}}
pos = {"symbol": "TEST-USDT", "qty": TOTAL, "buy_px": BASIS,
       "sell_cid": None, "better": {}}
client = FakeClient()


async def run():
    now = 0.0
    for _ in range(60):
        now += 2.0
        ex.now = now
        await ns["selling_step"](client, pos, meta, now)


asyncio.run(run())

asks = [e for e in ex.events if e[0] == "ASK"]
cancels = [e for e in ex.events if e[0] == "CANCEL"]
bails = [a for a in alerts if "bail-out" in a.lower()]

print("timeline:")
for e in ex.events:
    print("   ", e)
print()
print("asks=%d cancels=%d bail-outs=%d" % (len(asks), len(cancels), len(bails)))
print("bail-out alerts:", len(bails))
print("position flags:", {k: pos.get(k) for k in ("sell_cid", "canceling", "bail", "ask_px")})

ok = len(bails) == 1 and len(cancels) == 1 and len(asks) == 2
if ok:
    print("\nPASS: one bail-out -> one cancel -> one re-ask -> ask rests")
    sys.exit(0)
print("\nFAIL: cancel storm - the bot keeps canceling the ask it just placed"
      if len(cancels) > 1 else "\nFAIL: unexpected behaviour")
sys.exit(1)
