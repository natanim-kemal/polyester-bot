"""The book is pushed over websockets, not polled over REST.

The WS feed keeps a full book snapshot per pair in memory; readers prefer it
and fall back to REST when it goes stale (freeze, disconnect, cold start).

  1. a fresh WS book wins over REST (REST may be unreachable and the read still works),
  2. a stale or missing WS book falls back to REST,
  3. scan_books builds its rows from the WS cache when it is fresh,
  4. the chase fires on the WS book alone,
  5. run_books_ws populates the cache from subscription events.

Usage:  python tests/test_ws.py
Exit 0 = pass, Exit 1 = fail
"""

import asyncio
import os
import sys
import time
import types
from decimal import Decimal, ROUND_FLOOR

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

alerts = []
monitor.notify = lambda emoji, action, *lines: alerts.append(action)
monitor.wallet = lambda base=None: "USDT 2.99"

M = {"ARB-USDT": {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
                  "min_notional": Decimal("5"), "min_qty": Decimal("1"), "scale": 8}}


def fake_book(bid, ask, seq="1", available=True):
    class L:
        def __init__(self, p, q):
            self.price = Decimal(p)
            self.qty = Decimal(q)
    return types.SimpleNamespace(symbol="ARB-USDT", depth=10, book_seq=seq,
                                 bids=[L(bid, "1000")], asks=[L(ask, "2000")],
                                 available=available)


class RaisingBookAPI:
    async def get(self, symbol=None, depth=None):
        raise RuntimeError("REST down")


class RaisingC:
    def __init__(self):
        self.orderbook = RaisingBookAPI()


def seed(sym, age):
    monitor.WS_BOOKS[sym] = (fake_book("0.1671", "0.1690"), time.monotonic() - age)


monitor.WS_BOOKS.clear()
seed("ARB-USDT", 0.0)
cached = asyncio.run(monitor.book(RaisingC(), "ARB-USDT"))
check("a fresh WS book wins over REST",
      cached.bids and Decimal(cached.bids[0].price) == Decimal("0.1671"))

monitor.WS_BOOKS.clear()
seed("ARB-USDT", 9999.0)
try:
    asyncio.run(monitor.book(RaisingC(), "ARB-USDT"))
    check("a stale WS book falls back to REST", False)
except RuntimeError:
    check("a stale WS book falls back to REST", True)

monitor.WS_BOOKS.clear()


class RestingBookAPI:
    def __init__(self):
        self.calls = []

    async def get(self, symbol=None, depth=None):
        self.calls.append((symbol, depth))
        if symbol == "ARB-USDT":
            return fake_book("0.1670", "0.1690")
        raise RuntimeError("down")


class RestingC:
    def __init__(self):
        self.orderbook = RestingBookAPI()


rows = asyncio.run(monitor.scan_books(RestingC(), {"ARB-USDT": {}, "ETH-USDT": {}}))
check("REST builds rows when WS has nothing",
      len(rows) == 1 and rows[0][1] == "ARB-USDT", str(rows))

monitor.WS_BOOKS.clear()
seed("ARB-USDT", 0.0)
resting = RestingBookAPI()
mon = types.SimpleNamespace(orderbook=resting)
rows = asyncio.run(monitor.scan_books(mon, {"ARB-USDT": {}}))
check("scan builds rows from the WS cache without REST",
      len(rows) == 1 and rows[0][1] == "ARB-USDT" and resting.calls == [],
      f"rows={rows} calls={resting.calls}")
monitor.WS_BOOKS.clear()


class FakeOrders:
    def __init__(self):
        self.cancels = []

    async def cancel(self, key=None, symbol=None):
        self.cancels.append(symbol)


class ChaseC:
    def __init__(self):
        self.orderbook = RaisingBookAPI()
        self.orders = FakeOrders()


async def open_state(c, sym, cid, scale):
    return "open", Decimal(0), Decimal(0)


monitor.order_state = open_state
monitor.place = lambda *a, **k: asyncio.sleep(0, result=("cid-x", None))
monitor.rest_balances = lambda: [
    {"symbol": "USDT", "trading": "10000", "available": "10000"}]
monitor.WS_BOOKS["ARB-USDT"] = (fake_book("0.1671", "0.1690"), time.monotonic())
pos = {"symbol": "ARB-USDT", "buy_cid": "cid-1", "buy_px": Decimal("0.1670"),
       "buy_at": 0.0, "qty": None, "qty_target": Decimal("100"),
       "sell_cid": None, "better": {}, "tag": "GAP", "buy_fee_pct": Decimal("0.05")}
chase = ChaseC()
asyncio.run(monitor.chase_buy(chase, pos, M["ARB-USDT"], 100.0))
asyncio.run(monitor.chase_buy(chase, pos, M["ARB-USDT"], 100.0 + monitor.PERSIST_SECONDS))
check("the chase fires on the WS book alone",
      chase.orders.cancels == ["ARB-USDT"] and pos["buy_px"] == Decimal("0.1672"),
      f"cancels={chase.orders.cancels} px={pos['buy_px']}")
monitor.WS_BOOKS.clear()


class FakeSubscription:
    def __init__(self, data):
        self._data = data

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yield self._data
        while True:
            await asyncio.sleep(60)


class FakeSubBookAPI:
    def __init__(self, data):
        self._data = data
        self.subscribe_calls = 0
        self.symbol_id_seen = None

    async def subscribe(self, symbol=None, symbol_id=None, depth=None):
        self.subscribe_calls += 1
        self.symbol_id_seen = symbol_id
        return FakeSubscription(self._data)


async def run_once():
    c = types.SimpleNamespace(orderbook=FakeSubBookAPI(fake_book("0.1671", "0.1690")))
    stop = asyncio.Event()
    task = asyncio.create_task(monitor.run_books_ws(c, {"ARB-USDT": 23}, stop))
    await asyncio.sleep(0.3)
    stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return c


monitor.WS_BOOKS.clear()
c = asyncio.run(run_once())
got = monitor.WS_BOOKS.get("ARB-USDT")
check("run_books_ws populates the cache from WS events",
      got is not None and Decimal(got[0].asks[0].price) == Decimal("0.1690"),
      str(got and got[0].asks[0].price))
check("the subscription carries the symbol id from the spot config",
      c.orderbook.symbol_id_seen == 23, str(c.orderbook.symbol_id_seen))

print()
print("PASS: the book is pushed, not polled" if ok else "FAIL")
sys.exit(0 if ok else 1)
