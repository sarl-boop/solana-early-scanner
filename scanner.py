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
        and 150000 <= mc <= 3000000
        and vol24 >= 400000
        and tx24 >= 400
        and buys_h1 >= sells_h1
        and age_ok_gold
        and boosts_active >= 1
    ):
        return "🟨 GOLD", "BUY priority"

    if (
        liq >= 35000
        and 50000 <= mc <= 5000000
        and vol24 >= 100000
        and tx24 >= 120
        and buys_h1 >= sells_h1
        and age_ok_green
    ):
        return "🟢 GREEN", "BUY small"

    return None, None


def red_exit(m: dict):
    liq = m["liquidity_usd"]
    vol24 = m["volume_h24"]
    buys_h1 = m["buys_h1"]
    sells_h1 = m["sells_h1"]
    price_h1 = m["price_h1"]

    if liq < 15000:
        return True
    if vol24 < 20000:
        return True
    if price_h1 <= -20:
        return True
    if sells_h1 > buys_h1 * 2 and sells_h1 >= 20:
        return True

    return False


def get_candidates():
    items = []

    profiles = get_json(DEX_PROFILES)
    boosts = get_json(DEX_BOOSTS)

    for item in profiles[:20]:
        if (item.get("chainId") or "").lower() != "solana":
            continue

        name = clean_name(item.get("tokenName") or "")
        website, x_link, discord_link = extract_links_from_profile(item)

        items.append({
            "name": name,
            "token_address": item.get("tokenAddress") or "",
            "website": website,
            "x_link": x_link,
            "discord_link": discord_link,
        })

    for item in boosts[:20]:
        if (item.get("chainId") or "").lower() != "solana":
            continue

        name = clean_name(item.get("tokenName") or "")
        website, x_link, discord_link = extract_links_from_profile(item)

        items.append({
            "name": name,
            "token_address": item.get("tokenAddress") or "",
            "website": website,
            "x_link": x_link,
            "discord_link": discord_link,
        })

    seen = set()
    out = []

    for item in items:
        addr = item["token_address"]
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(item)

    return out


def should_send_same_signal(tracked_item: dict, new_signal: str) -> bool:
    last_signal = tracked_item.get("last_signal")
    last_alert_ts = safe_int(tracked_item.get("last_alert_ts"))
    now = int(time.time())

    if last_signal != new_signal:
        return True
    if now - last_alert_ts >= SAME_SIGNAL_COOLDOWN_SEC:
        return True
    return False


def main():
    state = load_state()
    tracked = state.setdefault("tracked", {})
    alerts = []

    for item in get_candidates():
        token_address = item["token_address"]

        try:
            pair = best_pair_for_token(token_address)
        except Exception:
            pair = None

        if not pair:
            continue

        pair_name = clean_name(((pair.get("baseToken") or {}).get("name")) or "")
        name = item["name"] or pair_name

        pair_website, pair_x, pair_discord = pair_links(pair)
        website = item["website"] or pair_website
        x_link = item["x_link"] or pair_x

        if looks_bad(name, website, x_link):
            continue

        m = metrics_from_pair(pair)
        color, action = classify_signal(m)
        if not color:
            continue

        existing = tracked.get(token_address, {})
        if not should_send_same_signal(existing, color):
            continue

        tracked[token_address] = {
            "name": name,
            "last_signal": color,
            "last_alert_ts": int(time.time()),
            "last_seen": int(time.time()),
        }

        priority = 2 if color == "🟨 GOLD" else 1
        alerts.append(
            (
                priority,
                f"{color}\n"
                f"Token: {name}\n"
                f"MC: ${int(m['market_cap']):,}\n"
                f"Liq: ${int(m['liquidity_usd']):,}\n"
                f"Vol24h: ${int(m['volume_h24']):,}\n"
                f"Action: {action}"
            )
        )

    for token_address, info in list(tracked.items()):
        try:
            pair = best_pair_for_token(token_address)
        except Exception:
            pair = None

        if not pair:
            continue

        m = metrics_from_pair(pair)
        if red_exit(m):
            alerts.append(
                (
                    3,
                    f"🔴 RED\n"
                    f"Token: {info.get('name', 'Unknown')}\n"
                    f"MC: ${int(m['market_cap']):,}\n"
                    f"Liq: ${int(m['liquidity_usd']):,}\n"
                    f"Vol24h: ${int(m['volume_h24']):,}\n"
                    f"Action: SELL / EXIT"
                )
            )
            tracked.pop(token_address, None)

    save_state(state)
    alerts.sort(key=lambda x: x[0], reverse=True)

    for _, msg in alerts[:MAX_ALERTS_PER_RUN]:
        send(msg)


if __name__ == "__main__":
    main()

