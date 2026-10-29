"""
strategy_v4.py - 4-Hour Volatility Squeeze + Donchian Hybrid Strategy
Universe: 17 Pairs (7 L1, 4 DeFi, 2 Gaming, 4 Legacy)
Execution: Strictly closed 4H candles (iloc[-2])
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# The Verified 17-Pair Universe
PAIRS_17 = [
    # Layer 1 Momentum (7)
    "SOLUSDT", "SEIUSDT", "ADAUSDT", "INJUSDT", "SUIUSDT", "XTZUSDT", "ATOMUSDT",
    # DeFi Protocols (4)
    "GRTUSDT", "UNIUSDT", "RUNEUSDT", "KAVAUSDT",
    # Gaming & Metaverse (2)
    "ENJUSDT", "SANDUSDT",
    # Legacy & Payments (4)
    "BCHUSDT", "BNBUSDT", "DOGEUSDT", "IOTAUSDT",
]


class MacroTrendStrategy4H:
    def __init__(self, donchian_n: int = 14, trail_n: int = 7, ema_span: int = 50):
        self.donchian_n = donchian_n
        self.trail_n = trail_n
        self.ema_span = ema_span

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_50"] = df["close"].ewm(span=self.ema_span, adjust=False).mean()

        df["donch_high_14"] = df["high"].shift(1).rolling(self.donchian_n).max()
        df["donch_low_14"] = df["low"].shift(1).rolling(self.donchian_n).min()
        df["trail_high_7"] = df["high"].shift(1).rolling(self.trail_n).max()
        df["trail_low_7"] = df["low"].shift(1).rolling(self.trail_n).min()

        df["vol_sma_20"] = df["volume"].rolling(20).mean()

        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["close"].shift(1)).abs()
        tr3 = (df["low"] - df["close"].shift(1)).abs()
        df["atr_20"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(20).mean()

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

    def generate_signals(self, df: pd.DataFrame, pair: str) -> list[dict]:
        if len(df) < 52:
            return []

        df = self.compute_indicators(df)
        bar = df.iloc[-2]  # Strict closed candle execution

        c, o, h, l = bar["close"], bar["open"], bar["high"], bar["low"]
        ema = bar["ema_50"]
        dh = bar["donch_high_14"]
        dl = bar["donch_low_14"]
        mom = bar["mom"]
        mom_slope = bar["mom_slope"]
        squeeze_recent = bar["squeeze_recent"]
        vol_sma = bar["vol_sma_20"] if bar["vol_sma_20"] > 0 else 1.0
        vol_ratio = bar["volume"] / vol_sma

        donch_long = (c > dh) and (c > ema)
        donch_short = (c < dl) and (c < ema)

        sqz_long = squeeze_recent and (c > ema) and (c > bar["bb_mid"]) and (c > o) and (mom > 0) and (mom_slope > 0) and (vol_ratio >= 1.15)
        sqz_short = squeeze_recent and (c < ema) and (c < bar["bb_mid"]) and (c < o) and (mom < 0) and (mom_slope < 0) and (vol_ratio >= 1.15)

        signals = []
        if sqz_long:
            score, lev, safe_pct = self._score_setup(bar, 1, "SQUEEZE", vol_ratio)
            stop = max(bar["trail_low_7"], bar["kc_lower"])
            signals.append({"pair": pair, "dir": "LONG", "entry": c, "stop": stop, "score": score, "lev": lev, "safe_pct": safe_pct, "entry_type": "SQUEEZE", "vol_ratio": vol_ratio})
        elif donch_long:
            score, lev, safe_pct = self._score_setup(bar, 1, "DONCHIAN", vol_ratio)
            signals.append({"pair": pair, "dir": "LONG", "entry": c, "stop": bar["trail_low_7"], "score": score, "lev": lev, "safe_pct": safe_pct, "entry_type": "DONCHIAN", "vol_ratio": vol_ratio})
        elif sqz_short:
            score, lev, safe_pct = self._score_setup(bar, -1, "SQUEEZE", vol_ratio)
            stop = min(bar["trail_high_7"], bar["kc_upper"])
            signals.append({"pair": pair, "dir": "SHORT", "entry": c, "stop": stop, "score": score, "lev": lev, "safe_pct": safe_pct, "entry_type": "SQUEEZE", "vol_ratio": vol_ratio})
        elif donch_short:
            score, lev, safe_pct = self._score_setup(bar, -1, "DONCHIAN", vol_ratio)
            signals.append({"pair": pair, "dir": "SHORT", "entry": c, "stop": bar["trail_high_7"], "score": score, "lev": lev, "safe_pct": safe_pct, "entry_type": "DONCHIAN", "vol_ratio": vol_ratio})

        return signals

    def _score_setup(self, bar: pd.Series, d: int, entry_type: str, vol_ratio: float) -> tuple[float, int, float]:
        c, h, l, ema = bar["close"], bar["high"], bar["low"], bar["ema_50"]
        bar_range = max(h - l, 1e-8)
        atr = bar["atr_20"] if bar["atr_20"] > 0 else bar_range

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

        safe_stop_pct = (1.0 / leverage) * 0.70
        return total_score, leverage, safe_stop_pct