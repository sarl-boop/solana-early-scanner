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
DEX_TOKENS_API = "https://api.dexscreener.com/tokens/v1/solana/"

STATE_FILE = Path("state_live.json")

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

MAX_MC = 5_000_000
MIN_LIQ = 15_000
ALERT_COOLDOWN_SECONDS = 12 * 3600
NO_CHASE_MULTIPLIER = 3.0
WASH_RATIO_LIMIT = 35.0
EVALUATE_EVERY_SECONDS = 20
SAVE_EVERY_SECONDS = 30

# =========================================================
# STATE
# =========================================================

STATE: Dict[str, Any] = {
    "tokens": {},    # mint -> token state
    "alerted": {},   # mint -> ts
}

# token state shape:
# {
#   "mint": str,
#   "name": str,
#   "symbol": str,
#   "first_seen_ts": int,
#   "first_seen_mc": float,
#   "max_seen_mc": float,
#   "last_seen_ts": int,
#   "last_pair_url": str,
#   "source": "new_token"|"migration",
#   "trade": {
#       "first_10_buyers": [],
#       "buy_counts": {},
#       "smart_wallet_hits": [],
#   }
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
    for mint, ts in STATE.get("alerted", {}).items():
        if now - int(ts) < 7 * 24 * 3600:
            keep_alerted[mint] = ts
    STATE["alerted"] = keep_alerted

    keep_tokens = {}
    for mint, rec in STATE.get("tokens", {}).items():
        last_seen = int(rec.get("last_seen_ts", 0))
        if now - last_seen < 48 * 3600:
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


# =========================================================
# SOLANA RPC
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
        data = r.json()
        return data.get("result")
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


def get_account_info(address: str) -> Optional[dict]:
    result = rpc_call(
        "getAccountInfo",
        [address, {"commitment": "confirmed", "encoding": "jsonParsed"}],
    )
    if not result:
        return None
    return result.get("value")


def classify_top_account(account_info: Optional[dict]) -> str:
    if not account_info:
        return "unknown"

    owner = account_info.get("owner", "")
    if owner != TOKEN_PROGRAM_ID:
        return "private_or_unknown"

    data = account_info.get("data")
    if isinstance(data, dict):
        parsed = data.get("parsed", {})
        info = parsed.get("info", {})
        if info.get("tokenAmount") is not None:
            return "token_account"

    return "private_or_unknown"


def get_holder_stats(mint: str) -> dict:
    if not SOLANA_RPC_URL:
        return {
            "enabled": False,
            "top1_pct": 0.0,
            "top3_pct": 0.0,
            "top1_kind": "unknown",
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
            "top1_kind": "unknown",
            "hard_reject": False,
            "soft_penalty": False,
        }

    top_amounts = [to_float(x.get("uiAmount"), 0.0) for x in largest[:3]]
    top1_pct = top_amounts[0] / supply if top_amounts else 0.0
    top3_pct = sum(top_amounts) / supply if supply > 0 else 0.0

    top1_addr = largest[0].get("address", "")
    top1_info = get_account_info(top1_addr) if top1_addr else None
    top1_kind = classify_top_account(top1_info)

    hard_reject = False
    soft_penalty = False

    # Anti dev wallet / concentration
    if top1_kind != "token_account" and top1_pct > 0.20:
        hard_reject = True
    if top1_kind != "token_account" and top3_pct > 0.40:
        hard_reject = True

    # Even if token-account-like, extreme concentration is still suspicious
    if top1_kind == "token_account" and top1_pct > 0.88:
        soft_penalty = True
    if top1_kind == "token_account" and top3_pct > 0.95:
        soft_penalty = True

    return {
        "enabled": True,
        "top1_pct": top1_pct,
        "top3_pct": top3_pct,
        "top1_kind": top1_kind,
        "hard_reject": hard_reject,
        "soft_penalty": soft_penalty,
    }


# =========================================================
# DEXSCREENER
# =========================================================

def get_best_pair_for_token(mint: str) -> Optional[dict]:
    try:
        r = requests.get(DEX_TOKENS_API + mint, timeout=12)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            return None

        # Keep Solana pairs only, choose highest liquidity
        candidates = [p for p in data if p.get("chainId") == "solana"]
        if not candidates:
            return None

        candidates.sort(
            key=lambda x: to_float((x.get("liquidity") or {}).get("usd"), 0.0),
            reverse=True,
        )
        return candidates[0]
    except Exception as e:
        print("dex fetch error:", mint, e)
        return None


# =========================================================
# TOKEN STATE
# =========================================================

