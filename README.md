# Solmara Tiny Bot

[![make.fun · u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/jarcokpahang5-ai/solmara-tiny-bot/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/jarcokpahang5-ai/solmara-tiny-bot/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

A tiny paper-trading bot for hourly Solana prices, built for the
[make.fun "Tiny Bot, Easy $2" bounty](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1).
It speaks the `agent-fun/bars-replay/v1` line protocol on stdin/stdout and
trades FARTCOIN, JITOSOL, POPCAT, RAY, SOL, WETH and WIF against USDC.

## Strategy

Long-only EMA crossover with a long-term trend filter, in pure Python 3.13
standard library (no third-party packages, no network, deterministic):

- **Enter** when EMA(8) crosses above EMA(21), the cross is fresh (within the
  last 10 bars), and the close sits above the trend line (SMA(48), or the slow
  EMA while the SMA is still seeding).
- **Exit** the whole position when EMA(21) crosses back above EMA(8), or at
  +40% take-profit, or at −15% stop-loss; after an exit it waits 3 bars
  before re-entering the same symbol.
- **Size** is equity split over the open slots, capped by `max_weight_bps`,
  scaled down when the last 24 bars were volatile — and halved while SOL
  itself trends down (a soft regime filter).

## Local score (dev window, `grade-local`)

| Bot | Net PnL (USDC) | Trades | Max drawdown |
| --- | --- | --- | --- |
| **Solmara Tiny Bot** | **−86.44** | 22 | 1.26% |
| ema-cross baseline | −173.04 | 28 | 2.51% |
| buy-and-hold baseline | −869.34 | 7 | — |

## Run it

```sh
python3 main.py   # reads agent-fun/bars-replay/v1 lines on stdin, replies on stdout
```

## License

[MIT](LICENSE)
