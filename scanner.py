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

# timing
HEARTBEAT_INTERVAL_SECONDS = 3600
SAVE_INTERVAL_SECONDS = 30
EVALUATE_INTERVAL_SECONDS = 20
TOKEN_TTL_SECONDS = 48 * 3600
ALERT_COOLDOWN_SECONDS = 12 * 3600

# risk / scoring
MAX_MARKET_CAP = 5_000_000
MIN_LIQUIDITY = 15_000
MIN_LIQ_TO_MC_RATIO = 0.40
WASH_RATIO_LIMIT = 35.0
NO_CHASE_MULTIPLIER = 3.0

GOLD_SCORE = 8
GREEN_SCORE = 6

# token program id
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# =========================================================
# STATE
# =========================================================

STATE: Dict[str, Any] = {
    "tokens": {},       # mint -> token record
    "alerted": {},      # mint -> last alert ts
    "last_heartbeat": 0
}

# token record example
# {
#   "mint": str,
#   "name": str,
#   "symbol": str,
#   "source": "new_token" | "migration" | "unknown",
#   "first_seen_ts": int,
#   "last_seen_ts": int,
#   "first_seen_mc": float,
#   "max_seen_mc": float,
#   "last_pair_url": str,
#   "smart_wallet_hits": [],
#   "buy_wallet_counts": {},
#   "first_buy_wallets": [],
#   "first_buy_ts": int,
# }

# =========================================================
# UTILS
# =========================================================

def now_ts() -> int:
    return int(time.time())


