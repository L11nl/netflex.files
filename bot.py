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
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
import telebot
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton

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
MAIL_MODE = os.getenv("MAIL_MODE", "generator").strip().lower()  # generator | imap
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
generator_cache_lock = threading.RLock()
generator_session_cache = {}
number_search_lock = threading.RLock()
number_search_jobs = {}
user_last_balance_texts = {}

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


def _normalize_profile(profile, number):
    profile = profile if isinstance(profile, dict) else {}
    profile["number"] = int(number)
    profile["pin"] = str(profile.get("pin") or DEFAULT_PROFILE_PINS[int(number)])
    status = str(profile.get("status") or "available")
    if status not in {"available", "review", "sold"}:
        status = "available"
    profile["status"] = status
    profile["sold_at"] = profile.get("sold_at") if status == "sold" else None
    return profile


def _normalize_managed_email(item):
    item = item if isinstance(item, dict) else {}
    now = time.time()
    address = normalize_email(str(item.get("address") or item.get("email") or ""))
    item["address"] = address
    if not item.get("id"):
        seed = f"{address}|{item.get('created_at', now)}".encode("utf-8", errors="ignore")
        item["id"] = "m" + hashlib.sha1(seed).hexdigest()[:16]
    try:
        created_at = float(item.get("created_at", now))
    except (TypeError, ValueError):
        created_at = now
    item["created_at"] = created_at
    try:
        expires_at = float(item.get("expires_at", created_at + EMAIL_LIFETIME_SECONDS))
    except (TypeError, ValueError):
        expires_at = created_at + EMAIL_LIFETIME_SECONDS
    item["expires_at"] = expires_at
    item["mode"] = str(item.get("mode") or "random")

    existing = {}
    for p in item.get("profiles", []) if isinstance(item.get("profiles"), list) else []:
        try:
            number = int(p.get("number"))
        except Exception:
            continue
        if 1 <= number <= 5:
            existing[number] = p
    item["profiles"] = [_normalize_profile(existing.get(i), i) for i in range(1, 6)]
    item["status"] = "completed" if all(p["status"] == "sold" for p in item["profiles"]) else "active"

    seen = item.get("seen")
    if not isinstance(seen, list):
        seen = item.get("generatorSeenIds") if isinstance(item.get("generatorSeenIds"), list) else []
    item["seen"] = [str(x) for x in seen if x][-200:]
    if not isinstance(item.get("message_times"), dict):
        item["message_times"] = {}
    try:
        item["auto_until"] = float(item.get("auto_until", created_at + AUTO_MAIL_WINDOW_SECONDS))
    except (TypeError, ValueError):
        item["auto_until"] = created_at + AUTO_MAIL_WINDOW_SECONDS
    try:
        item["auto_count"] = max(0, int(item.get("auto_count", 0)))
    except (TypeError, ValueError):
        item["auto_count"] = 0
    return item


def temp_user_state(user_id):
    """Return persistent email state for one Telegram user and migrate old state safely."""
    key = str(user_id)
    changed = False
    with state_lock:
        users = BOT_STATE.setdefault("users", {})
        if key not in users or not isinstance(users[key], dict):
            users[key] = {"emails": [], "selected": None}
            changed = True
        data = users[key]
        if not isinstance(data.get("emails"), list):
            data["emails"] = []
            changed = True
        normalized = []
        for raw in data["emails"]:
            if not isinstance(raw, dict):
                changed = True
                continue
            item = _normalize_managed_email(raw)
            if item.get("address"):
                normalized.append(item)
        if normalized != data["emails"]:
            data["emails"] = normalized
            changed = True
        ids = {x.get("id") for x in data["emails"]}
        if data.get("selected") not in ids:
            data["selected"] = data["emails"][0]["id"] if data["emails"] else None
            changed = True
    if changed:
        save_state(BOT_STATE)
    return data



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
        "status": "active",
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
    with state_lock:
        item = next((x for x in data.get("emails", []) if x.get("id") == email_id), None)
        return _normalize_managed_email(item) if item else None




def add_managed_email(user_id, address, mode="random"):
    address = normalize_email(address)
    data = temp_user_state(user_id)
    with state_lock:
        old_email = next((x for x in data["emails"] if x.get("address") == address), None)
        if old_email:
            data["selected"] = old_email["id"]
            item = old_email
            created = False
        else:
            item = make_managed_email(address, mode)
            data["emails"].insert(0, item)
            data["selected"] = item["id"]
            created = True
    save_state(BOT_STATE)
    return item, created



def make_temp_mail_name():
    chars = string.ascii_lowercase + string.digits
    while True:
        name = "".join(random.choice(chars) for _ in range(random.choice([5, 6])))
        if any(c.isalpha() for c in name) and any(c.isdigit() for c in name):
            return name



def normalize_mail_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\xad]", "", value)
    lines = []
    for raw in value.splitlines():
        line = re.sub(r"[\t\u00a0 ]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)



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



def _generator_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Mobile) "
            "AppleWebKit/537.36 Chrome/136.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def _cached_generator_session(address):
    address = normalize_email(address)
    with generator_cache_lock:
        entry = generator_session_cache.get(address)
        if entry is None:
            session = requests.Session()
            session.headers.update(_generator_headers())
            entry = {"session": session, "lock": threading.RLock(), "bootstrapped": False, "used": time.time()}
            generator_session_cache[address] = entry
        entry["used"] = time.time()
        # Prevent unbounded cache growth.
        if len(generator_session_cache) > 100:
            stale = sorted(generator_session_cache.items(), key=lambda kv: kv[1].get("used", 0))[:-80]
            for key, old in stale:
                try:
                    old["session"].close()
                except Exception:
                    pass
                generator_session_cache.pop(key, None)
        return entry


