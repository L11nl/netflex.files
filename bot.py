import os
import re
import json
import time
import html
import imaplib
import email
import threading
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

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            f"👤 {user['name']} | 💰 الرصيد: {balance_text}",
            callback_data="get_balance",
        ),
        InlineKeyboardButton(
            "📱 شراء رقم عراقي (آسياسيل أو زين العراق)",
            callback_data="buy_number",
        ),
        InlineKeyboardButton("💳 إيداع الأموال (USDT - BSC)", callback_data="deposit_bsc"),
        InlineKeyboardButton("🔐 تغيير رمز النتفلكس", callback_data="netflix_rotate_password"),
        InlineKeyboardButton(
            f"📴 تسجيل الخروج من جميع الأجهزة: {'✅ مفعّل' if sign_out else '❌ معطّل'}",
            callback_data="netflix_toggle_signout",
        ),
        InlineKeyboardButton(
            "✏️ تغيير كلمة المرور الافتراضية",
            callback_data="netflix_change_default_password",
        ),
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

print("البوت يعمل الآن...")
bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
