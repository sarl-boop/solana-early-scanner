import os
import json
import time
from pathlib import Path

import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path("state.json")

DEX_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"
MAX_BUY_ALERTS = 2
STATUS_INTERVAL_SEC = 3600


def send(msg: str) -> None:
    try:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print("Discord send error:", e)


def load_state() -> dict:
    default_state = {
        "seen": {},
        "owned": {},
        "last_status": 0
    }

    if not STATE_FILE.exists():
        return default_state

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_state

        if "seen" not in data or not isinstance(data["seen"], dict):
            data["seen"] = {}
        if "owned" not in data or not isinstance(data["owned"], dict):
            data["owned"] = {}
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


def fetch_pairs():
    try:
        r = requests.get(DEX_URL, timeout=12)
        r.raise_for_status()

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


def pair_age_minutes(pair: dict):
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    try:
        now_ms = int(time.time() * 1000)
        return max(0, (now_ms - int(created)) / 60000)
    except Exception:
        return None


def opportunity_level(score: int):
    if score >= 8:
        return "🟨 BUY GOLD", "GOLD"
    if score >= 6:
        return "🔵 BUY STRONG", "STRONG"
    if score >= 4:
        return "🟢 BUY MEDIUM", "MEDIUM"
    return None, None


def compute_score(mc: float, liq: float, vol: float, age_min):
    score = 0

    # Liquidity
    if liq > 30_000:
        score += 1
    if liq > 60_000:
        score += 1
    if liq > 100_000:
        score += 1

    # Volume
    if vol > 100_000:
        score += 1
    if vol > 250_000:
        score += 1
    if vol > 500_000:
        score += 1

    # Market cap
    if 0 < mc < 5_000_000:
        score += 1
    if 0 < mc < 3_000_000:
        score += 1
    if 0 < mc < 1_500_000:
        score += 1

    # Age bonus / penalty légère
    if age_min is not None:
        if 5 <= age_min <= 60:
            score += 1
        elif age_min < 3:
            score -= 1

    return max(0, min(score, 9))


def risk_level(mc: float, liq: float, vol: float, age_min):
    if liq < 30_000:
        return "HIGH"

    if age_min is not None and age_min < 5:
        return "HIGH"

    if liq < 100_000:
        return "MEDIUM"

    if liq >= 100_000 and vol >= 200_000:
        return "LOW"

    return "MEDIUM"


def pass_silent_filters(mc: float, liq: float, vol: float, age_min):
    # Mauvais tokens = ignorés sans notification
    if liq < 30_000:
        return False

    if vol < 50_000:
        return False

    if mc <= 0:
        return False

    if mc > 5_000_000:
        return False

    # Trop neuf = on n'achète pas encore
    if age_min is not None and age_min < 3:
        return False

    return True


def sell_level(liq: float, vol: float):
    if liq < 10_000 or vol < 10_000:
        return "🚨 SELL NOW", "IMMEDIATE"

    if liq < 20_000 or vol < 25_000:
        return "🔴 SELL HIGH RISK", "HIGH"

    if liq < 30_000 or vol < 50_000:
        return "🟠 SELL RISK MEDIUM", "MEDIUM"

    return None, None


def build_buy_message(signal: str, level: str, name: str, addr: str, score: int, risk: str, mc: float, liq: float, vol: float, age_min):
    age_text = "Unknown"
    if age_min is not None:
        age_text = f"{int(age_min)} min"

    return (
        f"{signal}\n\n"
        f"Token: {name}\n"
        f"Address: {addr}\n"
        f"Dex: https://dexscreener.com/solana/{addr}\n\n"
        f"Opportunity: {level}\n"
        f"Score: {score}/9\n"
        f"Risk: {risk}\n"
        f"Age: {age_text}\n"
        f"MC: ${int(mc):,}\n"
        f"Liq: ${int(liq):,}\n"
        f"Vol24h: ${int(vol):,}"
    )


def build_sell_message(signal: str, risk: str, name: str, addr: str, mc: float, liq: float, vol: float, age_min):
    age_text = "Unknown"
    if age_min is not None:
        age_text = f"{int(age_min)} min"

    return (
        f"{signal}\n\n"
        f"Token: {name}\n"
        f"Address: {addr}\n"
        f"Dex: https://dexscreener.com/solana/{addr}\n\n"
        f"Risk: {risk}\n"
        f"Age: {age_text}\n"
        f"MC: ${int(mc):,}\n"
        f"Liq: ${int(liq):,}\n"
        f"Vol24h: ${int(vol):,}"
    )


def main():
    state = load_state()
    pairs = fetch_pairs()

    buy_alerts = []
    found_signal = False
    by_addr = {}

    for p in pairs[:100]:
        try:
            base = p.get("baseToken") or {}
            addr = base.get("address") or ""
            if not addr:
                continue

            name = (
                base.get("symbol")
                or base.get("name")
                or "Unknown Token"
            )

            mc = safe_num(p.get("marketCap") or p.get("fdv"))
            liq = safe_num((p.get("liquidity") or {}).get("usd"))
            vol = safe_num((p.get("volume") or {}).get("h24"))
            age_min = pair_age_minutes(p)

            by_addr[addr] = {
                "name": name,
                "mc": mc,
                "liq": liq,
                "vol": vol,
                "age_min": age_min,
            }

            if addr not in state["seen"]:
                state["seen"][addr] = {
                    "name": name,
                    "first_seen": int(time.time()),
                    "buy_alerted": False,
                }

            # Eviter spam BUY
            if state["seen"][addr].get("buy_alerted"):
                continue

            # Filtres silencieux
            if not pass_silent_filters(mc, liq, vol, age_min):
                continue

            score = compute_score(mc, liq, vol, age_min)
            signal, level = opportunity_level(score)
            if not signal:
                continue

            risk = risk_level(mc, liq, vol, age_min)

            # Pas de BUY si risque haut
            if risk == "HIGH":
                continue

            buy_alerts.append(
                build_buy_message(signal, level, name, addr, score, risk, mc, liq, vol, age_min)
            )

            state["seen"][addr]["buy_alerted"] = True
            found_signal = True

        except Exception as e:
            print("Pair processing error:", e)
            continue

    for msg in buy_alerts[:MAX_BUY_ALERTS]:
        send(msg)

    # SELL alerts uniquement pour tokens possédés
    for addr, owned_info in list(state["owned"].items()):
        info = by_addr.get(addr)
        if not info:
            continue

        sell_signal_text, sell_risk = sell_level(info["liq"], info["vol"])
        if not sell_signal_text:
            continue

        send(
            build_sell_message(
                sell_signal_text,
                sell_risk,
                info["name"],
                addr,
                info["mc"],
                info["liq"],
                info["vol"],
                info["age_min"],
            )
        )

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
