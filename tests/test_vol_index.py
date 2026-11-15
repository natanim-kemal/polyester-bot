"""Volatility index: WS top-of-book churn and the calm-pair shortlist.

Covers:
  1. the first sighting of a book is not an event,
  2. top-of-book changes are counted and fall out after CHURN_WINDOW,
  3. calmest-first ordering (ties broken by gap), shortlist capped at 3,
  4. the 3-minute cycle gate on chase scoring.

Usage:  python tests/test_vol_index.py
Exit 0 = pass, Exit 1 = fail
"""

import os
import sys
import tempfile
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

monitor.CHURN.clear()
T = 1000.0

monitor.churn_note("X-USDT", Decimal("0.1"), Decimal("0.2"), T)
check("the first sighting is not an event", monitor.churn("X-USDT", T) == 0.0)
monitor.churn_note("X-USDT", Decimal("0.1"), Decimal("0.2"), T + 1)
check("an unchanged book is silent", monitor.churn("X-USDT", T + 1) == 0.0)
monitor.churn_note("X-USDT", Decimal("0.11"), Decimal("0.2"), T + 61)
monitor.churn_note("X-USDT", Decimal("0.11"), Decimal("0.21"), T + 121)
monitor.churn_note("X-USDT", Decimal("0.12"), Decimal("0.21"), T + 181)
check("three changes are counted",
      monitor.churn("X-USDT", T + 181) == 0.3, str(monitor.churn("X-USDT", T + 181)))
check("events older than the window fall out",
      monitor.churn("X-USDT", T + 700) == 0.2, str(monitor.churn("X-USDT", T + 700)))
check("unknown pairs rank clean", monitor.churn("NOPE-USDT", T + 700) == 0.0)

monitor.CHURN.clear()
now = 10_000.0
MON = monitor.CHURN_WINDOW
monitor.CHURN["CALM-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                               "events": deque()}
monitor.CHURN["MID-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                              "events": deque([now - 10, now - 20, now - 30])}
monitor.CHURN["CHOP-USDT"] = {"bid": Decimal("1"), "ask": Decimal("2"),
                               "events": deque(now - i for i in range(1, 13))}
rows = [(Decimal("5.0"), "CHOP-USDT"), (Decimal("3.0"), "MID-USDT"),
        (Decimal("5.0"), "CALM-USDT"), (Decimal("1.0"), "ETC-USDT")]
picked = [r[1] for r in monitor.calmest(rows, now)]
check("calmest first, shortlist holds the calm names",
      picked == ["CALM-USDT", "ETC-USDT", "MID-USDT"], str(picked))
check("the shortlist caps at three", len(monitor.calmest(rows, now)) == 3)

check("a fresh cycle is not scored yet",
      not monitor.chase_allowed({"buy_at": now - 30}, now))
check("an adopted position starts over",
      not monitor.chase_allowed({"since": now - 60}, now))
check("a cycle three minutes in does score",
      monitor.chase_allowed({"buy_at": now - 180}, now))
check("an adopted position three minutes in does score",
      monitor.chase_allowed({"since": now - 181}, now))

STATE = os.path.join(tempfile.mkdtemp(prefix="polyester-test-"),
                     "chase_state.json")
monitor.state_file = lambda name: STATE
monitor.CHASE_SCORES.clear()
monitor.CALM_STREAK.clear()
T = 1000.0
monitor.CHASE_SCORES["ARB-USDT"] = (2.0, T)
rows3 = [(Decimal("0.5"), "ARB-USDT"), (Decimal("0.4"), "BCH-USDT"),
         (Decimal("0.3"), "BONK-USDT")]
monitor.calm_streak_update(rows3, T)
check("one calm scan does not clear the park",
      monitor.chase_score("ARB-USDT", T) >= monitor.CHASE_PARK_SCORE)
monitor.calm_streak_update(rows3, T + 290)
check("under five minutes the park stays",
      monitor.chase_score("ARB-USDT", T + 290) >= monitor.CHASE_PARK_SCORE)
monitor.calm_streak_update(rows3, T + 300)
check("five calm minutes clear the park",
      monitor.chase_score("ARB-USDT", T + 300) == 0.0,
      str(monitor.chase_score("ARB-USDT", T + 300)))
monitor.CHASE_SCORES["BCH-USDT"] = (2.0, T)
monitor.CALM_STREAK.clear()
monitor.calm_streak_update(rows3, T)
check("a streak is tracked while in the shortlist", "BCH-USDT" in monitor.CALM_STREAK)
rows_no_bch = [(Decimal("0.5"), "ARB-USDT"), (Decimal("0.4"), "BONK-USDT"),
               (Decimal("0.3"), "ETC-USDT")]
monitor.calm_streak_update(rows_no_bch, T + 30)
check("leaving the shortlist resets the streak", "BCH-USDT" not in monitor.CALM_STREAK)
monitor.CHASE_SCORES.clear()

print()
print("PASS: the bot trades the calmest books first" if ok else "FAIL")
sys.exit(0 if ok else 1)
