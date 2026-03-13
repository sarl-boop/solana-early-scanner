import asyncio
import csv
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Set

import requests
import websockets

# =========================================================
# CONFIG
# =========================================================

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "").strip()

# Alpha wallets / smart wallets
SMART_WALLETS = {
    x.strip() for x in os.environ.get("SMART_WALLETS", "").split(",") if x.strip()
}

# Optional: only needed if you already hold tokens
HELD_TOKENS = {
    x.strip() for x in os.environ.get("HELD_TOKENS", "").split(",") if x.strip()
}

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"

STATE_FILE = Path("state.json")
ALERT_LOG_FILE = Path("alerts_log.csv")

HEARTBEAT_INTERVAL_SECONDS = 3600
SAVE_INTERVAL_SECONDS = 30
EVALUATE_INTERVAL_SECONDS = 12
GECKO_REFRESH_SECONDS = 20

TOKEN_TTL_SECONDS = 2 * 3600
ALERT_COOLDOWN_SECONDS = 12 * 3600

MAX_MARKET_CAP = 5_000_000
MIN_LIQUIDITY = 15_000
MIN_LIQ_TO_MC_RATIO = 0.50
WASH_RATIO_LIMIT = 35.0
NO_CHASE_MULTIPLIER = 2.0

GOLD_SCORE = 8

TOP1_HARD_REJECT = 0.15
TOP3_HARD_REJECT = 0.35
TOP1_SOFT_PENALTY = 0.08
TOP3_SOFT_PENALTY = 0.22

BUY_ALERT_WINDOW_SECONDS = 20 * 60
MAX_BUY_ALERTS_PER_WINDOW = 2

LOCKER_KEYWORDS = ["locker", "locked", "burn", "null", "dead"]
MIGRATION_SOURCES = {"migration", "raydium_pool", "meteora_pool", "orca_pool"}

DEBUG = True

# =========================================================
# STATE
# =========================================================

STATE: Dict[str, Any] = {
    "tokens": {},
    "alerted": {},
    "last_heartbeat": 0,
    "buy_alert_history": [],
}

SUBSCRIBED_TOKEN_TRADES: Set[str] = set()

# token state shape includes:
# early_buys, early_sells, early_unique_buyers, early_unique_sellers,
# early_volume_est, smart_wallet_hits, alpha_cluster_score, migration_flag

# =========================================================
# UTILS
# =========================================================

def dbg(*args):
    if DEBUG:
        print(*args, flush=True)


def now_ts() -> int:
    return int(time.time())


def to_float(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def compact_k(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def load_state() -> None:
    global STATE
    if not STATE_FILE.exists():
        dbg("state file not found, starting fresh")
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            STATE = data
            STATE.setdefault("tokens", {})
            STATE.setdefault("alerted", {})
            STATE.setdefault("last_heartbeat", 0)
            STATE.setdefault("buy_alert_history", [])
    except Exception as e:
        print("state load error:", e)


def save_state() -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(STATE, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("state save error:", e)


def cleanup_state() -> None:
    now = now_ts()

    keep_alerted = {}
    for key, ts in STATE.get("alerted", {}).items():
        if now - int(ts) < 7 * 24 * 3600:
            keep_alerted[key] = ts
    STATE["alerted"] = keep_alerted

    keep_tokens = {}
    for mint, rec in STATE.get("tokens", {}).items():
        if now - int(rec.get("last_seen_ts", 0)) < TOKEN_TTL_SECONDS:
            keep_tokens[mint] = rec
    STATE["tokens"] = keep_tokens

    STATE["buy_alert_history"] = [
        int(ts) for ts in STATE.get("buy_alert_history", [])
        if now - int(ts) < BUY_ALERT_WINDOW_SECONDS
    ]


def recently_alerted(alert_key: str) -> bool:
    ts = int(STATE.get("alerted", {}).get(alert_key, 0))
    return (now_ts() - ts) < ALERT_COOLDOWN_SECONDS


def mark_alerted(alert_key: str) -> None:
    STATE["alerted"][alert_key] = now_ts()


def can_send_buy_alert() -> bool:
    cleanup_state()
    return len(STATE.get("buy_alert_history", [])) < MAX_BUY_ALERTS_PER_WINDOW


def mark_buy_alert_sent() -> None:
    STATE.setdefault("buy_alert_history", []).append(now_ts())


def send_discord(msg: str) -> None:
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
    except Exception as e:
        print("discord send error:", e)


def ensure_alert_log_file() -> None:
    if ALERT_LOG_FILE.exists():
        return
    with ALERT_LOG_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "alert_type", "mint", "name", "source", "score",
            "market_cap", "liquidity", "first_seen_mc", "max_seen_mc", "age_min",
            "top1_pct", "top3_pct", "migration_flag", "smart_wallet_count",
            "early_buys", "early_unique_buyers", "dev_sold", "tradeability_ok", "dex_url"
        ])


def log_alert_csv(
    alert_type: str,
    mint: str,
    name: str,
    source: str,
    score: int,
    market_cap: float,
    liquidity: float,
    first_seen_mc: float,
    max_seen_mc: float,
    age_min: float,
    top1_pct: float,
    top3_pct: float,
    migration_flag: bool,
    smart_wallet_count: int,
    early_buys: int,
    early_unique_buyers: int,
    dev_sold: bool,
    tradeability_ok: bool,
    dex_url: str,
) -> None:
    try:
        ensure_alert_log_file()
        with ALERT_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                now_ts(), alert_type, mint, name, source, score,
                round(market_cap, 2), round(liquidity, 2),
                round(first_seen_mc, 2), round(max_seen_mc, 2), round(age_min, 2),
                round(top1_pct, 4), round(top3_pct, 4), migration_flag,
                smart_wallet_count, early_buys, early_unique_buyers, dev_sold,
                tradeability_ok, dex_url
            ])
    except Exception as e:
        print("alert log write error:", e)


