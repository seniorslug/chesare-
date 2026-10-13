"""Live execution through GMGN's official CLI (gmgn-cli), which handles request signing.

Install:   npm install -g gmgn-cli
Configure: gmgn-cli config   (API key with swap enabled + signing key; IP whitelist required)

Only used when trading_mode = "live" AND LIVE_TRADING_CONFIRM=yes.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


class GmgnError(RuntimeError):
    pass


class Gmgn:
    def __init__(self, wallet: str, chain: str = "sol", slippage: float = 15, anti_mev: bool = True):
        if not shutil.which("gmgn-cli"):
            raise GmgnError("gmgn-cli not found. Run: npm install -g gmgn-cli")
        if not wallet:
            raise GmgnError("GMGN_WALLET_ADDRESS is missing from .env")
        self.wallet, self.chain, self.slippage, self.anti_mev = wallet, chain, slippage, anti_mev

    def _run(self, args: list[str], timeout: int = 90) -> dict:
        cmd = ["gmgn-cli", *args, "--raw"]
        log.info("GMGN: %s", " ".join(cmd))
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise GmgnError(f"gmgn-cli failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            raise GmgnError(f"Unreadable gmgn-cli output: {p.stdout[:300]}")
        return out.get("data", out)

    def _base(self) -> list[str]:
        args = ["--chain", self.chain, "--from", self.wallet, "--slippage", str(self.slippage)]
        if self.anti_mev:
            args.append("--anti-mev")
        return args

    @staticmethod
    def _check(res: dict) -> dict:
        state = (res.get("confirmation") or {}).get("state")
        if res.get("error_code") or state in ("failed", "expired"):
            raise GmgnError(f"Swap not filled: {res}")
        return res

    def buy(self, token: str, sol_amount: float, hard_stop_pct: float | None = None) -> dict:
        """Buy `token` with SOL. Optionally attach a GMGN-side stop loss as a safety net
        that still fires if this bot is offline."""
        args = ["swap", *self._base(), "--input-token", SOL_MINT, "--output-token", token,
                "--amount", str(int(sol_amount * LAMPORTS))]
        if hard_stop_pct:
            orders = [{"order_type": "loss_stop", "side": "sell",
                       "price_scale": str(hard_stop_pct), "sell_ratio": "100"}]
            args += ["--condition-orders", json.dumps(orders), "--sell-ratio-type", "hold_amount"]
        return self._check(self._run(args))

    def sell_all(self, token: str) -> dict:
        args = ["swap", *self._base(), "--input-token", token, "--output-token", SOL_MINT,
                "--percent", "100"]
        return self._check(self._run(args))

    def cancel_strategy(self, order_id: str) -> None:
        try:
            self._run(["order", "strategy", "cancel", "--chain", self.chain, "--order-id", order_id])
        except GmgnError as e:
            log.warning("Could not cancel GMGN stop order %s: %s", order_id, e)
