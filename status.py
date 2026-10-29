"""
status.py - Zero-Flicker Live Position & Roadmap Monitor (17-Pair Hybrid Engine)
Features:
  - Clean Dual-Grid Layout: Active Positions Table + Compact SL Roadmap Table
  - Exact PnL at Stop-Loss: Real-time INR gain/loss if current SL is touched
  - Dynamic Slot Capacity Ladder (Tier 1: <₹200, Tier 2: <₹400, Tier 3: <₹700, Tier 4: >=₹700)
  - Anti-Ghost Screen Buffer: Erases trailing text with \\033[J to prevent duplicate lines
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib3
import requests

# Enable ANSI escape sequences in Windows Command Prompt
os.system("")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = Path("bot_state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}


def get_live_tick_price(pair: str) -> float:
    clean_sym = pair.replace("B-", "").replace("_USDT", "USDT")
    endpoints = [
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={clean_sym}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={clean_sym}",
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={clean_sym}",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=HEADERS, timeout=2.0, verify=False)
            if r.status_code == 200:
                data = r.json()
                if "price" in data:
                    val = float(data["price"])
                    if val > 0:
                        return val
                elif "result" in data and "list" in data["result"]:
                    val = float(data["result"]["list"][0].get("lastPrice", 0.0))
                    if val > 0:
                        return val
        except Exception:
            continue
    return 0.0


def get_time_to_next_4h_close() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    current_hour = now_utc.hour
    next_close_hour = ((current_hour // 4) + 1) * 4

    if next_close_hour >= 24:
        target_utc = (now_utc + timedelta(days=1)).replace(
            hour=next_close_hour - 24, minute=0, second=0, microsecond=0
        )
    else:
        target_utc = now_utc.replace(
            hour=next_close_hour, minute=0, second=0, microsecond=0
        )

    diff_sec = max(0, int((target_utc - now_utc).total_seconds()))
    hours = diff_sec // 3600
    minutes = (diff_sec % 3600) // 60
    seconds = diff_sec % 60

    target_ist = target_utc.astimezone(IST)
    target_str = target_ist.strftime("%I:%M %p IST")
    cd_str = f"T-{hours:02d}h {minutes:02d}m {seconds:02d}s"
    return target_str, cd_str


def get_ladder_slots(equity: float) -> tuple[int, str, str]:
    if equity < 200.0:
        return 1, "Tier 1 (< ₹200)", f"₹{200.0 - equity:.2f} to Tier 2 (2 Slots)"
    elif equity < 400.0:
        return 2, "Tier 2 (< ₹400)", f"₹{400.0 - equity:.2f} to Tier 3 (3 Slots)"
    elif equity < 700.0:
        return 3, "Tier 3 (< ₹700)", f"₹{700.0 - equity:.2f} to Tier 4 (4 Slots)"
    else:
        return 4, "Tier 4 (>= ₹700)", "Max Capacity (4 Slots Active)"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def format_px(val: float) -> str:
    if val <= 0:
        return "$0.00"
    if val >= 10.0:
        return f"${val:.2f}"
    elif val >= 1.0:
        return f"${val:.4f}"
    else:
        return f"${val:.5f}"


def render_monitor():
    state = load_state()
    active = state.get("active_positions", {})
    equity = float(state.get("equity", 248.18))
    peak_equity = float(state.get("peak_equity", equity))
    day_start_eq = float(state.get("day_start_equity", equity))
    daily_pnl = float(state.get("daily_pnl", 0.0))
    is_halted = state.get("halted", False)

    now_ist_str = datetime.now(timezone.utc).astimezone(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")
    next_bar_time, next_bar_cd = get_time_to_next_4h_close()

    total_slots, tier_name, tier_info = get_ladder_slots(equity)
    risky_positions = [p for p in active.values() if not p.get("be", False)]
    slots_used = len(risky_positions)
    slots_header_str = f"{slots_used}/{total_slots} Slots"

    total_margin_used = 0.0
    for p in active.values():
        q = float(p.get("qty", 0.0))
        e = float(p.get("entry", 0.0))
        lev = float(p.get("lev", 14.0))
        conv = float(p.get("conv_rate", 102.00))
        total_margin_used += (q * e * conv) / lev

    free_margin = max(0.0, equity - total_margin_used)
    max_daily_loss = day_start_eq * 0.50
    buffer_left = max(0.0, max_daily_loss + daily_pnl)

    W = 114
    lines = []

    lines.append("=" * W)
    lines.append(f"COINDCX 4H HYBRID ENGINE | 17 ELITE PAIRS | {now_ist_str}".center(W))
    lines.append("=" * W)

    if not active:
        lines.append(f"  Active Positions: None ({slots_header_str}) | Monitoring 17 Pairs for 4H Setups".center(W))
        lines.append("-" * W)
    else:
        # Table 1: Live Positions
        lines.append(
            f" {'PAIR':<9} | {'SIDE':<5} | {'LEV':<4} | {'QTY':<7} | {'ENTRY':<9} | {'MARK':<9} | {'ACTIVE SL':<9} | {'LIVE PNL (INR)':<17} | {'IF SL HIT':<17} | {'BUFFER'}"
        )
        lines.append("-" * W)

        roadmaps = []

        for pair, pos in active.items():
            entry = float(pos.get("entry", 0.0))
            mark = get_live_tick_price(pair) or float(pos.get("curr_price", entry))
            qty = float(pos.get("qty", 0.0))
            lev = float(pos.get("lev", 14.0))
            d = int(pos.get("d", 1))
            stop = float(pos.get("stop", 0.0))
            conv = float(pos.get("conv_rate", 102.00))
            safe_pct = float(pos.get("safe_pct", (1.0 / lev) * 0.70))
            rdist = float(pos.get("rdist", abs(entry - stop)))
            if rdist <= 0:
                rdist = abs(entry * safe_pct)

            pnl_inr = (mark - entry) * qty * d * conv
            pnl_pct = ((mark - entry) / entry) * d * lev * 100.0
            side_str = "LONG" if d == 1 else "SHORT"

            sl_pnl_inr = (stop - entry) * qty * d * conv
            sl_pnl_pct = ((stop - entry) / entry) * d * lev * 100.0

            dist_pct = abs(mark - stop) / mark * 100.0
            buf_str = f"{dist_pct:.1f}%"

            pnl_str = f"₹{pnl_inr:+.2f} ({pnl_pct:+.1f}%)"
            sl_pnl_str = f"₹{sl_pnl_inr:+.2f} ({sl_pnl_pct:+.1f}%)"

            lines.append(
                f" {pair:<9} | {side_str:<5} | {int(lev):<2}x  | {qty:<7.1f} | {format_px(entry):<9} | {format_px(mark):<9} | {format_px(stop):<9} | {pnl_str:<17} | {sl_pnl_str:<17} | {buf_str}"
            )

            init_stop = float(pos.get("initial_stop", entry - d * rdist))
            target_1r = round(entry + d * 1.0 * rdist, 5)
            target_18r = round(entry + d * 1.8 * rdist, 5)

            trail_buffer = round(entry * safe_pct, 5)
            next_trail_trigger = round(stop - trail_buffer, 5) if d == -1 else round(stop + trail_buffer, 5)
            favorable_move = (entry - mark) if d == -1 else (mark - entry)

            roadmaps.append({
                "pair": pair,
                "d": d,
                "mark": mark,
                "stop": stop,
                "init_stop": init_stop,
                "target_1r": target_1r,
                "target_18r": target_18r,
                "next_trail": next_trail_trigger,
                "is_1r": pos.get("sector_unlocked", favorable_move >= 1.0 * rdist),
                "is_be": pos.get("be", favorable_move >= 1.8 * rdist),
            })

        lines.append("-" * W)

        # Table 2: Structured SL Milestones & Targets
        lines.append("  SL TRAILING & TARGET ROADMAP")
        lines.append(
            f" {'PAIR':<9} | {'INITIAL SL':<10} | {'ACTIVE SL (SAVED)':<23} | {'NEXT TRAIL AT':<18} | {'+1.0R (SECTOR)':<18} | {'+1.8R (BE FREE)'}"
        )
        lines.append("-" * W)

        for r in roadmaps:
            pair = r["pair"]
            d = r["d"]
            mark = r["mark"]

            shift_diff = abs(r["stop"] - r["init_stop"])
            if shift_diff > 1e-5:
                saved_str = f"{format_px(r['stop'])} (+${shift_diff:.4f})"
            else:
                saved_str = f"{format_px(r['stop'])} (Base)"

            # Next Trail Trigger
            if (d == -1 and mark <= r["next_trail"]) or (d == 1 and mark >= r["next_trail"]):
                trail_str = "[ACTIVE TRAIL]"
            else:
                dist_trail = abs(mark - r["next_trail"]) / mark * 100.0
                trail_str = f"{format_px(r['next_trail'])} (-{dist_trail:.1f}%)"

            # 1.0R Target
            if r["is_1r"]:
                r1_str = "[UNLOCKED]"
            else:
                dist_1r = abs(mark - r["target_1r"]) / mark * 100.0
                r1_str = f"{format_px(r['target_1r'])} (-{dist_1r:.1f}%)"

            # 1.8R Target
            if r["is_be"]:
                r18_str = "[BE LOCKED]"
            else:
                dist_18r = abs(mark - r["target_18r"]) / mark * 100.0
                r18_str = f"{format_px(r['target_18r'])} (-{dist_18r:.1f}%)"

            lines.append(
                f" {pair:<9} | {format_px(r['init_stop']):<10} | {saved_str:<23} | {trail_str:<18} | {r1_str:<18} | {r18_str}"
            )
        lines.append("-" * W)

    # Clean 2-Column Summary Box
    col1_w = 53
    col2_w = 53

    line1_l = f" Futures Wallet   : ₹{equity:.2f} (Live)"
    line1_r = f" Peak Capital     : ₹{peak_equity:.2f}"

    line2_l = f" Active Slots     : {slots_used} / {total_slots} Slots Used ({tier_name})"
    line2_r = f" Ladder Status    : {tier_info}"

    line3_l = f" Free Margin      : ₹{free_margin:.2f}"
    line3_r = f" Margin In Use    : ₹{total_margin_used:.2f}"

    line4_l = f" Today's Realized : ₹{daily_pnl:+.2f}"
    line4_r = f" 50% Daily Buffer : ₹{buffer_left:.2f}"

    status_tag = "HALTED (Daily Cap)" if is_halted else "ACTIVE"
    line5_l = f" Next 4H Candle   : {next_bar_time} ({next_bar_cd})"
    line5_r = f" Bot Status       : {status_tag}"

    lines.append(f" {line1_l:<{col1_w}} | {line1_r:<{col2_w}}")
    lines.append(f" {line2_l:<{col1_w}} | {line2_r:<{col2_w}}")
    lines.append(f" {line3_l:<{col1_w}} | {line3_r:<{col2_w}}")
    lines.append(f" {line4_l:<{col1_w}} | {line4_r:<{col2_w}}")
    lines.append(f" {line5_l:<{col1_w}} | {line5_r:<{col2_w}}")
    lines.append("=" * W)

    # \033[H repositions cursor to top-left, \033[K clears each line, \033[J clears trailing ghost lines
    output = "\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J"
    sys.stdout.write(output)
    sys.stdout.flush()


def main():
    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\033[?25l")  # Hide terminal cursor
    sys.stdout.flush()

    try:
        while True:
            render_monitor()
            time.sleep(3)
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h\n[MONITOR STOPPED]\n")
        sys.stdout.flush()
        sys.exit(0)
    except Exception:
        time.sleep(3)


if __name__ == "__main__":
    main()