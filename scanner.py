import asyncio
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import websockets

LAST_HEARTBEAT_TS = 0

# =========================================================
# CONFIG
# =========================================================

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "").strip()
PUMPPORTAL_API_KEY = os.environ.get("PUMPPORTAL_API_KEY", "").strip()

SMART_WALLETS = {
    x.strip() for x in os.environ.get("SMART_WALLETS", "").split(",") if x.strip()
}
HELD_TOKENS = {
    x.strip() for x in os.environ.get("HELD_TOKENS", "").split(",") if x.strip()
}

RUGCHECK_URL = os.environ.get("RUGCHECK_URL", "").strip()
GOPLUS_URL = os.environ.get("GOPLUS_URL", "").strip()
HONEYPOT_URL = os.environ.get("HONEYPOT_URL", "").strip()

PUMPPORTAL_WS = (
    f"wss://pumpportal.fun/api/data?api-key={PUMPPORTAL_API_KEY}"
    if PUMPPORTAL_API_KEY
    else "wss://pumpportal.fun/api/data"
)

DEX_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"

STATE_FILE = Path("state.json")
ALERT_LOG_FILE = Path("alerts_log.csv")
PAPER_LOG_FILE = Path("paper_trades_log.csv")
SHORTLIST_LOG_FILE = Path("shortlist_log.csv")

DEBUG = True

# cadence
HEARTBEAT_INTERVAL_SECONDS = 300
SAVE_INTERVAL_SECONDS = 30
EVALUATE_INTERVAL_SECONDS = 12
GECKO_REFRESH_SECONDS = 25
PAPER_CHECK_INTERVAL_SECONDS = 300

# retention
TOKEN_TTL_SECONDS = 3 * 3600
ALERT_COOLDOWN_SECONDS = 8 * 3600
SHORTLIST_COOLDOWN_SECONDS = 3 * 3600
BUY_ALERT_WINDOW_SECONDS = 20 * 60
MAX_BUY_ALERTS_PER_WINDOW = 2

# extreme mode
MAX_DISCOVERY_MC = 5_000_000
MIN_LIQUIDITY = 800
MIN_LIQ_TO_MC_RATIO = 0.18
WASH_RATIO_LIMIT = 45.0
NO_CHASE_MULTIPLIER = 2.2
MAX_TRACKED_TOKENS = 700
GECKO_MAX_ADD_PER_CYCLE = 150

GOLD_SCORE = 5
SHORTLIST_SCORE = 4

MIN_GOLD_MC = 8_000
MAX_GOLD_MC = 250_000

MIN_CONVICTION_MC = 20_000
MAX_CONVICTION_MC = 320_000

TOP1_HARD_REJECT = 0.35
TOP3_HARD_REJECT = 0.65
TOP1_SOFT_PENALTY = 0.18
TOP3_SOFT_PENALTY = 0.40

PAPER_GOLD_SIZE_EUR = 50
PAPER_WINNER_ROI = 2.0
PAPER_STOP_ROI = -0.35

LOCKER_KEYWORDS = ["locker", "locked", "burn", "null", "dead", "renounced"]

# wallet learning
WALLET_HUNTER_X100_ROI = 100.0
WALLET_HUNTER_FALLBACK_ROI = 10.0
WALLET_HUNTER_FALLBACK_WINS = 1

# microstructure
BURST_WINDOW_SECONDS = 30
CLUSTER_WINDOW_SECONDS = 60
ALPHA_CLUSTER_WINDOW_SECONDS = 60
PREMIGRATION_WINDOW_SECONDS = 180
ELITE_PREBUY_WINDOW_SECONDS = 120

BURST_MIN_BUYS = 5
CLUSTER_MIN_UNIQ = 4
ALPHA_CLUSTER_MIN = 2

LIQUIDITY_ADD_MIN_USD = 5000
LIQUIDITY_ADD_RATIO = 1.8
DEV_ACCUM_MIN_BUYS = 3
ELITE_PREBUY_MIN_USD = 150.0

GRAD_PREDICT_WINDOW_SECONDS = 300
GRAD_PREDICT_MIN_BUYS = 8
GRAD_PREDICT_MIN_UNIQ = 6
GRAD_PREDICT_MIN_VOL = 1200.0
GRAD_PREDICT_MIN_LIQ = 12000.0
GRAD_PREDICT_MAX_MC = 180000.0

EARLY_LIQ_MIGRATION_WINDOW_SECONDS = 240
EARLY_LIQ_MIGRATION_MIN_DELTA = 8000.0
EARLY_LIQ_MIGRATION_MIN_RATIO = 1.8

WATCHERS_MIN_ABS = 50
WATCHERS_MULTIPLIER = 3.0

# creator reputation
DEV_REPUTATION_WIN_MULTIPLE = 10.0
DEV_REPUTATION_MIN_WINS = 1


# =========================================================
# STATE
# =========================================================

STATE: Dict[str, Any] = {
    "tokens": {},
    "alerted": {},
    "last_heartbeat": 0,
    "buy_alert_history": [],
    "paper_positions": {},
    "wallet_stats": {},
    "dev_wallet_stats": {},
    "alpha_discovered_wallets": [],
    "x100_discovered_wallets": [],
    "cycle_raw_seen": 0,
    "cycle_discovery_rejected": 0,
    "cycle_tracked_added": 0,
    "cycle_evaluated_tokens": 0,
    "cycle_deep_rejected": 0,
}

SUBSCRIBED_TOKEN_TRADES: Set[str] = set()
SUBSCRIBED_ACCOUNT_WALLETS: Set[str] = set()


# =========================================================
# BASICS
# =========================================================

def dbg(*args):
    if DEBUG:
        print(*args, flush=True)


def now_ts() -> int:
    return int(time.time())


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v in (None, ""):
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


def send_discord(msg: str) -> None:
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
    except Exception as e:
        print("discord send error:", e)


def load_state() -> None:
    global STATE
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            STATE = data
    except Exception as e:
        print("state load error:", e)

    for k, default in {
        "tokens": {},
        "alerted": {},
        "last_heartbeat": 0,
        "buy_alert_history": [],
        "paper_positions": {},
        "wallet_stats": {},
        "dev_wallet_stats": {},
        "alpha_discovered_wallets": [],
        "x100_discovered_wallets": [],
        "cycle_raw_seen": 0,
        "cycle_discovery_rejected": 0,
        "cycle_tracked_added": 0,
        "cycle_evaluated_tokens": 0,
        "cycle_deep_rejected": 0,
    }.items():
        STATE.setdefault(k, default)


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
        max_age = SHORTLIST_COOLDOWN_SECONDS if key.startswith("SHORTLIST:") else 7 * 24 * 3600
        if now - int(ts) < max_age:
            keep_alerted[key] = ts
    STATE["alerted"] = keep_alerted

    keep_tokens = {}
    for mint, rec in STATE.get("tokens", {}).items():
        if now - int(rec.get("last_seen_ts", 0)) < TOKEN_TTL_SECONDS:
            keep_tokens[mint] = rec
    ordered = sorted(
        keep_tokens.items(),
        key=lambda kv: int(kv[1].get("last_seen_ts", 0)),
        reverse=True,
    )
    STATE["tokens"] = dict(ordered[:MAX_TRACKED_TOKENS])

    STATE["buy_alert_history"] = [
        int(ts) for ts in STATE.get("buy_alert_history", [])
        if now - int(ts) < BUY_ALERT_WINDOW_SECONDS
    ]


