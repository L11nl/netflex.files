import os
import re
import json
import time
import html
import imaplib
import email
import threading
import random
import string
import hashlib
from email.header import decode_header
from urllib.parse import urlparse

import requests
import telebot
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============================================================
# Environment / configuration
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.getenv("SMSBOWER_BASE_URL", "https://smsbower.page/stubs/handler_api.php")
WALLET_URL = os.getenv("SMSBOWER_WALLET_URL", "https://smsbower.page/api/payment/getActualWalletAddress")
SERVICE_NETFLIX = os.getenv("SMSBOWER_NETFLIX_SERVICE", "nf")
COUNTRY_IRAQ = os.getenv("SMSBOWER_COUNTRY_IRAQ", "47")

# USERS_JSON example:
# {"6491999046":{"name":"عقيل","api_key":"YOUR_SMSBOWER_API_KEY"}}
USERS_JSON = os.getenv("USERS_JSON", "{}")
try:
    _raw_users = json.loads(USERS_JSON)
except json.JSONDecodeError as exc:
    raise RuntimeError("USERS_JSON is not valid JSON") from exc

USERS = {
    int(user_id): {
        "name": str(data.get("name", "مستخدم")),
        "api_key": str(data.get("api_key", "")),
    }
    for user_id, data in _raw_users.items()
}

# Guardrail: only accounts you explicitly authorize can be rotated.
ALLOWED_NETFLIX_EMAILS = {
    x.strip().lower()
    for x in os.getenv("NETFLIX_ALLOWED_EMAILS", "").split(",")
    if x.strip()
}
ALLOWED_NETFLIX_DOMAINS = {
    x.strip().lower().lstrip("@")
    for x in os.getenv("NETFLIX_ALLOWED_DOMAINS", "").split(",")
    if x.strip()
}

NETFLIX_LOGIN_HELP_URL = os.getenv(
    "NETFLIX_LOGIN_HELP_URL", "https://www.netflix.com/iq/LoginHelp"
)
MAIL_MODE = os.getenv("MAIL_MODE", "imap").strip().lower()  # imap | generator
RESET_MAIL_TIMEOUT = int(os.getenv("RESET_MAIL_TIMEOUT_SECONDS", "180"))
RESET_MAIL_POLL = float(os.getenv("RESET_MAIL_POLL_SECONDS", "3"))

# IMAP mode
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

# generator.email mode; useful for your existing @5xu.vn workflow.
GENERATOR_BASE_URL = os.getenv("GENERATOR_BASE_URL", "https://generator.email")
GENERATOR_INBOX = os.getenv("GENERATOR_INBOX_PREFIX", "inbox9")

# Temporary-mail / profile manager (kept in this same bot.py)
TEMPMAIL_DOMAIN = os.getenv("TEMPMAIL_DOMAIN", "5xu.vn").strip().lower()
EMAIL_LIFETIME_SECONDS = 6 * 24 * 60 * 60
AUTO_MAIL_WINDOW_SECONDS = 20 * 60
AUTO_MAIL_MAX_MESSAGES = 5
AUTO_MAIL_POLL_SECONDS = float(os.getenv("AUTO_MAIL_POLL_SECONDS", "3"))

DEFAULT_PROFILE_PINS = {1: "1212", 2: "1001", 3: "2121", 4: "2026", 5: "2002"}
PROFILE_COLORS = {1: "🔵", 2: "🟡", 3: "🔴", 4: "🔷", 5: "🟢"}

# Persistent settings. Mount a Railway Volume to /data.
STATE_FILE = os.getenv("BOT_STATE_FILE", "/data/bot_settings.json")
DEFAULT_PASSWORD_FALLBACK = os.getenv("DEFAULT_NETFLIX_PASSWORD", "Aa12345678911")

bot = telebot.TeleBot(BOT_TOKEN)

user_active_orders = {}
user_last_balances = {}
user_states = {}
active_password_rotations = set()
state_lock = threading.RLock()

# ============================================================
# Persistent bot settings
# ============================================================

def _default_state():
    return {
        "netflix_default_password": DEFAULT_PASSWORD_FALLBACK,
        "netflix_sign_out_all": True,
        # Per-user temporary emails and their five profiles.
        "users": {},
    }


def load_state():
    with state_lock:
        state = _default_state()
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    state.update(stored)
        except Exception:
            pass
        return state


def save_state(state):
    with state_lock:
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as exc:
            # Technical details stay in server logs, never in Telegram UI.
            print(f"state save failed: {type(exc).__name__}")


BOT_STATE = load_state()
if not isinstance(BOT_STATE.get("users"), dict):
    BOT_STATE["users"] = {}

# ============================================================
# Temporary email + five-profile storage
# ============================================================

def temp_user_state(user_id):
    """Return persistent email state for one Telegram user."""
    key = str(user_id)
    with state_lock:
        users = BOT_STATE.setdefault("users", {})
        if key not in users or not isinstance(users[key], dict):
            users[key] = {"emails": [], "selected": None}
        users[key].setdefault("emails", [])
        users[key].setdefault("selected", None)
        return users[key]


def make_profile(profile_number):
    return {
        "number": int(profile_number),
        "pin": DEFAULT_PROFILE_PINS[int(profile_number)],
        "status": "available",  # available | review | sold
        "sold_at": None,
    }


def make_managed_email(address, mode="random"):
    now = time.time()
    return {
        "id": f"m{int(now * 1000)}{random.randint(100, 999)}",
        "address": normalize_email(address),
        "created_at": now,
        "expires_at": now + EMAIL_LIFETIME_SECONDS,
        "status": "active",  # active | completed
        "mode": mode,
        "profiles": [make_profile(i) for i in range(1, 6)],
        "seen": [],
        "message_times": {},
        "auto_until": now + AUTO_MAIL_WINDOW_SECONDS,
        "auto_count": 0,
    }


def get_managed_email(user_id, email_id=None):
    data = temp_user_state(user_id)
    email_id = email_id or data.get("selected")
    if not email_id:
        return None
    return next((x for x in data.get("emails", []) if x.get("id") == email_id), None)


def add_managed_email(user_id, address, mode="random"):
    address = normalize_email(address)
    data = temp_user_state(user_id)
    old_email = next((x for x in data["emails"] if x.get("address") == address), None)
    if old_email:
        data["selected"] = old_email["id"]
        save_state(BOT_STATE)
        return old_email, False
    item = make_managed_email(address, mode)
    data["emails"].insert(0, item)
    data["selected"] = item["id"]
    save_state(BOT_STATE)
    return item, True


def make_temp_mail_name():
    chars = string.ascii_lowercase + string.digits
    while True:
        name = "".join(random.choice(chars) for _ in range(random.choice([5, 6])))
        if any(c.isalpha() for c in name) and any(c.isdigit() for c in name):
            return name


