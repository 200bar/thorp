"""
Paper Trading Bot v3 — Claude AI + WebSocket.

Claude Haiku analizuje rynek co N sekund i decyduje:
- BUY UP, BUY DOWN, lub HOLD
- z confidence score (0-100)

Trade tylko gdy confidence > threshold.

Użycie:
    source .venv/bin/activate
    export ANTHROPIC_API_KEY=sk-ant-...
    python paper_trader_ai.py --coin ETH --size 10

    --coin: BTC, ETH, SOL, XRP (default: ETH)
    --size: wielkość pozycji w USDC (default: 10)
    --confidence: min confidence do trade (default: 70)
    --interval: sekundy między zapytaniami do Claude (default: 30)
    --tp: take profit (default: 0.08)
    --sl: stop loss (default: 0.04)
    --patience: patience mode (cooldown + higher confidence after losses)
"""

import argparse
import asyncio
import json
import os
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict
from collections import deque

import aiohttp
import anthropic

from src.gamma_client import GammaClient
from src.websocket_client import MarketWebSocket, OrderbookSnapshot

logging.getLogger("src.websocket_client").setLevel(logging.WARNING)


@dataclass
class PaperPosition:
    side: str
    entry_price: float
    size_usdc: float
    shares: float
    entry_time: float
    take_profit: float
    stop_loss: float
    reason: str = ""

    def pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.shares

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0
        return ((current_price - self.entry_price) / self.entry_price) * 100

    def should_take_profit(self, current_price: float) -> bool:
        return current_price >= self.entry_price + self.take_profit

    def should_stop_loss(self, current_price: float) -> bool:
        return current_price <= self.entry_price - self.stop_loss


class ClaudeTrader:
    """Paper trading z Claude AI jako decision engine."""

    def __init__(
        self,
        coin: str = "ETH",
        size_usdc: float = 10.0,
        confidence_threshold: int = 70,
        ai_interval: float = 30.0,
        take_profit: float = 0.08,
        stop_loss: float = 0.04,
        lookback: int = 100,
        patience: bool = False,
    ):
        self.coin = coin
        self.size_usdc = size_usdc
        self.confidence_threshold = confidence_threshold
        self.ai_interval = ai_interval
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.lookback = lookback
        self.patience = patience
        self.cooldown_seconds = 60.0  # wait after closing position
        self.confidence_bump = 10  # extra confidence needed after a loss

        # State
        self.position: Optional[PaperPosition] = None
        self.last_trade_time: float = 0.0
        self.consecutive_losses: int = 0
        self.trades: list = []
        self.price_history: Dict[str, deque] = {
            "up": deque(maxlen=lookback),
            "down": deque(maxlen=lookback),
        }
        self.token_to_side: Dict[str, str] = {}
        self.current_prices: Dict[str, float] = {}
        self.current_slug: str = ""
        self.current_question: str = ""
        self.update_count: int = 0
        self.ai_calls: int = 0
        self.ai_cost_usd: float = 0.0
        self.needs_reconnect = False
        self.spot_price: Optional[float] = None

        # Binance symbol mapping
        self.spot_symbols = {
            "ETH": "ETHUSDT",
            "BTC": "BTCUSDT",
            "SOL": "SOLUSDT",
            "XRP": "XRPUSDT",
        }

        # Components
        self.gamma = GammaClient()
        self.ws = MarketWebSocket()
        self.claude = anthropic.Anthropic()

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {
            "INFO": " ", "BUY": "+", "SELL": "-", "WIN": "$",
            "LOSS": "!", "WARN": "?", "WS": "~", "AI": "*"
        }
        print(f"[{ts}] [{prefix.get(level, ' ')}] {msg}")

    async def fetch_spot_price(self):
        """Pobierz aktualną cenę spot z Binance."""
        symbol = self.spot_symbols.get(self.coin)
        if not symbol:
            return
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.spot_price = float(data["price"])
        except Exception:
            pass  # Keep last known price

    def get_market_context(self) -> str:
        """Zbuduj kontekst rynkowy dla Claude."""
        up = self.current_prices.get("up", 0)
        down = self.current_prices.get("down", 0)

        # Ostatnie 20 cen (jeśli są)
        up_recent = list(self.price_history["up"])[-20:]
        down_recent = list(self.price_history["down"])[-20:]

        # Trend
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

        # Pozycja info
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
                f"TP={self.position.entry_price + self.take_profit:.4f} "
                f"SL={self.position.entry_price - self.stop_loss:.4f}"
            )

        spot_info = f"Live {self.coin} spot price (Binance): ${self.spot_price:,.2f}" if self.spot_price else f"Live {self.coin} spot price: unavailable"

        # Trading history (last 10 trades)
        history_info = "No trades yet this session."
        if self.trades:
            stats = self.get_stats()
            lines = [f"Session: {stats['total']} trades, {stats['wins']}W/{stats['losses']}L, win rate {stats['win_rate']:.0f}%, PnL ${stats['total_pnl']:+.2f}"]
            for t in self.trades[-10:]:
                lines.append(
                    f"  {t['side'].upper()} entry={t['entry']:.4f} exit={t['exit']:.4f} "
                    f"PnL=${t['pnl']:+.2f} {t['reason']} | reason: {t.get('entry_reason', '')}"
                )
            history_info = "\n".join(lines)

        return f"""Market: {self.current_question}
Coin: {self.coin}
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
        """Zapytaj Claude o decyzję tradingową."""
        context = self.get_market_context()

        prompt = f"""You are a prediction market trading bot analyzing a 15-minute crypto Up/Down market on Polymarket.

