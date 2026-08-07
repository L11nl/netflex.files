'use strict';

const path = require('path');
const crypto = require('crypto');
const express = require('express');
const cors = require('cors');
const axios = require('axios');
const cheerio = require('cheerio');

const app = express();
const PORT = Number(process.env.PORT || 3000);
const GENERATOR_BASE = 'https://generator.email';
const GENERATOR_DOMAIN = '5xu.vn';

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

function stableId(sender, subject, body, index) {
  return 'gen-' + crypto
    .createHash('sha1')
    .update(`${sender}|${subject}|${body}|${index}`)
    .digest('hex')
    .slice(0, 16);
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

function parseMailbox(html, email) {
  const $ = cheerio.load(String(html || ''));
  const table = $('#email-table');
  const root = table.length ? table : $.root();

  let senders = root.find('.from_div_45g45gg').toArray();
  let subjects = root.find('.subj_div_45g45gg').toArray();
  let bodies = root.find('.mess_bodiyy').toArray();

  if (!senders.length) senders = $('.from_div_45g45gg').toArray();
  if (!subjects.length) subjects = $('.subj_div_45g45gg').toArray();
  if (!bodies.length) bodies = $('.mess_bodiyy').toArray();

  const count = Math.max(senders.length, subjects.length, bodies.length);
  const messages = [];

  for (let i = 0; i < count; i += 1) {
    const senderText = senders[i] ? $(senders[i]).text().trim() : 'غير معروف';
    const subject = subjects[i] ? $(subjects[i]).text().trim() : 'بدون عنوان';
    const bodyNode = bodies[i] || null;

    let text = '';
    let htmlBody = '';
    const links = [];

    if (bodyNode) {
      const body = $(bodyNode).clone();
      htmlBody = body.html() || '';

      body.find('a[href]').each((_, el) => {
        const href = normalizeUrl($(el).attr('href') || '');
        if (!href) return;
        if (!links.includes(href)) links.push(href);
        const title = $(el).text().trim();
        $(el).replaceWith(`\n${title ? `${title}\n` : ''}${href}\n`);
      });

      text = body.text()
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .join('\n');

      const plainUrls = text.match(/https?:\/\/[^\s<>'\"]+/gi) || [];
      for (const raw of plainUrls) {
        const url = normalizeUrl(raw.replace(/[),.;]+$/g, ''));
        if (url && !links.includes(url)) links.push(url);
      }
    }

    const codes = extractCodes(`${subject}\n${text}`);
    messages.push({
      id: stableId(senderText, subject, text || htmlBody, i),
      from: parseSender(senderText),
      subject,
      intro: text.slice(0, 180),
      text,
      html: htmlBody,
      createdAt: new Date(Date.now() - i * 1000).toISOString(),
      seen: false,
      verifications: codes,
      codes,
      links,
      _generator: true
    });
  }

  return messages;
}

async function fetchMailboxHtml(email) {
  const [username, domain] = email.split('@');
  const targets = [
    `${GENERATOR_BASE}/${domain}/${username}`,
    `${GENERATOR_BASE}/inbox9/${email}`,
    `${GENERATOR_BASE}/${email}`
  ];

  const headers = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 Chrome/136.0 Mobile Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache'
  };

  let lastError = null;
  for (const target of targets) {
    try {
      const response = await axios.get(target, {
        headers,
        timeout: 22000,
        responseType: 'text',
        maxRedirects: 5,
        validateStatus: (status) => status >= 200 && status < 400,
        params: { _: `${Date.now()}-${Math.random().toString(16).slice(2)}` }
      });

      const html = String(response.data || '');
      const lower = html.toLowerCase();
      const identityOk = lower.includes(email) || (lower.includes(username) && lower.includes(domain));
      if (!identityOk) {
        lastError = new Error('الخدمة ردت بصفحة لا تخص صندوق هذا الإيميل.');
        continue;
      }
      return { html, source: target };
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error('تعذر قراءة صندوق البريد من Generator.email.');
}

app.get('/health', (_req, res) => {
  res.json({ ok: true, service: 'generator-email-railway', time: new Date().toISOString() });
});

app.get('/api/inbox', async (req, res) => {
  const email = normalizeAddress(req.query.email);
  if (!isAllowedEmail(email)) {
    return res.status(400).json({ error: 'عنوان البريد غير صالح أو لا يستخدم @5xu.vn.' });
  }

  try {
    const { html, source } = await fetchMailboxHtml(email);
    const messages = parseMailbox(html, email);
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
    return res.status(502).json({
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

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running on port ${PORT}`);
});
