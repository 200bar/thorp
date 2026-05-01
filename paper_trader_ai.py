"""
Paper Trading Bot v3 — Claude AI decision engine on WebSocket data.

Claude Haiku analyzes the market every N seconds and decides BUY UP /
BUY DOWN / HOLD / SELL with a confidence score. Trades only execute
when confidence exceeds the threshold and the price is in the
[0.30, 0.70] band.

Optional patience mode: cooldown after losses + raised confidence
threshold + time-based decay of consecutive losses.

Usage:
    source .venv/bin/activate
    export ANTHROPIC_API_KEY=sk-ant-...
    python paper_trader_ai.py --coin ETH --size 10
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime
from typing import Dict, Optional

import aiohttp
import anthropic

from src.gamma_client import GammaClient
from src.paper_trading import (
    PaperConfig,
    PaperTraderBase,
    make_executor,
    with_retry_async,
)
from src.websocket_client import MarketWebSocket, OrderbookSnapshot

logging.getLogger("src.websocket_client").setLevel(logging.WARNING)


# Binance spot symbol mapping for context price lookup. Strategy-specific
# constant (paper_trader.py and paper_trader_ws.py don't use Binance).
SPOT_SYMBOLS = {
    "ETH": "ETHUSDT",
    "BTC": "BTCUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


class ClaudeTrader(PaperTraderBase):
    """Paper trading where Claude AI makes the buy/sell decisions."""

    def __init__(
        self,
        config: PaperConfig,
        confidence_threshold: int = 70,
        ai_interval: float = 30.0,
        patience: bool = True,
        max_session_losses: int = 4,
        extreme_skip_band: tuple = (0.20, 0.80),
        cooldown_base: float = 60.0,
        loss_decay_seconds: float = 180.0,
        confidence_bump: int = 5,
        run_name: str = "",
        executor=None,
    ):
        super().__init__(config, executor=executor)

        self.confidence_threshold = confidence_threshold
        self.ai_interval = ai_interval
        self.patience = patience
        self.max_session_losses = max_session_losses
        self.extreme_low, self.extreme_high = extreme_skip_band
        self.run_name = run_name

        # Patience tuning constants (now configurable from YAML)
        self.cooldown_base = cooldown_base       # base cooldown after closing position
        self.confidence_bump = confidence_bump   # extra confidence per consecutive loss
        self.loss_decay_seconds = loss_decay_seconds  # reset 1 loss every N seconds idle

        # Strategy state
        self.last_trade_time: float = 0.0
        self.consecutive_losses: int = 0
        self.session_locked: bool = False
        self.ai_calls: int = 0
        self.ai_cost_usd: float = 0.0
        self.spot_price: Optional[float] = None
        self.current_question: str = ""
        self.token_to_side: Dict[str, str] = {}
        self.needs_reconnect = False

        # Components
        self.gamma = GammaClient()
        self.ws = MarketWebSocket()
        self.claude = anthropic.Anthropic()

    # --- Hooks: extend base behavior --------------------------------------

    def _on_trade_closed(self, trade: dict) -> None:
        """Update patience-mode state after every trade close."""
        self.last_trade_time = time.time()
        if trade["pnl"] < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def _summary_extras(self) -> list:
        return [
            f"AI calls: {self.ai_calls} | AI cost: ${self.ai_cost_usd:.4f}",
            f"Confidence threshold: {self.confidence_threshold}%",
        ]

    def _trade_log_extra(self, trade: dict) -> str:
        return f"AI: {trade.get('entry_reason', '')}"

    # --- Spot price (Binance) -------------------------------------------

    async def fetch_spot_price(self) -> None:
        """Fetch live spot price from Binance for AI context. Best-effort —
        on failure, log and keep last known price (price is informational only).
        """
        symbol = SPOT_SYMBOLS.get(self.config.coin)
        if not symbol:
            return
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.spot_price = float(data["price"])
                    else:
                        self.log(
                            f"Binance spot price returned HTTP {resp.status}",
                            "WARN",
                        )
        except Exception as e:
            # Reguła 12 partial: not raising (spot price is non-critical),
            # but logging so the failure is visible (no more silent `pass`).
            self.log(f"Spot price fetch failed: {e}", "WARN")

    # --- Claude prompting -----------------------------------------------

    def get_market_context(self) -> str:
        """Build the market context block injected into Claude's prompt."""
        up = self.current_prices.get("up", 0)
        down = self.current_prices.get("down", 0)

        up_recent = list(self.price_history["up"])[-20:]
        down_recent = list(self.price_history["down"])[-20:]

        up_trend = ""
        if len(up_recent) >= 5:
            first_5 = sum(up_recent[:5]) / 5
            last_5 = sum(up_recent[-5:]) / 5
            if last_5 > first_5 + 0.02:
                up_trend = "RISING"
            elif last_5 < first_5 - 0.02:
                up_trend = "FALLING"
            else:
                up_trend = "STABLE"

        pos_info = "No open position."
        if self.position:
            current = self.current_prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            hold = time.time() - self.position.entry_time
            pos_info = (
                f"Open position: {self.position.side.upper()} "
                f"entry={self.position.entry_price:.4f} "
                f"current={current:.4f} PnL=${pnl:+.2f} "
                f"hold={hold:.0f}s "
                f"TP={self.position.entry_price + self.config.take_profit:.4f} "
                f"SL={self.position.entry_price - self.config.stop_loss:.4f}"
            )

        spot_info = (
            f"Live {self.config.coin} spot price (Binance): ${self.spot_price:,.2f}"
            if self.spot_price
            else f"Live {self.config.coin} spot price: unavailable"
        )

        history_info = "No trades yet this session."
        if self.trades:
            stats = self.get_stats()
            lines = [
                f"Session: {stats['total']} trades, {stats['wins']}W/{stats['losses']}L, "
                f"win rate {stats['win_rate']:.0f}%, PnL ${stats['total_pnl']:+.2f}"
            ]
            for t in self.trades[-10:]:
                lines.append(
                    f"  {t['side'].upper()} entry={t['entry']:.4f} exit={t['exit']:.4f} "
                    f"PnL=${t['pnl']:+.2f} {t['reason']} | reason: {t.get('entry_reason', '')}"
                )
            history_info = "\n".join(lines)

        return f"""Market: {self.current_question}
Coin: {self.config.coin}
{spot_info}
Current prices: UP={up:.4f} DOWN={down:.4f}
UP trend (last 20 ticks): {up_trend}
Recent UP prices: {[f'{p:.4f}' for p in up_recent[-10:]]}
Recent DOWN prices: {[f'{p:.4f}' for p in down_recent[-10:]]}
{pos_info}
WS updates so far: {self.update_count}

Trading history:
{history_info}"""

    async def ask_claude(self) -> Optional[dict]:
        """Ask Claude for a trading decision. Returns parsed dict or None.

        Network/rate-limit errors get retried with backoff (with_retry_async).
        After exhausting retries we log and return None — the AI loop skips
        this tick rather than crashing the bot. This is a deliberate
        compromise on Reguła 12: log loudly but recover, because Claude API
        rate limits are an expected runtime condition, not a programming bug.
        """
        context = self.get_market_context()

        prompt = f"""You are a prediction market trading bot analyzing a 15-minute crypto Up/Down market on Polymarket.

{context}

This is a binary market: if {self.config.coin} price goes UP in this 15-minute window, "UP" token pays $1. If DOWN, "DOWN" token pays $1.

RULE: Do NOT recommend buying if the token price is > 0.70 (too expensive, limited upside) or < 0.30 (too cheap/risky, likely losing side). Best value is in the 0.35-0.65 range.

LEARN from your trading history above. Notice which trades won/lost and why. Avoid repeating losing patterns. Double down on what works.

Analyze the current market state and decide:
1. Should I BUY UP, BUY DOWN, or HOLD?
2. What is your confidence (0-100)?
3. Brief reason (1 sentence).

If I already have an open position, you can also suggest HOLD (keep position) or SELL (close early).

Respond ONLY with valid JSON:
{{"action": "BUY_UP" | "BUY_DOWN" | "HOLD" | "SELL", "confidence": 0-100, "reason": "..."}}"""

        async def call_claude():
            return await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            response = await with_retry_async(call_claude, log_fn=self.log)
        except Exception as e:
            # 3 retries exhausted; skip this tick rather than crash
            self.log(f"Claude API permanently failed: {e}", "WARN")
            return None

        self.ai_calls += 1
        # Estimate cost (Haiku 4.5: ~$1/M input, ~$5/M output)
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = (input_tokens * 1.0 + output_tokens * 5.0) / 1_000_000
        self.ai_cost_usd += cost

        text = response.content[0].text.strip()
        # Strip optional markdown JSON fence
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            self.log(f"Claude returned non-JSON: {text[:100]}", "WARN")
            return None

    # --- WS callback ----------------------------------------------------

    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        self.update_count += 1
        asset_id = snapshot.asset_id
        side = self.token_to_side.get(asset_id)
        if not side:
            return
        mid = snapshot.mid_price
        self.current_prices[side] = mid
        self.price_history[side].append(mid)

        # Let the executor inspect the book — taker no-op, maker checks
        # whether pending limit orders should fill.
        self.executor.on_book_update(self, snapshot)

        # TP/SL on active position
        if self.position and self.position.side == side:
            if self.position.should_take_profit(mid):
                print()
                self.paper_sell(mid, "TAKE PROFIT")
            elif self.position.should_stop_loss(mid):
                print()
                self.paper_sell(mid, "STOP LOSS")

        if self.update_count % 20 == 0:
            self.print_status()

    # --- Status line ----------------------------------------------------

    def print_status(self) -> None:
        up = self.current_prices.get("up", 0)
        down = self.current_prices.get("down", 0)
        pos_str = ""
        if self.position:
            current = self.current_prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            pos_str = f" | POS: {self.position.side.upper()} PnL: ${pnl:+.2f}"
        stats = self.get_stats()
        spot_str = (
            f" SPOT=${self.spot_price:,.0f}" if self.spot_price else ""
        )
        text = (
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.config.coin}{spot_str} UP={up:.4f} DOWN={down:.4f}"
            f"{pos_str} | "
            f"W/L: {stats['wins']}/{stats['losses']} "
            f"PnL: ${stats['total_pnl']:+.2f} "
            f"| AI: {self.ai_calls} calls ${self.ai_cost_usd:.4f}    "
        )
        self._emit_status(text)

    # --- AI decision loop -----------------------------------------------

    async def ai_loop(self) -> None:
        """Every ai_interval seconds, ask Claude for a decision."""
        await asyncio.sleep(5)  # warmup so we have data

        while True:
            if len(self.price_history["up"]) >= 5:
                # Session lock: stop spending on AI calls after a streak of
                # losses signals the strategy doesn't fit current regime.
                # Keep AI active only when a position is open (need SELL).
                if (
                    self.consecutive_losses >= self.max_session_losses
                    and not self.position
                ):
                    if not self.session_locked:
                        self.log(
                            f"Session locked: {self.consecutive_losses} consecutive "
                            f"losses (>= {self.max_session_losses}). AI calls and BUY "
                            f"paused for rest of session.",
                            "AI",
                        )
                        self.session_locked = True
                    await asyncio.sleep(self.ai_interval)
                    continue

                # Extreme guard: when prices are far outside the value band,
                # the prompt's RULE forces HOLD anyway. Skip the API call.
                up = self.current_prices.get("up", 0)
                if (
                    not self.position
                    and (up < self.extreme_low or up > self.extreme_high)
                ):
                    self.log(
                        f"Extreme prices (UP={up:.4f} outside "
                        f"[{self.extreme_low:.2f}, {self.extreme_high:.2f}]), "
                        f"skipping AI call",
                        "AI",
                    )
                    await asyncio.sleep(self.ai_interval)
                    continue

                await self.fetch_spot_price()
                decision = await self.ask_claude()

                if decision:
                    action = decision.get("action", "HOLD")
                    confidence = decision.get("confidence", 0)
                    reason = decision.get("reason", "")

                    print()
                    self.log(
                        f"AI: {action} (confidence: {confidence}%) — {reason}",
                        "AI",
                    )

                    if action == "SELL" and self.position:
                        current = self.current_prices.get(self.position.side, 0)
                        if current > 0:
                            self.paper_sell(current, f"AI SELL ({confidence}%)")

                    elif action in ("BUY_UP", "BUY_DOWN") and not self.position:
                        # Patience: time-based decay of consecutive losses
                        if self.patience and self.consecutive_losses > 0 and self.last_trade_time > 0:
                            idle_time = time.time() - self.last_trade_time
                            decay_count = int(idle_time / self.loss_decay_seconds)
                            if decay_count > 0:
                                old_losses = self.consecutive_losses
                                self.consecutive_losses = max(
                                    0, self.consecutive_losses - decay_count
                                )
                                if self.consecutive_losses < old_losses:
                                    self.log(
                                        f"Patience: {old_losses}->{self.consecutive_losses} "
                                        f"losses (decay after {idle_time:.0f}s idle)",
                                        "AI",
                                    )

                        # Patience: scaled cooldown after last trade
                        if self.patience and self.last_trade_time > 0:
                            cooldown = self.cooldown_base * (
                                1.5 ** min(self.consecutive_losses, 3)
                            )
                            elapsed = time.time() - self.last_trade_time
                            if elapsed < cooldown:
                                self.log(
                                    f"Patience: cooldown {cooldown - elapsed:.0f}s "
                                    f"remaining, skipping",
                                    "AI",
                                )
                                await asyncio.sleep(self.ai_interval)
                                continue

                        # Patience: raise confidence after consecutive losses
                        effective_threshold = self.confidence_threshold
                        if self.patience and self.consecutive_losses > 0:
                            effective_threshold = min(
                                90,
                                self.confidence_threshold
                                + self.confidence_bump * self.consecutive_losses,
                            )
                            self.log(
                                f"Patience: {self.consecutive_losses} consecutive "
                                f"loss(es), threshold raised to {effective_threshold}%",
                                "AI",
                            )

                        if confidence >= effective_threshold:
                            side = "up" if action == "BUY_UP" else "down"
                            price = self.current_prices.get(side, 0)
                            if price > 0.70:
                                self.log(
                                    f"Price {price:.4f} > 0.70 — too expensive, skipping",
                                    "AI",
                                )
                            elif price < 0.30:
                                self.log(
                                    f"Price {price:.4f} < 0.30 — too cheap/risky, skipping",
                                    "AI",
                                )
                            elif price > 0:
                                self.paper_buy(
                                    side, price,
                                    reason=reason,
                                    extra_log=f"confidence: {confidence}%",
                                )
                        else:
                            self.log(
                                f"AI confidence {confidence}% < threshold "
                                f"{effective_threshold}%, skipping",
                                "AI",
                            )

            await asyncio.sleep(self.ai_interval)

    # --- Market discovery + main loop -----------------------------------

    async def discover_and_subscribe(self) -> bool:
        market = self.gamma.get_market_info(self.config.coin)
        if not market:
            self.log(f"No active 15min market for {self.config.coin}", "WARN")
            return False

        slug = market["slug"]
        token_ids = market["token_ids"]

        if slug != self.current_slug:
            if self.position and self.current_prices.get(self.position.side, 0) > 0:
                print()
                self.paper_sell(
                    self.current_prices[self.position.side],
                    "MARKET ENDED",
                )

            self.current_slug = slug
            self.current_question = market.get("question", "")
            self.price_history = {
                "up": deque(maxlen=self.config.lookback),
                "down": deque(maxlen=self.config.lookback),
            }
            self.token_to_side = {}
            for side, token_id in token_ids.items():
                self.token_to_side[token_id] = side

            print()
            self.log(f"Market: {self.current_question}")
            self.needs_reconnect = True

        return True

    async def market_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.discover_and_subscribe()
            except Exception as e:
                self.log(f"Market refresh error: {e}", "WARN")

    async def run(self) -> None:
        self.log(f"Paper trader v3 (Claude AI) starting: {self.config.coin}")
        if self.run_name:
            self.log(f"Run config: {self.run_name}")
        self.log(
            f"Size: ${self.config.size_usdc} | Confidence: {self.confidence_threshold}%"
        )
        self.log(
            f"AI interval: {self.ai_interval}s | TP: +{self.config.take_profit} "
            f"| SL: -{self.config.stop_loss}"
        )
        self.log(f"Executor: {self.executor.name()}")
        self.log("Price boundaries: skip buy if price > 0.70 or < 0.30")
        self.log(
            f"AI extreme guard: skip call if UP outside "
            f"[{self.extreme_low:.2f}, {self.extreme_high:.2f}]"
        )
        self.log(f"Session lock: {self.max_session_losses} consecutive losses")
        if self.patience:
            self.log(
                f"Patience mode: ON (cooldown {self.cooldown_base:.0f}s "
                f"x 1.5^losses, +{self.confidence_bump}% per loss, "
                f"decay {self.loss_decay_seconds:.0f}s)"
            )
        self.log("Model: claude-haiku-4-5 (~$0.001 per call)")
        self.log("Ctrl+C to stop")
        self.log("")

        try:
            while True:
                if not await self.discover_and_subscribe():
                    self.log("Cannot find active market, waiting...", "WARN")
                    await asyncio.sleep(10)
                    continue

                self.needs_reconnect = False
                self.ws = MarketWebSocket()
                self.ws.on_book(self.on_book_update)

                @self.ws.on_connect
                def on_connect():
                    self.log("WebSocket connected", "WS")

                @self.ws.on_disconnect
                def on_disconnect():
                    self.log("WebSocket disconnected", "WS")

                all_tokens = list(self.token_to_side.keys())
                await self.ws.subscribe(all_tokens)

                ws_task = asyncio.create_task(self.ws.run(auto_reconnect=True))
                refresh_task = asyncio.create_task(self.market_refresh_loop())
                ai_task = asyncio.create_task(self.ai_loop())

                while not self.needs_reconnect:
                    await asyncio.sleep(1)

                self.ws.stop()
                for task in [ws_task, refresh_task, ai_task]:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

                self.log("Reconnecting for new market...", "WS")

        except asyncio.CancelledError:
            pass
        finally:
            self.print_summary()