def recently_alerted(alert_key: str) -> bool:
    ts = int(STATE.get("alerted", {}).get(alert_key, 0))
    if alert_key.startswith("SHORTLIST:"):
        return (now_ts() - ts) < SHORTLIST_COOLDOWN_SECONDS
    return (now_ts() - ts) < ALERT_COOLDOWN_SECONDS


def mark_alerted(alert_key: str) -> None:
    STATE["alerted"][alert_key] = now_ts()


def can_send_buy_alert() -> bool:
    cleanup_state()
    return len(STATE.get("buy_alert_history", [])) < MAX_BUY_ALERTS_PER_WINDOW


def mark_buy_alert_sent() -> None:
    STATE.setdefault("buy_alert_history", []).append(now_ts())


# =========================================================
# FILES
# =========================================================

def ensure_csv(path: Path, header: List[str]) -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(header)


def ensure_files() -> None:
    ensure_csv(
        ALERT_LOG_FILE,
        [
            "timestamp", "alert_type", "mint", "name", "source", "score",
            "market_cap", "liquidity", "first_seen_mc", "max_seen_mc", "age_min",
            "top1_pct", "top3_pct", "migration_flag", "alpha_hits", "x100_hits",
            "elite_prebuy_hits", "early_liq_migration", "grad_predictor",
            "alpha_cluster", "creator_rep", "watchers_velocity",
            "early_buys", "early_unique_buyers", "dev_sold",
            "tradeability_ok", "risk_ok", "dex_url"
        ],
    )
    ensure_csv(
        PAPER_LOG_FILE,
        [
            "timestamp", "event", "mint", "name", "paper_signal",
            "size_eur", "entry_mc", "current_mc", "roi_pct", "source", "status"
        ],
    )
    ensure_csv(
        SHORTLIST_LOG_FILE,
        [
            "timestamp", "mint", "name", "score", "market_cap", "liquidity",
            "source", "reason_1", "reason_2", "reason_3", "dex_url"
        ],
    )


# =========================================================
# TOKEN MODEL
# =========================================================

def ensure_token(mint: str, name: str = "", symbol: str = "", source: str = "unknown") -> dict:
    rec = STATE["tokens"].get(mint)
    if rec:
        if name and not rec.get("name"):
            rec["name"] = name
        if symbol and not rec.get("symbol"):
            rec["symbol"] = symbol
        if source and rec.get("source") == "unknown":
            rec["source"] = source
        if source == "migration":
            rec["migration_flag"] = True
        rec["last_seen_ts"] = now_ts()
        return rec

    rec = {
        "mint": mint,
        "name": name or mint[:6],
        "symbol": symbol or "",
        "source": source,
        "first_seen_ts": now_ts(),
        "launch_seen_ts": now_ts(),
        "last_seen_ts": now_ts(),
        "first_seen_mc": 0.0,
        "max_seen_mc": 0.0,
        "last_pair_url": "",
        "last_liquidity_usd": 0.0,
        "last_liq_change_ts": 0,
        "smart_wallet_hits": [],
        "x100_wallet_hits": [],
        "elite_prebuy_hits": [],
        "buy_wallet_counts": {},
        "sell_wallet_counts": {},
        "first_buy_wallets": [],
        "first_buy_ts": 0,
        "candidate_dev_wallet": None,
        "dev_sold": False,
        "tradeability_ok": True,
        "risk_ok": True,
        "lp_safe": False,
        "lp_risk": False,
        "liq_lock_hint": False,
        "migration_flag": source == "migration",
        "early_buys": 0,
        "early_sells": 0,
        "early_unique_buyers": [],
        "early_unique_sellers": [],
        "early_volume_est": 0.0,
        "buy_events": [],
        "sell_events": [],
        "liq_history": [],
        "alpha_hit_events": [],
        "watchers_history": [],
    }
    STATE["tokens"][mint] = rec
    return rec


def learned_alpha_wallets() -> Set[str]:
    return set(STATE.get("alpha_discovered_wallets", []))


def learned_x100_wallets() -> Set[str]:
    return set(STATE.get("x100_discovered_wallets", []))


def add_alpha_hit(mint: str, wallet: str) -> None:
    rec = ensure_token(mint)
    hits = set(rec.get("smart_wallet_hits", []))
    hits.add(wallet)
    rec["smart_wallet_hits"] = list(hits)
    rec.setdefault("alpha_hit_events", []).append({"wallet": wallet, "ts": now_ts()})


def add_x100_hit(mint: str, wallet: str) -> None:
    rec = ensure_token(mint)
    hits = set(rec.get("x100_wallet_hits", []))
    hits.add(wallet)
    rec["x100_wallet_hits"] = list(hits)


def add_elite_prebuy_hit(mint: str, wallet: str) -> None:
    rec = ensure_token(mint)
    hits = set(rec.get("elite_prebuy_hits", []))
    hits.add(wallet)
    rec["elite_prebuy_hits"] = list(hits)


def prune_old_events(token_state: dict) -> None:
    now = now_ts()
    for key, ttl in [
        ("buy_events", 180),
        ("sell_events", 180),
        ("liq_history", 300),
        ("alpha_hit_events", 300),
        ("watchers_history", 600),
    ]:
        token_state[key] = [
            e for e in token_state.get(key, [])
            if now - int(e.get("ts", 0)) <= ttl
        ]


# =========================================================
# WALLET LEARNING
# =========================================================

def update_wallet_stats_from_trade(wallet: str) -> None:
    if not wallet:
        return
    stats = STATE["wallet_stats"].setdefault(
        wallet,
        {"wins": 0, "trades": 0, "high_roi_wins": 0, "x100_wins": 0},
    )
    stats["trades"] = int(stats.get("trades", 0)) + 1


def update_wallet_stats_from_winner(wallet: str, roi_multiple: float) -> None:
    if not wallet:
        return

    stats = STATE["wallet_stats"].setdefault(
        wallet,
        {"wins": 0, "trades": 0, "high_roi_wins": 0, "x100_wins": 0},
    )
    stats["wins"] = int(stats.get("wins", 0)) + 1

    if roi_multiple >= WALLET_HUNTER_FALLBACK_ROI:
        stats["high_roi_wins"] = int(stats.get("high_roi_wins", 0)) + 1
    if roi_multiple >= WALLET_HUNTER_X100_ROI:
        stats["x100_wins"] = int(stats.get("x100_wins", 0)) + 1

    trades = int(stats.get("trades", 0))
    wins = int(stats.get("wins", 0))
    high_roi_wins = int(stats.get("high_roi_wins", 0))
    x100_wins = int(stats.get("x100_wins", 0))

    if trades >= 3 and (wins / max(trades, 1)) >= 0.40:
        alpha = set(STATE.get("alpha_discovered_wallets", []))
        alpha.add(wallet)
        STATE["alpha_discovered_wallets"] = list(alpha)

    if x100_wins >= 1 or high_roi_wins >= WALLET_HUNTER_FALLBACK_WINS:
        elite = set(STATE.get("x100_discovered_wallets", []))
        elite.add(wallet)
        STATE["x100_discovered_wallets"] = list(elite)
        
