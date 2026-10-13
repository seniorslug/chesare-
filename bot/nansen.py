"""Nansen API client: smart money netflows and per-token flow intelligence."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)
BASE = "https://api.nansen.ai/api/v1"


class NansenError(RuntimeError):
    pass


class Nansen:
    def __init__(self, api_key: str, timeout: int = 30):
        if not api_key:
            raise NansenError("NANSEN_API_KEY is missing from .env")
        self.s = requests.Session()
        self.s.headers.update({"apiKey": api_key, "Content-Type": "application/json"})
        self.timeout = timeout
        self.credits_used = 0.0

    def _post(self, path: str, body: dict, retries: int = 3) -> dict:
        for attempt in range(retries):
            r = self.s.post(BASE + path, json=body, timeout=self.timeout)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2 ** attempt * 5))
                log.warning("Nansen rate limit, waiting %.0fs", wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise NansenError(f"{path} -> {r.status_code}: {r.text[:300]}")
            try:
                self.credits_used += float(r.headers.get("X-Nansen-Credits-Used", 0) or 0)
            except ValueError:
                pass
            return r.json()
        raise NansenError(f"{path}: rate limited after {retries} tries")

    def smart_money_netflows(self, chain: str, max_age_days: int, min_mcap: float,
                             max_mcap: float, labels: list[str], min_traders: int,
                             per_page: int = 100) -> list[dict]:
        """Young tokens ranked by 24h smart money net inflow."""
        body = {
            "chains": [chain],
            "filters": {
                "include_smart_money_labels": labels,
                "include_stablecoins": False,
                "include_native_tokens": False,
                "token_age_days": {"min": 0, "max": max_age_days},
                "market_cap_usd": {"min": min_mcap, "max": max_mcap},
                "trader_count": {"min": min_traders},
            },
            "pagination": {"page": 1, "per_page": per_page},
            "order_by": [{"field": "net_flow_24h_usd", "direction": "DESC"}],
        }
        return self._post("/smart-money/netflow", body).get("data", [])

    def flow_intelligence(self, chain: str, token: str, timeframe: str) -> dict:
        """Smart trader net flow + wallet count for one token. Returns {} if none."""
        body = {"chain": chain, "token_address": token, "timeframe": timeframe}
        data = self._post("/tgm/flow-intelligence", body).get("data", [])
        return data[0] if data else {}