async def main():
    defaults = PaperConfig()

    parser = argparse.ArgumentParser(description="Polymarket Paper Trader (Claude AI)")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML run config. CLI args override YAML when both provided.",
    )
    parser.add_argument(
        "--coin",
        default=defaults.coin,
        choices=PaperConfig.supported_coins(),
    )
    parser.add_argument("--size", type=float, default=defaults.size_usdc,
                        help="Position size in USDC")
    parser.add_argument("--confidence", type=int, default=70,
                        help="Min confidence to trade")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Seconds between AI calls")
    # AI variant historically used different TP/SL defaults than the flash-crash
    # variants — we override PaperConfig defaults here to match v4 production tuning.
    parser.add_argument("--tp", type=float, default=0.08, help="Take profit")
    parser.add_argument("--sl", type=float, default=0.06, help="Stop loss")
    parser.add_argument(
        "--no-patience",
        dest="patience",
        action="store_false",
        help="Disable patience mode (default ON: cooldown + higher confidence after losses)",
    )
    parser.add_argument(
        "--max-session-losses",
        type=int,
        default=4,
        help="Lock session (skip AI/BUY) after N consecutive losses",
    )
    parser.set_defaults(patience=True)
    args = parser.parse_args()

    # YAML config: load and apply as fallback for CLI args left at default.
    # Strategy params (cooldown, decay, bump, extreme band, executor) only live
    # in YAML — no CLI flag for them, keeps CLI surface small.
    yaml_data = {}
    if args.config:
        import yaml
        with open(args.config) as f:
            yaml_data = yaml.safe_load(f) or {}
        cli_defaults = vars(parser.parse_args([]))
        # Map yaml keys -> CLI args names. Apply only when CLI value is unchanged.
        cli_map = {
            "coin": "coin",
            "size_usdc": "size",
            "take_profit": "tp",
            "stop_loss": "sl",
            "confidence_threshold": "confidence",
            "ai_interval": "interval",
            "patience": "patience",
            "max_session_losses": "max_session_losses",
        }
        for yaml_key, args_key in cli_map.items():
            if yaml_key in yaml_data and getattr(args, args_key) == cli_defaults.get(args_key):
                setattr(args, args_key, yaml_data[yaml_key])

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        return

    config = PaperConfig(
        coin=args.coin,
        size_usdc=args.size,
        take_profit=args.tp,
        stop_loss=args.sl,
        lookback=100,  # AI variant historically uses larger window
    )
    # Strategy tunables — sourced from YAML, with hardcoded defaults matching
    # the production config from 30.04 (so CLI-only invocations keep working).
    executor = make_executor(yaml_data.get("executor", "taker"))
    trader = ClaudeTrader(
        config=config,
        confidence_threshold=args.confidence,
        ai_interval=args.interval,
        patience=args.patience,
        max_session_losses=args.max_session_losses,
        cooldown_base=yaml_data.get("cooldown_base", 60.0),
        loss_decay_seconds=yaml_data.get("loss_decay_seconds", 180.0),
        confidence_bump=yaml_data.get("confidence_bump", 5),
        extreme_skip_band=tuple(yaml_data.get("extreme_skip_band", (0.20, 0.80))),
        run_name=yaml_data.get("name", ""),
        executor=executor,
    )

    try:
        await trader.run()
    except KeyboardInterrupt:
        trader.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
