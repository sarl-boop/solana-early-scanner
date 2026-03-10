import os
import re
import json
import time
from pathlib import Path

import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("state.json")

DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_TOKEN_PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/{token}"

BAD_WORDS = {
    "test", "official", "pump", "100x", "1000x", "presale",
    "casino", "bet", "airdrop", "giveaway", "free",
    "elon", "moon", "lfg"
}

MAX_ALERTS_PER_RUN = 2
SAME_SIGNAL_COOLDOWN_SEC = 6 * 3600


def send(msg: str) -> None:
    requests.post(WEBHOOK, json={"content": msg}, timeout=20)


def get_json(url: str):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def load_state():
    if not STATE_FILE.exists():
        return {"tracked": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"tracked": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def safe_float(x) -> float:
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def safe_int(x) -> int:
    try:
        return int(float(x or 0))
    except Exception:
        return 0


def clean_name(raw_name: str) -> str:
    name = (raw_name or "").strip()
    if not name:
        return ""
    if name.startswith("http://") or name.startswith("https://"):
        return ""
    if "cdn.dexscreener.com" in name.lower():
        return ""
    return name


def looks_bad(name: str, website: str, x_link: str) -> bool:
    low = name.lower()

    if not name:
        return True
    if len(name) < 3 or len(name) > 28:
        return True
    if re.search(r"https?://", name):
        return True
    if sum(ch.isdigit() for ch in name) > 4:
        return True
    if any(word in low for word in BAD_WORDS):
        return True
    if not website:
        return True
    if not x_link:
        return True

    return False


def extract_links_from_profile(item: dict):
    links = item.get("links") or []
    website = ""
    x_link = ""
    discord_link = ""

    for link in links:
        label = (link.get("label") or "").lower()
        url = (link.get("url") or "").strip()

        if not url:
            continue

        if "website" in label and not website:
            website = url
        elif ("twitter" in label or label == "x") and not x_link:
            x_link = url
        elif "discord" in label and not discord_link:
            discord_link = url

    return website, x_link, discord_link


def pair_links(pair: dict):
    info = pair.get("info") or {}
    websites = info.get("websites") or []
    socials = info.get("socials") or []

    website = ""
    x_link = ""
    discord_link = ""

    if websites:
        website = (websites[0].get("url") or "").strip()

    for s in socials:
        platform = (s.get("platform") or "").lower()
        handle = (s.get("handle") or "").strip()
        if not handle:
            continue

        if platform in ("twitter", "x") and not x_link:
            x_link = handle if handle.startswith("http") else f"https://x.com/{handle.lstrip('@')}"
        elif platform == "discord" and not discord_link:
            discord_link = handle if handle.startswith("http") else handle

    return website, x_link, discord_link


def txns_count(block: dict) -> int:
    if not isinstance(block, dict):
        return 0
    return safe_int(block.get("buys")) + safe_int(block.get("sells"))


def best_pair_for_token(token_address: str):
    url = DEX_TOKEN_PAIRS.format(token=token_address)
    pairs = get_json(url)

    if not isinstance(pairs, list) or not pairs:
        return None

    pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
    if not pairs:
        return None

    return sorted(
        pairs,
        key=lambda p: safe_float((p.get("liquidity") or {}).get("usd")),
        reverse=True
    )[0]


def metrics_from_pair(pair: dict):
    txns = pair.get("txns") or {}
    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}
    liquidity = pair.get("liquidity") or {}
    boosts = pair.get("boosts") or {}

    created_at = pair.get("pairCreatedAt")
    age_hours = None
    if created_at:
        age_hours = max(0, (time.time() * 1000 - created_at) / 3600000)

    return {
        "liquidity_usd": safe_float(liquidity.get("usd")),
        "market_cap": safe_float(pair.get("marketCap") or pair.get("fdv")),
        "volume_h24": safe_float(volume.get("h24")),
        "txns_h24": txns_count(txns.get("h24") or {}),
        "buys_h1": safe_int((txns.get("h1") or {}).get("buys")),
        "sells_h1": safe_int((txns.get("h1") or {}).get("sells")),
        "price_h1": safe_float(price_change.get("h1")),
        "boosts_active": safe_int(boosts.get("active")),
        "age_hours": age_hours,
    }


def classify_signal(m: dict):
    liq = m["liquidity_usd"]
    mc = m["market_cap"]
    vol24 = m["volume_h24"]
    tx24 = m["txns_h24"]
    buys_h1 = m["buys_h1"]
    sells_h1 = m["sells_h1"]
    age = m["age_hours"]
    boosts_active = m["boosts_active"]

    age_ok_gold = age is None or age <= 48
    age_ok_green = age is None or age <= 96

    if (
        liq >= 100000
        and 150000 <= mc <= 300
