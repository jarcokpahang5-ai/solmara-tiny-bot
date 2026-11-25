#!/usr/bin/env python3
"""Solmara Tiny Bot: a tiny paper-trading bot for hourly Solana prices.

Speaks agent-fun/bars-replay/v1 on stdin/stdout (hello, batch, done -> one
reply per batch). Pure Python 3.13 standard library, deterministic, no network.

Strategy: long-only EMA crossover with a long-term trend filter.
  - Per symbol: EMA(8) vs EMA(21), seeded as the mean of the first span of closes.
  - Enter when EMA(8) is above EMA(21), the cross-up happened within the last
    10 bars, and the close sits above the trend line (SMA(48), or the slow EMA
    while the SMA is still seeding, so an early cross is never missed).
  - Exit the whole position when EMA(21) crosses back above EMA(8) (the sell
    signal holds while the slow EMA stays on top), or at +40% take-profit,
    or at -15% stop-loss. After an exit, wait 3 bars before re-entering.
  - Size: equity split over the open slots, capped by max_weight_bps, and
    scaled down when the last 24 bars were wild (volatility-aware sizing),
    and halved while SOL itself trends down (a soft regime filter: the bot
    keeps trading, but risks less when the market leader is weak).
  - A bar with zero volume signals nothing; a bar with no volume recorded is fine.
"""

