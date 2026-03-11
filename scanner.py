import os
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

# =========================
# CONFIG
# =========================

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
OWNED_TOKENS = {
    x.strip() for x in os.environ.get("OWNED_TOKENS", "").split(",") if x.strip()
}

STATE_FILE = Path("state.json")

GT_PAGES = 10                   # 5 pages ~ 100 pools, plus stable pour GitHub
MAX_ALERTS_PER_RUN = 3
TIMEOUT = 15
ALERT_COOLDOWN_HOURS = 12
STATUS_INTERVAL_SECONDS = 3600

GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"

# =========================
# UTILS
# =========================

def send(msg: str) -> None:
    try:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print("Discord send error:", e)


def get_json(url: str, params=None, headers=None):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("GET error:", url, e)
        return None


def to_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def load_state():
    default_state = {
        "alerted": {},
        "last_status_ts": 0
    }

    if not STATE_FILE.exists():
        return default_state

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_state
        if "alerted" not in data or not isinstance(data["alerted"], dict):
            data["alerted"] = {}
        if "last_status_ts" not in data:
            data["last_status_ts"] = 0
        return data
    except Exception as e:
        print("State load error:", e)
        return default_state


def save_state(state):
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("State save error:", e)


def cleanup_old_alerts(state):
    now = time.time()
    keep = {}
    for addr, ts in state["alerted"].items():
        if now - ts < 7 * 24 * 3600:
            keep[addr] = ts
    state["alerted"] = keep


def recently_alerted(state, token_addr: str):
    ts = state["alerted"].get(token_addr, 0)
    return (time.time() - ts) < ALERT_COOLDOWN_HOURS * 3600


def mark_alerted(state, token_addr: str):
    state["alerted"][token_addr] = int(time.time())


def parse_age_minutes(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)
    except Exception:
        pass

    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return max(0.0, (time.time() - ts) / 60.0)
    except Exception:
        return None


def tx_count(bucket):
    if not isinstance(bucket, dict):
        return 0
    try:
        return int(bucket.get("buys", 0)) + int(bucket.get("sells", 0))
    except Exception:
        return 0


def buy_ratio(bucket):
    if not isinstance(bucket, dict):
        return 0.5
    try:
        buys = float(bucket.get("buys", 0))
        sells = float(bucket.get("sells", 0))
        total = buys + sells
        if total <= 0:
            return 0.5
        return buys / total
    except Exception:
        return 0.5

# =========================
# DEXSCREENER
# =========================

def fetch_dex_profiles():
    out = {}
    data = get_json(DEX_PROFILES)
    if not isinstance(data, list):
        return out

    for item in data:
        if item.get("chainId") != "solana":
            continue

        addr = item.get("tokenAddress")
        if not addr:
            continue

        links = item.get("links", []) or []

        has_website = False
        has_x = False
        has_discord = False
        has_telegram = False

        for link in links:
            label = str(link.get("label", "")).lower()
            url = str(link.get("url", "")).lower()
            combo = f"{label} {url}"

            if "website" in combo or "site" in combo:
                has_website = True
            if "twitter" in combo or "x.com" in combo:
                has_x = True
            if "discord" in combo:
                has_discord = True
            if "telegram" in combo or "t.me" in combo:
                has_telegram = True

        out[addr] = {
            "has_website": has_website,
            "has_x": has_x,
            "has_discord": has_discord,
            "has_telegram": has_telegram,
        }

    return out


def fetch_dex_boosts():
    boosted = set()

    for url in (DEX_BOOSTS_LATEST, DEX_BOOSTS_TOP):
        data = get_json(url)
        if not isinstance(data, list):
            continue

        for item in data:
            if item.get("chainId") == "solana":
                addr = item.get("tokenAddress")
                if addr:
                    boosted.add(addr)

    return boosted

# =========================
# GECKOTERMINAL
# =========================