def normalize_mail_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\xad]", "", value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def extract_verification_code(value):
    """Prefer isolated 4-8 digit codes and avoid years/UUID chunks."""
    for match in re.finditer(r"(?<![A-Za-z0-9-])(\d{4,8})(?![A-Za-z0-9-])", value or ""):
        code = match.group(1)
        if len(code) == 4:
            try:
                if 1900 <= int(code) <= 2099:
                    continue
            except ValueError:
                pass
        return code
    return None


def important_message_url(urls):
    bad = re.compile(
        r"(assets\.|beacon|\.png(?:\?|$)|\.jpe?g(?:\?|$)|\.gif(?:\?|$)|\.svg(?:\?|$)|"
        r"privacy|terms|contact|corpinfo|unsubscribe|cookie|url_logo|url_src|url_email)",
        re.I,
    )
    good = re.compile(
        r"(verify|confirm|activate|complete|signup|register|create|reset|password|magic|"
        r"signin|login|authenticate|/epr|[?&](?:code|token|key)=)",
        re.I,
    )
    candidates = [u for u in urls if not bad.search(u)]
    for url in candidates:
        if good.search(url):
            return url
    return candidates[0] if candidates else None


def fetch_temp_mailbox(address):
    """Read generator.email using the same server-side pattern that worked before."""
    address = normalize_email(address)
    if "@" not in address:
        return []
    username, domain = address.split("@", 1)
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Mobile) "
            "AppleWebKit/537.36 Chrome/136.0 Mobile Safari/537.36"
        )
    }

    # Bootstrap a Generator.email session/cookies first, then open inbox9/address.
    try:
        session.get(
            f"{GENERATOR_BASE_URL.rstrip('/')}/{domain}/{username}",
            headers=headers,
            timeout=15,
            allow_redirects=True,
        )
    except Exception:
        pass

    response = session.get(
        f"{GENERATOR_BASE_URL.rstrip('/')}/{GENERATOR_INBOX}/{address}",
        headers=headers,
        timeout=20,
        allow_redirects=True,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("div", id="email-table")
    if not table:
        return []

    senders = table.find_all("div", class_="from_div_45g45gg")
    subjects = table.find_all("div", class_="subj_div_45g45gg")
    bodies = table.find_all("div", class_="mess_bodiyy")

    # Generator.email may include literal table headings in these same classes.
    senders = [n for n in senders if n.get_text(" ", strip=True).lower() != "from"]
    subjects = [n for n in subjects if n.get_text(" ", strip=True).lower() != "subject"]

    count = max(len(senders), len(subjects), len(bodies))
    messages = []
    for i in range(count):
        sender = senders[i].get_text(" ", strip=True) if i < len(senders) else "غير معروف"
        subject = subjects[i].get_text(" ", strip=True) if i < len(subjects) else "بدون عنوان"
        body = ""
        urls = []
        if i < len(bodies):
            body_copy = BeautifulSoup(str(bodies[i]), "html.parser")
            for tag in body_copy.find_all("a", href=True):
                href = (tag.get("href") or "").strip()
                if href.startswith(("http://", "https://")):
                    urls.append(href)
                    title = tag.get_text(" ", strip=True)
                    tag.replace_with(f"\n{title}\n{href}\n" if title else f"\n{href}\n")
            body = normalize_mail_text(body_copy.get_text("\n", strip=True))

        urls = list(dict.fromkeys(urls + extract_urls(body)))
        digest_source = f"{sender}|{subject}|{body[:1200]}".encode("utf-8", errors="ignore")
        message_id = hashlib.sha1(digest_source).hexdigest()
        messages.append(
            {
                "id": message_id,
                "from": sender,
                "subject": subject,
                "body": body,
                "urls": urls,
                "code": extract_verification_code(subject + "\n" + body),
            }
        )
    return messages[:20]


def relative_time(timestamp):
    seconds = max(0, int(time.time() - float(timestamp or time.time())))
    if seconds < 60:
        return "قبل ثانية" if seconds <= 1 else f"قبل {seconds} ثانية"
    if seconds < 3600:
        minutes = seconds // 60
        return "قبل دقيقة" if minutes == 1 else f"قبل {minutes} دقيقة"
    if seconds < 86400:
        hours = seconds // 3600
        return "قبل ساعة" if hours == 1 else f"قبل {hours} ساعة"
    days = seconds // 86400
    return "قبل يوم" if days == 1 else f"قبل {days} يوم"


def message_discovered_at(managed_email, message):
    times = managed_email.setdefault("message_times", {})
    mid = message.get("id")
    if mid not in times:
        times[mid] = time.time()
    return times[mid]


def email_create_keyboard():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("🎲 إنشاء إيميل عشوائي", callback_data="email_create_random"),
        InlineKeyboardButton("✍️ إنشاء إيميل يدوي", callback_data="email_create_manual"),
        InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu"),
    )
    return markup


