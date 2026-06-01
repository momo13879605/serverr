import asyncio
import logging
import re
from datetime import datetime
import os
import aiosqlite
import asyncssh
from cryptography.fernet import Fernet
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ======================== تنظیمات مستقیم ========================
TOKEN = "7635713211:AAH7A4PInJmeYgoLXSeFTPl9EaTquCyS24M"                     # توکن ربات تلگرام
ADMIN_IDS = [5914346958]           # لیست آیدی‌های عددی ادمین‌ها
MAX_MESSAGE_LENGTH = 4096
MAX_CONCURRENT_SSH = 5                       # حداکثر تعداد اتصال همزمان SSH
COMMAND_TIMEOUT = 30                         # تایم‌اوت اجرای دستور (ثانیه)
DB_PATH = "servers.db"
KEY_FILE = "secret.key"
# ================================================================

# -------------------- لاگ‌گیری --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------- رمزنگاری --------------------
def load_or_create_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    return key

cipher = Fernet(load_or_create_key())

def encrypt(text: str) -> str:
    return cipher.encrypt(text.encode()).decode()

def decrypt(text: str) -> str:
    return cipher.decrypt(text.encode()).decode()

# -------------------- دیتابیس (ناهمگام) --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                is_logged_in INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS command_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                output TEXT,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (server_id) REFERENCES servers (id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # ثبت ادمین‌های اولیه از ثابت‌ها
        for uid in ADMIN_IDS:
            await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
        # تنظیمات پیش‌فرض
        await db.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('max_concurrent_ssh', ?)",
            (str(MAX_CONCURRENT_SSH),)
        )
        await db.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('command_timeout', ?)",
            (str(COMMAND_TIMEOUT),)
        )
        await db.commit()

# -------------------- توابع کمکی دیتابیس --------------------
async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
            return (await cur.fetchone()) is not None

async def get_all_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cur:
            return [row[0] for row in await cur.fetchall()]

async def add_admin_to_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def remove_admin_from_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()

async def register_user(user_id: int, first_name: str, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
            (user_id, first_name, username)
        )
        await db.commit()

async def add_server(user_id, name, ip, username, password):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO servers (user_id, name, ip, username, password, is_logged_in) VALUES (?,?,?,?,?,0)",
            (user_id, name, ip, username, encrypt(password))
        )
        server_id = cursor.lastrowid
        await db.commit()
    return server_id

