"""Loads config.toml and .env into one settings object."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    raw: dict
    nansen_api_key: str = ""
    wallet_address: str = ""
    live_confirm: bool = False
    telegram_token: str = ""
    telegram_chat: str = ""
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    def __getitem__(self, section: str) -> dict:
        return self.raw[section]

    @property
    def is_live(self) -> bool:
        return self.raw["mode"]["trading_mode"] == "live" and self.live_confirm


def load_settings(config_path: Path | None = None) -> Settings:
    _load_dotenv(ROOT / ".env")
    path = config_path or ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    s = Settings(
        raw=raw,
        nansen_api_key=os.environ.get("NANSEN_API_KEY", ""),
        wallet_address=os.environ.get("GMGN_WALLET_ADDRESS", ""),
        live_confirm=os.environ.get("LIVE_TRADING_CONFIRM", "").lower() == "yes",
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    s.data_dir.mkdir(exist_ok=True)
    return s