def managed_email_summary(item):
    counts = {"available": 0, "review": 0, "sold": 0}
    for profile in item.get("profiles", []):
        counts[profile.get("status", "available")] = counts.get(profile.get("status", "available"), 0) + 1
    days_left = max(0, int((item.get("expires_at", time.time()) - time.time() + 86399) // 86400))
    return (
        f"📧 {item['address']}\n"
        f"🗓️ تم الإنشاء {relative_time(item['created_at'])}\n"
        f"⏳ الحذف من البوت بعد: {days_left} يوم\n"
        f"👥 البروفايلات: 🟢 {counts['available']}  🟡 {counts['review']}  🔴 {counts['sold']}\n"
        f"📌 الحالة: {'مكتمل/مباع' if item.get('status') == 'completed' else 'نشط'}\n"
        "🔔 أول 5 رسائل تصل تلقائياً خلال 20 دقيقة"
    )


def managed_email_keyboard(item):
    markup = InlineKeyboardMarkup(row_width=2)
    # Telegram clients that support CopyTextButton show native copy behavior.
    try:
        markup.add(InlineKeyboardButton("📋 نسخ الإيميل", copy_text={"text": item["address"]}))
    except TypeError:
        markup.add(InlineKeyboardButton("📋 الإيميل", callback_data=f"show_email:{item['id']}"))
    markup.add(
        InlineKeyboardButton(
            "📬 فتح صندوق الإيميل",
            url=f"{GENERATOR_BASE_URL.rstrip('/')}/{GENERATOR_INBOX}/{item['address']}",
        )
    )
    markup.row(
        InlineKeyboardButton("🔎 جلب الكود أو الرابط", callback_data=f"email_code_or_link:{item['id']}"),
        InlineKeyboardButton("📥 جلب آخر الرسائل", callback_data=f"email_latest:{item['id']}"),
    )
    markup.add(InlineKeyboardButton("👥 إدارة البروفايلات", callback_data=f"email_profiles:{item['id']}"))
    markup.add(InlineKeyboardButton("🗑️ حذف الإيميل", callback_data=f"email_delete:{item['id']}"))
    markup.add(InlineKeyboardButton("🔙 الإيميلات", callback_data="emails_list"))
    return markup


def profiles_keyboard(item):
    markup = InlineKeyboardMarkup(row_width=1)
    labels = {"available": "🟢 متاح", "review": "🟡 قيد المراجعة", "sold": "🔴 تم البيع"}
    for profile in sorted(item.get("profiles", []), key=lambda p: p.get("number", 0)):
        number = profile["number"]
        markup.add(
            InlineKeyboardButton(
                f"{PROFILE_COLORS[number]} البروفايل {number} • {labels.get(profile['status'], profile['status'])}",
                callback_data=f"email_profile:{item['id']}:{number}",
            )
        )
    markup.add(InlineKeyboardButton("🔙 الإيميل", callback_data=f"email_open:{item['id']}"))
    return markup


def profile_keyboard(item, profile):
    markup = InlineKeyboardMarkup(row_width=2)
    try:
        markup.add(
            InlineKeyboardButton(
                f"📋 نسخ الرمز {profile['pin']}",
                copy_text={"text": str(profile["pin"])},
            )
        )
        markup.add(InlineKeyboardButton("📧 نسخ الإيميل", copy_text={"text": item["address"]}))
    except TypeError:
        pass
    markup.add(
        InlineKeyboardButton(
            "🔎 جلب الكود أو الرابط",
            callback_data=f"email_code_or_link:{item['id']}",
        )
    )
    markup.add(
        InlineKeyboardButton(
            "✏️ تغيير الرمز",
            callback_data=f"email_change_pin:{item['id']}:{profile['number']}",
        )
    )
    markup.row(
        InlineKeyboardButton(
            "🟡 قيد المراجعة",
            callback_data=f"email_profile_status:{item['id']}:{profile['number']}:review",
        ),
        InlineKeyboardButton(
            "✅ تم البيع",
            callback_data=f"email_profile_status:{item['id']}:{profile['number']}:sold",
        ),
    )
    markup.add(
        InlineKeyboardButton(
            "🟢 إرجاع متاح",
            callback_data=f"email_profile_status:{item['id']}:{profile['number']}:available",
        )
    )
    markup.add(InlineKeyboardButton("🔙 البروفايلات", callback_data=f"email_profiles:{item['id']}"))
    return markup


def format_auto_mail(item, message):
    lines = [
        "🔔 وصلت رسالة جديدة",
        f"📧 إلى: {item['address']}",
        f"👤 من: {message.get('from', 'غير معروف')}",
        f"📝 العنوان: {message.get('subject', 'بدون عنوان')}",
    ]
    code = message.get("code")
    link = important_message_url(message.get("urls", []))
    if code:
        lines.extend(["", f"🔢 الكود: {code}"])
    elif link:
        lines.extend(["", f"🔗 الرابط المهم:\n{link}"])
    else:
        body = (message.get("body") or "رسالة جديدة")[:1800]
        lines.extend(["", body])
    return "\n".join(lines)


def background_temp_mail_monitor():
    """Send only first five new messages during first 20 minutes for each email."""
    while True:
        time.sleep(max(1.0, AUTO_MAIL_POLL_SECONDS))
        changed = False
        users_snapshot = list(BOT_STATE.get("users", {}).items())
        for user_id_text, data in users_snapshot:
            try:
                user_id = int(user_id_text)
            except (TypeError, ValueError):
                continue
            for item in list(data.get("emails", [])):
                if item.get("status") == "completed":
                    continue
                if time.time() > float(item.get("auto_until", 0)):
                    continue
                if int(item.get("auto_count", 0)) >= AUTO_MAIL_MAX_MESSAGES:
                    continue
                try:
                    messages = fetch_temp_mailbox(item["address"])
                except Exception:
                    continue

                # Oldest unseen first, so multiple arrivals are delivered in order.
                for message in reversed(messages[:10]):
                    mid = message.get("id")
                    if not mid or mid in item.setdefault("seen", []):
                        continue
                    item["seen"].append(mid)
                    item["seen"] = item["seen"][-100:]
                    discovered = message_discovered_at(item, message)
                    changed = True
                    if time.time() <= item.get("auto_until", 0) and item.get("auto_count", 0) < AUTO_MAIL_MAX_MESSAGES:
                        try:
                            keyboard = InlineKeyboardMarkup()
                            if message.get("code"):
                                try:
                                    keyboard.add(
                                        InlineKeyboardButton(
                                            f"📋 نسخ الكود {message['code']}",
                                            copy_text={"text": str(message["code"])},
                                        )
                                    )
                                except TypeError:
                                    pass
                            else:
                                link = important_message_url(message.get("urls", []))
                                if link:
                                    keyboard.add(InlineKeyboardButton("🔗 فتح الرابط", url=link))
                            bot.send_message(user_id, format_auto_mail(item, message), reply_markup=keyboard)
                        except Exception:
                            pass
                        item["auto_count"] = int(item.get("auto_count", 0)) + 1
                        if item["auto_count"] >= AUTO_MAIL_MAX_MESSAGES:
                            break
        if changed:
            save_state(BOT_STATE)


def cleanup_expired_emails():
    while True:
        time.sleep(300)
        now = time.time()
        changed = False
        for data in BOT_STATE.get("users", {}).values():
            emails = data.get("emails", [])
            old_count = len(emails)
            data["emails"] = [x for x in emails if float(x.get("expires_at", 0)) > now]
            if len(data["emails"]) != old_count:
                changed = True
            selected = data.get("selected")
            if selected and not any(x.get("id") == selected for x in data["emails"]):
                data["selected"] = data["emails"][0]["id"] if data["emails"] else None
                changed = True
        if changed:
            save_state(BOT_STATE)

# ============================================================
# Utilities
# ============================================================

def get_user_data(user_id):
    return USERS.get(user_id)


def normalize_email(value):
    return value.strip().lower()


def is_valid_email(value):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))


def is_authorized_netflix_email(address):
    address = normalize_email(address)
    if address in ALLOWED_NETFLIX_EMAILS:
        return True
    if "@" in address:
        domain = address.rsplit("@", 1)[1]
        if domain in ALLOWED_NETFLIX_DOMAINS:
            return True
    return False


def strong_enough(password):
    return 6 <= len(password) <= 60


