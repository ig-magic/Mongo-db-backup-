"""Private Telegram bot for MongoDB backup.

Creates two outputs per run:
  1) mongodump --archive --gzip: native, restorable MongoDB backup.
  2) JSONL files in a ZIP: human-readable Canonical Extended JSON export.

Large files are split into Telegram-safe parts; concatenate same-base parts in order.
"""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import concurrent.futures
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from bson.json_util import CANONICAL_JSON_OPTIONS, dumps as bson_dumps
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(item.strip())
    for item in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}
PART_BYTES = 1_900_000_000  # below local Bot API 2000 MB cap; safety margin
SYSTEM_DATABASES = {"admin", "config", "local"}
JOB_LOCK = threading.Lock()


def is_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and chat.type == ChatType.PRIVATE and user.id in ADMIN_IDS)


def check_full_cluster_uri(uri: str) -> None:
    parsed = urlsplit(uri)
    if parsed.scheme not in {"mongodb", "mongodb+srv"}:
        raise ValueError("MongoDB URI mongodb:// ya mongodb+srv:// se shuru hona chahiye.")
    if parsed.path not in {"", "/"}:
        raise ValueError(
            "Poore cluster ke backup ke liye URI mein database naam na dein; host ke baad sirf / rakhein."
        )


def split_for_telegram(source: Path, workdir: Path) -> list[Path]:
    size = source.stat().st_size
    if size <= PART_BYTES:
        return [source]

    parts: list[Path] = []
    with source.open("rb") as fin:
        index = 1
        while True:
            part = workdir / f"{source.name}.part{index:03d}"
            remaining = PART_BYTES
            wrote = False
            with part.open("wb") as fout:
                while remaining:
                    block = fin.read(min(8 * 1024 * 1024, remaining))
                    if not block:
                        break
                    fout.write(block)
                    remaining -= len(block)
                    wrote = True
            if not wrote:
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            index += 1
    return parts


def export_readable_json(uri: str, output_zip: Path, report_progress) -> int:
    """Export user databases as one Canonical Extended JSON document per JSONL line."""
    client = MongoClient(uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=20000)
    client.admin.command("ping")
    db_names = sorted(name for name in client.list_database_names() if name not in SYSTEM_DATABASES)

    collections: list[tuple[str, str, int]] = []
    for db_name in db_names:
        db = client[db_name]
        for coll_name in sorted(db.list_collection_names()):
            try:
                count = db[coll_name].count_documents({})
            except Exception:
                count = 0
            collections.append((db_name, coll_name, count))

    total_docs = sum(item[2] for item in collections)
    done_docs = 0
    exported_collections = 0
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(
            "README.txt",
            "Readable export: Canonical Extended JSON, one document per line (JSONL).\n"
            "This is for inspection/secondary import. Use the separate .archive.gz file\n"
            "for the complete native mongodump restore, including collection metadata/indexes.\n"
            "System databases admin/config/local are intentionally omitted from this readable export.\n",
        )
        for db_name, coll_name, expected_count in collections:
            safe_db = re.sub(r"[^A-Za-z0-9_.-]", "_", db_name)
            safe_coll = re.sub(r"[^A-Za-z0-9_.-]", "_", coll_name)
            entry_name = f"json/{safe_db}/{safe_coll}.jsonl"
            with zf.open(entry_name, "w") as raw:
                for document in client[db_name][coll_name].find({}).batch_size(1000):
                    line = bson_dumps(document, json_options=CANONICAL_JSON_OPTIONS) + "\n"
                    raw.write(line.encode("utf-8"))
                    done_docs += 1
                    if done_docs % 500 == 0 or (total_docs and done_docs == total_docs):
                        pct = 10 + int(55 * min(done_docs, total_docs) / max(total_docs, 1))
                        report_progress(pct, f"Readable JSON export: {done_docs:,}/{total_docs:,} documents")
            exported_collections += 1
            if expected_count == 0:
                report_progress(
                    10 + int(55 * done_docs / max(total_docs, 1)),
                    f"Readable JSON export: {exported_collections}/{len(collections)} collections",
                )

    client.close()
    report_progress(65, f"Readable export ready ({exported_collections} collections)")
    return exported_collections


def make_backup(uri: str, workdir: Path, report_progress) -> tuple[Path, Path]:
    check_full_cluster_uri(uri)
    if not shutil.which("mongodump"):
        raise RuntimeError("Server par mongodump install nahi hai.")

    archive = workdir / "mongodb-restorable.archive.gz"
    readable = workdir / "mongodb-readable-json.zip"

    report_progress(5, "MongoDB connection check ho raha hai")
    export_readable_json(uri, readable, report_progress)

    report_progress(68, "Native restorable mongodump chal raha hai (is stage ka percent approximate hai)")
    result = subprocess.run(
        ["mongodump", "--uri", uri, "--archive", str(archive), "--gzip"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60 * 60,
        check=False,
    )
    if result.returncode != 0:
        # Never return a raw tool error: it could contain connection details.
        raise RuntimeError("mongodump fail hua. URI/permissions/network aur Render logs check karein.")

    report_progress(84, "Restorable archive tayyar; Telegram ke liye file parts ban rahe hain")
    return archive, readable


async def send_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, pct: int, text: str):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"Backup progress: {pct}%\n{text}",
        )
    except Exception:
        pass


