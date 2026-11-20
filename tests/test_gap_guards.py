"""Gap guards reject fake books before any order is placed.

The XAUT loss had a best bid at 8,668 while the real market was 3,620 and a
126% gap. The guards must kill that trade on skew and inflated price, accept
ordinary pumps, and mark dumps for a fair-price exit with 3x patience.

Usage:  python tests/test_gap_guards.py
Exit 0 = pass, Exit 1 = fail
"""

import asyncio
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

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

D = Decimal
guards = monitor.gap_guards

ok_, kind, why, sf = guards(D("8668.08"), D("38686.66"), D("333"), D("7919"),
                            D("3620"), D("1146"))
check("the XAUT fat quote is rejected", not ok_ and "skewed" in why, why)

ok_, kind, why, sf = guards(D("3700"), D("3760"), D("5000"), D("5000"),
                            D("3620"), D("500"))
check("an inflated pump bid is rejected", not ok_ and "inflated" in why, why)

ok_, kind, why, sf = guards(D("3630"), D("3700"), D("5000"), D("5000"),
                            D("3620"), D("500"))
check("a normal pump passes", ok_ and kind == "pump" and sf is None,
      f"{ok_} {kind} {sf}")

ok_, kind, why, sf = guards(D("3000"), D("3050"), D("2000"), D("2000"),
                            D("3620"), D("500"))
check("a dump passes with a fair-price exit", ok_ and kind == "dump"
      and sf == D("3620"), f"{ok_} {kind} {sf}")

ok_, kind, why, sf = guards(D("3630"), D("3700"), D("600"), D("600"),
                            D("3620"), D("500"))
check("thin depth is rejected", not ok_ and "thin" in why, why)

ok_, kind, why, sf = guards(D("3620"), D("3660"), D("5000"), D("5000"),
                            D("3620"), D("500"))
check("a flat mid is classified flat", ok_ and kind == "flat", f"{ok_} {kind}")

ok_, kind, why, sf = guards(D("3630"), D("3700"), D("5000"), D("5000"),
                            None, D("500"))
check("no fair price means no trade", not ok_ and "fair" in why, why)

check("gap positions get 3x timeouts", monitor.GAP_TIMEOUT_MULT == 3.0,
      str(monitor.GAP_TIMEOUT_MULT))


class FakeData:
    async def get_candles(self, **kw):
        return SimpleNamespace(candles=[
            SimpleNamespace(ts_sec=1, close="3610", is_closed=True),
            SimpleNamespace(ts_sec=2, close="3620.5", is_closed=True),
            SimpleNamespace(ts_sec=3, close="3630", is_closed=False)])


async def run_fair():
    c = SimpleNamespace(market_data=FakeData())
    return await monitor.fair_close(c, "XAUT-USDT")


check("fair price is the last closed candle", asyncio.run(run_fair()) == D("3620.5"),
      str(asyncio.run(run_fair())))

print()
print("PASS: gap guards reject fake books" if ok else "FAIL")
sys.exit(0 if ok else 1)