def update_dev_wallet_reputation(token_state: dict, current_mc: float) -> None:
    dev = token_state.get("candidate_dev_wallet")
    if not dev:
        return

    stats = STATE.setdefault("dev_wallet_stats", {}).setdefault(
        dev,
        {"tokens": 0, "wins": 0, "last_tokens": []},
    )

    if token_state["mint"] not in stats["last_tokens"]:
        stats["tokens"] += 1
        stats["last_tokens"].append(token_state["mint"])
        stats["last_tokens"] = stats["last_tokens"][-50:]

    entry = to_float(token_state.get("first_seen_mc", 0.0), 0.0)
    if entry > 0 and current_mc >= entry * DEV_REPUTATION_WIN_MULTIPLE:
        win_mark_key = f"win::{token_state['mint']}"
        wins_seen = set(stats.get("wins_seen", []))
        if win_mark_key not in wins_seen:
            stats["wins"] = int(stats.get("wins", 0)) + 1
            wins_seen.add(win_mark_key)
            stats["wins_seen"] = list(wins_seen)


def creator_reputation_signal(token_state: dict) -> bool:
    dev = token_state.get("candidate_dev_wallet")
    if not dev:
        return False
    stats = STATE.get("dev_wallet_stats", {}).get(dev, {})
    return int(stats.get("wins", 0)) >= DEV_REPUTATION_MIN_WINS


# =========================================================
# RPC / HOLDERS
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
            "enabled": False,
            "top1_pct": 0.0,
            "top3_pct": 0.0,
            "hard_reject": False,
            "soft_penalty": False,
        }

    supply = get_token_supply(mint)
    largest = get_token_largest_accounts(mint)

    if supply <= 0 or not largest:
        return {
            "enabled": False,
            "top1_pct": 0.0,
            "top3_pct": 0.0,
            "hard_reject": False,
            "soft_penalty": False,
        }

    amounts = [to_float(x.get("uiAmount"), 0.0) for x in largest[:10]]
    top1_pct = amounts[0] / supply if amounts else 0.0
    top3_pct = sum(amounts[:3]) / supply if supply > 0 else 0.0

    return {
        "enabled": True,
        "top1_pct": top1_pct,
        "top3_pct": top3_pct,
        "hard_reject": top1_pct > TOP1_HARD_REJECT or top3_pct > TOP3_HARD_REJECT,
        "soft_penalty": top1_pct > TOP1_SOFT_PENALTY or top3_pct > TOP3_SOFT_PENALTY,
    }


# =========================================================
# OPTIONAL RISK
# =========================================================

