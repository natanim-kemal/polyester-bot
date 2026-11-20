"""Whole-book spike detection, replayed against the live XAUT book shape.

A skewed top level alone is not a reason to skip: the real question is whether
the book as a whole has a bid wall supporting price, hollow asks near touch,
and a cluster above with enough size to exit into profit.

Usage:  python tests/test_spike_setup.py
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

BIDS = [
    ("5510.01", "0.001303"), ("5509.91", "0.001251"), ("5123.2", "0.014639"),
    ("4890", "1.439206"), ("3998.69", "0.796334"), ("3998.67", "0.072249"),
    ("3998.61", "0.499424"), ("3625.03", "2.258107"), ("3625", "0.722512"),
    ("3617.18", "186.611341"), ("3617.17", "0.03047"), ("3617.08", "0.539539"),
    ("3604.55", "16.974213"), ("3549.12", "286.028649"),
    ("3515.43", "222.147504"),
]
ASKS = [
    ("5897.63", "0.052366"), ("5897.64", "0.012497"), ("5908.62", "0.001773"),
    ("10000", "1"), ("12879.9", "0.112915"), ("12880", "0.077223"),
    ("12887", "0.188192"), ("12941.68", "0.664528"), ("384862", "0.024531"),
]


class FakeBook:
    bids = [SimpleNamespace(price=SimpleNamespace(as_decimal=lambda p=p: D(p)),
                            qty=SimpleNamespace(as_decimal=lambda q=q: D(q)))
            for p, q in BIDS]
    asks = [SimpleNamespace(price=SimpleNamespace(as_decimal=lambda p=p: D(p)),
                            qty=SimpleNamespace(as_decimal=lambda q=q: D(q)))
            for p, q in ASKS]


async def fake_book(c, sym, depth=10, max_age=15.0):
    return FakeBook()


monitor.book = fake_book

mid = (D("5510.01") + D("5897.63")) / 2
setup = asyncio.run(monitor.spike_setup(None, "XAUT-USDT", mid, D("1500"), D("0.01")))
check("the live XAUT shape is detected", setup is not None, str(setup))
if setup:
    check("the biggest bid wall is the 1M support", setup["wall_px"] == D("3549.12"),
          str(setup["wall_px"]))
    check("the exit target sits just under the ask cluster",
          setup["target"] == D("12879.89"), str(setup["target"]))
    check("the single 10000 ask is not enough on its own",
          setup["target"] != D("9999.99"), str(setup["target"]))

ok_top, kind, why, sf = monitor.gap_guards(D("5510.01"), D("5897.63"),
                                           D("7"), D("383"), D("10665.92"),
                                           D("1500"))
check("top-level skew alone still rejects", not ok_top and "skewed" in why, why)

if setup:
    ok_setup, kind, why, sf = monitor.gap_guards(
        D("5510.01"), D("5897.63"), D("7"), D("383"), D("10665.92"),
        D("1500"), setup)
    check("the whole-book setup clears the skew", ok_setup, why)
    check("the exit target is carried to the sell side", sf == D("12879.89"), str(sf))

ok_fake, kind, why, sf = monitor.gap_guards(D("8668.08"), D("38686.66"),
                                            D("333"), D("7919"), D("3620"),
                                            D("1146"), setup)
check("an inflated bid is still rejected with a setup",
      not ok_fake and "inflated" in why, why)

print()
print("PASS: whole-book spike detection" if ok else "FAIL")
sys.exit(0 if ok else 1)