def ensure_token(mint: str, name: str = "", symbol: str = "", source: str = "unknown") -> dict:
    rec = STATE["tokens"].get(mint)
    if rec:
        if name and not rec.get("name"):
            rec["name"] = name
        if symbol and not rec.get("symbol"):
            rec["symbol"] = symbol
        if source and rec.get("source") == "unknown":
            rec["source"] = source
        if source in MIGRATION_SOURCES:
            rec["migration_flag"] = True
        rec["last_seen_ts"] = now_ts()
        return rec

    rec = {
        "mint": mint,
        "name": name or mint[:6],
        "symbol": symbol or "",
        "source": source,
        "first_seen_ts": now_ts(),
        "last_seen_ts": now_ts(),
        "first_seen_mc": 0.0,
        "max_seen_mc": 0.0,
        "last_pair_url": "",
        "smart_wallet_hits": [],
        "buy_wallet_counts": {},
        "sell_wallet_counts": {},
        "first_buy_wallets": [],
        "first_buy_ts": 0,
        "candidate_dev_wallet": None,
        "dev_sold": False,
        "tradeability_ok": True,
        "liq_lock_hint": False,
        "migration_flag": source in MIGRATION_SOURCES,
        "early_buys": 0,
        "early_sells": 0,
        "early_unique_buyers": [],
        "early_unique_sellers": [],
        "early_volume_est": 0.0,
        "alpha_cluster_score": 0,
    }
    STATE["tokens"][mint] = rec
    return rec

# =========================================================
# SOLANA RPC
# =========================================================

def rpc_call(method: str, params: list) -> Optional[dict]:
    if not SOLANA_RPC_URL:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(SOLANA_RPC_URL, json=payload, timeout=15)
        r.raise_for_status()
        return r.json().get("result")
    except Exception:
        return None


def get_token_supply(mint: str) -> float:
    result = rpc_call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    if not result:
        return 0.0
    return to_float((result.get("value") or {}).get("uiAmount"), 0.0)


def get_token_largest_accounts(mint: str) -> List[dict]:
    result = rpc_call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    if not result:
        return []
    return result.get("value", []) or []


def get_holder_stats(mint: str) -> dict:
    if not SOLANA_RPC_URL:
        return {
            "enabled": False, "top1_pct": 0.0, "top3_pct": 0.0,
            "hard_reject": False, "soft_penalty": False,
        }

    supply = get_token_supply(mint)
    largest = get_token_largest_accounts(mint)

    if supply <= 0 or not largest:
        return {
            "enabled": False, "top1_pct": 0.0, "top3_pct": 0.0,
            "hard_reject": False, "soft_penalty": False,
        }

    amounts = [to_float(x.get("uiAmount"), 0.0) for x in largest[:3]]
    top1_pct = amounts[0] / supply if amounts else 0.0
    top3_pct = sum(amounts) / supply if supply > 0 else 0.0

    hard_reject = top1_pct > TOP1_HARD_REJECT or top3_pct > TOP3_HARD_REJECT
    soft_penalty = top1_pct > TOP1_SOFT_PENALTY or top3_pct > TOP3_SOFT_PENALTY

    return {
        "enabled": True,
        "top1_pct": top1_pct,
        "top3_pct": top3_pct,
        "hard_reject": hard_reject,
        "soft_penalty": soft_penalty,
    }

