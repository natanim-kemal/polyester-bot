"""Telegram slash commands parse, filter and format without touching the API.

Covers the listener core and /volume:
  1. only slash commands parse; /cmd@Bot works,
  2. only TELEGRAM_CHAT_ID is answered,
  3. /volume shows the rounded volume, next line, snapshot time and age,
  4. unknown or broken commands answer instead of going silent.

Usage:  python tests/test_commands.py
Exit 0 = pass, Exit 1 = fail
"""

import os
import sys
import time
from datetime import datetime, timezone
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
    import commands
    import monitor
except Exception as e:
    print("FAIL cannot import commands:", repr(e))
    sys.exit(1)

commands.SRC = monitor

check("plain text is ignored", commands.parse("hello") == (None, ""))
check("a slash command parses", commands.parse("/volume") == ("volume", ""))
check("a bot suffix parses", commands.parse("/volume@PolyBot") == ("volume", ""))
check("arguments are kept", commands.parse("/chase ARB") == ("chase", "ARB"))
check("a bare slash is ignored", commands.parse("/") == (None, ""))

old_chat = os.environ.get("TELEGRAM_CHAT_ID")
os.environ["TELEGRAM_CHAT_ID"] = "424242"
check("other chats are ignored", not commands._allowed({"chat": {"id": 7}}))
check("the configured chat is answered",
      commands._allowed({"chat": {"id": 424242}}))
if old_chat is None:
    os.environ.pop("TELEGRAM_CHAT_ID", None)
else:
    os.environ["TELEGRAM_CHAT_ID"] = old_chat

check("an unknown command answers", commands.reply("nope") == "unknown command")


def boom():
    raise RuntimeError("bad")


commands.COMMANDS["boom"] = ("/boom", boom)
check("a broken handler answers instead of going silent",
      commands.reply("boom") == "command failed: bad")
del commands.COMMANDS["boom"]

check("a fresh loop is ok", commands.health(3) == "ok")
check("a minute late is slow", commands.health(60) == "slow")
check("five minutes late is stuck", commands.health(300) == "stuck")
check("seconds render as seconds", commands.age_text(45) == "45s")
check("minutes render as minutes", commands.age_text(125) == "2m")
check("hours keep the minutes", commands.age_text(7500) == "2h05m")

commands.VOLUME.update(
    vol=Decimal("8308872.438155897150394647"),
    next=None, tier=4,
    as_of=datetime(2026, 11, 14, 7, 0, tzinfo=timezone.utc),
    checked_at=time.time() - 125)
text = commands.reply("volume")
lines = text.splitlines()
check("volume replies in three lines", len(lines) == 3, str(len(lines)))
check("volume rounds to the nearest 10k", "8,310,000.00" in lines[1], lines[1])
check("the next milestone sits above the rounded volume",
      "next 8,320,000" in lines[1], lines[1])
check("the api snapshot time is shown", "API 07:00 UTC" in lines[2], lines[2])
check("the reading age is shown", "checked 2m ago" in lines[2], lines[2])
check("the vip tier is shown", "tier 4" in lines[2], lines[2])

commands.VOLUME.clear()
check("an unread volume says so",
      "no volume reading yet" in commands.reply("volume"))

monitor.CHASE_SCORES.clear()
monitor.CHASE_SCORES["ARB-USDT"] = (2.0, time.time())
monitor.CHASE_SCORES["BCH-USDT"] = (0.5, time.time())
text = commands.reply("chase")
lines = text.splitlines()
check("chase names the tracked and parked counts",
      lines[0] == "🎯 CHASE | 2 tracked | 1 parked", lines[0])
check("chase sorts the hottest pair first",
      lines[1].startswith("ARB-USDT 2.00 | parked "), lines[1])
check("a clear pair says clear", "BCH-USDT 0.50 | clear" in text)
monitor.CHASE_SCORES.clear()
check("an empty chase board says so", "no chase events" in commands.reply("chase"))

print()
print("PASS: telegram commands answer on demand" if ok else "FAIL")
sys.exit(0 if ok else 1)