def run_progress_callback(loop, context, chat_id, message_id):
    last = {"pct": -1, "time": 0.0}

    def report(pct: int, text: str):
        now = time.monotonic()
        pct = max(last["pct"], min(pct, 95))
        # Avoid flooding Telegram; still report meaningful percentage changes.
        if pct == last["pct"] and now - last["time"] < 12:
            return
        last.update(pct=pct, time=now)
        future = asyncio.run_coroutine_threadsafe(
            send_progress(context, chat_id, message_id, pct, text), loop
        )
        try:
            future.result(timeout=25)
        except Exception:
            pass

    return report


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Private MongoDB backup bot ready. /backup bhejein, phir Atlas URI send karein."
    )


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if JOB_LOCK.locked():
        await update.message.reply_text("Ek backup pehle se chal raha hai. Complete hone ka wait karein.")
        return
    context.user_data["awaiting_uri"] = True
    await update.message.reply_text(
        "Ab full-cluster Atlas URI private chat mein bhejein. URI mein database naam na ho.\n"
        "Dedicated read-only MongoDB user use karein. Bot URI message ko delete karne ki koshish karega."
    )


async def receive_uri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not context.user_data.get("awaiting_uri"):
        return
    uri = (update.message.text or "").strip()
    context.user_data["awaiting_uri"] = False

    # Best effort only. Do not rely on deletion as credential protection.
    try:
        await update.message.delete()
    except Exception:
        pass

    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        await update.effective_chat.send_message("Yeh MongoDB URI format nahi lag raha. /backup se dobara shuru karein.")
        return

    if not JOB_LOCK.acquire(blocking=False):
        await update.effective_chat.send_message("Ek backup pehle se chal raha hai.")
        return

    status = await update.effective_chat.send_message("Backup progress: 1%\nRequest receive hui.")
    loop = asyncio.get_running_loop()
    report = run_progress_callback(loop, context, update.effective_chat.id, status.message_id)

    try:
        with tempfile.TemporaryDirectory(prefix="atlas-backup-") as temp_dir:
            workdir = Path(temp_dir)
            archive, readable = await asyncio.to_thread(make_backup, uri, workdir, report)
            report(86, "Backup files split/send ke liye prepare ho rahi hain")

            items: list[tuple[Path, str]] = []
            for file_path, label in (
                (archive, "Restorable MongoDB archive"),
                (readable, "Readable Extended JSON ZIP"),
            ):
                for part in split_for_telegram(file_path, workdir):
                    items.append((part, label))

            await send_progress(context, update.effective_chat.id, status.message_id, 90,
                                f"{len(items)} Telegram file(s) bheji ja rahi hain")
            for index, (path, label) in enumerate(items, start=1):
                caption = f"{label} — part {index}/{len(items)}\nFile: {path.name}"
                with path.open("rb") as document:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=document,
                        filename=path.name,
                        caption=caption[:900],
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=30,
                    )
                pct = 90 + int(9 * index / max(len(items), 1))
                await send_progress(context, update.effective_chat.id, status.message_id, pct,
                                    f"Telegram par bheja: {index}/{len(items)} files")

            await send_progress(
                context, update.effective_chat.id, status.message_id, 100,
                "Complete. Archive restore ke liye hai; JSON ZIP padhne/inspection ke liye hai.",
            )
    except Exception as exc:
        # Avoid echoing URI or subprocess command in chat.
        message = str(exc)
        if "URI" in message or "mongodb" in message.lower():
            message = "Backup fail hua. URI, Atlas permissions aur network access check karein."
        await update.effective_chat.send_message(f"Backup fail hua: {message[:300]}")
    finally:
        uri = ""  # best-effort removal of the local reference
        JOB_LOCK.release()


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data["awaiting_uri"] = False
    await update.message.reply_text("URI prompt cancel kar diya. Chal raha backup cancel nahi hota.")


def start_health_server():
    """Small HTTP endpoint so Render can run this bot container as a Web Service."""
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            # Avoid noisy access logs; never log credentials.
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="render-health")
    thread.start()


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN environment variable required.")
    if not ADMIN_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS mein numeric Telegram user ID(s) set karein.")

    # Render Web Service requires an HTTP listener on PORT. This is a health endpoint;
    # backup control remains private in Telegram.
    start_health_server()

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=300,
        write_timeout=300,
        media_write_timeout=300,
    )
    # Local Bot API endpoint is hosted in this same Render worker container.
    # API ID/hash configure that server; they do not alter the cloud Bot API limit.
    base_url = os.environ.get("TELEGRAM_API_BASE_URL", "http://127.0.0.1:8081/bot")
    base_file_url = os.environ.get("TELEGRAM_FILE_BASE_URL", "http://127.0.0.1:8081/file/bot")
    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(base_url)
        .base_file_url(base_file_url)
        .request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_uri))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
