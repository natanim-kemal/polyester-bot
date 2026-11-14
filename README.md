<div align="center">

<img src="https://raw.githubusercontent.com/natanim-kemal/polyester-bot/main/polyester-bot.png" alt="Polyester" width="90" height="90" style="border-radius:50%;" />

# Polyester Gap Hunter

**Testnet market-making bot. One position at a time. Telegram alerts.**

</div>

Scans every USDT pair for spreads ≥ `GAP_MIN_PCT`, buys the bid, asks one tick under the
best offer — or falls back to market-making churn when spreads are below the fee bar, to
keep season volume up. Fees are live — read from the exchange (your VIP tier) every 30
minutes — so the gap floor and every cost line use the real rates, not a baked-in 0.20%.

## Install

```bash
npm install -g polyester-bot     # builds its own Python env on first run
polyester-bot setup              # wizard: key, Telegram, writes ~/.polyester-bot/.env
polyester-bot                    # trades testnet
```

Or from source:

```bash
git clone https://github.com/natanim-kemal/polyester-bot.git
cd polyester-bot
pip install -e .
polyester-bot setup
```

or straight from the release tag:

```bash
pip install "polyester-bot @ git+https://github.com/natanim-kemal/polyester-bot.git@v1.2.0"
```

The wizard asks for your API key id + Ed25519 seed, derives the account id itself, and
verifies the exchange and Telegram before writing anything.

## How it works

- **Fees are live** — read from the exchange every 30 min; the gap floor follows them
  (`2 x maker + 0.01` margin, `0.11%` at VIP3). Set `GAP_MIN_PCT` to pin it.
- **Gap mode** — buys the bid, asks under the offer, bot chasing every 2s:
  two contiguous lines per action, Telegram alerts on fills, skips, milestones.
- **Chase (both sides)** — orders are **amended** one tick past a competing ask/bid
  (atomic, order id kept), cancel only when the amend cannot.
- **Volume fallback** — deepest market-maker churn if no profitable gap; escalation
  ladder pennies the bid then crosses the spread as timeouts repeat.
- **Dead-man switch** — `cancel_all_after` re-arms every 60s; if the VM freezes/dies,
  the exchange cancels everything itself.
- **Freeze-resilient** — sandbox pause stops the guest clock; wall-vs-monotonic skew
  >`FREEZE_SECONDS` triggers a full re-sync (cancel own orders, adopt balances).

## Configuration

Everything tunable lives in `bot/.env` (created by `setup`) and documented there.
The ones that matter most:

| Key | Default | Meaning |
|---|---|---|
| `GAP_MIN_PCT` | fee-driven (`0.11`) | spread to take in gap mode |
| `DEPTH_MIN_USD` | `50` | min depth each side to enter |
| `BETTER_ASKS_LIMIT` / `BIDS_LIMIT` | `1` | better prices beating ours to trigger a chase |
| `PERSIST_SECONDS` | `60` | how long those must hold |
| `SAT_PENNY_AFTER` / `SAT_TAKER_AFTER` | `2` / `4` | fallback-ladder triggers |
| `FALLBACK_BUY_TIMEOUT` | `60` | fallback buy timeout on the ladder |
| `DEADMAN_TIMEOUT` | `120` | silent deadline before the exchange cancels everything |
| `FREEZE_SECONDS` | `300` | wall-vs-monotonic skew that counts as a paused sandbox |
| `TP_TRIGGER_PCT` | `0` | optional full-size server-side take-profit (off by default) |

## Alerts

Every alert is 1 header + 1 fact line + 1 money line (`🟢 BUY` / `🔵 ASK` / `✅ CYCLE DONE` /
`🏆 MILESTONE`). Telegram gets everything; journal keeps a copy too. The VIP volume reading
is on demand via `/volume`; only the boot reading and milestone crossings still alert.

## Telegram commands

The bot answers these in `TELEGRAM_CHAT_ID` only. Ask whenever you want a reading; no
automatic volume heartbeat floods the chat.

| Command | Reply |
|---|---|
| `/volume` | VIP 30d settled volume, next milestone, API snapshot time, reading age |
| `/chase` | every tracked pair's chase score and remaining park time |
| `/status` | loop heartbeat (`ok` / `slow` / `stuck`), state, position, open orders, uptime, last cycle and fill |
| `/balance` | USDT plus every held asset marked at mid |
| `/fees` | maker/taker, gap floor, pair count, VIP tier |
| `/help` | the list |

No reply means the process or host is down; nothing runs outside the bot.

## Verify

```bash
for t in tests/test_*.py; do python "$t" || exit 1; done
```

## Release history

- **v1.2.0** — live VIP fee rates, buy-side chase with atomic amend, websocket orderbook,
  liquidity-aware fallback, dead-man switch, daily equity line, opt-in TP triggers,
  orphan/cycle fixes (found on the live book: 500M SHIB stuck).
- v1.1.0 — guided setup wizard; test suites; scanner + re-sync.

## License

MIT
