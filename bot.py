import os
import re
import time
import html
import random
import string
import sqlite3
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import telebot
from bs4 import BeautifulSoup
from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
DOMAIN = os.getenv("EMAIL_DOMAIN", "5xu.vn").strip().lower()
BASE_URL = os.getenv("GENERATOR_BASE_URL", "https://generator.email").strip().rstrip("/")
DB_PATH = os.getenv("DB_PATH", "/data/email_bot.sqlite3").strip()
AUTO_CHECK_SECONDS = max(3.0, float(os.getenv("AUTO_CHECK_SECONDS", "5")))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "20")))
AUTO_WORKERS = max(1, min(16, int(os.getenv("AUTO_WORKERS", "6"))))
MAX_AUTO_MESSAGES_PER_EMAIL = max(1, min(20, int(os.getenv("MAX_AUTO_MESSAGES_PER_EMAIL", "10"))))

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DEFAULT_FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "").strip()
DEFAULT_FORCE_SUB_URL = os.getenv("FORCE_SUB_URL", "").strip()

bot = telebot.TeleBot(BOT_TOKEN)
state_lock = threading.RLock()
user_states = {}
subscription_cache = {}
email_locks = {}

# ============================================================
# Text / localization
# ============================================================

AR = {
    "create": "📧 إنشاء إيميل",
    "check": "📨 فحص الرسائل",
    "mine": "📋 إيميلاتي",
    "delete": "🗑️ حذف إيميل",
    "move": "🔄 نقل إيميل",
    "lang": "🌐 English",
    "welcome": "أهلاً بك في بوت الإيميلات.",
    "choose": "اختر من الأزرار 👇",
    "created": "✅ تم إنشاء إيميل جديد",
    "current": "📧 إيميلك الحالي",
    "no_emails": "📭 لا توجد لديك إيميلات حالياً.",
    "my_emails": "📋 إيميلاتك الخاصة",
    "check_choose": "اختر الإيميل الذي تريد فحصه:",
    "delete_choose": "اختر الإيميل الذي تريد حذفه:",
    "move_choose": "اختر الإيميل الذي تريد نقله:",
    "checking": "🔄 جاري فحص الرسائل...",
    "empty": "📭 لا توجد رسائل حالياً.",
    "deleted": "🗑️ تم حذف الإيميل من البوت.",
    "not_found": "❌ الإيميل غير موجود أو لا تملكه.",
    "confirm_delete": "هل أنت متأكد من حذف هذا الإيميل من البوت؟",
    "yes_delete": "✅ نعم، حذف",
    "cancel": "❌ إلغاء",
    "move_target": "أرسل الآن ID تيليجرام للمستخدم المستلم، أو @username.\n\nيجب أن يكون المستخدم قد بدأ البوت مرة واحدة على الأقل.",
    "bad_target": "❌ لم أجد المستخدم. أرسل Telegram ID صحيح أو @username لمستخدم سبق أن بدأ البوت.",
    "moved": "✅ تم نقل الإيميل بنجاح.",
    "received": "📥 تم نقل إيميل إلى حسابك",
    "self_move": "❌ لا يمكن نقل الإيميل إلى نفس حسابك.",
    "lang_changed": "✅ تم تغيير اللغة إلى العربية.",
    "sub_required": "🔒 يجب الاشتراك في القناة أولاً ثم اضغط «تحقق من الاشتراك».",
    "sub_check": "✅ تحقق من الاشتراك",
    "sub_ok": "✅ تم التحقق من الاشتراك.",
    "sub_fail": "❌ لم يتم العثور على اشتراكك بعد.",
    "sub_config_error": "⚠️ الاشتراك الإجباري مفعّل لكن إعداد القناة غير مكتمل. تواصل مع الإدارة.",
    "mail_error": "❌ تعذر جلب البريد حالياً. حاول مرة أخرى.",
    "new_mail": "📬 وصلت رسالة جديدة",
    "from": "👤 من",
    "subject": "📝 العنوان",
    "to": "📧 إلى",
    "no_subject": "بدون عنوان",
    "unknown": "غير معروف",
    "all_check": "📨 فحص كل الإيميلات",
}

