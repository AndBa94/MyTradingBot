import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.exchange.bybit import BybitClient
from app.models import Position
from app.strategy import SmartStrategy
from app.risk import position_size


class Engine:
    def __init__(self, s, store):
        self.s = s
        self.store = store

        if str(s.environment).lower() != "paper":
            raise RuntimeError("MyTradingBot currently supports PAPER mode only")

        self.client = BybitClient(s.bybit_testnet, s.bybit_category)
        self.strategy = SmartStrategy()

        self.balance = float(s.default_budget)
        self.positions = self.store.load_positions()
        self.last_scan = []
        self.latest_markets = []
        self.previous_orderbooks = {}
        self.scan_stats = {
            "markets_considered": 0,
            "candidates": 0,
            "errors": 0,
        }

        self.running = False
        self.last_error = None
        self.last_scan_at = None
        self.last_action = "Ожидание рынка"
        self.cooldowns = {}
        self.signal_confirmations = {}
        self.recent_closes = {}
        # Circuit breaker for adverse market regimes. Runtime-only by design.
        self.loss_streak = 0
        self.trading_pause_until = None
        self.paper_fee_rate = float(
            getattr(s, "paper_taker_fee_rate", 0.00055)
        )

        saved = self.store.get_settings()
        self.settings = {
            "budget": float(saved.get("budget", self.balance)),
            "leverage": int(saved.get("leverage", s.default_leverage)),
            "take_profits": int(saved.get("take_profits", 3)),
            "max_positions": int(
                saved.get("max_positions", s.max_simultaneous_positions)
            ),
        }

        self.balance = float(
            saved.get("balance", self.settings["budget"])
        )
        self.capital_base = float(
            saved.get("capital_base", self.settings["budget"])
        )
        self.trading_enabled = bool(
            saved.get("trading_enabled", False)
        )

    def _persist_balance(self):
        self.settings["budget"] = float(self.balance)
        self.store.save_settings(
            {
                "budget": self.balance,
                "balance": self.balance,
                "capital_base": self.capital_base,
            }
        )

    async def scan_once(self):
        try:
            markets = await self.client.get_tickers()
            self.latest_markets = markets
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise

        markets = [
            m
            for m in markets
            if m.turnover_24h >= self.s.min_24h_turnover_usdt
            and m.spread_bps <= self.s.max_spread_bps
            and m.bid > 0
            and m.ask > 0
        ]

        markets = sorted(
            markets,
            key=lambda x: x.turnover_24h,
            reverse=True,
        )[:30]

        self.scan_stats = {
            "markets_considered": len(markets),
            "candidates": 0,
            "errors": 0,
        }

        sem = asyncio.Semaphore(10)

        async def analyze_market(m):
            async with sem:
                try:
                    candles, orderbook = await asyncio.gather(
                        self.client.get_klines(m.symbol),
                        self.client.get_orderbook(m.symbol, limit=50),
                    )

                    previous_orderbook = self.previous_orderbooks.get(m.symbol)
                    # Bybit includes the currently forming 5m candle. Using it for
                    # entry logic causes intrabar wick/volume signals to repaint.
                    signal_candles = candles[:-1]
                    opportunity = self.strategy.analyze(
                        m,
                        signal_candles,
                        orderbook,
                        int(self.settings["take_profits"]),
                        previous_orderbook=previous_orderbook,
                    )

                    # Store the previous snapshot only after analysis. This
                    # gives the next scan a real before/after comparison.
                    self.previous_orderbooks[m.symbol] = orderbook
                    return opportunity

                except Exception as exc:
                    self.scan_stats["errors"] += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    return None

        results = await asyncio.gather(
            *(analyze_market(m) for m in markets)
        )

        self.last_scan = sorted(
            [x for x in results if x],
            key=lambda x: x.confidence,
            reverse=True,
        )
        self.scan_stats["candidates"] = len(self.last_scan)
        self.last_scan_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        )

        self.last_action = (
            f"SCAN {len(markets)} markets → "
            f"{len(self.last_scan)} setups"
        )

        return self.last_scan

    async def loop(self):
        self.running = True

        while self.running:
            try:
                await self.scan_once()
                await self.manage_positions()

                if self.trading_enabled:
                    self.auto_enter()

            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"

            await asyncio.sleep(
                max(5, self.s.scan_interval_seconds)
            )

    def _open_symbols(self):
        return {
            p.symbol
            for p in self.positions.values()
            if p.status == "OPEN"
        }

    def _open_side_count(self, side):
        return sum(
            1
            for p in self.positions.values()
            if p.status == "OPEN" and p.side == side
        )

    @staticmethod
    def _reason_value(o, key):
        prefix = f"{key}="
        for reason in getattr(o, "reasons", []):
            if str(reason).startswith(prefix):
                try:
                    return float(str(reason).split("=", 1)[1])
                except (TypeError, ValueError):
                    return None
        return None

    def _directional_cluster_blocked(self, o):
        """Avoid stacking a third highly correlated directional alt bet."""
        if self._open_side_count(o.side) < 2:
            return False

        candidate = next(
            (m for m in self.latest_markets if m.symbol == o.symbol),
            None,
        )
        if not candidate:
            return False

        c_change = float(candidate.change_24h)
        if abs(c_change) < 0.5:
            return False

        open_markets = {
            m.symbol: m
            for m in self.latest_markets
        }
        same_direction = 0

        for p in self.positions.values():
            if p.status != "OPEN" or p.side != o.side:
                continue
            m = open_markets.get(p.symbol)
            if not m:
                continue
            change = float(m.change_24h)
            if abs(change) >= 0.5 and change * c_change > 0:
                same_direction += 1

        return same_direction >= 2

    def _market_context_blocked(self, o):
        """Block alt entries when BTC and ETH are moving together against the setup."""
        if o.symbol in {"BTCUSDT", "ETHUSDT"}:
            return False

        markets = {m.symbol: m for m in self.latest_markets}
        btc = markets.get("BTCUSDT")
        eth = markets.get("ETHUSDT")
        if not btc or not eth:
            return False

        btc_change = float(btc.change_24h)
        eth_change = float(eth.change_24h)

        # Require both majors to agree before applying the broad-market filter.
        if o.side == "LONG":
            return btc_change <= -1.0 and eth_change <= -1.0
        return btc_change >= 1.0 and eth_change >= 1.0

    def _signal_confirmed(self, o, now):
        key = (o.symbol, o.side, o.setup)
        stale = [
            k for k, v in self.signal_confirmations.items()
            if (now - v["at"]).total_seconds() > 45
        ]
        for k in stale:
            self.signal_confirmations.pop(k, None)

        previous = self.signal_confirmations.get(key)
        if not previous:
            self.signal_confirmations[key] = {
                "count": 1,
                "at": now,
                "entry": float(o.entry),
                "confidence": float(o.confidence),
                "book_imbalance": self._reason_value(o, "book_imbalance"),
            }
            return False

        current_imbalance = self._reason_value(o, "book_imbalance")
        previous_imbalance = previous.get("book_imbalance")
        if current_imbalance is None or previous_imbalance is None:
            self.signal_confirmations.pop(key, None)
            return False

        if o.side == "LONG":
            microstructure_holds = (
                current_imbalance >= 0.54
                and previous_imbalance >= 0.53
                and current_imbalance >= previous_imbalance - 0.04
            )
        else:
            microstructure_holds = (
                current_imbalance <= 0.46
                and previous_imbalance <= 0.47
                and current_imbalance <= previous_imbalance + 0.04
            )

        if not microstructure_holds:
            self.signal_confirmations.pop(key, None)
            return False

        baseline_entry = float(previous["entry"])
        risk = abs(float(o.entry) - float(o.stop_loss))
        if risk <= 0 or baseline_entry <= 0:
            self.signal_confirmations.pop(key, None)
            return False

        favorable_move = (
            float(o.entry) - baseline_entry
            if o.side == "LONG"
            else baseline_entry - float(o.entry)
        )

        # The signal must actually improve between scans. A repeated static
        # order-book snapshot is not confirmation. We only need a small move
        # (about 0.08R), so the confirmation does not chase a large breakout.
        min_confirmation_move = max(
            risk * 0.08,
            baseline_entry * 0.00015,
        )
        max_chase_move = max(
            risk * 0.45,
            baseline_entry * 0.0010,
        )

        confidence_holds = (
            float(o.confidence) >= float(previous["confidence"]) - 0.03
        )
        confirmed = (
            favorable_move >= min_confirmation_move
            and favorable_move <= max_chase_move
            and confidence_holds
            and microstructure_holds
        )

        if confirmed:
            previous["count"] += 1
            previous["at"] = now
            previous["entry"] = float(o.entry)
            previous["confidence"] = float(o.confidence)
            previous["book_imbalance"] = current_imbalance
            return True

        # Confirmation failed: start a fresh baseline from the current signal.
        # This prevents an old signal from becoming an entry several scans later.
        self.signal_confirmations[key] = {
            "count": 1,
            "at": now,
            "entry": float(o.entry),
            "confidence": float(o.confidence),
            "book_imbalance": current_imbalance,
        }
        return False

    def _entry_blocked_after_close(self, symbol, now):
        closed = self.recent_closes.get(symbol)
        if not closed:
            return False

        closed_at, side, reason = closed
        age = (now - closed_at).total_seconds()

        if reason == "STOP_LOSS" and age < 600:
            return True
        if reason != "STOP_LOSS" and age < 180:
            return True
        return False

    def auto_enter(self):
        if not self.trading_enabled:
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if self.trading_pause_until:
            if now < self.trading_pause_until:
                remaining = int(
                    (self.trading_pause_until - now).total_seconds()
                )
                self.last_action = (
                    f"AUTO PAUSE after {self.loss_streak} losses | "
                    f"{max(0, remaining)}s left"
                )
                return
            self.loss_streak = min(self.loss_streak, 2)
            self.trading_pause_until = None

        max_positions = int(self.settings["max_positions"])

        if len(self.positions) >= max_positions:
            return

        for o in self.last_scan:
            if len(self.positions) >= max_positions:
                break

            if o.confidence < float(self.s.auto_min_confidence):
                continue

            if not self._signal_confirmed(o, now):
                continue

            if self._entry_blocked_after_close(o.symbol, now):
                continue

            if self._directional_cluster_blocked(o):
                continue

            if self._market_context_blocked(o):
                continue

            if o.symbol in self._open_symbols():
                continue

            # Limit directional concentration. Several altcoins can move
            # together, so multiple positions are not independent bets.
            if self._open_side_count(o.side) >= int(
                getattr(self.s, "max_same_direction_positions", 2)
            ):
                continue

            last_entry = self.cooldowns.get(o.symbol)
            if (
                last_entry
                and (now - last_entry).total_seconds()
                < self.s.auto_cooldown_seconds
            ):
                continue

            p = self.open_paper(o, automatic=True)

            if p:
                self.cooldowns[o.symbol] = now
                self.last_action = (
                    f"AUTO {p.side} {p.symbol} | "
                    f"conf {o.confidence * 100:.0f}%"
                )

    def open_paper(self, o, automatic=False):
        if str(self.s.environment).lower() != "paper":
            return None

        if not self.trading_enabled:
            return None

        if len(self.positions) >= int(self.settings["max_positions"]):
            return None

        if o.symbol in self._open_symbols():
            return None

        if automatic and self._entry_blocked_after_close(
            o.symbol,
            datetime.now(timezone.utc).replace(tzinfo=None),
        ):
            return None

        if self._open_side_count(o.side) >= int(
            getattr(self.s, "max_same_direction_positions", 2)
        ):
            return None

        leverage = int(self.settings["leverage"])

        q = position_size(
            self.balance,
            self.s.risk_per_trade,
            o.entry,
            o.stop_loss,
            leverage,
            fee_rate=self.paper_fee_rate,
            stop_slippage_rate=float(
                getattr(self.s, "paper_stop_slippage_rate", 0.001)
            ),
        )

        used_margin = sum(
            abs(p.entry * p.quantity) / max(1, p.leverage)
            for p in self.positions.values()
            if p.status == "OPEN"
        )

        available_margin = max(
            0.0,
            self.balance - used_margin,
        )

        margin_cap_qty = (
            available_margin * leverage / o.entry
        )

        q = min(q, margin_cap_qty)

        if q <= 0:
            return None

        tps = o.take_profits[
            : int(self.settings["take_profits"])
        ]

        # Defense-in-depth: never allow a stale/manual opportunity to bypass
        # the strategy's minimum 1.20R first-target rule.
        risk = abs(o.entry - o.stop_loss)
        if risk <= 0 or not tps:
            return None
        if abs(tps[0] - o.entry) < risk * 1.20:
            return None

        entry_fee = (
            abs(o.entry * q) * self.paper_fee_rate
        )

        if entry_fee >= self.balance:
            return None

        p = Position(
            str(uuid.uuid4()),
            o.symbol,
            o.side,
            o.entry,
            q,
            o.stop_loss,
            tps,
            datetime.utcnow(),
            leverage,
            pnl=-entry_fee,
            initial_quantity=q,
            last_price=o.entry,
            initial_stop_loss=o.stop_loss,
            entry_fee=entry_fee,
            fees=entry_fee,
            realized_pnl=-entry_fee,
        )

        self.balance -= entry_fee
        self._persist_balance()

        self.positions[p.id] = p
        self.store.save_position(p)

        if automatic:
            self.last_action = (
                f"AUTO OPEN {p.symbol} {p.side}"
            )

        return p

    def _sync_position_tps(self, p):
        """Never rebuild targets from a different formula after entry."""
        count = max(
            1,
            min(5, int(self.settings["take_profits"])),
        )

        levels = list(p.take_profits)
        completed = max(0, int(p.tp_index))

        if not levels:
            self.store.save_position(p)
            return

        if completed >= len(levels):
            # All targets have already been consumed. Do not create a fake
            # target from last_price; the position should be closed instead.
            self._finish_position(
                p,
                p.last_price or p.entry,
                "TAKE_PROFIT",
            )
            return

        remaining = levels[completed:]
        keep = remaining[:count]

        p.take_profits = levels[:completed] + keep
        p.tp_index = completed

        self.store.save_position(p)

    def _sync_all_open_tps(self):
        for p in list(self.positions.values()):
            if p.status == "OPEN":
                self._sync_position_tps(p)

    def _unrealized(self, p, price, quantity=None):
        q = p.quantity if quantity is None else quantity

        return (
            (price - p.entry) * q
            if p.side == "LONG"
            else (p.entry - price) * q
        )

    def _net_unrealized(self, p, price, quantity=None):
        q = p.quantity if quantity is None else quantity

        return (
            self._unrealized(p, price, q)
            - abs(price * q) * self.paper_fee_rate
        )

    async def manage_positions(self):
        if not self.positions or not self.latest_markets:
            return

        self._sync_all_open_tps()

        tickers = {
            m.symbol: m
            for m in self.latest_markets
        }

        for p in list(self.positions.values()):
            m = tickers.get(p.symbol)

            if not m:
                continue

            price = (
                m.bid
                if p.side == "LONG"
                else m.ask
            )

            p.last_price = price
            p.pnl = (
                p.realized_pnl
                + self._net_unrealized(p, price)
            )

            hit_sl = (
                price <= p.stop_loss
                if p.side == "LONG"
                else price >= p.stop_loss
            )

            if hit_sl:
                self._finish_position(
                    p,
                    price,
                    "STOP_LOSS",
                )
                continue

            if p.tp_index < len(p.take_profits):
                target = p.take_profits[p.tp_index]

                hit_tp = (
                    price >= target
                    if p.side == "LONG"
                    else price <= target
                )

                if hit_tp:
                    self._take_profit(p, target)

                    if p.id not in self.positions:
                        continue

            age = datetime.utcnow() - p.opened_at

            if age >= timedelta(
                minutes=self.s.max_hold_minutes
            ):
                self._finish_position(
                    p,
                    price,
                    "TIME_EXIT",
                )
                continue

            self.store.save_position(p)

    def _take_profit(self, p, price):
        remaining_tps = (
            len(p.take_profits) - p.tp_index
        )

        if remaining_tps <= 1:
            self._finish_position(
                p,
                price,
                "TAKE_PROFIT",
            )
            return

        if remaining_tps >= 3:
            # 25% / 25% / 50% for a three-target runner.
            qty_to_close = p.initial_quantity * 0.25
        elif remaining_tps == 2:
            # Once TP1 is gone, split the remainder evenly enough to preserve
            # a meaningful runner without overcomplicating the state model.
            qty_to_close = p.initial_quantity * 0.25
        else:
            qty_to_close = p.quantity

        qty_to_close = min(
            qty_to_close,
            p.quantity,
        )

        gross_realized = self._unrealized(
            p,
            price,
            qty_to_close,
        )

        exit_fee = (
            abs(price * qty_to_close)
            * self.paper_fee_rate
        )

        realized = gross_realized - exit_fee

        p.realized_pnl += realized
        p.fees += exit_fee
        p.quantity -= qty_to_close

        self.balance += realized
        self._persist_balance()

        p.tp_index += 1
        p.pnl = (
            p.realized_pnl
            + self._net_unrealized(p, price)
        )

        # Protect a portion of the first winner while leaving the
        # runner room. With a 3+ TP plan the first exit is 25%, the second
        # 25%, and the final target carries the remaining 50%.
        if p.tp_index == 1:
            initial_risk = abs(
                p.entry - p.initial_stop_loss
            )
            if p.side == "LONG":
                p.stop_loss = p.entry + initial_risk * 0.10
            else:
                p.stop_loss = p.entry - initial_risk * 0.10
        elif p.tp_index > 1:
            prev_tp = p.take_profits[
                p.tp_index - 1
            ]
            p.stop_loss = prev_tp

        self.store.save_position(p)

        self.last_action = (
            f"TP{p.tp_index} {p.symbol} | "
            f"{realized:+.2f} USDT net | "
            f"fee {exit_fee:.4f}"
        )

    def _finish_position(self, p, price, reason):
        if p.id not in self.positions:
            return

        gross_remaining = self._unrealized(
            p,
            price,
            p.quantity,
        )

        exit_fee = (
            abs(price * p.quantity)
            * self.paper_fee_rate
        )

        net_remaining = (
            gross_remaining - exit_fee
        )

        final_pnl = (
            p.realized_pnl + net_remaining
        )

        p.fees += exit_fee
        p.pnl = final_pnl
        p.last_price = price
        p.status = "CLOSED"

        self.store.add_trade(
            p,
            price,
            final_pnl,
            reason,
            p.fees,
        )

        self.balance += net_remaining
        self._persist_balance()

        # Consecutive-loss circuit breaker: stop adding risk when the market
        # produces a cluster of failed signals.
        if final_pnl < 0:
            self.loss_streak += 1
            pause_minutes = 0
            if self.loss_streak == 3:
                pause_minutes = 15
            elif self.loss_streak == 4:
                pause_minutes = 30
            elif self.loss_streak >= 5:
                pause_minutes = 60

            if pause_minutes:
                self.trading_pause_until = (
                    datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(minutes=pause_minutes)
                )
        else:
            self.loss_streak = 0
            self.trading_pause_until = None

        self.recent_closes[p.symbol] = (
            datetime.utcnow(),
            p.side,
            reason,
        )
        self.signal_confirmations = {
            k: v for k, v in self.signal_confirmations.items()
            if k[0] != p.symbol
        }

        del self.positions[p.id]
        self.store.delete_position(p.id)

        self.last_action = (
            f"CLOSE {p.symbol} | {reason} | "
            f"{final_pnl:+.2f} USDT | "
            f"fee {p.fees:.4f}"
        )

    async def refresh_positions(self):
        if not self.positions:
            return

        try:
            tickers = {
                m.symbol: m
                for m in await self.client.get_tickers()
            }

            for p in self.positions.values():
                m = tickers.get(p.symbol)

                if not m:
                    continue

                price = (
                    m.bid
                    if p.side == "LONG"
                    else m.ask
                )

                p.last_price = price
                p.pnl = (
                    p.realized_pnl
                    + self._net_unrealized(p, price)
                )

        except Exception as exc:
            self.last_error = (
                f"{type(exc).__name__}: {exc}"
            )

    def close_paper(self, position_id, reason="MANUAL"):
        p = self.positions.get(position_id)

        if not p:
            return None

        price = p.last_price or p.entry

        self._finish_position(
            p,
            price,
            reason,
        )

        return p

    def update_settings(self, data):
        old_budget = self.settings["budget"]

        if "budget" in data:
            self.settings["budget"] = max(
                1.0,
                float(data["budget"]),
            )

        if "leverage" in data:
            self.settings["leverage"] = max(
                1,
                min(20, int(data["leverage"])),
            )

        if "take_profits" in data:
            self.settings["take_profits"] = max(
                1,
                min(5, int(data["take_profits"])),
            )

        if "max_positions" in data:
            self.settings["max_positions"] = max(
                1,
                min(5, int(data["max_positions"])),
            )

        if self.settings["budget"] != old_budget:
            delta = (
                self.settings["budget"]
                - old_budget
            )
            new_balance = self.balance + delta

            if new_balance < 0:
                self.settings["budget"] = old_budget
                raise ValueError(
                    "budget withdrawal exceeds available PAPER balance"
                )

            self.balance = new_balance
            self.capital_base = self.settings["budget"]

        self._sync_all_open_tps()

        self.store.save_settings(
            {
                **self.settings,
                "balance": self.balance,
                "capital_base": self.capital_base,
            }
        )

        return self.settings

    def account_snapshot(self):
        today = (
            datetime.now(timezone.utc)
            .replace(tzinfo=None)
            .date()
        )

        history = self.store.all_history()

        today_pnl = sum(
            float(x["pnl"])
            for x in history
            if str(x["closed_at"])[:10]
            == today.isoformat()
        )

        closed_pnl = sum(
            float(x["pnl"])
            for x in history
        )

        unrealized = sum(
            self._net_unrealized(
                p,
                p.last_price or p.entry,
            )
            for p in self.positions.values()
        )

        return {
            "balance": self.balance,
            "initial_budget": self.capital_base,
            "today_pnl": today_pnl,
            "closed_pnl": closed_pnl,
            "unrealized_pnl": unrealized,
            "equity": self.balance + unrealized,
            "scan_stats": self.scan_stats,
        }

    def reset_paper(self):
        self.positions.clear()
        self.store.clear_positions()
        self.last_scan = []
        self.previous_orderbooks.clear()
        self.cooldowns.clear()
        self.signal_confirmations.clear()
        self.recent_closes.clear()

        self.balance = self.capital_base
        self.settings["budget"] = self.capital_base
        self.trading_enabled = False
        self.last_action = "PAPER сброшен"

        self.store.reset()

        self.store.save_settings(
            {
                **self.settings,
                "budget": self.balance,
                "balance": self.balance,
                "capital_base": self.capital_base,
                "trading_enabled": False,
            }
        )

    def set_trading(self, enabled):
        self.trading_enabled = bool(enabled)

        self.store.save_settings(
            {
                "trading_enabled": self.trading_enabled
            }
        )

        self.last_action = (
            "Автоторговля включена"
            if self.trading_enabled
            else "Автоторговля остановлена"
        )

        return self.trading_enabled
