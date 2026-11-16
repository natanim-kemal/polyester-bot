"""Volume-fallback selection: cheapest round-trip first.

GAP mode prefers the widest gap (that's where the profit is). The volume
fallback is taker on both sides, so profit-per-trade is always negative;
the best we can do is lose as little as possible: lowest spread, then
calmest book. Same park and shortlist rules as gap mode.

Usage:  python tests/test_vol_pick.py
Exit 0 = pass, Exit 1 = fail
"""

import os
import sys
from collections import deque
from decimal import Decimal

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

now = 10_000.0
monitor.CHURN.clear()
monitor.CHURN["CHOPPY-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                                 "events": deque(now - i for i in range(1, 11))}
monitor.CHURN["CALM-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                               "events": deque()}

rows = [(Decimal("5.0"), "CHOPPY-USDT"), (Decimal("3.0"), "CALM-USDT"),
        (Decimal("2.0"), "RAT-USDT"), (Decimal("9.0"), "WIDE-USDT")]
for sym, g in (("CHOPPY-USDT", "5.0"), ("CALM-USDT", "3.0"), ("RAT-USDT", "2.0"),
               ("WIDE-USDT", "9.0")):
    assert (Decimal(g), sym) in rows

pick = monitor.least_lossy(rows, now)
check("least_lossy picks the narrowest gap first",
      [r[1] for r in pick] == ["RAT-USDT", "CALM-USDT", "CHOPPY-USDT"],
      str([r[1] for r in pick]))
check("the shortlist caps at three", len(pick) == 3)

monitor.CHURN.clear()
pick = monitor.least_lossy(rows, now)
check("without churn data, narrowest gap still wins",
      [r[1] for r in pick] == ["RAT-USDT", "CALM-USDT", "CHOPPY-USDT"],
      str([r[1] for r in pick]))

# sanity: the original calmest() still prefers the widest gap (GAP mode)
monitor.CHURN.clear()
monitor.CHURN["X-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                            "events": deque()}
monitor.CHURN["Y-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                            "events": deque()}
gap_rows = [(Decimal("1.0"), "X-USDT"), (Decimal("4.0"), "Y-USDT")]
pick = monitor.calmest(gap_rows, now)
check("calmest keeps wide-gap priority for gap mode",
      pick[0][1] == "Y-USDT", str([r[1] for r in pick]))

print()
print("PASS: volume fallback loses least by taking the narrowest spread"
      if ok else "FAIL")
sys.exit(0 if ok else 1)