def to_float(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def load_state() -> None:
    global STATE
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            STATE = data
            STATE.setdefault("tokens", {})
            STATE.setdefault("alerted", {})
            STATE.setdefault("last_heartbeat", 0)
    except Exception as e:
        print("state load error:", e)


def save_state() -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(STATE, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        print("state save error:", e)


def cleanup_state() -> None:
    now = now_ts()

    keep_alerted = {}
    for mint, ts in STATE.get("alerted", {}).items():
        if now - int(ts) < 7 * 24 * 3600:
            keep_alerted[mint] = ts
    STATE["alerted"] = keep_alerted

    keep_tokens = {}
    for mint, rec in STATE.get("tokens", {}).items():
        if now - int(rec.get("last_seen_ts", 0)) < TOKEN_TTL_SECONDS:
            keep_tokens[mint] = rec
    STATE["tokens"] = keep_tokens


def recently_alerted(mint: str) -> bool:
    ts = int(STATE.get("alerted", {}).get(mint, 0))
    return (now_ts() - ts) < ALERT_COOLDOWN_SECONDS


def mark_alerted(mint: str) -> None:
    STATE["alerted"][mint] = now_ts()


def send_discord(msg: str) -> None:
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
    except Exception as e:
        print("discord send error:", e)


def ensure_token(mint: str, name: str = "", symbol: str = "", source: str = "unknown") -> dict:
    rec = STATE["tokens"].get(mint)
    if rec:
        if name and not rec.get("name"):
            rec["name"] = name
        if symbol and not rec.get("symbol"):
            rec["symbol"] = symbol
        if source and rec.get("source") == "unknown":
            rec["source"] = source
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
        "first_buy_wallets": [],
        "first_buy_ts": 0,
    }
    STATE["tokens"][mint] = rec
    return rec

# =========================================================
# RPC
# =========================================================

def rpc_call(method: str, params: list) -> Optional[dict]:
    if not SOLANA_RPC_URL:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        r = requests.post(SOLANA_RPC_URL, json=payload, timeout=15)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print("rpc error:", method, e)
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

    hard_reject = False
    soft_penalty = False

    # anti dev wallet / concentration
    if top1_pct > 0.20:
        hard_reject = True
    if top3_pct > 0.40:
        hard_reject = True

    # softer concentration warning
    if top1_pct > 0.12 or top3_pct > 0.30:
        soft_penalty = True

    return {
        "enabled": True,
        "top1_pct": top1_pct,
        "top3_pct": top3_pct,
        "hard_reject": hard_reject,
        "soft_penalty": soft_penalty,
    }

# =========================================================
# DEXSCREENER
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
    except Exception as e:
        print("dex error:", mint, e)
        return None

# =========================================================
# PARSING WEBSOCKET
# =========================================================

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

# =========================================================
# TRACKING / RISK
# =========================================================

def update_trade_tracking(mint: str, wallet: Optional[str], side: str) -> None:
    if not wallet or side != "buy":
        return

    rec = ensure_token(mint)

    if rec["first_buy_ts"] == 0:
        rec["first_buy_ts"] = now_ts()

    counts = rec["buy_wallet_counts"]
    counts[wallet] = counts.get(wallet, 0) + 1

    if len(rec["first_buy_wallets"]) < 10 and wallet not in rec["first_buy_wallets"]:
        rec["first_buy_wallets"].append(wallet)


def coordinated_pump_risk(token_state: dict) -> bool:
    """
    Suspicious if very few wallets dominate early buys.
    """
    counts = token_state.get("buy_wallet_counts", {})
    if not counts:
        return False

    total_buys = sum(counts.values())
    unique_wallets = len(counts)

    if total_buys < 8:
        return False

    biggest = max(counts.values()) if counts else 0

    # one wallet buying too many times very early
    if biggest > 2:
        return True

    # too few wallets for too many early buys
    if total_buys >= 10 and unique_wallets <= 3:
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


def add_smart_wallet_hit(mint: str, wallet: str) -> None:
    rec = ensure_token(mint)
    hits = set(rec.get("smart_wallet_hits", []))
    hits.add(wallet)
    rec["smart_wallet_hits"] = list(hits)

# =========================================================
# SCORE / CLASSIFY
# =========================================================

def compute_score(pair: dict, token_state: dict, holder_stats: dict) -> (int, List[str]):
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
    reasons = []

    if mc < 50_000:
        score += 2
        reasons.append("micro-cap basse")
    elif mc < 200_000:
        score += 1

    if liq >= mc * 0.7:
        score += 2
        reasons.append("liquidité forte")
    elif liq >= mc * 0.5:
        score += 1

    if v1 > 0 and v5 * 12 > v1 * 0.30 and v5 > 5_000:
        score += 2
        reasons.append("volume 5m accélère")
    elif v5 > 8_000:
        score += 1

    if buys > sells:
        score += 1
        reasons.append("acheteurs dominants")

    if buy_ratio > 0.60:
        score += 1

    if buys >= 10:
        score += 1

    if token_state.get("smart_wallet_hits"):
        score += 2
        reasons.append("smart wallet")

    if age_min <= 5:
        score += 1
        reasons.append("très early")

    if holder_stats.get("soft_penalty"):
        score -= 2
        reasons.append("concentration holders")

    if coordinated_pump_risk(token_state):
        score -= 3
        reasons.append("pump coordonné suspect")

    if wash_trading_risk(v24, liq):
        score -= 3
        reasons.append("wash trading suspect")

    return max(0, min(10, score)), reasons


def classify(score: int, hard_red: bool) -> str:
    if hard_red or score < GREEN_SCORE:
        return "🔴 RED"
    if score >= GOLD_SCORE:
        return "🟡 GOLD"
    return "🟢 GREEN"

# =========================================================
# ALERTS
# =========================================================

def build_alert(pair: dict, token_state: dict, holder_stats: dict, score: int, color: str) -> str:
    mint = token_state["mint"]
    name = token_state.get("name") or (pair.get("baseToken") or {}).get("name") or mint[:6]
    mc = int(to_float(pair.get("marketCap") or pair.get("fdv"), 0.0))
    liq = int(to_float((pair.get("liquidity") or {}).get("usd"), 0.0))
    age_min = int(max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0))
    pair_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"

    top1 = holder_stats.get("top1_pct", 0.0) * 100
    top3 = holder_stats.get("top3_pct", 0.0) * 100

    if color == "🟡 GOLD":
        action = "Buy 50€ maintenant"
    elif color == "🟢 GREEN":
        action = "Buy 25€ maintenant"
    else:
        action = "Avoid / Exit"

    return (
        f"{color}\n\n"
        f"Token name: {name}\n"
        f"Score: {score}/10\n"
        f"Color: {color}\n"
        f"Market cap: ${mc:,}\n"
        f"Liquidity: ${liq:,}\n"
        f"First seen MC: ${int(token_state.get('first_seen_mc', mc)):,}\n"
        f"Max seen MC: ${int(token_state.get('max_seen_mc', mc)):,}\n"
        f"Age: {age_min} min\n"
        f"Top1: {top1:.1f}%\n"
        f"Top3: {top3:.1f}%\n"
        f"Dex: {pair_url}\n\n"
        f"Action: {action}"
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
    vol24 = to_float((pair.get("volume") or {}).get("h24"), 0.0)
    age_min = max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0)

    token_state["last_pair_url"] = pair.get("url") or ""
    token_state["last_seen_ts"] = now_ts()

    if token_state["first_seen_mc"] <= 0 and mc > 0:
        token_state["first_seen_mc"] = mc
    token_state["max_seen_mc"] = max(token_state.get("max_seen_mc", 0.0), mc)

    # hard gates
    if mc <= 0 or mc > MAX_MARKET_CAP:
        return
    if liq < MIN_LIQUIDITY:
        return
    if age_min < 1:
        return

    holder_stats = get_holder_stats(mint)

    hard_red = False

    if holder_stats.get("hard_reject", False):
        hard_red = True
    if fake_liquidity_risk(mc, liq):
        hard_red = True
    if no_chase_risk(token_state.get("first_seen_mc", 0.0), mc, age_min):
        hard_red = True

    score, _ = compute_score(pair, token_state, holder_stats)
    color = classify(score, hard_red)

    if recently_alerted(mint):
        return

    # no uncertain alerts; send only clear action
    if color == "🔴 RED" or score >= GREEN_SCORE:
        msg = build_alert(pair, token_state, holder_stats, score, color)
        send_discord(msg)
        mark_alerted(mint)

