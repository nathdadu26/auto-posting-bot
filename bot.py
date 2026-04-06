import os
import re
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from pymongo import MongoClient
from dotenv import load_dotenv
from health_check import run_health_server

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    Application,
    filters,
)

# ================= LOAD ENV =================
load_dotenv()

BOT_TOKEN          = os.getenv("BOT_TOKEN")
MONGO_URI          = os.getenv("MONGO_URI")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID"))
POST_TIMES         = [t.strip() for t in os.getenv("POST_TIMES").split(",")]
TIMEZONE           = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
LOG_CHANNEL_ID     = int(os.getenv("LOG_CHANNEL_ID"))
JOIN_BACKUP_URL    = os.getenv("JOIN_BACKUP_URL")
HOW_TO_OPEN_URL    = os.getenv("HOW_TO_OPEN_URL")
BOT_NAME           = os.getenv("BOT_NAME", "Miss Ziya Bot")  # caption me dikhega

# Multiple admins — comma separated in env: ADMIN_IDS=123456,789012
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())

# ================= MONGO =================
client       = MongoClient(MONGO_URI)
db           = client["autopost_bot"]
channels_col = db["channels"]
config_col   = db["config"]

# ================= HELPERS =================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def now_str() -> str:
    return datetime.now(TIMEZONE).strftime("%H:%M")

# ── DB: next message ID ───────────────────────────────────────────────────────
def get_next_message_id() -> int:
    doc = config_col.find_one({"_id": "settings"})
    return doc.get("next_message_id", 1) if doc else 1

def save_next_message_id(mid: int):
    config_col.update_one(
        {"_id": "settings"},
        {"$set": {"next_message_id": mid}},
        upsert=True
    )

# ── DB: batch quantity ────────────────────────────────────────────────────────
def get_batch_size() -> int:
    doc = config_col.find_one({"_id": "settings"})
    return doc.get("batch_size", 5) if doc else 5

def save_batch_size(size: int):
    config_col.update_one(
        {"_id": "settings"},
        {"$set": {"batch_size": size}},
        upsert=True
    )

# ── Caption + Buttons builder ─────────────────────────────────────────────────
# ── Extract all URLs from a text ─────────────────────────────────────────────
URL_REGEX = re.compile(r'https?://[^\s]+')

def extract_links(text: str) -> list:
    return URL_REGEX.findall(text) if text else []

# ── Fetch original caption from storage channel ───────────────────────────────
async def get_original_caption(bot, msg_id: int) -> str:
    """
    Storage channel se message ko log channel me temporarily forward karke
    original caption padhta hai, phir delete kar deta hai.
    """
    try:
        fwd = await bot.forward_message(
            chat_id=LOG_CHANNEL_ID,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=msg_id
        )
        caption = fwd.caption or fwd.text or ""
        await bot.delete_message(chat_id=LOG_CHANNEL_ID, message_id=fwd.message_id)
        return caption
    except Exception as e:
        print(f"[get_caption] msg {msg_id}: {e}")
        return ""

# ── Caption builder using original links ─────────────────────────────────────
def build_caption(original_caption: str) -> str:
    links = extract_links(original_caption)

    if links:
        video_lines = "\n\n".join(
            f"Video {i}. 👉 {link}" for i, link in enumerate(links, 1)
        )
    else:
        video_lines = "Video 1. 👉 (No link found)"

    return (
        f"📥 Download Links/👀Watch Online\n\n"
        f"{video_lines}\n\n"
        f"▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▰\n"
        f"🚫⛔️ Note: We Don't Leak Anything here, "
        f"We Just Collect & Share from All Over the internet\n"
        f"Thanks🔎 For Content Removal Use {BOT_NAME}"
    )

def build_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔰 Join BackUP", url=JOIN_BACKUP_URL),
            InlineKeyboardButton("📖 How To Open", url=HOW_TO_OPEN_URL),
        ]
    ])

# ── Parse Telegram post link ──────────────────────────────────────────────────
def parse_post_link(link: str):
    """
    https://t.me/c/3775879602/15149
    Returns (channel_id, message_id) or (None, None)
    """
    match = re.match(r"https://t\.me/c/(\d+)/(\d+)", link.strip())
    if not match:
        return None, None
    channel_id = int("-100" + match.group(1))
    message_id = int(match.group(2))
    return channel_id, message_id

# ================= LOG CHANNEL =================
async def log(bot, text: str):
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text)
    except Exception as e:
        print(f"[Log error] {e}")

