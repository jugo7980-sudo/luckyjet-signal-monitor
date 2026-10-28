from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    game_url: str
    telegram_bot_token: str
    telegram_chat_id: str
 base_amount: int = 150
 trigger_streak: int = 4
 threshold: float = 2.0
 poll_seconds: float = 1.0
 headless: bool = True
 state_file: str = "data/state.json"
 storage_state_path: str | None = None
 currency: str = "KRW"
 history_top_ratio: float = 0.35
 minimum_history_items: int = 3
 error_notify_cooldown: float = 300.0

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
 game_url = os.getenv("GAME_URL", "").strip()
 token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
 chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
 missing = [
            name
            for ame, value in {
                "GAME_URL": game_url,
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_ID": chat_id,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

 storage_state = os.getenv("STORAGE_STATE_PATH", "").strip() or None
        return cls(
            game_url=game_url,
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
 base_amount=int(os.getenv("BASE_AMOUNT", "150")),
 trigger_streak=int(os.getenv("TRIGGER_STREAK", "4")),
 threshold=float(os.getenv("THRESHOLD", "2.0")),
 poll_seconds=float(os.getenv("POLL_SECONDS", "1.0")),
 headless=os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "on"},
 state_file=os.getenv("STATE_FILE", "data/state.json"),
            storage_state_path=storage_state,
 currency=os.getenv("CURRENCY", "KRW").strip() or "KRW",
 history_top_ratio=float(os.getenv("HISTORY_TOP_RATIO", "0.35")),
 minimum_history_items=int(os.getenv("MIN_HISTORY_ITEMS", "3")),
 error_notify_cooldown=float(os.getenv("ERROR_NOTIFY_COOLDOWN", "300")),
        )