EN = {
    "create": "📧 Create email",
    "check": "📨 Check messages",
    "mine": "📋 My emails",
    "delete": "🗑️ Delete email",
    "move": "🔄 Move email",
    "lang": "🌐 العربية",
    "welcome": "Welcome to the email bot.",
    "choose": "Choose an option 👇",
    "created": "✅ New email created",
    "current": "📧 Your current email",
    "no_emails": "📭 You do not have any emails yet.",
    "my_emails": "📋 Your private emails",
    "check_choose": "Choose the email you want to check:",
    "delete_choose": "Choose the email you want to delete:",
    "move_choose": "Choose the email you want to move:",
    "checking": "🔄 Checking messages...",
    "empty": "📭 No messages right now.",
    "deleted": "🗑️ Email removed from the bot.",
    "not_found": "❌ Email not found or it is not yours.",
    "confirm_delete": "Are you sure you want to remove this email from the bot?",
    "yes_delete": "✅ Yes, delete",
    "cancel": "❌ Cancel",
    "move_target": "Send the recipient's Telegram ID or @username now.\n\nThe recipient must have started the bot at least once.",
    "bad_target": "❌ User not found. Send a valid Telegram ID or @username for a user who already started the bot.",
    "moved": "✅ Email moved successfully.",
    "received": "📥 An email was moved to your account",
    "self_move": "❌ You cannot move an email to your own account.",
    "lang_changed": "✅ Language changed to English.",
    "sub_required": "🔒 You must join the channel first, then tap “Check subscription”.",
    "sub_check": "✅ Check subscription",
    "sub_ok": "✅ Subscription verified.",
    "sub_fail": "❌ Your subscription was not found yet.",
    "sub_config_error": "⚠️ Forced subscription is enabled, but the channel is not configured. Contact the admin.",
    "mail_error": "❌ Could not fetch the mailbox right now. Try again.",
    "new_mail": "📬 New message received",
    "from": "👤 From",
    "subject": "📝 Subject",
    "to": "📧 To",
    "no_subject": "No subject",
    "unknown": "Unknown",
    "all_check": "📨 Check all emails",
}

# ============================================================
# Database
# ============================================================

def db_connect():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                lang TEXT NOT NULL DEFAULT 'ar',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                address TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(owner_user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_emails_owner ON emails(owner_user_id);

            CREATE TABLE IF NOT EXISTS seen_messages (
                email_id INTEGER NOT NULL,
                message_hash TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                PRIMARY KEY(email_id, message_hash),
                FOREIGN KEY(email_id) REFERENCES emails(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('force_sub_enabled','0')"
        )


def upsert_user(tg_user):
    username = (tg_user.username or "").strip().lower()
    with db_connect() as conn:
        # Telegram usernames are unique. Clear an old stale owner before assigning it.
        if username:
            conn.execute(
                "UPDATE users SET username='' WHERE lower(username)=? AND user_id<>?",
                (username, tg_user.id),
            )
        conn.execute(
            """
            INSERT INTO users(user_id, username, lang, created_at)
            VALUES(?,?, 'ar', ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """,
            (tg_user.id, username, int(time.time())),
        )


def get_lang(user_id):
    with db_connect() as conn:
        row = conn.execute("SELECT lang FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["lang"] if row and row["lang"] in {"ar", "en"} else "ar"


def set_lang(user_id, lang):
    lang = "en" if lang == "en" else "ar"
    with db_connect() as conn:
        conn.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))


def tr(user_id):
    return EN if get_lang(user_id) == "en" else AR


def get_setting(key, default=""):
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def force_sub_enabled():
    return get_setting("force_sub_enabled", "0") == "1"


def force_sub_channel():
    return get_setting("force_sub_channel", DEFAULT_FORCE_SUB_CHANNEL).strip()


def force_sub_url():
    saved = get_setting("force_sub_url", DEFAULT_FORCE_SUB_URL).strip()
    if saved:
        return saved
    channel = force_sub_channel()
    if channel.startswith("@") and len(channel) > 1:
        return "https://t.me/" + channel[1:]
    return ""


def list_user_emails(user_id):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id,address,created_at FROM emails WHERE owner_user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_owned_email(user_id, email_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id,address,created_at FROM emails WHERE id=? AND owner_user_id=?",
            (email_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_all_emails():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id,owner_user_id,address FROM emails ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def create_email_for_user(user_id):
    for _ in range(100):
        address = f"{make_name()}@{DOMAIN}"
        try:
            with db_connect() as conn:
                cur = conn.execute(
                    "INSERT INTO emails(owner_user_id,address,created_at) VALUES(?,?,?)",
                    (user_id, address, int(time.time())),
                )
                return {"id": cur.lastrowid, "address": address, "created_at": int(time.time())}
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not create a unique email")


def delete_owned_email(user_id, email_id):
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM emails WHERE id=? AND owner_user_id=?",
            (email_id, user_id),
        )
        return cur.rowcount == 1


def resolve_user_target(value):
    value = (value or "").strip()
    with db_connect() as conn:
        if value.isdigit():
            row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (int(value),)).fetchone()
        elif value.startswith("@") and len(value) > 1:
            row = conn.execute(
                "SELECT user_id FROM users WHERE lower(username)=?",
                (value[1:].lower(),),
            ).fetchone()
        else:
            row = None
    return int(row["user_id"]) if row else None


def move_owned_email(owner_user_id, email_id, target_user_id):
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE emails SET owner_user_id=? WHERE id=? AND owner_user_id=?",
            (target_user_id, email_id, owner_user_id),
        )
        return cur.rowcount == 1