def ensure_token(mint: str, name: str = "", symbol: str = "", source: str = "") -> dict:
    rec = STATE["tokens"].get(mint)
    if rec:
        if name and not rec.get("name"):
            rec["name"] = name
        if symbol and not rec.get("symbol"):
            rec["symbol"] = symbol
        if source and not rec.get("source"):
            rec["source"] = source
        rec["last_seen_ts"] = now_ts()
        return rec

    rec = {
        "mint": mint,
        "name": name or mint[:6],
        "symbol": symbol or "",
        "source": source or "unknown",
        "first_seen_ts": now_ts(),
        "first_seen_mc": 0.0,
        "max_seen_mc": 0.0,
        "last_seen_ts": now_ts(),
        "last_pair_url": "",
        "trade": {
            "first_10_buyers": [],
            "buy_counts": {},
            "smart_wallet_hits": [],
        },
    }
    STATE["tokens"][mint] = rec
    return rec


def update_trade_state(mint: str, buyer: Optional[str], side: str) -> None:
    rec = ensure_token(mint)
    trade = rec["trade"]

    if side != "buy" or not buyer:
        return

    if len(trade["first_10_buyers"]) < 10:
        trade["first_10_buyers"].append(buyer)

    counts = trade["buy_counts"]
    counts[buyer] = counts.get(buyer, 0) + 1


def add_smart_wallet_hit(mint: str, wallet: str) -> None:
    rec = ensure_token(mint)
    hits = set(rec["trade"].get("smart_wallet_hits", []))
    hits.add(wallet)
    rec["trade"]["smart_wallet_hits"] = list(hits)


# =========================================================
# FILTERS / SCORE
# =========================================================

def sniper_pattern_reject(token_state: dict) -> bool:
    first_10 = token_state["trade"].get("first_10_buyers", [])
    counts = token_state["trade"].get("buy_counts", {})
    if len(first_10) < 5:
        return False
    if not counts:
        return False
    max_count = max(counts.values())
    return max_count > 2


def fake_liquidity_reject(mc: float, liq: float) -> bool:
    return mc > 0 and liq < 0.5 * mc


def wash_trading_reject(v24: float, liq: float) -> bool:
    if liq <= 0:
        return False
    return (v24 / liq) > WASH_RATIO_LIMIT


def no_chase_reject(first_seen_mc: float, current_mc: float, age_min: float) -> bool:
    if first_seen_mc <= 0:
        return False
    if age_min <= 10:
        return False
    return current_mc > first_seen_mc * NO_CHASE_MULTIPLIER


def compute_score(pair: dict, token_state: dict, holder_stats: dict) -> (int, List[str]):
    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    v5 = to_float((pair.get("volume") or {}).get("m5"), 0.0)
    v1 = to_float((pair.get("volume") or {}).get("h1"), 0.0)
    v24 = to_float((pair.get("volume") or {}).get("h24"), 0.0)

    tx5 = pair.get("txns", {}).get("m5", {}) or {}
    buys = int(tx5.get("buys", 0))
    sells = int(tx5.get("sells", 0))
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

    if token_state["trade"].get("smart_wallet_hits"):
        score += 2
        reasons.append("smart wallet")

    if holder_stats.get("enabled"):
        if holder_stats["top1_kind"] == "token_account":
            reasons.append("top1 pool-like")
        if holder_stats["top1_pct"] < 0.10 and holder_stats["top3_pct"] < 0.25:
            score += 1
            reasons.append("distribution saine")
        if holder_stats.get("soft_penalty"):
            score -= 1
            reasons.append("concentration élevée")

    if age_min <= 5:
        score += 1

    if wash_trading_reject(v24, liq):
        score -= 3
        reasons.append("wash risk")

    return max(0, min(10, score)), reasons


def build_alert(pair: dict, token_state: dict, holder_stats: dict, score: int, reasons: List[str], action: str) -> str:
    mint = token_state["mint"]
    name = token_state.get("name") or (pair.get("baseToken") or {}).get("name") or mint[:6]
    mc = int(to_float(pair.get("marketCap") or pair.get("fdv"), 0.0))
    liq = int(to_float((pair.get("liquidity") or {}).get("usd"), 0.0))
    age_min = int(max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0))
    pair_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"

    extra = ""
    if holder_stats.get("enabled"):
        extra = (
            f"Top1: {holder_stats['top1_pct'] * 100:.1f}%\n"
            f"Top3: {holder_stats['top3_pct'] * 100:.1f}%\n"
            f"Top1 kind: {holder_stats['top1_kind']}\n"
        )

    reason_text = " + ".join(reasons[:4]) if reasons else "signal confirmé"

    color = "🟡 GOLD" if score >= 8 else "🟢 GREEN"

    return (
        f"{color}\n\n"
        f"Token name: {name}\n"
        f"Score: {score}/10\n"
        f"Color: {color}\n"
        f"Market cap: ${mc:,}\n"
        f"Liquidity: ${liq:,}\n"
        f"First seen MC: ${int(token_state.get('first_seen_mc', mc)):,}\n"
        f"Max seen MC: ${int(token_state.get('max_seen_mc', mc)):,}\n"
        f"Reason: {reason_text}\n"
        f"Age: {age_min} min\n"
        f"{extra}"
        f"Dex: {pair_url}\n\n"
        f"Action: {action}"
    ).strip()