# =========================================================
# WEBSOCKET
# =========================================================

async def subscribe(ws, method: str, keys: Optional[List[str]] = None):
    payload = {"method": method}
    if keys:
        payload["keys"] = keys
    await ws.send(json.dumps(payload))


async def websocket_loop():
    while True:
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, ping_timeout=20) as ws:
                print("connected to PumpPortal")

                await subscribe(ws, "subscribeNewToken")
                await subscribe(ws, "subscribeMigration")

                if SMART_WALLETS:
                    await subscribe(ws, "subscribeAccountTrade", list(SMART_WALLETS))

                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)

                    mint = extract_mint(msg)
                    if not mint:
                        continue

                    event_text = json.dumps(msg).lower()

                    # new token / migration
                    if "migration" in event_text:
                        ensure_token(mint, extract_name(msg), extract_symbol(msg), "migration")
                        await subscribe(ws, "subscribeTokenTrade", [mint])
                        continue

                    if "new" in event_text or ("name" in msg and "symbol" in msg):
                        ensure_token(mint, extract_name(msg), extract_symbol(msg), "new_token")
                        await subscribe(ws, "subscribeTokenTrade", [mint])
                        continue

                    # token trades
                    side = extract_side(msg)
                    wallet = extract_wallet(msg)

                    if side:
                        update_trade_tracking(mint, wallet, side)
                        if wallet and wallet in SMART_WALLETS:
                            add_smart_wallet_hit(mint, wallet)

        except Exception as e:
            print("websocket error, reconnecting:", e)
            await asyncio.sleep(5)

# =========================================================
# BACKGROUND LOOPS
# =========================================================

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
    print("smart wallets loaded:", len(SMART_WALLETS))
    print("rpc enabled:", bool(SOLANA_RPC_URL))

    await asyncio.gather(
        websocket_loop(),
        evaluator_loop(),
        save_loop(),
        heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