def fetch_gecko_new_pools():
    pools = []

    for page in range(1, GT_PAGES + 1):
        data = get_json(
            GECKO_NEW_POOLS,
            params={"page": page, "include": "base_token,dex"},
            headers={"accept": "application/json"},
        )

        if not data or "data" not in data:
            continue

        included = data.get("included", []) or []
        inc_map = {}
        for obj in included:
            inc_map[(obj.get("type"), obj.get("id"))] = obj

        for pool in data.get("data", []):
            attrs = pool.get("attributes", {}) or {}
            rels = pool.get("relationships", {}) or {}

            base_ref = (((rels.get("base_token") or {}).get("data")) or {})
            dex_ref = (((rels.get("dex") or {}).get("data")) or {})

            base_obj = inc_map.get((base_ref.get("type"), base_ref.get("id")), {})
            dex_obj = inc_map.get((dex_ref.get("type"), dex_ref.get("id")), {})

            base_attrs = base_obj.get("attributes", {}) or {}
            dex_attrs = dex_obj.get("attributes", {}) or {}

            token_addr = (
                base_attrs.get("address")
                or attrs.get("base_token_address")
                or ""
            )

            name = (
                base_attrs.get("name")
                or attrs.get("name", "").split("/")[0].strip()
                or "Unknown Token"
            )

            symbol = (
                base_attrs.get("symbol")
                or name
            )

            dex_id = (
                dex_attrs.get("identifier")
                or dex_attrs.get("name")
                or ""
            ).lower()

            market_cap = to_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd"))
            liquidity = to_float(attrs.get("reserve_in_usd") or attrs.get("liquidity_usd"))

            volume_map = attrs.get("volume_usd", {}) or {}
            tx_map = attrs.get("transactions", {}) or {}

            volume_5m = to_float(volume_map.get("m5"))
            volume_1h = to_float(volume_map.get("h1"))
            volume_24h = to_float(volume_map.get("h24"))

            tx_5m = tx_count(tx_map.get("m5"))
            tx_1h = tx_count(tx_map.get("h1"))

            buy_ratio_5m = buy_ratio(tx_map.get("m5"))
            buy_ratio_1h = buy_ratio(tx_map.get("h1"))

            age_min = parse_age_minutes(
                attrs.get("pool_created_at")
                or attrs.get("created_at")
            )

            pools.append({
                "name": name,
                "symbol": symbol,
                "token_address": token_addr,
                "dex_id": dex_id,
                "market_cap": market_cap,
                "liquidity": liquidity,
                "volume_5m": volume_5m,
                "volume_1h": volume_1h,
                "volume_24h": volume_24h,
                "tx_5m": tx_5m,
                "tx_1h": tx_1h,
                "buy_ratio_5m": buy_ratio_5m,
                "buy_ratio_1h": buy_ratio_1h,
                "age_min": age_min,
            })

        time.sleep(0.25)

    return pools

# =========================
# FILTERS
# =========================

def enough_socials(profile):
    if not profile:
        return False
    score = 0
    if profile.get("has_website"):
        score += 1
    if profile.get("has_x"):
        score += 1
    if profile.get("has_discord") or profile.get("has_telegram"):
        score += 1
    return score >= 2


def rug_reject(candidate, profile):
    mc = candidate["market_cap"]
    liq = candidate["liquidity"]
    v24 = candidate["volume_24h"]
    v5 = candidate["volume_5m"]
    tx5 = candidate["tx_5m"]
    br5 = candidate["buy_ratio_5m"]
    age = candidate["age_min"]

    if not candidate["token_address"]:
        return True, "pas d'adresse"

    if mc <= 0 or mc > 8_000_000:
        return True, "hors filtre micro-cap"

    if liq < 20_000:
        return True, "liquidité trop faible"

    if liq / max(mc, 1) < 0.05:
        return True, "liquidité trop faible vs market cap"

    if age is not None and age < 2:
        return True, "trop neuf"

    if age is not None and age > 720:
        return True, "plus vraiment early"

    if v24 < 20_000:
        return True, "volume 24h trop faible"

    if tx5 < 5 and v5 < 2_500:
        return True, "activité trop faible"

    if br5 < 0.45:
        return True, "pression vendeuse"

    if not enough_socials(profile):
        return True, "socials insuffisants"

    return False, ""

# =========================
# SCORE
# =========================