import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
BPS = 10_000
FAST_SPAN = 8
SLOW_SPAN = 21
TREND_SPAN = 48
ENTRY_WINDOW = 10      # bars after a cross-up during which an entry may fire
REENTRY_WAIT = 3       # bars to wait after an exit before re-entering a symbol
MAX_SLIPPAGE_BPS = 100
TAKE_PROFIT_NUM, TAKE_PROFIT_DEN = 7, 5   # exit at 140% of cost
STOP_LOSS_NUM, STOP_LOSS_DEN = 17, 20      # exit at 85% of cost


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.symbols = {s["symbol"]: s for s in hello["symbols"]}
        self.caps = hello["caps"]
        self.fees = hello["fees"]
        self.quote_scale = hello["capital"]["quote_scale"]
        self.warmup = hello.get("warmup_bars", 0)
        self.seq = -1
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.positions = {}          # symbol -> (qty:int, cost:int)
        self.closes = {s: [] for s in self.symbols}   # symbol -> [close float]
        self.ema_fast = {}           # symbol -> (warm list, value or None)
        self.ema_slow = {}
        self.prev_gap = {}           # symbol -> fast - slow after previous bar
        self.gap = {}                # symbol -> fast - slow after latest bar
        self.down = {}               # symbol -> sell signal held while slow above fast
        self.cross_up_seq = {}       # symbol -> seq of the latest cross-up
        self.last_exit_seq = {}      # symbol -> seq of the latest exit decision
        self._next_id = 1
        self._used_ids = set()

    # -- indicator helpers -------------------------------------------------
    def _push_ema(self, store, symbol, span, close):
        warm, value = store.get(symbol, ([], None))
        if value is None:
            warm.append(close)
            if len(warm) == span:
                value = sum(warm) / span
                warm = []
        else:
            value += (2.0 / (span + 1)) * (close - value)
        store[symbol] = (warm, value)
        return value

    def _push_bar(self, symbol, close):
        self.closes[symbol].append(close)
        fast = self._push_ema(self.ema_fast, symbol, FAST_SPAN, close)
        slow = self._push_ema(self.ema_slow, symbol, SLOW_SPAN, close)
        self.prev_gap[symbol] = self.gap.get(symbol)
        self.gap[symbol] = None if fast is None or slow is None else fast - slow
        prev, cur = self.prev_gap[symbol], self.gap[symbol]
        if prev is not None and cur is not None:
            if cur > 0 and prev <= 0:
                self.cross_up_seq[symbol] = self.seq
            if cur < 0 and prev >= 0:
                self.down[symbol] = True
        if cur is None or cur >= 0:
            self.down[symbol] = False

    def _trend(self, symbol):
        hist = self.closes[symbol]
        if len(hist) >= TREND_SPAN:
            return sum(hist[-TREND_SPAN:]) / TREND_SPAN
        slow = self.ema_slow.get(symbol, ([], None))[1]
        return slow  # slow EMA as the trend line while the SMA seeds

    def _volatility(self, symbol, span=24):
        hist = self.closes[symbol]
        if len(hist) < span + 1:
            return 0.0
        total = 0.0
        count = 0
        for i in range(len(hist) - span, len(hist)):
            prev = hist[i - 1]
            if prev > 0:
                total += abs(hist[i] - prev) / prev
                count += 1
        return total / count if count else 0.0

    # -- money helpers -----------------------------------------------------
    def _value(self, symbol, qty, price):
        s = self.symbols[symbol]
        return qty * price * (10 ** self.quote_scale) // (10 ** (s["qty_scale"] + s["price_scale"]))

    def _client_id(self):
        while True:
            cid = "o%d" % self._next_id
            self._next_id += 1
            if cid not in self._used_ids:
                self._used_ids.add(cid)
                return cid

    # -- one batch ---------------------------------------------------------
    def on_batch(self, batch):
        self.seq = batch["seq"]
        fresh = set()
        for bar in batch["bars"]:
            symbol = bar["symbol"]
            if symbol not in self.symbols:
                continue
            try:
                close = int(bar["c"])
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            fresh.add(symbol)
            scale = self.symbols[symbol]["price_scale"]
            self._push_bar(symbol, close / (10 ** scale))
        account = batch["account"]
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {
            p["symbol"]: (int(p["qty"]), int(p["cost_quote_units"]))
            for p in account["positions"]
        }

        if self.seq < self.warmup:
            return []  # warm-up bars: every order would be rejected

        volumes = {b["symbol"]: b.get("v") for b in batch["bars"]}
        sells = []
        entries = []
        for symbol in self.symbols:
            if symbol not in fresh or volumes.get(symbol) == "0":
                continue
            hist = self.closes[symbol]
            if not hist:
                continue
            close = hist[-1]
            if close <= 0:
                continue
            held = self.positions.get(symbol)
            if held is not None:
                if self._should_exit(symbol, held, close):
                    self.last_exit_seq[symbol] = self.seq
                    sells.append({
                        "side": "sell", "client_id": self._client_id(),
                        "symbol": symbol, "qty": str(held[0]),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    })
            else:
                crossed = self.cross_up_seq.get(symbol)
                if crossed is None or self.seq - crossed > ENTRY_WINDOW:
                    continue
                if self.seq - self.last_exit_seq.get(symbol, -10 ** 9) < REENTRY_WAIT:
                    continue
                trend = self._trend(symbol)
                gap = self.gap.get(symbol)
                if gap is not None and gap > 0 and trend is not None and close > trend:
                    entries.append(symbol)

        orders = sells[: self.caps["max_orders_per_bar"]]
        slots = min(len(self.symbols), self.caps["max_open_positions"])
        if slots <= 0:
            return orders
        stake = self.equity // slots
        by_weight = self.caps["max_weight_bps"] * self.equity // BPS
        if stake > by_weight:
            stake = by_weight
        room = slots - len(self.positions)
        cash = self.cash
        min_notional = int(self.fees["min_notional_quote_units"])
        for symbol in entries:
            if room <= 0 or len(orders) >= self.caps["max_orders_per_bar"]:
                break
            vol = self._volatility(symbol)
            size = stake
            # Soft regime filter: halve the stake while SOL itself trends down.
            sol_hist = self.closes.get("SOL", [])
            sol_trend = self._trend("SOL") if sol_hist else None
            if sol_hist and sol_trend is not None and sol_hist[-1] <= sol_trend:
                size //= 2
            if vol > 0.10:
                size //= 4
            elif vol > 0.06:
                size //= 2
            if size > cash:
                size = cash
            if size <= 0 or size < min_notional:
                continue
            orders.append({
                "side": "buy", "client_id": self._client_id(),
                "symbol": symbol, "notional_quote_units": str(size),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            cash -= size
            room -= 1
        return orders

    def _should_exit(self, symbol, held, close):
        if self.down.get(symbol):
            return True
        qty, cost = held
        if cost <= 0 or qty <= 0:
            return False
        price = int(close * (10 ** self.symbols[symbol]["price_scale"]))
        value = self._value(symbol, qty, price)
        if value * TAKE_PROFIT_DEN >= cost * TAKE_PROFIT_NUM:
            return True
        if value * STOP_LOSS_DEN <= cost * STOP_LOSS_NUM:
            return True
        return False


def main():
    bot = None
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            sys.exit(1)
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                sys.exit(1)
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                sys.exit(1)
            try:
                orders = bot.on_batch(msg)
            except Exception:
                orders = []
            reply = json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":"), ensure_ascii=False)
            out.write(reply + "\n")
            out.flush()
        elif kind == "done":
            return 0
        else:
            sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
