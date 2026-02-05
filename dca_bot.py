#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Binance Daily DCA Bot (USDC) - v5 (ATR always + EMA regime, multi-asset)

This version keeps the carry + post-only LIMIT-buy workflow and extends the *dynamic budget multiplier*
(24h move + 200DMA ratio + ATR sizing + EMA(50) regime cap) to any active symbol (weight > 0).

Tracked / tradable symbols (USDC pairs): BTC, SOL, ETH, ADA, XRP.

Telegram commands:
- /status
- /preview_dca
- /run_orders confirm
- /run_dca confirm
- /reset_carry confirm
- /reset_carry_all confirm
- /reset_btc confirm
- /reset_sol confirm
- /reset_eth confirm
- /reset_ada confirm
- /reset_xrp confirm

Use at your own risk.
"""

import os
import sys
import time
import json
import logging
import threading
import requests

from datetime import datetime
from typing import Dict, Tuple, Optional

import ccxt
import schedule
from dotenv import load_dotenv
from binance import ThreadedWebsocketManager


# =============================================================================
# Logging + .env
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("dca_bot.log"), logging.StreamHandler()],
)

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=ENV_PATH)

DCA_LOCK = threading.Lock()


# =============================================================================
# Telegram
# =============================================================================

def tg_enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(os.getenv("TELEGRAM_CHAT_ID"))


def tg_send(text: str) -> None:
    if not tg_enabled():
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")


def tg_poll_status_commands(stop_event: threading.Event) -> None:
    """Minimal Telegram long-polling for commands."""

    if not tg_enabled():
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID"))
    poll_interval = int(os.getenv("TELEGRAM_POLL_INTERVAL", "5"))
    offset = None
    url = f"https://api.telegram.org/bot{token}/getUpdates"

    logging.info("📡 Telegram command polling enabled")

    while not stop_event.is_set():
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset

            r = requests.get(url, params=params, timeout=40)
            data = r.json() if r.ok else {}

            for upd in data.get("result", []) or []:
                offset = upd.get("update_id", 0) + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                # Only accept commands from configured chat_id
                from_chat = str((msg.get("chat") or {}).get("id", ""))
                if from_chat != chat_id:
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                parts = text.split()
                cmd = parts[0].lower()
                arg1 = parts[1].lower() if len(parts) > 1 else ""

                if cmd in ("/status", "status", "статус"):
                    tg_send(build_status_message())

                elif cmd in ("/preview_dca", "preview_dca", "преглед"):
                    send_dca_preview_to_telegram()

                elif cmd in ("/run_orders", "run_orders"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To run orders now, send: /run_orders confirm")
                    else:
                        run_orders_now(trigger="telegram")

                elif cmd in ("/run_dca", "run_dca"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To run full DCA now, send: /run_dca confirm")
                    else:
                        run_dca_now(trigger="telegram")

                # ---- CARRY RESET COMMANDS ----
                elif cmd in ("/reset_carry", "reset_carry"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To reset carry (active symbols), send: /reset_carry confirm")
                    else:
                        for sym in WEIGHTS.keys():
                            reset_carry_symbol(sym)
                        tg_send("✅ Carry reset for active symbols (WEIGHTS).")

                elif cmd in ("/reset_carry_all", "reset_carry_all"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To FULL reset carry, send: /reset_carry_all confirm")
                    else:
                        reset_carry_all()
                        tg_send("✅ Carry reset for ALL symbols (full).")

                elif cmd in ("/reset_btc", "reset_btc"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To reset BTC carry, send: /reset_btc confirm")
                    else:
                        reset_carry_symbol("BTC/USDC")
                        tg_send("✅ Carry reset: BTC/USDC")

                elif cmd in ("/reset_sol", "reset_sol"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To reset SOL carry, send: /reset_sol confirm")
                    else:
                        reset_carry_symbol("SOL/USDC")
                        tg_send("✅ Carry reset: SOL/USDC")

                elif cmd in ("/reset_eth", "reset_eth"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To reset ETH carry, send: /reset_eth confirm")
                    else:
                        reset_carry_symbol("ETH/USDC")
                        tg_send("✅ Carry reset: ETH/USDC")

                elif cmd in ("/reset_ada", "reset_ada"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To reset ADA carry, send: /reset_ada confirm")
                    else:
                        reset_carry_symbol("ADA/USDC")
                        tg_send("✅ Carry reset: ADA/USDC")

                elif cmd in ("/reset_xrp", "reset_xrp"):
                    if arg1 != "confirm":
                        tg_send("⚠️ To reset XRP carry, send: /reset_xrp confirm")
                    else:
                        reset_carry_symbol("XRP/USDC")
                        tg_send("✅ Carry reset: XRP/USDC")

                elif cmd in ("/help", "help"):
                    tg_send(
                        "Commands:\n Команди\n"
                        "/status\n Статус\n"
                        "/preview_dca\n Преглед на DCA\n"
                        "/run_orders confirm\n Пуска само натрупани поръчки в carry\n"
                        "/run_dca confirm\n Пуска нов DCA и изпълнява\n"
                        "/reset_carry confirm\n Занулява carry за активните символи (по WEIGHTS). Внимание: губиш натрупан, но неизползван бюджет\n"
                        "/reset_carry_all confirm\nПЪЛНО зануляване на carry за всички символи.\n"
                        "/reset_btc confirm\n"
                        "/reset_sol confirm\n"
                        "/reset_eth confirm\n"
                        "/reset_ada confirm\n"
                        "/reset_xrp confirm\n"
                        "/help"
                    )

        except Exception as e:
            logging.warning(f"Telegram polling error: {e}")

        time.sleep(poll_interval)


# =============================================================================
# Config
# =============================================================================

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

DAILY_BUDGET_USDC = float(os.getenv("DAILY_BUDGET_USDC", "12"))

# Adaptive limit offset (based on 24h abs % change)
OFFSET_MIN = float(os.getenv("OFFSET_MIN", "0.05"))  # percent
OFFSET_MAX = float(os.getenv("OFFSET_MAX", "0.50"))  # percent
VOL_LOW = float(os.getenv("VOL_LOW", "0.5"))         # percent
VOL_HIGH = float(os.getenv("VOL_HIGH", "3.0"))       # percent

# Symbols
BTCUSDC = "BTC/USDC"
SOLUSDC = "SOL/USDC"
ETHUSDC = "ETH/USDC"
ADAUSDC = "ADA/USDC"
XRPUSDC = "XRP/USDC"

ALL_SYMBOLS = [BTCUSDC, SOLUSDC, ETHUSDC, ADAUSDC, XRPUSDC]

# Weights
BTC_WEIGHT = float(os.getenv("BTC_WEIGHT", "0.75"))
SOL_WEIGHT = float(os.getenv("SOL_WEIGHT", "0.25"))
ETH_WEIGHT = float(os.getenv("ETH_WEIGHT", "0.0"))
ADA_WEIGHT = float(os.getenv("ADA_WEIGHT", "0.0"))
XRP_WEIGHT = float(os.getenv("XRP_WEIGHT", "0.0"))

WEIGHTS = {
    BTCUSDC: BTC_WEIGHT,
    SOLUSDC: SOL_WEIGHT,
    ETHUSDC: ETH_WEIGHT,
    ADAUSDC: ADA_WEIGHT,
    XRPUSDC: XRP_WEIGHT,
}

# Execution
LIMIT_OFFSET_PERCENT = float(os.getenv("LIMIT_OFFSET_PERCENT", "0.15"))
ADJUST_OFFSET_PERCENT = float(os.getenv("ADJUST_OFFSET_PERCENT", "0.02"))

# Multiplier clamps
BUDGET_MULT_MIN = float(os.getenv("BUDGET_MULT_MIN", "0.5"))

# Per-asset cap max (base cap before EMA-regime multiplier)
BUDGET_MULT_MAX_BTC = float(os.getenv("BUDGET_MULT_MAX_BTC", "2.0"))
BUDGET_MULT_MAX_SOL = float(os.getenv("BUDGET_MULT_MAX_SOL", "1.6"))
BUDGET_MULT_MAX_ETH = float(os.getenv("BUDGET_MULT_MAX_ETH", "1.6"))
BUDGET_MULT_MAX_ADA = float(os.getenv("BUDGET_MULT_MAX_ADA", "1.6"))
BUDGET_MULT_MAX_XRP = float(os.getenv("BUDGET_MULT_MAX_XRP", "1.6"))

# 24h thresholds (%)
CHG_UP_1 = float(os.getenv("CHG_UP_1", "5"))
CHG_UP_2 = float(os.getenv("CHG_UP_2", "10"))
CHG_DN_1 = float(os.getenv("CHG_DN_1", "-5"))
CHG_DN_2 = float(os.getenv("CHG_DN_2", "-10"))

# 24h multipliers
MULT_UP_1 = float(os.getenv("MULT_UP_1", "0.8"))
MULT_UP_2 = float(os.getenv("MULT_UP_2", "0.6"))
MULT_DN_1 = float(os.getenv("MULT_DN_1", "1.3"))
MULT_DN_2 = float(os.getenv("MULT_DN_2", "1.6"))

# 200DMA ratio thresholds (price/200DMA)
RATIO_LOW_1 = float(os.getenv("RATIO_LOW_1", "0.85"))
RATIO_LOW_2 = float(os.getenv("RATIO_LOW_2", "0.75"))
RATIO_HIGH_1 = float(os.getenv("RATIO_HIGH_1", "1.15"))
RATIO_HIGH_2 = float(os.getenv("RATIO_HIGH_2", "1.30"))
RATIO_HIGH_3 = float(os.getenv("RATIO_HIGH_3", "1.50"))

# 200DMA multipliers
MA_LOW_1 = float(os.getenv("MA_LOW_1", "1.2"))
MA_LOW_2 = float(os.getenv("MA_LOW_2", "1.5"))
MA_HIGH_1 = float(os.getenv("MA_HIGH_1", "0.8"))
MA_HIGH_2 = float(os.getenv("MA_HIGH_2", "0.6"))
MA_HIGH_3 = float(os.getenv("MA_HIGH_3", "0.4"))

# ATR sizing (requested: always influence when enabled)
ATR_ENABLED = int(os.getenv("ATR_ENABLED", "1")) == 1
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_LOW = float(os.getenv("ATR_LOW", "0.03"))
ATR_HIGH = float(os.getenv("ATR_HIGH", "0.06"))
ATR_MULT_LOW = float(os.getenv("ATR_MULT_LOW", "0.95"))
ATR_MULT_HIGH = float(os.getenv("ATR_MULT_HIGH", "0.85"))

# EMA regime for cap-max adjustment
EMA_ENABLED = int(os.getenv("EMA_ENABLED", "1")) == 1
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "50"))
REGIME_BULL_CAP_MULT = float(os.getenv("REGIME_BULL_CAP_MULT", "0.85"))
REGIME_BEAR_CAP_MULT = float(os.getenv("REGIME_BEAR_CAP_MULT", "1.25"))
REGIME_NEUTRAL_CAP_MULT = float(os.getenv("REGIME_NEUTRAL_CAP_MULT", "1.0"))

# Times
DCA_TIME = os.getenv("DCA_TIME", "06:00")
CHECK_TIME = os.getenv("CHECK_TIME", "19:00")

# Min notional fallback
MIN_NOTIONAL_FALLBACK = float(os.getenv("MIN_NOTIONAL", "5.0"))

# Targets
BTC_TARGET = float(os.getenv("BTC_TARGET", "1.0"))
PORTFOLIO_GOAL_USDC = float(os.getenv("PORTFOLIO_GOAL_USDC", "50000"))

# Persistence
CARRY_FILE = os.path.join(os.path.dirname(__file__), "carry_state.json")
POSITION_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
OPEN_ORDERS_FILE = os.path.join(os.path.dirname(__file__), "open_orders.json")

# Portfolio symbols (tracked for value/pnl)
PORTFOLIO_SYMBOLS = ALL_SYMBOLS


if not API_KEY or not API_SECRET:
    logging.error("Missing BINANCE_API_KEY / BINANCE_API_SECRET in .env")
    sys.exit(1)

if LIMIT_OFFSET_PERCENT <= 0 or LIMIT_OFFSET_PERCENT > 5:
    logging.error("LIMIT_OFFSET_PERCENT looks wrong. Use something like 0.10 - 0.50")
    sys.exit(1)

active_weights_sum = sum(w for w in WEIGHTS.values() if w > 0)
if active_weights_sum <= 0:
    logging.error("All weights are 0. Set at least one weight > 0")
    sys.exit(1)

if abs(active_weights_sum - 1.0) > 1e-6:
    logging.warning(
        f"⚠️ Weights sum is {active_weights_sum:.6f} (not 1.0). Budgets will be DAILY_BUDGET_USDC * weight."
    )


# =============================================================================
# Exchange
# =============================================================================

exchange = ccxt.binance(
    {
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    }
)


# =============================================================================
# Persistence
# =============================================================================

def load_json(filepath: str, default=None):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load {filepath}: {e}")
    return default if default is not None else {}


def save_json(filepath: str, data) -> None:
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to save {filepath}: {e}")


carry_state: Dict[str, float] = {}
positions: Dict[str, dict] = {}
OPEN_ORDERS: Dict[str, dict] = {}


def save_carry() -> None:
    save_json(CARRY_FILE, carry_state)


def save_positions() -> None:
    save_json(POSITION_FILE, positions)


def save_open_orders() -> None:
    save_json(OPEN_ORDERS_FILE, OPEN_ORDERS)


def load_state() -> None:
    global carry_state, positions, OPEN_ORDERS

    carry_state = load_json(CARRY_FILE, {})
    positions = load_json(POSITION_FILE, {})
    raw_open = load_json(OPEN_ORDERS_FILE, {})

    migrated: Dict[str, dict] = {}
    if isinstance(raw_open, dict):
        for sym, v in raw_open.items():
            if isinstance(v, str):
                migrated[sym] = {"id": v, "planned_spend_usdc": None}
            elif isinstance(v, dict) and "id" in v:
                migrated[sym] = {"id": str(v.get("id")), "planned_spend_usdc": v.get("planned_spend_usdc")}

    OPEN_ORDERS = migrated

    logging.info(
        f"📂 Loaded state: carry={list(carry_state.keys())}, positions={list(positions.keys())}, open_orders={list(OPEN_ORDERS.keys())}"
    )


# =============================================================================
# Helpers
# =============================================================================
ORDER_PRIORITY = ["BTC/USDC", "SOL/USDC", "ETH/USDC", "ADA/USDC", "XRP/USDC"]

def iter_symbols_btc_first(symbols):
    ordered = [s for s in ORDER_PRIORITY if s in symbols]
    ordered += [s for s in symbols if s not in ordered]
    return ordered


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def test_connection_and_markets() -> bool:
    try:
        exchange.load_markets()
        exchange.fetch_balance()

        for sym, w in WEIGHTS.items():
            if w <= 0:
                continue
            if sym not in exchange.markets:
                logging.error(f"Market not available on this account/API: {sym}")
                return False
            exchange.fetch_ticker(sym)

        logging.info("✅ Connection OK + markets OK")
        return True

    except ccxt.AuthenticationError as e:
        logging.error(f"❌ AuthenticationError: {e}")
        return False
    except ccxt.PermissionDenied as e:
        logging.error(f"❌ PermissionDenied: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ Connection test failed: {e}")
        return False


def get_free_balance(asset: str) -> float:
    bal = exchange.fetch_balance()
    return _safe_float((bal.get("free") or {}).get(asset, 0.0), 0.0)


def compute_limit_buy_qty(symbol: str, spend_quote: float, limit_price: float) -> float:
    qty = spend_quote / limit_price
    return float(exchange.amount_to_precision(symbol, qty))


def get_min_notional(symbol: str) -> float:
    market = exchange.market(symbol)
    mn = _safe_float((market.get("limits") or {}).get("cost", {}).get("min", 0.0), 0.0)
    return mn if mn > 0 else MIN_NOTIONAL_FALLBACK


def active_symbols() -> list[str]:
    return [sym for sym, w in WEIGHTS.items() if w > 0]


def get_cap_base(symbol: str) -> float:
    if symbol == BTCUSDC:
        return BUDGET_MULT_MAX_BTC
    if symbol == SOLUSDC:
        return BUDGET_MULT_MAX_SOL
    if symbol == ETHUSDC:
        return BUDGET_MULT_MAX_ETH
    if symbol == ADAUSDC:
        return BUDGET_MULT_MAX_ADA
    if symbol == XRPUSDC:
        return BUDGET_MULT_MAX_XRP
    return BUDGET_MULT_MAX_BTC


# =============================================================================
# Adaptive limit offset (24h)
# =============================================================================

def get_dynamic_offset_percent(symbol: str) -> float:
    """Adaptive limit offset (%) based on abs(24h % change)."""
    try:
        t = exchange.fetch_ticker(symbol)
        chg = t.get("percentage")
        if chg is None:
            return LIMIT_OFFSET_PERCENT
        vol = abs(float(chg))
    except Exception:
        return LIMIT_OFFSET_PERCENT

    if VOL_HIGH <= VOL_LOW:
        return LIMIT_OFFSET_PERCENT

    x = (vol - VOL_LOW) / (VOL_HIGH - VOL_LOW)
    x = max(0.0, min(1.0, x))
    return OFFSET_MIN + x * (OFFSET_MAX - OFFSET_MIN)


def format_dynamic_offsets_status() -> str:
    lines = []
    for sym in active_symbols():
        off = get_dynamic_offset_percent(sym)
        try:
            t = exchange.fetch_ticker(sym)
            chg = t.get("percentage")
            chg = float(chg) if chg is not None else None
        except Exception:
            chg = None

        base = sym.split("/")[0]
        if chg is None:
            lines.append(f"{base}: offset={off:.3f}% | 24h n/a")
        else:
            lines.append(f"{base}: offset={off:.3f}% | 24h {chg:+.2f}%")

    return "\n".join(lines) if lines else "(no active weights)"


# =============================================================================
# Indicators: 200DMA, EMA, ATR
# =============================================================================

def get_symbol_200dma(symbol: str) -> float:
    """200-day SMA of daily closes."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=200)
        if not ohlcv or len(ohlcv) < 50:
            return 0.0
        closes = [float(c[4]) for c in ohlcv if c and c[4] is not None]
        if not closes:
            return 0.0
        return sum(closes) / len(closes)
    except Exception as e:
        logging.warning(f"get_symbol_200dma error for {symbol}: {e}")
        return 0.0


