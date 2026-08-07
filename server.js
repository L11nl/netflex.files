'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const express = require('express');
const cors = require('cors');
const axios = require('axios');
const cheerio = require('cheerio');
const TelegramBot = require('node-telegram-bot-api');

const app = express();
const PORT = Number(process.env.PORT || 3000);
const GENERATOR_BASE = 'https://generator.email';
const GENERATOR_DOMAIN = '5xu.vn';
const TELEGRAM_BOT_TOKEN = String(process.env.TELEGRAM_BOT_TOKEN || '').trim();
const MAIL_READER_BUILD = '2026-08-08-inbox9-cookie-v5-dedup';
const TELEGRAM_ALLOWED_IDS = new Set(
  String(process.env.TELEGRAM_ALLOWED_IDS || '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
);

function resolveBotStateFile() {
  if (process.env.BOT_STATE_FILE) return process.env.BOT_STATE_FILE;
  if (fs.existsSync('/data')) return '/data/bot-state.json';
  return path.join(__dirname, 'bot-state.json');
}

const BOT_STATE_FILE = resolveBotStateFile();

app.disable('x-powered-by');
app.use(cors({ origin: true, methods: ['GET', 'OPTIONS'] }));
app.use(express.json({ limit: '128kb' }));

function normalizeAddress(value = '') {
  return String(value).trim().toLowerCase();
}

function isAllowedEmail(address) {
  return /^[a-z0-9][a-z0-9._-]{0,63}@5xu\.vn$/i.test(address);
}

function normalizeUrl(value = '') {
  const text = String(value).trim().replace(/&amp;/g, '&');
  if (!/^https?:\/\//i.test(text)) return '';
  try {
    return new URL(text).toString();
  } catch {
    return '';
  }
}

function uniqueUrls(values = []) {
  const out = [];
  const seen = new Set();
  for (const value of Array.isArray(values) ? values : []) {
    const url = normalizeUrl(value);
    if (!url) continue;
    const key = url;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(url);
  }
  return out;
}

function urlsInText(text = '') {
  const matches = String(text || '').match(/https?:\/\/[^\s<>'"]+/gi) || [];
  return uniqueUrls(matches.map((raw) => raw.replace(/[),.;"'<>]+$/g, '')));
}

function dedupeUrlsInText(text = '') {
  const seen = new Set();
  const lines = String(text || '').split(/\r?\n/);
  const output = [];

  for (let line of lines) {
    const matches = line.match(/https?:\/\/[^\s<>'"]+/gi) || [];
    for (const raw of matches) {
      const cleaned = raw.replace(/[),.;"'<>]+$/g, '');
      const url = normalizeUrl(cleaned);
      if (!url) continue;
      if (seen.has(url)) {
        line = line.replace(raw, '').replace(/\s{2,}/g, ' ').trim();
      } else {
        seen.add(url);
      }
    }
    if (line.trim()) output.push(line.trim());
  }
  return output.join('\n');
}

function extractCodes(text = '') {
  const out = [];
  const seen = new Set();
  const re = /(?:^|\D)(\d{4})(?!\d)/g;
  let match;
  while ((match = re.exec(String(text))) !== null) {
    const code = match[1];
    if (!seen.has(code)) {
      seen.add(code);
      out.push(code);
    }
  }
  return out;
}

function stableId(sender, subject, body) {
  // لا نستخدم ترتيب الرسالة داخل الصفحة حتى لا يتغير المعرّف عند وصول رسالة جديدة.
  return 'gen-' + crypto
    .createHash('sha1')
    .update(`${sender}|${subject}|${body}`)
    .digest('hex')
    .slice(0, 20);
}

function parseSender(raw = '') {
  const text = String(raw).trim();
  const angle = text.match(/^(.*?)\s*<([^<>\s]+@[^<>\s]+)>$/);
  if (angle) return { name: angle[1].trim(), address: angle[2].trim() };
  const emailMatch = text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
  return {
    name: emailMatch ? text.replace(emailMatch[0], '').replace(/[<>]/g, '').trim() : text,
    address: emailMatch ? emailMatch[0] : ''
  };
}

function cleanHeaderLikeText(value = '', headerWords = []) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  const lower = text.toLowerCase();
  if (!text) return '';
  if (headerWords.some((word) => lower === String(word).toLowerCase())) return '';
  return text;
}

function extractTimestamp(text = '') {
  const value = String(text || '');
  const match = value.match(/\b(20\d{2})[-\/](\d{1,2})[-\/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\b/);
  if (!match) return '';
  const [, y, m, d, hh, mm, ss = '00'] = match;
  const iso = new Date(`${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}T${String(hh).padStart(2,'0')}:${mm}:${ss}Z`);
  return Number.isNaN(iso.getTime()) ? '' : iso.toISOString();
}

function extractBodyFromNode($, node) {
  if (!node) return { text: '', htmlBody: '', links: [] };
  const body = $(node).clone();
  const links = [];
  const htmlBody = body.html() || '';

  body.find('iframe').each((_, el) => {
    const embedded = $(el).attr('srcdoc') || $(el).attr('data-srcdoc') || $(el).attr('data-content') || '';
    if (embedded) $(el).replaceWith(`\n${embedded}\n`);
  });

  body.find('a[href]').each((_, el) => {
    const href = normalizeUrl($(el).attr('href') || '');
    if (!href) return;
    if (!links.includes(href)) links.push(href);
    const title = $(el).text().trim();
    $(el).replaceWith(`\n${title ? `${title}\n` : ''}${href}\n`);
  });

  let text = body.text()
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .join('\n');

  if (!text && htmlBody) {
    text = cheerio.load(htmlBody).root().text()
      .split(/\r?\n/).map((line) => line.trim()).filter(Boolean).join('\n');
  }

  const plainUrls = `${text}\n${htmlBody}`.match(/https?:\/\/[^\s<>'"]+/gi) || [];
  for (const raw of plainUrls) {
    const url = normalizeUrl(raw.replace(/[),.;"'<>]+$/g, ''));
    if (url && !links.includes(url)) links.push(url);
  }

  if (!text && links.length) text = links.join('\n');
  return { text, htmlBody, links };
}

function decodeBasicEntities(value = '') {
  return String(value || '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function getFullMessageLikePython($, node) {
  if (!node) return { text: '', htmlBody: '', links: [] };

  // نفس فكرة get_full_message في كود Python: ننسخ جسم الرسالة،
  // ثم نستبدل كل <a> باسم الرابط + الرابط الحقيقي قبل استخراج النص.
  const body = $(node).clone();
  const links = [];
  const htmlBody = body.html() || '';

  body.find('a[href]').each((_, el) => {
    const href = normalizeUrl(decodeBasicEntities($(el).attr('href') || ''));
    const title = $(el).text().replace(/\s+/g, ' ').trim();
    if (href) {
      const isNew = !links.includes(href);
      if (isNew) links.push(href);
      // الرابط نفسه يظهر مرة واحدة فقط حتى لو تكرر داخل HTML الرسالة.
      $(el).replaceWith(`\n${title ? `${title}\n` : ''}${isNew ? `${href}\n` : ''}`);
    }
  });

  // بعض الرسائل تكون داخل iframe/srcdoc في HTML المصدر.
  body.find('iframe').each((_, el) => {
    const embedded = $(el).attr('srcdoc') || $(el).attr('data-srcdoc') || $(el).attr('data-content') || '';
    if (embedded) {
      const inner = cheerio.load(decodeBasicEntities(embedded));
      inner('a[href]').each((__, a) => {
        const href = normalizeUrl(decodeBasicEntities(inner(a).attr('href') || ''));
        const title = inner(a).text().replace(/\s+/g, ' ').trim();
        if (!href) return;
        const isNew = !links.includes(href);
        if (isNew) links.push(href);
        inner(a).replaceWith(`\n${title ? `${title}\n` : ''}${isNew ? `${href}\n` : ''}`);
      });
      // لا نلحق قائمة links العامة هنا؛ كانت تسبب تكرار الروابط مع كل iframe.
      $(el).replaceWith(`\n${inner.root().text()}\n`);
    }
  });

  const lines = body.text()
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, ' ').trim())
    .filter(Boolean);

  let text = lines.join('\n');

  // التقط الروابط المكتوبة كنص أو الموجودة داخل HTML حتى لو لم تكن داخل <a>.
  const plainUrls = `${text}\n${htmlBody}`.match(/https?:\/\/[^\s<>'"]+/gi) || [];
  for (const raw of plainUrls) {
    const url = normalizeUrl(decodeBasicEntities(raw.replace(/[),.;"'<>]+$/g, '')));
    if (url && !links.includes(url)) links.push(url);
  }

  text = dedupeUrlsInText(text);
  const dedupedLinks = uniqueUrls(links);
  if (!text && dedupedLinks.length) text = dedupedLinks.join('\n');
  return { text, htmlBody, links: dedupedLinks };
}

function normalizeGeneratorColumns($, root) {
  let senders = root.find('.from_div_45g45gg').toArray();
  let subjects = root.find('.subj_div_45g45gg').toArray();
  let bodies = root.find('.mess_bodiyy').toArray();

  // Generator.email يضع أحياناً خلايا العناوين From / Subject داخل نفس الكلاسات.
  // نزيلها من بداية الأعمدة قبل الربط حتى لا تنزاح الرسالة الحقيقية عن جسمها.
  while (senders.length) {
    const v = $(senders[0]).text().replace(/\s+/g, ' ').trim().toLowerCase();
    if (['from', 'المرسل', 'من'].includes(v)) senders.shift(); else break;
  }
  while (subjects.length) {
    const v = $(subjects[0]).text().replace(/\s+/g, ' ').trim().toLowerCase();
    if (['subject', 'العنوان', 'الموضوع'].includes(v)) subjects.shift(); else break;
  }

  // أبقِ فقط أجسام الرسائل التي تحتوي شيئاً حقيقياً.
  bodies = bodies.filter((node) => {
    const data = getFullMessageLikePython($, node);
    return Boolean(data.text || data.htmlBody || data.links.length);
  });

  return { senders, subjects, bodies };
}

function parseMailbox(html, email) {
  const $ = cheerio.load(String(html || ''));
  const table = $('#email-table');
  if (!table.length) return [];

  // نستخدم نفس الأعمدة الموجودة في كود Python المرسل من المستخدم.
  const { senders, subjects, bodies } = normalizeGeneratorColumns($, table);
  const messages = [];
  const count = Math.max(senders.length, subjects.length, bodies.length);

  for (let i = 0; i < count; i += 1) {
    const senderRaw = senders[i] ? $(senders[i]).text().replace(/\s+/g, ' ').trim() : '';
    const subjectRaw = subjects[i] ? $(subjects[i]).text().replace(/\s+/g, ' ').trim() : '';
    const bodyData = getFullMessageLikePython($, bodies[i] || null);

    let senderText = senderRaw || 'غير معروف';
    let subject = subjectRaw || 'بدون عنوان';
    let text = bodyData.text;
    const htmlBody = bodyData.htmlBody;
    let links = uniqueUrls(bodyData.links);

    // إذا كانت بنية الصفحة مختلفة قليلاً، نقرأ بيانات To/From/Subject/Received من أقرب حاوية.
    const anchorNode = bodies[i] || senders[i] || subjects[i] || null;
    let container = anchorNode ? $(anchorNode) : null;
    if (container?.length) {
      // اصعد حتى حاوية صغيرة تحتوي بيانات الرسالة ولا تبتلع كل جدول البريد.
      let cursor = container;
      for (let depth = 0; depth < 6 && cursor.length; depth += 1) {
        const t = cursor.text().replace(/\s+/g, ' ').trim();
        if (/\bTo:\s*/i.test(t) || (/\bFrom\b/i.test(t) && /\bSubject\b/i.test(t))) {
          container = cursor;
          break;
        }
        cursor = cursor.parent();
      }
    }

    const containerText = container?.length ? container.text().replace(/\s+/g, ' ').trim() : '';
    const containerHtml = container?.length ? (container.html() || '') : '';

    const toMatch = containerText.match(/\bTo:\s*([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/i);
    const fromMatch = containerText.match(/\bFrom:\s*(.*?)(?=\s+(?:Subject:|Time:|Received:|To:|Delete Message|View source)|$)/i);
    const subjectMatch = containerText.match(/\bSubject:\s*(.*?)(?=\s+(?:Time:|Received:|To:|From:|Delete Message|View source)|$)/i);
    const receivedMatch = containerText.match(/(?:\bReceived:|\bTime:)\s*(20\d{2}[-\/]\d{1,2}[-\/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)/i);

    if ((!senderRaw || /^from$/i.test(senderRaw)) && fromMatch?.[1]) senderText = fromMatch[1].trim();
    if ((!subjectRaw || /^subject$/i.test(subjectRaw)) && subjectMatch?.[1]) subject = subjectMatch[1].trim();

    // لو body div لم يحتوِ النص كاملًا، استخرج النص من الحاوية مع حذف حقول التحكم.
    if (!text && containerText) {
      let fallback = containerText
        .replace(/\bTo:\s*[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig, '')
        .replace(/\bFrom:\s*.*?(?=\s+Subject:|\s+Time:|\s+Received:|\s+To:|$)/ig, '')
        .replace(/\bSubject:\s*.*?(?=\s+Time:|\s+Received:|\s+To:|\s+From:|$)/ig, '')
        .replace(/(?:\bReceived:|\bTime:)\s*20\d{2}[-\/]\d{1,2}[-\/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?/ig, '')
        .replace(/\b(Delete Message|View source|From|Subject|Time(?:\s*\(UTC\))?)\b/gi, '')
        .replace(/\s+/g, ' ')
        .trim();
      if (fallback) text = fallback;
    }

    const allUrls = `${text}\n${htmlBody}\n${containerHtml}`.match(/https?:\/\/[^\s<>'"]+/gi) || [];
    for (const raw of allUrls) {
      const url = normalizeUrl(decodeBasicEntities(raw.replace(/[),.;"'<>]+$/g, '')));
      if (url && !links.includes(url)) links.push(url);
    }
    links = uniqueUrls(links);
    text = dedupeUrlsInText(text);
    if (!text && links.length) text = links.join('\n');

    const normalizedSender = String(senderText || '').trim();
    const normalizedSubject = String(subject || '').trim();

    // لا نرسل صف عناوين الجدول كأنه رسالة.
    if (/^from$/i.test(normalizedSender) && /^subject$/i.test(normalizedSubject) && !text && !links.length) continue;
    if (!normalizedSender && !normalizedSubject && !text && !links.length) continue;

    const sender = parseSender(normalizedSender || 'غير معروف');
    const createdAt = receivedMatch?.[1]
      ? (extractTimestamp(receivedMatch[1]) || new Date().toISOString())
      : (extractTimestamp(containerText) || '');
    const codes = extractCodes(`${normalizedSubject}\n${text}\n${links.join('\n')}`);
    const toAddress = normalizeAddress(toMatch?.[1] || email);

    // استخدم بصمة مستقرة لا تعتمد على وقت الفحص حتى لا تتكرر الرسالة كل دورة.
    const idSeed = [
      sender.address || sender.name || normalizedSender,
      normalizedSubject,
      receivedMatch?.[1] || '',
      text,
      links.join('|')
    ].join('|');

    messages.push({
      id: 'gen-' + crypto.createHash('sha1').update(idSeed).digest('hex').slice(0, 24),
      to: toAddress || normalizeAddress(email),
      from: sender,
      subject: normalizedSubject || 'بدون عنوان',
      intro: text.slice(0, 300),
      text,
      html: htmlBody,
      createdAt: createdAt || '',
      receivedAt: createdAt || '',
      seen: false,
      verifications: codes,
      codes,
      links,
      _generator: true
    });
  }

  // الأحدث أولاً كما يظهر في Generator.email.
  return [...new Map(messages.map((message) => [message.id, message])).values()];
}

function storeSetCookies(jar, setCookieHeaders = []) {
  const values = Array.isArray(setCookieHeaders) ? setCookieHeaders : [setCookieHeaders].filter(Boolean);
  for (const line of values) {
    const pair = String(line || '').split(';', 1)[0];
    const eq = pair.indexOf('=');
    if (eq <= 0) continue;
    const name = pair.slice(0, eq).trim();
    const value = pair.slice(eq + 1).trim();
    if (name) jar.set(name, value);
  }
}

function cookieHeader(jar) {
  return Array.from(jar.entries()).map(([name, value]) => `${name}=${value}`).join('; ');
}

async function requestGeneratorWithCookies(url, jar, baseHeaders) {
  let current = url;
  for (let hop = 0; hop < 10; hop += 1) {
    const cookies = cookieHeader(jar);
    const response = await axios.get(current, {
      headers: { ...baseHeaders, ...(cookies ? { Cookie: cookies } : {}) },
      timeout: 20000,
      responseType: 'text',
      maxRedirects: 0,
      validateStatus: (status) => status >= 200 && status < 400
    });

    storeSetCookies(jar, response.headers?.['set-cookie']);

    if (response.status >= 300 && response.status < 400 && response.headers?.location) {
      current = new URL(response.headers.location, current).toString();
      continue;
    }

    return {
      html: String(response.data || ''),
      finalUrl: current,
      status: response.status
    };
  }
  throw new Error('Generator.email أعاد تحويلات كثيرة جداً أثناء فتح الصندوق.');
}

async function fetchMailboxHtml(email) {
  const address = normalizeAddress(email);
  const [username, domain] = address.split('@');
  const inboxUrl = `${GENERATOR_BASE}/inbox9/${address}`;
  const bootstrapUrl = `${GENERATOR_BASE}/${domain}/${username}`;

  const headers = {
    // نفس User-Agent الذي في كود Python الذي أثبت أنه يعمل.
    'User-Agent': 'Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 Chrome/136.0 Mobile Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache'
  };

  // requests في Python يحتفظ بالكوكيز أثناء التحويلات تلقائياً.
  // Axios لا يفعل ذلك بنفس الصورة، لذلك نطبّق jar صغيراً يدوياً ثم نفتح inbox9 نفسه.
  const jar = new Map();
  const candidates = [];
  let lastError = null;

  try {
    const boot = await requestGeneratorWithCookies(bootstrapUrl, jar, headers);
    if (boot.html) candidates.push({ ...boot, source: bootstrapUrl, kind: 'bootstrap' });
  } catch (error) {
    lastError = error;
  }

  try {
    // هذا هو الرابط الذي طلبه المستخدم حرفياً؛ يتم فتحه على السيرفر وبنفس جلسة الكوكيز.
    const inbox = await requestGeneratorWithCookies(inboxUrl, jar, {
      ...headers,
      Referer: bootstrapUrl
    });
    if (inbox.html) candidates.push({ ...inbox, source: inboxUrl, kind: 'inbox9' });
  } catch (error) {
    lastError = error;
  }

  // اختر النسخة التي تحتوي أكبر عدد رسائل حقيقية. عند التعادل نفضّل inbox9.
  let best = null;
  for (const candidate of candidates) {
    const $ = cheerio.load(candidate.html);
    if (!$('#email-table').length) continue;
    const count = parseMailbox(candidate.html, address).length;
    const score = count * 100 + (candidate.kind === 'inbox9' ? 10 : 0);
    if (!best || score > best.score) best = { ...candidate, score, count };
  }

  if (best) {
    return { html: best.html, source: inboxUrl, finalUrl: best.finalUrl, messageCount: best.count };
  }

  if (candidates.length) {
    const error = new Error('تم فتح Generator.email لكن لم يظهر جدول email-table في المصدر الذي أعاده السيرفر.');
    error.statusCode = 502;
    throw error;
  }

  throw lastError || new Error('تعذر فتح صندوق البريد من Generator.email.');
}

async function getMailbox(email) {
  const address = normalizeAddress(email);
  if (!isAllowedEmail(address)) {
    const error = new Error('عنوان البريد غير صالح أو لا يستخدم @5xu.vn.');
    error.statusCode = 400;
    throw error;
  }
  const { html, source, finalUrl } = await fetchMailboxHtml(address);
  return {
    email: address,
    source,
    finalUrl,
    messages: parseMailbox(html, address)
  };
}

// =========================
// Telegram bot — مدير كامل مثل الموقع
// =========================

const EMAIL_LIFETIME_MS = 6 * 24 * 60 * 60 * 1000;
const AUTO_CHECK_INTERVAL_MS = Math.max(3000, Number(process.env.AUTO_CHECK_INTERVAL_MS || 3000));
const DEFAULT_PINS = ['1212', '1001', '2121', '2026', '2002'];

function randomGeneratorUsername() {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  for (let tries = 0; tries < 50; tries += 1) {
    const length = 5 + crypto.randomInt(0, 2); // 5 أو 6 خانات
    let value = '';
    for (let i = 0; i < length; i += 1) {
      value += chars[crypto.randomInt(0, chars.length)];
    }
    if (/[a-z]/.test(value) && /\d/.test(value)) return value;
  }
  return `a${String(Date.now()).slice(-4)}`;
}

function newGeneratorEmail() {
  return `${randomGeneratorUsername()}@${GENERATOR_DOMAIN}`;
}

function createBotProfile(number) {
  return {
    number,
    pin: DEFAULT_PINS[number - 1],
    status: 'available', // available | review | sold
    statusChangedAt: new Date().toISOString(),
    reservedAt: null,
    soldAt: null
  };
}

function makeBotEmailRecord(address, mode = 'random') {
  const now = Date.now();
  return {
    id: crypto.randomUUID ? crypto.randomUUID() : `mail-${now}-${Math.random().toString(16).slice(2)}`,
    address: normalizeAddress(address),
    localName: '',
    mode,
    createdAt: new Date(now).toISOString(),
    expiresAt: new Date(now + EMAIL_LIFETIME_MS).toISOString(),
    status: 'active', // active | completed
    completedAt: null,
    profiles: [1, 2, 3, 4, 5].map(createBotProfile),
    seenMessageIds: [],
    lastCheckedAt: null
  };
}

function normalizeBotState(raw) {
  const root = raw && typeof raw === 'object' ? raw : {};
  const chats = root.chats && typeof root.chats === 'object' ? root.chats : {};
  for (const [chatId, chat] of Object.entries(chats)) {
    chat.emails = Array.isArray(chat.emails) ? chat.emails : [];
    chat.pending = chat.pending && typeof chat.pending === 'object' ? chat.pending : null;
    chat.selectedEmailId = chat.selectedEmailId || chat.emails[0]?.id || null;
    chat.emails = chat.emails.map((email) => {
      const createdAt = email.createdAt || new Date().toISOString();
      const profiles = Array.isArray(email.profiles) ? email.profiles : [];
      return {
        ...email,
        id: email.id || (crypto.randomUUID ? crypto.randomUUID() : `mail-${Date.now()}-${Math.random()}`),
        address: normalizeAddress(email.address || ''),
        localName: email.localName || '',
        mode: email.mode || 'random',
        createdAt,
        expiresAt: email.expiresAt || new Date(new Date(createdAt).getTime() + EMAIL_LIFETIME_MS).toISOString(),
        status: email.status === 'completed' ? 'completed' : 'active',
        completedAt: email.completedAt || null,
        profiles: [1, 2, 3, 4, 5].map((number) => {
          const old = profiles.find((item) => Number(item.number) === number) || {};
          return { ...createBotProfile(number), ...old, number };
        }),
        seenMessageIds: Array.isArray(email.seenMessageIds) ? email.seenMessageIds : [],
        lastCheckedAt: email.lastCheckedAt || null
      };
    }).filter((email) => isAllowedEmail(email.address));
    chats[chatId] = chat;
  }
  return { chats };
}

function loadBotState() {
  try {
    if (!fs.existsSync(BOT_STATE_FILE)) return { chats: {} };
    const parsed = JSON.parse(fs.readFileSync(BOT_STATE_FILE, 'utf8'));
    return normalizeBotState(parsed);
  } catch (error) {
    console.warn('[telegram] could not load bot state:', error.message);
    return { chats: {} };
  }
}

const botState = loadBotState();

function saveBotState() {
  try {
    fs.mkdirSync(path.dirname(BOT_STATE_FILE), { recursive: true });
    const temp = `${BOT_STATE_FILE}.tmp`;
    fs.writeFileSync(temp, JSON.stringify(botState, null, 2));
    fs.renameSync(temp, BOT_STATE_FILE);
  } catch (error) {
    console.warn('[telegram] could not save bot state:', error.message);
  }
}

function chatKey(chatId) {
  return String(chatId);
}

function getBotChat(chatId, create = true) {
  const key = chatKey(chatId);
  if (!botState.chats[key] && create) {
    botState.chats[key] = { emails: [], selectedEmailId: null, pending: null, createdAt: new Date().toISOString() };
    saveBotState();
  }
  return botState.chats[key] || null;
}

function getBotEmail(chatId, emailId = '') {
  const chat = getBotChat(chatId, false);
  if (!chat) return null;
  const id = emailId || chat.selectedEmailId;
  return chat.emails.find((email) => email.id === id) || null;
}

function setSelectedBotEmail(chatId, emailId) {
  const chat = getBotChat(chatId);
  if (!chat.emails.some((email) => email.id === emailId)) return null;
  chat.selectedEmailId = emailId;
  saveBotState();
  return getBotEmail(chatId, emailId);
}

function addBotEmail(chatId, address, mode = 'random') {
  const chat = getBotChat(chatId);
  const normalized = normalizeAddress(address);
  if (!isAllowedEmail(normalized)) throw new Error('الإيميل يجب أن يكون على النطاق @5xu.vn.');
  const existing = chat.emails.find((email) => email.address === normalized);
  if (existing) {
    chat.selectedEmailId = existing.id;
    saveBotState();
    return { email: existing, created: false };
  }
  const email = makeBotEmailRecord(normalized, mode);
  chat.emails.unshift(email);
  chat.selectedEmailId = email.id;
  saveBotState();
  return { email, created: true };
}

function removeBotEmail(chatId, emailId) {
  const chat = getBotChat(chatId, false);
  if (!chat) return null;
  const index = chat.emails.findIndex((email) => email.id === emailId);
  if (index < 0) return null;
  const [removed] = chat.emails.splice(index, 1);
  if (chat.selectedEmailId === emailId) chat.selectedEmailId = chat.emails[0]?.id || null;
  saveBotState();
  return removed;
}

function isTelegramChatAllowed(chatId) {
  return TELEGRAM_ALLOWED_IDS.size === 0 || TELEGRAM_ALLOWED_IDS.has(String(chatId));
}

function publicWebsiteUrl() {
  const explicit = String(process.env.PUBLIC_URL || '').trim().replace(/\/$/, '');
  if (explicit) return explicit;
  const railwayDomain = String(process.env.RAILWAY_PUBLIC_DOMAIN || '').trim();
  if (railwayDomain) return `https://${railwayDomain}`;
  return '';
}

function telegramKeyboard() {
  return {
    keyboard: [
      [{ text: '➕ إنشاء إيميل' }, { text: '📧 الإيميلات' }],
      [{ text: '✅ الإيميلات المباعة' }, { text: '🌐 فتح الموقع' }]
    ],
    resize_keyboard: true,
    is_persistent: true
  };
}

function createEmailKeyboard() {
  return {
    inline_keyboard: [
      [{ text: '🎲 إيميل عشوائي', callback_data: 'create:random' }],
      [{ text: '✍️ إيميل يدوي', callback_data: 'create:manual' }],
      [{ text: '✖️ إلغاء', callback_data: 'noop' }]
    ]
  };
}

function emailAgeDays(email) {
  const ms = Date.now() - new Date(email.createdAt).getTime();
  return Math.max(0, Math.floor(ms / 86400000));
}

function emailRemainingText(email) {
  const left = new Date(email.expiresAt).getTime() - Date.now();
  if (left <= 0) return 'منتهي';
  const hours = Math.ceil(left / 3600000);
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  if (days > 0) return `${days} يوم${remHours ? ` و ${remHours} ساعة` : ''}`;
  return `${hours} ساعة`;
}

function profileStatusLabel(status) {
  return status === 'sold' ? 'مباع' : status === 'review' ? 'قيد المراجعة' : 'متاح';
}

function profileStatusEmoji(status) {
  return status === 'sold' ? '🔴' : status === 'review' ? '🟡' : '🟢';
}

function emailSummaryText(email) {
  const counts = { available: 0, review: 0, sold: 0 };
  email.profiles.forEach((p) => { counts[p.status] = (counts[p.status] || 0) + 1; });
  return [
    `📧 ${email.address}`,
    email.localName ? `🏷️ ${email.localName}` : '',
    `🗓️ تم الإنشاء قبل ${emailAgeDays(email)} يوم`,
    `⏳ الحذف من البوت بعد: ${emailRemainingText(email)}`,
    `👥 البروفايلات: 🟢 ${counts.available}  🟡 ${counts.review}  🔴 ${counts.sold}`,
    `📌 الحالة: ${email.status === 'completed' ? 'مكتمل/مباع' : 'نشط'}`,
    '🔔 الرسائل: تصل تلقائياً بدون زر فحص'
  ].filter(Boolean).join('\n');
}

function emailDetailKeyboard(email) {
  const rows = [
    [{ text: '📋 نسخ الإيميل', copy_text: { text: email.address } }],
    [{ text: '👥 البروفايلات', callback_data: `profiles:${email.id}` }],
    [{ text: '✏️ اسم محلي', callback_data: `rename:${email.id}` }, { text: '🗑️ حذف الآن', callback_data: `delete:${email.id}` }],
    [{ text: '⬅️ الإيميلات', callback_data: 'emails:active' }]
  ];
  return { inline_keyboard: rows };
}

function profilesKeyboard(email) {
  const rows = email.profiles.map((profile) => ([{
    text: `${profileStatusEmoji(profile.status)} بروفايل ${profile.number} · ${profile.pin}`,
    callback_data: `profile:${email.id}:${profile.number}`
  }]));
  rows.push([{ text: '⬅️ رجوع للإيميل', callback_data: `email:${email.id}` }]);
  return { inline_keyboard: rows };
}

function profileDetailKeyboard(email, profile) {
  const rows = [
    [{ text: `📋 نسخ الرمز ${profile.pin}`, copy_text: { text: String(profile.pin) } }],
    [{ text: '📋 نسخ الإيميل', copy_text: { text: email.address } }],
    [{ text: '✏️ تغيير الرمز', callback_data: `pin:${email.id}:${profile.number}` }]
  ];
  if (profile.status === 'available') {
    rows.push([
      { text: '🟡 قيد المراجعة', callback_data: `pstatus:${email.id}:${profile.number}:review` },
      { text: '✅ تم البيع', callback_data: `pstatus:${email.id}:${profile.number}:sold` }
    ]);
  } else if (profile.status === 'review') {
    rows.push([
      { text: '✅ تم البيع', callback_data: `pstatus:${email.id}:${profile.number}:sold` },
      { text: '↩️ إلغاء المراجعة', callback_data: `pstatus:${email.id}:${profile.number}:available` }
    ]);
  } else {
    rows.push([{ text: '♻️ استرجاع البروفايل', callback_data: `pstatus:${email.id}:${profile.number}:available` }]);
  }
  rows.push([{ text: '⬅️ البروفايلات', callback_data: `profiles:${email.id}` }]);
  return { inline_keyboard: rows };
}

function messageInlineKeyboard(message) {
  const rows = [];
  const codes = Array.isArray(message.codes) ? message.codes.slice(0, 5) : [];
  const links = uniqueUrls(message.links).slice(0, 6);
  for (const code of codes) rows.push([{ text: `📋 نسخ ${code}`, copy_text: { text: String(code) } }]);
  links.forEach((url, index) => rows.push([{ text: `🔗 فتح الرابط ${index + 1}`, url }]));
  return rows.length ? { inline_keyboard: rows } : undefined;
}

function formatTelegramMessage(message, index = 0, emailAddress = '') {
  const senderName = String(message?.from?.name || '').trim();
  const senderAddress = String(message?.from?.address || '').trim();
  const sender = senderAddress
    ? (senderName && senderName !== senderAddress ? `${senderName} <${senderAddress}>` : senderAddress)
    : (senderName || 'غير معروف');
  const subject = message?.subject || 'بدون عنوان';
  const body = dedupeUrlsInText(String(message?.text || message?.intro || '').trim());
  const received = message?.receivedAt || message?.createdAt || '';
  const codes = Array.isArray(message?.codes) && message.codes.length ? `\n\n🔢 الأكواد: ${message.codes.join(' - ')}` : '';
  const bodyUrls = new Set(urlsInText(body));
  const extraLinks = uniqueUrls(message?.links).filter((url) => !bodyUrls.has(url)).slice(0, 10);
  // لا نعيد طباعة الرابط في قسم الروابط إذا كان موجوداً أصلاً داخل نص الرسالة.
  const links = extraLinks.length ? `\n\n🔗 الروابط:\n${extraLinks.join('\n')}` : '';
  return [
    index >= 0 ? `📬 الرسالة ${index + 1}` : '🔔 وصلت رسالة جديدة',
    `📧 إلى: ${message?.to || emailAddress || ''}`,
    `👤 من: ${sender}`,
    `📝 العنوان: ${subject}`,
    received ? `🕒 وقت الوصول: ${received}` : '',
    '',
    '━━━━━━━━━━━━━━',
    '',
    body || (uniqueUrls(message?.links).length ? uniqueUrls(message?.links).join('\n') : '(لا يوجد نص ظاهر؛ تم عرض جميع البيانات التي أمكن استخراجها)'),
    codes,
    links
  ].filter((x) => x !== '').join('\n');
}

function splitTelegramText(text, maxLength = 3900) {
  const parts = [];
  let remaining = String(text || '').trim();
  while (remaining) {
    if (remaining.length <= maxLength) { parts.push(remaining); break; }
    let cut = remaining.lastIndexOf('\n', maxLength);
    if (cut < maxLength * 0.5) cut = maxLength;
    parts.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut).trim();
  }
  return parts;
}

async function safeSendMessage(bot, chatId, text, options = {}) {
  try {
    return await bot.sendMessage(chatId, text, options);
  } catch (error) {
    // بعض إصدارات تيليجرام قد لا تقبل copy_text، نحذف الـ markup كحل احتياطي.
    if (options.reply_markup) {
      try { return await bot.sendMessage(chatId, text, { ...options, reply_markup: undefined }); } catch (_) {}
    }
    throw error;
  }
}

async function sendFormattedMessage(bot, chatId, message, index, emailAddress) {
  const chunks = splitTelegramText(formatTelegramMessage(message, index, emailAddress));
  for (let partIndex = 0; partIndex < chunks.length; partIndex += 1) {
    const isLast = partIndex === chunks.length - 1;
    await safeSendMessage(bot, chatId, chunks[partIndex], {
      disable_web_page_preview: true,
      ...(isLast && messageInlineKeyboard(message) ? { reply_markup: messageInlineKeyboard(message) } : {})
    });
  }
}

async function establishMailboxBaseline(email) {
  try {
    const { messages } = await getMailbox(email.address);
    email.seenMessageIds = messages.map((m) => m.id).slice(0, 100);
    email.lastCheckedAt = new Date().toISOString();
    saveBotState();
  } catch (_) {
    // إذا فشل أول فحص، يبقى الصندوق مراقَب وسيتم الالتقاط في الدورة القادمة.
  }
}

async function sendMailboxToTelegram(bot, chatId, emailId = '') {
  const email = getBotEmail(chatId, emailId);
  if (!email) {
    await bot.sendMessage(chatId, 'لا يوجد إيميل محدد. أنشئ إيميل أولاً.', { reply_markup: telegramKeyboard() });
    return;
  }
  const wait = await bot.sendMessage(chatId, `🔄 جاري فحص الرسائل...\n\n📧 ${email.address}`);
  try {
    const { messages } = await getMailbox(email.address);
    email.lastCheckedAt = new Date().toISOString();
    messages.forEach((m) => {
      if (!email.seenMessageIds.includes(m.id)) email.seenMessageIds.push(m.id);
    });
    email.seenMessageIds = email.seenMessageIds.slice(-200);
    saveBotState();
    if (!messages.length) {
      await bot.editMessageText(`📭 لا توجد رسائل حالياً\n\n📧 ${email.address}`, { chat_id: chatId, message_id: wait.message_id });
      return;
    }
    try { await bot.deleteMessage(chatId, wait.message_id); } catch (_) {}
    const newest = messages.slice(0, 5);
    for (let i = 0; i < newest.length; i += 1) await sendFormattedMessage(bot, chatId, newest[i], i, email.address);
  } catch (error) {
    const text = `❌ تعذر جلب الرسائل حالياً.\n\n${String(error?.message || error)}`;
    try { await bot.editMessageText(text, { chat_id: chatId, message_id: wait.message_id }); }
    catch (_) { await bot.sendMessage(chatId, text); }
  }
}

function activeBotEmails(chat) {
  return chat.emails.filter((email) => email.status !== 'completed');
}

function completedBotEmails(chat) {
  return chat.emails.filter((email) => email.status === 'completed');
}

async function showEmailList(bot, chatId, completed = false) {
  const chat = getBotChat(chatId);
  const items = completed ? completedBotEmails(chat) : activeBotEmails(chat);
  if (!items.length) {
    await bot.sendMessage(chatId, completed ? '✅ لا توجد إيميلات مباعة حالياً.' : '📭 لا توجد إيميلات نشطة. اضغط «➕ إنشاء إيميل».', { reply_markup: telegramKeyboard() });
    return;
  }
  const rows = items.slice(0, 50).map((email) => [{
    text: `${email.status === 'completed' ? '✅' : '📧'} ${email.address} · ${emailRemainingText(email)}`,
    callback_data: `email:${email.id}`
  }]);
  await bot.sendMessage(chatId, completed ? '✅ الإيميلات المباعة:' : '📧 الإيميلات:', { reply_markup: { inline_keyboard: rows } });
}

async function showEmailDetail(bot, chatId, emailId, editMessageId = null) {
  const email = setSelectedBotEmail(chatId, emailId);
  if (!email) return;
  const text = emailSummaryText(email);
  const options = { reply_markup: emailDetailKeyboard(email), disable_web_page_preview: true };
  if (editMessageId) {
    try { await bot.editMessageText(text, { chat_id: chatId, message_id: editMessageId, ...options }); return; } catch (_) {}
  }
  await safeSendMessage(bot, chatId, text, options);
}

async function showProfiles(bot, chatId, emailId, editMessageId = null) {
  const email = getBotEmail(chatId, emailId);
  if (!email) return;
  const text = [
    `👥 بروفايلات ${email.address}`,
    '',
    ...email.profiles.map((p) => `${profileStatusEmoji(p.status)} ${p.number}. الرمز: ${p.pin} · ${profileStatusLabel(p.status)}`)
  ].join('\n');
  const options = { reply_markup: profilesKeyboard(email) };
  if (editMessageId) {
    try { await bot.editMessageText(text, { chat_id: chatId, message_id: editMessageId, ...options }); return; } catch (_) {}
  }
  await safeSendMessage(bot, chatId, text, options);
}

async function showProfileDetail(bot, chatId, emailId, number, editMessageId = null) {
  const email = getBotEmail(chatId, emailId);
  if (!email) return;
  const profile = email.profiles.find((p) => p.number === Number(number));
  if (!profile) return;
  const text = [
    `👤 البروفايل ${profile.number}`,
    `📧 ${email.address}`,
    `🔐 الرمز: ${profile.pin}`,
    `📌 الحالة: ${profileStatusLabel(profile.status)}`
  ].join('\n');
  const options = { reply_markup: profileDetailKeyboard(email, profile) };
  if (editMessageId) {
    try { await bot.editMessageText(text, { chat_id: chatId, message_id: editMessageId, ...options }); return; } catch (_) {}
  }
  await safeSendMessage(bot, chatId, text, options);
}

function recomputeEmailCompletion(email) {
  const allSold = email.profiles.every((profile) => profile.status === 'sold');
  if (allSold) {
    if (email.status !== 'completed') email.completedAt = new Date().toISOString();
    email.status = 'completed';
  } else {
    email.status = 'active';
    email.completedAt = null;
  }
}

async function cleanupExpiredEmails(bot = null) {
  let changed = false;
  const now = Date.now();
  for (const [chatId, chat] of Object.entries(botState.chats)) {
    const expired = chat.emails.filter((email) => new Date(email.expiresAt).getTime() <= now);
    if (!expired.length) continue;
    const expiredIds = new Set(expired.map((email) => email.id));
    chat.emails = chat.emails.filter((email) => !expiredIds.has(email.id));
    if (expiredIds.has(chat.selectedEmailId)) chat.selectedEmailId = chat.emails[0]?.id || null;
    changed = true;
    if (bot && isTelegramChatAllowed(chatId)) {
      for (const email of expired) {
        try {
          await bot.sendMessage(chatId, `🗑️ تم حذف الإيميل من البوت بعد مرور 6 أيام:\n\n${email.address}\n\nتم إيقاف مراقبة رسائله تلقائياً.`);
        } catch (_) {}
      }
    }
  }
  if (changed) saveBotState();
}

let monitorBusy = false;
async function monitorAllMailboxes(bot) {
  if (monitorBusy) return;
  monitorBusy = true;
  try {
    await cleanupExpiredEmails(bot);
    for (const [chatId, chat] of Object.entries(botState.chats)) {
      if (!isTelegramChatAllowed(chatId)) continue;
      for (const email of chat.emails) {
        if (new Date(email.expiresAt).getTime() <= Date.now()) continue;
        try {
          const { messages } = await getMailbox(email.address);
          email.lastCheckedAt = new Date().toISOString();
          const seen = new Set(email.seenMessageIds || []);
          const fresh = messages.filter((message) => !seen.has(message.id)).reverse();
          for (const message of messages) seen.add(message.id);
          email.seenMessageIds = Array.from(seen).slice(-200);
          if (fresh.length) {
            for (const message of fresh.slice(-10)) {
              await sendFormattedMessage(bot, chatId, message, -1, email.address);
            }
          }
          saveBotState();
        } catch (error) {
          console.warn(`[telegram-monitor] ${email.address}:`, error?.message || error);
        }
        // تخفيف الطلبات على Generator.email عند وجود عدة صناديق.
        await new Promise((resolve) => setTimeout(resolve, 600));
      }
    }
  } finally {
    monitorBusy = false;
  }
}

let telegramBot = null;
let telegramStatus = TELEGRAM_BOT_TOKEN ? 'starting' : 'disabled';
let monitorTimer = null;
let cleanupTimer = null;

function startTelegramBot() {
  if (!TELEGRAM_BOT_TOKEN) {
    telegramStatus = 'disabled';
    console.log('[telegram] TELEGRAM_BOT_TOKEN is not set. Website/API will still run.');
    return;
  }

  try {
    const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, {
      polling: { interval: 500, params: { timeout: 25 } }
    });
    telegramBot = bot;
    telegramStatus = 'running';

    bot.on('polling_error', (error) => {
      telegramStatus = 'polling_error';
      console.error('[telegram] polling error:', error?.message || error);
    });

    bot.onText(/^\/start(?:@\w+)?(?:\s.*)?$/i, async (msg) => {
      const chatId = msg.chat.id;
      if (!isTelegramChatAllowed(chatId)) {
        await bot.sendMessage(chatId, '⛔ هذا البوت غير متاح لهذا الحساب.');
        return;
      }
      getBotChat(chatId);
      await bot.sendMessage(chatId,
        '📬 مدير الإيميلات والبروفايلات\n\nيمكنك إنشاء إيميل عشوائي أو يدوي، إدارة 5 بروفايلات لكل إيميل، واستلام الرسائل تلقائياً.\n\n⏳ كل إيميل يُحذف من بيانات البوت تلقائياً بعد 6 أيام.',
        { reply_markup: telegramKeyboard() }
      );
    });

    bot.on('callback_query', async (query) => {
      const chatId = query.message?.chat?.id;
      if (!chatId || !isTelegramChatAllowed(chatId)) return;
      const data = String(query.data || '');
      try { await bot.answerCallbackQuery(query.id); } catch (_) {}
      const messageId = query.message?.message_id || null;
      const chat = getBotChat(chatId);

      if (data === 'noop') return;
      if (data === 'create:random') {
        const { email } = addBotEmail(chatId, newGeneratorEmail(), 'random');
        await establishMailboxBaseline(email);
        await bot.sendMessage(chatId, `✅ تم إنشاء الإيميل العشوائي\n\n📧 ${email.address}\n⏳ سيُحذف من البوت بعد 6 أيام.`, { reply_markup: telegramKeyboard() });
        await showEmailDetail(bot, chatId, email.id);
        return;
      }
      if (data === 'create:manual') {
        chat.pending = { action: 'manual-email' };
        saveBotState();
        await bot.sendMessage(chatId, '✍️ أرسل اسم الإيميل الذي تريده.\n\nمثال:\n`nabil77`\nأو:\n`nabil77@5xu.vn`', { parse_mode: 'Markdown' });
        return;
      }
      if (data === 'emails:active') { await showEmailList(bot, chatId, false); return; }
      if (data.startsWith('email:')) { await showEmailDetail(bot, chatId, data.split(':')[1], messageId); return; }
      if (data.startsWith('profiles:')) { await showProfiles(bot, chatId, data.split(':')[1], messageId); return; }
      if (data.startsWith('profile:')) {
        const [, emailId, number] = data.split(':');
        await showProfileDetail(bot, chatId, emailId, Number(number), messageId);
        return;
      }
      if (data.startsWith('inbox:')) {
        const emailId = data.split(':')[1];
        setSelectedBotEmail(chatId, emailId);
        await sendMailboxToTelegram(bot, chatId, emailId);
        return;
      }
      if (data.startsWith('pin:')) {
        const [, emailId, number] = data.split(':');
        chat.pending = { action: 'change-pin', emailId, profileNumber: Number(number) };
        saveBotState();
        await bot.sendMessage(chatId, `✏️ أرسل الرمز الجديد للبروفايل ${number}.\nيجب أن يكون أرقاماً فقط.`);
        return;
      }
      if (data.startsWith('rename:')) {
        const emailId = data.split(':')[1];
        chat.pending = { action: 'rename-email', emailId };
        saveBotState();
        await bot.sendMessage(chatId, '✏️ أرسل الاسم المحلي لهذا الإيميل، أو أرسل - لمسحه.');
        return;
      }
      if (data.startsWith('pstatus:')) {
        const [, emailId, numberRaw, status] = data.split(':');
        const email = getBotEmail(chatId, emailId);
        const profile = email?.profiles.find((p) => p.number === Number(numberRaw));
        if (!email || !profile || !['available', 'review', 'sold'].includes(status)) return;
        profile.status = status;
        profile.statusChangedAt = new Date().toISOString();
        if (status === 'review') profile.reservedAt = profile.statusChangedAt;
        if (status === 'sold') profile.soldAt = profile.statusChangedAt;
        if (status === 'available') { profile.reservedAt = null; profile.soldAt = null; }
        recomputeEmailCompletion(email);
        saveBotState();
        await showProfileDetail(bot, chatId, email.id, profile.number, messageId);
        if (email.status === 'completed') {
          await bot.sendMessage(chatId, `✅ تم بيع البروفايلات الخمسة للإيميل:\n${email.address}\n\nسيبقى محفوظاً حتى تنتهي مدة الـ6 أيام ثم يُحذف تلقائياً.`);
        }
        return;
      }
      if (data.startsWith('delete:')) {
        const emailId = data.split(':')[1];
        const email = getBotEmail(chatId, emailId);
        if (!email) return;
        await bot.sendMessage(chatId, `⚠️ حذف ${email.address} من البوت؟`, {
          reply_markup: { inline_keyboard: [[
            { text: 'نعم، حذف', callback_data: `delete-confirm:${email.id}` },
            { text: 'إلغاء', callback_data: `email:${email.id}` }
          ]] }
        });
        return;
      }
      if (data.startsWith('delete-confirm:')) {
        const emailId = data.split(':')[1];
        const removed = removeBotEmail(chatId, emailId);
        if (removed) await bot.sendMessage(chatId, `🗑️ تم حذف ${removed.address} من بيانات البوت وإيقاف مراقبته.`, { reply_markup: telegramKeyboard() });
        return;
      }
    });

    bot.on('message', async (msg) => {
      const chatId = msg.chat?.id;
      if (!chatId || !msg.text || /^\/start(?:@\w+)?/i.test(msg.text)) return;
      if (!isTelegramChatAllowed(chatId)) return;
      const text = msg.text.trim();
      const chat = getBotChat(chatId);

      // حالات الإدخال اليدوي.
      if (chat.pending?.action === 'manual-email') {
        let local = text.toLowerCase().trim();
        if (local.includes('@')) {
          if (!local.endsWith('@5xu.vn')) {
            await bot.sendMessage(chatId, '❌ الإيميل اليدوي يجب أن ينتهي بـ @5xu.vn. حاول مرة ثانية.');
            return;
          }
          local = local.split('@')[0];
        }
        if (!/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(local)) {
          await bot.sendMessage(chatId, '❌ الاسم غير صالح. استخدم أحرف إنجليزية وأرقام و . _ - فقط.');
          return;
        }
        chat.pending = null;
        const { email, created } = addBotEmail(chatId, `${local}@5xu.vn`, 'manual');
        await establishMailboxBaseline(email);
        await bot.sendMessage(chatId, `${created ? '✅ تم إضافة' : 'ℹ️ الإيميل موجود مسبقاً وتم اختياره'}\n\n📧 ${email.address}\n⏳ سيُحذف من البوت بعد 6 أيام.`, { reply_markup: telegramKeyboard() });
        await showEmailDetail(bot, chatId, email.id);
        return;
      }

      if (chat.pending?.action === 'change-pin') {
        const pending = chat.pending;
        if (!/^\d{1,12}$/.test(text)) {
          await bot.sendMessage(chatId, '❌ الرمز يجب أن يحتوي على أرقام فقط، من 1 إلى 12 رقم.');
          return;
        }
        const email = getBotEmail(chatId, pending.emailId);
        const profile = email?.profiles.find((p) => p.number === Number(pending.profileNumber));
        chat.pending = null;
        if (profile) {
          profile.pin = text;
          saveBotState();
          await bot.sendMessage(chatId, `✅ تم تغيير رمز البروفايل ${profile.number} إلى ${profile.pin}.`);
          await showProfileDetail(bot, chatId, email.id, profile.number);
        }
        return;
      }

      if (chat.pending?.action === 'rename-email') {
        const pending = chat.pending;
        const email = getBotEmail(chatId, pending.emailId);
        chat.pending = null;
        if (email) {
          email.localName = text === '-' ? '' : text.slice(0, 80);
          saveBotState();
          await bot.sendMessage(chatId, '✅ تم حفظ الاسم المحلي.');
          await showEmailDetail(bot, chatId, email.id);
        }
        return;
      }

      if (text === '➕ إنشاء إيميل' || text === '/new') {
        await bot.sendMessage(chatId, 'اختر طريقة إنشاء الإيميل:', { reply_markup: createEmailKeyboard() });
        return;
      }
      if (text === '📧 الإيميلات' || text === '/emails') { await showEmailList(bot, chatId, false); return; }
      if (text === '✅ الإيميلات المباعة' || text === '/sold') { await showEmailList(bot, chatId, true); return; }
      if (text === '🌐 فتح الموقع') {
        const site = publicWebsiteUrl();
        if (site) await bot.sendMessage(chatId, '🌐 الموقع:', { reply_markup: { inline_keyboard: [[{ text: 'فتح الموقع', url: site }]] } });
        else await bot.sendMessage(chatId, 'أضف PUBLIC_URL في Railway أو أنشئ Public Domain للخدمة.');
        return;
      }
    });

    bot.setMyCommands([
      { command: 'start', description: 'تشغيل البوت' },
      { command: 'new', description: 'إنشاء إيميل عشوائي أو يدوي' },
      { command: 'emails', description: 'عرض الإيميلات' },
      { command: 'sold', description: 'الإيميلات المباعة' }
    ]).catch((error) => console.warn('[telegram] setMyCommands:', error.message));

    bot.getMe()
      .then((me) => {
        telegramStatus = 'running';
        console.log(`[telegram] bot @${me.username || me.id} is running with polling`);
        cleanupExpiredEmails(bot).catch(() => {});
        monitorAllMailboxes(bot).catch(() => {});
        monitorTimer = setInterval(() => monitorAllMailboxes(bot).catch(() => {}), AUTO_CHECK_INTERVAL_MS);
        cleanupTimer = setInterval(() => cleanupExpiredEmails(bot).catch(() => {}), 10 * 60 * 1000);
        monitorTimer.unref?.();
        cleanupTimer.unref?.();
      })
      .catch((error) => {
        telegramStatus = 'token_error';
        console.error('[telegram] token check failed:', error?.message || error);
      });
  } catch (error) {
    telegramStatus = 'startup_error';
    console.error('[telegram] startup error:', error?.message || error);
  }
}

// =========================
// Web API + static website
// =========================

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'generator-email-railway-web-telegram',
    mailReader: MAIL_READER_BUILD,
    telegram: {
      configured: Boolean(TELEGRAM_BOT_TOKEN),
      status: telegramStatus,
      autoCheckSeconds: Math.round(AUTO_CHECK_INTERVAL_MS / 1000),
      storedChats: Object.keys(botState.chats).length
    },
    time: new Date().toISOString()
  });
});

app.get('/api/inbox', async (req, res) => {
  const email = normalizeAddress(req.query.email);
  if (!isAllowedEmail(email)) {
    return res.status(400).json({ error: 'عنوان البريد غير صالح أو لا يستخدم @5xu.vn.' });
  }

  try {
    const { messages, source } = await getMailbox(email);
    return res.json({
      ok: true,
      email,
      count: messages.length,
      source,
      messages,
      checkedAt: new Date().toISOString()
    });
  } catch (error) {
    console.error('[inbox]', email, error?.message || error);
    return res.status(error?.statusCode || 502).json({
      error: 'تعذر جلب صندوق البريد حالياً.',
      detail: String(error?.message || error)
    });
  }
});

const publicDir = path.join(__dirname, 'public');
app.use(express.static(publicDir, {
  etag: true,
  maxAge: '5m',
  setHeaders(res, filePath) {
    if (/app\.js$|index\.html$/i.test(filePath)) res.setHeader('Cache-Control', 'no-store');
  }
}));

app.get('*', (_req, res) => {
  res.sendFile(path.join(publicDir, 'index.html'));
});

const server = app.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running on port ${PORT}`);
  startTelegramBot();
});

async function shutdown(signal) {
  console.log(`[shutdown] ${signal}`);
  try {
    if (monitorTimer) clearInterval(monitorTimer);
    if (cleanupTimer) clearInterval(cleanupTimer);
    if (telegramBot) await telegramBot.stopPolling({ cancel: true });
  } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
}

process.once('SIGTERM', () => shutdown('SIGTERM'));
process.once('SIGINT', () => shutdown('SIGINT'));
