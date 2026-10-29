"""
market_stream.py - High-Resilience Public Candle Streamer
Features:
  - Campus / Firewall SSL Bypass: Disables certificate inspection blocks on public candle feeds
  - Multi-Endpoint Mirror Fallback: Rotates across fapi, api, api1, and api3
  - Non-Blocking Exceptions: Catches network errors cleanly without halting main.py
"""
from __future__ import annotations

import time
import warnings
import urllib3
import pandas as pd
import requests

# Suppress SSL certificate verification warnings for public market feeds
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


class MarketStream:
    def __init__(self, pairs: list[str]):
        self.pairs = pairs

    def fetch_pair_candles(self, pair: str, interval: str = "4h", limit: int = 100) -> pd.DataFrame:
        clean_sym = pair.replace("B-", "").replace("_USDT", "USDT")
        
        endpoints = [
            f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_sym}&interval={interval}&limit={limit}",
            f"https://api.binance.com/api/v3/klines?symbol={clean_sym}&interval={interval}&limit={limit}",
            f"https://api1.binance.com/api/v3/klines?symbol={clean_sym}&interval={interval}&limit={limit}",
            f"https://api3.binance.com/api/v3/klines?symbol={clean_sym}&interval={interval}&limit={limit}",
        ]

        for url in endpoints:
            try:
                # verify=False prevents campus/institutional firewall self-signed cert blocks
                r = requests.get(url, headers=HEADERS, timeout=4.0, verify=False)
                if r.status_code == 200:
                    raw = r.json()
                    if isinstance(raw, list) and len(raw) > 10:
                        df = pd.DataFrame(raw, columns=[
                            "open_time", "open", "high", "low", "close", "volume",
                            "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"
                        ])
                        for col in ["open", "high", "low", "close", "volume"]:
                            df[col] = df[col].astype(float)
                        df["datetime"] = pd.to_datetime(df["open_time"], unit="ms")
                        df = df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime").reset_index(drop=True)
                        return df
            except Exception:
                continue

        # If all public endpoints fail, return an empty DataFrame gracefully
        return pd.DataFrame()