# =========================================================
# DEX / GECKO
# =========================================================

def get_best_pair(mint: str) -> Optional[dict]:
    try:
        r = requests.get(DEX_TOKEN_API + mint, timeout=12)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        solana_pairs = [p for p in data if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None
        solana_pairs.sort(
            key=lambda x: to_float((x.get("liquidity") or {}).get("usd"), 0.0),
            reverse=True,
        )
        return solana_pairs[0]
    except Exception:
        return None


def fetch_gecko_new_pools() -> List[dict]:
    try:
        r = requests.get(
            GECKO_NEW_POOLS,
            params={"page": 1, "include": "base_token,dex"},
            headers={"accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        out = []
        included = data.get("included", []) or []
        inc_map = {}
        for obj in included:
            inc_map[(obj.get("type"), obj.get("id"))] = obj

        for pool in data.get("data", []):
            attrs = pool.get("attributes", {}) or {}
            rels = pool.get("relationships", {}) or {}

            base_ref = (((rels.get("base_token") or {}).get("data")) or {})
            base_obj = inc_map.get((base_ref.get("type"), base_ref.get("id")), {})
            base_attrs = base_obj.get("attributes", {}) or {}

            mint = base_attrs.get("address") or attrs.get("base_token_address")
            if not mint:
                continue

            name = base_attrs.get("name") or attrs.get("name", "").split("/")[0].strip() or mint[:6]
            symbol = base_attrs.get("symbol") or ""

            out.append({
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "source": "gecko_new_pool",
            })
        return out
    except Exception:
        return []

# =========================================================
# MESSAGE PARSING
# =========================================================

def extract_message_payload(raw: str) -> Optional[dict]:
    try:
        msg = json.loads(raw)
    except Exception:
        return None
    if isinstance(msg, dict):
        if isinstance(msg.get("data"), dict):
            return msg["data"]
        return msg
    return None


def extract_mint(msg: dict) -> Optional[str]:
    return (
        msg.get("mint")
        or msg.get("token")
        or msg.get("tokenAddress")
        or msg.get("baseTokenAddress")
    )


def extract_name(msg: dict) -> str:
    return msg.get("name") or msg.get("tokenName") or ""


def extract_symbol(msg: dict) -> str:
    return msg.get("symbol") or msg.get("tokenSymbol") or ""


def extract_wallet(msg: dict) -> Optional[str]:
    return (
        msg.get("traderPublicKey")
        or msg.get("wallet")
        or msg.get("account")
        or msg.get("maker")
    )


def extract_side(msg: dict) -> str:
    side = str(msg.get("txType") or msg.get("side") or msg.get("type") or "").lower()
    if "buy" in side:
        return "buy"
    if "sell" in side:
        return "sell"
    return ""


def extract_amount_usd(msg: dict) -> float:
    for key in ["usdMarketCap", "volumeUsd", "amountUsd", "usdValue"]:
        if key in msg:
            return to_float(msg.get(key), 0.0)
    return 0.0

# =========================================================
# TRACKING
# =========================================================

def update_trade_tracking(mint: str, wallet: Optional[str], side: str, usd_est: float = 0.0) -> None:
    if not wallet or side not in {"buy", "sell"}:
        return

    rec = ensure_token(mint)

    if side == "buy":
        if rec["first_buy_ts"] == 0:
            rec["first_buy_ts"] = now_ts()

        rec["early_buys"] = int(rec.get("early_buys", 0)) + 1
        rec["early_volume_est"] = to_float(rec.get("early_volume_est", 0.0)) + usd_est

        counts = rec["buy_wallet_counts"]
        counts[wallet] = counts.get(wallet, 0) + 1

        buyers = set(rec.get("early_unique_buyers", []))
        buyers.add(wallet)
        rec["early_unique_buyers"] = list(buyers)

        if len(rec["first_buy_wallets"]) < 10 and wallet not in rec["first_buy_wallets"]:
            rec["first_buy_wallets"].append(wallet)

        if rec.get("candidate_dev_wallet") is None and len(rec["first_buy_wallets"]) <= 2:
            rec["candidate_dev_wallet"] = wallet

    else:
        rec["early_sells"] = int(rec.get("early_sells", 0)) + 1

        sell_counts = rec["sell_wallet_counts"]
        sell_counts[wallet] = sell_counts.get(wallet, 0) + 1

        sellers = set(rec.get("early_unique_sellers", []))
        sellers.add(wallet)
        rec["early_unique_sellers"] = list(sellers)

        if wallet == rec.get("candidate_dev_wallet"):
            rec["dev_sold"] = True


def add_smart_wallet_hit(mint: str, wallet: str) -> None:
    rec = ensure_token(mint)
    hits = set(rec.get("smart_wallet_hits", []))
    hits.add(wallet)
    rec["smart_wallet_hits"] = list(hits)

    hit_count = len(rec["smart_wallet_hits"])
    if hit_count >= 4:
        rec["alpha_cluster_score"] = 5
    elif hit_count >= 2:
        rec["alpha_cluster_score"] = 3
    else:
        rec["alpha_cluster_score"] = 1

# =========================================================
# FILTERS / SCORE
# =========================================================

def sniper_trap_risk(token_state: dict) -> bool:
    counts = token_state.get("buy_wallet_counts", {})
    first_buy_wallets = token_state.get("first_buy_wallets", [])

    if not counts:
        return False

    total_buys = sum(counts.values())
    unique_wallets = len(counts)
    biggest = max(counts.values()) if counts else 0

    if total_buys < 6:
        return False
    if biggest >= 3:
        return True
    if total_buys >= 10 and unique_wallets <= 3:
        return True
    if len(first_buy_wallets) >= 5 and len(set(first_buy_wallets[:5])) <= 2:
        return True
    return False


def fake_liquidity_risk(mc: float, liq: float) -> bool:
    if mc <= 0:
        return True
    return liq < mc * MIN_LIQ_TO_MC_RATIO


def wash_trading_risk(v24: float, liq: float) -> bool:
    if liq <= 0:
        return False
    return (v24 / liq) > WASH_RATIO_LIMIT


def no_chase_risk(first_seen_mc: float, current_mc: float, age_min: float) -> bool:
    if first_seen_mc <= 0 or age_min <= 10:
        return False
    return current_mc > first_seen_mc * NO_CHASE_MULTIPLIER


def liquidity_lock_hint(pair: dict) -> bool:
    text = json.dumps(pair).lower()
    return any(k in text for k in LOCKER_KEYWORDS)


def anti_honeypot_guard(pair: dict, token_state: dict) -> bool:
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    txs = pair.get("txns") or {}
    m5 = txs.get("m5") or {}
    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))
    if liq > 10_000 and buys == 0 and sells == 0:
        return False
    return True


def early_pump_signal(token_state: dict) -> bool:
    buys = int(token_state.get("early_buys", 0))
    uniq = len(token_state.get("early_unique_buyers", []))
    vol = to_float(token_state.get("early_volume_est", 0.0), 0.0)
    return buys >= 8 and uniq >= 5 and vol >= 2000


def live_confirmation_count(pair: dict, token_state: dict, holder_stats: dict) -> int:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    txs = pair.get("txns") or {}
    m5 = txs.get("m5") or {}
    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))
    total = buys + sells

    count = 0
    if mc > 0 and liq >= mc * 0.6:
        count += 1
    if total >= 8 and buys > sells:
        count += 1
    if len(token_state.get("smart_wallet_hits", [])) >= 1:
        count += 1
    if token_state.get("migration_flag"):
        count += 1
    if holder_stats.get("enabled"):
        count += 1
    if early_pump_signal(token_state):
        count += 1
    return count


