"""
scan_check.py - 17-Pair Diagnostic Scanner & Bar Inspector
Evaluates the exact last completed 4-Hour bar (iloc[-2]) across all 17 pairs.
Displays Close Price, 50 EMA, 14-Donchian Range, Squeeze state, Volume multiplier,
and the precise technical reason why a trade was taken or skipped.
"""
from __future__ import annotations

import warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

from datetime import datetime, timezone, timedelta
import pandas as pd
from market_stream import MarketStream
from strategy_v4 import MacroTrendStrategy4H, PAIRS_17

IST = timezone(timedelta(hours=5, minutes=30))


def run_scan_check():
    streamer = MarketStream(PAIRS_17)
    strategy = MacroTrendStrategy4H(donchian_n=14, trail_n=7, ema_span=50)

    print("=" * 110)
    print("      COINDCX 4H BAR DIAGNOSTIC INSPECTOR (17 PAIR UNIVERSE)")
    print("=" * 110)

    results = []
    closed_bar_time_str = "Unknown"

    for idx, pair in enumerate(PAIRS_17, 1):
        print(f"[{idx:02d}/17] Fetching & evaluating {pair:<10}...", end="\r")
        df = streamer.fetch_pair_candles(pair, interval="4h", limit=100)
        if df.empty or len(df) < 52:
            results.append({
                "pair": pair,
                "close": 0.0,
                "ema": 0.0,
                "range_info": "Data Error",
                "vol_ratio": 0.0,
                "sqz": "NO",
                "verdict": "FETCH_FAILED",
                "detail": "Insufficient historical candles from endpoints"
            })
            continue

        df = strategy.compute_indicators(df)
        bar = df.iloc[-2]  # The exact closed candle evaluated by main.py

        if closed_bar_time_str == "Unknown" and "datetime" in bar:
            bar_utc = bar["datetime"]
            if hasattr(bar_utc, "tzinfo") and bar_utc.tzinfo is None:
                bar_utc = bar_utc.replace(tzinfo=timezone.utc)
            bar_ist = bar_utc.astimezone(IST)
            closed_bar_time_str = f"{bar_ist.strftime('%Y-%m-%d %I:%M %p IST')} ({bar_utc.strftime('%H:%M UTC')})"

        c, o, h, l = bar["close"], bar["open"], bar["high"], bar["low"]
        ema = bar["ema_50"]
        dh = bar["donch_high_14"]
        dl = bar["donch_low_14"]
        mom = bar["mom"]
        mom_slope = bar["mom_slope"]
        squeeze_recent = bool(bar["squeeze_recent"])
        vol_sma = bar["vol_sma_20"] if bar["vol_sma_20"] > 0 else 1.0
        vol_ratio = bar["volume"] / vol_sma

        # Check conditions
        donch_long = (c > dh) and (c > ema)
        donch_short = (c < dl) and (c < ema)
        sqz_long = squeeze_recent and (c > ema) and (c > bar["bb_mid"]) and (c > o) and (mom > 0) and (mom_slope > 0) and (vol_ratio >= 1.15)
        sqz_short = squeeze_recent and (c < ema) and (c < bar["bb_mid"]) and (c < o) and (mom < 0) and (mom_slope < 0) and (vol_ratio >= 1.15)

        # Distance to boundaries
        dist_high_pct = ((dh - c) / c) * 100.0
        dist_low_pct = ((c - dl) / c) * 100.0

        if sqz_long:
            verdict = "BUY SIGNAL"
            detail = f"SQUEEZE FIRE | Vol: {vol_ratio:.1f}x | Momentum: +{mom:.4f}"
        elif donch_long:
            verdict = "BUY SIGNAL"
            detail = f"DONCHIAN BREAKOUT | High ${dh:.4f} broken"
        elif sqz_short:
            verdict = "SELL SIGNAL"
            detail = f"SQUEEZE FIRE | Vol: {vol_ratio:.1f}x | Momentum: {mom:.4f}"
        elif donch_short:
            verdict = "SELL SIGNAL"
            detail = f"DONCHIAN BREAKOUT | Low ${dl:.4f} broken"
        else:
            verdict = "NO SIGNAL"
            # Pinpoint the exact reason
            if c > ema:
                detail = f"Above EMA50 | Inside Range ({dist_high_pct:+.1f}% to Breakout High)"
            else:
                detail = f"Below EMA50 | Inside Range ({dist_low_pct:+.1f}% to Breakdown Low)"

        results.append({
            "pair": pair,
            "close": c,
            "ema": ema,
            "dh": dh,
            "dl": dl,
            "vol_ratio": vol_ratio,
            "sqz": "YES" if squeeze_recent else "NO",
            "verdict": verdict,
            "detail": detail
        })

    print(" " * 110, end="\r")  # Clear progress line
    print(f" Last Closed 4H Bar Inspected : {closed_bar_time_str}")
    print("=" * 110)
    print(f" {'PAIR':<9} | {'CLOSE':<9} | {'50 EMA':<9} | {'DONCH HIGH':<10} | {'DONCH LOW':<10} | {'VOL':<5} | {'SQZ':<4} | {'STATUS':<11} | {'DIAGNOSTIC REASON'}")
    print("-" * 110)

    signals_found = 0
    for r in results:
        v = r["verdict"]
        if "SIGNAL" in v:
            signals_found += 1
            status_str = f"⭐ {v}"
        else:
            status_str = "No Signal"

        close_str = f"${r['close']:.4f}" if r['close'] < 10 else f"${r['close']:.2f}"
        ema_str = f"${r['ema']:.4f}" if r['ema'] < 10 else f"${r['ema']:.2f}"
        dh_str = f"${r.get('dh', 0.0):.4f}" if r.get('dh', 0.0) < 10 else f"${r.get('dh', 0.0):.2f}"
        dl_str = f"${r.get('dl', 0.0):.4f}" if r.get('dl', 0.0) < 10 else f"${r.get('dl', 0.0):.2f}"

        print(f" {r['pair']:<9} | {close_str:<9} | {ema_str:<9} | {dh_str:<10} | {dl_str:<10} | {r['vol_ratio']:>4.1f}x | {r['sqz']:<4} | {status_str:<11} | {r['detail']}")

    print("=" * 110)
    if signals_found == 0:
        print(" Verdict: 0 of 17 pairs triggered. All tokens are consolidating inside their 14-period channels.")
    else:
        print(f" Verdict: {signals_found} qualifying signal(s) found on this candle!")
    print("=" * 110 + "\n")


if __name__ == "__main__":
    run_scan_check()