def decode_mime_header(value):
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


def extract_urls(text):
    if not text:
        return []
    text = html.unescape(text)
    found = re.findall(r"https?://[^\s<>\"']+", text, flags=re.I)
    cleaned = []
    seen = set()
    for url in found:
        url = url.rstrip(".,);]}\"")
        if url not in seen:
            seen.add(url)
            cleaned.append(url)
    return cleaned


def is_netflix_reset_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not (host == "netflix.com" or host.endswith(".netflix.com")):
            return False
        haystack = (parsed.path + "?" + parsed.query).lower()
        keywords = ("password", "reset", "epr", "loginhelp", "recovery", "change")
        return any(k in haystack for k in keywords)
    except Exception:
        return False


def choose_reset_url(urls):
    # Only return a Netflix HTTPS URL. Never follow arbitrary links from mail.
    candidates = []
    for url in urls:
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https":
                continue
            host = (parsed.hostname or "").lower()
            if host == "netflix.com" or host.endswith(".netflix.com"):
                candidates.append(url)
        except Exception:
            continue

    for url in candidates:
        if is_netflix_reset_url(url):
            return url
    return candidates[0] if candidates else None

# ============================================================
# SMSBower functions from your existing bot
# ============================================================

def get_current_balance(api_key):
    try:
        params = {"api_key": api_key, "action": "getBalance"}
        response = requests.get(BASE_URL, params=params, timeout=10)
        res_text = response.text.strip()
        if res_text.startswith("ACCESS_BALANCE:"):
            balance_str = res_text.split(":", 1)[1]
            return float(balance_str), balance_str + "$"
    except Exception:
        pass
    return 0.0, "غير معروف"


def balance_monitor():
    while True:
        time.sleep(30)
        for user_id, user_info in USERS.items():
            api_key = user_info["api_key"]
            num_val, text_val = get_current_balance(api_key)
            if user_id not in user_last_balances:
                user_last_balances[user_id] = num_val
                continue
            if num_val > user_last_balances[user_id]:
                user_last_balances[user_id] = num_val
                try:
                    bot.send_message(
                        user_id,
                        f"🎉 **تم استلام الأموال بنجاح!**\n💰 رصيدك الحالي أصبح: **{text_val}**",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            elif num_val < user_last_balances[user_id]:
                user_last_balances[user_id] = num_val


def cancel_order(api_key, activation_id):
    try:
        params = {
            "api_key": api_key,
            "action": "setStatus",
            "id": activation_id,
            "status": 8,
        }
        requests.get(BASE_URL, params=params, timeout=10)
    except Exception:
        pass


def check_for_sms(chat_id, api_key, activation_id, phone_number):
    start_time = time.time()
    while time.time() - start_time < 600:
        time.sleep(5)
        if chat_id not in user_active_orders or user_active_orders[chat_id] != activation_id:
            break
        try:
            params = {"api_key": api_key, "action": "getStatus", "id": activation_id}
            response = requests.get(BASE_URL, params=params, timeout=10)
            res = response.text.strip()
            if res.startswith("STATUS_OK:"):
                code = res.split(":", 1)[1]
                user_active_orders.pop(chat_id, None)
                bot.send_message(chat_id, code)
                break
            if res == "STATUS_CANCEL":
                user_active_orders.pop(chat_id, None)
                bot.send_message(chat_id, "❌ تم إلغاء العملية من قبل الموقع.")
                break
        except Exception:
            break

# ============================================================
# Mail readers
# ============================================================

def message_text_and_urls(msg):
    chunks = []
    urls = []

    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = [msg]

    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
        except Exception:
            continue

        if ctype == "text/html":
            soup = BeautifulSoup(body, "html.parser")
            for a in soup.find_all("a", href=True):
                urls.append(a["href"].strip())
            chunks.append(soup.get_text("\n", strip=True))
        else:
            chunks.append(body)

        urls.extend(extract_urls(body))

    unique_urls = []
    seen = set()
    for url in urls:
        url = html.unescape(str(url).strip())
        if url and url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return "\n".join(chunks), unique_urls


def wait_reset_link_imap(target_email, since_ts):
    if not all([IMAP_HOST, IMAP_USER, IMAP_PASSWORD]):
        raise RuntimeError("IMAP configuration is incomplete")

    deadline = time.time() + RESET_MAIL_TIMEOUT
    while time.time() < deadline:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
            mail.login(IMAP_USER, IMAP_PASSWORD)
            mail.select(IMAP_FOLDER)

            # Search recent messages. We validate recipient + Netflix URL ourselves.
            status, data = mail.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            ids = data[0].split()[-40:]

            for msg_id in reversed(ids):
                status, payload = mail.fetch(msg_id, "(RFC822 INTERNALDATE)")
                if status != "OK" or not payload:
                    continue

                raw_bytes = None
                for item in payload:
                    if isinstance(item, tuple) and isinstance(item[1], bytes):
                        raw_bytes = item[1]
                        break
                if not raw_bytes:
                    continue

                msg = email.message_from_bytes(raw_bytes)
                to_header = " ".join(
                    filter(None, [msg.get("To"), msg.get("Delivered-To"), msg.get("X-Original-To")])
                ).lower()
                subject = decode_mime_header(msg.get("Subject", "")).lower()
                sender = decode_mime_header(msg.get("From", "")).lower()

                body, urls = message_text_and_urls(msg)
                combined = (to_header + "\n" + body.lower())
                if target_email.lower() not in combined and target_email.lower() not in to_header:
                    continue
                if "netflix" not in sender and "netflix" not in subject and "netflix" not in body.lower():
                    continue

                chosen = choose_reset_url(urls)
                if chosen:
                    return chosen
        except Exception as exc:
            print(f"imap poll failed: {type(exc).__name__}")
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

        time.sleep(RESET_MAIL_POLL)

    raise TimeoutError("reset email timeout")


def generator_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Mobile) "
                "AppleWebKit/537.36 Chrome/136.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        }
    )
    try:
        session.get(GENERATOR_BASE_URL, timeout=20)
    except Exception:
        pass
    return session