def is_seen(email_id, message_hash):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_messages WHERE email_id=? AND message_hash=?",
            (email_id, message_hash),
        ).fetchone()
    return bool(row)


def mark_seen(email_id, message_hash):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_messages(email_id,message_hash,first_seen_at) VALUES(?,?,?)",
            (email_id, message_hash, int(time.time())),
        )


def get_email_lock(email_id):
    with state_lock:
        lock = email_locks.get(email_id)
        if lock is None:
            lock = threading.RLock()
            email_locks[email_id] = lock
        return lock

# ============================================================
# Generator.email logic — same workflow as supplied code
# ============================================================

def make_name():
    length = random.choice([5, 6])
    chars = string.ascii_lowercase + string.digits
    while True:
        name = "".join(random.choice(chars) for _ in range(length))
        if any(x.isalpha() for x in name) and any(x.isdigit() for x in name):
            return name


def get_full_message(body_tag):
    if not body_tag:
        return ""

    for a in body_tag.find_all("a", href=True):
        href = html.unescape(a.get("href", "").strip())
        title = a.get_text(" ", strip=True)
        if href.startswith(("http://", "https://")):
            replacement = f"\n{title}\n{href}\n" if title else f"\n{href}\n"
            a.replace_with(replacement)

    text = body_tag.get_text("\n", strip=True)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def message_hash(sender, subject, body):
    raw = f"{sender}\n{subject}\n{body}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()


def read_messages(email_address):
    username = email_address.split("@", 1)[0]
    url = f"{BASE_URL}/{DOMAIN}/{username}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Mobile) "
            "AppleWebKit/537.36 Chrome/136.0 Mobile Safari/537.36"
        ),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("div", id="email-table")
    if not table:
        return []

    senders = table.find_all("div", class_="from_div_45g45gg")
    subjects = table.find_all("div", class_="subj_div_45g45gg")
    bodies = table.find_all("div", class_="mess_bodiyy")

    messages = []
    count = max(len(senders), len(subjects), len(bodies))

    for i in range(count):
        sender = senders[i].get_text(" ", strip=True) if i < len(senders) else ""
        subject = subjects[i].get_text(" ", strip=True) if i < len(subjects) else ""
        body = ""
        if i < len(bodies):
            body_copy = BeautifulSoup(str(bodies[i]), "html.parser")
            body = get_full_message(body_copy)

        messages.append(
            {
                "from": sender,
                "subject": subject,
                "body": body,
                "hash": message_hash(sender, subject, body),
            }
        )

    # Keep source order and remove exact duplicates from the same page.
    unique = []
    used = set()
    for item in messages:
        if item["hash"] in used:
            continue
        used.add(item["hash"])
        unique.append(item)
    return unique

# ============================================================
# Telegram helpers
# ============================================================

def main_keyboard(user_id):
    t = tr(user_id)
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton(t["create"]), KeyboardButton(t["check"]))
    kb.add(KeyboardButton(t["mine"]), KeyboardButton(t["delete"]))
    kb.add(KeyboardButton(t["move"]), KeyboardButton(t["lang"]))
    return kb


