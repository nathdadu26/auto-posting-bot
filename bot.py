import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from pymongo import MongoClient
from dotenv import load_dotenv
from health_check import run_health_server

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Application,
)

# ================= LOAD ENV =================
load_dotenv()

BOT_TOKEN          = os.getenv("BOT_TOKEN")
MONGO_URI          = os.getenv("MONGO_URI")
ADMIN_ID           = int(os.getenv("ADMIN_ID"))
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID"))
POST_TIMES         = [t.strip() for t in os.getenv("POST_TIMES").split(",")]
TIMEZONE           = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))

# ================= MONGO =================
client       = MongoClient(MONGO_URI)
db           = client["autopost_bot"]
channels_col = db["channels"]
config_col   = db["config"]

# ================= HELPERS =================
def is_admin(uid):
    return uid == ADMIN_ID

def now_str():
    return datetime.now(TIMEZONE).strftime("%H:%M")

def get_next_message_id():
    doc = config_col.find_one({"_id": "settings"})
    return doc.get("next_message_id", 1) if doc else 1

def save_next_message_id(mid):
    config_col.update_one(
        {"_id": "settings"},
        {"$set": {"next_message_id": mid}},
        upsert=True
    )

# ================= LIVE NOTIFY =================
class LiveNotify:
    """
    Admin ko ek message bhejta hai aur har update pe wahi message edit karta hai.
    Naya message sirf tab bhejta hai jab pehla message available na ho.
    """

    def __init__(self, bot, chat_id: int):
        self.bot      = bot
        self.chat_id  = chat_id
        self.msg_id   = None           # live message ka Telegram message_id
        self.lines    = []             # current log lines

    async def send(self, text: str):
        """Pehli baar message bhejo."""
        self.lines = [text]
        try:
            sent = await self.bot.send_message(
                chat_id=self.chat_id,
                text=self._build()
            )
            self.msg_id = sent.message_id
        except Exception as e:
            print(f"[Notify] send error: {e}")

    async def update(self, new_line: str):
        """Existing message mein ek nayi line add karke edit karo."""
        self.lines.append(new_line)
        if not self.msg_id:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.msg_id,
                text=self._build()
            )
        except Exception as e:
            print(f"[Notify] edit error: {e}")

    async def replace_last(self, new_line: str):
        """Last line ko replace karke edit karo (channel progress update ke liye)."""
        if self.lines:
            self.lines[-1] = new_line
        else:
            self.lines.append(new_line)
        if not self.msg_id:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.msg_id,
                text=self._build()
            )
        except Exception as e:
            print(f"[Notify] replace error: {e}")

    def _build(self) -> str:
        return "\n".join(self.lines)