def fetch_optional_json(url: str, mint: str) -> Optional[dict]:
    if not url:
        return None
    try:
        r = requests.get(url.format(mint=mint), timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def optional_risk_check(mint: str) -> Tuple[bool, Dict[str, bool], List[str]]:
    notes: List[str] = []
    signals = {
        "lp_safe": False,
        "lp_risk": False,
        "owner_risk": False,
        "mint_risk": False,
    }

    rc = fetch_optional_json(RUGCHECK_URL, mint)
    gp = fetch_optional_json(GOPLUS_URL, mint)
    hp = fetch_optional_json(HONEYPOT_URL, mint)

    for label, data in [("rugcheck", rc), ("goplus", gp), ("honeypot", hp)]:
        if not data:
            continue
        txt = json.dumps(data).lower()

        if any(x in txt for x in ["honeypot", "cannot sell", "blacklist", "malicious", "rug", "scam"]):
            notes.append(f"{label} risk")
            signals["owner_risk"] = True

        if any(x in txt for x in ["lp locked", "liquidity locked", "locked liquidity", "burned lp"]):
            signals["lp_safe"] = True

        if any(x in txt for x in ["lp unlocked", "unlocked liquidity", "liquidity not locked"]):
            signals["lp_risk"] = True
            notes.append(f"{label} lp risk")

        if any(x in txt for x in ["mintable", "can mint", "owner can mint"]):
            signals["mint_risk"] = True
            notes.append(f"{label} mint risk")

    risk_ok = not (signals["owner_risk"] or signals["lp_risk"] or signals["mint_risk"])
    return risk_ok, signals, notes


# =========================================================
# MARKET SOURCES
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
            key=lambda x: (
                to_float((x.get("liquidity") or {}).get("usd"), 0.0),
                to_float((x.get("volume") or {}).get("m5"), 0.0),
            ),
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
        inc_map = {(obj.get("type"), obj.get("id")): obj for obj in included}

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
    return msg.get("mint") or msg.get("token") or msg.get("tokenAddress") or msg.get("baseTokenAddress")


def extract_name(msg: dict) -> str:
    return msg.get("name") or msg.get("tokenName") or ""


def extract_symbol(msg: dict) -> str:
    return msg.get("symbol") or msg.get("tokenSymbol") or ""


def extract_wallet(msg: dict) -> Optional[str]:
    return msg.get("traderPublicKey") or msg.get("wallet") or msg.get("account") or msg.get("maker")


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
    ts = now_ts()

    if side == "buy":
        if rec["first_buy_ts"] == 0:
            rec["first_buy_ts"] = ts

        rec["early_buys"] = int(rec.get("early_buys", 0)) + 1
        rec["early_volume_est"] = to_float(rec.get("early_volume_est", 0.0)) + usd_est

        counts = rec["buy_wallet_counts"]
        counts[wallet] = counts.get(wallet, 0) + 1

        buyers = set(rec.get("early_unique_buyers", []))
        buyers.add(wallet)
        rec["early_unique_buyers"] = list(buyers)

        if len(rec["first_buy_wallets"]) < 16 and wallet not in rec["first_buy_wallets"]:
            rec["first_buy_wallets"].append(wallet)

        if rec.get("candidate_dev_wallet") is None and len(rec["first_buy_wallets"]) <= 2:
            rec["candidate_dev_wallet"] = wallet

        rec["buy_events"].append({"ts": ts, "wallet": wallet, "usd": usd_est})

        token_age = ts - int(rec.get("launch_seen_ts", ts))
        if (
            wallet in learned_x100_wallets()
            and token_age <= ELITE_PREBUY_WINDOW_SECONDS
            and usd_est >= ELITE_PREBUY_MIN_USD
            and not rec.get("migration_flag", False)
        ):
            add_elite_prebuy_hit(mint, wallet)

    else:
        rec["early_sells"] = int(rec.get("early_sells", 0)) + 1

        sell_counts = rec["sell_wallet_counts"]
        sell_counts[wallet] = sell_counts.get(wallet, 0) + 1

        sellers = set(rec.get("early_unique_sellers", []))
        sellers.add(wallet)
        rec["early_unique_sellers"] = list(sellers)

        rec["sell_events"].append({"ts": ts, "wallet": wallet, "usd": usd_est})

        if wallet == rec.get("candidate_dev_wallet"):
            rec["dev_sold"] = True

    prune_old_events(rec)
    update_wallet_stats_from_trade(wallet)


# =========================================================
# SIGNALS / RISKS
# =========================================================

def volume_burst_signal(token_state: dict) -> bool:
    prune_old_events(token_state)
    now = now_ts()
    recent = [e for e in token_state.get("buy_events", []) if now - int(e.get("ts", 0)) <= BURST_WINDOW_SECONDS]
    return len(recent) >= BURST_MIN_BUYS


def wallet_cluster_signal(token_state: dict) -> bool:
    prune_old_events(token_state)
    now = now_ts()
    recent = [e for e in token_state.get("buy_events", []) if now - int(e.get("ts", 0)) <= CLUSTER_WINDOW_SECONDS]
    uniq = {e.get("wallet") for e in recent if e.get("wallet")}
    return len(uniq) >= CLUSTER_MIN_UNIQ


def alpha_wallet_cluster_signal(token_state: dict) -> bool:
    prune_old_events(token_state)
    now = now_ts()
    recent = [e for e in token_state.get("alpha_hit_events", []) if now - int(e.get("ts", 0)) <= ALPHA_CLUSTER_WINDOW_SECONDS]
    uniq = {e.get("wallet") for e in recent if e.get("wallet")}
    return len(uniq) >= ALPHA_CLUSTER_MIN


def liquidity_add_signal(token_state: dict) -> bool:
    prune_old_events(token_state)
    hist = token_state.get("liq_history", [])
    if len(hist) < 2:
        return False
    old_liq = to_float(hist[0].get("liq"), 0.0)
    new_liq = to_float(hist[-1].get("liq"), 0.0)
    if old_liq <= 0 or new_liq <= 0:
        return False
    delta = new_liq - old_liq
    ratio = new_liq / old_liq if old_liq > 0 else 0.0
    return delta >= LIQUIDITY_ADD_MIN_USD and ratio >= LIQUIDITY_ADD_RATIO


def early_liquidity_migration_detector(token_state: dict) -> bool:
    prune_old_events(token_state)
    age = now_ts() - int(token_state.get("launch_seen_ts", token_state.get("first_seen_ts", now_ts())))
    if age > EARLY_LIQ_MIGRATION_WINDOW_SECONDS:
        return False

    hist = token_state.get("liq_history", [])
    if len(hist) < 2:
        return False

    old_liq = to_float(hist[0].get("liq"), 0.0)
    new_liq = to_float(hist[-1].get("liq"), 0.0)
    if old_liq <= 0 or new_liq <= 0:
        return False

    delta = new_liq - old_liq
    ratio = new_liq / old_liq if old_liq > 0 else 0.0

    return (
        delta >= EARLY_LIQ_MIGRATION_MIN_DELTA
        and ratio >= EARLY_LIQ_MIGRATION_MIN_RATIO
        and (
            volume_burst_signal(token_state)
            or wallet_cluster_signal(token_state)
            or alpha_wallet_cluster_signal(token_state)
            or len(token_state.get("x100_wallet_hits", [])) >= 1
            or len(token_state.get("elite_prebuy_hits", [])) >= 1
        )
    )


def dev_accumulation_signal(token_state: dict) -> bool:
    counts = token_state.get("buy_wallet_counts", {})
    return bool(counts) and any(c >= DEV_ACCUM_MIN_BUYS for c in counts.values())


def elite_prebuy_signal(token_state: dict) -> bool:
    age = now_ts() - int(token_state.get("launch_seen_ts", token_state.get("first_seen_ts", now_ts())))
    elite_hits = len(token_state.get("elite_prebuy_hits", []))
    return age <= ELITE_PREBUY_WINDOW_SECONDS and elite_hits >= 1 and not token_state.get("migration_flag", False)


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


def dev_supply_proxy_risk(token_state: dict) -> bool:
    counts = token_state.get("buy_wallet_counts", {})
    dev_wallet = token_state.get("candidate_dev_wallet")
    if not dev_wallet or dev_wallet not in counts:
        return False

    dev_buys = counts.get(dev_wallet, 0)
    total_buys = max(1, sum(counts.values()))
    share = dev_buys / total_buys

    if dev_buys >= 4:
        return True
    if total_buys >= 5 and share >= 0.5:
        return True
    return False


def fake_liquidity_risk(mc: float, liq: float) -> bool:
    return mc <= 0 or liq < mc * MIN_LIQ_TO_MC_RATIO


def wash_trading_risk(v24: float, liq: float) -> bool:
    return liq > 0 and (v24 / liq) > WASH_RATIO_LIMIT


def no_chase_risk(first_seen_mc: float, current_mc: float, age_min: float) -> bool:
    if first_seen_mc <= 0 or age_min <= 10:
        return False
    return current_mc > first_seen_mc * NO_CHASE_MULTIPLIER


def liquidity_lock_hint(pair: dict) -> bool:
    text = json.dumps(pair).lower()
    return any(k in text for k in LOCKER_KEYWORDS)


def anti_honeypot_guard(pair: dict) -> bool:
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    txs = pair.get("txns") or {}
    m5 = txs.get("m5") or {}
    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))
    if liq > 10_000 and buys == 0 and sells == 0:
        return False
    return True


def holder_explosion_signal(token_state: dict) -> bool:
    uniq = len(token_state.get("early_unique_buyers", []))
    age_min = max(0.0, (now_ts() - int(token_state.get("first_seen_ts", now_ts()))) / 60.0)
    return uniq >= 6 and age_min <= 10


def early_pump_signal(token_state: dict) -> bool:
    buys = int(token_state.get("early_buys", 0))
    uniq = len(token_state.get("early_unique_buyers", []))
    vol = to_float(token_state.get("early_volume_est", 0.0), 0.0)
    return buys >= 4 and uniq >= 3 and vol >= 400


def watchers_velocity_signal(token_state: dict) -> bool:
    hist = token_state.get("watchers_history", [])
    if len(hist) < 2:
        return False
    recent = hist[-5:]
    start = int(recent[0].get("value", 0))
    end = int(recent[-1].get("value", 0))
    return end >= WATCHERS_MIN_ABS and end > max(1, start) * WATCHERS_MULTIPLIER


def pump_fun_graduation_predictor(token_state: dict, pair: dict) -> bool:
    age = now_ts() - int(token_state.get("launch_seen_ts", token_state.get("first_seen_ts", now_ts())))
    if age > GRAD_PREDICT_WINDOW_SECONDS or token_state.get("migration_flag", False):
        return False

    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    if mc <= 0 or liq <= 0 or liq < GRAD_PREDICT_MIN_LIQ or mc > GRAD_PREDICT_MAX_MC:
        return False

    early_buys = int(token_state.get("early_buys", 0))
    uniq_buyers = len(token_state.get("early_unique_buyers", []))
    early_vol = to_float(token_state.get("early_volume_est", 0.0), 0.0)

    if early_buys < GRAD_PREDICT_MIN_BUYS or uniq_buyers < GRAD_PREDICT_MIN_UNIQ or early_vol < GRAD_PREDICT_MIN_VOL:
        return False

    txs = pair.get("txns") or {}
    m5 = txs.get("m5") or {}
    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))

    return (
        buys >= sells
        and not sniper_trap_risk(token_state)
        and not dev_supply_proxy_risk(token_state)
        and not token_state.get("dev_sold", False)
    )