def send_long_message(chat_id, text, reply_markup=None):
    max_length = 3900
    text = text or ""
    first = True
    while text:
        if len(text) <= max_length:
            bot.send_message(
                chat_id,
                text,
                disable_web_page_preview=True,
                reply_markup=reply_markup if first else None,
            )
            break
        cut = text.rfind("\n", 0, max_length)
        if cut == -1:
            cut = max_length
        part = text[:cut]
        bot.send_message(
            chat_id,
            part,
            disable_web_page_preview=True,
            reply_markup=reply_markup if first else None,
        )
        first = False
        text = text[cut:].strip()


def format_mail(user_id, email_address, mail, automatic=False):
    t = tr(user_id)
    title = t["new_mail"] if automatic else "📬 " + ("الرسالة" if get_lang(user_id) == "ar" else "Message")
    sender = mail.get("from") or t["unknown"]
    subject = mail.get("subject") or t["no_subject"]
    body = mail.get("body") or ("(الرسالة بدون نص ظاهر)" if get_lang(user_id) == "ar" else "(No visible message body)")
    return (
        f"{title}\n\n"
        f"{t['to']}: {email_address}\n"
        f"{t['from']}: {sender}\n"
        f"{t['subject']}: {subject}\n\n"
        f"━━━━━━━━━━━━━━\n\n{body}"
    )


def email_picker(user_id, action):
    emails = list_user_emails(user_id)
    markup = InlineKeyboardMarkup(row_width=1)
    prefix = {"check": "check", "delete": "delete_select", "move": "move_select"}[action]
    for item in emails:
        markup.add(InlineKeyboardButton(item["address"], callback_data=f"{prefix}:{item['id']}"))
    if action == "check" and len(emails) > 1:
        markup.add(InlineKeyboardButton(tr(user_id)["all_check"], callback_data="check_all"))
    return markup


def show_my_emails(chat_id, user_id):
    t = tr(user_id)
    emails = list_user_emails(user_id)
    if not emails:
        bot.send_message(chat_id, t["no_emails"], reply_markup=main_keyboard(user_id))
        return
    lines = [t["my_emails"] + ":", ""]
    for i, item in enumerate(emails, 1):
        lines.append(f"{i}. {item['address']}")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=main_keyboard(user_id))

# ============================================================
# Forced subscription
# ============================================================

def clear_subscription_cache():
    with state_lock:
        subscription_cache.clear()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_subscribed(user_id, use_cache=True):
    if is_admin(user_id) or not force_sub_enabled():
        return True

    channel = force_sub_channel()
    if not channel:
        return False

    now = time.time()
    if use_cache:
        with state_lock:
            cached = subscription_cache.get(user_id)
            if cached and now - cached[0] < 60:
                return cached[1]

    try:
        member = bot.get_chat_member(channel, user_id)
        status = str(getattr(member, "status", ""))
        ok = status in {"member", "administrator", "creator"}
        if status == "restricted":
            ok = bool(getattr(member, "is_member", False))
    except Exception as exc:
        print(f"subscription check failed for {user_id}: {type(exc).__name__}: {exc}")
        ok = False

    with state_lock:
        subscription_cache[user_id] = (now, ok)
    return ok


def subscription_markup(user_id):
    t = tr(user_id)
    markup = InlineKeyboardMarkup(row_width=1)
    url = force_sub_url()
    if url:
        markup.add(InlineKeyboardButton("📢 " + ("الاشتراك في القناة" if get_lang(user_id) == "ar" else "Join channel"), url=url))
    markup.add(InlineKeyboardButton(t["sub_check"], callback_data="sub_check"))
    return markup


def guard_subscription(chat_id, user_id):
    if is_subscribed(user_id):
        return True
    t = tr(user_id)
    if not force_sub_channel():
        bot.send_message(chat_id, t["sub_config_error"])
    else:
        bot.send_message(chat_id, t["sub_required"], reply_markup=subscription_markup(user_id))
    return False