def get_symbol_ema(symbol: str, period: int = 50) -> float:
    """EMA(period) of daily closes."""
    try:
        limit = max(period * 3, period + 2)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=limit)
        if not ohlcv or len(ohlcv) < period:
            return 0.0
        closes = [float(c[4]) for c in ohlcv if c and c[4] is not None]
        if len(closes) < period:
            return 0.0

        k = 2.0 / (period + 1.0)
        ema = sum(closes[:period]) / period
        for c in closes[period:]:
            ema = (c * k) + (ema * (1.0 - k))
        return float(ema)

    except Exception as e:
        logging.warning(f"get_symbol_ema error for {symbol}: {e}")
        return 0.0


def get_symbol_atr(symbol: str, period: int = 14) -> float:
    """Daily ATR (simple average True Range over period)."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=period + 1)
        if not ohlcv or len(ohlcv) < period + 1:
            return 0.0

        trs = []
        prev_close = None
        for c in ohlcv:
            _ts, _o, h, l, close, _v = c
            if prev_close is None:
                prev_close = close
                continue

            tr = max(
                float(h) - float(l),
                abs(float(h) - float(prev_close)),
                abs(float(l) - float(prev_close)),
            )
            trs.append(tr)
            prev_close = close

        if not trs:
            return 0.0
        return sum(trs) / len(trs)

    except Exception as e:
        logging.warning(f"get_symbol_atr error for {symbol}: {e}")
        return 0.0


def get_atr_budget_multiplier(symbol: str, price: float) -> Tuple[float, float]:
    """Return (vol_mult, atr_pct). Higher ATR% => smaller vol_mult."""
    if price <= 0:
        return 1.0, 0.0

    atr = get_symbol_atr(symbol, period=ATR_PERIOD)
    if atr <= 0:
        return 1.0, 0.0

    atr_pct = atr / price

    if atr_pct >= ATR_HIGH:
        return ATR_MULT_HIGH, atr_pct
    if atr_pct >= ATR_LOW:
        return ATR_MULT_LOW, atr_pct
    return 1.0, atr_pct


def get_regime_cap_multiplier(symbol: str, price: float, ma200: float) -> Tuple[float, str, float]:
    """Return (cap_mult, regime, ema_val)."""
    if not EMA_ENABLED or price <= 0 or ma200 <= 0:
        return REGIME_NEUTRAL_CAP_MULT, "neutral", 0.0

    ema_val = get_symbol_ema(symbol, period=EMA_PERIOD)
    if ema_val <= 0:
        return REGIME_NEUTRAL_CAP_MULT, "neutral", ema_val

    if price > ma200 and ema_val > ma200:
        return REGIME_BULL_CAP_MULT, "bull", ema_val

    if price < ma200 and ema_val < ma200:
        return REGIME_BEAR_CAP_MULT, "bear", ema_val

    return REGIME_NEUTRAL_CAP_MULT, "neutral", ema_val


# =============================================================================
# Budget multiplier (applies to all active symbols)
# =============================================================================

def get_budget_multiplier(symbol: str) -> float:
    """24h + 200DMA + ATR sizing + EMA regime cap, clamped."""

    try:
        t = exchange.fetch_ticker(symbol)
        change_24h = t.get("percentage")
        price = float(t.get("last") or 0.0)
    except Exception as e:
        logging.warning(f"get_budget_multiplier ticker error for {symbol}: {e}")
        return 1.0

    if price <= 0:
        return 1.0

    # --- 24h move rule ---
    base_mult = 1.0
    try:
        if change_24h is not None:
            chg = float(change_24h)
            if chg <= CHG_DN_2:
                base_mult = MULT_DN_2
            elif chg <= CHG_DN_1:
                base_mult = MULT_DN_1
            elif chg >= CHG_UP_2:
                base_mult = MULT_UP_2
            elif chg >= CHG_UP_1:
                base_mult = MULT_UP_1
    except Exception:
        pass

    # --- 200DMA ratio rule ---
    ma200 = get_symbol_200dma(symbol)
    if ma200 > 0:
        ratio = price / ma200
        long_mult = 1.0
        if ratio <= RATIO_LOW_2:
            long_mult = MA_LOW_2
        elif ratio <= RATIO_LOW_1:
            long_mult = MA_LOW_1
        elif ratio >= RATIO_HIGH_3:
            long_mult = MA_HIGH_3
        elif ratio >= RATIO_HIGH_2:
            long_mult = MA_HIGH_2
        elif ratio >= RATIO_HIGH_1:
            long_mult = MA_HIGH_1
        mult = base_mult * long_mult
    else:
        mult = base_mult

    # --- ATR sizing (always influences when enabled) ---
    if ATR_ENABLED:
        vol_mult, _atr_pct = get_atr_budget_multiplier(symbol, price=price)
        mult = mult * vol_mult

    # --- cap max adjusted by EMA regime ---
    cap_base = get_cap_base(symbol)
    cap_mult, _regime, _ema = get_regime_cap_multiplier(symbol, price=price, ma200=ma200 if ma200 > 0 else 0.0)
    cap_max = cap_base * cap_mult
    cap_max = max(BUDGET_MULT_MIN, cap_max)

    mult = max(BUDGET_MULT_MIN, min(cap_max, mult))
    return mult


def format_symbol_context(symbol: str) -> str:
    """Compact indicator/context line for Telegram preview."""
    try:
        t = exchange.fetch_ticker(symbol)
        chg = t.get("percentage")
        chg = float(chg) if chg is not None else 0.0
        price = float(t.get("last") or 0.0)
    except Exception:
        return f"{symbol}: n/a"

    ma200 = get_symbol_200dma(symbol)
    ratio = (price / ma200) if (price > 0 and ma200 > 0) else 0.0

    cap_mult, regime, ema_val = get_regime_cap_multiplier(symbol, price=price, ma200=ma200 if ma200 > 0 else 0.0)

    vol_mult = 1.0
    atr_pct = 0.0
    if ATR_ENABLED and price > 0:
        vol_mult, atr_pct = get_atr_budget_multiplier(symbol, price=price)

    short = symbol.replace("/USDC", "")
    return (
        f"{short}: price={price:.2f}, 24h {chg:+.2f}%, 200DMA={ma200:.2f}, ratio={ratio:.2f}x, "
        f"EMA{EMA_PERIOD}={ema_val:.2f}, regime={regime}, capMult={cap_mult:.2f}x, "
        f"ATR%={(atr_pct*100):.2f}% volMult={vol_mult:.2f}x"
    )


# =============================================================================
# BTC target + portfolio
# =============================================================================

def progress_bar(current: float, goal: float, width: int = 20) -> str:
    if goal <= 0:
        return "[?]"
    ratio = max(0.0, min(1.0, current / goal))
    filled = int(round(ratio * width))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + f"] {ratio*100:.1f}%"


def btc_target_status_line() -> str:
    pos = positions.get(BTCUSDC) or {}
    have = float(pos.get("total_qty", 0.0) or 0.0)
    remaining_btc = max(0.0, BTC_TARGET - have)

    remaining_usdc = None
    try:
        t = exchange.fetch_ticker(BTCUSDC)
        spot = float(t.get("last") or 0.0)
        if spot > 0:
            remaining_usdc = remaining_btc * spot
    except Exception:
        pass

    bar = progress_bar(have, BTC_TARGET, width=20)
    if remaining_usdc is not None:
        return (
            f"🎯 BTC target: {have:.8f}/{BTC_TARGET:.8f} BTC\n"
            f"{bar}\n"
            f"Remaining: {remaining_btc:.8f} BTC ≈ {remaining_usdc:.2f} USDC"
        )

    return (
        f"🎯 BTC target: {have:.8f}/{BTC_TARGET:.8f} BTC\n"
        f"{bar}\n"
        f"Remaining: {remaining_btc:.8f} BTC"
    )


def format_portfolio_allocation() -> Tuple[str, float, float, float]:
    values = {}
    total_value = 0.0
    total_cost = 0.0

    for sym in PORTFOLIO_SYMBOLS:
        pos = positions.get(sym)
        if not pos:
            continue

        qty = float(pos.get("total_qty", 0.0) or 0.0)
        cost = float(pos.get("total_cost_usdc", 0.0) or 0.0)
        if qty <= 0 or cost <= 0:
            continue

        price = 0.0
        try:
            t = exchange.fetch_ticker(sym)
            price = float(t.get("last") or 0.0)
        except Exception:
            price = 0.0

        if price <= 0:
            price = float(pos.get("avg_price", 0.0) or 0.0)

        if price <= 0:
            continue

        value_usdc = qty * price
        pnl_usdc = value_usdc - cost
        pnl_pct = (pnl_usdc / cost * 100.0) if cost > 0 else 0.0

        values[sym] = (qty, value_usdc, cost, pnl_usdc, pnl_pct)
        total_value += value_usdc
        total_cost += cost

    if not values:
        return "(no valued positions yet)", 0.0, 0.0, 0.0

    lines = []
    for sym in PORTFOLIO_SYMBOLS:
        if sym not in values:
            continue

        qty, val, cost, pnl_usdc, pnl_pct = values[sym]
        base = sym.split("/")[0]
        alloc_pct = (val / total_value * 100.0) if total_value > 0 else 0.0
        sign = "+" if pnl_usdc >= 0 else ""
        emoji = "🟢" if pnl_usdc >= 0 else "🔴"

        lines.append(
            f"{base}: {alloc_pct:.2f}% ({qty:.8f} {base} ≈ {val:.2f} USDC)\n"
            f" {emoji} P&L: {sign}{pnl_usdc:.2f} USDC ({sign}{pnl_pct:.2f}%)"
        )

    total_pnl_usdc = total_value - total_cost
    total_pnl_pct = (total_pnl_usdc / total_cost * 100.0) if total_cost > 0 else 0.0

    return "\n".join(lines), total_value, total_pnl_usdc, total_pnl_pct


# =============================================================================
# Orders + position tracking
# =============================================================================

def place_post_only_limit_buy(symbol: str, spend_usdc: float, offset_pct: float) -> Optional[dict]:
    try:
        ticker = exchange.fetch_ticker(symbol)
        spot = _safe_float(ticker.get("last"), 0.0)
        if spot <= 0:
            raise RuntimeError("Invalid spot price")

        limit_price = spot * (1 - offset_pct / 100.0)
        limit_price = float(exchange.price_to_precision(symbol, limit_price))

        qty = compute_limit_buy_qty(symbol, spend_usdc, limit_price)

        notional = qty * limit_price
        min_notional = get_min_notional(symbol)
        if min_notional and notional < min_notional:
            logging.warning(
                f"⚠️ {symbol} notional {notional:.4f} < minNotional {min_notional}. Increase budget or skip."
            )
            return None

        order = exchange.create_limit_buy_order(
            symbol=symbol,
            amount=qty,
            price=limit_price,
            params={"postOnly": True},
        )

        logging.info(
            f"✅ Placed {symbol} post-only LIMIT BUY | spend≈{spend_usdc:.2f} USDC | qty={qty} | price={limit_price} | id={order.get('id')}"
        )

        tg_send(
            f"🟢 PLACED {symbol}\n"
            f"├ Spend≈{spend_usdc:.2f} USDC\n"
            f"├ Price={limit_price}\n"
            f"├ Qty={qty}\n"
            f"└ ID={order.get('id')}"
        )

        return order

    except ccxt.InvalidOrder as e:
        logging.error(f"❌ InvalidOrder for {symbol}: {e}")
        return None
    except Exception as e:
        logging.error(f"❌ Failed placing order for {symbol}: {e}")
        return None


def cancel_order_safe(symbol: str, order_id: str) -> bool:
    """Cancel safely; returns True only if cancel confirmed."""
    try:
        exchange.cancel_order(order_id, symbol)
        logging.info(f"🧹 Canceled {symbol} order id={order_id}")
        tg_send(f"🟡 CANCELED {symbol} id={order_id}")
        return True
    except Exception as e:
        msg = str(e)
        if "-2011" in msg or "Unknown order sent" in msg:
            logging.info(f"ℹ️ Cancel skipped (already closed): {symbol} id={order_id}")
            return False
        logging.warning(f"⚠️ Failed to cancel {symbol} order id={order_id}: {e}")
        return False


def update_position(symbol: str, filled_qty: float, filled_price: float, filled_cost: float) -> None:
    if symbol not in positions:
        positions[symbol] = {"total_qty": 0.0, "total_cost_usdc": 0.0, "avg_price": 0.0}

    pos = positions[symbol]
    pos["total_qty"] = float(pos.get("total_qty", 0.0) or 0.0) + filled_qty
    pos["total_cost_usdc"] = float(pos.get("total_cost_usdc", 0.0) or 0.0) + filled_cost
    pos["avg_price"] = pos["total_cost_usdc"] / pos["total_qty"] if pos["total_qty"] > 0 else 0.0

    save_positions()


def format_positions_summary() -> str:
    if not positions:
        return "(no positions yet)"

    lines = []
    for sym in sorted(positions.keys()):
        base = sym.split("/")[0]
        p = positions[sym]
        lines.append(
            f"{base}: {float(p.get('total_qty', 0.0) or 0.0):.8f} | "
            f"avg {float(p.get('avg_price', 0.0) or 0.0):.2f} | "
            f"cost {float(p.get('total_cost_usdc', 0.0) or 0.0):.2f} USDC"
        )

    return "\n".join(lines)


def format_open_orders_summary() -> str:
    if not OPEN_ORDERS:
        return "(no open orders tracked)"

    lines = []
    for sym in sorted(OPEN_ORDERS.keys()):
        o = OPEN_ORDERS[sym]
        lines.append(
            f"{sym}: id={o.get('id')} | planned≈{_safe_float(o.get('planned_spend_usdc'), 0.0):.2f} USDC"
        )

    return "\n".join(lines)


def format_weights_with_amounts() -> str:
    lines = []
    sum_w = sum(WEIGHTS.values())
    for sym, w in WEIGHTS.items():
        planned = DAILY_BUDGET_USDC * w
        base = sym.split("/")[0]
        lines.append(f"{base}: {w*100:.2f}% (≈ {planned:.2f} USDC)")
    total_planned = DAILY_BUDGET_USDC * sum_w
    lines.append(f"Total planned: {total_planned:.2f} USDC")
    return "\n".join(lines)


def build_status_message() -> str:
    usdc = 0.0
    try:
        usdc = get_free_balance("USDC")
    except Exception:
        pass

    btc_line = btc_target_status_line()
    portfolio_text, total_value, total_pnl_usdc, total_pnl_pct = format_portfolio_allocation()

    goal_line = (
        f"🏁 Goal: {progress_bar(total_value, PORTFOLIO_GOAL_USDC)} "
        f"({total_value:,.2f} / {PORTFOLIO_GOAL_USDC:,.2f} USDC)"
    )

    sign = "+" if total_pnl_usdc >= 0 else ""
    emoji = "🟢" if total_pnl_usdc >= 0 else "🔴"
    total_pnl_line = f"{emoji} TOTAL P&L: {sign}{total_pnl_usdc:.2f} USDC ({sign}{total_pnl_pct:.2f}%)"

    carry_lines = []
    for sym in sorted(carry_state.keys()):
        carry_lines.append(f"{sym}: {carry_state.get(sym, 0.0):.2f} USDC")
    carry_text = "\n".join(carry_lines) if carry_lines else "(none)"

    dyn_lines = []
    try:
        alloc = compute_today_allocations()
        for sym in sorted(alloc.keys()):
            amt = float(alloc.get(sym, 0.0) or 0.0)
            mult = get_budget_multiplier(sym)
            base = sym.split("/")[0]
            dyn_lines.append(f"{base}: {amt:.2f} USDC (mult={mult:.2f}x)")
    except Exception:
        pass
    dyn_text = "\n".join(dyn_lines) if dyn_lines else "(n/a)"

    return (
        f"🤖 Status\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"USDC free: {usdc:.2f}\n\n"
        f"{goal_line}\n\n"
        f"Targets\n{btc_line}\n\n"
        f"Portfolio (market value)\n{portfolio_text}\n\n"
        f"{total_pnl_line}\n\n"
        f"Config\n"
        f"Daily budget: {DAILY_BUDGET_USDC:.2f} USDC\n"
        f"Weights:\n{format_weights_with_amounts()}\n\n"
        f"DCA allocation today (dynamic)\n{dyn_text}\n\n"
        f"Adaptive offsets (24h)\n{format_dynamic_offsets_status()}\n\n"
        f"Carry (unspent)\n{carry_text}\n\n"
        f"Open orders\n{format_open_orders_summary()}\n\n"
        f"Positions\n{format_positions_summary()}"
    )


# =============================================================================
# Websocket (FILLED)
# =============================================================================

WS_SYMBOL_MAP = {
    "BTCUSDC": BTCUSDC,
    "SOLUSDC": SOLUSDC,
    "ETHUSDC": ETHUSDC,
    "ADAUSDC": ADAUSDC,
    "XRPUSDC": XRPUSDC,
}


def ws_handle_user_event(msg: dict) -> None:
    """Notify only on FILLED and update position."""
    try:
        if not isinstance(msg, dict):
            return
        if msg.get("e") != "executionReport":
            return
        if msg.get("X") != "FILLED":
            return

        ws_symbol = msg.get("s")
        symbol = WS_SYMBOL_MAP.get(ws_symbol)
        if not symbol:
            return

        order_id = str(msg.get("i"))
        cum_qty_f = _safe_float(msg.get("z"), 0.0)
        quote_cum_f = _safe_float(msg.get("Z"), 0.0)
        last_price_f = _safe_float(msg.get("L"), 0.0)

        avg_price = (quote_cum_f / cum_qty_f) if cum_qty_f > 0 else last_price_f
        update_position(symbol, cum_qty_f, avg_price, quote_cum_f)

        tracked = OPEN_ORDERS.get(symbol)
        if tracked and str(tracked.get("id")) == order_id:
            OPEN_ORDERS.pop(symbol, None)
            save_open_orders()

        base = symbol.split("/")[0]
        pos = positions.get(symbol, {})

        tg_send(
            f"✅ FILLED {symbol}\n"
            f"├ Qty: {cum_qty_f:.8f}\n"
            f"├ Avg: {avg_price:.2f} USDC\n"
            f"├ Cost: {quote_cum_f:.2f} USDC\n"
            f"└ ID: {order_id}\n\n"
            f"📊 Position\n"
            f"├ Total {base}: {float(pos.get('total_qty', 0.0) or 0.0):.8f}\n"
            f"├ Avg Price: {float(pos.get('avg_price', 0.0) or 0.0):.2f} USDC\n"
            f"└ Total Cost: {float(pos.get('total_cost_usdc', 0.0) or 0.0):.2f} USDC"
        )

    except Exception as e:
        logging.error(f"WS handler error: {e}")


# =============================================================================
# Allocation + carry
# =============================================================================

def compute_today_allocations() -> Dict[str, float]:
    """Per-symbol budget from weights, with dynamic multiplier for all active symbols."""

    alloc: Dict[str, float] = {}

    for sym, w in WEIGHTS.items():
        if w <= 0:
            continue

        base_budget = DAILY_BUDGET_USDC * w
        mult = get_budget_multiplier(sym)
        final_budget = base_budget * mult

        alloc[sym] = final_budget

        logging.info(
            f"Budget {sym}: weight={w:.4f} base={base_budget:.2f} mult={mult:.2f} final={final_budget:.2f}"
        )

    return alloc


def add_to_carry(symbol: str, amount_usdc: float) -> float:
    carry_state.setdefault(symbol, 0.0)
    carry_state[symbol] += amount_usdc
    save_carry()
    return carry_state[symbol]


def reset_carry_symbol(symbol: str) -> None:
    carry_state[symbol] = 0.0
    save_carry()


def reset_carry_all() -> None:
    for s in list(carry_state.keys()):
        carry_state[s] = 0.0
    save_carry()


def try_place_from_carry(symbol: str) -> None:
    """If spend=min(carry, USDC free) >= minNotional and not tracked as open => place order."""
    if symbol in OPEN_ORDERS:
        return

    min_notional = get_min_notional(symbol)
    carry = _safe_float(carry_state.get(symbol, 0.0), 0.0)

    usdc_free = _safe_float(get_free_balance("USDC"), 0.0)
    spend = min(carry, usdc_free)

    if spend < min_notional:
        logging.info(
            f"i {symbol} spend={spend:.2f} < minNotional={min_notional:.2f} "
            f"(carry={carry:.2f}, usdc_free={usdc_free:.2f}) => skip"
        )
        return

    dyn_offset = get_dynamic_offset_percent(symbol)
    order = place_post_only_limit_buy(symbol, spend, dyn_offset)

    if order and order.get("id"):
        OPEN_ORDERS[symbol] = {"id": str(order["id"]), "planned_spend_usdc": spend}
        save_open_orders()

        carry_state[symbol] = max(0.0, carry_state.get(symbol, 0.0) - spend)
        save_carry()


# =============================================================================
# Jobs
# =============================================================================

def send_dca_preview_to_telegram() -> None:
    if not tg_enabled():
        return

    try:
        lines = ["📈 Market context"]
        for sym in active_symbols():
            lines.append(format_symbol_context(sym))

        alloc_preview = compute_today_allocations()
        lines.append("")
        lines.append("📊 DCA allocation today (preview)")

        for sym, amt in alloc_preview.items():
            base = sym.split("/")[0]
            mult = get_budget_multiplier(sym)
            lines.append(f"{base}: {amt:.2f} USDC (mult={mult:.2f}x)")

        tg_send("\n".join(lines))

    except Exception as e:
        logging.error(f"send_dca_preview_to_telegram error: {e}")
        tg_send(f"❌ Preview error: {e}")


def daily_dca_job() -> None:
    logging.info("=" * 60)
    logging.info(f"DAILY DCA JOB @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 60)

    usdc_free = get_free_balance("USDC")
    logging.info(f"USDC free balance: {usdc_free:.2f}")

    alloc = compute_today_allocations()
    total_budget = sum(alloc.values())

    if usdc_free < total_budget:
        logging.warning(
            f"⚠️ Not enough USDC for full DCA. Need {total_budget:.2f}, have {usdc_free:.2f}. Will still try."
        )

    if tg_enabled():
        try:
            lines = ["📈 Market context"]
            for sym in active_symbols():
                lines.append(format_symbol_context(sym))
            lines.append("")
            lines.append("📊 DCA allocation today")
            for sym, amt in alloc.items():
                base = sym.split("/")[0]
                mult = get_budget_multiplier(sym)
                lines.append(f"{base}: {amt:.2f} USDC (mult={mult:.2f}x)")
            tg_send("\n".join(lines))
        except Exception as e:
            logging.warning(f"Telegram context send failed: {e}")

    for sym, amt in alloc.items():
        new_carry = add_to_carry(sym, amt)
        logging.info(f"💰 {sym}: +{amt:.2f} => carry={new_carry:.2f}")

    for sym in iter_symbols_btc_first(list(alloc.keys())):
        try_place_from_carry(sym)
        time.sleep(1)

    logging.info(f"Open orders tracked: {OPEN_ORDERS}")


def check_and_adjust_job() -> None:
    """At CHECK_TIME, verify tracked orders and replace safely if needed."""

    logging.info("-" * 60)
    logging.info(f"CHECK/ADJUST JOB @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("-" * 60)

    if not OPEN_ORDERS:
        logging.info("No tracked orders to check.")
        return

    exchange.load_markets()

    for symbol, info in list(OPEN_ORDERS.items()):
        order_id = str(info.get("id"))
        planned_spend = _safe_float(info.get("planned_spend_usdc"), 0.0)

        try:
            order = exchange.fetch_order(order_id, symbol)
            status = order.get("status")

            if status == "closed":
                OPEN_ORDERS.pop(symbol, None)
                save_open_orders()
                continue

            if status in ("canceled", "rejected", "expired"):
                logging.warning(f"⚠️ {symbol} status={status} id={order_id}. Returning planned spend to carry.")
                if planned_spend > 0:
                    carry_state[symbol] = _safe_float(carry_state.get(symbol, 0.0), 0.0) + planned_spend
                    save_carry()
                OPEN_ORDERS.pop(symbol, None)
                save_open_orders()
                continue

            filled_amt = _safe_float(order.get("filled"), 0.0)
            if filled_amt > 0:
                logging.info(f"ℹ️ {symbol} open but partially filled (filled={filled_amt}). Skipping cancel/replace.")
                continue

            logging.warning(f"⚠️ {symbol} open and NOT filled (filled=0). Attempt cancel & replace.")
            canceled = cancel_order_safe(symbol, order_id)
            if not canceled:
                logging.info(f"ℹ️ {symbol} cancel not confirmed. Skipping replace.")
                continue

            if planned_spend <= 0:
                logging.info(f"ℹ️ {symbol} planned_spend missing; removing from tracking")
                OPEN_ORDERS.pop(symbol, None)
                save_open_orders()
                continue

            new_order = place_post_only_limit_buy(symbol, planned_spend, ADJUST_OFFSET_PERCENT)
            if new_order and new_order.get("id"):
                OPEN_ORDERS[symbol] = {"id": str(new_order["id"]), "planned_spend_usdc": planned_spend}
                save_open_orders()
                tg_send(f"🟠 REPLACED {symbol} old={order_id} new={new_order.get('id')}")
            else:
                carry_state[symbol] = _safe_float(carry_state.get(symbol, 0.0), 0.0) + planned_spend
                save_carry()
                OPEN_ORDERS.pop(symbol, None)
                save_open_orders()

        except Exception as e:
            logging.error(f"❌ Error checking {symbol} order id={order_id}: {e}")

        time.sleep(1)

    logging.info(f"Remaining tracked orders: {OPEN_ORDERS}")


# =============================================================================
# Manual triggers
# =============================================================================

def run_orders_now(trigger: str = "manual") -> None:
    """Try to place orders using current carry only (no new carry added)."""

    if not DCA_LOCK.acquire(blocking=False):
        tg_send("⚠️ Another run is in progress. Try again in a minute.")
        return

    try:
        tg_send(f"▶️ RUN ORDERS ({trigger})")
        alloc = compute_today_allocations()
        for sym in iter_symbols_btc_first(list(alloc.keys())):
            try_place_from_carry(sym)
            time.sleep(1)

        tg_send("✅ RUN ORDERS finished")

    except Exception as e:
        logging.error(f"run_orders_now error: {e}")
        tg_send(f"❌ RUN ORDERS error: {e}")

    finally:
        DCA_LOCK.release()


def run_dca_now(trigger: str = "manual") -> None:
    """Run the full daily DCA job (adds carry + may place orders)."""

    if not DCA_LOCK.acquire(blocking=False):
        tg_send("⚠️ Another run is in progress. Try again in a minute.")
        return

    try:
        tg_send(f"▶️ RUN DCA ({trigger})")
        daily_dca_job()
        tg_send("✅ RUN DCA finished")

    except Exception as e:
        logging.error(f"run_dca_now error: {e}")
        tg_send(f"❌ RUN DCA error: {e}")

    finally:
        DCA_LOCK.release()


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    logging.info("BINANCE DCA BOT (USDC) - v5 starting...")
    logging.info(f"Daily budget: {DAILY_BUDGET_USDC} USDC")
    logging.info(f"Weights: {WEIGHTS}")
    logging.info(f"DCA_TIME={DCA_TIME} | CHECK_TIME={CHECK_TIME}")

    load_state()

    if not test_connection_and_markets():
        sys.exit(1)

    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    twm.start()
    twm.start_user_socket(callback=ws_handle_user_event)
    logging.info("🛰️ Websocket started: real-time FILLED notifications enabled")

    stop_event = threading.Event()
    if tg_enabled():
        t = threading.Thread(target=tg_poll_status_commands, args=(stop_event,), daemon=True)
        t.start()

    schedule.every().day.at(DCA_TIME).do(daily_dca_job)
    schedule.every().day.at(CHECK_TIME).do(check_and_adjust_job)

    if tg_enabled():
        tg_send(
            "🤖 DCA Bot Started\n"
            f"├ Budget: {DAILY_BUDGET_USDC:.2f} USDC/day\n"
            f"├ BTC weight: {BTC_WEIGHT:.4f}\n"
            f"├ SOL weight: {SOL_WEIGHT:.4f}\n"
            f"├ ETH weight: {ETH_WEIGHT:.4f}\n"
            f"├ ADA weight: {ADA_WEIGHT:.4f}\n"
            f"├ XRP weight: {XRP_WEIGHT:.4f}\n"
            f"├ BTC target: {BTC_TARGET:.8f} BTC\n"
            f"├ DCA time: {DCA_TIME}\n"
            f"└ Check time: {CHECK_TIME}\n\n"
            "Type /status to see current state."
        )

    logging.info("🤖 Bot running. Waiting for scheduled jobs...")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        stop_event.set()
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