def pre_migration_x50_signal(token_state: dict, pair: dict) -> bool:
    age = now_ts() - int(token_state.get("launch_seen_ts", token_state.get("first_seen_ts", now_ts())))
    return (
        age <= PREMIGRATION_WINDOW_SECONDS
        and not token_state.get("migration_flag", False)
        and (
            elite_prebuy_signal(token_state)
            or early_liquidity_migration_detector(token_state)
            or pump_fun_graduation_predictor(token_state, pair)
            or volume_burst_signal(token_state)
            or wallet_cluster_signal(token_state)
            or alpha_wallet_cluster_signal(token_state)
            or dev_accumulation_signal(token_state)
            or len(token_state.get("x100_wallet_hits", [])) >= 1
        )
    )


def cult_meme_proxy_signal(token_state: dict, pair: dict, holder_stats: dict) -> bool:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    uniq_buyers = len(token_state.get("early_unique_buyers", []))
    age_min = max(0.0, (now_ts() - int(token_state.get("first_seen_ts", now_ts()))) / 60.0)
    if mc <= 0 or liq <= 0:
        return False

    ratio = liq / mc
    return (
        20_000 <= mc <= 240_000
        and ratio >= 0.40
        and uniq_buyers >= 6
        and age_min <= 30
        and not token_state.get("dev_sold", False)
        and not holder_stats.get("hard_reject", False)
        and not sniper_trap_risk(token_state)
        and not dev_supply_proxy_risk(token_state)
    )


def swing_conviction_signal(token_state: dict, pair: dict, holder_stats: dict) -> bool:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    if mc <= 0 or liq <= 0:
        return False

    ratio = liq / mc
    uniq_buyers = len(token_state.get("early_unique_buyers", []))
    x100_hits = len(token_state.get("x100_wallet_hits", []))

    return (
        MIN_CONVICTION_MC <= mc <= MAX_CONVICTION_MC
        and ratio >= 0.45
        and uniq_buyers >= 5
        and not holder_stats.get("hard_reject", False)
        and not token_state.get("lp_risk", False)
        and not dev_supply_proxy_risk(token_state)
        and not token_state.get("dev_sold", False)
        and (
            x100_hits >= 1
            or cult_meme_proxy_signal(token_state, pair, holder_stats)
            or liquidity_add_signal(token_state)
            or alpha_wallet_cluster_signal(token_state)
            or creator_reputation_signal(token_state)
            or watchers_velocity_signal(token_state)
        )
    )


def fast_roi_signal(token_state: dict, pair: dict, holder_stats: dict) -> bool:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    if mc <= 0 or liq <= 0:
        return False

    ratio = liq / mc
    return (
        8_000 <= mc <= 150_000
        and ratio >= 0.30
        and not holder_stats.get("hard_reject", False)
        and not token_state.get("dev_sold", False)
        and not token_state.get("lp_risk", False)
        and not dev_supply_proxy_risk(token_state)
        and (
            elite_prebuy_signal(token_state)
            or early_liquidity_migration_detector(token_state)
            or pump_fun_graduation_predictor(token_state, pair)
            or alpha_wallet_cluster_signal(token_state)
            or creator_reputation_signal(token_state)
            or watchers_velocity_signal(token_state)
            or (
                len(token_state.get("x100_wallet_hits", [])) >= 1
                and (volume_burst_signal(token_state) or liquidity_add_signal(token_state))
            )
            or (
                pre_migration_x50_signal(token_state, pair)
                and wallet_cluster_signal(token_state)
            )
        )
    )


def x100_wallet_hunter_signal(token_state: dict) -> bool:
    return len(token_state.get("x100_wallet_hits", [])) >= 1


def momentum_only_signal(token_state: dict) -> bool:
    return (
        not volume_burst_signal(token_state)
        and not wallet_cluster_signal(token_state)
        and not alpha_wallet_cluster_signal(token_state)
        and not liquidity_add_signal(token_state)
        and not early_liquidity_migration_detector(token_state)
        and not early_pump_signal(token_state)
        and not holder_explosion_signal(token_state)
        and not creator_reputation_signal(token_state)
        and not watchers_velocity_signal(token_state)
        and len(token_state.get("smart_wallet_hits", [])) == 0
        and len(token_state.get("x100_wallet_hits", [])) == 0
        and len(token_state.get("elite_prebuy_hits", [])) == 0
        and not dev_accumulation_signal(token_state)
        and not token_state.get("migration_flag", False)
    )


def has_real_action_signal(token_state: dict, pair: dict, holder_stats: dict) -> bool:
    paths = 0
    if fast_roi_signal(token_state, pair, holder_stats):
        paths += 1
    if swing_conviction_signal(token_state, pair, holder_stats):
        paths += 1
    if x100_wallet_hunter_signal(token_state):
        paths += 1
    if cult_meme_proxy_signal(token_state, pair, holder_stats):
        paths += 1
    return paths >= 1


def liquidity_quality_tier(mc: float, liq: float) -> str:
    if mc <= 0:
        return "bad"
    ratio = liq / mc
    if ratio >= 0.8:
        return "strong"
    if ratio >= 0.4:
        return "ok"
    return "weak"


