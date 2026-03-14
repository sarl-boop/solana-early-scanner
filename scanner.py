import asyncio
import csv
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Set, Tuple

import requests
import websockets

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

# Optional external risk adapters
RUGCHECK_URL = os.environ.get("RUGCHECK_URL", "").strip()
GOPLUS_URL = os.environ.get("GOPLUS_URL", "").strip()
HONEYPOT_URL = os.environ.get("HONEYPOT_URL", "").strip()

PUMPPORTAL_WS_BASE = "wss://pumpportal.fun/api/data"
PUMPPORTAL_WS = (
    f"{PUMPPORTAL_WS_BASE}?api-key={PUMPPORTAL_API_KEY}"
    if PUMPPORTAL_API_KEY
    else PUMPPORTAL_WS_BASE
)

DEX_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"

STATE_FILE = Path("state.json")
ALERT_LOG_FILE = Path("alerts_log.csv")
PAPER_LOG_FILE = Path("paper_trades_log.csv")

HEARTBEAT_INTERVAL_SECONDS = 3600
SAVE_INTERVAL_SECONDS = 30
EVALUATE_INTERVAL_SECONDS = 10
GECKO_REFRESH_SECONDS = 20
PAPER_CHECK_INTERVAL_SECONDS = 300

TOKEN_TTL_SECONDS = 3 * 3600
ALERT_COOLDOWN_SECONDS = 8 * 3600

BUY_ALERT_WINDOW_SECONDS = 20 * 60
MAX_BUY_ALERTS_PER_WINDOW = 3

MAX_MARKET_CAP = 5_000_000
MIN_LIQUIDITY = 1_200
MIN_LIQ_TO_MC_RATIO = 0.06
WASH_RATIO_LIMIT = 45.0
NO_CHASE_MULTIPLIER = 2.6
MAX_TRACKED_TOKENS = 280
GECKO_MAX_ADD_PER_CYCLE = 80

GOLD_SCORE = 8
GREEN_SCORE = 6

TOP1_HARD_REJECT = 0.18
TOP3_HARD_REJECT = 0.40
TOP1_SOFT_PENALTY = 0.10
TOP3_SOFT_PENALTY = 0.25

LOCKER_KEYWORDS = ["locker", "locked", "burn", "null", "dead"]

PAPER_GOLD_SIZE_EUR = 50
PAPER_GREEN_SIZE_EUR = 25
PAPER_WINNER_ROI = 2.0
PAPER_STOP_ROI = -0.35

ALPHA_MIN_TRADES = 3
ALPHA_MIN_WIN_RATE = 0.40

# New advanced detection
BURST_WINDOW_SECONDS = 30
CLUSTER_WINDOW_SECONDS = 60

BURST_MIN_BUYS = 5
CLUSTER_MIN_UNIQ = 4
LIQUIDITY_ADD_MIN_USD = 5000
LIQUIDITY_ADD_RATIO = 1.8

DEBUG = True

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
    "alpha_discovered_wallets": [],
    "cycle_seen_tokens": 0,
    "cycle_evaluated_tokens": 0,
}

SUBSCRIBED_TOKEN_TRADES: Set[str] = set()

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
            STATE.setdefault("paper_positions", {})
            STATE.setdefault("wallet_stats", {})
            STATE.setdefault("alpha_discovered_wallets", [])
            STATE.setdefault("cycle_seen_tokens", 0)
            STATE.setdefault("cycle_evaluated_tokens", 0)
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

# =========================================================
# CSV LOGS
# =========================================================

def ensure_alert_log_file() -> None:
    if ALERT_LOG_FILE.exists():
        return
    with ALERT_LOG_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "alert_type", "mint", "name", "source", "score",
            "market_cap", "liquidity", "first_seen_mc", "max_seen_mc", "age_min",
            "top1_pct", "top3_pct", "migration_flag", "smart_wallet_count",
            "early_buys", "early_unique_buyers", "dev_sold", "tradeability_ok",
            "risk_ok", "dex_url"
        ])


