import asyncio
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests
import websockets

# =========================================================
# CONFIG
# =========================================================

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "").strip()
SMART_WALLETS = {
    x.strip() for x in os.environ.get("SMART_WALLETS", "").split(",") if x.strip()
}

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/"

STATE_FILE = Path("state.json")

HEARTBEAT_INTERVAL_SECONDS = 3600
SAVE_INTERVAL_SECONDS = 30
EVALUATE_INTERVAL_SECONDS = 12
TOKEN_TTL_SECONDS = 48 * 3600
ALERT_COOLDOWN_SECONDS = 12 * 3600

MAX_MARKET_CAP = 5_000_000
MIN_LIQUIDITY = 15_000
MIN_LIQ_TO_MC_RATIO = 0.40
WASH_RATIO_LIMIT = 35.0
NO_CHASE_MULTIPLIER = 2.2

GOLD_SCORE = 8
GREEN_SCORE = 6

# holder thresholds (strict)
TOP1_HARD_REJECT = 0.15
TOP3_HARD_REJECT = 0.35
TOP1_SOFT_PENALTY = 0.08
TOP3_SOFT_PENALTY = 0.22

# =========================================================
# STATE
# =========================================================

STATE: Dict[str, Any] = {
    "tokens": {},
    "alerted": {},
    "last_heartbeat": 0,
}

# token shape:
# {
#   "mint": str,
#   "name": str,
#   "symbol": str,
#   "source": "new_token"|"migration"|"unknown",
#   "first_seen_ts": int,
#   "last_seen_ts":