def live_confirmation_count(pair: dict, token_state: dict, holder_stats: dict) -> int:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    txs = pair.get("txns") or {}
    m5 = txs.get("m5") or {}
    buys = int(m5.get("buys", 0))
    sells = int(m5.get("sells", 0))
    total = buys + sells

    count = 0
    if mc > 0 and liq >= mc * 0.15:
        count += 1
    if total >= 2 and buys >= sells:
        count += 1
    if len(token_state.get("smart_wallet_hits", [])) >= 1:
        count += 1
    if len(token_state.get("x100_wallet_hits", [])) >= 1:
        count += 2
    if len(token_state.get("elite_prebuy_hits", [])) >= 1:
        count += 3
    if alpha_wallet_cluster_signal(token_state):
        count += 3
    if creator_reputation_signal(token_state):
        count += 2
    if watchers_velocity_signal(token_state):
        count += 2
    if early_liquidity_migration_detector(token_state):
        count += 2
    if pump_fun_graduation_predictor(token_state, pair):
        count += 2
    if token_state.get("migration_flag", False):
        count += 1
    if holder_stats.get("enabled"):
        count += 1
    if early_pump_signal(token_state):
        count += 1
    if holder_explosion_signal(token_state):
        count += 1
    if volume_burst_signal(token_state):
        count += 2
    if wallet_cluster_signal(token_state):
        count += 2
    if liquidity_add_signal(token_state):
        count += 2
    if dev_accumulation_signal(token_state):
        count += 1
    if pre_migration_x50_signal(token_state, pair):
        count += 2
    if fast_roi_signal(token_state, pair, holder_stats):
        count += 2
    if swing_conviction_signal(token_state, pair, holder_stats):
        count += 2
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
    liq_tier = liquidity_quality_tier(mc, liq)

    score = 0

    if 8_000 <= mc < 25_000:
        score += 3
    elif 25_000 <= mc < 60_000:
        score += 3
    elif 60_000 <= mc <= 120_000:
        score += 2
    elif 120_000 < mc <= 250_000:
        score += 1

    if liq_tier == "strong":
        score += 3
    elif liq_tier == "ok":
        score += 1
    else:
        score -= 3

    if buys > sells:
        score += 1
    if buy_ratio > 0.55:
        score += 1
    if buys >= 3:
        score += 1
    if age_min <= 25:
        score += 1
    if v1 > 0 and v5 * 12 > v1 * 0.08 and v5 > 700:
        score += 1

    elif v5 > 1200:
        score += 1

    if early_pump_signal(token_state):
        score += 1
    if holder_explosion_signal(token_state):
        score += 2
    if volume_burst_signal(token_state):
        score += 2
    if wallet_cluster_signal(token_state):
        score += 2
    if alpha_wallet_cluster_signal(token_state):
        score += 4
    if creator_reputation_signal(token_state):
        score += 3
    if watchers_velocity_signal(token_state):
        score += 2
    if liquidity_add_signal(token_state):
        score += 3
    if early_liquidity_migration_detector(token_state):
        score += 3
    if pump_fun_graduation_predictor(token_state, pair):
        score += 3
    if dev_accumulation_signal(token_state):
        score += 1
    if pre_migration_x50_signal(token_state, pair):
        score += 3
    if elite_prebuy_signal(token_state):
        score += 4

    alpha_hits = len(token_state.get("smart_wallet_hits", []))
    x100_hits = len(token_state.get("x100_wallet_hits", []))

    if alpha_hits >= 1:
        score += 2
    if alpha_hits >= 2:
        score += 4
    if alpha_hits >= 3:
        score += 6
    if x100_hits >= 1:
        score += 4

    if cult_meme_proxy_signal(token_state, pair, holder_stats):
        score += 2
    if swing_conviction_signal(token_state, pair, holder_stats):
        score += 3
    if fast_roi_signal(token_state, pair, holder_stats):
        score += 3

    if token_state.get("migration_flag", False):
        score += 1
    if token_state.get("lp_safe", False):
        score += 1

    if holder_stats.get("soft_penalty", False):
        score -= 3
    if sniper_trap_risk(token_state):
        score -= 4
    if dev_supply_proxy_risk(token_state):
        score -= 5
    if wash_trading_risk(v24, liq):
        score -= 3
    if token_state.get("dev_sold", False):
        score -= 6
    if token_state.get("lp_risk", False):
        score -= 6
    if not token_state.get("tradeability_ok", True):
        score -= 6
    if not token_state.get("risk_ok", True):
        score -= 6
    if momentum_only_signal(token_state):
        score -= 4

    return max(0, min(10, score))


# =========================================================
# PAPER
# =========================================================

def open_paper_position(mint: str, token_state: dict, pair: dict, alert_type: str) -> None:
    if alert_type != "GOLD":
        return
    if mint in STATE.get("paper_positions", {}):
        return

    size = PAPER_GOLD_SIZE_EUR
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    name = token_state.get("name") or mint[:6]
    source = token_state.get("source", "unknown")

    STATE.setdefault("paper_positions", {})[mint] = {
        "mint": mint,
        "name": name,
        "signal": alert_type,
        "size_eur": size,
        "entry_mc": mc,
        "opened_ts": now_ts(),
        "status": "OPEN",
        "source": source,
        "winner_notified": False,
        "stop_notified": False,
    }

    with PAPER_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [now_ts(), "OPEN", mint, name, alert_type, size, mc, mc, 0.0, source, "OPEN"]
        )


def process_paper_winner(mint: str, current_mc: float) -> None:
    pos = STATE.get("paper_positions", {}).get(mint)
    if not pos or pos.get("status") != "OPEN":
        return

    entry_mc = to_float(pos.get("entry_mc", 0.0))
    if entry_mc <= 0:
        return

    roi = (current_mc - entry_mc) / entry_mc
    if roi < PAPER_WINNER_ROI or pos.get("winner_notified", False):
        return

    pos["winner_notified"] = True
    name = pos.get("name", mint[:6])

    send_discord(
        f"🚀 PAPER WINNER | {name}\nROI +{roi*100:.0f}%\nMC {compact_k(entry_mc)} → {compact_k(current_mc)}"
    )

    token_state = STATE.get("tokens", {}).get(mint, {})
    roi_multiple = current_mc / entry_mc if entry_mc > 0 else 0.0

    for wallet in token_state.get("first_buy_wallets", []):
        update_wallet_stats_from_winner(wallet, roi_multiple)

    with PAPER_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [now_ts(), "WINNER", mint, name, pos.get("signal", ""), pos.get("size_eur", 0.0),
             entry_mc, current_mc, roi * 100.0, pos.get("source", "unknown"), "WINNER"]
        )


def process_paper_stop(mint: str, current_mc: float) -> None:
    pos = STATE.get("paper_positions", {}).get(mint)
    if not pos or pos.get("status") != "OPEN":
        return

    entry_mc = to_float(pos.get("entry_mc", 0.0))
    if entry_mc <= 0:
        return

    roi = (current_mc - entry_mc) / entry_mc
    if roi > PAPER_STOP_ROI or pos.get("stop_notified", False):
        return

    pos["stop_notified"] = True
    name = pos.get("name", mint[:6])

    send_discord(
        f"⚠️ PAPER STOP | {name}\nROI {roi*100:.0f}%\nMC {compact_k(entry_mc)} → {compact_k(current_mc)}"
    )

    with PAPER_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [now_ts(), "STOP", mint, name, pos.get("signal", ""), pos.get("size_eur", 0.0),
             entry_mc, current_mc, roi * 100.0, pos.get("source", "unknown"), "STOP"]
        )


# =========================================================
# ALERTS
# =========================================================

def shortlist_reasons(token_state: dict, pair: dict, holder_stats: dict) -> List[str]:
    reasons = []
    if elite_prebuy_signal(token_state):
        reasons.append("elite prebuy")
    if alpha_wallet_cluster_signal(token_state):
        reasons.append("alpha cluster")
    if creator_reputation_signal(token_state):
        reasons.append("creator reputation")
    if watchers_velocity_signal(token_state):
        reasons.append("watchers velocity")
    if early_liquidity_migration_detector(token_state):
        reasons.append("early liquidity migration")
    if pump_fun_graduation_predictor(token_state, pair):
        reasons.append("graduation predictor")
    if len(token_state.get("x100_wallet_hits", [])) >= 1:
        reasons.append("x100 wallet")
    if fast_roi_signal(token_state, pair, holder_stats):
        reasons.append("fast roi")
    if swing_conviction_signal(token_state, pair, holder_stats):
        reasons.append("swing conviction")
    if not reasons:
        reasons.append("near-threshold")
    return reasons[:3]