def generator_read_messages(session, target_email):
    local, domain = target_email.split("@", 1)
    urls_to_try = [
        f"{GENERATOR_BASE_URL}/{GENERATOR_INBOX}/{target_email}",
        f"{GENERATOR_BASE_URL}/{domain}/{local}",
    ]

    for mailbox_url in urls_to_try:
        response = session.get(mailbox_url, timeout=20, allow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("div", id="email-table")
        if not table:
            continue

        bodies = table.find_all("div", class_="mess_bodiyy")
        senders = table.find_all("div", class_="from_div_45g45gg")
        subjects = table.find_all("div", class_="subj_div_45g45gg")
        out = []

        count = max(len(bodies), len(senders), len(subjects))
        for i in range(count):
            sender = senders[i].get_text(" ", strip=True) if i < len(senders) else ""
            subject = subjects[i].get_text(" ", strip=True) if i < len(subjects) else ""
            body = bodies[i] if i < len(bodies) else None
            links = []
            text = ""
            if body:
                body_copy = BeautifulSoup(str(body), "html.parser")
                for a in body_copy.find_all("a", href=True):
                    links.append(a["href"].strip())
                text = body_copy.get_text("\n", strip=True)
                links.extend(extract_urls(str(body_copy)))
            out.append({"sender": sender, "subject": subject, "text": text, "urls": links})
        return out
    return []


def wait_reset_link_generator(target_email, since_ts):
    session = generator_session()
    deadline = time.time() + RESET_MAIL_TIMEOUT
    while time.time() < deadline:
        try:
            messages = generator_read_messages(session, target_email)
            for item in messages[:10]:
                hay = f"{item['sender']}\n{item['subject']}\n{item['text']}".lower()
                if "netflix" not in hay:
                    continue
                chosen = choose_reset_url(item["urls"])
                if chosen:
                    return chosen
        except Exception as exc:
            print(f"generator poll failed: {type(exc).__name__}")
        time.sleep(RESET_MAIL_POLL)
    raise TimeoutError("reset email timeout")


def wait_for_reset_link(target_email, since_ts):
    if MAIL_MODE == "generator":
        return wait_reset_link_generator(target_email, since_ts)
    return wait_reset_link_imap(target_email, since_ts)

# ============================================================
# Netflix browser automation
# ============================================================

def first_visible(locator_candidates):
    for locator in locator_candidates:
        try:
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def fill_login_help_email(page, account_email):
    field = first_visible(
        [
            page.locator('input[type="email"]'),
            page.locator('input[name="email"]'),
            page.locator('input[id*="email" i]'),
        ]
    )
    if not field:
        raise RuntimeError("email field not found")
    field.fill(account_email)

    button = first_visible(
        [
            page.get_by_role("button", name=re.compile(r"email|send|إرسال|البريد", re.I)),
            page.locator('button[type="submit"]'),
            page.locator('input[type="submit"]'),
        ]
    )
    if not button:
        raise RuntimeError("submit button not found")
    button.click()


def fill_new_password_form(page, new_password, sign_out_all):
    page.wait_for_load_state("domcontentloaded")

    password_fields = page.locator('input[type="password"]')
    count = password_fields.count()
    if count < 2:
        # Some Netflix layouts render inputs slightly later.
        page.wait_for_timeout(1500)
        count = password_fields.count()
    if count < 2:
        raise RuntimeError("password fields not found")

    password_fields.nth(0).fill(new_password)
    password_fields.nth(1).fill(new_password)

    # Prefer label-based checkbox; fall back to a single checkbox on the form.
    checkbox = first_visible(
        [
            page.get_by_role(
                "checkbox",
                name=re.compile(r"sign out|all devices|تسجيل الخروج|جميع الأجهزة", re.I),
            ),
            page.locator('input[type="checkbox"]'),
        ]
    )
    if checkbox:
        try:
            checked = checkbox.is_checked()
            if sign_out_all and not checked:
                checkbox.check(force=True)
            elif not sign_out_all and checked:
                checkbox.uncheck(force=True)
        except Exception:
            pass

    save_button = first_visible(
        [
            page.get_by_role("button", name=re.compile(r"save|حفظ|continue|متابعة", re.I)),
            page.locator('button[type="submit"]'),
            page.locator('input[type="submit"]'),
        ]
    )
    if not save_button:
        raise RuntimeError("save button not found")
    save_button.click()

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    # A conservative completion check: password form must disappear.
    page.wait_for_timeout(1200)
    if page.locator('input[type="password"]').count() >= 2:
        raise RuntimeError("password form still present")


def rotate_netflix_password(account_email, new_password, sign_out_all):
    reset_requested_at = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            locale="ar-IQ",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            page.goto(NETFLIX_LOGIN_HELP_URL, wait_until="domcontentloaded", timeout=30000)
            fill_login_help_email(page, account_email)

            # Mail waiting happens after the reset request has been sent.
            reset_url = wait_for_reset_link(account_email, reset_requested_at)
            if not reset_url or not is_netflix_reset_url(reset_url):
                raise RuntimeError("invalid reset URL")

            page.goto(reset_url, wait_until="domcontentloaded", timeout=30000)
            fill_new_password_form(page, new_password, sign_out_all)
        finally:
            context.close()
            browser.close()


def password_rotation_worker(chat_id, user_id, account_email, status_message_id):
    try:
        with state_lock:
            new_password = str(BOT_STATE.get("netflix_default_password", DEFAULT_PASSWORD_FALLBACK))
            sign_out_all = bool(BOT_STATE.get("netflix_sign_out_all", True))

        rotate_netflix_password(account_email, new_password, sign_out_all)
        bot.edit_message_text(
            "تم تغيير كلمة المرور بنجاح ✅",
            chat_id,
            status_message_id,
        )
    except Exception as exc:
        # Do not leak email contents, reset URLs, selectors, credentials or technical traces.
        print(f"Netflix rotation failed: {type(exc).__name__}")
        try:
            bot.edit_message_text(
                "تعذر إكمال تغيير كلمة المرور حالياً ❌",
                chat_id,
                status_message_id,
            )
        except Exception:
            pass
    finally:
        active_password_rotations.discard(user_id)

# ============================================================
# Keyboards
# ============================================================

def main_keyboard(user_id):
    user = get_user_data(user_id)
    if not user:
        return None

    _, balance_text = get_current_balance(user["api_key"])
    with state_lock:
        sign_out = bool(BOT_STATE.get("netflix_sign_out_all", True))

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(
            f"👤 {user['name']} | 💰 الرصيد: {balance_text}",
            callback_data="get_balance",
        )
    )
    markup.row(
        InlineKeyboardButton("➕ إنشاء إيميل", callback_data="email_create_menu"),
        InlineKeyboardButton("📧 الإيميلات", callback_data="emails_list"),
    )
    markup.row(
        InlineKeyboardButton("✅ الإيميلات المباعة", callback_data="emails_sold"),
        InlineKeyboardButton("🔐 تغيير رمز النتفلكس", callback_data="netflix_rotate_password"),
    )
    markup.add(
        InlineKeyboardButton(
            "📱 شراء رقم عراقي (آسياسيل أو زين العراق)",
            callback_data="buy_number",
        )
    )
    markup.add(InlineKeyboardButton("💳 إيداع الأموال (USDT - BSC)", callback_data="deposit_bsc"))
    markup.add(
        InlineKeyboardButton(
            f"📴 تسجيل الخروج من جميع الأجهزة: {'✅ مفعّل' if sign_out else '❌ معطّل'}",
            callback_data="netflix_toggle_signout",
        )
    )
    markup.add(
        InlineKeyboardButton(
            "✏️ تغيير كلمة المرور الافتراضية",
            callback_data="netflix_change_default_password",
        )
    )
    return markup

