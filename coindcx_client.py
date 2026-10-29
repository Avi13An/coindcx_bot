import os
import time
import json
import hmac
import hashlib
import logging
from typing import Dict, Any, Optional, List
import requests
from dotenv import load_dotenv

# Load environment configuration
load_dotenv()

logger = logging.getLogger("CoinDCXClient")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

class CoinDCXClient:
    """
    Production client for CoinDCX Futures & Account REST APIs with
    authenticated static proxy routing, HMAC-SHA256 signing, and resilient fallbacks.
    """
    BASE_URL = "https://api.coindcx.com"

    def __init__(self):
        self.api_key = os.getenv("COINDCX_API_KEY", "").strip()
        self.api_secret = os.getenv("COINDCX_API_SECRET", "").strip()
        self.dry_run = os.getenv("DRY_RUN", "False").lower() in ("true", "1")
        self.manual_equity = float(os.getenv("MANUAL_EQUITY_INR", "82.41"))

        self.session = requests.Session()

        # Route requests through static proxy if configured in .env
        proxy_url = os.getenv("STATIC_PROXY_URL", "").strip()
        if proxy_url:
            self.session.proxies = {
                "http": proxy_url,
                "https": proxy_url
            }
            logger.info("Outbound traffic configured through static proxy.")

    # -------------------------------------------------------------------------
    # Authentication & Request Helpers
    # -------------------------------------------------------------------------
    def _generate_signature(self, body: Dict[str, Any]) -> tuple[str, str]:
        """Serializes payload into compact JSON and generates HMAC-SHA256 signature."""
        json_body = json.dumps(body, separators=(',', ':'))
        secret_bytes = bytes(self.api_secret, encoding="utf-8")
        signature = hmac.new(
            secret_bytes,
            json_body.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return json_body, signature

    def _get_auth_headers(self, signature: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature
        }

    def _post(self, endpoint: str, body: Dict[str, Any], timeout: int = 10) -> Any:
        """Executes signed POST request against CoinDCX private API endpoints."""
        url = f"{self.BASE_URL}{endpoint}"
        json_body, signature = self._generate_signature(body)
        headers = self._get_auth_headers(signature)

        response = self.session.post(url, data=json_body, headers=headers, timeout=timeout)
        
        if response.status_code == 401:
            logger.error(f"HTTP 401 Unauthorized on {endpoint}: Check IP binding & API credentials.")
        elif response.status_code != 200:
            logger.error(f"HTTP {response.status_code} Error on {endpoint}: {response.text}")

        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------------------
    # Wallet & Balance Endpoints
    # -------------------------------------------------------------------------
    def get_wallet_details(self) -> Dict[str, Any]:
        """
        Retrieves real-time account balances and equity.
        Returns is_live: True if verified on exchange, or falls back safely.
        """
        if self.dry_run:
            return {
                "is_live": False,
                "total_equity": self.manual_equity,
                "free_inr": self.manual_equity,
                "status": "dry_run"
            }

        try:
            body = {"timestamp": int(round(time.time() * 1000))}
            res = self._post("/exchange/v1/users/balances", body)

            total_inr = 0.0
            free_inr = 0.0

            if isinstance(res, list):
                for item in res:
                    currency = item.get("currency", "")
                    balance = float(item.get("balance", 0.0) or 0.0)
                    locked = float(item.get("locked_balance", 0.0) or 0.0)

                    if currency == "INR":
                        free_inr += balance
                        total_inr += (balance + locked)
                    elif currency == "USDT":
                        # Collateral estimation for USDT futures holdings
                        usdt_inr_peg = 88.0
                        total_inr += (balance + locked) * usdt_inr_peg

            # Safeguard: If account balance is not returned, maintain fallback
            if total_inr == 0.0:
                total_inr = self.manual_equity
                free_inr = self.manual_equity

            return {
                "is_live": True,
                "total_equity": round(total_inr, 2),
                "free_inr": round(free_inr, 2)
            }
        except Exception as e:
            logger.warning(f"Wallet balance query failed ({e}). Reverting to fallback values.")
            return {
                "is_live": False,
                "total_equity": self.manual_equity,
                "free_inr": self.manual_equity,
                "error": str(e)
            }

    # -------------------------------------------------------------------------
    # Derivatives Futures Positions & Orders
    # -------------------------------------------------------------------------
    def get_positions(self) -> List[Dict[str, Any]]:
        """Fetches active futures positions from the exchange."""
        if self.dry_run:
            return []

        try:
            body = {
                "timestamp": int(round(time.time() * 1000)),
                "page": 1,
                "size": 50
            }
            res = self._post("/exchange/v1/derivatives/futures/positions", body)
            if isinstance(res, list):
                return res
            elif isinstance(res, dict) and "data" in res:
                return res["data"]
            return []
        except Exception as e:
            logger.error(f"Error fetching active positions: {e}")
            return []

    def create_order(
        self,
        pair: str,
        side: str,
        quantity: float,
        order_type: str = "market_order",
        price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Submits a futures order on CoinDCX.
        side: 'buy' or 'sell'
        order_type: 'market_order' or 'limit_order'
        """
        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulated {side.upper()} order for {quantity} on {pair}")
            return {"status": "simulated", "pair": pair, "side": side, "quantity": quantity}

        body: Dict[str, Any] = {
            "timestamp": int(round(time.time() * 1000)),
            "order": {
                "side": side.lower(),
                "pair": pair,
                "order_type": order_type,
                "total_quantity": quantity,
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False
            }
        }
        if price is not None and order_type == "limit_order":
            body["order"]["price_per_unit"] = price

        return self._post("/exchange/v1/derivatives/futures/orders/create", body)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancels an existing futures order by order ID."""
        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulated cancel for order ID: {order_id}")
            return {"status": "simulated", "id": order_id}

        body = {
            "timestamp": int(round(time.time() * 1000)),
            "id": order_id
        }
        return self._post("/exchange/v1/derivatives/futures/orders/cancel", body)