def compute_score(pair: dict, token_state: dict, holder_stats: dict) -> int:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)

    vol = pair.get("volume") or {}
    txs = pair.get("txns") or {}

    v5 = to_float(vol.get("m5"), 0.0)
    v1 = to_float(vol.get("h1"), 0.0)
    v24 = to_float(vol.get("h24"), 0.0)

    m5 = txs.get("m5") or {}
    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))
    total = buys + sells
    buy_ratio = buys / total if total > 0 else 0.5

    age_min = max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0)

    score = 0

    if mc < 50_000:
        score += 2
    elif mc < 200_000:
        score += 1

    if liq >= mc * 0.7:
        score += 2
    elif liq >= mc * 0.5:
        score += 1

    if v1 > 0 and v5 * 12 > v1 * 0.24 and v5 > 3500:
        score += 2
    elif v5 > 6000:
        score += 1

    if buys > sells:
        score += 1
    if buy_ratio > 0.60:
        score += 1
    if buys >= 7:
        score += 1

    if age_min <= 8:
        score += 1

    if early_pump_signal(token_state):
        score += 2

    if len(token_state.get("early_unique_buyers", [])) >= 8:
        score += 2

    score += int(token_state.get("alpha_cluster_score", 0))

    if token_state.get("migration_flag"):
        score += 1

    if token_state.get("liq_lock_hint"):
        score += 1

    if holder_stats.get("soft_penalty"):
        score -= 2
    if sniper_trap_risk(token_state):
        score -= 4
    if wash_trading_risk(v24, liq):
        score -= 3
    if token_state.get("dev_sold"):
        score -= 4
    if not token_state.get("tradeability_ok", True):
        score -= 4

    return max(0, min(10, score))