def validate_force_channel():
    channel = force_sub_channel()
    if not channel:
        return False, "اضبط القناة أولاً باستخدام /setchannel"
    try:
        me = bot.get_me()
        member = bot.get_chat_member(channel, me.id)
        status = str(getattr(member, "status", ""))
        if status not in {"administrator", "creator"}:
            return False, "اجعل البوت Admin في القناة أولاً"
        return True, ""
    except Exception as exc:
        print(f"channel validation failed: {type(exc).__name__}: {exc}")
        return False, "تعذر التحقق من القناة. تأكد من الـ ID وأن البوت Admin"

# ============================================================
# Mail delivery
# ============================================================

def check_one_email(chat_id, user_id, item, manual=True):
    t = tr(user_id)
    with get_email_lock(item["id"]):
        try:
            messages = read_messages(item["address"])
        except requests.exceptions.Timeout:
            bot.send_message(chat_id, t["mail_error"])
            return 0
        except Exception as exc:
            print(f"mail fetch failed {item['address']}: {type(exc).__name__}: {exc}")
            bot.send_message(chat_id, t["mail_error"])
            return 0

        if not messages:
            if manual:
                bot.send_message(chat_id, f"{t['empty']}\n\n📧 {item['address']}")
            return 0

        chosen = messages[:5] if manual else messages[:MAX_AUTO_MESSAGES_PER_EMAIL]
        sent = 0
        for mail in chosen:
            if not manual and is_seen(item["id"], mail["hash"]):
                continue
            try:
                send_long_message(chat_id, format_mail(user_id, item["address"], mail, automatic=not manual))
                mark_seen(item["id"], mail["hash"])
                sent += 1
            except Exception as exc:
                print(f"mail send failed to {chat_id}: {type(exc).__name__}: {exc}")
        return sent


def auto_mail_worker(item):
    user_id = item["owner_user_id"]
    if not is_subscribed(user_id):
        return
    with get_email_lock(item["id"]):
        try:
            messages = read_messages(item["address"])
        except Exception as exc:
            print(f"auto mail fetch failed {item['address']}: {type(exc).__name__}: {exc}")
            return

        # Send older unseen messages first for natural order.
        unseen = [m for m in messages[:MAX_AUTO_MESSAGES_PER_EMAIL] if not is_seen(item["id"], m["hash"])]
        for mail in reversed(unseen):
            # Ownership can change while the mailbox is being checked.
            owned = get_owned_email(user_id, item["id"])
            if not owned:
                return
            try:
                send_long_message(user_id, format_mail(user_id, item["address"], mail, automatic=True))
                mark_seen(item["id"], mail["hash"])
            except Exception as exc:
                print(f"auto mail send failed {user_id}: {type(exc).__name__}: {exc}")
                return


def background_mail_monitor():
    while True:
        started = time.time()
        items = get_all_emails()
        if items:
            with ThreadPoolExecutor(max_workers=AUTO_WORKERS) as pool:
                futures = [pool.submit(auto_mail_worker, item) for item in items]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        print(f"mail monitor task failed: {type(exc).__name__}: {exc}")
        elapsed = time.time() - started
        time.sleep(max(1.0, AUTO_CHECK_SECONDS - elapsed))

# ============================================================
# Admin
# ============================================================

def admin_panel(chat_id):
    enabled = force_sub_enabled()
    channel = force_sub_channel() or "غير مضبوط / not set"
    url = force_sub_url() or "غير مضبوط / not set"
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            "🔴 إيقاف الاشتراك الإجباري" if enabled else "🟢 تفعيل الاشتراك الإجباري",
            callback_data="admin_toggle_force_sub",
        )
    )
    bot.send_message(
        chat_id,
        "⚙️ لوحة الإدارة\n\n"
        f"الاشتراك الإجباري: {'مفعّل ✅' if enabled else 'متوقف ❌'}\n"
        f"القناة: {channel}\n"
        f"رابط الانضمام: {url}\n\n"
        "لتغيير القناة:\n"
        "/setchannel @channel\n"
        "أو للقناة الخاصة:\n"
        "/setchannel -1001234567890 https://t.me/+INVITE",
        reply_markup=markup,
        disable_web_page_preview=True,
    )

# ============================================================
# Handlers
# ============================================================

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    upsert_user(message.from_user)
    if not is_admin(message.from_user.id):
        return
    admin_panel(message.chat.id)


