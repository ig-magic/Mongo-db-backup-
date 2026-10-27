# Private Telegram MongoDB backup bot (Render Web Service)

This project runs the bot together with Telegram's **official self-hosted Local Bot API server** in one Render Web Service. A small `/health` endpoint listens on Render's `PORT`; it exists to satisfy Render's web-service health checks. Backup control is via the private Telegram bot, not a browser dashboard.

In `--local` mode, the Telegram server supports uploads up to 2000 MB. `api_id` and `api_hash` are credentials for that self-hosted Bot API server; simply adding them to a normal cloud Bot API bot does not raise the 50 MB cloud upload limit.

The bot creates and sends:

1. `mongodb-restorable.archive.gz` — native `mongodump` archive, preferred for a later restore.
2. `mongodb-readable-json.zip` — Canonical Extended JSONL, one collection per file, for reading/inspection.

Large outputs are split at 1,900,000,000 bytes, below the Local Bot API 2000 MB maximum. If split, join parts with the same base filename in numeric order before extracting/restoring. No restore code is included.

## Render deployment

Create a **Web Service** from this folder using the **Docker** runtime. The Dockerfile builds the official Telegram Local Bot API server and MongoDB Database Tools; the first build may take longer and needs adequate build resources. Set Render's health check path to `/health` if configuring a health check.

Set these Render environment variables:

- `TELEGRAM_API_ID` — API ID from `my.telegram.org`
- `TELEGRAM_API_HASH` — API hash from `my.telegram.org`
- `TELEGRAM_BOT_TOKEN` — token from BotFather
- `TELEGRAM_ADMIN_IDS` — your numeric Telegram user ID; comma-separated IDs if needed
- `PORT` — Render supplies this automatically; do not hard-code it

Do not put these credentials in code or chat. API ID/hash are not the Telegram bot token.

### One-time bot migration

Before pointing a bot at a Local Bot API server, Telegram requires logging the bot out from the cloud Bot API. Call the cloud `logOut` method once using the bot token privately, then deploy/start this worker. Do not call `logOut` on every restart. If the bot is already migrated, skip this step.

## Use

1. Start a private chat with the bot and send `/start`.
2. Send `/backup`, then send the full-cluster Atlas URI (no database name after the host).
3. The bot attempts to delete the URI message (best effort only), exports both formats, and sends progress updates and files.

Use a dedicated read-only Atlas user and restrict Atlas network access where practical. URI deletion is not a guarantee that the secret disappears from Telegram history or notifications.

The native archive is the authoritative restore backup and includes collection metadata/indexes. The readable JSON export omits system databases `admin`, `config`, and `local`; it is for inspection/secondary imports. Full-cluster read/list privileges are needed.