def ensure_paper_log_file() -> None:
    if PAPER_LOG_FILE.exists():
        return
    with PAPER_LOG_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "event", "mint", "name", "paper_signal", "size_eur",
            "entry_mc", "current_mc", "roi_pct", "source", "status"
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
    risk_ok: bool,
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
                tradeability_ok, risk_ok, dex_url
            ])
    except Exception as e:
        print("alert log write error:", e)


def log_paper_csv(
    event: str,
    mint: str,
    name: str,
    paper_signal: str,
    size_eur: float,
    entry_mc: float,
    current_mc: float,
    roi_pct: float,
    source: str,
    status: str,
) -> None:
    try:
        ensure_paper_log_file()
        with PAPER_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                now_ts(), event, mint, name, paper_signal, size_eur,
                round(entry_mc, 2), round(current_mc, 2), round(roi_pct, 2),
                source, status
            ])
    except Exception as e:
        print("paper log write error:", e)

# =========================================================
# TOKEN / WALLET STATE
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
        "last_seen_ts": now_ts(),
        "first_seen_mc": 0.0,
        "max_seen_mc": 0.0,
        "last_pair_url": "",
        "last_liquidity_usd": 0.0,
        "last_liq_change_ts": 0,
        "smart_wallet_hits": [],
        "buy_wallet_counts": {},
        "sell_wallet_counts": {},
        "first_buy_wallets": [],
        "first_buy_ts": 0,
        "candidate_dev_wallet": None,
        "dev_sold": False,
        "tradeability_ok": True,
        "risk_ok": True,
        "liq_lock_hint": False,
        "migration_flag": source == "migration",
        "early_buys": 0,
        "early_sells": 0,
        "early_unique_buyers": [],
        "early_unique_sellers": [],
        "early_volume_est": 0.0,
        "alpha_cluster_score": 0,
        "buy_events": [],
        "sell_events": [],
        "liq_history": [],
    }
    STATE["tokens"][mint] = rec
    return rec


def learned_alpha_wallets() -> Set[str]:
    return set(STATE.get("alpha_discovered_wallets", []))


def update_wallet_stats_from_trade(wallet: str) -> None:
    if not wallet:
        return
    stats = STATE["wallet_stats"].setdefault(wallet, {"wins": 0, "trades": 0})
    stats["trades"] = int(stats.get("trades", 0)) + 1


def update_wallet_stats_from_winner(wallet: str) -> None:
    if not wallet:
        return
    stats = STATE["wallet_stats"].setdefault(wallet, {"wins": 0, "trades": 0})
    stats["wins"] = int(stats.get("wins", 0)) + 1

    trades = int(stats.get("trades", 0))
    wins = int(stats.get("wins", 0))
    if trades >= ALPHA_MIN_TRADES and (wins / max(trades, 1)) >= ALPHA_MIN_WIN_RATE:
        alphas = set(STATE.get("alpha_discovered_wallets", []))
        alphas.add(wallet)
        STATE["alpha_discovered_wallets"] = list(alphas)

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
# OPTIONAL RISK ADAPTERS
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


def optional_risk_check(mint: str) -> Tuple[bool, List[str]]:
    notes: List[str] = []

    rc = fetch_optional_json(RUGCHECK_URL, mint)
    gp = fetch_optional_json(GOPLUS_URL, mint)
    hp = fetch_optional_json(HONEYPOT_URL, mint)

    for label, data in [("rugcheck", rc), ("goplus", gp), ("honeypot", hp)]:
        if not data:
            continue
        txt = json.dumps(data).lower()
        if any(x in txt for x in ["honeypot", "cannot sell", "blacklist", "malicious", "rug", "scam"]):
            notes.append(f"{label} risk flag")

    return (len(notes) == 0), notes

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


def extract
