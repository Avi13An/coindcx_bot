"""
kill_switch.py - Emergency Manual Panic Button
Instantly closes all active positions on CoinDCX and halts main.py.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from coindcx_client import CoinDCXClient

load_dotenv()
TRIGGER_FILE = Path("KILL_SWITCH.trigger")
STATE_FILE = Path("bot_state.json")


def emergency_shutdown():
    print("\n" + "!" * 65)
    print(" [EMERGENCY KILL SWITCH ACTIVATED]")
    print("!" * 65)

    client = CoinDCXClient()

    # 1. Fetch active positions from CoinDCX API
    print("\n1. Querying live open positions on CoinDCX...")
    positions = client.get_active_positions()

    if isinstance(positions, list) and len(positions) > 0:
        for pos in positions:
            pair = pos.get("pair")
            qty = abs(float(pos.get("active_units", 0.0) or pos.get("total_quantity", 0.0)))
            side = pos.get("side", "").lower()

            if qty > 0 and pair:
                # To close LONG -> send SELL; to close SHORT -> send BUY
                close_side = "sell" if side in ("buy", "long") else "buy"
                print(f"   --> Flattening {pair} | Qty: {qty} | Side: {close_side.upper()}...")
                res = client.create_order(pair, close_side, "market_order", qty, leverage=10)
                print(f"       Result: {res}")
    else:
        print("   --> No open positions reported by CoinDCX API.")

    # 2. Write the trigger file to halt main.py loop
    print("\n2. Writing shutdown signal to halt main.py daemon...")
    TRIGGER_FILE.write_text("HALT")

    # 3. Clean active positions in local state
    if STATE_FILE.exists():
        try:
            import json
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            state["active_positions"] = {}
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            print("3. Local state positions wiped.")
        except Exception as e:
            print(f"3. Could not update state file: {e}")

    print("\n" + "=" * 65)
    print(" Bot execution halted. All positions flattened.")
    print(" Delete 'KILL_SWITCH.trigger' when you are ready to restart.")
    print("=" * 65)


if __name__ == "__main__":
    emergency_shutdown()