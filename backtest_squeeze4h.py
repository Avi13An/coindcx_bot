"""
backtest_squeeze4h.py - 17-Pair 4H Volatility Squeeze + Donchian Hybrid Engine
Updated Configuration:
  - 17-Pair Official Production Universe
  - Capital Preservation Ladder:
      * Tier 1 (< ₹200): 1 Slot
      * Tier 2 (< ₹400): 2 Slots
      * Tier 3 (< ₹700): 3 Slots
      * Tier 4 (>= ₹700): 4 Slots
  - Sizing: 4.5% Fixed-Risk Dynamic Compounding with ₹650.00 Floor
  - Evaluation Logic: Strictly Completed 4-Hour Closed Candles (iloc[-2])
  - Exact Contract Step Divisibility matching CoinDCX Futures
  - Portfolio Rules: Max 2/Sector | Break-Even (+1.8R) Recycles Slot | Unlock (+1.0R)
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")

import argparse
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

PAIRS_17 = [
    # Layer 1 Momentum (7)
    "SOLUSDT", "SEIUSDT", "ADAUSDT", "INJUSDT", "SUIUSDT", "XTZUSDT", "ATOMUSDT",
    # DeFi Protocols (4)
    "GRTUSDT", "UNIUSDT", "RUNEUSDT", "KAVAUSDT",
    # Gaming & Metaverse Infrastructure (2)
    "ENJUSDT", "SANDUSDT",
    # Legacy & Payments (4)
    "BCHUSDT", "BNBUSDT", "DOGEUSDT", "IOTAUSDT",
]

SECTORS_17 = {
    "SOLUSDT": "L1", "SEIUSDT": "L1", "ADAUSDT": "L1", "INJUSDT": "L1",
    "SUIUSDT": "L1", "XTZUSDT": "L1", "ATOMUSDT": "L1",
    "GRTUSDT": "DEFI", "UNIUSDT": "DEFI", "RUNEUSDT": "DEFI", "KAVAUSDT": "DEFI",
    "ENJUSDT": "GAMING", "SANDUSDT": "GAMING",
    "BCHUSDT": "LEGACY", "BNBUSDT": "LEGACY", "DOGEUSDT": "LEGACY", "IOTAUSDT": "LEGACY",
}

PAIR_SPECS = {
    # Large-cap (> $100) -> Step 0.01, 2 Decimals
    "SOLUSDT": {"step": 0.01, "decimals": 2},
    "BNBUSDT": {"step": 0.01, "decimals": 2},
    "BCHUSDT": {"step": 0.01, "decimals": 2},

    # Mid-cap ($1 - $50) -> Step 0.1, 1 Decimal
    "INJUSDT": {"step": 0.1, "decimals": 1},
    "UNIUSDT": {"step": 0.1, "decimals": 1},
    "RUNEUSDT": {"step": 0.1, "decimals": 1},
    "SUIUSDT": {"step": 0.1, "decimals": 1},
    "ATOMUSDT": {"step": 0.1, "decimals": 1},
    "XTZUSDT": {"step": 0.1, "decimals": 1},

    # Small-cap / Sub-$1 -> Step 1.0, Integer contracts
    "SEIUSDT": {"step": 1.0, "decimals": 0},
    "ADAUSDT": {"step": 1.0, "decimals": 0},
    "ENJUSDT": {"step": 1.0, "decimals": 0},
    "GRTUSDT": {"step": 1.0, "decimals": 0},
    "DOGEUSDT": {"step": 1.0, "decimals": 0},
    "IOTAUSDT": {"step": 1.0, "decimals": 0},
    "KAVAUSDT": {"step": 1.0, "decimals": 0},
    "SANDUSDT": {"step": 1.0, "decimals": 0},
}

CACHE_DIR = Path(__file__).resolve().parent / "data_cache"
USD_INR_RATE = 102.00
COINDCX_MIN_NOTIONAL_INR = 650.0
TAKER_FEE_PCT = 0.0005


def fetch_historical_candles(symbol: str, limit: int = 1000) -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{symbol}_4h.csv"

    if cache_file.exists():
        try:
            df = pd.read_csv(cache_file)
            df["datetime"] = pd.to_datetime(df["datetime"])
            if len(df) >= 300:
                return df
        except Exception:
            pass

    endpoints = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=4h&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&limit={limit}",
    ]

    for url in endpoints:
        try:
            r = requests.get(url, timeout=6, verify=False)
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and len(raw) > 50:
                    df = pd.DataFrame(raw, columns=[
                        "open_time", "open", "high", "low", "close", "volume",
                        "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"
                    ])
                    for col in ["open", "high", "low", "close", "volume"]:
                        df[col] = df[col].astype(float)
                    df["datetime"] = pd.to_datetime(df["open_time"], unit="ms")
                    df = df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime").reset_index(drop=True)
                    df.to_csv(cache_file, index=False)
                    return df
        except Exception:
            continue

    return pd.DataFrame()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["donch_high_14"] = df["high"].shift(1).rolling(14).max()
    df["donch_low_14"] = df["low"].shift(1).rolling(14).min()
    df["trail_high_7"] = df["high"].shift(1).rolling(7).max()
    df["trail_low_7"] = df["low"].shift(1).rolling(7).min()
    df["vol_sma_20"] = df["volume"].rolling(20).mean()

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr_20"] = tr.rolling(20).mean()

    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2.0 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2.0 * df["bb_std"]

    df["kc_mid"] = df["close"].rolling(20).mean()
    df["kc_upper"] = df["kc_mid"] + 1.5 * df["atr_20"]
    df["kc_lower"] = df["kc_mid"] - 1.5 * df["atr_20"]

    df["squeeze_on"] = (df["bb_lower"] > df["kc_lower"]) & (df["bb_upper"] < df["kc_upper"])
    df["squeeze_recent"] = df["squeeze_on"].shift(1) | df["squeeze_on"].shift(2) | df["squeeze_on"].shift(3)

    midpoint_20 = (df["high"].rolling(20).max() + df["low"].rolling(20).min()) / 2.0
    df["mom"] = df["close"] - (midpoint_20 + df["bb_mid"]) / 2.0
    df["mom_slope"] = df["mom"] - df["mom"].shift(1)

    return df


def calculate_confidence_score(bar: pd.Series, d: int, entry_type: str) -> tuple[float, int, float]:
    c, h, l, ema = bar["close"], bar["high"], bar["low"], bar["ema_50"]
    bar_range = max(h - l, 1e-8)
    atr = bar["atr_20"] if bar["atr_20"] > 0 else bar_range

    vol_sma = bar["vol_sma_20"] if bar["vol_sma_20"] > 0 else 1.0
    vol_ratio = bar["volume"] / vol_sma
    vol_score = 1.0 if vol_ratio >= 1.60 else (0.5 if vol_ratio >= 1.15 else 0.0)

    ema_dist = (c - ema) / atr if d == 1 else (ema - c) / atr
    ema_score = 1.0 if ema_dist >= 1.00 else (0.5 if ema_dist >= 0.50 else 0.0)

    close_pct = (c - l) / bar_range if d == 1 else (h - c) / bar_range
    wick_score = 1.0 if close_pct >= 0.80 else (0.5 if close_pct >= 0.60 else 0.0)

    type_bonus = 0.5 if entry_type == "SQUEEZE" else 0.0
    total_score = vol_score + ema_score + wick_score + type_bonus

    if total_score >= 2.5:
        leverage = 14
    elif total_score >= 1.5:
        leverage = 12
    else:
        leverage = 10

    max_safe_stop_pct = (1.0 / leverage) * 0.70
    return total_score, leverage, max_safe_stop_pct


class PortfolioSimulator:
    def __init__(self, initial_capital: float = 148.18, risk_pct: float = 0.045):
        self.initial_capital = initial_capital
        self.equity = initial_capital
        self.risk_pct = risk_pct
        self.active_positions = {}
        self.closed_trades = []
        self.current_ist_day = None
        self.day_start_equity = initial_capital
        self.daily_pnl = 0.0
        self.is_halted = False
        self.peak_equity = initial_capital
        self.max_drawdown_pct = 0.0

    def get_max_slots(self) -> int:
        eq = self.equity
        if eq < 200.0:
            return 1  # Tier 1: Preservation Mode
        elif eq < 400.0:
            return 2  # Tier 2: 2 Slots
        elif eq < 700.0:
            return 3  # Tier 3: 3 Slots
        else:
            return 4  # Tier 4: 4 Slots

    def update_daily_cycle(self, dt: datetime):
        ist_dt = dt.astimezone(timezone(timedelta(hours=5, minutes=30))) if dt.tzinfo else dt + timedelta(hours=5, minutes=30)
        day_str = ist_dt.strftime("%Y-%m-%d")

        if day_str != self.current_ist_day:
            self.current_ist_day = day_str
            self.day_start_equity = self.equity
            self.daily_pnl = 0.0
            self.is_halted = False

    def get_margin_in_use(self) -> float:
        return sum(p["margin_inr"] for p in self.active_positions.values())

    def close_position(self, pair: str, exit_px: float, exit_time: datetime, reason: str):
        pos = self.active_positions.pop(pair)
        d = pos["d"]
        qty = pos["qty"]
        entry_px = pos["entry"]

        if reason == "LIQUIDATION":
            net_inr = -pos["margin_inr"]
        else:
            gross_usd = (exit_px - entry_px) * qty * d
            gross_inr = gross_usd * USD_INR_RATE
            notional_entry_inr = qty * entry_px * USD_INR_RATE
            notional_exit_inr = qty * exit_px * USD_INR_RATE
            fees_inr = (notional_entry_inr + notional_exit_inr) * TAKER_FEE_PCT
            net_inr = gross_inr - fees_inr

        self.equity += net_inr
        self.daily_pnl += net_inr

        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        current_dd = ((self.peak_equity - self.equity) / self.peak_equity) * 100.0
        if current_dd > self.max_drawdown_pct:
            self.max_drawdown_pct = current_dd

        if self.daily_pnl <= -0.50 * self.day_start_equity:
            self.is_halted = True

        self.closed_trades.append({
            "pair": pair,
            "entry_type": pos["entry_type"],
            "d": "LONG" if d == 1 else "SHORT",
            "entry_time": pos["entry_time"],
            "exit_time": exit_time,
            "entry_px": entry_px,
            "exit_px": exit_px,
            "leverage": pos["lev"],
            "score": pos["score"],
            "notional_inr": pos["notional_inr"],
            "margin_inr": pos["margin_inr"],
            "net_inr": net_inr,
            "reason": reason,
            "equity_after": self.equity,
        })


def run_squeeze_backtest(initial_capital: float = 148.18, risk_pct: float = 0.045):
    print("\n" + "=" * 95)
    print("      17-PAIR 4H SQUEEZE + DONCHIAN HYBRID ENGINE (OFFICIAL PRODUCTION)")
    print(f"  Starting Capital : ₹{initial_capital:.2f} | Dynamic Sizing: {risk_pct*100:.1f}% Fixed-Risk Compounding")
    print("  Active Universe  : 17 Pairs")
    print("  Preservation Map : Tier 1 (< ₹200) = 1 Slot | Tier 2 (< ₹400) = 2 Slots | Tier 3 (< ₹700) = 3 Slots | Tier 4 = 4 Slots")
    print("  Safe Floor       : ₹650.00 INR minimum notional with exact contract divisibility")
    print("  Evaluation Logic : Completed 4-Hour Closed Candles (iloc[-2])")
    print("  Slot Mechanics   : Max 2/Sector | Break-Even (+1.8R) Recycles Slot | Unlock (+1.0R)")
    print("=" * 95)

    dfs = {}
    for sym in PAIRS_17:
        raw = fetch_historical_candles(sym, limit=1000)
        if not raw.empty and len(raw) > 52:
            dfs[sym] = compute_indicators(raw)

    if not dfs:
        print("[ERROR] Failed to load 4H candle data.")
        return

    common_times = sorted(list(set.intersection(*[set(df["datetime"]) for df in dfs.values()])))
    if len(common_times) < 52:
        ref_sym = list(dfs.keys())[0]
        common_times = dfs[ref_sym]["datetime"].tolist()

    sim = PortfolioSimulator(initial_capital=initial_capital, risk_pct=risk_pct)
    indexed_dfs = {sym: df.set_index("datetime") for sym, df in dfs.items()}
    total_bars = len(common_times)

    for idx in range(30, total_bars):
        current_time = common_times[idx]
        sim.update_daily_cycle(current_time)

        # 1. Manage Active Positions
        for pair in list(sim.active_positions.keys()):
            if pair not in indexed_dfs or current_time not in indexed_dfs[pair].index:
                continue

            pos = sim.active_positions[pair]
            bar = indexed_dfs[pair].loc[current_time]
            pos["bars"] += 1

            o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
            d = pos["d"]
            entry = pos["entry"]
            stop = pos["stop"]
            lev = pos["lev"]
            liq_dist_pct = 1.0 / lev

            gap_liq = (d == 1 and o <= entry * (1.0 - liq_dist_pct)) or (d == -1 and o >= entry * (1.0 + liq_dist_pct))
            hit_stop = (d == 1 and l <= stop) or (d == -1 and h >= stop)
            hit_liq = (d == 1 and l <= entry * (1.0 - liq_dist_pct)) or (d == -1 and h >= entry * (1.0 + liq_dist_pct))

            if gap_liq:
                sim.close_position(pair, entry * (1.0 - d * liq_dist_pct), current_time, "LIQUIDATION")
                continue
            elif hit_stop:
                exit_fill = min(o, stop) if d == 1 else max(o, stop)
                sim.close_position(pair, exit_fill, current_time, "STOP_LOSS")
                continue
            elif hit_liq:
                sim.close_position(pair, entry * (1.0 - d * liq_dist_pct), current_time, "LIQUIDATION")
                continue

            favorable_move = (h - entry) if d == 1 else (entry - l)
            if not pos.get("sector_unlocked", False) and favorable_move >= 1.0 * pos["rdist"]:
                pos["sector_unlocked"] = True

            if not pos["be"]:
                if favorable_move >= 1.8 * pos["rdist"]:
                    pos["be"] = True
                    be_level = round(entry * (1.0 + d * 0.0012), 4)
                    if (d == 1 and be_level > pos["stop"]) or (d == -1 and be_level < pos["stop"]):
                        pos["stop"] = be_level

            trail_buffer = round(entry * pos["safe_pct"], 4)
            if d == 1:
                cand_stop = round(h - trail_buffer, 4)
                if cand_stop > pos["stop"] and cand_stop < c:
                    pos["stop"] = cand_stop
            else:
                cand_stop = round(l + trail_buffer, 4)
                if cand_stop < pos["stop"] and cand_stop > c:
                    pos["stop"] = cand_stop

            trail_close_level = bar["trail_low_7"] if d == 1 else bar["trail_high_7"]
            hit_donch_exit = (d == 1 and c <= trail_close_level and c <= pos["stop"]) or (d == -1 and c >= trail_close_level and c >= pos["stop"])
            if hit_donch_exit:
                sim.close_position(pair, c, current_time, "DONCHIAN_CLOSE")
                continue

        # 2. Risk Slot Capacity Check
        max_slots = sim.get_max_slots()
        risky_positions = [p for p in sim.active_positions.values() if not p.get("be", False)]
        if len(risky_positions) >= max_slots or sim.is_halted:
            continue

        free_margin = sim.equity - sim.get_margin_in_use()
        if free_margin < 30.0:
            continue

        # 3. Scan Hybrid Signals across 17 Pairs
        candidates = []
        for pair in PAIRS_17:
            if pair in sim.active_positions or pair not in indexed_dfs:
                continue

            pair_sec = SECTORS_17.get(pair, "OTHER")
            locked_sector_count = sum(
                1 for p in sim.active_positions.values()
                if SECTORS_17.get(p["pair"], "OTHER") == pair_sec and not p.get("sector_unlocked", False) and not p.get("be", False)
            )
            if locked_sector_count >= 2:
                continue

            bar = indexed_dfs[pair].loc[current_time]
            c, o, h, l = bar["close"], bar["open"], bar["high"], bar["low"]
            ema = bar["ema_50"]
            dh = bar["donch_high_14"]
            dl = bar["donch_low_14"]
            mom = bar["mom"]
            mom_slope = bar["mom_slope"]
            squeeze_recent = bar["squeeze_recent"]
            vol_sma = bar["vol_sma_20"]

            donch_long = (c > dh) and (c > ema)
            donch_short = (c < dl) and (c < ema)

            squeeze_long = squeeze_recent and (c > ema) and (c > bar["bb_mid"]) and (c > o) and (mom > 0) and (mom_slope > 0) and (bar["volume"] >= vol_sma * 1.15)
            squeeze_short = squeeze_recent and (c < ema) and (c < bar["bb_mid"]) and (c < o) and (mom < 0) and (mom_slope < 0) and (bar["volume"] >= vol_sma * 1.15)

            if squeeze_long:
                score, lev, safe_pct = calculate_confidence_score(bar, 1, "SQUEEZE")
                stop_cand = max(bar["trail_low_7"], bar["kc_lower"])
                candidates.append((pair, 1, c, stop_cand, score, lev, safe_pct, "SQUEEZE"))
            elif donch_long:
                score, lev, safe_pct = calculate_confidence_score(bar, 1, "DONCHIAN")
                candidates.append((pair, 1, c, bar["trail_low_7"], score, lev, safe_pct, "DONCHIAN"))
            elif squeeze_short:
                score, lev, safe_pct = calculate_confidence_score(bar, -1, "SQUEEZE")
                stop_cand = min(bar["trail_high_7"], bar["kc_upper"])
                candidates.append((pair, -1, c, stop_cand, score, lev, safe_pct, "SQUEEZE"))
            elif donch_short:
                score, lev, safe_pct = calculate_confidence_score(bar, -1, "DONCHIAN")
                candidates.append((pair, -1, c, bar["trail_high_7"], score, lev, safe_pct, "DONCHIAN"))

        candidates.sort(key=lambda x: -x[4])

        for pair, d, entry_signal, raw_stop, score, lev, safe_pct, entry_type in candidates:
            risky_positions = [p for p in sim.active_positions.values() if not p.get("be", False)]
            if len(risky_positions) >= max_slots:
                break

            price_inr = entry_signal * USD_INR_RATE
            target_risk_inr = sim.equity * sim.risk_pct
            calculated_notional = target_risk_inr / safe_pct
            notional_inr = max(COINDCX_MIN_NOTIONAL_INR, calculated_notional)

            req_margin = (notional_inr / lev) * 1.05
            if req_margin > (sim.equity - sim.get_margin_in_use()):
                continue

            tech_sp = abs(entry_signal - raw_stop) / entry_signal
            if tech_sp > safe_pct:
                final_stop = round(entry_signal * (1.0 - d * safe_pct), 4)
            else:
                final_stop = round(raw_stop, 4)

            raw_qty = notional_inr / price_inr
            spec = PAIR_SPECS.get(pair, {"step": 0.1, "decimals": 1})
            step = spec["step"]
            decs = spec["decimals"]
            step_units = math.ceil(round(raw_qty / step, 6))
            computed_qty = step_units * step

            if decs == 0:
                qty = float(int(round(computed_qty)))
            else:
                qty = round(computed_qty, decs)

            final_notional = qty * price_inr
            rdist = abs(entry_signal - final_stop)

            sim.active_positions[pair] = {
                "pair": pair,
                "entry_type": entry_type,
                "d": d,
                "entry": entry_signal,
                "stop": final_stop,
                "rdist": rdist,
                "qty": qty,
                "lev": lev,
                "score": score,
                "safe_pct": safe_pct,
                "margin_inr": req_margin,
                "notional_inr": final_notional,
                "entry_time": current_time,
                "bars": 0,
                "be": False,
                "sector_unlocked": False,
            }

    trades = sim.closed_trades
    print("-" * 95)
    print("                   17-PAIR PERFORMANCE SUMMARY")
    print("-" * 95)

    if not trades:
        print("  [Notice] No completed trades recorded.")
        return

    wins = [t for t in trades if t["net_inr"] > 0]
    losses = [t for t in trades if t["net_inr"] <= 0]
    liquidations = [t for t in trades if t["reason"] == "LIQUIDATION"]

    win_rate = (len(wins) / len(trades)) * 100.0
    tot_profit = sum(t["net_inr"] for t in wins)
    tot_loss = abs(sum(t["net_inr"] for t in losses))
    pf = (tot_profit / tot_loss) if tot_loss > 0 else 99.0

    avg_win = (tot_profit / len(wins)) if wins else 0.0
    avg_loss = (tot_loss / len(losses)) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    net_return_inr = sim.equity - initial_capital
    return_pct = (net_return_inr / initial_capital) * 100.0

    streak = 0
    max_streak = 0
    for t in trades:
        if t["net_inr"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    reasons = Counter(t["reason"] for t in trades)
    lev_counts = Counter(t["leverage"] for t in trades)

    print(f"  Starting Balance       : ₹{initial_capital:.2f}")
    print(f"  Ending Wallet Balance  : ₹{sim.equity:.2f}")
    print(f"  Total Net Profit/Loss  : ₹{net_return_inr:+.2f} ({return_pct:+.1f}%)")
    print(f"  Max Portfolio Drawdown : {sim.max_drawdown_pct:.2f}%")
    print(f"  Completed Trades       : {len(trades)} trades ({len(wins)} Wins / {len(losses)} Losses)")
    print(f"  Trade Pace             : {len(trades) / (total_bars / 6):.2f} trades/day")
    print(f"  Win Rate               : {win_rate:.1f}%")
    print(f"  Payoff Ratio           : {payoff:.2f} : 1  (Avg Win: ₹{avg_win:.2f} | Avg Loss: ₹{avg_loss:.2f})")
    print(f"  Profit Factor (PF)     : {pf:.2f}")
    print(f"  Max Consecutive Losses : {max_streak} losses in a row")
    print(f"  Leverage Distribution  : {dict(lev_counts)}")
    print(f"  Exit Breakdown         : {dict(reasons)}")
    print(f"  Liquidations Hit       : {len(liquidations)}")
    print("=" * 95)

    print("\n" + "-" * 90)
    print(f" {'PAIR':<12} | {'SECTOR':<8} | {'TRADES':<7} | {'WIN RATE':<9} | {'PAYOFF':<9} | {'NET PNL (INR)':<14} | {'PF'}")
    print("-" * 90)
    pair_groups = defaultdict(list)
    for t in trades:
        pair_groups[t["pair"]].append(t)

    for p in sorted(pair_groups.keys(), key=lambda x: -sum(t["net_inr"] for t in pair_groups[x])):
        p_trades = pair_groups[p]
        p_wins = [t for t in p_trades if t["net_inr"] > 0]
        p_wr = (len(p_wins) / len(p_trades)) * 100.0
        p_net = sum(t["net_inr"] for t in p_trades)
        p_win_sum = sum(t["net_inr"] for t in p_wins)
        p_loss_sum = abs(sum(t["net_inr"] for t in p_trades if t["net_inr"] <= 0))
        p_pf = (p_win_sum / p_loss_sum) if p_loss_sum > 0 else 99.0
        p_payoff = (np.mean([t["net_inr"] for t in p_wins]) / abs(np.mean([t["net_inr"] for t in p_trades if t["net_inr"] <= 0]))) if (p_wins and p_loss_sum > 0) else 0.0
        sec = SECTORS_17.get(p, "OTHER")
        print(f" {p:<12} | {sec:<8} | {len(p_trades):<7} | {p_wr:>5.1f}%   | {p_payoff:>5.2f}:1  | ₹{p_net:>+11.2f}  | {p_pf:>5.2f}")
    print("-" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=148.18, help="Starting capital in INR (default: 148.18)")
    parser.add_argument("--risk", type=float, default=0.045, help="Risk fraction per trade (default 0.045 = 4.5%)")
    args = parser.parse_args()

    run_squeeze_backtest(initial_capital=args.capital, risk_pct=args.risk)


if __name__ == "__main__":
    main()