def compute_score(candidate, profile, boosted):
    mc = candidate["market_cap"]
    liq = candidate["liquidity"]
    v5 = candidate["volume_5m"]
    v1 = candidate["volume_1h"]
    v24 = candidate["volume_24h"]
    tx5 = candidate["tx_5m"]
    tx1 = candidate["tx_1h"]
    br5 = candidate["buy_ratio_5m"]
    age = candidate["age_min"]

    score = 0
    reasons = []

    # micro-cap priority
    if mc < 500_000:
        score += 3
        reasons.append("micro-cap très basse")
    elif mc < 2_000_000:
        score += 2
        reasons.append("micro-cap correcte")
    elif mc < 5_000_000:
        score += 1

    # liquidity
    liq_ratio = liq / max(mc, 1)
    if liq > 100_000:
        score += 2
        reasons.append("bonne liquidité")
    elif liq > 50_000:
        score += 1
        reasons.append("liquidité correcte")

    if liq_ratio > 0.20:
        score += 2
        reasons.append("liquidité forte vs market cap")
    elif liq_ratio > 0.10:
        score += 1

    # volume burst
    burst = False
    if v1 > 0 and (v5 * 10) > (0.25 * v1) and tx5 >= 6:
        score += 2
        reasons.append("volume 5m accélère")
        burst = True
    elif v5 > 10_000 and tx5 >= 8:
        score += 1
        reasons.append("activité 5m monte")

    if v24 > 300_000:
        score += 2
        reasons.append("volume 24h solide")
    elif v24 > 100_000:
        score += 1

    # buy pressure
    if br5 > 0.60:
        score += 2
        reasons.append("plus d'acheteurs que de vendeurs")
    elif br5 > 0.53:
        score += 1

    # transactions burst
    if tx5 >= 20:
        score += 2
    elif tx5 >= 10:
        score += 1

    if tx1 > 0 and tx5 > 0 and tx5 * 12 > 0.25 * tx1:
        score += 1
        if not burst:
            reasons.append("transactions accélèrent")

    # socials
    social_score = 0
    if profile:
        if profile.get("has_website"):
            social_score += 1
        if profile.get("has_x"):
            social_score += 1
        if profile.get("has_discord") or profile.get("has_telegram"):
            social_score += 1

    if social_score >= 3:
        score += 2
        reasons.append("site + X + communauté")
    elif social_score == 2:
        score += 1
        reasons.append("présence sociale correcte")

    # Dex boosts
    if candidate["token_address"] in boosted:
        score += 1
        reasons.append("boost DexScreener")

    # early launchpad / pump-like
    if "pump" in candidate["dex_id"] or "launch" in candidate["dex_id"]:
        score += 1
        reasons.append("très early launchpad")

    # age sweet spot
    if age is not None and 5 <= age <= 120:
        score += 1

    return min(score, 10), reasons


def classify_buy(score):
    if score >= 9:
        return "🟡 GOLD", "Buy 50€ maintenant"
    if score >= 7:
        return "🟢 GREEN", "Buy 25€ maintenant"
    return None, None


def classify_sell(candidate):
    liq = candidate["liquidity"]
    v5 = candidate["volume_5m"]
    br5 = candidate["buy_ratio_5m"]

    if liq < 10_000 or (v5 < 3_000 and br5 < 0.35):
        return "🔴 RED", "Sell"
    return None, None

# =========================
# MESSAGE
# =========================

def build_message(color, candidate, score, reasons, action):
    mc = int(candidate["market_cap"])
    liq = int(candidate["liquidity"])
    age = "?"
    if candidate["age_min"] is not None:
        age = f"{int(candidate['age_min'])} min"

    reason = " + ".join(reasons[:3]) if reasons else "signal confirmé"

    return (
        f"{color}\n\n"
        f"Token name: {candidate['name']}\n"
        f"Score: {score}/10\n"
        f"Color: {color}\n"
        f"Market cap: ${mc:,}\n"
        f"Liquidity: ${liq:,}\n"
        f"Reason: {reason}\n"
        f"Age: {age}\n"
        f"Dex: https://dexscreener.com/solana/{candidate['token_address']}\n\n"
        f"Action: {action}"
    )

# =========================
# MAIN
# =========================

def main():
    state = load_state()
    cleanup_old_alerts(state)

    profiles = fetch_dex_profiles()
    boosted = fetch_dex_boosts()
    pools = fetch_gecko_new_pools()

    print(f"Fetched {len(pools)} new pools")

    alerts = []

    for candidate in pools:
        token_addr = candidate["token_address"]
        profile = profiles.get(token_addr, {})

        reject, _ = rug_reject(candidate, profile)
        if reject:
            continue

        score, reasons = compute_score(candidate, profile, boosted)

        buy_color, buy_action = classify_buy(score)
        if buy_color and not recently_alerted(state, token_addr):
            alerts.append(
                (score, build_message(buy_color, candidate, score, reasons, buy_action), token_addr)
            )
            continue

        if token_addr in OWNED_TOKENS:
            sell_color, sell_action = classify_sell(candidate)
            if sell_color and not recently_alerted(state, token_addr):
                alerts.append(
                    (score, build_message(sell_color, candidate, score, reasons, sell_action), token_addr)
                )

    alerts.sort(key=lambda x: x[0], reverse=True)

    if alerts:
        sent = 0
        for _, msg, token_addr in alerts:
            send(msg)
            mark_alerted(state, token_addr)
            sent += 1
            if sent >= MAX_ALERTS_PER_RUN:
                break
    else:
        now = int(time.time())
        if now - state.get("last_status_ts", 0) >= STATUS_INTERVAL_SECONDS:
            send(f"🤖 SCANNER ACTIVE — No signals detected | Checked {len(pools)} pools")
            state["last_status_ts"] = now

    save_state(state)


if __name__ == "__main__":
    main()
