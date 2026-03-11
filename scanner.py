import os
import time
import math
import requests
from datetime import datetime, timezone

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()
OWNED_TOKENS = {
    x.strip() for x in os.environ.get("OWNED_TOKENS", "").split(",") if x.strip()
}

GT_PAGES = 10
MAX_ALERTS_PER_RUN = 3
TIMEOUT = 15

GT_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
BIRDEYE_TOP_TRADERS = "https://public-api.birdeye.so/defi/v2/tokens/top_traders"


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


def parse_age_minutes(v):
    if not v:
        return None
    try:
        # ISO time
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)
    except Exception:
        try:
            # epoch ms
            ts = int(v)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return max(0.0, (time.time() - ts) / 60.0)
        except Exception:
            return None


def tx_count(bucket):
    if not isinstance(bucket, dict):
        return 0
    buys = bucket.get("buys", 0)
    sells = bucket.get("sells", 0)
    try:
        return int(buys) + int(sells)
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
            "links_count": len(links),
        }

    return out


def fetch_dex_boosts():
    boosted = set()

    for url in (DEX_BOOSTS_LATEST, DEX_BOOSTS_TOP):
        data = get_json(url)
        if not isinstance(data, list):
            continue
        for item in data:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                boosted.add(item["tokenAddress"])

    return boosted


