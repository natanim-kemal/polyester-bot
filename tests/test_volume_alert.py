"""VIP volume display rounding and milestone crossings.

The 30d figure is shown rounded to the nearest 10k, while milestones use
the exact reading and never replay boundaries after a dip.

  1. the displayed volume rounds to the nearest 10,000,
  2. a full 10k crossing fires once,
  3. a dip below the line does not replay it on the way back up.

Usage:  python tests/test_volume_alert.py
Exit 0 = pass, Exit 1 = fail
"""

import os
import sys
from decimal import Decimal

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

check("the volume shows rounded to the nearest 10k",
      monitor.vol_money(Decimal("8217953.82")) == "8,220,000.00",
      monitor.vol_money(Decimal("8217953.82")))
check("a half step rounds up",
      monitor.vol_money(Decimal("8215000")) == "8,220,000.00",
      monitor.vol_money(Decimal("8215000")))

crossed, nxt = monitor.crossed_milestones(Decimal("8217953.82"), None)
check("the first reading only sets the next line",
      crossed == [] and nxt == 8220000, "%s %s" % (crossed, nxt))

crossed, nxt = monitor.crossed_milestones(Decimal("8220000"), 8220000)
check("a full 10k crossing fires once",
      crossed == [8220000] and nxt == 8230000, "%s %s" % (crossed, nxt))

crossed, nxt = monitor.crossed_milestones(Decimal("8190000"), 8230000)
check("a dip does not replay the line on the way up",
      crossed == [] and nxt == 8230000, "%s %s" % (crossed, nxt))

shown = monitor.vol_next(Decimal("8308872"), 8310000)
check("the next milestone sits above the rounded volume",
      shown == 8320000, str(shown))
shown = monitor.vol_next(Decimal("8310100"), 8320000)
check("a reading at the line still shows the following line",
      shown == 8320000, str(shown))

print()
print("PASS: volume alerts round cleanly and milestones fire once" if ok else "FAIL")
sys.exit(0 if ok else 1)