# ================= LIVE NOTIFY =================
class LiveNotify:
    def __init__(self, bot, chat_id: int):
        self.bot     = bot
        self.chat_id = chat_id
        self.msg_id  = None
        self.lines   = []

    async def send(self, text: str):
        self.lines = [text]
        try:
            sent = await self.bot.send_message(chat_id=self.chat_id, text=self._build())
            self.msg_id = sent.message_id
        except Exception as e:
            print(f"[Notify.send] {e}")

    async def update(self, new_line: str):
        self.lines.append(new_line)
        await self._edit()

    async def replace_last(self, new_line: str):
        if self.lines:
            self.lines[-1] = new_line
        else:
            self.lines.append(new_line)
        await self._edit()

    async def _edit(self):
        if not self.msg_id:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.msg_id,
                text=self._build()
            )
        except Exception as e:
            print(f"[Notify.edit] {e}")

    def _build(self) -> str:
        return "\n".join(self.lines)

# ================= POST LOGIC =================
async def do_post(bot, manual=False):
    channels = list(channels_col.find())
    notify   = LiveNotify(bot, next(iter(ADMIN_IDS)))   # pehle admin ko notify

    if not channels:
        await notify.send("⚠️ Koi channel add nahi hai!")
        return

    BATCH    = get_batch_size()
    next_id  = get_next_message_id()
    total_ch = len(channels)

    header = (
        f"{'🔧 Manual' if manual else '🕐 Scheduled'} Post Session\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 Total Channels : {total_ch}\n"
        f"▶️ Start ID        : {next_id}\n"
        f"📦 Batch Size      : {BATCH}\n"
        f"🕒 Time           : {now_str()}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await notify.send(header)
    await asyncio.sleep(0.3)

    for idx, ch in enumerate(channels, 1):
        chat_id  = ch["chat_id"]
        start_id = next_id
        end_id   = next_id + BATCH
        errors   = []
        sent     = 0

        ch_line = f"\n➡️  CH {idx}/{total_ch} ({chat_id})\n   IDs {start_id}–{end_id - 1} | Sending..."
        await notify.update(ch_line)

        for msg_id in range(start_id, end_id):
            try:
                # Pehle original caption fetch karo
                original_caption = await get_original_caption(bot, msg_id)

                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=STORAGE_CHANNEL_ID,
                    message_id=msg_id,
                    caption=build_caption(original_caption),
                    reply_markup=build_buttons(),
                )
                sent += 1
            except Exception as e:
                err_text = str(e)
                errors.append(f"   ❌ msg {msg_id}: {err_text[:50]}")
                print(f"❌ msg {msg_id} → {chat_id}: {e}")

            await asyncio.sleep(0.5)

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

    save_next_message_id(next_id)
    footer = f"\n━━━━━━━━━━━━━━━━━━━━\n✅ Done! Next start ID: {next_id}"
    await notify.update(footer)
    print(f"✅ Session done | Next ID: {next_id}")

# ================= CONTENT REMOVAL FLOW (Non-Admin) =================
WAITING_FOR_LINK = 1