# =========================================================
# EVALUATION
# =========================================================

def evaluate_token(mint: str) -> None:
    token_state = STATE["tokens"].get(mint)
    if not token_state:
        return

    pair = get_best_pair_for_token(mint)
    if not pair:
        return

    mc = to_float(pair.get("marketCap") or pair.get("fdv"), 0.0)
    liq = to_float((pair.get("liquidity") or {}).get("usd"), 0.0)
    v24 = to_float((pair.get("volume") or {}).get("h24"), 0.0)
    age_min = max(0.0, (now_ts() - token_state["first_seen_ts"]) / 60.0)

    token_state["last_pair_url"] = pair.get("url") or ""
    token_state["last_seen_ts"] = now_ts()

    if token_state["first_seen_mc"] <= 0 and mc > 0:
        token_state["first_seen_mc"] = mc
    token_state["max_seen_mc"] = max(token_state.get("max_seen_mc", 0.0), mc)

    # Basic gates
    if mc <= 0 or mc > MAX_MC:
        return
    if liq < MIN_LIQ:
        return
    if age_min < 1:
        return

    # Traps
    if fake_liquidity_reject(mc, liq):
        return
    if wash_trading_reject(v24, liq):
        return
    if sniper_pattern_reject(token_state):
        return
    if no_chase_reject(token_state.get("first_seen_mc", 0.0), mc, age_min):
        return

    holder_stats = get_holder_stats(mint)
    if holder_stats.get("hard_reject", False):
        return

    score, reasons = compute_score(pair, token_state, holder_stats)

    if score >= 8 and not recently_alerted(mint):
        msg = build_alert(pair, token_state, holder_stats, score, reasons, "Buy 50€ maintenant")
        send_discord(msg)
        mark_alerted(mint)
    elif score >= 6 and not recently_alerted(mint):
        msg = build_alert(pair, token_state, holder_stats, score, reasons, "Buy 25€ maintenant")
        send_discord(msg)
        mark_alerted(mint)


# =========================================================
# PUMPPORTAL MESSAGE PARSING
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
        or msg.get("account")
        or msg.get("wallet")
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
# LIVE SCANNER
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

                    # New token / migration
                    event_text = json.dumps(msg).lower()
                    if "migration" in event_text:
                        ensure_token(mint, extract_name(msg), extract_symbol(msg), "migration")
                        await subscribe(ws, "subscribeTokenTrade", [mint])
                        continue

                    if "new" in event_text or ("name" in msg and "symbol" in msg):
                        ensure_token(mint, extract_name(msg), extract_symbol(msg), "new_token")
                        await subscribe(ws, "subscribeTokenTrade", [mint])
                        continue

                    # Trade
                    side = extract_side(msg)
                    wallet = extract_wallet(msg)
                    if side:
                        update_trade_state(mint, wallet, side)
                        if wallet and wallet in SMART_WALLETS:
                            add_smart_wallet_hit(mint, wallet)

        except Exception as e:
            print("websocket error, reconnecting:", e)
            await asyncio.sleep(5)


async def evaluator_loop():
    while True:
        try:
            cleanup_state()
            mints = list(STATE.get("tokens", {}).keys())
            for mint in mints:
                evaluate_token(mint)
        except Exception as e:
            print("evaluator error:", e)

        await asyncio.sleep(EVALUATE_EVERY_SECONDS)


async def save_loop():
    while True:
        save_state()
        await asyncio.sleep(SAVE_EVERY_SECONDS)


async def main():
    load_state()
    cleanup_state()
    print("SMART_WALLETS loaded:", len(SMART_WALLETS))
    print("RPC enabled:", bool(SOLANA_RPC_URL))

    await asyncio.gather(
        websocket_loop(),
        evaluator_loop(),
        save_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