@bot.message_handler(commands=["setchannel"])
def cmd_setchannel(message):
    upsert_user(message.from_user)
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "الاستخدام:\n/setchannel @channel\nأو\n/setchannel -1001234567890 https://t.me/+INVITE")
        return
    channel = parts[1].strip()
    url = parts[2].strip() if len(parts) >= 3 else ""
    if not (channel.startswith("@") or re.fullmatch(r"-100\d+", channel)):
        bot.send_message(message.chat.id, "❌ القناة يجب أن تكون @username أو Chat ID يبدأ بـ -100")
        return
    if not url and channel.startswith("@"):
        url = "https://t.me/" + channel[1:]
    set_setting("force_sub_channel", channel)
    set_setting("force_sub_url", url)
    clear_subscription_cache()
    bot.send_message(message.chat.id, "✅ تم حفظ القناة.")
    admin_panel(message.chat.id)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    upsert_user(message.from_user)
    user_id = message.from_user.id
    if not guard_subscription(message.chat.id, user_id):
        return

    emails = list_user_emails(user_id)
    if not emails:
        item = create_email_for_user(user_id)
        text = f"{tr(user_id)['welcome']}\n\n{tr(user_id)['current']}:\n{item['address']}\n\n{tr(user_id)['choose']}"
    else:
        text = f"{tr(user_id)['welcome']}\n\n{tr(user_id)['current']}:\n{emails[0]['address']}\n\n{tr(user_id)['choose']}"
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(user_id))