# ================= POST LOGIC =================
async def do_post(bot, manual=False):
    channels = list(channels_col.find())
    notify   = LiveNotify(bot, ADMIN_ID)

    if not channels:
        await notify.send("⚠️ Koi channel add nahi hai!")
        return

    BATCH    = int(os.getenv("BATCH_SIZE", "10"))
    next_id  = get_next_message_id()
    total_ch = len(channels)

    # ── Header message bhejo ──────────────────────────────────────────────────
    header = (
        f"{'🔧 Manual' if manual else '🕐 Scheduled'} Post Session\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 Total Channels : {total_ch}\n"
        f"▶️ Start ID        : {next_id}\n"
        f"🕒 Time           : {now_str()}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await notify.send(header)
    await asyncio.sleep(0.3)

    # ── Per-channel posting ───────────────────────────────────────────────────
    for idx, ch in enumerate(channels, 1):
        chat_id  = ch["chat_id"]
        start_id = next_id
        end_id   = next_id + BATCH
        errors   = []
        sent     = 0

        # Channel line add karo (progress shuru)
        ch_line = f"\n➡️  CH {idx}/{total_ch} ({chat_id})\n   IDs {start_id}–{end_id - 1} | Sending..."
        await notify.update(ch_line)

        for msg_id in range(start_id, end_id):
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=STORAGE_CHANNEL_ID,
                    message_id=msg_id
                )
                sent += 1
            except Exception as e:
                err_text = str(e)
                errors.append(f"   ❌ msg {msg_id}: {err_text[:40]}")
                print(f"❌ msg {msg_id} → {chat_id}: {e}")

            await asyncio.sleep(0.5)

        # Channel line update karo with result
        status_icon = "✅" if not errors else "⚠️"
        result_line = (
            f"\n{status_icon} CH {idx}/{total_ch} ({chat_id})\n"
            f"   IDs {start_id}–{end_id - 1} | Sent: {sent}/{BATCH}"
        )
        if errors:
            result_line += "\n" + "\n".join(errors)

        await notify.replace_last(result_line)
        await asyncio.sleep(0.3)

        next_id += BATCH

    # ── Footer ────────────────────────────────────────────────────────────────
    save_next_message_id(next_id)
    footer = f"\n━━━━━━━━━━━━━━━━━━━━\n✅ Done! Next start ID: {next_id}"
    await notify.update(footer)
    print(f"✅ Session done | Next ID: {next_id}")


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Access Denied")

    await update.message.reply_text(
        "🤖 Auto Post Bot Active\n\n"
        "📋 Commands:\n"
        "/addchannel -100xxxx\n"
        "/removechannel -100xxxx\n"
        "/listchannels\n"
        "/status\n"
        "/setid <number>\n"
        "/testpost"
    )

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not allowed")

    if not context.args:
        return await update.message.reply_text("Usage: /addchannel -100xxxx")

    try:
        chat_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Invalid channel ID")

    if channels_col.find_one({"chat_id": chat_id}):
        return await update.message.reply_text("⚠️ Already added")

    channels_col.insert_one({"chat_id": chat_id})
    await update.message.reply_text(f"✅ Channel added: {chat_id}")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not allowed")

    if not context.args:
        return await update.message.reply_text("Usage: /removechannel -100xxxx")

    chat_id = int(context.args[0])
    result  = channels_col.delete_one({"chat_id": chat_id})
    if result.deleted_count:
        await update.message.reply_text(f"🗑️ Removed: {chat_id}")
    else:
        await update.message.reply_text("⚠️ Not found")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    channels = list(channels_col.find())
    if not channels:
        return await update.message.reply_text("📭 Koi channel nahi hai")

    text = f"📢 Total: {len(channels)}\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"{i}. {ch['chat_id']}\n"
    await update.message.reply_text(text)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    next_id  = get_next_message_id()
    ch_count = channels_col.count_documents({})
    current  = now_str()

    await update.message.reply_text(
        f"📊 Bot Status\n\n"
        f"🕐 Bot time       : {current} ({os.getenv('TIMEZONE', 'Asia/Kolkata')})\n"
        f"▶️ Next Msg ID    : {next_id}\n"
        f"📢 Total Channels : {ch_count}\n"
        f"📅 Post slots     : {len(POST_TIMES)}\n\n"
        f"{'✅ Current time matches a slot!' if current in POST_TIMES else '⏳ Waiting for next slot...'}"
    )

async def set_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not allowed")

    if not context.args:
        return await update.message.reply_text("Usage: /setid <number>")

    try:
        new_id = int(context.args[0])
        save_next_message_id(new_id)
        await update.message.reply_text(f"✅ Next ID set to: {new_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def testpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not allowed")

    await update.message.reply_text("🚀 Manual post trigger kiya...")
    await do_post(context.bot, manual=True)

# ================= SCHEDULER =================
async def scheduler(app: Application):
    last_posted = None
    print(f"⏰ Scheduler active | TZ: {os.getenv('TIMEZONE', 'Asia/Kolkata')}")

    while True:
        current = now_str()

        if current in POST_TIMES and current != last_posted:
            print(f"🕐 Match: {current}")
            last_posted = current
            try:
                await do_post(app.bot)
            except Exception as e:
                print(f"❌ Scheduler error: {e}")

        await asyncio.sleep(20)

async def post_init(app: Application):
    run_health_server(port=8000)
    asyncio.create_task(scheduler(app))
    print(f"🚀 Bot started | Time: {now_str()} | Slots: {len(POST_TIMES)}")

# ================= MAIN =================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("addchannel",    add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("listchannels",  list_channels))
    app.add_handler(CommandHandler("status",        status))
    app.add_handler(CommandHandler("setid",         set_id))
    app.add_handler(CommandHandler("testpost",      testpost))

    app.run_polling()

if __name__ == "__main__":
    main()