def build_alert(pair: dict, token_state: dict, holder_stats: dict, score: int, alert_type: str) -> str:
    mint = token_state["mint"]
    name = token_state.get("name") or mint[:6]
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    pair_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"

    reasons = shortlist_reasons(token_state, pair, holder_stats)
    color = "🔴 RED" if alert_type == "RED" else "🟡 GOLD"
    action = "Sell" if alert_type == "RED" else "Buy 25€ or 50€"

    return (
        f"{name}\n"
        f"Score: {score}/10\n"
        f"Color: {color}\n"
        f"Market cap: ${compact_k(mc)}\n"
        f"Liquidity: ${compact_k(liq)}\n"
        f"Reason: {', '.join(reasons)}\n"
        f"Action: {action}\n"
        f"Dex: <{pair_url}>"
    )


def classify_alert_type(pair: dict, token_state: dict, holder_stats: dict, score: int, hard_red: bool) -> str:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq_tier = liquidity_quality_tier(mc, to_float((pair.get("liquidity") or {}).get("usd"), 0.0))

    if hard_red:
        return "RED" if token_state["mint"] in HELD_TOKENS else "IGNORE"

    if liq_tier == "weak":
        return "IGNORE"
    if not (MIN_GOLD_MC <= mc <= MAX_GOLD_MC):
        return "IGNORE"
    if not can_send_buy_alert():
        return "IGNORE"

    if (
        has_real_action_signal(token_state, pair, holder_stats)
        and live_confirmation_count(pair, token_state, holder_stats) >= 5
        and score >= GOLD_SCORE
    ):
        return "GOLD"

    return "IGNORE"


def qualifies_shortlist(pair: dict, token_state: dict, holder_stats: dict, score: int, hard_red: bool) -> bool:
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    if hard_red or mc <= 0 or mc > MAX_DISCOVERY_MC:
        return False
    if score < 3 or score >= GOLD_SCORE:
        return False
    if holder_stats.get("hard_reject", False):
        return False
    if dev_supply_proxy_risk(token_state) or token_state.get("lp_risk", False):
        return False
    if token_state.get("dev_sold", False):
        return False
    if not token_state.get("tradeability_ok", True):
        return False

    return (
        elite_prebuy_signal(token_state)
        or alpha_wallet_cluster_signal(token_state)
        or creator_reputation_signal(token_state)
        or watchers_velocity_signal(token_state)
        or early_liquidity_migration_detector(token_state)
        or pump_fun_graduation_predictor(token_state, pair)
        or fast_roi_signal(token_state, pair, holder_stats)
        or swing_conviction_signal(token_state, pair, holder_stats)
        or len(token_state.get("x100_wallet_hits", [])) >= 1
        or cult_meme_proxy_signal(token_state, pair, holder_stats)
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
        STATE["cycle_deep_rejected"] += 1
        return

    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    vol = pair.get("volume") or {}
    v5 = to_float(vol.get("m5"), 0.0)
    v1 = to_float(vol.get("h1"), 0.0)
    v24 = to_float(vol.get("h24"), 0.0)
    age_min = max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0)

    token_state["last_pair_url"] = pair.get("url") or ""
    token_state["last_seen_ts"] = now_ts()
    token_state["liq_lock_hint"] = liquidity_lock_hint(pair)

    current_liq = liq
    last_liq = to_float(token_state.get("last_liquidity_usd", 0.0), 0.0)
    if current_liq > 0:
        if last_liq != current_liq:
            token_state["liq_history"].append({"ts": now_ts(), "liq": current_liq})
        token_state["last_liquidity_usd"] = current_liq

    watchers = int(pair.get("watchers", 0)) if isinstance(pair, dict) else 0
    token_state["watchers_history"].append({"ts": now_ts(), "value": watchers})

    prune_old_events(token_state)

    risk_ok, risk_signals, _ = optional_risk_check(mint)
    token_state["risk_ok"] = risk_ok
    token_state["lp_safe"] = bool(risk_signals.get("lp_safe")) or token_state.get("liq_lock_hint", False)
    token_state["lp_risk"] = bool(risk_signals.get("lp_risk", False))
    token_state["tradeability_ok"] = anti_honeypot_guard(pair)

    if token_state["first_seen_mc"] <= 0 and mc > 0:
        token_state["first_seen_mc"] = mc

    token_state["max_seen_mc"] = max(token_state.get("max_seen_mc", 0.0), mc)

    update_dev_wallet_reputation(token_state, mc)

    STATE["cycle_evaluated_tokens"] = int(STATE.get("cycle_evaluated_tokens", 0)) + 1

    # HARD FILTERS
    if mc <= 0 or mc > MAX_DISCOVERY_MC:
        STATE["cycle_deep_rejected"] += 1
        return
    if liq < MIN_LIQUIDITY:
        STATE["cycle_deep_rejected"] += 1
        return
    if age_min < 1:
        STATE["cycle_deep_rejected"] += 1
        return

    holder_stats = get_holder_stats(mint)

    hard_red = (
        holder_stats.get("hard_reject", False)
        or fake_liquidity_risk(mc, liq)
        or sniper_trap_risk(token_state)
        or dev_supply_proxy_risk(token_state)
        or token_state.get("dev_sold", False)
        or token_state.get("lp_risk", False)
        or not token_state.get("tradeability_ok", True)
        or not token_state.get("risk_ok", True)
    )

    score = compute_score(pair, token_state, holder_stats)

    alert_type = classify_alert_type(pair, token_state, holder_stats, score, hard_red)

    if alert_type == "IGNORE":
        STATE["cycle_deep_rejected"] += 1
        return

    alert_key = f"{alert_type}:{mint}"
    if recently_alerted(alert_key):
        return

    send_discord(build_alert(pair, token_state, holder_stats, score, alert_type))
    mark_alerted(alert_key)

    name = token_state.get("name") or mint[:6]
    source = token_state.get("source", "unknown")
    dex_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"

    with ALERT_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            now_ts(), alert_type, mint, name, source, score,
            round(mc, 2), round(liq, 2),
            round(to_float(token_state.get("first_seen_mc", 0.0)), 2),
            round(to_float(token_state.get("max_seen_mc", 0.0)), 2),
            round(age_min, 2),
            round(holder_stats.get("top1_pct", 0.0), 4),
            round(holder_stats.get("top3_pct", 0.0), 4),
            bool(token_state.get("migration_flag", False)),
            len(token_state.get("smart_wallet_hits", [])),
            len(token_state.get("x100_wallet_hits", [])),
            len(token_state.get("elite_prebuy_hits", [])),
            bool(early_liquidity_migration_detector(token_state)),
            bool(pump_fun_graduation_predictor(token_state, pair)),
            bool(alpha_wallet_cluster_signal(token_state)),
            bool(creator_reputation_signal(token_state)),
            bool(watchers_velocity_signal(token_state)),
            int(token_state.get("early_buys", 0)),
            len(token_state.get("early_unique_buyers", [])),
            bool(token_state.get("dev_sold", False)),
            bool(token_state.get("tradeability_ok", True)),
            bool(token_state.get("risk_ok", True)),
            dex_url,
        ])

    if alert_type == "GOLD":
        mark_buy_alert_sent()
        open_paper_position(mint, token_state, pair, alert_type)
        