def compute_priority(pair: dict, token_state: dict, holder_stats: dict, score: int) -> int:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    age_min = max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0)

    p = 0
    if score >= 9:
        p += 2
    elif score >= 8:
        p += 1

    if age_min <= 5:
        p += 2
    elif age_min <= 8:
        p += 1

    if token_state.get("first_seen_mc", 0.0) > 0 and token_state.get("first_seen_mc", 0.0) < 20_000:
        p += 2

    if mc > 0 and liq >= mc:
        p += 2
    elif mc > 0 and liq >= mc * 0.7:
        p += 1

    if early_pump_signal(token_state):
        p += 2

    smart_hits = len(token_state.get("smart_wallet_hits", []))
    if smart_hits >= 4:
        p += 3
    elif smart_hits >= 2:
        p += 2
    elif smart_hits >= 1:
        p += 1

    if token_state.get("migration_flag"):
        p += 1
    if token_state.get("liq_lock_hint"):
        p += 1
    if holder_stats.get("enabled"):
        p += 1
    if token_state.get("dev_sold"):
        p -= 3
    if not token_state.get("tradeability_ok", True):
        p -= 3

    return p


def classify_alert_type(color: str, pair: dict, token_state: dict, holder_stats: dict, score: int, hard_red: bool) -> str:
    mint = token_state["mint"]

    if hard_red or color == "🔴 RED":
        if mint in HELD_TOKENS:
            return "RED-EXIT"
        return "IGNORE"

    if color != "🟡 GOLD":
        return "IGNORE"

    if live_confirmation_count(pair, token_state, holder_stats) < 3:
        return "IGNORE"

    if not can_send_buy_alert():
        return "IGNORE"

    priority = compute_priority(pair, token_state, holder_stats, score)
    if priority >= 8:
        return "GOLD-A"
    return "GOLD-B"

# =========================================================
# ALERT BUILD
# =========================================================

def build_alert(pair: dict, token_state: dict, holder_stats: dict, score: int, alert_type: str) -> str:
    mint = token_state["mint"]
    name = token_state.get("name") or (pair.get("baseToken") or {}).get("name") or mint[:6]
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    age_min = int(max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0))
    pair_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"

    top1 = holder_stats.get("top1_pct", 0.0) * 100
    top3 = holder_stats.get("top3_pct", 0.0) * 100

    source = token_state.get("source", "unknown")
    dev_sold = token_state.get("dev_sold", False)
    liq_lock = token_state.get("liq_lock_hint", False)
    tradeability_ok = token_state.get("tradeability_ok", True)
    migration_flag = token_state.get("migration_flag", False)

    if alert_type == "GOLD-A":
        header = f"🟡 GOLD-A | {name}"
        action = "Buy 50€"
    elif alert_type == "GOLD-B":
        header = f"🟡 GOLD-B | {name}"
        action = "Buy 25€"
    else:
        header = f"🔴 RED | {name}"
        action = "Sell"

    return (
        f"{header}\n"
        f"Score {score}/10 | Age {age_min}m | Source {source}\n"
        f"MC {compact_k(mc)} | Liq {compact_k(liq)} | First {compact_k(token_state.get('first_seen_mc', mc))} | Max {compact_k(token_state.get('max_seen_mc', mc))}\n"
        f"Top1 {top1:.1f}% | Top3 {top3:.1f}%\n"
        f"Flags: migration {'✅' if migration_flag else '❌'} | lock {'✅' if liq_lock else '❌'} | dev_sell {'✅' if dev_sold else '❌'} | trade {'✅' if tradeability_ok else '❌'}\n"
        f"Early: buys {token_state.get('early_buys', 0)} | uniq {len(token_state.get('early_unique_buyers', []))} | alpha {len(token_state.get('smart_wallet_hits', []))}\n"
        f"Action: {action}\n"
        f"Dex: <{pair_url}>"
    )