async def get_user_servers(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, ip, username, password, is_logged_in FROM servers WHERE user_id=?",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    servers = []
    for r in rows:
        servers.append({
            "id": r[0],
            "name": r[1],
            "ip": r[2],
            "username": r[3],
            "password": decrypt(r[4]),
            "is_logged_in": bool(r[5]),
        })
    return servers

async def get_server_by_id(server_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, name, ip, username, password, is_logged_in FROM servers WHERE id=?",
            (server_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "name": row[2],
        "ip": row[3],
        "username": row[4],
        "password": decrypt(row[5]),
        "is_logged_in": bool(row[6]),
    }

async def set_server_status(server_id, status: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE servers SET is_logged_in=? WHERE id=?", (int(status), server_id))
        if status:
            await db.execute("UPDATE servers SET last_used=CURRENT_TIMESTAMP WHERE id=?", (server_id,))
        await db.commit()

async def update_server_name(server_id, new_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE servers SET name=? WHERE id=?", (new_name, server_id))
        await db.commit()

async def delete_server_from_db(server_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM servers WHERE id=?", (server_id,))
        await db.commit()

async def get_all_servers_admin():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT s.id, s.user_id, s.name, s.ip, s.username, s.is_logged_in, u.first_name "
            "FROM servers s LEFT JOIN users u ON s.user_id = u.user_id"
        ) as cur:
            return await cur.fetchall()

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, first_name, username, joined_at FROM users") as cur:
            return await cur.fetchall()

async def get_settings():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM bot_settings") as cur:
            rows = await cur.fetchall()
    return {k: v for k, v in rows}

async def update_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def add_command_history(server_id, user_id, command, output):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO command_history (server_id, user_id, command, output) VALUES (?,?,?,?)",
            (server_id, user_id, command, output)
        )
        await db.commit()

# -------------------- مدیریت اتصال SSH --------------------
connections_cache: dict[int, asyncssh.SSHClientConnection] = {}
ssh_semaphore: asyncio.Semaphore = None  # در main مقداردهی می‌شود

async def get_ssh_connection(server_id: int) -> asyncssh.SSHClientConnection:
    """برمی‌گرداند یک کانکشن فعال برای سرور داده شده. در صورت نیاز مجدداً متصل می‌شود."""
    global ssh_semaphore
    conn = connections_cache.get(server_id)
    if conn is not None and conn.is_closed():
        conn = None
        connections_cache.pop(server_id, None)
    if conn is None:
        server = await get_server_by_id(server_id)
        if not server:
            raise ValueError("Server not found")
        async with ssh_semaphore:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    server["ip"],
                    username=server["username"],
                    password=server["password"],
                    known_hosts=None,
                ),
                timeout=10
            )
        connections_cache[server_id] = conn
        await set_server_status(server_id, True)
    return conn

async def execute_command(server_id: int, user_id: int, command: str) -> str:
    try:
        conn = await get_ssh_connection(server_id)
        settings = await get_settings()
        timeout = int(settings.get("command_timeout", COMMAND_TIMEOUT))
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        output = stdout
        if stderr:
            output += ("\n" + stderr) if output else stderr
        await add_command_history(server_id, user_id, command, output)
        return output.strip() or "(خروجی خالی)"
    except asyncio.TimeoutError:
        return "⏰ اجرای دستور بیش از حد طول کشید (timeout)"
    except asyncssh.Error as e:
        return f"❌ خطای SSH: {e}"
    except Exception as e:
        return f"❌ خطای غیرمنتظره: {e}"

async def logout_server(server_id: int):
    conn = connections_cache.pop(server_id, None)
    if conn:
        conn.close()
        await conn.wait_closed()
    await set_server_status(server_id, False)

# -------------------- تقسیم پیام بلند --------------------
async def send_long_message(update: Update, text: str):
    """ارسال پیام با رعایت محدودیت طول تلگرام"""
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        chunk = text[i:i+MAX_MESSAGE_LENGTH]
        if update.message:
            await update.message.reply_text(chunk)
        elif update.callback_query:
            await update.callback_query.message.reply_text(chunk)

# -------------------- وضعیت‌های ConversationHandler --------------------
WAITING_FOR_IP, WAITING_FOR_USERNAME, WAITING_FOR_PASSWORD = range(3)
WAITING_FOR_NEW_NAME = 4

# -------------------- کیبوردهای اصلی --------------------
def main_keyboard(is_admin: bool, is_active: bool):
    keyboard = []
    if is_active:
        keyboard.append(["➕ افزودن سرور جدید", "🔄 جابجایی بین سرورها"])
        keyboard.append(["🚪 خروج از سرور", "✏️ تغییر نام سرور"])
    else:
        keyboard.append(["➕ افزودن سرور جدید", "🔄 جابجایی بین سرورها"])
    if is_admin:
        keyboard.append(["🛡 پنل مدیریت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# -------------------- هندلرها --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.first_name, user.username)
    context.user_data.clear()
    servers = await get_user_servers(user.id)
    active_server = next((s for s in servers if s["is_logged_in"]), None)
    if active_server:
        context.user_data["active_server_id"] = active_server["id"]
    else:
        context.user_data.pop("active_server_id", None)

    admin = await is_admin(user.id)
    await update.message.reply_text(
        f"👋 خوش آمدید {user.first_name}!\n"
        "برای شروع یک سرور اضافه کنید یا از منو استفاده کنید.",
        reply_markup=main_keyboard(admin, active_server is not None)
    )

# ---------- مکالمۀ افزودن سرور ----------
async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "adding_server"
    await update.message.reply_text("📡 لطفاً آی‌پی سرور را وارد کنید:")
    return WAITING_FOR_IP

async def add_server_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = update.message.text.strip()
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
        await update.message.reply_text("❌ فرمت آی‌پی نامعتبر. دوباره وارد کنید:")
        return WAITING_FOR_IP
    context.user_data["temp_ip"] = ip
    await update.message.reply_text("👤 نام کاربری SSH را وارد کنید:")
    return WAITING_FOR_USERNAME

async def add_server_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_username"] = update.message.text.strip()
    await update.message.reply_text("🔐 رمز عبور SSH را وارد کنید:")
    return WAITING_FOR_PASSWORD

async def add_server_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    ip = context.user_data.get("temp_ip")
    username = context.user_data.get("temp_username")
    if not all([ip, username]):
        await update.message.reply_text("⚠️ اطلاعات ناقص. عملیات لغو شد.")
        context.user_data.clear()
        return ConversationHandler.END
    await update.message.reply_text("⏳ در حال اتصال به سرور...")
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(ip, username=username, password=password, known_hosts=None),
            timeout=10
        )
        conn.close()
        server_name = f"سرور {ip}"
        server_id = await add_server(update.effective_user.id, server_name, ip, username, password)
        old_active = context.user_data.get("active_server_id")
        if old_active:
            await logout_server(old_active)
        global ssh_semaphore
        async with ssh_semaphore:
            live_conn = await asyncssh.connect(ip, username=username, password=password, known_hosts=None)
        connections_cache[server_id] = live_conn
        await set_server_status(server_id, True)
        context.user_data["active_server_id"] = server_id
        context.user_data.pop("state", None)
        await update.message.reply_text(
            "✅ ورود موفق!\nاکنون می‌توانید دستورات خود را ارسال کنید.",
            reply_markup=main_keyboard(await is_admin(update.effective_user.id), True)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ اتصال ناموفق: {e}")
    finally:
        for k in ("temp_ip", "temp_username", "temp_password", "state"):
            context.user_data.pop(k, None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ عملیات لغو شد.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- مکالمۀ تغییر نام ----------
async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    servers = await get_user_servers(update.effective_user.id)
    if not servers:
        await update.message.reply_text("❌ سروری ندارید.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"{s['name']} ({s['ip']})", callback_data=f"renamesel_{s['id']}")] for s in servers]
    await update.message.reply_text("✏️ سرور مورد نظر برای تغییر نام را انتخاب کنید:",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_FOR_NEW_NAME

async def rename_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split("_")[1])
    context.user_data["rename_server_id"] = server_id
    await query.edit_message_text("✏️ لطفاً نام جدید را وارد کنید:")
    return WAITING_FOR_NEW_NAME

async def rename_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    server_id = context.user_data.get("rename_server_id")
    if not server_id:
        await update.message.reply_text("❌ خطا. عملیات لغو شد.")
        return ConversationHandler.END
    await update_server_name(server_id, new_name)
    await update.message.reply_text(f"✅ نام سرور به «{new_name}» تغییر یافت.")
    context.user_data.pop("rename_server_id", None)
    return ConversationHandler.END

# ---------- هندلر پیام‌های متنی (اجرای دستور، منوها) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    user_id = user.id
    user_data = context.user_data

    # دکمه‌های منو
    if text == "➕ افزودن سرور جدید":
        return await add_server_start(update, context)
    if text == "🔄 جابجایی بین سرورها":
        servers = await get_user_servers(user_id)
        if not servers:
            await update.message.reply_text("❌ هیچ سروری ندارید.")
            return
        keyboard = [[InlineKeyboardButton(f"{s['name']} ({s['ip']})", callback_data=f"switch_{s['id']}")] for s in servers]
        await update.message.reply_text("🔁 یک سرور را برای ورود انتخاب کنید:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return
    if text == "🚪 خروج از سرور":
        servers = await get_user_servers(user_id)
        if not servers:
            await update.message.reply_text("❌ هیچ سروری ندارید.")
            return
        keyboard = [[InlineKeyboardButton(f"🚪 {s['name']} ({s['ip']})", callback_data=f"logout_{s['id']}")] for s in servers]
        await update.message.reply_text("از کدام سرور خارج می‌شوید؟",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return
    if text == "✏️ تغییر نام سرور":
        return await rename_start(update, context)
    if text == "🛡 پنل مدیریت" and await is_admin(user_id):
        return await admin_panel(update, context)

    # اجرای دستور Bash روی سرور فعال
    active_id = user_data.get("active_server_id")
    if not active_id:
        await update.message.reply_text("⚠️ شما وارد هیچ سروری نشده‌اید.")
        return
    # بررسی دستورات مخرب (ساده)
    dangerous_patterns = [r"rm\s+-rf\s+/", r">\s*/dev/sda", r"mkfs", r"dd\s+if="]
    for pat in dangerous_patterns:
        if re.search(pat, text, re.IGNORECASE):
            await update.message.reply_text("⛔️ دستور مخرب تشخیص داده شد، اجرا نمی‌شود!")
            return
    await update.message.reply_text("⏳ در حال اجرای دستور...")
    output = await execute_command(active_id, user_id, text)
    await send_long_message(update, output)

# ---------- کلیک روی دکمه‌های شیشه‌ای ----------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    user_data = context.user_data

    if data.startswith("switch_"):
        server_id = int(data.split("_")[1])
        server = await get_server_by_id(server_id)
        if not server or server["user_id"] != user_id:
            await query.edit_message_text("❌ دسترسی غیرمجاز.")
            return
        await query.edit_message_text(f"⏳ در حال اتصال به {server['name']} ...")
        try:
            await get_ssh_connection(server_id)
            old_active = user_data.get("active_server_id")
            if old_active:
                await logout_server(old_active)
            user_data["active_server_id"] = server_id
            await query.edit_message_text(f"✅ به {server['name']} متصل شدید.\nاکنون می‌توانید دستور بفرستید.")
            await context.bot.send_message(
                chat_id=user_id,
                text="منوی اصلی:",
                reply_markup=main_keyboard(await is_admin(user_id), True)
            )
        except Exception as e:
            await query.edit_message_text(f"❌ اتصال ناموفق: {e}")

    elif data.startswith("logout_"):
        server_id = int(data.split("_")[1])
        server = await get_server_by_id(server_id)
        if not server or server["user_id"] != user_id:
            await query.edit_message_text("❌ دسترسی غیرمجاز.")
            return
        keyboard = [
            [
                InlineKeyboardButton("✅ بله", callback_data=f"confirmlogout_{server_id}"),
                InlineKeyboardButton("❌ خیر", callback_data="cancellogout")
            ]
        ]
        await query.edit_message_text(
            f"⚠️ مطمئن هستید از {server['name']} خارج شوید؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("confirmlogout_"):
        server_id = int(data.split("_")[1])
        await logout_server(server_id)
        if user_data.get("active_server_id") == server_id:
            user_data.pop("active_server_id", None)
        await query.edit_message_text("✅ از سرور خارج شدید.")
        await context.bot.send_message(
            chat_id=user_id,
            text="منوی اصلی:",
            reply_markup=main_keyboard(await is_admin(user_id), False)
        )

    elif data == "cancellogout":
        await query.edit_message_text("❌ عملیات لغو شد.")

    # ---------- پنل مدیریت ----------
    elif data == "admin_all_servers":
        if not await is_admin(user_id):
            return
        rows = await get_all_servers_admin()
        if not rows:
            await query.edit_message_text("ℹ️ هیچ سروری وجود ندارد.")
            return
        text = "📋 لیست تمام سرورها:\n\n"
        for r in rows:
            text += f"🆔 {r[0]} | کاربر: {r[1]} ({r[6]}) | نام: {r[2]} | IP: {r[3]} | وضعیت: {'آنلاین' if r[5] else 'آفلاین'}\n"
        await query.edit_message_text(text[:4096])

    elif data == "admin_list_users":
        if not await is_admin(user_id):
            return
        users = await get_all_users()
        if not users:
            await query.edit_message_text("ℹ️ هیچ کاربری ثبت نشده.")
            return
        text = "👥 لیست کاربران:\n\n"
        for u in users:
            text += f"🆔 {u[0]} | نام: {u[1]} | یوزرنیم: @{u[2] or '---'} | تاریخ عضویت: {u[3]}\n"
        await query.edit_message_text(text[:4096])

    elif data == "admin_add_admin":
        if not await is_admin(user_id):
            return
        context.user_data["admin_action"] = "add_admin"
        await query.edit_message_text("🆔 آیدی عددی ادمین جدید را وارد کنید:")

    elif data == "admin_remove_admin":
        if not await is_admin(user_id):
            return
        context.user_data["admin_action"] = "remove_admin"
        await query.edit_message_text("🆔 آیدی عددی ادمینی که می‌خواهید حذف کنید را وارد کنید:")

    elif data == "admin_broadcast":
        if not await is_admin(user_id):
            return
        context.user_data["admin_action"] = "broadcast"
        await query.edit_message_text("📢 پیام خود را برای ارسال به همه کاربران وارد کنید:")

    elif data == "admin_delete_server":
        if not await is_admin(user_id):
            return
        rows = await get_all_servers_admin()
        if not rows:
            await query.edit_message_text("ℹ️ سروری برای حذف وجود ندارد.")
            return
        keyboard = [[InlineKeyboardButton(f"🗑 {r[2]} (کاربر {r[1]})", callback_data=f"admdel_{r[0]}")] for r in rows]
        await query.edit_message_text("🗑 سرور مورد نظر برای حذف را انتخاب کنید:",
                                      reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("admdel_"):
        if not await is_admin(user_id):
            return
        server_id = int(data.split("_")[1])
        await logout_server(server_id)
        await delete_server_from_db(server_id)
        await query.edit_message_text(f"✅ سرور {server_id} حذف شد.")

    elif data == "admin_stats":
        if not await is_admin(user_id):
            return
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM servers") as cur:
                server_count = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                user_count = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM command_history") as cur:
                cmd_count = (await cur.fetchone())[0]
        text = f"📊 آمار ربات:\n\nسرورها: {server_count}\nکاربران: {user_count}\nدستورات اجرا شده: {cmd_count}"
        await query.edit_message_text(text)

    elif data == "admin_settings":
        if not await is_admin(user_id):
            return
        settings = await get_settings()
        text = "⚙️ تنظیمات فعلی:\n\n"
        text += f"max_concurrent_ssh: {settings.get('max_concurrent_ssh', MAX_CONCURRENT_SSH)}\n"
        text += f"command_timeout: {settings.get('command_timeout', COMMAND_TIMEOUT)} ثانیه\n"
        text += "\nبرای تغییر یکی از مقادیر، از دستور /setsetting key value استفاده کنید. (فقط ادمین)"
        await query.edit_message_text(text)

    elif data == "admin_back":
        await admin_panel(query, context)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    keyboard = [
        [InlineKeyboardButton("📋 همه سرورها", callback_data="admin_all_servers")],
        [InlineKeyboardButton("👥 لیست کاربران", callback_data="admin_list_users")],
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data="admin_add_admin"),
         InlineKeyboardButton("❌ حذف ادمین", callback_data="admin_remove_admin")],
        [InlineKeyboardButton("📢 ارسال همگانی", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🗑 حذف سرور", callback_data="admin_delete_server")],
        [InlineKeyboardButton("📊 آمار", callback_data="admin_stats"),
         InlineKeyboardButton("⚙️ تنظیمات", callback_data="admin_settings")],
    ]
    await update.message.reply_text("🛡 پنل مدیریت:", reply_markup=InlineKeyboardMarkup(keyboard))

# ---------- مدیریت ورودی‌های ادمین ----------
async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        return
    action = context.user_data.get("admin_action")
    text = update.message.text.strip()
    if action == "add_admin":
        try:
            new_admin = int(text)
            await add_admin_to_db(new_admin)
            await update.message.reply_text("✅ ادمین اضافه شد.")
        except ValueError:
            await update.message.reply_text("❌ آیدی عددی معتبر نیست.")
        finally:
            context.user_data.pop("admin_action", None)

    elif action == "remove_admin":
        try:
            target = int(text)
            await remove_admin_from_db(target)
            await update.message.reply_text("✅ ادمین حذف شد.")
        except ValueError:
            await update.message.reply_text("❌ آیدی عددی معتبر نیست.")
        finally:
            context.user_data.pop("admin_action", None)

    elif action == "broadcast":
        users = await get_all_users()
        success = 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=u[0], text=text)
                success += 1
            except:
                pass
        await update.message.reply_text(f"✅ پیام به {success} از {len(users)} کاربر ارسال شد.")
        context.user_data.pop("admin_action", None)

# ---------- تنظیم دستی تنظیمات (برای ادمین) ----------
async def set_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("❌ فرمت صحیح: /setsetting key value")
        return
    key, value = args[0], args[1]
    await update_setting(key, value)
    if key == "max_concurrent_ssh":
        global ssh_semaphore
        ssh_semaphore = asyncio.Semaphore(int(value))
    await update.message.reply_text(f"✅ تنظیم {key} به {value} تغییر یافت.")

# ---------- اصلی ----------
async def post_init(app: Application):
    global ssh_semaphore
    settings = await get_settings()
    max_ssh = int(settings.get("max_concurrent_ssh", MAX_CONCURRENT_SSH))
    ssh_semaphore = asyncio.Semaphore(max_ssh)

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # ConversationHandler برای افزودن سرور
    add_server_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ افزودن سرور جدید$"), add_server_start)],
        states={
            WAITING_FOR_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_server_ip)],
            WAITING_FOR_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_server_username)],
            WAITING_FOR_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_server_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler برای تغییر نام
    rename_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ تغییر نام سرور$"), rename_start)],
        states={
            WAITING_FOR_NEW_NAME: [
                CallbackQueryHandler(rename_select, pattern="^renamesel_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive_name)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setsetting", set_setting))
    app.add_handler(add_server_conv)
    app.add_handler(rename_conv)
    # هندلر پیام‌های متنی معمولی (منوها و اجرای دستور)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    # هندلر مخصوص دریافت ورودی‌های ادمین (باید بعد از هندلرهای اصلی باشد)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_input), group=1)

    logger.info("ربات شروع شد...")
    app.run_polling()

if __name__ == "__main__":
    import os  # فقط برای load_or_create_key نیاز است
    asyncio.run(init_db())
    main()