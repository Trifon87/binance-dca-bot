# Binance DCA Bot (Spot) + Telegram

A Python bot for Binance Spot that runs a scheduled multi-asset DCA workflow, manages post-only LIMIT buy orders, listens for FILLED events via Binance User Data Stream (WebSocket), and exposes operational commands via Telegram.

## Key workflows (mapped to code)
- Daily DCA (UC1): `daily_dca_job()` computes allocations, adds to carry, sends a Telegram preview, and places orders when carry is above min notional.
- Check & adjust (UC2): `check_and_adjust_job()` cancels/replaces tracked open orders at a scheduled time.
- Real-time fills (UC3): `ws_handle_user_event()` processes user data stream events and updates local positions.
- Manual triggers (UC4): `run_orders_now()` and `run_dca_now()` allow manual execution (guarded by Telegram confirmation).
- Persistence (UC5): `load_state()`, `save_carry()`, `save_positions()`, `save_open_orders()` store runtime state locally as JSON.

## Telegram commands
- `/status` — show current state
- `/preview_dca` — show today's DCA preview
- `/run_orders confirm` — place orders from existing carry (manual)
- `/run_dca confirm` — run full DCA now (manual)
- `/reset_carry confirm` — reset carry for active symbols (based on weights)
- `/reset_carry_all confirm` — reset carry for all symbols
- `/reset_btc confirm`
- `/reset_sol confirm`
- `/reset_eth confirm`
- `/reset_ada confirm`
- `/reset_xrp confirm`
- `/help`

## Configuration (.env)
Create a `.env` file from `.env.example` and fill in the values.

Main variables used by the bot:
- Binance: `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_POLL_INTERVAL`
- Scheduling: `DCA_TIME`, `CHECK_TIME`
- Budget & weights: `DAILY_BUDGET_USDC`, `BTC_WEIGHT`, `SOL_WEIGHT`, `ETH_WEIGHT`, `ADA_WEIGHT`, `XRP_WEIGHT`
- Execution: `LIMIT_OFFSET_PERCENT`, `ADJUST_OFFSET_PERCENT`
- Dynamic offsets: `OFFSET_MIN`, `OFFSET_MAX`, `VOL_LOW`, `VOL_HIGH`
- Risk/logic knobs: `BUDGET_MULT_MIN`, `BUDGET_MULT_MAX_BTC`, `BUDGET_MULT_MAX_SOL`, `BUDGET_MULT_MAX_ETH`, `BUDGET_MULT_MAX_ADA`, `BUDGET_MULT_MAX_XRP`
- 24h change rules: `CHG_UP_1`, `CHG_UP_2`, `CHG_DN_1`, `CHG_DN_2`, `MULT_UP_1`, `MULT_UP_2`, `MULT_DN_1`, `MULT_DN_2`
- 200DMA regime rules: `RATIO_LOW_1`, `RATIO_LOW_2`, `RATIO_HIGH_1`, `RATIO_HIGH_2`, `RATIO_HIGH_3`, `MA_LOW_1`, `MA_LOW_2`, `MA_HIGH_1`, `MA_HIGH_2`, `MA_HIGH_3`
- Indicators: `ATR_ENABLED`, `ATR_PERIOD`, `ATR_LOW`, `ATR_HIGH`, `ATR_MULT_LOW`, `ATR_MULT_HIGH`, `EMA_ENABLED`, `EMA_PERIOD`, `REGIME_BULL_CAP_MULT`, `REGIME_BEAR_CAP_MULT`, `REGIME_NEUTRAL_CAP_MULT`
- Misc: `MIN_NOTIONAL`, `BTC_TARGET`, `PORTFOLIO_GOAL_USDC`

## Install & run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python3 dca_bot.py


## Disclaimer
Educational project. Use at your own risk. Test with small amounts before using real funds.