def _parse_generator_messages(page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    table = soup.find("div", id="email-table")
    if not table:
        return []

    senders = [
        n for n in table.find_all("div", class_="from_div_45g45gg")
        if n.get_text(" ", strip=True).strip().lower() not in {"from", "المرسل"}
    ]
    subjects = [
        n for n in table.find_all("div", class_="subj_div_45g45gg")
        if n.get_text(" ", strip=True).strip().lower() not in {"subject", "العنوان"}
    ]
    bodies = table.find_all("div", class_="mess_bodiyy")

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
                href = html.unescape((tag.get("href") or "").strip())
                if href.startswith(("http://", "https://")):
                    urls.append(href)
                    title = tag.get_text(" ", strip=True)
                    tag.replace_with(f"\n{title}\n{href}\n" if title else f"\n{href}\n")
            body = normalize_mail_text(body_copy.get_text("\n", strip=True))

        urls = list(dict.fromkeys([u for u in urls + extract_urls(body) if u]))
        visible_for_code = re.sub(r"https?://\S+", " ", subject + "\n" + body, flags=re.I)
        digest_source = f"{sender}|{subject}|{body[:2000]}".encode("utf-8", errors="ignore")
        messages.append(
            {
                "id": hashlib.sha1(digest_source).hexdigest(),
                "from": sender or "غير معروف",
                "subject": subject or "بدون عنوان",
                "body": body,
                "urls": urls,
                "code": extract_verification_code(visible_for_code),
            }
        )
    # Preserve source ordering but drop exact duplicate message ids.
    unique = []
    seen_ids = set()
    for msg in messages:
        if msg["id"] in seen_ids:
            continue
        seen_ids.add(msg["id"])
        unique.append(msg)
    return unique[:30]



def fetch_temp_mailbox(address):
    """Read generator.email quickly with a persistent cookie/session per mailbox."""
    address = normalize_email(address)
    if "@" not in address:
        return []
    username, domain = address.split("@", 1)
    base = GENERATOR_BASE_URL.rstrip("/")
    entry = _cached_generator_session(address)
    with entry["lock"]:
        session = entry["session"]
        if not entry.get("bootstrapped"):
            try:
                session.get(f"{base}/{domain}/{username}", timeout=12, allow_redirects=True)
            except Exception:
                try:
                    session.get(base, timeout=10, allow_redirects=True)
                except Exception:
                    pass
            entry["bootstrapped"] = True

        urls_to_try = [
            f"{base}/{GENERATOR_INBOX}/{address}",
            f"{base}/{domain}/{username}",
        ]
        last_error = None
        for mailbox_url in urls_to_try:
            try:
                response = session.get(mailbox_url, timeout=15, allow_redirects=True)
                response.raise_for_status()
                messages = _parse_generator_messages(response.text)
                if messages or 'id="email-table"' in response.text or "id='email-table'" in response.text:
                    return messages
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []



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
    markup.add(
        InlineKeyboardButton(
            "📋 نسخ الإيميل",
            copy_text=CopyTextButton(text=str(item["address"])[:256]),
        )
    )
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
    markup.add(
        InlineKeyboardButton(
            f"📋 نسخ الرمز {profile['pin']}",
            copy_text=CopyTextButton(text=str(profile["pin"])[:256]),
        )
    )
    markup.add(
        InlineKeyboardButton(
            "📧 نسخ الإيميل",
            copy_text=CopyTextButton(text=str(item["address"])[:256]),
        )
    )
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



def _deliver_auto_mail(user_id, item_id, messages):
    """Apply one mailbox poll result under the state lock and send only eligible new mail."""
    for message in reversed(messages[:10]):
        with state_lock:
            item = get_managed_email(user_id, item_id)
            if not item:
                return False
            now = time.time()
            if item.get("status") == "completed":
                return False
            if now > float(item.get("auto_until", 0)):
                return False
            if int(item.get("auto_count", 0)) >= AUTO_MAIL_MAX_MESSAGES:
                return False
            mid = message.get("id")
            if not mid or mid in item.setdefault("seen", []):
                continue

        keyboard = InlineKeyboardMarkup()
        if message.get("code"):
            keyboard.add(
                InlineKeyboardButton(
                    f"📋 نسخ الكود {message['code']}",
                    copy_text=CopyTextButton(text=str(message["code"])[:256]),
                )
            )
        else:
            link = important_message_url(message.get("urls", []))
            if link:
                keyboard.add(InlineKeyboardButton("🔗 فتح الرابط", url=link))
        try:
            bot.send_message(user_id, format_auto_mail(item, message), reply_markup=keyboard)
        except Exception as exc:
            print(f"auto mail send failed: {type(exc).__name__}")
            # Do not mark it seen; a later poll can retry.
            continue

        with state_lock:
            current = get_managed_email(user_id, item_id)
            if not current:
                return True
            if mid not in current.setdefault("seen", []):
                current["seen"].append(mid)
                current["seen"] = current["seen"][-200:]
            message_discovered_at(current, message)
            current["auto_count"] = min(
                AUTO_MAIL_MAX_MESSAGES,
                int(current.get("auto_count", 0)) + 1,
            )
        save_state(BOT_STATE)
    return True


def background_temp_mail_monitor():
    """Poll active mailboxes concurrently; first 5 messages only during first 20 minutes."""
    workers = max(1, min(8, int(os.getenv("AUTO_MAIL_WORKERS", "6"))))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mailpoll") as pool:
        while True:
            time.sleep(max(1.0, AUTO_MAIL_POLL_SECONDS))
            now = time.time()
            jobs = []
            with state_lock:
                for user_id_text, data in list(BOT_STATE.get("users", {}).items()):
                    try:
                        user_id = int(user_id_text)
                    except (TypeError, ValueError):
                        continue
                    for item in list(data.get("emails", [])):
                        if item.get("status") == "completed":
                            continue
                        if now > float(item.get("auto_until", 0)):
                            continue
                        if int(item.get("auto_count", 0)) >= AUTO_MAIL_MAX_MESSAGES:
                            continue
                        if not item.get("address") or not item.get("id"):
                            continue
                        jobs.append((user_id, item["id"], item["address"]))

            futures = {
                pool.submit(fetch_temp_mailbox, address): (user_id, item_id)
                for user_id, item_id, address in jobs
            }
            for future in as_completed(futures):
                user_id, item_id = futures[future]
                try:
                    messages = future.result()
                except Exception:
                    continue
                _deliver_auto_mail(user_id, item_id, messages)




def cleanup_expired_emails(run_once=False):
    while True:
        now = time.time()
        changed = False
        with state_lock:
            for data in BOT_STATE.get("users", {}).values():
                emails = data.get("emails", []) if isinstance(data, dict) else []
                old_count = len(emails)
                kept = []
                for x in emails:
                    if not isinstance(x, dict):
                        changed = True
                        continue
                    try:
                        expires = float(x.get("expires_at", 0) or 0)
                    except (TypeError, ValueError):
                        expires = 0
                    if expires > now:
                        kept.append(x)
                data["emails"] = kept
                if len(data["emails"]) != old_count:
                    changed = True
                selected = data.get("selected")
                if selected and not any(x.get("id") == selected for x in data["emails"]):
                    data["selected"] = data["emails"][0]["id"] if data["emails"] else None
                    changed = True
        if changed:
            save_state(BOT_STATE)
        if run_once:
            return
        time.sleep(300)


# ============================================================
# Utilities
# ============================================================


def safe_answer_callback(call, text=None, show_alert=False):
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=show_alert)
    except Exception:
        pass


