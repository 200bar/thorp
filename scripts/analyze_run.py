#!/usr/bin/env python3
"""
analyze_run.py — parse a paper trader log and emit run metrics as JSON.

Usage:
    python scripts/analyze_run.py logs/paper-2026-05-01.log
    python scripts/analyze_run.py logs/paper-2026-05-01.log --pretty
    python scripts/analyze_run.py logs/*.log --compare   # multi-run table

Designed for n=10 testing — replaces ad-hoc grepping.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# --- Regexes (intentionally tolerant — log format may evolve slightly) ------

RE_TRADE_OPEN = re.compile(
    r"\[(\d{2}:\d{2}:\d{2})\]\s+\[\+\]\s+PAPER BUY (UP|DOWN) @ ([\d.]+)"
)
RE_TRADE_CLOSE = re.compile(
    r"\[(\d{2}:\d{2}:\d{2})\]\s+\[[\$!]\]\s+PAPER SELL (UP|DOWN) @ ([\d.]+) \| "
    r"(TAKE PROFIT|STOP LOSS|AI SELL[^|]*|MARKET ENDED) \| PnL: \$([+-][\d.]+)"
)
RE_EXTREME_SKIP = re.compile(r"\[\*\]\s+Extreme prices \(UP=")
RE_SESSION_LOCK = re.compile(r"\[\*\]\s+Session locked")
RE_PATIENCE_SKIP = re.compile(r"\[\*\]\s+Patience:.+(skipping|cooldown)")
RE_AI_CALL = re.compile(r"\[\*\]\s+AI: (BUY_UP|BUY_DOWN|HOLD|SELL)")
RE_NON_JSON = re.compile(r"Claude returned non-JSON")
RE_FINAL_STATS = re.compile(
    r"W/L: (\d+)/(\d+)\s+PnL: \$([+-]?[\d.]+)\s+\| AI: (\d+) calls \$([\d.]+)"
)
RE_GRACEFUL_OK = re.compile(r"Exited gracefully after (\d+)s")
RE_GRACEFUL_FAIL = re.compile(r"No graceful exit after (\d+)s")
RE_SUMMARY_PRESENT = re.compile(r"PAPER TRADING SESSION SUMMARY")


def analyze(path: Path) -> dict:
    """Parse one log file. Returns metrics dict.

    Failures here are loud (Reguła 12) — if regex doesn't match anything,
    that's a real problem, surface it.
    """
    text = path.read_text(errors="replace")
    lines = text.splitlines()

    trades_open = []
    trades_closed = []
    extreme_skips = 0
    session_locks = 0
    patience_skips = 0
    ai_calls_logged = 0
    non_json_count = 0
    final_stats = None
    graceful_status = "unknown"
    summary_present = False

    for line in lines:
        if m := RE_TRADE_OPEN.search(line):
            trades_open.append({"time": m.group(1), "side": m.group(2), "entry": float(m.group(3))})
        if m := RE_TRADE_CLOSE.search(line):
            trades_closed.append({
                "time": m.group(1),
                "side": m.group(2),
                "exit": float(m.group(3)),
                "reason": m.group(4).strip(),
                "pnl": float(m.group(5)),
            })
        if RE_EXTREME_SKIP.search(line):
            extreme_skips += 1
        if RE_SESSION_LOCK.search(line):
            session_locks += 1
        if RE_PATIENCE_SKIP.search(line):
            patience_skips += 1
        if RE_AI_CALL.search(line):
            ai_calls_logged += 1
        if RE_NON_JSON.search(line):
            non_json_count += 1
        if m := RE_FINAL_STATS.search(line):
            final_stats = {
                "wins": int(m.group(1)),
                "losses": int(m.group(2)),
                "pnl": float(m.group(3)),
                "ai_calls": int(m.group(4)),
                "ai_cost_usd": float(m.group(5)),
            }
        if m := RE_GRACEFUL_OK.search(line):
            graceful_status = f"ok_{m.group(1)}s"
        elif RE_GRACEFUL_FAIL.search(line):
            graceful_status = "failed"
        if RE_SUMMARY_PRESENT.search(line):
            summary_present = True

    # Reconcile: closed trades from log vs final W/L line
    closed_count = len(trades_closed)
    closed_wins = sum(1 for t in trades_closed if t["pnl"] > 0)
    closed_losses = sum(1 for t in trades_closed if t["pnl"] < 0)
    closed_pnl = sum(t["pnl"] for t in trades_closed)

    # Exit reason breakdown
    exits = {}
    for t in trades_closed:
        # Normalize "AI SELL (72%)" -> "AI SELL"
        reason = t["reason"].split("(")[0].strip()
        exits[reason] = exits.get(reason, 0) + 1

    # Max consecutive losses (raw, no decay)
    max_consec_loss = 0
    current_streak = 0
    for t in trades_closed:
        if t["pnl"] < 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0

    # Trade direction breakdown (UP bias check)
    up_trades = sum(1 for t in trades_closed if t["side"] == "UP")
    down_trades = sum(1 for t in trades_closed if t["side"] == "DOWN")

    # PnL stats
    if trades_closed:
        wins = [t["pnl"] for t in trades_closed if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades_closed if t["pnl"] < 0]
        pnl_stats = {
            "total": round(closed_pnl, 4),
            "best_trade": max((t["pnl"] for t in trades_closed), default=0),
            "worst_trade": min((t["pnl"] for t in trades_closed), default=0),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
        }
    else:
        pnl_stats = {"total": 0, "best_trade": 0, "worst_trade": 0, "avg_win": 0, "avg_loss": 0}

    return {
        "file": str(path.name),
        "trades": {
            "opened": len(trades_open),
            "closed": closed_count,
            "wins": closed_wins,
            "losses": closed_losses,
            "win_rate_pct": round(closed_wins / closed_count * 100, 1) if closed_count else 0,
            "max_consec_loss": max_consec_loss,
            "up_count": up_trades,
            "down_count": down_trades,
        },
        "pnl": pnl_stats,
        "exits": exits,
        "ai": {
            "calls_logged": ai_calls_logged,
            "extreme_skips": extreme_skips,
            "patience_skips": patience_skips,
            "session_lock_triggers": session_locks,
            "non_json_responses": non_json_count,
            "final_calls": final_stats["ai_calls"] if final_stats else None,
            "final_cost_usd": final_stats["ai_cost_usd"] if final_stats else None,
        },
        "shutdown": {
            "graceful": graceful_status,
            "summary_in_log": summary_present,
        },
        "final_stats_line": final_stats,
        "trades_log": trades_closed,  # full sequence for further analysis
    }


def compact(metrics: dict) -> str:
    """One-line summary — for quick scanning when comparing runs."""
    t = metrics["trades"]
    p = metrics["pnl"]
    a = metrics["ai"]
    return (
        f"{metrics['file']}: "
        f"{t['closed']}T {t['wins']}W/{t['losses']}L ({t['win_rate_pct']}%) "
        f"PnL ${p['total']:+.2f} "
        f"max-consec-loss {t['max_consec_loss']} "
        f"AI {a['final_calls']}c ${a['final_cost_usd']} "
        f"extreme-skip {a['extreme_skips']} "
        f"shutdown {metrics['shutdown']['graceful']}"
    )


def main():
    ap = argparse.ArgumentParser(description="Analyze paper trader run log(s)")
    ap.add_argument("paths", nargs="+", type=Path, help="log file(s)")
    ap.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    ap.add_argument("--compare", action="store_true",
                    help="emit one-line summary per file (instead of JSON)")
    args = ap.parse_args()

    if args.compare:
        for p in sorted(args.paths):
            if not p.exists():
                print(f"{p}: NOT FOUND", file=sys.stderr)
                continue
            print(compact(analyze(p)))
        return

    out = [analyze(p) for p in args.paths if p.exists()]
    if len(out) == 1:
        out = out[0]
    indent = 2 if args.pretty else None
    print(json.dumps(out, indent=indent))


if __name__ == "__main__":
    main()