# ============================================================
# Telegram handlers
# ============================================================

@bot.message_handler(commands=["start", "menu"])
def send_welcome(message):
    user_id = message.from_user.id
    user = get_user_data(user_id)
    if not user:
        bot.send_message(message.chat.id, "عذراً، هذا البوت مخصص لأشخاص محددين فقط ❌")
        return

    user_states.pop(user_id, None)
    bot.send_message(
        message.chat.id,
        f"مرحباً بك يا **{user['name']}** في بوت أرقام نتفلكس العراقية 🇮🇶\n\n"
        "• يتم قبول أرقام **آسياسيل (96477)** و **زين العراق (96478)** فقط.",
        reply_markup=main_keyboard(user_id),
        parse_mode="Markdown",
    )


@bot.message_handler(func=lambda message: True)
def handle_text_messages(message):
    user_id = message.from_user.id
    user = get_user_data(user_id)
    if not user or not message.text:
        return

    state = user_states.get(user_id)
    text = message.text.strip()

    # New email/profile manager states are dictionaries so they cannot collide
    # with any of the original string states below.
    if isinstance(state, dict):
        kind = state.get("kind")
        if kind == "temp_manual_email":
            local = text.lower().strip()
            address = local if "@" in local else f"{local}@{TEMPMAIL_DOMAIN}"
            if not re.fullmatch(r"[a-z0-9._-]{3,32}@" + re.escape(TEMPMAIL_DOMAIN), address):
                bot.send_message(message.chat.id, "❌ اسم الإيميل غير صالح.")
                return
            item, created = add_managed_email(user_id, address, "manual")
            user_states.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                ("✅ تم إنشاء الإيميل" if created else "ℹ️ الإيميل موجود مسبقاً")
                + "\n\n"
                + managed_email_summary(item),
                reply_markup=managed_email_keyboard(item),
            )
            return

        if kind == "temp_change_pin":
            if not re.fullmatch(r"\d{3,12}", text):
                bot.send_message(message.chat.id, "أرسل رمزاً من 3 إلى 12 رقم.")
                return
            item = get_managed_email(user_id, state.get("email_id"))
            profile = (
                next(
                    (p for p in item.get("profiles", []) if p.get("number") == state.get("profile_number")),
                    None,
                )
                if item
                else None
            )
            if not profile:
                user_states.pop(user_id, None)
                bot.send_message(message.chat.id, "❌ لم يتم العثور على البروفايل.")
                return
            profile["pin"] = text
            save_state(BOT_STATE)
            user_states.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "✅ تم تغيير الرمز.",
                reply_markup=profile_keyboard(item, profile),
            )
            return

    if state == "waiting_for_amount":
        try:
            amount = float(text)
            if amount < 1.5:
                bot.send_message(
                    message.chat.id,
                    "⚠️ عذراً، الحد الأدنى للإيداع هو **1.5 دولار**.",
                    parse_mode="Markdown",
                )
                return
            user_states.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                f"⏳ جاري إنشاء محفظة إيداع بمبلغ **{amount}$** عبر شبكة USDT BSC...",
                parse_mode="Markdown",
            )
            params = {"api_key": user["api_key"], "coin": "usdt", "network": "bsc"}
            response = requests.get(WALLET_URL, params=params, timeout=15)
            wallet_address = response.json().get("wallet_address", "تعذر الحصول على العنوان")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu"))
            bot.send_message(
                message.chat.id,
                f"💳 **عنوان المحفظة للإيداع بقيمة {amount}$ (USDT - BSC):**\n\n`{wallet_address}`",
                reply_markup=markup,
                parse_mode="Markdown",
            )
        except ValueError:
            bot.send_message(message.chat.id, "⚠️ يرجى إدخال رقم صحيح.")
        return

    if state == "waiting_for_netflix_email":
        account_email = normalize_email(text)
        if not is_valid_email(account_email):
            bot.send_message(message.chat.id, "أرسل بريداً إلكترونياً صحيحاً.")
            return
        if not is_authorized_netflix_email(account_email):
            bot.send_message(message.chat.id, "هذا البريد غير موجود ضمن الحسابات المسموح بإدارتها ❌")
            user_states.pop(user_id, None)
            return
        if user_id in active_password_rotations:
            bot.send_message(message.chat.id, "توجد عملية جارية حالياً، انتظر حتى تنتهي.")
            return

        user_states.pop(user_id, None)
        active_password_rotations.add(user_id)
        working = bot.send_message(message.chat.id, "⏳ جارٍ العمل...")
        threading.Thread(
            target=password_rotation_worker,
            args=(message.chat.id, user_id, account_email, working.message_id),
            daemon=True,
        ).start()
        return

    if state == "waiting_for_new_default_password":
        if not strong_enough(text):
            bot.send_message(message.chat.id, "يجب أن تكون كلمة المرور بين 6 و60 حرفاً.")
            return
        with state_lock:
            BOT_STATE["netflix_default_password"] = text
            save_state(BOT_STATE)
        user_states.pop(user_id, None)
        bot.send_message(
            message.chat.id,
            "تم تحديث كلمة المرور الافتراضية ✅",
            reply_markup=main_keyboard(user_id),
        )
        return