def safe_edit_message(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    try:
        return bot.edit_message_text(
            text,
            chat_id,
            message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception:
        try:
            return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            return None


def back_to_main_keyboard():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu"))
    return markup


def _parse_email_profile_callback(data, expected_parts):
    try:
        parts = data.split(":")
        if len(parts) != expected_parts:
            return None
        return parts
    except Exception:
        return None


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
    # Match Netflix current password guidance more closely so a weak/default
    # password is rejected before opening the reset flow.
    return (
        8 <= len(password) <= 60
        and bool(re.search(r"[A-Z]", password))
        and bool(re.search(r"[a-z]", password))
        and bool(re.search(r"\d", password))
        and bool(re.search(r"[^A-Za-z0-9]", password))
    )


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
        for user_id, user_info in list(USERS.items()):
            api_key = user_info.get("api_key", "")
            if not api_key:
                continue
            num_val, text_val = get_current_balance(api_key)
            old = user_last_balances.get(user_id)
            user_last_balance_texts[user_id] = text_val
            if old is None:
                user_last_balances[user_id] = num_val
            elif num_val != old:
                user_last_balances[user_id] = num_val
                if num_val > old:
                    try:
                        bot.send_message(
                            user_id,
                            f"🎉 **تم استلام الأموال بنجاح!**\n💰 رصيدك الحالي أصبح: **{text_val}**",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
        time.sleep(30)



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
    consecutive_errors = 0
    while time.time() - start_time < 600:
        time.sleep(5)
        if user_active_orders.get(chat_id) != activation_id:
            return
        try:
            params = {"api_key": api_key, "action": "getStatus", "id": activation_id}
            response = requests.get(BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            res = response.text.strip()
            consecutive_errors = 0
            if res.startswith("STATUS_OK:"):
                code = res.split(":", 1)[1]
                user_active_orders.pop(chat_id, None)
                bot.send_message(chat_id, code)
                return
            if res == "STATUS_CANCEL":
                user_active_orders.pop(chat_id, None)
                bot.send_message(chat_id, "❌ تم إلغاء العملية من قبل الموقع.")
                return
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= 6:
                try:
                    bot.send_message(chat_id, "⚠️ تعذر فحص كود الرسالة مؤقتاً. أعد المحاولة بعد قليل.")
                except Exception:
                    pass
                return
    if user_active_orders.get(chat_id) == activation_id:
        try:
            bot.send_message(chat_id, "⌛ انتهت مدة انتظار الكود. يمكنك إلغاء الرقم أو شراء رقم جديد.")
        except Exception:
            pass


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



def wait_reset_link_imap(target_email, since_ts, exclude_urls=None):
    if not all([IMAP_HOST, IMAP_USER, IMAP_PASSWORD]):
        raise RuntimeError("IMAP configuration is incomplete")
    exclude_urls = set(exclude_urls or ())
    deadline = time.time() + RESET_MAIL_TIMEOUT
    while time.time() < deadline:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
            mail.login(IMAP_USER, IMAP_PASSWORD)
            mail.select(IMAP_FOLDER)
            status, data = mail.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            ids = data[0].split()[-60:]
            for msg_id in reversed(ids):
                status, payload = mail.fetch(msg_id, "(RFC822)")
                if status != "OK" or not payload:
                    continue
                raw_bytes = next(
                    (item[1] for item in payload if isinstance(item, tuple) and isinstance(item[1], bytes)),
                    None,
                )
                if not raw_bytes:
                    continue
                msg = email.message_from_bytes(raw_bytes)
                try:
                    msg_dt = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                    if msg_dt is not None and msg_dt.timestamp() < since_ts - 90:
                        continue
                except Exception:
                    pass
                to_header = " ".join(
                    filter(None, [msg.get("To"), msg.get("Delivered-To"), msg.get("X-Original-To")])
                ).lower()
                subject = decode_mime_header(msg.get("Subject", "")).lower()
                sender = decode_mime_header(msg.get("From", "")).lower()
                body, urls = message_text_and_urls(msg)
                combined = to_header + "\n" + body.lower()
                if target_email.lower() not in combined and target_email.lower() not in to_header:
                    continue
                if "netflix" not in sender and "netflix" not in subject and "netflix" not in body.lower():
                    continue
                chosen = choose_reset_url([u for u in urls if u not in exclude_urls])
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
        time.sleep(max(1.0, RESET_MAIL_POLL))
    raise TimeoutError("reset email timeout")




def generator_session():
    session = requests.Session()
    session.headers.update(_generator_headers())
    try:
        session.get(GENERATOR_BASE_URL.rstrip("/"), timeout=15, allow_redirects=True)
    except Exception:
        pass
    return session




def generator_read_messages(session, target_email):
    target_email = normalize_email(target_email)
    local, domain = target_email.split("@", 1)
    base = GENERATOR_BASE_URL.rstrip("/")
    urls_to_try = [
        f"{base}/{GENERATOR_INBOX}/{target_email}",
        f"{base}/{domain}/{local}",
    ]
    for mailbox_url in urls_to_try:
        response = session.get(mailbox_url, timeout=15, allow_redirects=True)
        response.raise_for_status()
        parsed = _parse_generator_messages(response.text)
        if parsed or 'id="email-table"' in response.text or "id='email-table'" in response.text:
            return [
                {
                    "id": item["id"],
                    "sender": item["from"],
                    "subject": item["subject"],
                    "text": item["body"],
                    "urls": item["urls"],
                }
                for item in parsed
            ]
    return []




def wait_reset_link_generator(target_email, since_ts, exclude_urls=None, session=None):
    session = session or generator_session()
    exclude_urls = set(exclude_urls or ())
    deadline = time.time() + RESET_MAIL_TIMEOUT
    while time.time() < deadline:
        try:
            messages = generator_read_messages(session, target_email)
            for item in messages[:15]:
                hay = f"{item['sender']}\n{item['subject']}\n{item['text']}".lower()
                if "netflix" not in hay:
                    continue
                chosen = choose_reset_url([u for u in item["urls"] if u not in exclude_urls])
                if chosen:
                    return chosen
        except Exception as exc:
            print(f"generator poll failed: {type(exc).__name__}")
        time.sleep(max(1.0, RESET_MAIL_POLL))
    raise TimeoutError("reset email timeout")




def wait_for_reset_link(target_email, since_ts, exclude_urls=None, generator_mail_session=None):
    if MAIL_MODE == "generator":
        return wait_reset_link_generator(
            target_email,
            since_ts,
            exclude_urls=exclude_urls,
            session=generator_mail_session,
        )
    return wait_reset_link_imap(target_email, since_ts, exclude_urls=exclude_urls)


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
    # Dismiss common cookie banner when it blocks the form.
    for candidate in [
        page.get_by_role("button", name=re.compile(r"accept|agree|قبول|موافق", re.I)),
        page.locator('button[data-uia*="cookie" i]'),
    ]:
        try:
            if candidate.count() and candidate.first.is_visible():
                candidate.first.click(timeout=2000)
                break
        except Exception:
            pass

    # Some layouts require choosing Email before the input is shown.
    for candidate in [
        page.get_by_role("radio", name=re.compile(r"email|البريد", re.I)),
        page.locator('input[type="radio"][value*="email" i]'),
    ]:
        try:
            if candidate.count() and candidate.first.is_visible() and not candidate.first.is_checked():
                candidate.first.check(force=True)
                page.wait_for_timeout(300)
                break
        except Exception:
            pass

    field = first_visible(
        [
            page.locator('input[type="email"]'),
            page.locator('input[name="email"]'),
            page.locator('input[id*="email" i]'),
            page.locator('input[autocomplete="email"]'),
        ]
    )
    if not field:
        raise RuntimeError("email field not found")
    field.fill(account_email)

    button = first_visible(
        [
            page.get_by_role("button", name=re.compile(r"email|send|إرسال|البريد|text me|رسالة", re.I)),
            page.locator('button[type="submit"]'),
            page.locator('input[type="submit"]'),
        ]
    )
    if not button:
        raise RuntimeError("submit button not found")
    button.click()




def fill_new_password_form(page, new_password, sign_out_all):
    page.wait_for_load_state("domcontentloaded")
    for _ in range(3):
        visible_passwords = [
            page.locator('input[type="password"]').nth(i)
            for i in range(page.locator('input[type="password"]').count())
            if page.locator('input[type="password"]').nth(i).is_visible()
        ]
        if len(visible_passwords) >= 2:
            break
        page.wait_for_timeout(700)
    if len(visible_passwords) < 2:
        raise RuntimeError("password fields not found")

    visible_passwords[0].fill(new_password)
    visible_passwords[1].fill(new_password)

    checkbox = first_visible(
        [
            page.get_by_role(
                "checkbox",
                name=re.compile(r"sign out|all devices|تسجيل الخروج|جميع الأجهزة", re.I),
            ),
            page.locator('input[type="checkbox"][name*="sign" i]'),
            page.locator('input[type="checkbox"][id*="sign" i]'),
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
            page.get_by_role("button", name=re.compile(r"save|حفظ|continue|متابعة|submit|إرسال", re.I)),
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
    page.wait_for_timeout(1200)

    # Netflix can leave the form visible when it rejects a weak/common password.
    # Detect that case explicitly instead of returning the same generic failure.
    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body_text = ""
    rejection_markers = (
        "not secure",
        "too common",
        "same as your last password",
        "كلمة المرور هذه غير آمنة",
        "كلمة المرور تلك غير آمنة",
        "شائعة للغاية",
        "نفس كلمة المرور",
    )
    if any(marker in body_text for marker in rejection_markers):
        raise RuntimeError("password rejected")

    remaining = page.locator('input[type="password"]')
    visible_remaining = 0
    for i in range(remaining.count()):
        try:
            visible_remaining += 1 if remaining.nth(i).is_visible() else 0
        except Exception:
            pass
    if visible_remaining >= 2:
        raise RuntimeError("password form still present")




def rotate_netflix_password(account_email, new_password, sign_out_all):
    generator_mail_session = None
    exclude_urls = set()
    if MAIL_MODE == "generator":
        generator_mail_session = generator_session()
        try:
            for old in generator_read_messages(generator_mail_session, account_email)[:20]:
                exclude_urls.update(u for u in old.get("urls", []) if is_netflix_reset_url(u))
        except Exception:
            pass

    reset_requested_at = time.time()
    with sync_playwright() as p:
        browser = None
        context = None
        try:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(locale="ar-IQ", viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.set_default_timeout(20000)
            page.goto(NETFLIX_LOGIN_HELP_URL, wait_until="domcontentloaded", timeout=30000)
            fill_login_help_email(page, account_email)

            reset_url = wait_for_reset_link(
                account_email,
                reset_requested_at,
                exclude_urls=exclude_urls,
                generator_mail_session=generator_mail_session,
            )
            if not reset_url or not is_netflix_reset_url(reset_url):
                raise RuntimeError("invalid reset URL")
            page.goto(reset_url, wait_until="domcontentloaded", timeout=30000)
            fill_new_password_form(page, new_password, sign_out_all)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if generator_mail_session is not None:
                try:
                    generator_mail_session.close()
                except Exception:
                    pass



def password_rotation_worker(chat_id, user_id, account_email, status_message_id):
    try:
        with state_lock:
            new_password = str(BOT_STATE.get("netflix_default_password", DEFAULT_PASSWORD_FALLBACK))
            sign_out_all = bool(BOT_STATE.get("netflix_sign_out_all", True))

        if not strong_enough(new_password):
            raise RuntimeError("weak configured password")

        rotate_netflix_password(account_email, new_password, sign_out_all)
        bot.edit_message_text(
            "تم تغيير كلمة المرور بنجاح ✅",
            chat_id,
            status_message_id,
        )
    except Exception as exc:
        # Keep reset URLs, credentials and message contents out of Telegram/logs, but
        # expose a safe reason so Railway debugging is no longer a blind generic error.
        reason = str(exc)
        if isinstance(exc, TimeoutError) or reason == "reset email timeout":
            public_message = (
                "لم تصل رسالة إعادة تعيين كلمة مرور Netflix ضمن مدة الانتظار ❌\n"
                "تأكد أن الإيميل صحيح وأن صندوق Generator.email يستقبل رسائل Netflix، ثم أعد المحاولة."
            )
            safe_code = "RESET_MAIL_TIMEOUT"
        elif reason == "weak configured password":
            public_message = (
                "كلمة المرور الافتراضية الحالية لا تطابق متطلبات Netflix ❌\n"
                "غيّرها من زر ✏️ تغيير كلمة المرور الافتراضية واجعلها 8 أحرف أو أكثر وتحتوي كبير + صغير + رقم + رمز."
            )
            safe_code = "WEAK_CONFIGURED_PASSWORD"
        elif reason == "password rejected":
            public_message = (
                "Netflix رفضت كلمة المرور لأنها ضعيفة/شائعة أو غير مسموحة ❌\n"
                "غيّر كلمة المرور الافتراضية إلى كلمة أقوى ثم أعد المحاولة."
            )
            safe_code = "PASSWORD_REJECTED"
        elif reason in {"email field not found", "submit button not found"}:
            public_message = "تعذر الوصول إلى نموذج إرسال رابط الاستعادة في Netflix حالياً ❌"
            safe_code = "LOGIN_HELP_FORM_CHANGED"
        elif reason in {"password fields not found", "save button not found", "password form still present"}:
            public_message = "وصل رابط الاستعادة، لكن تعذر إكمال نموذج كلمة المرور في Netflix حالياً ❌"
            safe_code = "RESET_FORM_CHANGED"
        elif reason == "invalid reset URL":
            public_message = "وصلت رسالة Netflix لكن لم يتم العثور على رابط استعادة صالح ❌"
            safe_code = "INVALID_RESET_URL"
        elif reason == "IMAP configuration is incomplete":
            public_message = "إعداد البريد غير صحيح. اضبط MAIL_MODE=generator في Railway ثم أعد المحاولة ❌"
            safe_code = "MAIL_MODE_IMAP_MISCONFIGURED"
        elif isinstance(exc, PlaywrightTimeoutError):
            public_message = "انتهت مهلة تحميل صفحة Netflix. أعد المحاولة بعد قليل ❌"
            safe_code = "NETFLIX_PAGE_TIMEOUT"
        else:
            public_message = "تعذر إكمال تغيير كلمة المرور حالياً ❌"
            safe_code = "UNKNOWN"

        print(f"Netflix rotation failed: {safe_code}/{type(exc).__name__}")
        try:
            bot.edit_message_text(public_message, chat_id, status_message_id)
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
    balance_text = user_last_balance_texts.get(user_id)
    if balance_text is None:
        _, balance_text = get_current_balance(user.get("api_key", ""))
        user_last_balance_texts[user_id] = balance_text
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
        InlineKeyboardButton("📱 شراء رقم عراقي (آسياسيل أو زين العراق)", callback_data="buy_number")
    )
    markup.add(InlineKeyboardButton("💳 إيداع الأموال (USDT - BSC)", callback_data="deposit_bsc"))
    markup.add(
        InlineKeyboardButton(
            f"📴 تسجيل الخروج من جميع الأجهزة: {'✅ مفعّل' if sign_out else '❌ معطّل'}",
            callback_data="netflix_toggle_signout",
        )
    )
    markup.add(
        InlineKeyboardButton("✏️ تغيير كلمة المرور الافتراضية", callback_data="netflix_change_default_password")
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
    try:
        user_id = message.from_user.id
        user = get_user_data(user_id)
        if not user or not message.text:
            return
        state = user_states.get(user_id)
        text = message.text.strip()

        if isinstance(state, dict):
            kind = state.get("kind")
            if kind == "temp_manual_email":
                local = text.lower().strip()
                address = local if "@" in local else f"{local}@{TEMPMAIL_DOMAIN}"
                if not re.fullmatch(r"[a-z0-9._-]{3,32}@" + re.escape(TEMPMAIL_DOMAIN), address):
                    bot.send_message(message.chat.id, "❌ اسم الإيميل غير صالح. استخدم أحرفاً إنجليزية وأرقاماً فقط.")
                    return
                item, created = add_managed_email(user_id, address, "manual")
                user_states.pop(user_id, None)
                bot.send_message(
                    message.chat.id,
                    ("✅ تم إنشاء الإيميل" if created else "ℹ️ الإيميل موجود مسبقاً") + "\n\n" + managed_email_summary(item),
                    reply_markup=managed_email_keyboard(item),
                )
                return

            if kind == "temp_change_pin":
                if not re.fullmatch(r"\d{3,12}", text):
                    bot.send_message(message.chat.id, "أرسل رمزاً من 3 إلى 12 رقم.")
                    return
                item = get_managed_email(user_id, state.get("email_id"))
                profile = None
                if item:
                    profile = next(
                        (p for p in item.get("profiles", []) if p.get("number") == state.get("profile_number")),
                        None,
                    )
                if not profile:
                    user_states.pop(user_id, None)
                    bot.send_message(message.chat.id, "❌ لم يتم العثور على البروفايل.", reply_markup=main_keyboard(user_id))
                    return
                with state_lock:
                    profile["pin"] = text
                save_state(BOT_STATE)
                user_states.pop(user_id, None)
                bot.send_message(message.chat.id, "✅ تم تغيير الرمز.", reply_markup=profile_keyboard(item, profile))
                return

        if state == "waiting_for_amount":
            try:
                amount = float(text)
            except ValueError:
                bot.send_message(message.chat.id, "⚠️ يرجى إدخال رقم صحيح.")
                return
            if amount < 1.5:
                bot.send_message(message.chat.id, "⚠️ عذراً، الحد الأدنى للإيداع هو **1.5 دولار**.", parse_mode="Markdown")
                return
            bot.send_message(
                message.chat.id,
                f"⏳ جاري إنشاء محفظة إيداع بمبلغ **{amount}$** عبر شبكة USDT BSC...",
                parse_mode="Markdown",
            )
            try:
                params = {"api_key": user["api_key"], "coin": "usdt", "network": "bsc"}
                response = requests.get(WALLET_URL, params=params, timeout=15)
                response.raise_for_status()
                payload = response.json()
                wallet_address = str(payload.get("wallet_address") or "").strip()
                if not wallet_address:
                    raise RuntimeError("wallet missing")
            except Exception:
                bot.send_message(message.chat.id, "❌ تعذر إنشاء عنوان الإيداع حالياً. حاول مرة أخرى.")
                return
            user_states.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                f"💳 **عنوان المحفظة للإيداع بقيمة {amount}$ (USDT - BSC):**\n\n`{wallet_address}`",
                reply_markup=back_to_main_keyboard(),
                parse_mode="Markdown",
            )
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
                bot.send_message(message.chat.id, "كلمة المرور يجب أن تكون 8 أحرف أو أكثر وتحتوي حرفاً كبيراً وصغيراً ورقماً ورمزاً.")
                return
            with state_lock:
                BOT_STATE["netflix_default_password"] = text
            save_state(BOT_STATE)
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "تم تحديث كلمة المرور الافتراضية ✅", reply_markup=main_keyboard(user_id))
            return

        # A normal text outside a flow should never become a silent dead-end.
        bot.send_message(message.chat.id, "اختر أحد الأزرار من القائمة 👇", reply_markup=main_keyboard(user_id))
    except Exception as exc:
        print(f"text handler failed: {type(exc).__name__}")
        try:
            bot.send_message(message.chat.id, "❌ حدث خطأ مؤقت. افتح /menu وحاول مرة أخرى.")
        except Exception:
            pass




def buy_number_worker(chat_id, user_id, api_key, status_message_id, cancel_event):
    activation_id = None
    phone_number = None
    try:
        while not cancel_event.is_set():
            params = {
                "api_key": api_key,
                "action": "getNumber",
                "service": SERVICE_NETFLIX,
                "country": COUNTRY_IRAQ,
            }
            try:
                response = requests.get(BASE_URL, params=params, timeout=15)
                response.raise_for_status()
                res_text = response.text.strip()
                if res_text.startswith("ACCESS_NUMBER:"):
                    parts = res_text.split(":")
                    if len(parts) >= 3:
                        activation_id, phone_number = parts[1], parts[2]
                        if phone_number.startswith(("96477", "96478")):
                            break
                        cancel_order(api_key, activation_id)
                        activation_id = None
                        phone_number = None
                time.sleep(2.5)
            except Exception:
                time.sleep(3)

        if cancel_event.is_set():
            if activation_id:
                cancel_order(api_key, activation_id)
            safe_edit_message(chat_id, status_message_id, "❌ تم إلغاء البحث عن الرقم.", reply_markup=main_keyboard(user_id))
            return
        if not activation_id or not phone_number:
            safe_edit_message(chat_id, status_message_id, "❌ تعذر الحصول على رقم حالياً.", reply_markup=main_keyboard(user_id))
            return

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
        safe_edit_message(
            chat_id,
            status_message_id,
            msg,
            reply_markup=cancel_markup,
            parse_mode="Markdown",
        )
        threading.Thread(
            target=check_for_sms,
            args=(chat_id, api_key, activation_id, phone_number),
            daemon=True,
        ).start()
    finally:
        with number_search_lock:
            current = number_search_jobs.get(chat_id)
            if current is cancel_event:
                number_search_jobs.pop(chat_id, None)



@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    try:
        if not getattr(call, "message", None):
            safe_answer_callback(call, "تعذر تنفيذ هذا الزر.", True)
            return
        chat_id = call.message.chat.id
        user_id = call.from_user.id
        user = get_user_data(user_id)
        if not user:
            safe_answer_callback(call, "هذا البوت ليس مخصصاً لك!", True)
            return
        api_key = user.get("api_key", "")
        data = str(call.data or "")

        if data == "email_create_menu":
            safe_answer_callback(call)
            safe_edit_message(chat_id, call.message.message_id, "اختر طريقة إنشاء الإيميل:", reply_markup=email_create_keyboard())
            return

        if data == "email_create_random":
            item, _ = add_managed_email(user_id, f"{make_temp_mail_name()}@{TEMPMAIL_DOMAIN}", "random")
            safe_answer_callback(call, "تم إنشاء الإيميل")
            safe_edit_message(
                chat_id,
                call.message.message_id,
                "✅ تم إنشاء إيميل جديد\n\n" + managed_email_summary(item),
                reply_markup=managed_email_keyboard(item),
            )
            return

        if data == "email_create_manual":
            user_states[user_id] = {"kind": "temp_manual_email"}
            safe_answer_callback(call)
            safe_edit_message(
                chat_id,
                call.message.message_id,
                f"✍️ أرسل اسم الإيميل فقط مثل nabil77 وسأضيف @{TEMPMAIL_DOMAIN}\n"
                f"أو أرسل إيميلاً كاملاً على @{TEMPMAIL_DOMAIN}.",
                reply_markup=back_to_main_keyboard(),
            )
            return

        if data in {"emails_list", "emails_sold"}:
            sold_only = data == "emails_sold"
            emails = [
                item for item in temp_user_state(user_id).get("emails", [])
                if (item.get("status") == "completed") == sold_only
            ]
            safe_answer_callback(call)
            markup = InlineKeyboardMarkup(row_width=1)
            for item in emails:
                markup.add(InlineKeyboardButton(item["address"], callback_data=f"email_open:{item['id']}"))
            markup.add(InlineKeyboardButton("🔙 الرئيسية", callback_data="main_menu"))
            title = "الإيميلات المباعة:" if sold_only else "اختر الإيميل:"
            if not emails:
                title = "📭 لا توجد إيميلات هنا حالياً."
            bot.send_message(chat_id, title, reply_markup=markup)
            return

        if data.startswith("email_open:"):
            item = get_managed_email(user_id, data.split(":", 1)[1])
            safe_answer_callback(call)
            if not item:
                bot.send_message(chat_id, "❌ لم يتم العثور على الإيميل.", reply_markup=back_to_main_keyboard())
                return
            with state_lock:
                temp_user_state(user_id)["selected"] = item["id"]
            save_state(BOT_STATE)
            bot.send_message(chat_id, managed_email_summary(item), reply_markup=managed_email_keyboard(item))
            return

        if data.startswith("email_profiles:"):
            item = get_managed_email(user_id, data.split(":", 1)[1])
            safe_answer_callback(call)
            if not item:
                bot.send_message(chat_id, "❌ لم يتم العثور على الإيميل.", reply_markup=back_to_main_keyboard())
                return
            bot.send_message(chat_id, "👥 البروفايلات 1 → 5", reply_markup=profiles_keyboard(item))
            return

        if data.startswith("email_profile:"):
            parts = _parse_email_profile_callback(data, 3)
            safe_answer_callback(call)
            if not parts:
                bot.send_message(chat_id, "❌ بيانات الزر غير صالحة.")
                return
            _, email_id, number_text = parts
            try:
                number = int(number_text)
            except ValueError:
                bot.send_message(chat_id, "❌ رقم البروفايل غير صالح.")
                return
            item = get_managed_email(user_id, email_id)
            profile = next((p for p in item.get("profiles", []) if p.get("number") == number), None) if item else None
            if not profile:
                bot.send_message(chat_id, "❌ لم يتم العثور على البروفايل.")
                return
            status_ar = {"available": "متاح", "review": "قيد المراجعة", "sold": "تم البيع"}.get(profile.get("status"), "متاح")
            bot.send_message(
                chat_id,
                f"{PROFILE_COLORS.get(number, '👤')} البروفايل {number}\n"
                f"🔐 الرمز: {profile['pin']}\n📌 الحالة: {status_ar}\n📧 {item['address']}",
                reply_markup=profile_keyboard(item, profile),
            )
            return

        if data.startswith("email_change_pin:"):
            parts = _parse_email_profile_callback(data, 3)
            safe_answer_callback(call)
            if not parts:
                bot.send_message(chat_id, "❌ بيانات الزر غير صالحة.")
                return
            _, email_id, number_text = parts
            try:
                number = int(number_text)
            except ValueError:
                bot.send_message(chat_id, "❌ رقم البروفايل غير صالح.")
                return
            item = get_managed_email(user_id, email_id)
            if not item or not any(p.get("number") == number for p in item.get("profiles", [])):
                bot.send_message(chat_id, "❌ لم يتم العثور على البروفايل.")
                return
            user_states[user_id] = {"kind": "temp_change_pin", "email_id": email_id, "profile_number": number}
            bot.send_message(chat_id, "✏️ أرسل الرمز الجديد:", reply_markup=back_to_main_keyboard())
            return

        if data.startswith("email_profile_status:"):
            parts = _parse_email_profile_callback(data, 4)
            safe_answer_callback(call)
            if not parts:
                bot.send_message(chat_id, "❌ بيانات الزر غير صالحة.")
                return
            _, email_id, number_text, status = parts
            if status not in {"available", "review", "sold"}:
                bot.send_message(chat_id, "❌ الحالة غير صالحة.")
                return
            try:
                number = int(number_text)
            except ValueError:
                bot.send_message(chat_id, "❌ رقم البروفايل غير صالح.")
                return
            item = get_managed_email(user_id, email_id)
            profile = next((p for p in item.get("profiles", []) if p.get("number") == number), None) if item else None
            if not profile:
                bot.send_message(chat_id, "❌ لم يتم العثور على البروفايل.")
                return
            with state_lock:
                profile["status"] = status
                profile["sold_at"] = time.time() if status == "sold" else None
                item["status"] = "completed" if all(p.get("status") == "sold" for p in item.get("profiles", [])) else "active"
            save_state(BOT_STATE)
            bot.send_message(chat_id, "✅ تم تحديث حالة البروفايل.", reply_markup=profile_keyboard(item, profile))
            return

        if data.startswith("email_delete:"):
            email_id = data.split(":", 1)[1]
            state = temp_user_state(user_id)
            with state_lock:
                before = len(state.get("emails", []))
                state["emails"] = [x for x in state.get("emails", []) if x.get("id") != email_id]
                if state.get("selected") == email_id:
                    state["selected"] = state["emails"][0]["id"] if state["emails"] else None
                deleted = len(state["emails"]) < before
            save_state(BOT_STATE)
            safe_answer_callback(call, "تم الحذف" if deleted else "الإيميل غير موجود")
            bot.send_message(chat_id, "🗑️ تم حذف الإيميل من البوت." if deleted else "❌ لم يتم العثور على الإيميل.", reply_markup=back_to_main_keyboard())
            return

        if data.startswith("show_email:"):
            item = get_managed_email(user_id, data.split(":", 1)[1])
            safe_answer_callback(call)
            if item:
                bot.send_message(chat_id, f"📧 `{item['address']}`", parse_mode="Markdown")
            return

        if data.startswith("email_code_or_link:"):
            item = get_managed_email(user_id, data.split(":", 1)[1])
            safe_answer_callback(call)
            if not item:
                bot.send_message(chat_id, "❌ لم يتم العثور على الإيميل.")
                return
            try:
                messages = fetch_temp_mailbox(item["address"])
            except Exception:
                bot.send_message(chat_id, "❌ تعذر جلب البريد حالياً. حاول مرة أخرى.")
                return
            found = next((("code", m["code"], m) for m in messages if m.get("code")), None)
            if not found:
                for m in messages:
                    link = important_message_url(m.get("urls", []))
                    if link:
                        found = ("url", link, m)
                        break
            if not found:
                bot.send_message(chat_id, "📭 لا يوجد كود أو رابط مهم حالياً.")
                return
            kind, value, msg = found
            discovered = message_discovered_at(item, msg)
            save_state(BOT_STATE)
            markup = InlineKeyboardMarkup()
            if kind == "code":
                markup.add(InlineKeyboardButton(f"📋 نسخ الكود {value}", copy_text=CopyTextButton(text=str(value)[:256])))
                bot.send_message(chat_id, f"🔢 آخر كود: {value}\n⏱️ وصل هذا الكود {relative_time(discovered)}", reply_markup=markup)
            else:
                markup.add(InlineKeyboardButton("🔗 فتح الرابط", url=value))
                bot.send_message(chat_id, f"🔗 الرابط المهم\n⏱️ وصل {relative_time(discovered)}", reply_markup=markup)
            return

        if data.startswith("email_latest:") or data.startswith("email_all_messages:"):
            all_messages = data.startswith("email_all_messages:")
            item = get_managed_email(user_id, data.split(":", 1)[1])
            safe_answer_callback(call)
            if not item:
                bot.send_message(chat_id, "❌ لم يتم العثور على الإيميل.")
                return
            try:
                messages = fetch_temp_mailbox(item["address"])
            except Exception:
                bot.send_message(chat_id, "❌ تعذر جلب الرسائل حالياً. حاول مرة أخرى.")
                return
            messages = messages if all_messages else messages[:5]
            if not messages:
                bot.send_message(chat_id, "📭 لا توجد رسائل حالياً.")
                return
            for index, msg in enumerate(messages, 1):
                discovered = message_discovered_at(item, msg)
                body = msg.get("body") or "(الرسالة بدون نص ظاهر)"
                text = (
                    f"📬 الرسالة {index}\n📧 إلى: {item['address']}\n"
                    f"👤 من: {msg.get('from', 'غير معروف')}\n"
                    f"📝 العنوان: {msg.get('subject', 'بدون عنوان')}\n"
                    f"⏱️ وصلت {relative_time(discovered)}\n━━━━━━━━━━━━━━\n{body[:3200]}"
                )
                keyboard = InlineKeyboardMarkup()
                if msg.get("code"):
                    keyboard.add(InlineKeyboardButton(f"📋 نسخ الكود {msg['code']}", copy_text=CopyTextButton(text=str(msg["code"])[:256])))
                link = important_message_url(msg.get("urls", []))
                if link:
                    keyboard.add(InlineKeyboardButton("🔗 فتح الرابط المهم", url=link))
                bot.send_message(chat_id, text, reply_markup=keyboard)
            if not all_messages:
                more = InlineKeyboardMarkup()
                more.add(InlineKeyboardButton("📚 جلب كل الرسائل والكودات", callback_data=f"email_all_messages:{item['id']}"))
                bot.send_message(chat_id, "لإظهار كل محتوى الصندوق:", reply_markup=more)
            save_state(BOT_STATE)
            return

        if data == "netflix_rotate_password":
            user_states[user_id] = "waiting_for_netflix_email"
            safe_answer_callback(call)
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu"))
            safe_edit_message(chat_id, call.message.message_id, "🔐 أرسل الآن إيميل حساب نتفلكس المسموح بإدارته:", reply_markup=markup)
            return

        if data == "netflix_toggle_signout":
            with state_lock:
                BOT_STATE["netflix_sign_out_all"] = not bool(BOT_STATE.get("netflix_sign_out_all", True))
            save_state(BOT_STATE)
            safe_answer_callback(call, "تم تحديث الخيار")
            try:
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=main_keyboard(user_id))
            except Exception:
                bot.send_message(chat_id, "تم تحديث الخيار ✅", reply_markup=main_keyboard(user_id))
            return

        if data == "netflix_change_default_password":
            user_states[user_id] = "waiting_for_new_default_password"
            safe_answer_callback(call)
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu"))
            safe_edit_message(
                chat_id,
                call.message.message_id,
                "✏️ أرسل كلمة المرور الافتراضية الجديدة الآن:\n\nلن يتم عرضها مجدداً داخل البوت.",
                reply_markup=markup,
            )
            return

        if data == "deposit_bsc":
            user_states[user_id] = "waiting_for_amount"
            safe_answer_callback(call)
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 إلغاء والعودة للقائمة", callback_data="main_menu"))
            safe_edit_message(
                chat_id,
                call.message.message_id,
                "💳 **إيداع الأموال (USDT - BSC):**\nأرسل الآن المبلغ:",
                reply_markup=markup,
                parse_mode="Markdown",
            )
            return

        if data == "main_menu":
            user_states.pop(user_id, None)
            safe_answer_callback(call)
            safe_edit_message(
                chat_id,
                call.message.message_id,
                f"مرحباً بك يا **{user['name']}** في بوت أرقام نتفلكس العراقية 🇮🇶",
                reply_markup=main_keyboard(user_id),
                parse_mode="Markdown",
            )
            return

        if data == "buy_number":
            safe_answer_callback(call, "بدأ البحث عن رقم")
            if not api_key:
                bot.send_message(chat_id, "❌ API Key الخاص بالأرقام غير مضبوط.")
                return
            if chat_id in user_active_orders:
                cancel_order(api_key, user_active_orders[chat_id])
                user_active_orders.pop(chat_id, None)
            with number_search_lock:
                old_job = number_search_jobs.get(chat_id)
                if old_job:
                    old_job.set()
                cancel_event = threading.Event()
                number_search_jobs[chat_id] = cancel_event
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("❌ إلغاء البحث", callback_data="cancel_number_search"))
            status_msg = bot.send_message(chat_id, "⏳ جاري البحث المستمر عن رقم يبدأ بـ 96477 أو 96478...", reply_markup=markup)
            threading.Thread(
                target=buy_number_worker,
                args=(chat_id, user_id, api_key, status_msg.message_id, cancel_event),
                daemon=True,
            ).start()
            return

        if data == "cancel_number_search":
            safe_answer_callback(call, "جاري إلغاء البحث...")
            with number_search_lock:
                event = number_search_jobs.get(chat_id)
                if event:
                    event.set()
            if not event:
                bot.send_message(chat_id, "لا توجد عملية بحث جارية.", reply_markup=main_keyboard(user_id))
            return

        if data.startswith("cancel_num_"):
            activation_id = data.split("cancel_num_", 1)[1]
            safe_answer_callback(call, "جاري إلغاء الرقم...")
            cancel_order(api_key, activation_id)
            if user_active_orders.get(chat_id) == activation_id:
                user_active_orders.pop(chat_id, None)
            bot.send_message(chat_id, "❌ **تم إلغاء الرقم بنجاح واسترداد الرصيد.**", parse_mode="Markdown", reply_markup=main_keyboard(user_id))
            return

        if data == "get_balance":
            _, balance_text = get_current_balance(api_key)
            user_last_balance_texts[user_id] = balance_text
            safe_answer_callback(call, f"رصيدك الحالي هو: {balance_text}", True)
            return

        safe_answer_callback(call, "هذا الزر قديم أو غير معروف. افتح /menu.", True)
    except Exception as exc:
        print(f"callback failed: {type(exc).__name__}")
        safe_answer_callback(call, "حدث خطأ مؤقت. افتح /menu وحاول مرة أخرى.", True)


# ============================================================
# Start
# ============================================================

def run_bot():
    cleanup_expired_emails(run_once=True)
    threading.Thread(target=balance_monitor, daemon=True, name="balance-monitor").start()
    threading.Thread(target=background_temp_mail_monitor, daemon=True, name="mail-monitor").start()
    threading.Thread(target=cleanup_expired_emails, daemon=True, name="email-cleanup").start()
    print("البوت يعمل الآن...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    run_bot()
