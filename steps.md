## Start the bot, tunnel, and Telegram webhook

1. Set valid `YNAB_TOKEN`, `YNAB_BUDGET_ID`, and `TELEGRAM_BOT_TOKEN` in `.env`.
2. Run `uv run python run_tunnel.py`.

The launcher checks YNAB access, starts `apfel --serve` if needed, waits for the
local model, then starts Gunicorn, opens the SSH tunnel through
`qr@free.pinggy.io`, and registers the Pinggy HTTPS URL as the Telegram webhook.
Keep it running; press Ctrl+C to stop the server, tunnel, and model process it
started.

To check YNAB separately, run `uv run python check_ynab.py`.