# =========================================================
# EVALUATION
# =========================================================

def evaluate_token(mint: str) -> None:
    token_state = STATE["tokens"].get(mint)
    if not token_state:
        return

    pair = get_best_pair(mint)
    if not pair:
        return

    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)

    vol = pair.get("volume") or {}
    v5 = to_float(vol.get("m5"), 0.0)
    v1 = to_float(vol.get("h1"), 0.0)
    vol24 = to_float(vol.get("h24"), 0.0)

    age_min = max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0)

    token_state["last_pair_url"] = pair.get("url") or ""
    token_state["last_seen_ts"] = now_ts()
    token_state["liq_lock_hint"] = liquidity_lock_hint(pair)
    token_state["tradeability_ok"] = anti_honeypot_guard(pair, token_state)

    if token_state["first_seen_mc"] <= 0 and mc > 0:
        token_state["first_seen_mc"] = mc
    token_state["max_seen_mc"] = max(token_state.get("max_seen_mc", 0.0), mc)

    if mc <= 0 or mc > MAX_MARKET_CAP:
        return
    if liq < MIN_LIQUIDITY:
        return
    if age_min < 1:
        return
    if vol24 > 0 and v5 > vol24 * 0.4:
        return
    if v1 > 0 and v5 > v1 * 0.6:
        return

    # Gecko only valid when fresh
    if token_state.get("source") == "gecko_new_pool" and age_min > 12:
        return

    holder_stats = get_holder_stats(mint)

    # Require holders sooner on larger caps
    if not holder_stats.get("enabled") and mc > 40_000:
        return

    hard_red = False
    if holder_stats.get("hard_reject", False):
        hard_red = True
    if fake_liquidity_risk(mc, liq):
        hard_red = True
    if no_chase_risk(token_state.get("first_seen_mc", 0.0), mc, age_min):
        hard_red = True
    if sniper_trap_risk(token_state):
        hard_red = True
    if token_state.get("dev_sold"):
        hard_red = True
    if not token_state.get("tradeability_ok", True):
        hard_red = True

    score = compute_score(pair, token_state, holder_stats)
    color = "🔴 RED" if (hard_red or score < GOLD_SCORE) else "🟡 GOLD"
    alert_type = classify_alert_type(color, pair, token_state, holder_stats, score, hard_red)

    if alert_type == "IGNORE":
        return

    alert_key = f"BUY:{mint}" if alert_type in {"GOLD-A", "GOLD-B"} else f"SELL:{mint}"
    if recently_alerted(alert_key):
        return

    msg = build_alert(pair, token_state, holder_stats, score, alert_type)
    send_discord(msg)
    mark_alerted(alert_key)

    name = token_state.get("name") or mint[:6]
    source = token_state.get("source", "unknown")
    dex_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"
    log_alert_csv(
        alert_type=alert_type,
        mint=mint,
        name=name,
        source=source,
        score=score,
        market_cap=mc,
        liquidity=liq,
        first_seen_mc=to_float(token_state.get("first_seen_mc", 0.0)),
        max_seen_mc=to_float(token_state.get("max_seen_mc", 0.0)),
        age_min=age_min,
        top1_pct=holder_stats.get("top1_pct", 0.0),
        top3_pct=holder_stats.get("top3_pct", 0.0),
        migration_flag=bool(token_state.get("migration_flag", False)),
        smart_wallet_count=len(token_state.get("smart_wallet_hits", [])),
        early_buys=int(token_state.get("early_buys", 0)),
        early_unique_buyers=len(token_state.get("early_unique_buyers", [])),
        dev_sold=bool(token_state.get("dev_sold", False)),
        tradeability_ok=bool(token_state.get("tradeability_ok", True)),
        dex_url=dex_url,
    )

    if alert_type in {"GOLD-A", "GOLD-B"}:
        mark_buy_alert_sent()

