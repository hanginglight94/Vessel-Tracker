import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from scraper import fetch_vessel_telemetry

# Load environment variables if available
load_dotenv()

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL", "3600"))  # Default: 1 hour (3600s)

# Conversation States
ASK_MMSI, TRACKING = range(2)

# Active tracking sessions stored by chat_id
# Structure: {chat_id: {"active": bool, "id": str, "last_summary": str, "stop_event": threading.Event}}
active_trackers = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- RENDER / UPTIMEROBOT HEALTH CHECK SERVER ---

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Responds to Render & UptimeRobot HTTP pings to keep the free service alive."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Vessel Tracker Bot is running!")

    def log_message(self, format, *args):
        # Suppress routine ping logging from UptimeRobot
        pass


def start_health_server():
    """Starts background HTTP server listening on Render's $PORT."""
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"Health check HTTP server listening on port {port}")
    except Exception as e:
        logger.warning(f"Could not start health check HTTP server on port {port}: {e}")


def format_vessel_message(info: dict) -> str:
    """Formats vessel telemetry data into a clean, informative Telegram Markdown message."""
    name = info.get("name", "Unknown Vessel")
    identifiers = info.get("imo_mmsi", info.get("id", "N/A"))
    v_type = info.get("type", "Vessel")
    flag = info.get("flag", "N/A")
    status = info.get("status", "N/A")
    speed = info.get("speed", "N/A")
    dest = info.get("destination", "Not specified")
    eta = info.get("eta", "Not specified")
    last_seen = info.get("last_seen", "Recently")
    summary = info.get("summary", "")
    url = info.get("url", "https://www.vesselfinder.com")

    msg_lines = [
        f"🚢 *Vessel Update: {name}*",
        f"• *IMO / MMSI:* `{identifiers}`",
        f"• *Type & Flag:* {v_type} ({flag})",
        f"• *Navigation Status:* {status}",
        f"• *Current Speed:* {speed}",
        f"🏁 *Destination:* `{dest}`",
        f"⏱️ *ETA:* `{eta}`",
        f"📡 *Last AIS Report:* {last_seen}",
    ]

    if info.get("lat") is not None and info.get("lon") is not None:
        lat = info["lat"]
        lon = info["lon"]
        msg_lines.append(f"📍 *Coordinates:* `{lat}, {lon}`")
        maps_link = f"https://maps.google.com/?q={lat},{lon}"
        msg_lines.append(f"🗺️ [View on Google Maps]({maps_link})")

    if summary:
        msg_lines.append(f"\n📋 *AIS Narrative:*\n_{summary}_")

    msg_lines.append(f"\n🔗 [Open on VesselFinder]({url})")

    return "\n".join(msg_lines)


# --- BACKGROUND TRACKING LOOP ---

def tracking_worker(chat_id, vessel_id, app, stop_event):
    """Background thread loop that polls vessel telemetry every interval."""
    logger.info(f"Background tracking started for chat {chat_id}, ID {vessel_id}")
    while not stop_event.is_set() and active_trackers.get(chat_id, {}).get("active", False):
        if stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
            break

        if not active_trackers.get(chat_id, {}).get("active", False):
            break

        info = fetch_vessel_telemetry(vessel_id)

        if info and (info.get("summary") or info.get("lat")):
            msg_text = format_vessel_message(info)
            app.create_task(
                app.bot.send_message(
                    chat_id=chat_id,
                    text=msg_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                )
            )
        else:
            app.create_task(
                app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Unable to retrieve new telemetry update for `{vessel_id}`.",
                    parse_mode="Markdown",
                )
            )

    logger.info(f"Background tracking terminated for chat {chat_id}, ID {vessel_id}")


# --- CONVERSATION HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: asks for MMSI, IMO, or Name."""
    await update.message.reply_text(
        "👋 *Welcome to the Free Vessel Finder Tracker Bot!*\n\n"
        "Please enter the **9-digit MMSI**, **7-digit IMO**, or **Vessel Name** you would like to track.\n\n"
        "📌 *Examples only (you can track any vessel):*\n"
        "• MMSI example: `525111005`\n"
        "• IMO example: `9260031`\n"
        "• Name example: `EVER GIVEN`",
        parse_mode="Markdown",
    )
    return ASK_MMSI


async def receive_mmsi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives vessel query, verifies it on VesselFinder, and launches background tracking."""
    vessel_id = update.message.text.strip()
    chat_id = update.message.chat_id

    await update.message.reply_text(f"🔎 Searching VesselFinder for `{vessel_id}`...", parse_mode="Markdown")

    info = fetch_vessel_telemetry(vessel_id)
    if not info or not (info.get("summary") or info.get("lat")):
        await update.message.reply_text(
            f"❌ Could not locate vessel `{vessel_id}` on VesselFinder.\n\n"
            "Please check the ID/name and try again (e.g. `525111005`, `9260031`, or `EVER GIVEN`):",
            parse_mode="Markdown"
        )
        return ASK_MMSI

    if chat_id in active_trackers:
        active_trackers[chat_id]["active"] = False
        active_trackers[chat_id]["stop_event"].set()

    stop_event = threading.Event()
    active_trackers[chat_id] = {
        "active": True,
        "id": vessel_id,
        "last_summary": info.get("summary", ""),
        "stop_event": stop_event,
    }

    thread = threading.Thread(
        target=tracking_worker,
        args=(chat_id, vessel_id, context.application, stop_event),
        daemon=True,
    )
    thread.start()

    interval_minutes = max(1, POLL_INTERVAL_SECONDS // 60)
    reply_keyboard = [["/stop"]]
    markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

    header = f"✅ *Tracking Started for {info['name']}!*\n\n"
    msg_body = format_vessel_message(info)
    footer = f"\n\n⏱️ Updates will be dispatched every {interval_minutes} minutes.\nSend /stop at any time to end tracking."

    await update.message.reply_text(
        header + msg_body + footer,
        reply_markup=markup,
        parse_mode="Markdown",
        disable_web_page_preview=False,
    )
    return TRACKING


async def stop_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stops current tracking loop and resets back to prompt."""
    chat_id = update.message.chat_id

    if chat_id in active_trackers:
        active_trackers[chat_id]["active"] = False
        active_trackers[chat_id]["stop_event"].set()
        del active_trackers[chat_id]

    await update.message.reply_text(
        "🛑 Tracking stopped and session reset.\n\n"
        "Enter a new **MMSI**, **IMO**, or **Name** to start tracking another vessel:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return ASK_MMSI


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels conversation entirely."""
    chat_id = update.message.chat_id
    if chat_id in active_trackers:
        active_trackers[chat_id]["active"] = False
        active_trackers[chat_id]["stop_event"].set()
        del active_trackers[chat_id]

    await update.message.reply_text(
        "Session closed. Type /start whenever you want to track a vessel again.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# --- MAIN EXECUTION ---

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not TELEGRAM_BOT_TOKEN:
        print("❌ Error: Missing Telegram Bot Token in .env!")
        return

    # Start the HTTP health server on $PORT for Render and UptimeRobot
    start_health_server()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_MMSI: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mmsi)],
            TRACKING: [CommandHandler("stop", stop_tracking)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("stop", stop_tracking)],
    )

    app.add_handler(conv_handler)
    print("Bot running with VesselFinder backend & health server active...")
    app.run_polling()


if __name__ == "__main__":
    main()