@bot.message_handler(content_types=["text"])
def text_handler(message):
    upsert_user(message.from_user)
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()

    # Commands are handled above; ignore unknown commands here.
    if text.startswith("/"):
        return

    if not guard_subscription(chat_id, user_id):
        return

    t = tr(user_id)

    with state_lock:
        pending = user_states.get(user_id)

    if pending and pending.get("action") == "move_target":
        target_user_id = resolve_user_target(text)
        if not target_user_id:
            bot.send_message(chat_id, t["bad_target"])
            return
        if target_user_id == user_id:
            bot.send_message(chat_id, t["self_move"])
            return
        item = get_owned_email(user_id, pending["email_id"])
        if not item:
            with state_lock:
                user_states.pop(user_id, None)
            bot.send_message(chat_id, t["not_found"], reply_markup=main_keyboard(user_id))
            return
        if move_owned_email(user_id, item["id"], target_user_id):
            with state_lock:
                user_states.pop(user_id, None)
            bot.send_message(chat_id, f"{t['moved']}\n\n📧 {item['address']}", reply_markup=main_keyboard(user_id))
            try:
                bot.send_message(
                    target_user_id,
                    f"{tr(target_user_id)['received']}:\n\n📧 {item['address']}",
                    reply_markup=main_keyboard(target_user_id),
                )
            except Exception as exc:
                print(f"move notification failed: {type(exc).__name__}: {exc}")
        else:
            bot.send_message(chat_id, t["not_found"], reply_markup=main_keyboard(user_id))
        return

    if text in {AR["create"], EN["create"]}:
        item = create_email_for_user(user_id)
        bot.send_message(chat_id, f"{t['created']}\n\n📧 {item['address']}", reply_markup=main_keyboard(user_id))
        return

    if text in {AR["mine"], EN["mine"]}:
        show_my_emails(chat_id, user_id)
        return

    if text in {AR["check"], EN["check"]}:
        emails = list_user_emails(user_id)
        if not emails:
            bot.send_message(chat_id, t["no_emails"], reply_markup=main_keyboard(user_id))
        elif len(emails) == 1:
            wait = bot.send_message(chat_id, t["checking"])
            check_one_email(chat_id, user_id, emails[0], manual=True)
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass
        else:
            bot.send_message(chat_id, t["check_choose"], reply_markup=email_picker(user_id, "check"))
        return

    if text in {AR["delete"], EN["delete"]}:
        emails = list_user_emails(user_id)
        if not emails:
            bot.send_message(chat_id, t["no_emails"], reply_markup=main_keyboard(user_id))
        else:
            bot.send_message(chat_id, t["delete_choose"], reply_markup=email_picker(user_id, "delete"))
        return

    if text in {AR["move"], EN["move"]}:
        emails = list_user_emails(user_id)
        if not emails:
            bot.send_message(chat_id, t["no_emails"], reply_markup=main_keyboard(user_id))
        else:
            bot.send_message(chat_id, t["move_choose"], reply_markup=email_picker(user_id, "move"))
        return

    if text in {AR["lang"], EN["lang"]}:
        new_lang = "en" if get_lang(user_id) == "ar" else "ar"
        set_lang(user_id, new_lang)
        bot.send_message(chat_id, tr(user_id)["lang_changed"], reply_markup=main_keyboard(user_id))
        return

    bot.send_message(chat_id, t["choose"], reply_markup=main_keyboard(user_id))


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    upsert_user(call.from_user)
    user_id = call.from_user.id
    chat_id = call.message.chat.id if call.message else user_id
    data = call.data or ""

    if data == "admin_toggle_force_sub":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id)
            return
        if not force_sub_enabled():
            valid, reason = validate_force_channel()
            if not valid:
                bot.answer_callback_query(call.id, reason, show_alert=True)
                return
        set_setting("force_sub_enabled", "0" if force_sub_enabled() else "1")
        clear_subscription_cache()
        bot.answer_callback_query(call.id, "تم تحديث الإعداد")
        admin_panel(chat_id)
        return

    if data == "sub_check":
        clear_subscription_cache()
        if is_subscribed(user_id, use_cache=False):
            bot.answer_callback_query(call.id, tr(user_id)["sub_ok"], show_alert=True)
            emails = list_user_emails(user_id)
            if not emails:
                item = create_email_for_user(user_id)
                bot.send_message(chat_id, f"{tr(user_id)['current']}:\n{item['address']}", reply_markup=main_keyboard(user_id))
            else:
                bot.send_message(chat_id, tr(user_id)["choose"], reply_markup=main_keyboard(user_id))
        else:
            bot.answer_callback_query(call.id, tr(user_id)["sub_fail"], show_alert=True)
        return

    if not guard_subscription(chat_id, user_id):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data.startswith("check:"):
        try:
            email_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        item = get_owned_email(user_id, email_id)
        if not item:
            bot.send_message(chat_id, tr(user_id)["not_found"])
            return
        wait = bot.send_message(chat_id, tr(user_id)["checking"])
        check_one_email(chat_id, user_id, item, manual=True)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        return

    if data == "check_all":
        emails = list_user_emails(user_id)
        if not emails:
            bot.send_message(chat_id, tr(user_id)["no_emails"])
            return
        wait = bot.send_message(chat_id, tr(user_id)["checking"])
        for item in emails:
            check_one_email(chat_id, user_id, item, manual=True)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        return

    if data.startswith("delete_select:"):
        try:
            email_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        item = get_owned_email(user_id, email_id)
        if not item:
            bot.send_message(chat_id, tr(user_id)["not_found"])
            return
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton(tr(user_id)["yes_delete"], callback_data=f"delete_confirm:{email_id}"),
            InlineKeyboardButton(tr(user_id)["cancel"], callback_data="cancel_action"),
        )
        bot.send_message(chat_id, f"{tr(user_id)['confirm_delete']}\n\n📧 {item['address']}", reply_markup=markup)
        return

    if data.startswith("delete_confirm:"):
        try:
            email_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        ok = delete_owned_email(user_id, email_id)
        bot.send_message(
            chat_id,
            tr(user_id)["deleted"] if ok else tr(user_id)["not_found"],
            reply_markup=main_keyboard(user_id),
        )
        return

    if data.startswith("move_select:"):
        try:
            email_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        item = get_owned_email(user_id, email_id)
        if not item:
            bot.send_message(chat_id, tr(user_id)["not_found"])
            return
        with state_lock:
            user_states[user_id] = {"action": "move_target", "email_id": email_id}
        bot.send_message(chat_id, f"📧 {item['address']}\n\n{tr(user_id)['move_target']}")
        return

    if data == "cancel_action":
        with state_lock:
            user_states.pop(user_id, None)
        bot.send_message(chat_id, tr(user_id)["choose"], reply_markup=main_keyboard(user_id))
        return

# ============================================================
# Startup
# ============================================================

def run_bot():
    init_db()
    threading.Thread(target=background_mail_monitor, daemon=True, name="mail-monitor").start()
    print("EMAIL BOT RUNNING...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    run_bot()