@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    user = get_user_data(user_id)
    if not user:
        bot.answer_callback_query(call.id, "هذا البوت ليس مخصصاً لك!", show_alert=True)
        return

    api_key = user["api_key"]

    # --------------------------------------------------------
    # Temporary email + profiles callbacks (additive; original callbacks below stay intact)
    # --------------------------------------------------------
    if call.data == "email_create_menu":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "اختر طريقة إنشاء الإيميل:",
            chat_id,
            call.message.message_id,
            reply_markup=email_create_keyboard(),
        )
        return

    if call.data == "email_create_random":
        item, _ = add_managed_email(
            user_id,
            f"{make_temp_mail_name()}@{TEMPMAIL_DOMAIN}",
            "random",
        )
        bot.answer_callback_query(call.id, "تم إنشاء الإيميل")
        bot.edit_message_text(
            "✅ تم إنشاء إيميل جديد\n\n" + managed_email_summary(item),
            chat_id,
            call.message.message_id,
            reply_markup=managed_email_keyboard(item),
        )
        return

    if call.data == "email_create_manual":
        user_states[user_id] = {"kind": "temp_manual_email"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            f"✍️ أرسل اسم الإيميل فقط مثل nabil77 وسأضيف @{TEMPMAIL_DOMAIN}\n"
            f"أو أرسل إيميلاً كاملاً على @{TEMPMAIL_DOMAIN}.",
        )
        return

    if call.data in ("emails_list", "emails_sold"):
        sold_only = call.data == "emails_sold"
        emails = [
            item
            for item in temp_user_state(user_id).get("emails", [])
            if (item.get("status") == "completed") == sold_only
        ]
        bot.answer_callback_query(call.id)
        if not emails:
            bot.send_message(chat_id, "لا توجد إيميلات هنا حالياً.")
            return
        markup = InlineKeyboardMarkup(row_width=1)
        for item in emails:
            markup.add(
                InlineKeyboardButton(
                    item["address"],
                    callback_data=f"email_open:{item['id']}",
                )
            )
        markup.add(InlineKeyboardButton("🔙 الرئيسية", callback_data="main_menu"))
        bot.send_message(
            chat_id,
            "اختر الإيميل:" if not sold_only else "الإيميلات المباعة:",
            reply_markup=markup,
        )
        return

    if call.data.startswith("email_open:"):
        email_id = call.data.split(":", 1)[1]
        item = get_managed_email(user_id, email_id)
        bot.answer_callback_query(call.id)
        if not item:
            bot.send_message(chat_id, "❌ لم يتم العثور على الإيميل.")
            return
        temp_user_state(user_id)["selected"] = item["id"]
        save_state(BOT_STATE)
        bot.send_message(
            chat_id,
            managed_email_summary(item),
            reply_markup=managed_email_keyboard(item),
        )
        return

    if call.data.startswith("email_profiles:"):
        email_id = call.data.split(":", 1)[1]
        item = get_managed_email(user_id, email_id)
        bot.answer_callback_query(call.id)
        if item:
            bot.send_message(chat_id, "👥 البروفايلات 1 → 5", reply_markup=profiles_keyboard(item))
        return

    if call.data.startswith("email_profile:"):
        _, email_id, number_text = call.data.split(":", 2)
        item = get_managed_email(user_id, email_id)
        profile = (
            next((p for p in item.get("profiles", []) if p.get("number") == int(number_text)), None)
            if item
            else None
        )
        bot.answer_callback_query(call.id)
        if profile:
            status_ar = {
                "available": "متاح",
                "review": "قيد المراجعة",
                "sold": "تم البيع",
            }.get(profile.get("status"), profile.get("status"))
            bot.send_message(
                chat_id,
                f"{PROFILE_COLORS[profile['number']]} البروفايل {profile['number']}\n"
                f"🔐 الرمز: {profile['pin']}\n"
                f"📌 الحالة: {status_ar}\n"
                f"📧 {item['address']}",
                reply_markup=profile_keyboard(item, profile),
            )
        return

    if call.data.startswith("email_change_pin:"):
        _, email_id, number_text = call.data.split(":", 2)
        user_states[user_id] = {
            "kind": "temp_change_pin",
            "email_id": email_id,
            "profile_number": int(number_text),
        }
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "✏️ أرسل الرمز الجديد:")
        return

    if call.data.startswith("email_profile_status:"):
        _, email_id, number_text, status = call.data.split(":", 3)
        if status not in {"available", "review", "sold"}:
            return
        item = get_managed_email(user_id, email_id)
        profile = (
            next((p for p in item.get("profiles", []) if p.get("number") == int(number_text)), None)
            if item
            else None
        )
        bot.answer_callback_query(call.id)
        if not profile:
            return
        profile["status"] = status
        profile["sold_at"] = time.time() if status == "sold" else None
        if all(p.get("status") == "sold" for p in item.get("profiles", [])):
            item["status"] = "completed"
        elif item.get("status") == "completed":
            item["status"] = "active"
        save_state(BOT_STATE)
        bot.send_message(
            chat_id,
            "✅ تم تحديث حالة البروفايل.",
            reply_markup=profile_keyboard(item, profile),
        )
        return

    if call.data.startswith("email_delete:"):
        email_id = call.data.split(":", 1)[1]
        data = temp_user_state(user_id)
        data["emails"] = [x for x in data.get("emails", []) if x.get("id") != email_id]
        if data.get("selected") == email_id:
            data["selected"] = data["emails"][0]["id"] if data["emails"] else None
        save_state(BOT_STATE)
        bot.answer_callback_query(call.id, "تم الحذف")
        bot.send_message(chat_id, "🗑️ تم حذف الإيميل من البوت.")
        return

    if call.data.startswith("show_email:"):
        item = get_managed_email(user_id, call.data.split(":", 1)[1])
        bot.answer_callback_query(call.id)
        if item:
            bot.send_message(chat_id, f"📧 `{item['address']}`", parse_mode="Markdown")
        return

    if call.data.startswith("email_code_or_link:"):
        item = get_managed_email(user_id, call.data.split(":", 1)[1])
        bot.answer_callback_query(call.id)
        if not item:
            return
        try:
            messages = fetch_temp_mailbox(item["address"])
        except Exception:
            bot.send_message(chat_id, "❌ تعذر جلب البريد حالياً.")
            return
        found = None
        # Requested priority: code first; if no code, important link.
        for message in messages:
            if message.get("code"):
                found = ("code", message["code"], message)
                break
        if not found:
            for message in messages:
                link = important_message_url(message.get("urls", []))
                if link:
                    found = ("url", link, message)
                    break
        if not found:
            bot.send_message(chat_id, "📭 لا يوجد كود أو رابط مهم حالياً.")
            return
        kind, value, message = found
        discovered = message_discovered_at(item, message)
        save_state(BOT_STATE)
        markup = InlineKeyboardMarkup()
        if kind == "code":
            try:
                markup.add(InlineKeyboardButton(f"📋 نسخ الكود {value}", copy_text={"text": value}))
            except TypeError:
                pass
            bot.send_message(
                chat_id,
                f"🔢 آخر كود: {value}\n⏱️ وصل هذا الكود {relative_time(discovered)}",
                reply_markup=markup,
            )
        else:
            markup.add(InlineKeyboardButton("🔗 فتح الرابط", url=value))
            bot.send_message(chat_id, "🔗 تم العثور على الرابط المهم.", reply_markup=markup)
        return

    if call.data.startswith("email_latest:") or call.data.startswith("email_all_messages:"):
        all_messages = call.data.startswith("email_all_messages:")
        email_id = call.data.split(":", 1)[1]
        item = get_managed_email(user_id, email_id)
        bot.answer_callback_query(call.id)
        if not item:
            return
        try:
            messages = fetch_temp_mailbox(item["address"])
        except Exception:
            bot.send_message(chat_id, "❌ تعذر جلب الرسائل حالياً.")
            return
        messages = messages if all_messages else messages[:5]
        if not messages:
            bot.send_message(chat_id, "📭 لا توجد رسائل حالياً.")
            return
        for index, message in enumerate(messages, 1):
            discovered = message_discovered_at(item, message)
            body = message.get("body") or "(الرسالة بدون نص ظاهر)"
            text = (
                f"📬 الرسالة {index}\n"
                f"📧 إلى: {item['address']}\n"
                f"👤 من: {message.get('from', 'غير معروف')}\n"
                f"📝 العنوان: {message.get('subject', 'بدون عنوان')}\n"
                f"⏱️ وصلت {relative_time(discovered)}\n"
                "━━━━━━━━━━━━━━\n"
                f"{body[:3200]}"
            )
            keyboard = InlineKeyboardMarkup()
            if message.get("code"):
                try:
                    keyboard.add(
                        InlineKeyboardButton(
                            f"📋 نسخ الكود {message['code']}",
                            copy_text={"text": str(message["code"])},
                        )
                    )
                except TypeError:
                    pass
            link = important_message_url(message.get("urls", []))
            if link:
                keyboard.add(InlineKeyboardButton("🔗 فتح الرابط المهم", url=link))
            bot.send_message(chat_id, text, reply_markup=keyboard)
        if not all_messages:
            more = InlineKeyboardMarkup()
            more.add(
                InlineKeyboardButton(
                    "📚 جلب كل الرسائل والكودات",
                    callback_data=f"email_all_messages:{item['id']}",
                )
            )
            bot.send_message(chat_id, "لإظهار كل محتوى الصندوق:", reply_markup=more)
        save_state(BOT_STATE)
        return

    if call.data == "netflix_rotate_password":
        user_states[user_id] = "waiting_for_netflix_email"
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu"))
        bot.edit_message_text(
            "🔐 أرسل الآن إيميل حساب نتفلكس المسموح بإدارته:",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    if call.data == "netflix_toggle_signout":
        with state_lock:
            BOT_STATE["netflix_sign_out_all"] = not bool(BOT_STATE.get("netflix_sign_out_all", True))
            save_state(BOT_STATE)
        bot.answer_callback_query(call.id, "تم تحديث الخيار")
        bot.edit_message_reply_markup(
            chat_id,
            call.message.message_id,
            reply_markup=main_keyboard(user_id),
        )
        return

    if call.data == "netflix_change_default_password":
        user_states[user_id] = "waiting_for_new_default_password"
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu"))
        bot.edit_message_text(
            "✏️ أرسل كلمة المرور الافتراضية الجديدة الآن:\n\n"
            "لن يتم عرضها مجدداً داخل البوت.",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    if call.data == "deposit_bsc":
        user_states[user_id] = "waiting_for_amount"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 إلغاء والعودة للقائمة", callback_data="main_menu"))
        bot.edit_message_text(
            "💳 **إيداع الأموال (USDT - BSC):**\nأرسل الآن المبلغ:",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown",
        )
        return

    if call.data == "main_menu":
        user_states.pop(user_id, None)
        bot.edit_message_text(
            f"مرحباً بك يا **{user['name']}** في بوت أرقام نتفلكس العراقية 🇮🇶",
            chat_id,
            call.message.message_id,
            reply_markup=main_keyboard(user_id),
            parse_mode="Markdown",
        )
        return

    if call.data == "buy_number":
        user_states.pop(user_id, None)
        bot.answer_callback_query(call.id, "جاري البحث المستمر عن رقم...")
        if chat_id in user_active_orders:
            cancel_order(api_key, user_active_orders[chat_id])
            user_active_orders.pop(chat_id, None)

        status_msg = bot.send_message(chat_id, "⏳ جاري البحث المستمر عن رقم يبدأ بـ 96477 أو 96478...")
        success = False
        phone_number = ""
        activation_id = ""

        while not success:
            params = {
                "api_key": api_key,
                "action": "getNumber",
                "service": SERVICE_NETFLIX,
                "country": COUNTRY_IRAQ,
            }
            try:
                response = requests.get(BASE_URL, params=params, timeout=15)
                res_text = response.text.strip()
                if res_text.startswith("ACCESS_NUMBER:"):
                    parts = res_text.split(":")
                    activation_id = parts[1]
                    phone_number = parts[2]
                    if phone_number.startswith("96477") or phone_number.startswith("96478"):
                        success = True
                    else:
                        cancel_order(api_key, activation_id)
                else:
                    time.sleep(3)
            except Exception:
                time.sleep(3)

        user_active_orders[chat_id] = activation_id
        cancel_markup = InlineKeyboardMarkup()
        cancel_markup.add(
            InlineKeyboardButton(
                "❌ إلغاء الرقم واسترداد الرصيد",
                callback_data=f"cancel_num_{activation_id}",
            )
        )
        network_name = "آسياسيل" if phone_number.startswith("96477") else "زين العراق"
        msg = (
            f"✅ **تم شراء الرقم بنجاح ({network_name})**\n\n"
            f"📞 الرقم:\n`{phone_number}`\n\n"
            f"🆔 رقم العملية:\n`{activation_id}`\n\n"
            "⏳ جاري انتظار وصول الكود..."
        )
        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass
        bot.send_message(chat_id, msg, reply_markup=cancel_markup, parse_mode="Markdown")
        threading.Thread(
            target=check_for_sms,
            args=(chat_id, api_key, activation_id, phone_number),
            daemon=True,
        ).start()
        return

    if call.data.startswith("cancel_num_"):
        activation_id = call.data.split("cancel_num_", 1)[1]
        bot.answer_callback_query(call.id, "جاري إلغاء الرقم...")
        cancel_order(api_key, activation_id)
        if user_active_orders.get(chat_id) == activation_id:
            user_active_orders.pop(chat_id, None)
        bot.send_message(chat_id, "❌ **تم إلغاء الرقم بنجاح واسترداد الرصيد.**", parse_mode="Markdown")
        return

    if call.data == "get_balance":
        _, balance_text = get_current_balance(api_key)
        bot.answer_callback_query(call.id, f"رصيدك الحالي هو: {balance_text}", show_alert=True)
        return

# ============================================================
# Start
# ============================================================

threading.Thread(target=balance_monitor, daemon=True).start()
threading.Thread(target=background_temp_mail_monitor, daemon=True).start()
threading.Thread(target=cleanup_expired_emails, daemon=True).start()

print("البوت يعمل الآن...")
bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