async def subscribe_token_trade_once(ws, mint: str):
    if mint in SUBSCRIBED_TOKEN_TRADES:
        return
    await subscribe(ws, "subscribeTokenTrade", [mint])
    SUBSCRIBED_TOKEN_TRADES.add(mint)


async def subscribe_account_trade_once(ws, wallet: str):
    if not wallet or wallet in SUBSCRIBED_ACCOUNT_WALLETS:
        return
    await subscribe(ws, "subscribeAccountTrade", [wallet])
    SUBSCRIBED_ACCOUNT_WALLETS.add(wallet)


def discovery_accepts_pair(pair: Optional[dict]) -> bool:
    if not pair:
        return False
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    if mc <= 0 or mc > MAX_DISCOVERY_MC:
        return False
    if liq < MIN_LIQUIDITY:
        return False
    if fake_liquidity_risk(mc, liq):
        return False
    return True


# =========================================================
# LOOPS
# =========================================================

async def websocket_loop():
    while True:
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, ping_timeout=20) as ws:
                dbg("connected to PumpPortal")

                await subscribe(ws, "subscribeNewToken")
                await subscribe(ws, "subscribeMigration")

                watched_wallets = list(set(SMART_WALLETS) | learned_alpha_wallets() | learned_x100_wallets())
                for wallet in watched_wallets:
                    await subscribe_account_trade_once(ws, wallet)

                while True:
                    latest_wallets = list(set(learned_alpha_wallets()) | learned_x100_wallets())
                    for wallet in latest_wallets:
                        if wallet not in SUBSCRIBED_ACCOUNT_WALLETS:
                            await subscribe_account_trade_once(ws, wallet)

                    raw = await ws.recv()
                    payload = extract_message_payload(raw)
                    if not payload:
                        continue

                    STATE["cycle_raw_seen"] += 1

                    mint = extract_mint(payload)
                    event_text = json.dumps(payload).lower()

                    if mint and ("name" in payload and "symbol" in payload):
                        pair = get_best_pair(mint)
                        if discovery_accepts_pair(pair):
                            rec = ensure_token(mint, extract_name(payload), extract_symbol(payload), "new_token")
                            rec["launch_seen_ts"] = now_ts()
                            STATE["cycle_tracked_added"] += 1
                            await subscribe_token_trade_once(ws, mint)
                        else:
                            STATE["cycle_discovery_rejected"] += 1

                    if mint and "migration" in event_text:
                        pair = get_best_pair(mint)
                        if discovery_accepts_pair(pair):
                            rec = ensure_token(mint, extract_name(payload), extract_symbol(payload), "migration")
                            rec["migration_flag"] = True
                            STATE["cycle_tracked_added"] += 1
                            await subscribe_token_trade_once(ws, mint)
                        else:
                            STATE["cycle_discovery_rejected"] += 1
                        continue

                    if not mint:
                        continue

                    side = extract_side(payload)
                    wallet = extract_wallet(payload)
                    usd_est = extract_amount_usd(payload)

                    if side:
                        update_trade_tracking(mint, wallet, side, usd_est)

                        if wallet and wallet in SMART_WALLETS:
                            add_alpha_hit(mint, wallet)
                        if wallet and wallet in learned_alpha_wallets():
                            add_alpha_hit(mint, wallet)
                        if wallet and wallet in learned_x100_wallets():
                            add_x100_hit(mint, wallet)

        except Exception as e:
            print("websocket error, reconnecting:", e)
            await asyncio.sleep(5)


async def gecko_new_pools_loop():
    while True:
        try:
            pools = fetch_gecko_new_pools()
            added = 0

            for item in pools:
                if added >= GECKO_MAX_ADD_PER_CYCLE:
                    break

                STATE["cycle_raw_seen"] += 1
                mint = item["mint"]

                if mint in STATE["tokens"]:
                    continue

                pair = get_best_pair(mint)
                if not discovery_accepts_pair(pair):
                    STATE["cycle_discovery_rejected"] += 1
                    continue

                rec = ensure_token(
                    mint,
                    item.get("name", ""),
                    item.get("symbol", ""),
                    item.get("source", "gecko_new_pool"),
                )
                rec["launch_seen_ts"] = now_ts()
                STATE["cycle_tracked_added"] += 1
                added += 1
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


async def paper_positions_loop():
    while True:
        try:
            for mint, pos in list(STATE.get("paper_positions", {}).items()):
                if pos.get("status") != "OPEN":
                    continue
                pair = get_best_pair(mint)
                if not pair:
                    continue
                current_mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
                if current_mc <= 0:
                    continue
                process_paper_winner(mint, current_mc)
                process_paper_stop(mint, current_mc)
        except Exception as e:
            print("paper loop error:", e)

        await asyncio.sleep(PAPER_CHECK_INTERVAL_SECONDS)


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
                raw_seen = int(STATE.get("cycle_raw_seen", 0))
                discovery_rejected = int(STATE.get("cycle_discovery_rejected", 0))
                tracked_added = int(STATE.get("cycle_tracked_added", 0))
                evaluated = int(STATE.get("cycle_evaluated_tokens", 0))
                deep_rejected = int(STATE.get("cycle_deep_rejected", 0))
                paper_open = sum(
                    1 for p in STATE.get("paper_positions", {}).values()
                    if p.get("status") == "OPEN"
                )
                learned_x100 = len(STATE.get("x100_discovered_wallets", []))
                learned_alpha = len(STATE.get("alpha_discovered_wallets", []))
                
                global LAST_HEARTBEAT_TS
                if time.time() - LAST_HEARTBEAT_TS < 60:
                    await asyncio.sleep(30)
                    continue
                LAST_HEARTBEAT_TS = time.time()
            
                if raw_seen >= 500 or tracked >= 50:
                    send_discord(
                        f"🤖 SCANNER ACTIVE — raw_seen {raw_seen} — discovery_rejected {discovery_rejected} — "
                        f"tracked {tracked} — tracked_added {tracked_added} — evaluated {evaluated} — "
                        f"deep_rejected {deep_rejected} — paper open {paper_open} — "
                        f"x100 wallets {learned_x100} — alpha wallets {learned_alpha} — extreme x100 mode"
                )

                STATE["last_heartbeat"] = now
                STATE["cycle_raw_seen"] = 0
                STATE["cycle_discovery_rejected"] = 0
                STATE["cycle_tracked_added"] = 0
                STATE["cycle_evaluated_tokens"] = 0
                STATE["cycle_deep_rejected"] = 0

        except Exception as e:
            print("heartbeat error:", e)

        await asyncio.sleep(30)


async def main():
    load_state()
    cleanup_state()
    ensure_files()

    dbg("manual smart wallets:", len(SMART_WALLETS))
    dbg("learned alpha wallets:", len(STATE.get("alpha_discovered_wallets", [])))
    dbg("learned x100 wallets:", len(STATE.get("x100_discovered_wallets", [])))
    dbg("rpc enabled:", bool(SOLANA_RPC_URL))
    dbg("pumpportal api key enabled:", bool(PUMPPORTAL_API_KEY))

    await asyncio.gather(
        websocket_loop(),
        gecko_new_pools_loop(),
        evaluator_loop(),
        paper_positions_loop(),
        save_loop(),
        heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
    
