"""Per-asset chase score that parks contested pairs for a while.

An ask chase costs 1, a bail-out costs 2, the score halves every 4 hours,
and a pair at 2 or more is skipped until it decays back under the line.
One bail-out therefore parks the pair immediately; two chases do too.

Usage:  python tests/test_chase_score.py
Exit 0 = pass, Exit 1 = fail
"""

import os
import sys
import tempfile

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

STATE = os.path.join(tempfile.mkdtemp(prefix="polyester-test-"),
                     "chase_state.json")
monitor.state_file = lambda name: STATE
monitor.CHASE_SCORES.clear()

SYM = "ARB-USDT"
NOW = 1000.0

check("a fresh pair is not parked", not monitor.chase_parked(SYM, NOW),
      str(monitor.chase_score(SYM, NOW)))

monitor.chase_note(SYM, monitor.CHASE_ASK_WEIGHT, NOW)
check("one chase is tolerated", not monitor.chase_parked(SYM, NOW),
      str(monitor.chase_score(SYM, NOW)))

monitor.chase_note(SYM, monitor.CHASE_ASK_WEIGHT, NOW)
check("two chases park the pair", monitor.chase_parked(SYM, NOW),
      str(monitor.chase_score(SYM, NOW)))

parked_at = monitor.chase_score(SYM, NOW)
decayed = monitor.chase_score(SYM, NOW + monitor.CHASE_HALF_LIFE)
check("the score halves after 4 hours", abs(parked_at / 2 - decayed) < 1e-9,
      "%s -> %s" % (parked_at, decayed))
check("the pair is tradeable again after the cooldown",
      not monitor.chase_parked(SYM, NOW + monitor.CHASE_HALF_LIFE),
      str(decayed))

monitor.CHASE_SCORES.clear()
monitor.chase_note("BCH-USDT", monitor.CHASE_BAIL_WEIGHT, NOW)
check("one bail-out parks the pair immediately",
      monitor.chase_parked("BCH-USDT", NOW),
      str(monitor.chase_score("BCH-USDT", NOW)))
check("the score survives a restart",
      monitor.chase_load().get("BCH-USDT", (0, 0))[0] == 2.0,
      str(monitor.chase_load()))

print()
print("PASS: contested pairs park themselves and cool down" if ok else "FAIL")
sys.exit(0 if ok else 1)
