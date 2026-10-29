"""
risk_manager.py - Portfolio Risk, Sizing, and Sector Management
Ladder Configuration:
  - Tier 1 (< ₹200): 1 Slot
  - Tier 2 (< ₹400): 2 Slots
  - Tier 3 (< ₹700): 3 Slots
  - Tier 4 (>= ₹700): 4 Slots
Rules:
  - 17 Pairs mapped across L1, DEFI, GAMING, and LEGACY sectors
  - Max 2 concurrent positions per sector (released on +1.0R or BE)
  - 4.5% Fixed-Risk Compounding with ₹650.00 Floor
  - Strict Step-Size Rounding matching CoinDCX Futures
"""
from __future__ import annotations

import math

USD_INR_RATE = 102.00
COINDCX_MIN_NOTIONAL_INR = 650.0

SECTORS_17 = {
    # Layer 1 (7)
    "SOLUSDT": "L1", "SEIUSDT": "L1", "ADAUSDT": "L1", "INJUSDT": "L1",
    "SUIUSDT": "L1", "XTZUSDT": "L1", "ATOMUSDT": "L1",
    # DeFi (4)
    "GRTUSDT": "DEFI", "UNIUSDT": "DEFI", "RUNEUSDT": "DEFI", "KAVAUSDT": "DEFI",
    # Gaming (2)
    "ENJUSDT": "GAMING", "SANDUSDT": "GAMING",
    # Legacy (4)
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


class RiskManager:
    def __init__(self, initial_equity: float = 148.18, risk_pct: float = 0.045):
        self.equity = initial_equity
        self.risk_pct = risk_pct

    def get_ladder_tier(self) -> tuple[float, int]:
        eq = self.equity
        if eq < 200.0:
            return self.risk_pct, 1  # Tier 1: 1 Slot
        elif eq < 400.0:
            return self.risk_pct, 2  # Tier 2: 2 Slots
        elif eq < 700.0:
            return self.risk_pct, 3  # Tier 3: 3 Slots
        else:
            return self.risk_pct, 4  # Tier 4: 4 Slots

    def is_sector_locked(self, pair: str, active_positions: dict) -> bool:
        sec = SECTORS_17.get(pair, "OTHER")
        locked_count = 0
        for p in active_positions.values():
            if SECTORS_17.get(p.get("pair"), "OTHER") == sec:
                if not p.get("sector_unlocked", False) and not p.get("be", False):
                    locked_count += 1
        return locked_count >= 2

    def calculate_order_qty(
        self,
        pair: str,
        entry_price: float,
        raw_stop_price: float,
        free_margin_available: float,
        direction: int,
        signal_lev: int = 14,
        signal_safe_pct: float = 0.05,
    ) -> tuple[float | None, float, int, float]:
        if entry_price <= 0:
            return None, 0.0, signal_lev, 0.0

        tech_dist_pct = abs(entry_price - raw_stop_price) / entry_price
        if tech_dist_pct > signal_safe_pct:
            safe_stop_px = round(entry_price * (1.0 - direction * signal_safe_pct), 4)
        else:
            safe_stop_px = round(raw_stop_price, 4)

        target_risk_inr = self.equity * self.risk_pct
        calculated_notional = target_risk_inr / signal_safe_pct
        notional_inr = max(COINDCX_MIN_NOTIONAL_INR, calculated_notional)
        req_margin = (notional_inr / signal_lev) * 1.05

        if req_margin > free_margin_available:
            notional_inr = COINDCX_MIN_NOTIONAL_INR
            req_margin = (notional_inr / signal_lev) * 1.05
            if req_margin > free_margin_available:
                return None, 0.0, signal_lev, safe_stop_px

        price_inr = entry_price * USD_INR_RATE
        raw_qty = notional_inr / price_inr

        clean_pair = pair.replace("B-", "").replace("_USDT", "USDT")
        spec = PAIR_SPECS.get(clean_pair, {"step": 0.1, "decimals": 1})
        step = spec["step"]
        decs = spec["decimals"]

        step_units = math.ceil(round(raw_qty / step, 6))
        computed_qty = step_units * step

        if decs == 0:
            qty = float(int(round(computed_qty)))
        else:
            qty = round(computed_qty, decs)

        final_notional_inr = qty * price_inr
        if qty <= 0:
            return None, 0.0, signal_lev, safe_stop_px

        return qty, final_notional_inr, signal_lev, safe_stop_px