def fetch_gecko_new_pools():
    pools = []

    for page in range(1, GT_PAGES + 1):
        data = get_json(
            GT_NEW_POOLS,
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

            mc = to_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd"))
            liq = to_float(attrs.get("reserve_in_usd") or attrs.get("liquidity_usd"))

            volume_map = attrs.get("volume_usd", {}) or {}
            tx_map = attrs.get("transactions", {}) or {}

            v5 = to_float(volume_map.get("m5"))
            v1 = to_float(volume_map.get("h1"))
            v24 = to_float(volume_map.get("h24"))

            tx5 = tx_count(tx_map.get("m5"))
            tx1 = tx_count(tx_map.get("h1"))
            br5 = buy_ratio(tx_map.get("m5"))
            br1 = buy_ratio(tx_map.get("h1"))

            age_min = parse_age_minutes(
                attrs.get("pool_created_at")
                or attrs.get("created_at")
            )

            pools.append({
                "name": name,
                "symbol": symbol,
                "token_address": token_addr,
                "dex_id": dex_id,
                "market_cap": mc,
                "liquidity": liq,
                "volume_5m": v5,
                "volume_1h": v1,
                "volume_24h": v24,
                "tx_5m": tx5,
                "tx_1h": tx1,
                "buy_ratio_5m": br5,
                "buy_ratio_1h": br1,
                "age_min": age_min,
            })

        time.sleep(0.15)

    return pools


def get_smart_money_bonus(token_address: str) -> int:
    if not BIRDEYE_API_KEY:
        return 0

    data = get_json(
        BIRDEYE_TOP_TRADERS,
        params={"address": token_address},
        headers={
            "accept": "application/json",
            "x-chain": "solana",
            "X-API-KEY": BIRDEYE_API_KEY,
        },
    )

    if not data:
        return 0

    payload = data.get("data", data)
    traders = []

    if isinstance(payload, list):
        traders = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            traders = payload["items"]
        elif isinstance(payload.get("traders"), list):
            traders = payload["traders"]
        elif isinstance(payload.get("data"), list):
            traders = payload["data"]

    count = len(traders)

    if count >= 10:
        return 2
    if count >= 3:
        return 1
    return 0


def has_enough_socials(profile):
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


def advanced_rug_reject(c, profile):
    mc = c["market_cap"]
    liq = c["liquidity"]
    v5 = c["volume_5m"]
    v1 = c["volume_1h"]
    v24 = c["volume_24h"]
    tx5 = c["tx_5m"]
    br5 = c["buy_ratio_5m"]
    age = c["age_min"]

    if not c["token_address"]:
        return True, "pas d'adresse token"

    if mc <= 0 or mc > 5_000_000:
        return True, "market cap hors filtre"

    if liq < 20_000:
        return True, "liquidité trop faible"

    if liq / max(mc, 1) < 0.05:
        return True, "liquidité trop faible vs market cap"

    if age is not None and age < 2:
        return True, "token trop neuf"

    if age is not None and age > 720:
        return True, "token plus vraiment early"

    if v24 < 20_000:
        return True, "volume 24h trop faible"

    if tx5 < 5 and v5 < 2_500:
        return True, "pas assez d'activité immédiate"

    if br5 < 0.45:
        return True, "pression vendeuse"

    if not has_enough_socials(profile):
        return True, "socials insuffisants"

    # si 1h existe et que 5m est totalement mort
    if v1 > 0 and v5 <= 0:
        return True, "pas de burst 5m"

    return False, ""


def compute_score(c, profile, boosted, smart_bonus):
    mc = c["market_cap"]
    liq = c["liquidity"]
    v5 = c["volume_5m"]
    v1 = c["volume_1h"]
    v24 = c["volume_24h"]
    tx5 = c["tx_5m"]
    tx1 = c["tx_1h"]
    br5 = c["buy_ratio_5m"]
    age = c["age_min"]

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

    # liquidity quality
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
    if v1 > 0 and (v5 * 12) > (0.35 * v1) and tx5 >= 10:
        score += 2
        burst = True
        reasons.append("volume 5m accélère")
    elif v5 > 10_000 and tx5 >= 8:
        score += 1
        reasons.append("activité 5m monte")

    if v24 > 300_000:
        score += 2
        reasons.append("volume 24h solide")
    elif v24 > 100_000:
        score += 1

    # buys
    if br5 > 0.60:
        score += 2
        reasons.append("plus d'acheteurs que de vendeurs")
    elif br5 > 0.53:
        score += 1

    # tx acceleration
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
    if c["token_address"] in boosted:
        score += 1
        reasons.append("boost DexScreener")

    # Pump.fun / launchpad bonus
    if "pump-fun" in c["dex_id"] or "launchlab" in c["dex_id"] or "dbc" in c["dex_id"]:
        score += 1
        reasons.append("très early launchpad")

    # Smart money bonus
    if smart_bonus >= 2:
        score += 2
        reasons.append("smart money détecté")
    elif smart_bonus == 1:
        score += 1
        reasons.append("quelques top traders détectés")

    # age
    if age is not None and 5 <= age <= 120:
        score += 1

    return min(score, 10), reasons


def classify_buy(score):
    if score >= 9:
        return "🟡 GOLD", "Buy 50€ maintenant"
    if score >= 7:
        return "🟢 GREEN", "Buy 25€ maintenant"
    return None, None


def classify_sell(c):
    liq = c["liquidity"]
    v5 = c["volume_5m"]
    br5 = c["buy_ratio_5m"]

    if liq < 10_000 or (v5 < 3_000 and br5 < 0.35):
        return "🔴 RED", "Sell"
    return None, None


def build_message(color, c, score, reasons, action):
    mc = int(c["market_cap"])
    liq = int(c["liquidity"])
    age = "?"
    if c["age_min"] is not None:
        age = f"{int(c['age_min'])} min"

    reason = " + ".join(reasons[:3]) if reasons else "signal confirmé"

    return (
        f"{color}\n\n"
        f"Token name: {c['name']}\n"
        f"Score: {score}/10\n"
        f"Color: {color}\n"
        f"Market cap: ${mc:,}\n"
        f"Liquidity: ${liq:,}\n"
        f"Reason: {reason}\n"
        f"Age: {age}\n"
        f"Dex: https://dexscreener.com/solana/{c['token_address']}\n\n"
        f"Action: {action}"
    )


def main():
    profiles = fetch_dex_profiles()
    boosted = fetch_dex_boosts()
    pools = fetch_gecko_new_pools()

    print(f"Fetched {len(pools)} new pools")

    if not pools:
        send("🤖 SCANNER ACTIVE — No signals detected")
        return

    alerts = []

    for c in pools:
        profile = profiles.get(c["token_address"], {})
        reject, why = advanced_rug_reject(c, profile)
        if reject:
            continue

        smart_bonus = 0
        # on ne dépense des appels Birdeye que pour les candidats déjà intéressants
        rough_interest = (
            c["market_cap"] < 5_000_000
            and c["liquidity"] > 20_000
            and c["volume_24h"] > 50_000
        )
        if rough_interest and BIRDEYE_API_KEY:
            smart_bonus = get_smart_money_bonus(c["token_address"])

        score, reasons = compute_score(c, profile, boosted, smart_bonus)

        color, action = classify_buy(score)
        if color:
            alerts.append((score, build_message(color, c, score, reasons, action)))
            continue

        if c["token_address"] in OWNED_TOKENS:
            sell_color, sell_action = classify_sell(c)
            if sell_color:
                alerts.append((score, build_message(sell_color, c, score, reasons, sell_action)))

    alerts.sort(key=lambda x: x[0], reverse=True)

    if not alerts:
        send(f"🤖 SCANNER ACTIVE — No signals detected | Checked {len(pools)} pools")
        return

    sent = 0
    for _, msg in alerts:
        send(msg)
        sent += 1
        if sent >= MAX_ALERTS_PER_RUN:
            break


if __name__ == "__main__":
    main()
