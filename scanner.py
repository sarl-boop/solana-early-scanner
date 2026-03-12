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

BATCHES = 10
PAGES_PER_BATCH = 1
PAUSE_BETWEEN_BATCHES = 1.8
RETRY_429_WAIT = 4.0

MAX_ALERTS_PER_RUN = 4
TIMEOUT = 15

ALERT_COOLDOWN_HOURS = 12
STATUS_INTERVAL_SECONDS = 3600
SEEN_TTL_HOURS = 48

GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_COMMUNITY_TAKEOVERS = "https://api.dexscreener.com/community-takeovers/latest/v1"

# =========================
# UTILS
# =========================

def send(msg: str) -> None:
    try:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print("Discord send error:", e)


def to_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def now_ts() -> int:
    return int(time.time())


def load_state():
    default_state = {
        "alerted": {},
        "last_status_ts": 0,
        "seen": {}
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
        if "seen" not in data or not isinstance(data["seen"], dict):
            data["seen"] = {}

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
    now = now_ts()
    keep = {}
    for addr, ts in state["alerted"].items():
        if now - ts < 7 * 24 * 3600:
            keep[addr] = ts
    state["alerted"] = keep


def cleanup_old_seen(state):
    now = now_ts()
    keep = {}
    for addr, rec in state["seen"].items():
        if not isinstance(rec, dict):
            continue
        last_seen = int(rec.get("last_seen_ts", 0))
        if now - last_seen < SEEN_TTL_HOURS * 3600:
            keep[addr] = rec
    state["seen"] = keep


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
# HTTP
# =========================

def get_json(url: str, params=None, headers=None):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("GET error:", url, e)
        return None


def get_json_with_429_retry(url: str, params=None, headers=None, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)

            if r.status_code == 429:
                print("429 rate limit on", url, "attempt", attempt + 1)
                if attempt < retries:
                    time.sleep(RETRY_429_WAIT)
                    continue
                return None

            r.raise_for_status()
            return r.json()

        except Exception as e:
            print("GET error:", url, e)
            if attempt < retries:
                time.sleep(1.0)
                continue
            return None

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


def fetch_dex_community_takeovers():
    takeover = set()
    data = get_json(DEX_COMMUNITY_TAKEOVERS)
    if not isinstance(data, list):
        return takeover

    for item in data:
        if item.get("chainId") == "solana":
            addr = item.get("tokenAddress")
            if addr:
                takeover.add(addr)

    return takeover

# =========================
# GECKOTERMINAL
# =========================

def fetch_gecko_new_pools_batched():
    pools = []
    page = 1
    raw_count = 0

    for _ in range(BATCHES):
        for _ in range(PAGES_PER_BATCH):
            data = get_json_with_429_retry(
                GECKO_NEW_POOLS,
                params={"page": page, "include": "base_token,dex"},
                headers={"accept": "application/json"},
                retries=2,
            )

            if data and "data" in data:
                included = data.get("included", []) or []
                inc_map = {}
                for obj in included:
                    inc_map[(obj.get("type"), obj.get("id"))] = obj

                for pool in data.get("data", []):
                    raw_count += 1

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
                    fdv = to_float(attrs.get("fdv_usd"))
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
                        "fdv": fdv,
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

            page += 1

        time.sleep(PAUSE_BETWEEN_BATCHES)

    dedup = {}
    for p in pools:
        addr = p["token_address"]
        if addr and addr not in dedup:
            dedup[addr] = p

    print(f"Raw pools fetched: {raw_count}")
    return list(dedup.values())

# =========================
# FILTERS
# =========================

def socials_quality(profile):
    if not profile:
        return 0
    score = 0
    if profile.get("has_website"):
        score += 1
    if profile.get("has_x"):
        score += 1
    if profile.get("has_discord") or profile.get("has_telegram"):
        score += 1
    return score


def wash_trading_risk(candidate):
    liq = candidate["liquidity"]
    v24 = candidate["volume_24h"]
    tx5 = candidate["tx_5m"]
    age = candidate["age_min"]

    if liq <= 0:
        return False

    vol_liq_ratio = v24 / liq

    # suspicious when volume is absurdly high relative to tiny pool
    if vol_liq_ratio > 35:
        return True

    # very early hyperactivity on very small liquidity
    if age is not None and age <= 20 and liq < 25_000 and tx5 > 45:
        return True

    return False


def fragile_pool_risk(candidate):
    liq = candidate["liquidity"]
    tx5 = candidate["tx_5m"]
    v5 = candidate["volume_5m"]

    return liq < 30_000 and (tx5 > 40 or v5 > 20_000)


def unknown_mcap_risk(candidate):
    mc = candidate["market_cap"]
    fdv = candidate["fdv"]

    return mc <= 0 and fdv <= 0


def rug_reject(candidate, profile):
    mc = candidate["market_cap"]
    fdv = candidate["fdv"]
    liq = candidate["liquidity"]
    v24 = candidate["volume_24h"]
    v5 = candidate["volume_5m"]
    tx5 = candidate["tx_5m"]
    br5 = candidate["buy_ratio_5m"]
    age = candidate["age_min"]

    effective_mc = mc if mc > 0 else fdv

    if not candidate["token_address"]:
        return True

    if unknown_mcap_risk(candidate):
        return True

    if effective_mc <= 0 or effective_mc > 5_000_000:
        return True

    if liq < 15_000:
        return True

    if liq / max(effective_mc, 1) < 0.04:
        return True

    # very early is okay; only sub-1-minute is too raw
    if age is not None and age < 1:
        return True

    if age is not None and age > 720:
        return True

    if v24 < 15_000:
        return True

    if tx5 < 4 and v5 < 2_000:
        return True

    if br5 < 0.45:
        return True

    if socials_quality(profile) == 0 and (v24 < 40_000 or tx5 < 8):
        return True

    if wash_trading_risk(candidate):
        return True

    if fragile_pool_risk(candidate):
        return True

    return False

# =========================
# RADARS
# =========================

def momentum_signal(candidate):
    v5 = candidate["volume_5m"]
    v1 = candidate["volume_1h"]

    if v1 <= 0:
        return False

    return v5 * 12 > v1 * 0.30 and v5 > 6000


def holder_burst(candidate):
    tx5 = candidate["tx_5m"]
    tx1 = candidate["tx_1h"]

    if tx1 <= 0:
        return False

    return tx5 >= 10 and tx5 * 10 > tx1 * 0.25


def pumpfun_style(candidate):
    dex = candidate["dex_id"]
    mc = candidate["market_cap"]
    fdv = candidate["fdv"]
    liq = candidate["liquidity"]

    effective_mc = mc if mc > 0 else fdv
    return "pump" in dex and effective_mc < 300_000 and liq > 20_000


def x_engine(candidate):
    mc = candidate["market_cap"]
    fdv = candidate["fdv"]
    liq = candidate["liquidity"]
    v5 = candidate["volume_5m"]
    v1 = candidate["volume_1h"]
    tx5 = candidate["tx_5m"]
    tx1 = candidate["tx_1h"]
    br5 = candidate["buy_ratio_5m"]

    effective_mc = mc if mc > 0 else fdv
    signals = 0

    if 10_000 < effective_mc < 200_000:
        signals += 1

    if liq / max(effective_mc, 1) > 0.25:
        signals += 1

    if v1 > 0 and v5 * 12 > v1 * 0.35:
        signals += 1

    if tx1 > 0 and tx5 >= 12 and tx5 * 10 > tx1 * 0.25:
        signals += 1

    if br5 > 0.60:
        signals += 1

    return signals


def update_seen_record(state, candidate):
    addr = candidate["token_address"]
    now = now_ts()

    effective_mc = candidate["market_cap"] if candidate["market_cap"] > 0 else candidate["fdv"]

    rec = state["seen"].get(addr, {})
    if not isinstance(rec, dict):
        rec = {}

    first_seen_ts = int(rec.get("first_seen_ts", now))
    first_seen_mc = to_float(rec.get("first_seen_mc"), effective_mc)
    first_seen_v5 = to_float(rec.get("first_seen_v5"), candidate["volume_5m"])

    max_mc = max(to_float(rec.get("max_mc"), effective_mc), effective_mc)
    max_v5 = max(to_float(rec.get("max_v5"), candidate["volume_5m"]), candidate["volume_5m"])
    max_tx5 = max(int(rec.get("max_tx5", candidate["tx_5m"])), candidate["tx_5m"])

    prev_v5 = to_float(rec.get("last_v5"), candidate["volume_5m"])
    prev_tx5 = int(rec.get("last_tx5", candidate["tx_5m"]))
    prev_mc = to_float(rec.get("last_mc"), effective_mc)

    new_rec = {
        "first_seen_ts": first_seen_ts,
        "first_seen_mc": first_seen_mc,
        "first_seen_v5": first_seen_v5,
        "last_seen_ts": now,
        "last_mc": effective_mc,
        "last_v5": candidate["volume_5m"],
        "last_tx5": candidate["tx_5m"],
        "prev_mc": prev_mc,
        "prev_v5": prev_v5,
        "prev_tx5": prev_tx5,
        "max_mc": max_mc,
        "max_v5": max_v5,
        "max_tx5": max_tx5,
    }

    state["seen"][addr] = new_rec
    return new_rec


def no_chase_filter(candidate, seen):
    current_mc = candidate["market_cap"] if candidate["market_cap"] > 0 else candidate["fdv"]
    age = candidate["age_min"]

    first_mc = to_float(seen.get("first_seen_mc"), current_mc)
    max_mc = to_float(seen.get("max_mc"), current_mc)

    if age is not None and age > 15 and first_mc > 0 and current_mc > first_mc * 2.4:
        return True

    if max_mc > 0 and current_mc >= max_mc * 0.98 and first_mc > 0 and max_mc > first_mc * 2.8:
        return True

    return False


def early_base_signal(candidate, seen):
    current_mc = candidate["market_cap"] if candidate["market_cap"] > 0 else candidate["fdv"]
    first_mc = to_float(seen.get("first_seen_mc"), current_mc)
    age = candidate["age_min"]
    br5 = candidate["buy_ratio_5m"]

    if first_mc <= 0:
        return False

    return (
        age is not None
        and age <= 20
        and current_mc <= first_mc * 1.35
        and br5 > 0.55
        and candidate["volume_5m"] > 6000
    )


def second_wave_signal(candidate, seen):
    current_mc = candidate["market_cap"] if candidate["market_cap"] > 0 else candidate["fdv"]
    br5 = candidate["buy_ratio_5m"]
    v5 = candidate["volume_5m"]
    tx5 = candidate["tx_5m"]

    first_mc = to_float(seen.get("first_seen_mc"), current_mc)
    prev_v5 = to_float(seen.get("prev_v5"), v5)
    prev_tx5 = int(seen.get("prev_tx5", tx5))
    max_mc = to_float(seen.get("max_mc"), current_mc)

    if first_mc <= 0 or max_mc <= 0:
        return False

    return (
        max_mc > first_mc * 1.25
        and current_mc < max_mc * 0.92
        and current_mc > first_mc * 1.05
        and v5 > max(prev_v5 * 1.5, 8000)
        and tx5 >= max(prev_tx5 + 3, 8)
        and br5 > 0.56
    )

# =========================
# SCORE
# =========================

def compute_score(candidate, profile, boosted, takeovers, seen):
    mc = candidate["market_cap"]
    fdv = candidate["fdv"]
    liq = candidate["liquidity"]
    v5 = candidate["volume_5m"]
    v1 = candidate["volume_1h"]
    v24 = candidate["volume_24h"]
    tx5 = candidate["tx_5m"]
    tx1 = candidate["tx_1h"]
    br5 = candidate["buy_ratio_5m"]
    age = candidate["age_min"]
    token_addr = candidate["token_address"]

    effective_mc = mc if mc > 0 else fdv

    score = 0
    reasons = []

    # Micro-cap
    if effective_mc < 800_000:
        score += 3
        reasons.append("micro-cap basse")
    elif effective_mc < 2_000_000:
        score += 2
        reasons.append("micro-cap correcte")
    elif effective_mc < 5_000_000:
        score += 1

    # Liquidité
    liq_ratio = liq / max(effective_mc, 1)
    if liq > 120_000:
        score += 2
        reasons.append("bonne liquidité")
    elif liq > 60_000:
        score += 1
        reasons.append("liquidité correcte")

    if liq_ratio > 0.20:
        score += 2
        reasons.append("liquidité forte vs market cap")
    elif liq_ratio > 0.10:
        score += 1

    # Volume burst
    burst = False
    if v1 > 0 and (v5 * 10) > (0.25 * v1) and tx5 >= 6:
        score += 2
        reasons.append("volume 5m accélère")
        burst = True
    elif v5 > 8_000 and tx5 >= 6:
        score += 1
        reasons.append("activité 5m monte")

    if v24 > 250_000:
        score += 2
        reasons.append("volume 24h solide")
    elif v24 > 80_000:
        score += 1

    # Buy pressure
    if br5 > 0.60:
        score += 2
        reasons.append("acheteurs dominants")
    elif br5 > 0.53:
        score += 1

    # Transactions burst
    if tx5 >= 18:
        score += 2
    elif tx5 >= 8:
        score += 1

    if tx1 > 0 and tx5 > 0 and tx5 * 12 > 0.20 * tx1:
        score += 1
        if not burst:
            reasons.append("transactions accélèrent")

    # Socials
    social_score = socials_quality(profile)
    if social_score >= 3:
        score += 2
        reasons.append("site + X + communauté")
    elif social_score == 2:
        score += 1
    elif social_score == 1 and v24 > 80_000:
        score += 1

    # Dex boosts
    if token_addr in boosted:
        score += 1
        reasons.append("boost DexScreener")

    # Community takeover
    if token_addr in takeovers:
        score += 1
        reasons.append("community takeover")

    # Launchpad / pump-like
    if "pump" in candidate["dex_id"] or "launch" in candidate["dex_id"]:
        score += 1
        reasons.append("launchpad early")

    # Sweet spot d’âge
    if age is not None and 2 <= age <= 180:
        score += 1

    # V4/V5 bonuses
    if momentum_signal(candidate):
        score += 1
        reasons.append("momentum radar")

    if holder_burst(candidate):
        score += 1
        reasons.append("holder burst")

    if pumpfun_style(candidate):
        score += 1
        reasons.append("pump.fun early")

    x_signals = x_engine(candidate)
    if x_signals >= 4:
        score += 2
        reasons.append("x-engine signals")
    elif x_signals >= 3:
        score += 1
        reasons.append("x-engine momentum")

    # V6 bonuses
    if early_base_signal(candidate, seen):
        score += 1
        reasons.append("early base")

    if second_wave_signal(candidate, seen):
        score += 2
        reasons.append("second wave")

    # anti-trap soft penalties
    if wash_trading_risk(candidate):
        score -= 3
        reasons.append("wash-trading risk")

    if fragile_pool_risk(candidate):
        score -= 2
        reasons.append("fragile pool")

    if candidate["market_cap"] <= 0 and candidate["fdv"] > 0:
        score -= 1
        reasons.append("mcap incertaine")

    return max(0, min(score, 10)), reasons


def classify_buy(score, candidate, seen):
    if no_chase_filter(candidate, seen):
        return None, None

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

def build_message(color, candidate, score, reasons, action, seen):
    current_mc = candidate["market_cap"] if candidate["market_cap"] > 0 else candidate["fdv"]
    mc = int(current_mc)
    liq = int(candidate["liquidity"])
    first_mc = int(to_float(seen.get("first_seen_mc"), current_mc))
    max_mc = int(to_float(seen.get("max_mc"), current_mc))

    age = "?"
    if candidate["age_min"] is not None:
        age = f"{int(candidate['age_min'])} min"

    reason = " + ".join(reasons[:4]) if reasons else "signal confirmé"

    return (
        f"{color}\n\n"
        f"Token name: {candidate['name']}\n"
        f"Score: {score}/10\n"
        f"Color: {color}\n"
        f"Market cap: ${mc:,}\n"
        f"Liquidity: ${liq:,}\n"
        f"First seen MC: ${first_mc:,}\n"
        f"Max seen MC: ${max_mc:,}\n"
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
    cleanup_old_seen(state)

    profiles = fetch_dex_profiles()
    boosted = fetch_dex_boosts()
    takeovers = fetch_dex_community_takeovers()
    pools = fetch_gecko_new_pools_batched()

    print(f"Checked {len(pools)} pools")
    print(f"Dex community takeovers: {len(takeovers)}")

    alerts = []

    for candidate in pools:
        token_addr = candidate["token_address"]
        profile = profiles.get(token_addr, {})

        seen = update_seen_record(state, candidate)

        if rug_reject(candidate, profile):
            continue

        score, reasons = compute_score(candidate, profile, boosted, takeovers, seen)

        buy_color, buy_action = classify_buy(score, candidate, seen)
        if buy_color and not recently_alerted(state, token_addr):
            alerts.append(
                (
                    score,
                    build_message(buy_color, candidate, score, reasons, buy_action, seen),
                    token_addr
                )
            )
            continue

        if token_addr in OWNED_TOKENS:
            sell_color, sell_action = classify_sell(candidate)
            if sell_color and not recently_alerted(state, token_addr):
                alerts.append(
                    (
                        score,
                        build_message(sell_color, candidate, score, reasons, sell_action, seen),
                        token_addr
                    )
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
        now = now_ts()
        if now - state.get("last_status_ts", 0) >= STATUS_INTERVAL_SECONDS:
            send(f"🤖 SCANNER ACTIVE — No signals detected | Checked {len(pools)} pools")
            state["last_status_ts"] = now

    save_state(state)


if __name__ == "__main__":
    main()