# =========================================================
# WS HELPERS
# =========================================================

async def subscribe(ws, method: str, keys: Optional[List[str]] = None):
    payload = {"method": method}
    if keys:
        payload["keys"] = keys
    await ws.send(json.dumps(payload))
    dbg("subscribed:", method, keys if keys else "")


async def subscribe_token_trade_once(ws, mint: str):
    if mint in SUBSCRIBED_TOKEN_TRADES:
        return
    await subscribe(ws, "subscribeTokenTrade", [mint])
    SUBSCRIBED_TOKEN_TRADES.add(mint)

# =========================================================
# WEBSOCKET LOOP
# =========================================================

async def websocket_loop():
    while True:
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, ping_timeout=20) as ws:
                dbg("connected to PumpPortal")

                await subscribe(ws, "subscribeNewToken")
                await subscribe(ws, "subscribeMigration")

                if SMART_WALLETS:
                    await subscribe(ws, "subscribeAccountTrade", list(SMART_WALLETS))

                while True:
                    raw = await ws.recv()
                    payload = extract_message_payload(raw)
                    if not payload:
                        continue

                    mint = extract_mint(payload)
                    event_text = json.dumps(payload).lower()

                    # New token creation
                    if mint and ("name" in payload and "symbol" in payload):
                        ensure_token(mint, extract_name(payload), extract_symbol(payload), "new_token")
                        await subscribe_token_trade_once(ws, mint)

                    # Migration event
                    if mint and "migration" in event_text:
                        ensure_token(mint, extract_name(payload), extract_symbol(payload), "migration")
                        await subscribe_token_trade_once(ws, mint)
                        continue

                    if not mint:
                        continue

                    # Trade events on subscribed tokens
                    side = extract_side(payload)
                    wallet = extract_wallet(payload)
                    usd_est = extract_amount_usd(payload)

                    if side:
                        update_trade_tracking(mint, wallet, side, usd_est)

                        if wallet and wallet in SMART_WALLETS:
                            add_smart_wallet_hit(mint, wallet)

        except Exception as e:
            print("websocket error, reconnecting:", e)
            await asyncio.sleep(5)

# =========================================================
# BACKGROUND LOOPS
# =========================================================

async def gecko_new_pools_loop():
    while True:
        try:
            pools = fetch_gecko_new_pools()
            for item in pools:
                ensure_token(
                    item["mint"],
                    item.get("name", ""),
                    item.get("symbol", ""),
                    item.get("source", "gecko_new_pool"),
                )
        except Exception as e:
            dbg("gecko loop error:", e)

        await asyncio.sleep(GECKO_REFRESH_SECONDS)


async def evaluator_loop():
    while True:
        try:
            cleanup_state()
            for mint in list(STATE.get("tokens", {}).keys()):
                evaluate_token(mint)
        except Exception as e:
            print("evaluator error:", e)

        await asyncio.sleep(EVALUATE_INTERVAL_SECONDS)


async def save_loop():
    while True:
        save_state()
        await asyncio.sleep(SAVE_INTERVAL_SECONDS)


async def heartbeat_loop():
    while True:
        try:
            now = now_ts()
            if now - int(STATE.get("last_heartbeat", 0)) >= HEARTBEAT_INTERVAL_SECONDS:
                tracked = len(STATE.get("tokens", {}))
                send_discord(f"🤖 SCANNER ACTIVE — tracked {tracked} tokens — no action signal")
                STATE["last_heartbeat"] = now
        except Exception as e:
            print("heartbeat error:", e)

        await asyncio.sleep(30)

# =========================================================
# MAIN
# =========================================================

async def main():
    load_state()
    cleanup_state()
    ensure_alert_log_file()
    dbg("smart wallets loaded:", len(SMART_WALLETS))
    dbg("held tokens loaded:", len(HELD_TOKENS))
    dbg("rpc enabled:", bool(SOLANA_RPC_URL))

    await asyncio.gather(
        websocket_loop(),
        gecko_new_pools_loop(),
        evaluator_loop(),
        save_loop(),
        heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
