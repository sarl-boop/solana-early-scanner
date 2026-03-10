import os
import json
import time
from pathlib import Path

import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("state.json")

DEX_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"
MAX_ALERTS = 2
STATUS_INTERVAL_SEC = 3600


def send(msg: str) -> None:
    try:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print("Discord send error:", e)


def load_state() -> dict:
    default_state = {"tokens": {}, "last_status": 0}

    if not STATE_FILE.exists():
        return default_state

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_state
        if "tokens" not in data or not isinstance(data["tokens"], dict):
            data["tokens"] = {}
        if "last_status" not in data or not isinstance(data["last_status"], int):
            data["last_status"] = 0
        return data
    except Exception as e:
        print("State load error:", e)
        return default_state


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("State save error:", e)


def safe_num(value, default=0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def classify(liq: float, mc: float, vol: float):
    score = 0

    if liq > 30_000:
        score += 1
    if liq > 60_000:
        score += 1
    if liq > 100_000:
        score += 1

    if vol > 100_000:
        score += 1
    if vol > 250_000:
        score += 1
    if vol > 500_000:
        score += 1

    if 0 < mc < 5_000_000:
        score += 1
    if 0 < mc < 3_000_000:
        score += 1
    if 0 < mc < 1_500_000:
        score += 1

    if score >= 7:
        return "🟨 GOLD", "BUY priority", score

    if score >= 4:
        return "🟢 GREEN", "BUY small", score

    return None, None, score


def fetch_pairs():
    try:
        r = requests.get(DEX_URL, timeout=12)
        r.raise_for_status()

        # Certaines réponses Dexscreener peuvent être vides ou non JSON
        try:
            data = r.json()
        except Exception as e:
            print("JSON decode error:", e)
            return []

        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]

        if isinstance(data, list):
            return data

        print("Unexpected API format")
        return []

    except Exception as e:
        print("Dexscreener API error:", e)
        return []


def build_message(signal: str, name: str, addr: str, score: int, mc: float, liq: float, vol: float, action: str) -> str:
    return (
        f"{signal}\n\n"
        f"Token: {name}\n"
        f"Address: {addr}\n"
        f"Dex: https://dexscreener.com/solana/{addr}\n\n"
        f"Score: {score}/9\n"
        f"MC: ${int(mc):,}\n"
        f"Liq: ${int(liq):,}\n"
        f"Vol24h: ${int(vol):,}\n\n"
        f"Action: {action}"
    )


def main():
    state = load_state()
    pairs = fetch_pairs()

    alerts = []
    found_signal = False

    for p in pairs[:40]:
        try:
            base = p.get("baseToken") or {}
            addr = base.get("address") or ""
            if not addr:
                continue

            if addr in state["tokens"]:
                continue

            name = (
                base.get("symbol")
                or base.get("name")
                or "Unknown Token"
            )

            mc = safe_num(p.get("marketCap") or p.get("fdv"))
            liq = safe_num((p.get("liquidity") or {}).get("usd"))
            vol = safe_num((p.get("volume") or {}).get("h24"))

            signal, action, score = classify(liq, mc, vol)

            # On mémorise quand même le token vu pour éviter de retraiter toujours les mêmes
            state["tokens"][addr] = {
                "name": name,
                "first_seen": int(time.time())
            }

            if signal:
                found_signal = True
                alerts.append(
                    build_message(signal, name, addr, score, mc, liq, vol, action)
                )

        except Exception as e:
            print("Pair processing error:", e)
            continue

    for msg in alerts[:MAX_ALERTS]:
        send(msg)

    now = int(time.time())
    if now - state.get("last_status", 0) >= STATUS_INTERVAL_SEC:
        if not found_signal:
            send("🤖 SCANNER ACTIVE — No signals detected")
        else:
            send("🤖 SCANNER ACTIVE — Scan completed")
        state["last_status"] = now

    save_state(state)


if __name__ == "__main__":
    main()