{context}

This is a binary market: if {self.coin} price goes UP in this 15-minute window, "UP" token pays $1. If DOWN, "DOWN" token pays $1.

RULE: Do NOT recommend buying if the token price is > 0.80 (too expensive, limited upside) or < 0.20 (too cheap/risky, likely losing side). Best value is in the 0.30-0.70 range.

LEARN from your trading history above. Notice which trades won/lost and why. Avoid repeating losing patterns. Double down on what works.

Analyze the current market state and decide:
1. Should I BUY UP, BUY DOWN, or HOLD?
2. What is your confidence (0-100)?
3. Brief reason (1 sentence).

If I already have an open position, you can also suggest HOLD (keep position) or SELL (close early).

Respond ONLY with valid JSON:
{{"action": "BUY_UP" | "BUY_DOWN" | "HOLD" | "SELL", "confidence": 0-100, "reason": "..."}}"""

        try:
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )

            self.ai_calls += 1
            # Estimate cost (Haiku: $1/M input, $5/M output)
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = (input_tokens * 1.0 + output_tokens * 5.0) / 1_000_000
            self.ai_cost_usd += cost

            text = response.content[0].text.strip()
            # Parse JSON from response
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text)
            return result

        except json.JSONDecodeError:
            self.log(f"Claude returned non-JSON: {text[:100]}", "WARN")
            return None
        except Exception as e:
            self.log(f"Claude API error: {e}", "WARN")
            return None

    def paper_buy(self, side: str, price: float, reason: str, confidence: int):
        shares = self.size_usdc / price
        self.position = PaperPosition(
            side=side,
            entry_price=price,
            size_usdc=self.size_usdc,
            shares=shares,
            entry_time=time.time(),
            take_profit=self.take_profit,
            stop_loss=self.stop_loss,
            reason=reason,
        )
        self.log(
            f"PAPER BUY {side.upper()} @ {price:.4f} | "
            f"${self.size_usdc:.2f} = {shares:.1f} shares | "
            f"confidence: {confidence}% | {reason}",
            "BUY"
        )

    def paper_sell(self, price: float, reason: str):
        if not self.position:
            return
        pnl = self.position.pnl(price)
        pnl_pct = self.position.pnl_pct(price)
        hold_time = time.time() - self.position.entry_time
        self.trades.append({
            "side": self.position.side,
            "entry": self.position.entry_price,
            "exit": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_seconds": hold_time,
            "reason": reason,
            "entry_reason": self.position.reason,
            "time": datetime.now().isoformat(),
        })
        level = "WIN" if pnl >= 0 else "LOSS"
        self.log(
            f"PAPER SELL {self.position.side.upper()} @ {price:.4f} | "
            f"{reason} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | hold: {hold_time:.0f}s",
            level
        )
        self.last_trade_time = time.time()
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self.position = None

    def get_stats(self) -> dict:
        wins = [t for t in self.trades if t["pnl"] >= 0]
        losses = [t for t in self.trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in self.trades)
        return {
            "total": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "total_pnl": total_pnl,
            "win_rate": (len(wins) / len(self.trades) * 100) if self.trades else 0,
        }

    async def on_book_update(self, snapshot: OrderbookSnapshot):
        self.update_count += 1
        asset_id = snapshot.asset_id
        side = self.token_to_side.get(asset_id)
        if not side:
            return
        mid = snapshot.mid_price
        self.current_prices[side] = mid
        self.price_history[side].append(mid)

        # Check TP/SL
        if self.position and self.position.side == side:
            if self.position.should_take_profit(mid):
                print()
                self.paper_sell(mid, "TAKE PROFIT")
            elif self.position.should_stop_loss(mid):
                print()
                self.paper_sell(mid, "STOP LOSS")

        # Status co 20 updates
        if self.update_count % 20 == 0:
            self.print_status()

    def print_status(self):
        up = self.current_prices.get("up", 0)
        down = self.current_prices.get("down", 0)
        pos_str = ""
        if self.position:
            current = self.current_prices.get(self.position.side, 0)
            pnl = self.position.pnl(current)
            pos_str = f" | POS: {self.position.side.upper()} PnL: ${pnl:+.2f}"
        stats = self.get_stats()
        spot_str = f" SPOT=${self.spot_price:,.0f}" if self.spot_price else ""
        print(
            f"\r[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.coin}{spot_str} UP={up:.4f} DOWN={down:.4f}"
            f"{pos_str} | "
            f"W/L: {stats['wins']}/{stats['losses']} PnL: ${stats['total_pnl']:+.2f} "
            f"| AI: {self.ai_calls} calls ${self.ai_cost_usd:.4f}    ",
            end="", flush=True
        )

    def print_summary(self):
        print("\n")
        self.log("=" * 60)
        self.log("PAPER TRADING SESSION SUMMARY (Claude AI)")
        self.log("=" * 60)
        stats = self.get_stats()
        self.log(f"Coin: {self.coin}")
        self.log(f"Trades: {stats['total']}")
        self.log(f"Wins: {stats['wins']} | Losses: {stats['losses']}")
        self.log(f"Win rate: {stats['win_rate']:.1f}%")
        self.log(f"Total PnL: ${stats['total_pnl']:+.2f}")
        self.log(f"AI calls: {self.ai_calls} | AI cost: ${self.ai_cost_usd:.4f}")
        self.log(f"Confidence threshold: {self.confidence_threshold}%")
        self.log(f"TP: +{self.take_profit} | SL: -{self.stop_loss}")
        if self.trades:
            self.log("")
            self.log("Trade log:")
            for i, t in enumerate(self.trades, 1):
                self.log(
                    f"  #{i} {t['side'].upper()} "
                    f"entry={t['entry']:.4f} exit={t['exit']:.4f} "
                    f"PnL=${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) "
                    f"{t['reason']} | AI: {t.get('entry_reason', '')}"
                )

    async def ai_loop(self):
        """Co ai_interval sekund pytaj Claude o decyzję."""
        # Czekaj na dane z WS
        await asyncio.sleep(5)

        while True:
            # Potrzebujemy min. 5 data points
            if len(self.price_history["up"]) >= 5:
                await self.fetch_spot_price()
                decision = await self.ask_claude()

                if decision:
                    action = decision.get("action", "HOLD")
                    confidence = decision.get("confidence", 0)
                    reason = decision.get("reason", "")

                    print()
                    self.log(
                        f"AI: {action} (confidence: {confidence}%) — {reason}",
                        "AI"
                    )

                    if action == "SELL" and self.position:
                        current = self.current_prices.get(self.position.side, 0)
                        if current > 0:
                            self.paper_sell(current, f"AI SELL ({confidence}%)")

                    elif action in ("BUY_UP", "BUY_DOWN") and not self.position:
                        # Patience: cooldown after last trade
                        if self.patience and self.last_trade_time > 0:
                            elapsed = time.time() - self.last_trade_time
                            if elapsed < self.cooldown_seconds:
                                self.log(
                                    f"Patience: cooldown {self.cooldown_seconds - elapsed:.0f}s remaining, skipping",
                                    "AI"
                                )
                                await asyncio.sleep(self.ai_interval)
                                continue

                        # Patience: raise confidence after consecutive losses
                        effective_threshold = self.confidence_threshold
                        if self.patience and self.consecutive_losses > 0:
                            effective_threshold = min(95, self.confidence_threshold + self.confidence_bump * self.consecutive_losses)
                            self.log(
                                f"Patience: {self.consecutive_losses} consecutive loss(es), threshold raised to {effective_threshold}%",
                                "AI"
                            )

                        if confidence >= effective_threshold:
                            side = "up" if action == "BUY_UP" else "down"
                            price = self.current_prices.get(side, 0)
                            if price > 0.80:
                                self.log(
                                    f"Price {price:.4f} > 0.80 — too expensive, skipping",
                                    "AI"
                                )
                            elif price < 0.20:
                                self.log(
                                    f"Price {price:.4f} < 0.20 — too cheap/risky, skipping",
                                    "AI"
                                )
                            elif price > 0:
                                self.paper_buy(side, price, reason, confidence)
                        else:
                            self.log(
                                f"AI confidence {confidence}% < threshold {effective_threshold}%, skipping",
                                "AI"
                            )

            await asyncio.sleep(self.ai_interval)

    async def discover_and_subscribe(self) -> bool:
        market = self.gamma.get_market_info(self.coin)
        if not market:
            self.log(f"No active 15min market for {self.coin}", "WARN")
            return False

        slug = market["slug"]
        token_ids = market["token_ids"]

        if slug != self.current_slug:
            if self.position and self.current_prices.get(self.position.side, 0) > 0:
                print()
                self.paper_sell(self.current_prices[self.position.side], "MARKET ENDED")

            self.current_slug = slug
            self.current_question = market.get("question", "")
            self.price_history = {"up": deque(maxlen=self.lookback), "down": deque(maxlen=self.lookback)}
            self.token_to_side = {}
            for side, token_id in token_ids.items():
                self.token_to_side[token_id] = side

            print()
            self.log(f"Market: {self.current_question}")
            self.needs_reconnect = True

        return True

    async def market_refresh_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.discover_and_subscribe()
            except Exception as e:
                self.log(f"Market refresh error: {e}", "WARN")

    async def run(self):
        self.log(f"Paper trader v3 (Claude AI) starting: {self.coin}")
        self.log(f"Size: ${self.size_usdc} | Confidence: {self.confidence_threshold}%")
        self.log(f"AI interval: {self.ai_interval}s | TP: +{self.take_profit} | SL: -{self.stop_loss}")
        self.log(f"Price boundaries: skip buy if price > 0.80 or < 0.20")
        if self.patience:
            self.log(f"Patience mode: ON (cooldown {self.cooldown_seconds:.0f}s, +{self.confidence_bump}% per loss)")
        self.log(f"Model: claude-haiku-4-5 (~$0.001 per call)")
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
    parser = argparse.ArgumentParser(description="Polymarket Paper Trader (Claude AI)")
    parser.add_argument("--coin", default="ETH", choices=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--size", type=float, default=10.0, help="Position size in USDC")
    parser.add_argument("--confidence", type=int, default=70, help="Min confidence to trade")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between AI calls")
    parser.add_argument("--tp", type=float, default=0.08, help="Take profit")
    parser.add_argument("--sl", type=float, default=0.04, help="Stop loss")
    parser.add_argument("--patience", action="store_true", help="Patience mode: cooldown + higher confidence after losses")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        return

    trader = ClaudeTrader(
        coin=args.coin,
        size_usdc=args.size,
        confidence_threshold=args.confidence,
        ai_interval=args.interval,
        take_profit=args.tp,
        stop_loss=args.sl,
        patience=args.patience,
    )

    try:
        await trader.run()
    except KeyboardInterrupt:
        trader.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
