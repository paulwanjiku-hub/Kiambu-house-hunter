import os
import csv
import logging
import hashlib
import sqlite3
from typing import List
from asyncio import Lock
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------------
# Setup
# -------------------------
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set")

LISTINGS_FILE = "listings_with_url.csv"

# Detect Render environment for persistent storage
if os.getenv("RENDER"):
    os.makedirs("/var/data", exist_ok=True)
    FAV_DB = "/var/data/favorites.db"
else:
    FAV_DB = "favorites.db"


# -------------------------
# Database helpers
# -------------------------
def init_db():
    conn = sqlite3.connect(FAV_DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS favorites
                 (user_id TEXT, title TEXT, price TEXT, bedrooms TEXT,
                  location TEXT, url TEXT, image_url TEXT)""")
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized: %s", FAV_DB)

def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()

def add_favorite(user_id: int, listing: dict):
    conn = sqlite3.connect(FAV_DB)
    c = conn.cursor()
    c.execute("""INSERT INTO favorites (user_id, title, price, bedrooms, location, url, image_url)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (str(user_id), listing.get("title", ""), str(listing.get("price", "")),
               listing.get("bedrooms", ""), listing.get("location", ""),
               listing.get("url", ""), listing.get("image_url", "")))
    conn.commit()
    conn.close()

def remove_favorite_by_hash(user_id: int, url_hash: str) -> bool:
    conn = sqlite3.connect(FAV_DB)
    c = conn.cursor()
    c.execute("SELECT url FROM favorites WHERE user_id = ?", (str(user_id),))
    rows = c.fetchall()
    removed = False
    for row in rows:
        if _md5(row[0]) == url_hash:
            c.execute("DELETE FROM favorites WHERE user_id = ? AND url = ?", (str(user_id), row[0]))
            removed = True
    conn.commit()
    conn.close()
    return removed

def load_user_favorites(user_id: int) -> List[dict]:
    conn = sqlite3.connect(FAV_DB)
    c = conn.cursor()
    c.execute("SELECT * FROM favorites WHERE user_id = ?", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "title": r[1], "price": r[2],
             "bedrooms": r[3], "location": r[4], "url": r[5], "image_url": r[6]} for r in rows]

# -------------------------
# Listings
# -------------------------
listings: List[dict] = []

def normalize_bedrooms(raw) -> str:
    if raw is None or str(raw).strip() == "":
        return "Unknown"
    s = str(raw).strip()
    try:
        v = int(float(s))
        return "Bedsitter" if v == 0 else str(v)
    except:
        if s.lower() in ("bedsitter", "bedsit", "bed sitter"):
            return "Bedsitter"
        return s

def safe_int_price(raw) -> int:
    try:
        return int(float(raw))
    except:
        return 0

def load_listings():
    global listings
    listings = []
    if not os.path.exists(LISTINGS_FILE):
        logger.warning("Listings file not found: %s", LISTINGS_FILE)
        return
    with open(LISTINGS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            loc = (r.get("location") or "").strip().title()
            br = normalize_bedrooms(r.get("bedrooms", ""))
            price = safe_int_price(r.get("price", 0))
            listings.append({
                "title": (r.get("title") or "").strip(),
                "location": loc,
                "image_url": (r.get("image_url") or "").strip(),
                "url": (r.get("url") or "").strip(),
                "bedrooms": br,
                "price": price,
            })
    logger.info("✅ Loaded %d listings", len(listings))

load_listings()

# -------------------------
# Telegram bot handlers
# -------------------------
user_locks = {}

def get_user_lock(uid: int) -> Lock:
    if uid not in user_locks:
        user_locks[uid] = Lock()
    return user_locks[uid]

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not listings:
        await update.message.reply_text("Sorry, no listings available.")
        return
    # Show all available locations as buttons
    unique_locations = sorted({l['location'] for l in listings})
    kb = [[InlineKeyboardButton(loc, callback_data=f"location|{loc}")] for loc in unique_locations]
    await update.message.reply_text("🏡 Welcome! Select a location:", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Commands:\n"
        "/start - Browse listings\n"
        "/favorites - View your saved listings\n"
        "/help - Show this help"
    )

async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    favs = load_user_favorites(update.effective_user.id)
    if not favs:
        await update.message.reply_text("⭐ You have no favorites yet.")
        return
    for fav in favs:
        text = f"{fav['title']}\n💲 {fav['price']} | 🛏 {fav['bedrooms']}\n📍 {fav['location']}\n🔗 {fav['url']}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Remove", callback_data=f"removefav|{_md5(fav['url'])}")]
        ])
        if fav["image_url"]:
            await update.message.reply_photo(photo=fav["image_url"], caption=text, reply_markup=kb)
        else:
            await update.message.reply_text(text, reply_markup=kb)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    action = data[0]

    if action == "location":
        loc = data[1]
        loc_listings = [l for l in listings if l["location"] == loc]
        if not loc_listings:
            await query.edit_message_text(f"No listings found for {loc}.")
            return
        # Show first listing only (simplify browsing)
        l = loc_listings[0]
        caption = f"{l['title']}\n💲 {l['price']} | 🛏 {l['bedrooms']}\n📍 {l['location']}\n🔗 {l['url']}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Save", callback_data=f"savefav|{_md5(l['url'])}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
        ])
        if l["image_url"]:
            await query.edit_message_media(InputMediaPhoto(media=l["image_url"], caption=caption), reply_markup=kb)
        else:
            await query.edit_message_text(caption, reply_markup=kb)

    elif action == "savefav":
        url_hash = data[1]
        # Find listing by hash
        for l in listings:
            if _md5(l["url"]) == url_hash:
                add_favorite(query.from_user.id, l)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text("✅ Saved to favorites.")
                break

    elif action == "removefav":
        url_hash = data[1]
        if remove_favorite_by_hash(query.from_user.id, url_hash):
            await query.edit_message_text("❌ Removed from favorites.")

    elif action == "back":
        await cmd_start(update, context)

async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to begin.")

# -------------------------
# Entrypoint
# -------------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))
    logger.info("🤖 Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
