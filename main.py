"""
main.py - 4-Hour Volatility Squeeze + Donchian Hybrid Engine for CoinDCX Futures
Production Setup:
  - 17 Pairs: SOL, SEI, ADA, INJ, SUI, XTZ, ATOM, GRT, UNI, RUNE, KAVA, ENJ, SAND, BCH, BNB, DOGE, IOTA
  - Allocation: 4.5% Fixed-Risk Compounding (₹650 Safe Floor)
  - Anti-Chase Drift Guard: 2.0% (MAX_PRICE_DRIFT_PCT = 0.020)
  - 30-Second Wallet Sync: Live balance and free margin auto-refresh continuously
  - Network-Resilient Sync: Prevents Wi-Fi/API drops from clearing active state
  - Margin Routing: Explicit INR Margin (margin_currency="INR")
  - Live Tick Dashboard: Direct real-time price queries & 15-second disk heartbeat
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib3
import requests
from dotenv import load_dotenv

from coindcx_client import CoinDCXClient
from market_stream import MarketStream
from risk_manager import RiskManager
from strategy_v4 import MacroTrendStrategy4H, PAIRS_17

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
MANUAL_EQUITY = float(os.getenv("MANUAL_EQUITY_INR", "248.18"))

STATE_FILE = Path("bot_state.json")
KILL_TRIGGER = Path("KILL_SWITCH.trigger")
COOLDOWN_SECONDS = 300
MAX_PRICE_DRIFT_PCT = 0.020

_last_heartbeat_save = 0.0


def load_state() -> dict:
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    default_state = {
        "active_positions": {},
        "equity": MANUAL_EQUITY,
        "peak_equity": MANUAL_EQUITY,
        "day_start_equity": MANUAL_EQUITY,
        "last_ist_day": now_ist.strftime("%Y-%m-%d"),
        "last_scanned_bar": "",
        "daily_pnl": 0.0,
        "halted": False,
        "cooldowns": {},
    }
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                default_state.update(data)
                default_state.setdefault("cooldowns", {})
                default_state.setdefault("last_scanned_bar", "")
                if "day_start_equity" not in default_state or default_state["day_start_equity"] <= 0:
                    default_state["day_start_equity"] = default_state["equity"]
                default_state.setdefault("last_ist_day", now_ist.strftime("%Y-%m-%d"))
                return default_state
        except Exception:
            pass
    return default_state


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def to_coindcx_symbol(symbol: str) -> str:
    base = symbol.replace("USDT", "")
    return f"B-{base}_USDT"


def get_live_tick_price(pair: str) -> float:
    clean_sym = pair.replace("B-", "").replace("_USDT", "USDT")
    endpoints = [
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={clean_sym}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={clean_sym}",
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={clean_sym}",
    ]
    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, timeout=2.0, verify=False)
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


def reconcile_with_exchange(client: CoinDCXClient, state: dict) -> bool:
    if DRY_RUN:
        return False

    state_active = state.get("active_positions", {})

    try:
        exch_positions = client.get_active_positions()
    except Exception as e:
        print(f"[RECONCILIATION] Network/API error: {e}. Skipping sync to protect state.")
        return False

    if exch_positions is None:
        return False

    # Guard: If exchange returns 0 positions while local state has active trades, check network before deleting
    if len(exch_positions) == 0 and len(state_active) > 0:
        print("\n[WARNING] Exchange returned 0 positions while local state has active trades.")
        print("[PROTECTION] Verifying network connection before altering state...")
        test_price = get_live_tick_price("SANDUSDT")
        if test_price <= 0:
            print("[ABORT SYNC] Connection check failed (network offline). Retaining active trades.")
            return False

    exch_lookup = {}
    for p in exch_positions:
        clean_pair = str(p.get("pair", "")).replace("B-", "").replace("_USDT", "USDT")
        exch_lookup[clean_pair] = p

    state_changed = False

    # 1. Adopt orphan exchange positions
    for clean_pair, p in exch_lookup.items():
        if clean_pair not in state_active:
            qty = abs(float(p.get("active_pos") or 0.0))
            side_dir = 1 if float(p.get("active_pos") or 0.0) > 0 else -1
            avg_px = float(p.get("avg_price") or p.get("entry_price") or 0.0)
            lev = float(p.get("leverage") or 14.0)
            safe_pct = (1.0 / lev) * 0.70
            live_px = get_live_tick_price(clean_pair) or avg_px

            trail_buffer = round(avg_px * safe_pct, 4)
            if side_dir == -1 and live_px < avg_px:
                stop_px = round(min(avg_px * (1.0 + safe_pct), live_px + trail_buffer), 4)
            elif side_dir == 1 and live_px > avg_px:
                stop_px = round(max(avg_px * (1.0 - safe_pct), live_px - trail_buffer), 4)
            else:
                stop_px = round(avg_px * (1.0 - side_dir * safe_pct), 4)

            state_active[clean_pair] = {
                "pair": clean_pair,
                "d": side_dir,
                "tf": "4H",
                "entry_type": "ADOPTED",
                "qty": qty,
                "lev": lev,
                "safe_pct": safe_pct,
                "entry": avg_px,
                "curr_price": live_px,
                "stop": stop_px,
                "rdist": round(abs(avg_px - stop_px), 4),
                "be": False,
                "sector_unlocked": False,
                "open_time": time.time(),
                "conv_rate": float(p.get("settlement_currency_conversion_price") or 102.00),
            }
            state_changed = True
            print(f"\n[RECONCILIATION] Adopted exchange position: {clean_pair} (SL: ${stop_px:.4f})")

    # 2. Only delete if exchange explicitly confirmed closure while online
    for clean_pair in list(state_active.keys()):
        if clean_pair not in exch_lookup and len(exch_lookup) > 0:
            print(f"\n[RECONCILIATION] Position {clean_pair} confirmed closed on CoinDCX. Syncing local state.")
            del state_active[clean_pair]
            state_changed = True

    if state_changed:
        state["active_positions"] = state_active
        save_state(state)

    return state_changed


def check_and_apply_midnight_ist_reset(state: dict, risk_mgr: RiskManager) -> bool:
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    current_ist_day = now_ist.strftime("%Y-%m-%d")

    if current_ist_day != state.get("last_ist_day"):
        state["last_ist_day"] = current_ist_day
        state["day_start_equity"] = risk_mgr.equity
        state["daily_pnl"] = 0.0
        state["halted"] = False

        max_loss = round(risk_mgr.equity * 0.50, 2)
        print("\n" + "=" * 95)
        print(f" [12:00 AM IST MIDNIGHT CYCLE RESET] New Day: {current_ist_day} IST")
        print(f"  • Starting Capital      : ₹{risk_mgr.equity:.2f}")
        print(f"  • 50% Daily Loss Limit : ₹{max_loss:.2f}")
        print("  • Trading Status        : ACTIVE / RESUMED")
        print("=" * 95 + "\n")
        save_state(state)
        return True

    return False


def print_cmd_dashboard(
    equity: float,
    active: dict,
    daily_pnl: float,
    peak: float,
    slots_str: str,
    bal_source: str,
    free_cash: float,
    margin_used: float,
    cooldowns: dict,
    state: dict,
):
    now_ist_str = datetime.now(timezone.utc).astimezone(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")
    day_start_eq = float(state.get("day_start_equity", equity))
    max_daily_loss = day_start_eq * 0.50
    loss_remaining = max(0.0, max_daily_loss + daily_pnl)

    print("\n" + "=" * 102)
    print(f"       COINDCX 4H HYBRID ENGINE (17 PAIRS | 4.5% ALLOCATION) | {now_ist_str}")
    print("=" * 102)

    if not active:
        print("  Active Positions: None (0 Open) | Monitoring 17 Pairs for 4H Squeeze & Donchian setups")
    else:
        print(
            f" {'PAIR':<9} | {'TYPE':<8} | {'SIDE':<5} | {'LEV':<4} | {'QTY':<5} | {'ENTRY':<8} | {'MARK':<8} | {'STOP':<8} | {'PNL (INR)':<13} | {'STATUS'}"
        )
        print("-" * 102)
        for pair, pos in active.items():
            entry = float(pos["entry"])
            mark = get_live_tick_price(pair) or float(pos.get("curr_price", entry))
            pos["curr_price"] = mark

            qty = float(pos["qty"])
            lev = float(pos.get("lev", 14))
            d = int(pos["d"])
            stop = float(pos["stop"])
            conv_rate = float(pos.get("conv_rate", 102.00))
            etype = pos.get("entry_type", "HYBRID")[:8]

            pnl_inr = (mark - entry) * qty * d * conv_rate
            pnl_pct = ((mark - entry) / entry) * d * lev * 100.0

            side_str = "LONG" if d == 1 else "SHORT"
            if pos.get("be", False):
                status_str = "BE Locked (Free Slot)"
            elif pos.get("sector_unlocked", False):
                status_str = "+1.0R Unlocked"
            elif (d == 1 and mark > stop) or (d == -1 and mark < stop):
                dist_pct = abs(mark - stop) / mark * 100.0
                status_str = f"SL: {dist_pct:.1f}% buf"
            else:
                status_str = "Trailing"

            pnl_formatted = f"₹{pnl_inr:+.2f} ({pnl_pct:+.1f}%)"
            print(
                f" {pair:<9} | {etype:<8} | {side_str:<5} | {int(lev):<2}x  | {qty:<5.1f} | ${entry:<7.4f} | ${mark:<7.4f} | ${stop:<7.4f} | {pnl_formatted:<13} | {status_str}"
            )

    now = time.time()
    active_cds = [
        f"{p} ({int(COOLDOWN_SECONDS - (now - ts))}s left)"
        for p, ts in cooldowns.items()
        if (now - ts) < COOLDOWN_SECONDS
    ]
    cd_str = ", ".join(active_cds) if active_cds else "None"
    status_tag = "HALTED (50% Loss Breached)" if state.get("halted", False) else "ACTIVE"

    print("-" * 102)
    print(f"  Account Equity    : ₹{equity:.2f} ({bal_source})   | Peak Equity       : ₹{peak:.2f}")
    print(f"  12 AM IST Baseline: ₹{day_start_eq:.2f}                 | 50% Daily Loss Cap: ₹{max_daily_loss:.2f}")
    print(f"  Margin In Use     : ₹{margin_used:.2f}                 | Free Balance      : ₹{free_cash:.2f}")
    print(f"  Today's Realized  : ₹{daily_pnl:+.2f}                 | Remaining Buffer  : ₹{loss_remaining:.2f}")
    print(f"  Live Status       : {status_tag:<24} | Risk Slot Cap     : {slots_str}")
    print(f"  Active Cooldowns  : {cd_str}")
    print("=" * 102)


def execute_verified_exit(client: CoinDCXClient, pair: str, pos: dict, state: dict, risk_mgr: RiskManager):
    d = pos["d"]
    side = "buy" if d == -1 else "sell"
    entry_px = float(pos["entry"])
    qty = float(pos["qty"])
    conv_rate = float(pos.get("conv_rate", 102.00))
    lev = float(pos.get("lev", 14))

    exit_px = get_live_tick_price(pair) or float(pos.get("curr_price", entry_px))

    if not DRY_RUN:
        res = client.create_order(
            pair=to_coindcx_symbol(pair),
            side=side,
            order_type="market_order",
            qty=qty,
            leverage=int(lev),
            margin_currency="INR",
        )
        print(f"\n[EXIT ORDER DISPATCH] Pair: {pair} | Side: {side.upper()} | Response: {res}")

        is_closed = client.poll_position_closed(pair, max_attempts=6, delay=1.0)
        if not is_closed:
            print(f"[WARNING] Exchange still reports open position for {pair}! Position retained in state.")
            return

        final_exit_tick = get_live_tick_price(pair)
        if final_exit_tick > 0:
            exit_px = final_exit_tick

        pnl_inr = round((exit_px - entry_px) * qty * d * conv_rate, 2)
        fresh_wallet = client.get_wallet_details()
        if fresh_wallet.get("is_live"):
            actual_equity = fresh_wallet["total_equity"]
            state["equity"] = actual_equity
            state["daily_pnl"] = round(float(state.get("daily_pnl", 0.0)) + pnl_inr, 2)
            risk_mgr.equity = actual_equity
            print(f"[EXIT HANDSHAKE CONFIRMED] Realized: ₹{pnl_inr:+.2f} | New Wallet: ₹{actual_equity:.2f}")
    else:
        pnl_inr = round((exit_px - entry_px) * qty * d * conv_rate, 2)
        state["equity"] = round(float(state.get("equity", MANUAL_EQUITY)) + pnl_inr, 2)
        state["daily_pnl"] = round(float(state.get("daily_pnl", 0.0)) + pnl_inr, 2)

    if state["equity"] > state.get("peak_equity", 0.0):
        state["peak_equity"] = state["equity"]

    day_start_eq = float(state.get("day_start_equity", state["equity"]))
    max_daily_loss = day_start_eq * 0.50
    if state["daily_pnl"] <= -max_daily_loss:
        state["halted"] = True
        print(f"\n[DAILY LOSS LIMIT HALT TRIGGERED] Realized Loss ₹{state['daily_pnl']:.2f} crossed limit of ₹{max_daily_loss:.2f}")

    state.setdefault("cooldowns", {})[pair] = time.time()
    if pair in state.get("active_positions", {}):
        del state["active_positions"][pair]
    save_state(state)


def monitor_live_positions(client: CoinDCXClient, streamer: MarketStream, risk_mgr: RiskManager, strategy: MacroTrendStrategy4H, state: dict):
    global _last_heartbeat_save
    active = state.get("active_positions", {})
    if not active:
        return

    updated = False
    now = time.time()

    for pair, pos in list(active.items()):
        mark = get_live_tick_price(pair)
        if mark <= 0:
            continue

        pos["curr_price"] = mark
        d = pos["d"]
        stop = float(pos["stop"])
        entry = float(pos["entry"])
        lev = float(pos.get("lev", 14))
        safe_pct = float(pos.get("safe_pct", (1.0 / lev) * 0.70))
        trail_buffer = round(entry * safe_pct, 4)

        rdist = float(pos.get("rdist", abs(entry - stop)))
        favorable_move = (entry - mark) if d == -1 else (mark - entry)

        hit_stop = (d == 1 and mark <= stop) or (d == -1 and mark >= stop)
        if hit_stop:
            print(f"\n[INSTANT STOP TRIGGERED] {pair} touched stop at ${mark:.4f} (Stop: ${stop:.4f})")
            execute_verified_exit(client, pair, pos, state, risk_mgr)
            return

        if not pos.get("sector_unlocked", False) and favorable_move >= 1.0 * rdist:
            pos["sector_unlocked"] = True
            updated = True
            print(f"\n>>> [+1.0R UNLOCKED] {pair} reached 1.0R target! Sector limit released.")

        if not pos.get("be", False) and favorable_move >= 1.8 * rdist:
            pos["be"] = True
            be_target = round(entry * (1.0 - d * 0.0012), 4)
            if (d == 1 and be_target > pos["stop"]) or (d == -1 and be_target < pos["stop"]):
                old_stop = pos["stop"]
                pos["stop"] = be_target
                updated = True
                print(f"\n>>> [BREAK-EVEN LOCKED] {pair} reached +1.8R! Stop moved: ${old_stop:.4f} -> ${be_target:.4f}. Slot recycled!")

        if d == 1:
            candidate_stop = round(mark - trail_buffer, 4)
            if candidate_stop > stop and candidate_stop < mark:
                old_stop = pos["stop"]
                pos["stop"] = candidate_stop
                updated = True
                print(f"\n>>> [TRAILING STOP TIGHTENED] {pair} (LONG) Stop ratcheted up: ${old_stop:.4f} -> ${candidate_stop:.4f} (Mark: ${mark:.4f})")
        else:
            candidate_stop = round(mark + trail_buffer, 4)
            if candidate_stop < stop and candidate_stop > mark:
                old_stop = pos["stop"]
                pos["stop"] = candidate_stop
                updated = True
                print(f"\n>>> [TRAILING STOP TIGHTENED] {pair} (SHORT) Stop ratcheted down: ${old_stop:.4f} -> ${candidate_stop:.4f} (Mark: ${mark:.4f})")

    if updated or (now - _last_heartbeat_save) > 15.0:
        save_state(state)
        _last_heartbeat_save = now


def run_4h_cycle(client: CoinDCXClient, streamer: MarketStream, risk_mgr: RiskManager, strategy: MacroTrendStrategy4H):
    state = load_state()

    if KILL_TRIGGER.exists():
        print("\n[HALT] Manual Kill Switch Trigger Detected.")
        sys.exit(0)

    reconcile_with_exchange(client, state)

    for p_sym, p_data in state.get("active_positions", {}).items():
        live_px = get_live_tick_price(p_sym)
        if live_px > 0:
            p_data["curr_price"] = live_px

    wallet = client.get_wallet_details()
    if wallet.get("is_live"):
        risk_mgr.equity = wallet["total_equity"]
        state["equity"] = wallet["total_equity"]
        free_cash = wallet["free_inr"]
        margin_used = wallet["locked_inr"]
        bal_source = "CoinDCX Live Wallet"
    else:
        state_eq = float(state.get("equity", MANUAL_EQUITY))
        risk_mgr.equity = state_eq
        bal_source = "Manual Setting Sync"
        margin_used = sum(
            (float(p["qty"]) * float(p["entry"]) * float(p.get("conv_rate", 102.00))) / float(p.get("lev", 14))
            for p in state.get("active_positions", {}).values()
        )
        free_cash = max(0.0, risk_mgr.equity - margin_used)

    if risk_mgr.equity > state.get("peak_equity", 0.0):
        state["peak_equity"] = risk_mgr.equity

    check_and_apply_midnight_ist_reset(state, risk_mgr)

    day_start_eq = float(state.get("day_start_equity", risk_mgr.equity))
    max_daily_loss = day_start_eq * 0.50
    state["halted"] = state.get("daily_pnl", 0.0) <= -max_daily_loss

    risk_cur, total_slots = risk_mgr.get_ladder_tier()
    active = state.get("active_positions", {})

    now = time.time()
    cooldowns = state.get("cooldowns", {})
    state["cooldowns"] = {p: ts for p, ts in cooldowns.items() if (now - ts) < COOLDOWN_SECONDS}

    risky_positions = [p for p in active.values() if not p.get("be", False)]
    slots_str = f"{len(risky_positions)} / {total_slots} Risk Slots Used ({len(active)} Total Open)"

    print_cmd_dashboard(
        risk_mgr.equity,
        active,
        state.get("daily_pnl", 0.0),
        state["peak_equity"],
        slots_str,
        bal_source,
        free_cash,
        margin_used,
        state["cooldowns"],
        state,
    )

    if state["halted"] or len(risky_positions) >= total_slots or free_cash < 30.0:
        save_state(state)
        return

    # Scan 17 Pairs
    candidates = []
    for pair in PAIRS_17:
        if pair in active or risk_mgr.is_sector_locked(pair, active):
            continue

        df = streamer.fetch_pair_candles(pair, interval="4h", limit=100)
        if df.empty or len(df) < 52:
            continue

        sigs = strategy.generate_signals(df, pair)
        for s in sigs:
            candidates.append(s)

    candidates.sort(key=lambda x: (-x.get("score", 0), -x.get("vol_ratio", 1.0)))

    if candidates:
        print(f"\n[SIGNALS DETECTED] Found {len(candidates)} qualifying candidate(s):")
        for c_cand in candidates:
            print(f"  • {c_cand['pair']} | Side: {c_cand['dir']} | Type: {c_cand['entry_type']} | Signal Price: ${c_cand['entry']:.4f}")
    else:
        print("\n[SCAN COMPLETE] All 17 pairs scanned successfully. 0 qualifying signals (all pairs consolidating in range).")

    for sig in candidates:
        risky_positions = [p for p in active.values() if not p.get("be", False)]
        if len(risky_positions) >= total_slots:
            print("[CAPACITY REACHED] All risk slots are occupied.")
            break

        pair = sig["pair"]
        cooldown_exit_ts = state.get("cooldowns", {}).get(pair, 0)
        if (now - cooldown_exit_ts) < COOLDOWN_SECONDS:
            print(f"[COOLDOWN ACTIVE] {pair} exited recently, cooling down.")
            continue

        d = 1 if sig["dir"] == "LONG" else -1
        raw_stop_px = float(sig["stop"])
        signal_entry_px = float(sig["entry"])
        live_quote_px = get_live_tick_price(pair) or signal_entry_px

        drift_pct = abs(live_quote_px - signal_entry_px) / signal_entry_px
        if drift_pct > MAX_PRICE_DRIFT_PCT:
            print(f"[SKIP CHASE] {pair} drifted {drift_pct*100:.2f}% from 4H close (${signal_entry_px:.4f} -> ${live_quote_px:.4f} | Limit: 2.0%)")
            continue

        qty, notional_inr, lev, safe_stop = risk_mgr.calculate_order_qty(
            pair,
            live_quote_px,
            raw_stop_px,
            free_margin_available=free_cash,
            direction=d,
            signal_lev=sig.get("lev", 14),
            signal_safe_pct=sig.get("safe_pct", 0.05),
        )
        if qty is None or qty <= 0:
            print(f"[SIZING REJECTED] {pair} calculated qty was 0 or invalid margin.")
            continue

        side = "buy" if d == 1 else "sell"
        order_success = False
        actual_fill_px = live_quote_px
        actual_qty = qty
        conv_price = 102.00

        print(f"\n[DISPATCHING COINDCX ORDER] {pair} | Side: {side.upper()} | Qty: {qty} | Lev: {lev}x | Est Price: ${live_quote_px:.4f} | Notional: ₹{notional_inr:.2f}...")

        if not DRY_RUN:
            res = client.create_order(
                pair=to_coindcx_symbol(pair),
                side=side,
                order_type="market_order",
                qty=qty,
                leverage=int(lev),
                margin_currency="INR",
            )
            print(f"[COINDCX RAW RESPONSE]: {res}")

            is_error = False
            if isinstance(res, dict):
                if "error" in res or res.get("status") == "error" or ("code" in res and res.get("code") not in [200, 201]):
                    is_error = True

            if not is_error:
                filled_pos = client.poll_position_fill(pair, max_attempts=5, delay=1.0)
                if filled_pos:
                    actual_fill_px = float(filled_pos.get("avg_price") or filled_pos.get("entry_price") or actual_fill_px)
                    actual_qty = abs(float(filled_pos.get("active_pos") or filled_pos.get("total_quantity") or actual_qty))
                    conv_price = float(filled_pos.get("settlement_currency_conversion_price") or 102.00)
                order_success = True
            else:
                print(f"[ORDER PLACEMENT FAILED]: {res}")
        else:
            print("[DRY RUN] Order marked filled in simulation mode.")
            order_success = True

        if order_success:
            rdist = round(abs(actual_fill_px - safe_stop), 4)
            active[pair] = {
                "pair": pair,
                "d": d,
                "tf": "4H",
                "entry_type": sig.get("entry_type", "HYBRID"),
                "qty": actual_qty,
                "lev": lev,
                "safe_pct": sig.get("safe_pct", 0.05),
                "entry": actual_fill_px,
                "curr_price": actual_fill_px,
                "stop": safe_stop,
                "rdist": rdist,
                "be": False,
                "sector_unlocked": False,
                "open_time": time.time(),
                "conv_rate": conv_price,
            }
            if pair in state.get("cooldowns", {}):
                del state["cooldowns"][pair]
            state["active_positions"] = active
            save_state(state)
            print(f"[ACCURATE FILL LOCKED] {pair} ({sig.get('entry_type')}) entered at ${actual_fill_px:.4f} with {lev}x leverage (SL: ${safe_stop:.4f})")
            break

    save_state(state)


def get_current_closed_bar_key() -> str:
    now_utc = datetime.now(timezone.utc)
    current_cycle_hour = (now_utc.hour // 4) * 4
    closed_bar_time = now_utc.replace(hour=current_cycle_hour, minute=0, second=0, microsecond=0)
    return closed_bar_time.strftime("%Y-%m-%d_%H:00_UTC")


def main():
    if KILL_TRIGGER.exists():
        print("[ERROR] 'KILL_SWITCH.trigger' exists! Remove it to start the bot.")
        sys.exit(1)

    client = CoinDCXClient()
    streamer = MarketStream(PAIRS_17)
    risk_mgr = RiskManager(risk_pct=0.045)
    strategy = MacroTrendStrategy4H(donchian_n=14, trail_n=7, ema_span=50)

    print("=" * 72)
    print(" CoinDCX 4H Volatility Squeeze + Donchian Hybrid Engine (17 Pairs)")
    print(f" Mode: {'DRY RUN (Paper Trading)' if DRY_RUN else 'LIVE REAL CAPITAL (CoinDCX Futures)'}")
    print(" Sizing: 4.5% Fixed-Risk Compounding (₹650 Safe Floor)")
    print(" Universe: 100% Verified Tradable on CoinDCX Futures")
    print("=" * 72)

    run_4h_cycle(client, streamer, risk_mgr, strategy)
    state = load_state()
    state["last_scanned_bar"] = get_current_closed_bar_key()
    save_state(state)

    _last_wallet_sync = time.time()

    while True:
        try:
            time.sleep(5)
            state = load_state()
            active = state.get("active_positions", {})
            now = time.time()

            # Continuous Wallet Sync every 30 seconds
            if (now - _last_wallet_sync) >= 30.0:
                try:
                    wallet = client.get_wallet_details()
                    if wallet.get("is_live"):
                        risk_mgr.equity = wallet["total_equity"]
                        state["equity"] = wallet["total_equity"]
                        save_state(state)
                except Exception:
                    pass
                _last_wallet_sync = now

            if check_and_apply_midnight_ist_reset(state, risk_mgr):
                run_4h_cycle(client, streamer, risk_mgr, strategy)

            if active:
                monitor_live_positions(client, streamer, risk_mgr, strategy, state)

            latest_bar_key = get_current_closed_bar_key()
            if state.get("last_scanned_bar") != latest_bar_key:
                print(f"\n[NEW 4-HOUR BAR CLOSE DETECTED: {latest_bar_key}] Scanning 17 pairs...")
                run_4h_cycle(client, streamer, risk_mgr, strategy)
                state = load_state()
                state["last_scanned_bar"] = latest_bar_key
                save_state(state)

        except Exception as e:
            print(f"[Loop Exception Caught]: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()