async def user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Non-admin users ke liye /start — content removal shuru karo"""
    user = update.effective_user
    bot  = context.bot

    # Log: user ne bot start kiya
    await log(
        bot,
        f"👤 New User\n"
        f"Name : {user.full_name}\n"
        f"ID   : {user.id}\n"
        f"User : @{user.username or 'N/A'}\n"
        f"Time : {now_str()}"
    )

    await update.message.reply_text(
        "👋 Hello!\n\n"
        "If you want to request content removal, please send me the post link.\n\n"
        "📌 Example:\n"
        "https://t.me/c/1234567890/100\n\n"
        "Send the link and I will remove it immediately. ✅"
    )
    return WAITING_FOR_LINK

async def handle_removal_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne post link bheja — parse karke delete karo"""
    user    = update.effective_user
    bot     = context.bot
    text    = update.message.text.strip()

    channel_id, message_id = parse_post_link(text)

    if not channel_id:
        await update.message.reply_text(
            "❌ Invalid link format!\n\n"
            "Please send a valid Telegram post link:\n"
            "https://t.me/c/1234567890/100"
        )
        return WAITING_FOR_LINK   # dobara try karne do

    # Log: removal request mili
    await log(
        bot,
        f"📩 Content Removal Request\n"
        f"From : {user.full_name} (@{user.username or 'N/A'})\n"
        f"ID   : {user.id}\n"
        f"Link : {text}\n"
        f"Time : {now_str()}"
    )

    # Admins ko bhi notify karo
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🚨 Content Removal Request\n\n"
                    f"👤 User: {user.full_name} (@{user.username or 'N/A'})\n"
                    f"🆔 ID: {user.id}\n"
                    f"🔗 Link: {text}\n"
                    f"🕒 Time: {now_str()}"
                )
            )
        except Exception as e:
            print(f"[Admin notify] {e}")

    # Post delete karo
    try:
        await bot.delete_message(chat_id=channel_id, message_id=message_id)

        # Log: deleted
        await log(
            bot,
            f"🗑️ Post Deleted Successfully\n"
            f"Channel : {channel_id}\n"
            f"Msg ID  : {message_id}\n"
            f"By      : {user.full_name} (request)\n"
            f"Time    : {now_str()}"
        )

        await update.message.reply_text(
            "✅ Done! The post has been removed successfully.\n\n"
            "Thank you for reporting. 🙏"
        )

    except Exception as e:
        err = str(e)
        print(f"[Delete error] {e}")

        await log(
            bot,
            f"❌ Delete Failed\n"
            f"Channel : {channel_id}\n"
            f"Msg ID  : {message_id}\n"
            f"Error   : {err}\n"
            f"Time    : {now_str()}"
        )

        await update.message.reply_text(
            f"❌ Could not delete the post.\n\n"
            f"Reason: {err}\n\n"
            "Please make sure the link is correct and the bot is admin in that channel."
        )

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ================= ADMIN COMMANDS =================
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Auto Post Bot — Admin Panel\n\n"
        "📋 Commands:\n"
        "/addchannel -100xxxx\n"
        "/removechannel -100xxxx\n"
        "/listchannels\n"
        "/status\n"
        "/setid <number>\n"
        "/set_quantity <number>\n"
        "/testpost"
    )

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if not context.args:
        return await update.message.reply_text("Usage: /removechannel -100xxxx")
    chat_id = int(context.args[0])
    result  = channels_col.delete_one({"chat_id": chat_id})
    if result.deleted_count:
        await update.message.reply_text(f"🗑️ Removed: {chat_id}")
    else:
        await update.message.reply_text("⚠️ Not found")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = list(channels_col.find())
    if not channels:
        return await update.message.reply_text("📭 Koi channel nahi hai")
    text = f"📢 Total: {len(channels)}\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"{i}. {ch['chat_id']}\n"
    await update.message.reply_text(text)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    next_id  = get_next_message_id()
    ch_count = channels_col.count_documents({})
    batch    = get_batch_size()
    current  = now_str()
    await update.message.reply_text(
        f"📊 Bot Status\n\n"
        f"🕐 Bot time       : {current} ({os.getenv('TIMEZONE', 'Asia/Kolkata')})\n"
        f"▶️ Next Msg ID    : {next_id}\n"
        f"📦 Batch Size      : {batch}\n"
        f"📢 Total Channels : {ch_count}\n"
        f"📅 Post slots     : {len(POST_TIMES)}\n\n"
        f"{'✅ Current time matches a slot!' if current in POST_TIMES else '⏳ Waiting for next slot...'}"
    )

async def set_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /setid <number>")
    try:
        new_id = int(context.args[0])
        save_next_message_id(new_id)
        await update.message.reply_text(f"✅ Next ID set to: {new_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            f"Usage: /set_quantity <number>\n"
            f"Current batch size: {get_batch_size()}"
        )
    try:
        size = int(context.args[0])
        if size < 1 or size > 100:
            return await update.message.reply_text("❌ 1 se 100 ke beech number do")
        save_batch_size(size)
        await update.message.reply_text(f"✅ Batch size set to: {size} messages per channel")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def testpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Manual post trigger kiya...")
    await do_post(context.bot, manual=True)

# ================= ADMIN GATE — runs before every admin command =================
def admin_only(func):
    """Decorator: non-admins ko block karo"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return await update.message.reply_text("❌ Access Denied")
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

add_channel    = admin_only(add_channel)
remove_channel = admin_only(remove_channel)
list_channels  = admin_only(list_channels)
status         = admin_only(status)
set_id         = admin_only(set_id)
set_quantity   = admin_only(set_quantity)
testpost       = admin_only(testpost)

# ================= START HANDLER (route admin vs user) =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        return await admin_start(update, context)
    else:
        return await user_start(update, context)

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
    print(f"🚀 Bot started | Admins: {ADMIN_IDS} | Time: {now_str()} | Slots: {len(POST_TIMES)}")

# ================= MAIN =================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Non-admin content removal conversation ────────────────────────────────
    removal_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=~filters.User(list(ADMIN_IDS)))
        ],
        states={
            WAITING_FOR_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_removal_link)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    # ── Admin commands ────────────────────────────────────────────────────────
    app.add_handler(removal_conv)
    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("addchannel",     add_channel))
    app.add_handler(CommandHandler("removechannel",  remove_channel))
    app.add_handler(CommandHandler("listchannels",   list_channels))
    app.add_handler(CommandHandler("status",         status))
    app.add_handler(CommandHandler("setid",          set_id))
    app.add_handler(CommandHandler("set_quantity",   set_quantity))
    app.add_handler(CommandHandler("testpost",       testpost))

    app.run_polling()

if __name__ == "__main__":
    main()
