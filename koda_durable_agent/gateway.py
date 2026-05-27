import os
import sqlite3
from pathlib import Path
import asyncio
import logging
import httpx
import shutil
import subprocess
import tempfile
from datetime import datetime
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from koda_durable_agent.runtime import bootstrap_pythonpath

bootstrap_pythonpath()

from koda_durable_agent.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("koda.gateway")

DB_PATH = str(Path.home() / ".koda" / "koda_gateway.db")

def init_db():
    """Initializes SQLite tables for resilient message queues."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inbound_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            status TEXT DEFAULT 'pending', -- pending, processing, completed, failed
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS outbound_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT DEFAULT 'pending', -- pending, sending, completed, failed
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def queue_inbound(chat_id: str, username: str, text: str):
    """Enqueues an incoming Telegram message."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO inbound_queue (chat_id, username, text) VALUES (?, ?, ?)",
        (chat_id, username, text)
    )
    conn.commit()
    conn.close()
    logger.info(f"Enqueued inbound message from Telegram: {text[:60]}")

def get_pending_outbound():
    """Retrieves all pending outgoing messages."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, text FROM outbound_queue WHERE status = 'pending'")
    rows = cursor.fetchall()
    conn.close()
    return rows

def queue_outbound(chat_id: str, text: str) -> None:
    """Enqueue a message for outbound delivery to Telegram."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO outbound_queue (chat_id, text) VALUES (?, ?)",
        (chat_id, text)
    )
    conn.commit()
    conn.close()
    logger.info(f"Enqueued outbound message: {text[:60]}")


def update_outbound_status(msg_id: int, status: str):
    """Updates status of a queued outbound message."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE outbound_queue SET status = ? WHERE id = ?", (status, msg_id))
    conn.commit()
    conn.close()

async def send_telegram_message(client: httpx.AsyncClient, chat_id: str, text: str) -> bool:
    """Attempts to send a message to Telegram, returning True if successful."""
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            return True
        logger.error(f"Telegram returned error code {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
    return False

async def send_keyboard_message(client: httpx.AsyncClient, chat_id: str, text: str, keyboard: list) -> bool:
    """Send a message with an inline keyboard. keyboard is list[list[{text, callback_data}]]."""
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            return True
        logger.error(f"Telegram keyboard message error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send keyboard message: {e}")
    return False

async def answer_callback_query(client: httpx.AsyncClient, callback_query_id: str) -> None:
    """Acknowledge a callback query to clear Telegram's loading spinner."""
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        await client.post(url, json={"callback_query_id": callback_query_id})
    except Exception:
        pass

async def _get_telegram_file_url(client: httpx.AsyncClient, file_id: str) -> str | None:
    """Resolve a Telegram file_id to a download URL."""
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getFile"
    try:
        resp = await client.post(url, json={"file_id": file_id})
        data = resp.json()
        file_path = data.get("result", {}).get("file_path")
        if file_path:
            return f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{file_path}"
    except Exception as e:
        logger.error(f"Failed to resolve Telegram file: {e}")
    return None

async def transcribe_voice_note(client: httpx.AsyncClient, file_id: str) -> str | None:
    """Download a Telegram voice note and transcribe it with the local Whisper CLI.

    Returns the transcribed text, or None on failure.
    First call auto-downloads the 'base' model (~74 MB) to ~/.cache/whisper/.
    """
    whisper_bin = shutil.which("whisper")
    if not whisper_bin:
        logger.error("whisper CLI not found — install via: brew install openai-whisper")
        return None

    file_url = await _get_telegram_file_url(client, file_id)
    if not file_url:
        return None

    try:
        resp = await client.get(file_url, timeout=30.0)
        resp.raise_for_status()
        audio_bytes = resp.content
    except Exception as e:
        logger.error(f"Failed to download voice note: {e}")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / "voice.ogg"
        audio_path.write_bytes(audio_bytes)

        whisper_model = os.environ.get("KODA_WHISPER_MODEL", "base")
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    whisper_bin,
                    str(audio_path),
                    "--model", whisper_model,
                    "--output_format", "txt",
                    "--output_dir", tmpdir,
                    "--language", "en",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error(f"Whisper failed: {result.stderr[:200]}")
                return None
            txt_path = Path(tmpdir) / "voice.txt"
            if txt_path.exists():
                return txt_path.read_text().strip()
            # Fallback: parse from stdout
            for line in result.stdout.splitlines():
                if "-->" not in line and line.strip():
                    return line.strip()
        except subprocess.TimeoutExpired:
            logger.error("Whisper transcription timed out")
        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")

    return None

async def process_outbound_loop():
    """Periodically checks and sends pending outbound messages."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                pending = get_pending_outbound()
                for msg_id, chat_id, text in pending:
                    update_outbound_status(msg_id, "sending")
                    success = await send_telegram_message(client, chat_id, text)
                    if success:
                        update_outbound_status(msg_id, "completed")
                        logger.info(f"Successfully sent outbound reply: {text[:60]}")
                    else:
                        update_outbound_status(msg_id, "failed")
            except Exception as e:
                logger.error(f"Error in outbound loop: {e}")
            await asyncio.sleep(2)

async def _dispatch_handler(message_handler, chat_id: str, text: str) -> None:
    """Run message_handler in a task so the polling loop is not blocked."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            reply = await message_handler(text)
            if not reply:
                return
            if isinstance(reply, dict) and "keyboard" in reply:
                await send_keyboard_message(client, chat_id, reply.get("text", "Choose:"), reply["keyboard"])
            else:
                await send_telegram_message(client, chat_id, reply)
        except Exception as handler_err:
            logger.error("message_handler error: %s", handler_err, exc_info=True)
            await send_telegram_message(client, chat_id, "⚠️ Koda hit an error. Check logs.")

async def run_inbound_polling(message_handler=None):
    """Long polls Telegram for messages with exponential backoff resiliency."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Gateway polling disabled.")
        return

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"
    offset = None
    backoff = 1.0
    max_backoff = 60.0

    logger.info("Starting resilient Telegram Gateway polling daemon...")
    
    async with httpx.AsyncClient(timeout=35.0) as client:
        while True:
            try:
                params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset

                resp = await client.get(url, params=params)

                # Check for HTTP failures or rate limits
                if resp.status_code != 200:
                    logger.warning(f"Telegram API warning (HTTP {resp.status_code}). Backing off...")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    logger.warning(f"Telegram returned ok=False: {data}. Backing off...")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue

                # Reset backoff on successful call
                backoff = 1.0

                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    # Inline keyboard button tap
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        cq_id = cq["id"]
                        cq_chat_id = str(cq["message"]["chat"]["id"])
                        cmd_text = cq.get("data", "").strip()
                        # Ack immediately so Telegram clears the loading spinner
                        await answer_callback_query(client, cq_id)
                        is_owner = (settings.TELEGRAM_CHAT_ID and cq_chat_id == str(settings.TELEGRAM_CHAT_ID))
                        is_allowed = cq_chat_id in settings.TELEGRAM_ALLOWED_USERS
                        if (is_owner or is_allowed) and cmd_text and message_handler is not None:
                            await send_telegram_message(client, cq_chat_id, "⚡ Koda thinking...")
                            asyncio.create_task(_dispatch_handler(message_handler, cq_chat_id, cmd_text))
                        continue

                    if "message" not in update:
                        continue

                    message = update["message"]
                    chat = message.get("chat", {})
                    chat_id = str(chat.get("id"))
                    sender = message.get("from", {})
                    username = sender.get("username", "unknown")
                    text = message.get("text", "").strip()
                    voice = message.get("voice")

                    if not text and not voice:
                        continue

                    # Resilient allowlist check
                    is_owner = (settings.TELEGRAM_CHAT_ID and chat_id == str(settings.TELEGRAM_CHAT_ID))
                    is_allowed = chat_id in settings.TELEGRAM_ALLOWED_USERS

                    if not (is_owner or is_allowed):
                        logger.warning(f"Blocked unauthorized message from chat_id {chat_id} (@{username})")
                        await send_telegram_message(client, chat_id, "⚠️ Access Denied. Unauthorized operator.")
                        continue

                    if voice:
                        file_id = voice["file_id"]
                        await send_telegram_message(client, chat_id, "🎙️ Transcribing voice note...")
                        transcript = await transcribe_voice_note(client, file_id)
                        if not transcript:
                            await send_telegram_message(client, chat_id, "❌ Could not transcribe voice note. Check that whisper is installed.")
                            continue
                        logger.info(f"Voice note transcribed: {transcript[:80]}")
                        await send_telegram_message(client, chat_id, f"📝 *Transcribed:* {transcript}")
                        text = transcript

                    queue_inbound(chat_id, username, text)
                    await send_telegram_message(client, chat_id, "⚡ Koda thinking...")

                    if message_handler is not None:
                        asyncio.create_task(
                            _dispatch_handler(message_handler, chat_id, text)
                        )

            except httpx.RequestError as e:
                logger.warning(f"Connection issue: {e}. Backing off in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except Exception as e:
                logger.error(f"Critical error in gateway polling: {e}. Recovering...")
                await asyncio.sleep(5)

async def start_gateway(message_handler=None):
    """Initializes the database and runs the concurrent gateway tasks."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram gateway disabled.")
        return
    init_db()
    await asyncio.gather(
        run_inbound_polling(message_handler=message_handler),
        process_outbound_loop()
    )

if __name__ == "__main__":
    try:
        asyncio.run(start_gateway())
    except KeyboardInterrupt:
        logger.info("Gateway daemon terminated by user.")
