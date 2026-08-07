const APP_BUILD = "2026-08-07-fetch-code-no-redirect-v4";
'use strict';

/* تطبيق ثابت بالكامل: جميع البيانات تحفظ داخل LocalStorage فقط. */
const STORAGE_KEY = 'cd-mail-profile-manager-v1';
const GENERATOR_BASE = 'https://generator.email';
const GENERATOR_HOME_PAGES = ['https://generator.email/', 'https://ja.generator.email/'];
const GENERATOR_DOMAIN = '5xu.vn';
// نُبقي 5xu.vn كما هو تماماً مثل كود Python المرسل من المستخدم.
const LEGACY_GENERATOR_DOMAINS = ['5xu.vn'];
const GENERATOR_FALLBACK_DOMAINS = [GENERATOR_DOMAIN];
// أكثر من جسر CORS كخطة احتياط لأن GitHub Pages لا يستطيع قراءة generator.email مباشرة دائماً.
const GENERATOR_CORS_BUILDERS = [
  (url) => `https://corsproxy.io/?url=${encodeURIComponent(url)}`,
  (url) => `https://api.allorigins.win/raw?url=${encodeURIComponent(url)}`,
  (url) => `https://api.codetabs.com/v1/proxy/?quest=${encodeURIComponent(url)}`
];
// تبقى هذه المزودات فقط لفتح الحسابات القديمة المحفوظة سابقاً.
const MAIL_PROVIDERS = [
  { id: 'mailtm', name: 'Mail.tm', apiBase: 'https://api.mail.tm' },
  { id: 'mailgw', name: 'Mail.gw', apiBase: 'https://api.mail.gw' }
];
const DEFAULT_PINS = ['1212', '1001', '2121', '2026', '2002'];
const DEFAULT_COLORS = ['#0A84FF', '#FFD60A', '#FF3B30', '#00677A', '#30D18A'];
const READY_ICONS = ['face', 'star', 'crown', 'heart', 'bolt'];

const els = {
  splash: document.getElementById('splash'),
  app: document.getElementById('app'),
  main: document.getElementById('mainContent'),
  sheet: document.getElementById('bottomSheet'),
  sheetContent: document.getElementById('sheetContent'),
  sheetBackdrop: document.getElementById('sheetBackdrop'),
  modal: document.getElementById('modal'),
  modalContent: document.getElementById('modalContent'),
  modalBackdrop: document.getElementById('modalBackdrop'),
  toastRegion: document.getElementById('toastRegion'),
  importInput: document.getElementById('backupImportInput'),
  imageInput: document.getElementById('profileImageInput'),
  lockScreen: document.getElementById('lockScreen'),
  unlockPin: document.getElementById('unlockPin'),
  unlockBtn: document.getElementById('unlockBtn'),
  unlockError: document.getElementById('unlockError')
};

let state = loadState();
let ui = {
  view: 'home',
  returnView: 'home',
  selectedEmailId: null,
  addMode: 'auto',
  busy: new Set(),
  inboxEmailId: null,
  inboxMessages: [],
  inboxFilter: 'all',
  inboxSearch: '',
  inboxLoading: false,
  messageDetail: null,
  profileEditor: null,
  autoSaveTimer: null
};
let modalResolver = null;
let polling = null;
let pullStartY = 0;
let generatorDomainCache = { domains: [], fetchedAt: 0 };

function initialState() {
  return {
    version: 1,
    emails: [],
    sales: [],
    archivedMessages: [],
    recoveredProfiles: [],
    settings: {
      theme: 'dark',
      accent: '#0A84FF',
      showPasswords: false,
      waitDuration: 60,
      sound: true,
      vibration: true,
      lockEnabled: false,
      pinHash: ''
    },
    lastEmailId: null,
    lastProfileRef: null,
    globalProfileStyles: null,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString()
  };
}

function createDefaultProfile(number) {
  return {
    number,
    pin: DEFAULT_PINS[number - 1],
    defaultPin: DEFAULT_PINS[number - 1],
    color: DEFAULT_COLORS[number - 1],
    icon: 'face',
    imageData: '',
    status: 'available',
    customerName: '',
    customerPhone: '',
    notes: '',
    statusChangedAt: new Date().toISOString(),
    reservedAt: null,
    soldAt: null,
    recoveredAt: null
  };
}

function normalizeState(raw) {
  const base = initialState();
  const next = raw && typeof raw === 'object' ? raw : base;
  next.version = 1;
  next.emails = Array.isArray(next.emails) ? next.emails : [];
  next.sales = Array.isArray(next.sales) ? next.sales : [];
  next.archivedMessages = Array.isArray(next.archivedMessages) ? next.archivedMessages : [];
  next.recoveredProfiles = Array.isArray(next.recoveredProfiles) ? next.recoveredProfiles : [];
  next.settings = { ...base.settings, ...(next.settings || {}) };
  next.emails = next.emails.map((email) => {
    const profiles = Array.isArray(email.profiles) ? email.profiles : [];
    const fixedProfiles = [1, 2, 3, 4, 5].map((number) => {
      const old = profiles.find((item) => Number(item.number) === number) || {};
      return { ...createDefaultProfile(number), ...old, number };
    });
    const address = email.address || '';
    const domain = String(address).split('@').pop().trim().toLowerCase();
    let provider = email.provider;
    if (!['generator', 'mailtm', 'mailgw', 'local'].includes(provider)) {
      provider = LEGACY_GENERATOR_DOMAINS.includes(domain) ? 'generator' : ((email.token || email.password) ? 'mailtm' : 'local');
    }
    return {
      localId: email.localId || makeId(),
      mailTmId: email.mailTmId || email.id || '',
      address,
      password: email.password || '',
      token: email.token || '',
      provider,
      apiBase: email.apiBase || (provider === 'mailgw' ? 'https://api.mail.gw' : provider === 'mailtm' ? 'https://api.mail.tm' : ''),
      generatorDomain: email.generatorDomain || (provider === 'generator' ? (domain || GENERATOR_DOMAIN) : ''),
      generatorSeenIds: Array.isArray(email.generatorSeenIds) ? email.generatorSeenIds : [],
      generatorDeletedIds: Array.isArray(email.generatorDeletedIds) ? email.generatorDeletedIds : [],
      localName: email.localName || '',
      createdAt: email.createdAt || new Date().toISOString(),
      status: ['active', 'archived', 'completed'].includes(email.status) ? email.status : 'active',
      archivedAt: email.archivedAt || null,
      completedAt: email.completedAt || null,
      archivedMessageIds: Array.isArray(email.archivedMessageIds) ? email.archivedMessageIds : [],
      profiles: fixedProfiles.sort((a, b) => a.number - b.number)
    };
  });
  return next;
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return normalizeState(raw ? JSON.parse(raw) : initialState());
  } catch (error) {
    return initialState();
  }
}

function saveState({ silent = false } = {}) {
  state.updatedAt = new Date().toISOString();
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    return true;
  } catch (error) {
    if (!silent) toast('امتلأت مساحة التخزين المحلية. صدّر نسخة احتياطية واحذف الصور الكبيرة أو البيانات القديمة.', 'error', 6500);
    return false;
  }
}

function makeId() {
  if (crypto?.randomUUID) return crypto.randomUUID();
  return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function escapeHTML(value = '') {
  return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function formatDate(value, withTime = true) {
  if (!value) return 'غير محدد';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'غير محدد';
  return new Intl.DateTimeFormat('ar-IQ', {
    dateStyle: 'medium',
    ...(withTime ? { timeStyle: 'short' } : {})
  }).format(date);
}

function formatAccountAge(value) {
  if (!value) return 'تاريخ الإنشاء غير معروف';
  const created = new Date(value);
  if (Number.isNaN(created.getTime())) return 'تاريخ الإنشاء غير معروف';

  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startCreated = new Date(created.getFullYear(), created.getMonth(), created.getDate());
  const days = Math.max(0, Math.floor((startToday - startCreated) / 86400000));

  if (days === 0) return 'تاريخ الإنشاء: اليوم';
  if (days === 1) return 'تم إنشاء هذا الحساب قبل يوم واحد';
  if (days === 2) return 'تم إنشاء هذا الحساب قبل يومين';
  if (days >= 3 && days <= 10) return `تم إنشاء هذا الحساب قبل ${days} أيام`;
  return `تم إنشاء هذا الحساب قبل ${days} يوماً`;
}

function statusLabel(status) {
  return status === 'available' ? 'متاح' : status === 'review' ? 'قيد المراجعة' : 'تم البيع';
}

function emailStatusLabel(status) {
  return status === 'completed' ? 'مكتمل' : status === 'archived' ? 'مؤرشف' : 'نشط';
}

function isGeneratorEmail(email) {
  return email?.provider === 'generator' || (!email?.provider && LEGACY_GENERATOR_DOMAINS.includes(emailDomain(email?.address)));
}

function isMailTmEmail(email) {
  return email?.provider === 'mailtm' || email?.provider === 'mailgw';
}

function hasRemoteInbox(email) {
  return isGeneratorEmail(email) || isMailTmEmail(email);
}

function emailProviderLabel(email) {
  if (isGeneratorEmail(email)) return 'Generator.email';
  if (email?.provider === 'mailgw') return 'Mail.gw';
  if (email?.provider === 'mailtm') return 'Mail.tm';
  return 'إيميل عادي';
}

function getEmailApiBase(email) {
  if (!isMailTmEmail(email)) return '';
  return email.apiBase || (email.provider === 'mailgw' ? 'https://api.mail.gw' : 'https://api.mail.tm');
}

function getProvider(providerId) {
  return MAIL_PROVIDERS.find((provider) => provider.id === providerId) || MAIL_PROVIDERS[0];
}

function emailDomain(address = '') {
  return String(address).split('@').pop().trim().toLowerCase();
}

function isValidEmailAddress(address) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/i.test(address);
}

function getEmail(id) {
  return state.emails.find((email) => email.localId === id);
}

function getProfile(email, number) {
  return email?.profiles.find((profile) => profile.number === Number(number));
}

function profileCounts(email) {
  return email.profiles.reduce((acc, profile) => {
    acc[profile.status] += 1;
    return acc;
  }, { available: 0, review: 0, sold: 0 });
}

function totalStats() {
  const profiles = state.emails.flatMap((email) => email.profiles);
  return {
    emails: state.emails.length,
    available: profiles.filter((p) => p.status === 'available').length,
    review: profiles.filter((p) => p.status === 'review').length,
    sold: profiles.filter((p) => p.status === 'sold').length
  };
}

function applyTheme() {
  const theme = state.settings.theme;
  const useLight = theme === 'light' || (theme === 'auto' && matchMedia('(prefers-color-scheme: light)').matches);
  document.body.classList.toggle('light', useLight);
  document.documentElement.style.setProperty('--accent', state.settings.accent || '#0A84FF');
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.content = useLight ? '#f2f2f7' : '#0b0b0d';
}

function toast(message, type = 'success', duration = 3200) {
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.textContent = message;
  els.toastRegion.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transform = 'translateY(-8px)';
    setTimeout(() => node.remove(), 220);
  }, duration);
}

function setBusy(key, active) {
  active ? ui.busy.add(key) : ui.busy.delete(key);
}

function isBusy(key) {
  return ui.busy.has(key);
}

function profileSvg(icon = 'face') {
  const common = 'viewBox="0 0 100 100" class="face-svg" aria-hidden="true"';
  if (icon === 'star') return `<svg ${common}><path fill="white" d="m50 13 10.4 21 23.2 3.4-16.8 16.4 4 23.1L50 66 29.2 76.9l4-23.1-16.8-16.4L39.6 34 50 13Z"/></svg>`;
  if (icon === 'crown') return `<svg ${common}><path fill="white" d="m18 33 19 14 13-25 13 25 19-14-8 42H26l-8-42Zm10 48h44v7H28v-7Z"/></svg>`;
  if (icon === 'heart') return `<svg ${common}><path fill="white" d="M50 82S18 64 18 39c0-13 15-22 32-7 17-15 32-6 32 7 0 25-32 43-32 43Z"/></svg>`;
  if (icon === 'bolt') return `<svg ${common}><path fill="white" d="M57 8 22 56h25l-4 36 35-49H54l3-35Z"/></svg>`;
  return `<svg ${common}><circle cx="32" cy="38" r="7" fill="white"/><circle cx="68" cy="38" r="7" fill="white"/><path d="M29 61c11 12 31 12 42 0" fill="none" stroke="white" stroke-width="7" stroke-linecap="round"/></svg>`;
}

function profileVisual(profile, sizeClass = '') {
  const media = profile.imageData
    ? `<img src="${escapeHTML(profile.imageData)}" alt="صورة البروفايل ${profile.number}">`
    : profileSvg(profile.icon);
  return `<div class="profile-face ${sizeClass}" style="background:${escapeHTML(profile.color)}">${media}</div>`;
}

function openModal(html) {
  els.modalContent.innerHTML = html;
  els.modal.classList.remove('hidden');
  els.modalBackdrop.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeModal(result = null) {
  els.modal.classList.add('hidden');
  els.modalBackdrop.classList.add('hidden');
  els.modalContent.innerHTML = '';
  if (modalResolver) {
    const resolve = modalResolver;
    modalResolver = null;
    resolve(result);
  }
  if (els.sheet.classList.contains('hidden')) document.body.style.overflow = '';
}

function confirmDialog({ title = 'تأكيد', message = '', confirmText = 'تأكيد', cancelText = 'إلغاء', danger = false } = {}) {
  return new Promise((resolve) => {
    modalResolver = resolve;
    openModal(`
      <h2>${escapeHTML(title)}</h2>
      <p>${escapeHTML(message)}</p>
      <div class="modal-actions">
        <button class="btn ${danger ? 'danger' : 'primary'} wide" data-modal-result="confirm">${escapeHTML(confirmText)}</button>
        <button class="btn ghost wide" data-modal-result="cancel">${escapeHTML(cancelText)}</button>
      </div>
    `);
  });
}

function choiceDialog({ title, message, choices }) {
  return new Promise((resolve) => {
    modalResolver = resolve;
    openModal(`
      <h2>${escapeHTML(title)}</h2>
      <p>${escapeHTML(message)}</p>
      <div class="modal-actions">
        ${choices.map((choice) => `<button class="btn ${choice.className || 'ghost'} wide" data-modal-result="${escapeHTML(choice.value)}">${escapeHTML(choice.label)}</button>`).join('')}
        <button class="btn ghost wide" data-modal-result="cancel">إلغاء</button>
      </div>
    `);
  });
}

function openSheet(html) {
  els.sheetContent.innerHTML = html;
  els.sheet.classList.remove('hidden');
  els.sheetBackdrop.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeSheet() {
  if (polling && ui.inboxEmailId && polling.emailId === ui.inboxEmailId) stopPolling(false);
  els.sheet.classList.add('hidden');
  els.sheetBackdrop.classList.add('hidden');
  els.sheetContent.innerHTML = '';
  ui.inboxEmailId = null;
  ui.messageDetail = null;
  ui.profileEditor = null;
  if (els.modal.classList.contains('hidden')) document.body.style.overflow = '';
}

function updateNav() {
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === ui.view || (ui.view === 'email-detail' && btn.dataset.view === ui.returnView));
  });
}

function goView(view) {
  closeSheet();
  ui.view = view;
  ui.selectedEmailId = null;
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function render() {
  applyTheme();
  updateNav();
  if (ui.view === 'email-detail') renderEmailDetail();
  else if (ui.view === 'sold-emails') renderSoldEmails();
  else if (ui.view === 'sales') renderSales();
  else if (ui.view === 'archive') renderArchive();
  else if (ui.view === 'settings') renderSettings();
  else renderHome();
}

function renderStats() {
  const stats = totalStats();
  return `
    <div class="stats-grid" aria-label="الإحصائيات">
      <div class="stat-card"><b>${stats.emails}</b><span>عدد الإيميلات</span></div>
      <div class="stat-card"><b>${stats.available}</b><span>البروفايلات المتاحة</span></div>
      <div class="stat-card"><b>${stats.review}</b><span>قيد المراجعة</span></div>
      <div class="stat-card"><b>${stats.sold}</b><span>تم بيعها</span></div>
    </div>
  `;
}

function renderHome() {
  const activeEmails = state.emails.filter((email) => email.status === 'active');
  els.main.innerHTML = `
    <section class="section">
      <div class="section-head"><div><h2>إضافة إيميل</h2><p>اختر الطريقة المناسبة</p></div></div>
      <div class="card add-card simple-add-card">
        <button class="btn primary wide" data-action="create-email" ${isBusy('create-email') ? 'disabled' : ''}>
          ${isBusy('create-email') ? '<span class="spinner"></span> جاري إنشاء الإيميل...' : '＋ إنشاء إيميل تلقائي'}
        </button>
        <button class="btn soft wide" data-action="open-local-email-modal">＋ إضافة إيميل</button>
      </div>
    </section>

    <section class="section">
      <div class="section-head">
        <div><h2>الإيميلات الحالية</h2><p>${activeEmails.length ? 'افتح الإيميل أو ابحث عنه' : 'لا توجد إيميلات حالياً'}</p></div>
        ${activeEmails.length ? '<button class="btn ghost small" data-action="focus-active-search">البحث</button>' : ''}
      </div>
      ${activeEmails.length ? '<div class="searchbar"><span>⌕</span><input id="activeEmailSearch" class="input" dir="ltr" data-live-filter="active-email-list" placeholder="ابحث بالإيميل"></div>' : ''}
      <div id="active-email-list" class="email-list">
        ${activeEmails.length ? activeEmails.map((email) => emailCard(email, false)).join('') : emptyState('📨', 'لا توجد إيميلات بعد', 'أنشئ إيميلاً تلقائياً أو أضف إيميلاً للبدء.')}
      </div>
    </section>
  `;
}

function emailCard(email, soldPage = false) {
  const searchable = email.address.toLowerCase();
  return `
    <article class="card email-card simple-email-card" data-search-item="${escapeHTML(searchable)}">
      <div class="email-title">
        <h3 dir="ltr">${escapeHTML(email.address)}</h3>
        <p>${formatAccountAge(email.createdAt)}</p>
      </div>
      <div class="email-actions simple-actions">
        <button class="btn primary small" data-action="open-email" data-email-id="${email.localId}" data-return-view="${soldPage ? 'sold-emails' : 'home'}">فتح</button>
        <button class="btn soft small" data-action="copy-text" data-copy="${escapeHTML(email.address)}">نسخ الإيميل</button>
        ${!soldPage ? `<button class="btn danger small" data-action="delete-email" data-email-id="${email.localId}">حذف</button>` : ''}
      </div>
    </article>
  `;
}

function emptyState(emoji, title, text, actionHtml = '') {
  return `<div class="card empty-state"><div class="emoji">${emoji}</div><h3>${escapeHTML(title)}</h3><p>${escapeHTML(text)}</p>${actionHtml}</div>`;
}

function renderSoldEmails() {
  const completed = state.emails.filter((email) => email.status === 'completed');
  els.main.innerHTML = `
    <section class="section">
      <div class="section-head">
        <div><h2>الإيميلات المباعة</h2><p>تظهر هنا الإيميلات المكتملة فقط</p></div>
        ${completed.length ? '<button class="btn ghost small" data-action="focus-sold-search">البحث</button>' : ''}
      </div>
      ${completed.length ? '<div class="searchbar"><span>⌕</span><input id="soldEmailSearch" class="input" dir="ltr" data-live-filter="sold-email-list" placeholder="ابحث بالإيميل"></div>' : ''}
      <div id="sold-email-list" class="email-list">
        ${completed.length ? completed.map((email) => emailCard(email, true)).join('') : emptyState('✅', 'لا توجد إيميلات مباعة', 'عندما تُباع البروفايلات الخمسة سيظهر الإيميل هنا.')}
      </div>
    </section>
  `;
}

function renderEmailDetail() {
  const email = getEmail(ui.selectedEmailId);
  if (!email) {
    ui.view = ui.returnView || 'home';
    render();
    return;
  }
  state.lastEmailId = email.localId;
  saveState({ silent: true });
  const remoteInbox = hasRemoteInbox(email);
  const soldMode = email.status === 'completed' || ui.returnView === 'sold-emails';
  const shownProfiles = soldMode ? email.profiles : email.profiles.filter((p) => p.status !== 'sold');

  els.main.innerHTML = `
    <div class="detail-header">
      <button class="back-btn" data-action="back-from-email" aria-label="رجوع">‹</button>
      <div><h2 dir="ltr">${escapeHTML(email.address)}</h2><p>${formatAccountAge(email.createdAt)}</p></div>
    </div>

    <section class="card account-summary simple-account-summary">
      <div class="simple-account-actions">
        <button class="btn soft" data-action="copy-text" data-copy="${escapeHTML(email.address)}">نسخ الإيميل</button>
        <button type="button" class="btn primary" data-action="fetch-code" data-email-id="${email.localId}">جلب الكود والروابط</button>
        <button class="btn ghost" data-action="change-profile-icons" data-email-id="${email.localId}">تغيير الرموز</button>
      </div>
      ${!remoteInbox ? '<p class="helper simple-note">هذا الإيميل مضاف للتنظيم فقط. جلب الرسائل يعمل مع الإيميلات المنشأة تلقائياً من Generator.email.</p>' : ''}
    </section>

    <section class="section">
      <div class="section-head"><div><h2>${soldMode ? 'الملفات الخمسة' : 'البروفايلات'}</h2><p>الترتيب: 1 ثم 2 ثم 3 ثم 4 ثم 5</p></div></div>
      <div class="profile-grid">
        ${shownProfiles.length ? shownProfiles.sort((a,b) => a.number - b.number).map((profile) => profileCard(email, profile)).join('') : emptyState('🎉', 'تم بيع جميع البروفايلات', 'ستجد هذا الإيميل في صفحة الإيميلات المباعة.')}
      </div>
    </section>
  `;
}

function profileCard(email, profile) {
  return `
    <button class="profile-card ${profile.status}" data-action="open-profile" data-email-id="${email.localId}" data-profile-number="${profile.number}">
      <span class="status-dot"></span>
      ${profileVisual(profile)}
      <span class="profile-number">${profile.number}</span>
      <span class="profile-status">${statusLabel(profile.status)}${profile.status === 'review' ? ' · بانتظار الدفع' : ''}</span>
    </button>
  `;
}

function renderSales() {
  const records = [...state.sales].sort((a, b) => new Date(b.soldAt) - new Date(a.soldAt));
  els.main.innerHTML = `
    <section class="section">
      <div class="section-head"><div><h2>المبيعات</h2><p>جميع عمليات بيع البروفايلات المحفوظة محلياً</p></div></div>
      <div class="form-grid" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:13px">
        <input class="input" data-sales-filter="email" placeholder="البحث بالإيميل">
        <input class="input" data-sales-filter="profile" inputmode="numeric" placeholder="رقم البروفايل">
        <input class="input" data-sales-filter="customer" placeholder="اسم العميل">
        <input class="input" data-sales-filter="date" type="date" aria-label="تاريخ البيع">
      </div>
      <div id="sales-list" class="sales-list">
        ${records.length ? records.map(saleCard).join('') : emptyState('🧾', 'لا توجد مبيعات', 'عند تسجيل أي بروفايل كمباع سيظهر سجله هنا.')}
      </div>
    </section>
  `;
}

function saleCard(sale) {
  return `
    <article class="card list-card" data-sale-item
      data-email="${escapeHTML((sale.emailAddress || '').toLowerCase())}"
      data-profile="${escapeHTML(String(sale.profileNumber))}"
      data-customer="${escapeHTML((sale.customerName || '').toLowerCase())}"
      data-date="${escapeHTML((sale.soldAt || '').slice(0,10))}">
      <div class="email-top"><div><h3>البروفايل ${sale.profileNumber} · ${escapeHTML(sale.emailAddress)}</h3><p>تاريخ البيع: ${formatDate(sale.soldAt)}</p></div>${sale.restoredAt ? '<span class="tag">تم استرجاعه</span>' : '<span class="tag">مباع</span>'}</div>
      <p>العميل: ${escapeHTML(sale.customerName || 'غير مسجل')} ${sale.customerPhone ? `· ${escapeHTML(sale.customerPhone)}` : ''}</p>
      ${sale.notes ? `<p>الملاحظات: ${escapeHTML(sale.notes)}</p>` : ''}
      <div class="btn-row" style="margin-top:10px">
        <button class="btn soft small" data-action="copy-sale" data-sale-id="${sale.id}">نسخ البيانات</button>
        ${!sale.restoredAt ? `<button class="btn warning small" data-action="restore-sale" data-sale-id="${sale.id}">استرجاع البروفايل</button>` : ''}
        <button class="btn danger small" data-action="delete-sale-record" data-sale-id="${sale.id}">حذف السجل محلياً</button>
      </div>
    </article>
  `;
}

function renderArchive() {
  const archivedEmails = state.emails.filter((email) => email.status === 'archived');
  const archivedMessages = [...state.archivedMessages].sort((a,b) => new Date(b.archivedAt) - new Date(a.archivedAt));
  const recovered = [...state.recoveredProfiles].sort((a,b) => new Date(b.recoveredAt) - new Date(a.recoveredAt));
  const completedEmails = state.emails.filter((email) => email.status === 'completed');
  els.main.innerHTML = `
    <section class="section">
      <div class="section-head"><div><h2>الإيميلات المكتملة</h2><p>نسخة سريعة من الإيميلات التي بيعت بروفايلاتها الخمسة</p></div></div>
      <div class="archive-list">
        ${completedEmails.length ? completedEmails.map((email) => `<article class="card list-card"><h3>${escapeHTML(email.localName || email.address)}</h3><p>${escapeHTML(email.address)}</p><p>تاريخ الإنشاء: ${formatDate(email.createdAt)} · تاريخ الاكتمال: ${formatDate(email.completedAt)}</p><button class="btn primary small" data-action="open-email" data-email-id="${email.localId}" data-return-view="archive">فتح المعلومات والملفات</button></article>`).join('') : emptyState('✅','لا توجد إيميلات مكتملة','ستظهر هنا الإيميلات التي تم بيع ملفاتها الخمسة.')}
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><h2>الإيميلات المؤرشفة</h2><p>يمكن استرجاعها أو حذفها محلياً</p></div></div>
      <div class="archive-list">
        ${archivedEmails.length ? archivedEmails.map((email) => `
          <article class="card list-card"><h3>${escapeHTML(email.localName || email.address)}</h3><p>${escapeHTML(email.address)}</p><p>تاريخ الإنشاء: ${formatDate(email.createdAt)} · تاريخ الأرشفة: ${formatDate(email.archivedAt)}</p>
          <div class="btn-row"><button class="btn success small" data-action="restore-email" data-email-id="${email.localId}">استرجاع</button><button class="btn danger small" data-action="delete-email" data-email-id="${email.localId}">حذف</button></div></article>
        `).join('') : emptyState('📦', 'لا توجد إيميلات مؤرشفة', 'الإيميلات التي تؤرشفها تظهر هنا.')}
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><h2>الرسائل المؤرشفة محلياً</h2><p>الأرشفة هنا محلية داخل هذا المتصفح</p></div></div>
      <div class="archive-list">
        ${archivedMessages.length ? archivedMessages.map((message) => `
          <article class="card list-card"><h3>${escapeHTML(message.subject || 'بدون عنوان')}</h3><p>${escapeHTML(message.fromName || message.fromAddress || 'مرسل غير معروف')} · ${formatDate(message.createdAt)}</p><p>${escapeHTML(message.emailAddress || '')}</p>
          <div class="btn-row"><button class="btn soft small" data-action="view-archived-message" data-archive-id="${message.id}">عرض</button><button class="btn success small" data-action="restore-archived-message" data-archive-id="${message.id}">استرجاع</button><button class="btn danger small" data-action="delete-archived-message" data-archive-id="${message.id}">حذف محلي</button></div></article>
        `).join('') : emptyState('🗂️', 'لا توجد رسائل مؤرشفة', 'يمكنك أرشفة أي رسالة من صندوق الوارد.')}
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><h2>البروفايلات المسترجعة</h2><p>سجل محلي لعمليات استرجاع البروفايلات المباعة</p></div></div>
      <div class="archive-list">
        ${recovered.length ? recovered.map((record) => `<article class="card list-card"><h3>البروفايل ${record.profileNumber} · ${escapeHTML(record.emailAddress)}</h3><p>تم الاسترجاع: ${formatDate(record.recoveredAt)}</p><button class="btn danger small" data-action="delete-recovery-record" data-recovery-id="${record.id}">حذف السجل</button></article>`).join('') : emptyState('↩️', 'لا توجد عمليات استرجاع', 'عند استرجاع بروفايل مباع سيظهر هنا.')}
      </div>
    </section>
  `;
}

function renderSettings() {
  const s = state.settings;
  els.main.innerHTML = `
    <section class="section">
      <div class="section-head"><div><h2>الإعدادات</h2><p>إعدادات محلية محفوظة على هذا المتصفح</p></div></div>
      <div class="card settings-list">
        <div class="setting-row"><div class="setting-label"><span class="setting-icon">◐</span><div><b>المظهر</b><small>داكن، فاتح أو تلقائي</small></div></div><select class="select" style="width:145px" data-setting-select="theme"><option value="dark" ${s.theme==='dark'?'selected':''}>داكن</option><option value="light" ${s.theme==='light'?'selected':''}>فاتح</option><option value="auto" ${s.theme==='auto'?'selected':''}>تلقائي</option></select></div>
        <div class="setting-row"><div class="setting-label"><span class="setting-icon">●</span><div><b>لون الواجهة</b><small>اختر اللون الأساسي</small></div></div><div class="color-dots">${['#0A84FF','#5E5CE6','#BF5AF2','#FF375F','#FF9F0A','#30D158'].map(c=>`<button class="color-dot ${s.accent===c?'active':''}" style="background:${c}" data-action="set-accent" data-color="${c}" aria-label="${c}"></button>`).join('')}</div></div>
        ${settingSwitch('showPasswords','إظهار كلمات المرور','تبقى مخفية افتراضياً',s.showPasswords,'◉')}
        <div class="setting-row"><div class="setting-label"><span class="setting-icon">⏱</span><div><b>مدة انتظار الرسائل</b><small>الفحص كل 5 ثوانٍ وبحد أقصى دقيقة</small></div></div><select class="select" style="width:120px" data-setting-select="waitDuration"><option value="30" ${s.waitDuration===30?'selected':''}>30 ثانية</option><option value="45" ${s.waitDuration===45?'selected':''}>45 ثانية</option><option value="60" ${s.waitDuration===60?'selected':''}>60 ثانية</option></select></div>
        ${settingSwitch('sound','صوت وصول الرسالة','تشغيل نغمة قصيرة عند وصول الكود',s.sound,'♫')}
        ${settingSwitch('vibration','اهتزاز الهاتف','عند وصول رسالة أثناء الانتظار',s.vibration,'⌁')}
        ${settingSwitch('lockEnabled','قفل الموقع برمز PIN','رمز من 4 إلى 6 أرقام',s.lockEnabled,'🔒', true)}
        <button class="setting-row" data-action="change-profile-icons-global" style="width:100%;border:0;color:inherit;text-align:right;cursor:pointer"><div class="setting-label"><span class="setting-icon">☺</span><div><b>تغيير رموز البروفايلات</b><small>تطبيق شكل موحد على جميع الإيميلات</small></div></div><span>‹</span></button>
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><h2>النسخ الاحتياطي</h2><p>احمِ بياناتك من فقدان بيانات المتصفح</p></div></div>
      <div class="card settings-list">
        <button class="setting-row" data-action="export-backup" style="width:100%;border:0;color:inherit;text-align:right;cursor:pointer"><div class="setting-label"><span class="setting-icon">⇩</span><div><b>تصدير نسخة احتياطية</b><small>تنزيل جميع البيانات داخل ملف JSON</small></div></div><span>‹</span></button>
        <button class="setting-row" data-action="import-backup" style="width:100%;border:0;color:inherit;text-align:right;cursor:pointer"><div class="setting-label"><span class="setting-icon">⇧</span><div><b>استيراد نسخة احتياطية</b><small>استبدال البيانات الحالية بعد التحقق</small></div></div><span>‹</span></button>
        <button class="setting-row" data-action="clear-all-data" style="width:100%;border:0;color:var(--danger);text-align:right;cursor:pointer"><div class="setting-label"><span class="setting-icon">⌫</span><div><b>مسح جميع البيانات</b><small>يتطلب تأكيداً مرتين</small></div></div><span>‹</span></button>
      </div>
    </section>

    <div class="notice">البيانات محفوظة على هذا الجهاز والمتصفح فقط. مسح بيانات المتصفح سيؤدي إلى فقدانها ما لم يتم تصدير نسخة احتياطية.</div>

    <section class="section" style="margin-top:18px">
      <div class="card settings-list">
        <button class="setting-row" data-action="api-info" style="width:100%;border:0;color:inherit;text-align:right;cursor:pointer"><div class="setting-label"><span class="setting-icon">API</span><div><b>معلومات API</b><small>طريقة الاتصال بخدمة Generator.email</small></div></div><span>‹</span></button>
        <button class="setting-row" data-action="about-app" style="width:100%;border:0;color:inherit;text-align:right;cursor:pointer"><div class="setting-label"><span class="setting-icon">i</span><div><b>حول الموقع</b><small>نسخة محلية ثابتة بدون Backend</small></div></div><span>‹</span></button>
      </div>
    </section>
  `;
}

function settingSwitch(key, title, subtitle, on, icon, pin = false) {
  return `<div class="setting-row"><div class="setting-label"><span class="setting-icon">${icon}</span><div><b>${title}</b><small>${subtitle}</small></div></div><button class="switch ${on?'on':''}" data-action="toggle-setting" data-setting="${key}" ${pin?'data-pin-setting="true"':''} aria-pressed="${on}"></button></div>`;
}

function openProfileSheet(emailId, profileNumber) {
  const email = getEmail(emailId);
  const profile = getProfile(email, profileNumber);
  if (!email || !profile) return;
  state.lastProfileRef = { emailId, profileNumber: profile.number };
  saveState({ silent: true });
  openSheet(`
    <div class="sheet-title"><h2>البروفايل ${profile.number}</h2><button class="close-btn" data-action="close-sheet">×</button></div>
    <div class="profile-sheet-visual">${profileVisual(profile)}</div>
    <div class="profile-quick-actions">
      <button class="btn primary wide" data-action="copy-text" data-copy="${escapeHTML(profile.pin)}">نسخ الرمز</button>
      <button class="btn soft wide" data-action="copy-text" data-copy="${escapeHTML(email.address)}">نسخ الإيميل</button>
      <button type="button" class="btn ghost wide" data-action="fetch-code" data-email-id="${email.localId}">جلب الكود والروابط</button>
    </div>
    <div class="simple-status-block">
      <span>الحالة الحالية: <strong>${statusLabel(profile.status)}</strong></span>
      ${profileStatusActions(email, profile)}
    </div>
  `);
}

function profileStatusActions(email, profile) {
  if (profile.status === 'available') {
    return `<div class="simple-status-actions"><button class="btn warning wide" data-action="mark-review" data-email-id="${email.localId}" data-profile-number="${profile.number}">قيد المراجعة</button><button class="btn success wide" data-action="mark-sold" data-email-id="${email.localId}" data-profile-number="${profile.number}">تم البيع</button></div>`;
  }
  if (profile.status === 'review') {
    return `<div class="simple-status-actions"><button class="btn success wide" data-action="mark-sold" data-email-id="${email.localId}" data-profile-number="${profile.number}">تم البيع</button><button class="btn ghost wide" data-action="cancel-review" data-email-id="${email.localId}" data-profile-number="${profile.number}">إلغاء المراجعة</button></div>`;
  }
  return `<div class="simple-status-actions"><button class="btn warning wide" data-action="restore-profile" data-email-id="${email.localId}" data-profile-number="${profile.number}">استرجاع البروفايل</button></div>`;
}

function openProfileEditor(emailId = null, globalMode = false) {
  let source;
  if (globalMode) {
    source = state.globalProfileStyles || [1,2,3,4,5].map((n) => {
      const p = createDefaultProfile(n);
      return { number:n, color:p.color, icon:p.icon, imageData:p.imageData };
    });
  } else {
    const email = getEmail(emailId);
    if (!email) return;
    source = email.profiles.map(({ number, color, icon, imageData }) => ({ number, color, icon, imageData }));
  }
  ui.profileEditor = { emailId, globalMode, drafts: clone(source) };
  renderProfileEditorSheet();
}

function renderProfileEditorSheet() {
  const editor = ui.profileEditor;
  if (!editor) return;
  openSheet(`
    <div class="sheet-title"><div><h2>تغيير رموز البروفايلات</h2><p class="helper">يمكنك تغيير اللون أو الرمز أو رفع صورة مضغوطة من الهاتف.</p></div><button class="close-btn" data-action="close-sheet">×</button></div>
    <div class="profile-editor-grid">
      ${editor.drafts.sort((a,b)=>a.number-b.number).map((draft) => `
        <div class="card profile-editor">
          <strong>البروفايل ${draft.number}</strong>
          <div style="margin:10px 0">${profileVisual(draft)}</div>
          <div class="field"><label>اللون</label><input type="color" value="${escapeHTML(draft.color)}" data-profile-color="${draft.number}" style="width:100%;height:42px;border:0;border-radius:11px;background:transparent"></div>
          <div class="field"><label>رمز جاهز</label><div class="icon-picker">${READY_ICONS.map(icon=>`<button class="icon-choice ${draft.icon===icon && !draft.imageData?'active':''}" data-action="choose-profile-icon" data-profile-number="${draft.number}" data-icon="${icon}">${icon==='face'?'☺':icon==='star'?'★':icon==='crown'?'♛':icon==='heart'?'♥':'ϟ'}</button>`).join('')}</div></div>
          <div class="btn-row" style="margin-top:9px"><button class="btn soft small" data-action="upload-profile-image" data-profile-number="${draft.number}">رفع صورة</button>${draft.imageData?`<button class="btn danger small" data-action="remove-profile-image" data-profile-number="${draft.number}">حذف الصورة</button>`:''}</div>
          <button class="btn ghost small wide" style="margin-top:7px" data-action="reset-profile-style" data-profile-number="${draft.number}">استرجاع الافتراضي</button>
        </div>
      `).join('')}
    </div>
    <div class="modal-actions">
      ${editor.globalMode ? `<button class="btn primary wide" data-action="apply-profile-styles-all">تطبيق على جميع الإيميلات</button>` : `<button class="btn primary wide" data-action="apply-profile-styles-current">تطبيق على هذا الإيميل فقط</button><button class="btn soft wide" data-action="apply-profile-styles-all">تطبيق على جميع الإيميلات</button>`}
    </div>
  `);
}

function renderInboxSheet() {
  const email = getEmail(ui.inboxEmailId);
  if (!email) return;
  const archivedIds = new Set(email.archivedMessageIds || []);
  let list;
  if (ui.inboxFilter === 'archived') {
    list = state.archivedMessages.filter((m) => m.emailId === email.localId).map((m) => ({
      id: m.messageId,
      from: { name: m.fromName, address: m.fromAddress },
      subject: m.subject,
      intro: m.intro,
      createdAt: m.createdAt,
      seen: m.seen,
      _archiveId: m.id,
      _localArchive: true
    }));
  } else {
    list = ui.inboxMessages.filter((message) => !archivedIds.has(message.id));
    if (ui.inboxFilter === 'unread') list = list.filter((message) => !message.seen);
  }
  const query = ui.inboxSearch.trim().toLowerCase();
  if (query) list = list.filter((m) => `${m.from?.name || ''} ${m.from?.address || ''} ${m.subject || ''}`.toLowerCase().includes(query));
  const pollingText = polling && polling.emailId === email.localId ? `جارٍ الانتظار... ${Math.max(0, Math.ceil((polling.deadline - Date.now()) / 1000))} ثانية` : 'انتظار وصول الكود';
  openSheet(`
    <div data-inbox-root>
      <div class="sheet-title"><div><h2>صندوق الوارد</h2><p class="helper">${escapeHTML(email.address)}</p></div><button class="close-btn" data-action="close-sheet">×</button></div>
      <div class="btn-row" style="margin-bottom:10px">
        <button class="btn primary small" data-action="refresh-messages" data-email-id="${email.localId}" ${ui.inboxLoading?'disabled':''}>${ui.inboxLoading?'<span class="spinner"></span> جاري التحديث':'تحديث'}</button>
        ${polling && polling.emailId === email.localId ? `<button class="btn danger small" data-action="stop-waiting">إيقاف الانتظار</button>` : `<button class="btn warning small" data-action="start-waiting" data-email-id="${email.localId}">${pollingText}</button>`}
      </div>
      <div class="segmented">
        <button class="${ui.inboxFilter==='all'?'active':''}" data-action="set-inbox-filter" data-filter="all">الكل</button>
        <button class="${ui.inboxFilter==='unread'?'active':''}" data-action="set-inbox-filter" data-filter="unread">غير المقروءة</button>
      </div>
      <button class="btn ghost wide" style="margin-bottom:10px" data-action="set-inbox-filter" data-filter="archived">الرسائل المؤرشفة محلياً</button>
      <div class="searchbar"><span>⌕</span><input class="input" data-inbox-search value="${escapeHTML(ui.inboxSearch)}" placeholder="البحث باسم المرسل أو عنوان الرسالة"></div>
      <div class="message-list">
        ${ui.inboxLoading ? '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>' : list.length ? list.map(messageCard).join('') : emptyState('📭', ui.inboxFilter==='archived'?'لا توجد رسائل مؤرشفة':'لا توجد رسائل حالياً', ui.inboxFilter==='archived'?'الرسائل المؤرشفة محلياً ستظهر هنا.':'اضغط تحديث أو شغّل انتظار وصول الكود.')}
      </div>
      <p class="helper" style="text-align:center;margin-top:12px">اسحب النافذة إلى الأسفل من أعلى القائمة لتحديث الرسائل.</p>
    </div>
  `);
}

function messageCard(message) {
  return `
    <article class="card list-card">
      <div class="email-top"><div><h3>${escapeHTML(message.subject || 'بدون عنوان')}</h3><p>${escapeHTML(message.from?.name || message.from?.address || 'مرسل غير معروف')}</p></div><span class="tag">${message.seen ? 'مقروءة' : 'جديدة'}</span></div>
      <p>${formatDate(message.createdAt)}${message.intro ? ` · ${escapeHTML(message.intro)}` : ''}</p>
      <div class="btn-row">
        <button class="btn primary small" data-action="open-message" data-message-id="${escapeHTML(message.id)}" ${message._localArchive?`data-archive-id="${message._archiveId}"`:''}>عرض الرسالة</button>
        ${message._localArchive ? `<button class="btn success small" data-action="restore-archived-message" data-archive-id="${message._archiveId}">استرجاع</button>` : `<button class="btn ghost small" data-action="archive-message" data-message-id="${escapeHTML(message.id)}">أرشفة محلياً</button>`}
      </div>
    </article>
  `;
}

function renderMessageDetailSheet(detail, email, archiveRecord = null) {
  const code = extractVerificationCode(detail);
  const text = detail.text || detail.intro || '';
  const safeHtml = detail.html ? sanitizeHtml(Array.isArray(detail.html) ? detail.html.join('\n') : detail.html) : '';
  const fromName = detail.from?.name || archiveRecord?.fromName || '';
  const fromAddress = detail.from?.address || archiveRecord?.fromAddress || '';
  openSheet(`
    <div class="sheet-title"><button class="back-btn" data-action="back-to-inbox">‹</button><div style="flex:1"><h2>${escapeHTML(detail.subject || 'بدون عنوان')}</h2><p class="helper">${escapeHTML(email?.address || archiveRecord?.emailAddress || '')}</p></div><button class="close-btn" data-action="close-sheet">×</button></div>
    <div class="card" style="padding:13px">
      <div class="info-line"><span>اسم المرسل</span><strong>${escapeHTML(fromName || 'غير معروف')}</strong></div>
      <div class="info-line"><span>بريد المرسل</span><strong>${escapeHTML(fromAddress || 'غير معروف')}</strong></div>
      <div class="info-line"><span>وقت الوصول</span><strong>${formatDate(detail.createdAt || archiveRecord?.createdAt)}</strong></div>
      <div class="info-line"><span>حالة القراءة</span><strong>${detail.seen ? 'مقروءة' : 'غير مقروءة'}</strong></div>
    </div>
    ${code ? `<div class="code-card"><small>كود التحقق المستخرج</small><strong>${escapeHTML(code)}</strong><button class="btn" data-action="copy-text" data-copy="${escapeHTML(code)}">نسخ الكود</button></div>` : '<div class="notice" style="margin:13px 0">لم يتم العثور على كود واضح من 4 إلى 8 أرقام داخل الرسالة.</div>'}
    ${text ? `<h3>نص الرسالة</h3><div class="message-text">${escapeHTML(text)}</div>` : ''}
    ${safeHtml ? `<h3>محتوى HTML الآمن</h3><div class="message-html">${safeHtml}</div>` : ''}
    <div class="btn-row" style="margin-top:14px">
      ${code ? `<button class="btn primary" data-action="copy-text" data-copy="${escapeHTML(code)}">نسخ الكود</button>` : ''}
      <button class="btn soft" data-action="copy-text" data-copy="${escapeHTML(text || stripHtml(safeHtml))}">نسخ الرسالة</button>
      ${archiveRecord ? `<button class="btn success" data-action="restore-archived-message" data-archive-id="${archiveRecord.id}">استرجاع من الأرشيف</button>` : `
        <button class="btn ghost" data-action="refresh-current-message" data-message-id="${escapeHTML(detail.id)}">تحديث</button>
        ${!detail.seen ? `<button class="btn ghost" data-action="mark-message-seen" data-message-id="${escapeHTML(detail.id)}">تعليم كمقروء</button>` : ''}
        <button class="btn ghost" data-action="archive-message" data-message-id="${escapeHTML(detail.id)}">أرشفة محلياً</button>
        <button class="btn danger" data-action="delete-message" data-message-id="${escapeHTML(detail.id)}">حذف الرسالة</button>
      `}
    </div>
  `);
}

function stripHtml(html = '') {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return doc.body.textContent || '';
}

function sanitizeHtml(html = '') {
  const doc = new DOMParser().parseFromString(String(html), 'text/html');
  const forbidden = ['script','style','iframe','object','embed','link','meta','base','form','input','button','textarea','select','svg','math','video','audio','canvas','img'];
  doc.querySelectorAll(forbidden.join(',')).forEach((el) => el.remove());
  doc.body.querySelectorAll('*').forEach((el) => {
    [...el.attributes].forEach((attr) => {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim().toLowerCase();
      if (name.startsWith('on') || name === 'style' || name === 'srcdoc') el.removeAttribute(attr.name);
      if (['href','src','xlink:href'].includes(name) && (value.startsWith('javascript:') || value.startsWith('data:text/html'))) el.removeAttribute(attr.name);
      if (name === 'target') el.setAttribute('rel', 'noopener noreferrer');
    });
  });
  return doc.body.innerHTML;
}

function extractVerificationCode(detail) {
  const candidates = [];
  const addCandidate = (value, priority) => {
    if (value === null || value === undefined) return;
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    const matches = text.match(/(?<!\d)\d{4,8}(?!\d)/g) || [];
    for (const code of matches) candidates.push({ code, priority, index: text.indexOf(code), text });
  };
  addCandidate(detail.verifications, 100);
  addCandidate(detail.subject, 80);
  addCandidate(detail.text, 60);
  addCandidate(detail.intro, 55);
  addCandidate(Array.isArray(detail.html) ? detail.html.join(' ') : stripHtml(detail.html || ''), 45);

  const yearNow = new Date().getFullYear();
  const scored = candidates.map((item) => {
    let score = item.priority;
    const num = Number(item.code);
    if (item.code.length === 6) score += 18;
    if (item.code.length === 4) score += 8;
    if (num >= 1900 && num <= yearNow + 10) score -= 55;
    const around = item.text.slice(Math.max(0, item.index - 35), item.index + item.code.length + 35).toLowerCase();
    if (/code|otp|verify|verification|رمز|كود|تحقق|تأكيد/.test(around)) score += 30;
    if (/phone|tel|هاتف|واتساب|whatsapp/.test(around)) score -= 25;
    return { ...item, score };
  }).filter((item) => item.score > 20).sort((a,b) => b.score - a.score);
  return scored[0]?.code || '';
}

function extractFourDigitCode(detail) {
  const sources = [
    { value: detail?.verifications, priority: 100 },
    { value: detail?.subject, priority: 80 },
    { value: detail?.text, priority: 70 },
    { value: detail?.intro, priority: 60 },
    { value: Array.isArray(detail?.html) ? detail.html.join(' ') : stripHtml(detail?.html || ''), priority: 50 }
  ];
  const currentYear = new Date().getFullYear();
  const candidates = [];

  for (const source of sources) {
    if (source.value === null || source.value === undefined) continue;
    const text = typeof source.value === 'string' ? source.value : JSON.stringify(source.value);
    for (const match of text.matchAll(/(?<!\d)(\d{4})(?!\d)/g)) {
      const code = match[1];
      const number = Number(code);
      const around = text.slice(Math.max(0, match.index - 40), match.index + 44).toLowerCase();
      let score = source.priority;
      if (/code|otp|verify|verification|رمز|كود|تحقق|تأكيد|security/.test(around)) score += 35;
      if (/phone|tel|mobile|هاتف|واتساب|whatsapp/.test(around)) score -= 30;
      if (number >= 1900 && number <= currentYear + 5) score -= 70;
      candidates.push({ code, score });
    }
  }
  candidates.sort((a, b) => b.score - a.score);
  return candidates.find((item) => item.score > 20)?.code || '';
}


function decodeHtmlEntities(value = '') {
  const textarea = document.createElement('textarea');
  textarea.innerHTML = String(value);
  return textarea.value;
}

function normalizeExtractedUrl(value = '') {
  let url = decodeHtmlEntities(value)
    .trim()
    .replace(/^[\s<({"']+/, '')
    .replace(/[\s>)}"'.,;!?،؛]+$/, '');
  if (/^www\./i.test(url)) url = `https://${url}`;
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) return '';
    return parsed.href;
  } catch (_) {
    return '';
  }
}

function extractMessageLinks(detail) {
  const links = [];
  const add = (value) => {
    const normalized = normalizeExtractedUrl(value);
    if (normalized && !links.includes(normalized)) links.push(normalized);
  };

  const htmlParts = Array.isArray(detail?.html) ? detail.html : [detail?.html || ''];
  for (const html of htmlParts) {
    if (!html) continue;
    const doc = new DOMParser().parseFromString(String(html), 'text/html');
    doc.querySelectorAll('a[href]').forEach((anchor) => add(anchor.getAttribute('href')));
  }

  const sources = [
    detail?.subject,
    detail?.text,
    detail?.intro,
    detail?.verifications,
    ...htmlParts
  ];
  const urlRegex = /(?:https?:\/\/|www\.)[^\s<>"']+/gi;
  for (const source of sources) {
    if (source === null || source === undefined) continue;
    const text = typeof source === 'string' ? source : JSON.stringify(source);
    for (const match of text.matchAll(urlRegex)) add(match[0]);
  }

  return links.slice(0, 12);
}

function linkDisplayText(url) {
  try {
    const parsed = new URL(url);
    const path = `${parsed.pathname || ''}${parsed.search || ''}`;
    const compact = `${parsed.hostname}${path === '/' ? '' : path}`;
    return compact.length > 70 ? `${compact.slice(0, 67)}…` : compact;
  } catch (_) {
    return url.length > 70 ? `${url.slice(0, 67)}…` : url;
  }
}

function renderFetchedLinks(links = []) {
  if (!links.length) return '';
  return `
    <div class="fetched-links-block">
      <h3>الروابط الموجودة في الرسالة</h3>
      <div class="fetched-links-list">
        ${links.map((url, index) => `
          <article class="fetched-link-card">
            <div class="fetched-link-info">
              <span>الرابط ${index + 1}</span>
              <b dir="ltr">${escapeHTML(linkDisplayText(url))}</b>
            </div>
            <div class="fetched-link-actions">
              <a class="btn primary" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">فتح الرابط</a>
              <button class="btn soft" data-action="copy-text" data-copy="${escapeHTML(url)}">نسخ الرابط</button>
            </div>
          </article>
        `).join('')}
      </div>
    </div>
  `;
}

async function fetchJson(url, options = {}, timeoutMs = 22000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    let data = null;
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('json')) {
      try { data = await response.json(); } catch (_) { data = null; }
    } else {
      try { data = await response.text(); } catch (_) { data = null; }
    }
    if (!response.ok) {
      const error = new Error(apiErrorMessage(response.status, data));
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return { data, response };
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('انتهت مهلة الاتصال بالخدمة. حاول مرة أخرى.');
    if (error instanceof TypeError) throw new Error('تعذر الاتصال بالخدمة. تحقق من اتصال الإنترنت أو افتح الموقع من استضافة HTTPS.');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function apiErrorMessage(status, data) {
  const server = data?.['hydra:description'] || data?.detail || data?.message || '';
  if (status === 401) return 'انتهت جلسة الإيميل أو كلمة المرور غير صحيحة.';
  if (status === 404) return 'العنصر المطلوب غير موجود في خدمة البريد.';
  if (status === 422) return server || 'البيانات غير صالحة أو الإيميل مستخدم مسبقاً.';
  if (status === 429) return 'تم تجاوز عدد الطلبات. انتظر قليلاً ثم حاول مرة أخرى.';
  if (status >= 500) return 'خدمة البريد تواجه مشكلة مؤقتة. حاول لاحقاً.';
  return server || `حدث خطأ من API برمز ${status}.`;
}

async function refreshEmailToken(email, showSuccess = false) {
  if (!isMailTmEmail(email)) throw new Error('هذا إيميل عادي ولا يرتبط بخدمة البريد المؤقت.');
  if (!email?.address || !email?.password) throw new Error('لا توجد كلمة مرور محفوظة لهذا الإيميل.');
  const apiBase = getEmailApiBase(email);
  const { data } = await fetchJson(`${apiBase}/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: JSON.stringify({ address: email.address, password: email.password })
  });
  if (!data?.token) throw new Error('لم يتم استخراج Token جديد. تحقق من كلمة المرور.');
  email.token = data.token;
  if (!email.mailTmId && data.id) email.mailTmId = data.id;
  saveState();
  if (showSuccess) toast('تم تحديث رمز الدخول.');
  return data.token;
}

async function apiForEmail(email, path, options = {}, retry401 = true) {
  if (!isMailTmEmail(email)) throw new Error('هذا إيميل عادي ولا يمكن جلب رسائله عبر API.');
  const headers = new Headers(options.headers || {});
  headers.set('Accept', 'application/json');
  if (email.token) headers.set('Authorization', `Bearer ${email.token}`);
  try {
    const apiBase = getEmailApiBase(email);
    return await fetchJson(`${apiBase}${path}`, { ...options, headers });
  } catch (error) {
    if (error.status === 401 && retry401) {
      try {
        await refreshEmailToken(email, false);
      } catch (_) {
        throw new Error('انتهت جلسة الإيميل، تحقق من كلمة المرور.');
      }
      return apiForEmail(email, path, options, false);
    }
    throw error;
  }
}

function randomPassword(length = 20) {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%';
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (n) => alphabet[n % alphabet.length]).join('');
}

function randomUsername() {
  const bytes = new Uint32Array(1);
  crypto.getRandomValues(bytes);
  const random = String(bytes[0] % 100000).padStart(5, '0');
  return `cd${random}`;
}

function randomGeneratorUsername() {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  for (let tries = 0; tries < 50; tries += 1) {
    const lengthBytes = new Uint32Array(1);
    crypto.getRandomValues(lengthBytes);
    const length = 5 + (lengthBytes[0] % 2); // 5 أو 6 أحرف فقط
    const bytes = new Uint32Array(length);
    crypto.getRandomValues(bytes);
    const value = Array.from(bytes, (n) => chars[n % chars.length]).join('');
    if (/[a-z]/.test(value) && /\d/.test(value)) return value;
  }
  return `a${String(Date.now()).slice(-4)}`;
}

function buildEmailRecord({ mailTmId, address, password, token, provider = 'generator', apiBase = '', createdAt = new Date().toISOString() }) {
  const profiles = [1,2,3,4,5].map(createDefaultProfile);
  if (state.globalProfileStyles) {
    profiles.forEach((profile) => {
      const style = state.globalProfileStyles.find((item) => item.number === profile.number);
      if (style) Object.assign(profile, { color: style.color, icon: style.icon, imageData: style.imageData || '' });
    });
  }
  return {
    localId: makeId(), mailTmId: mailTmId || '', address, password: password || '', token: token || '', provider,
    apiBase: apiBase || (provider === 'mailgw' ? 'https://api.mail.gw' : provider === 'mailtm' ? 'https://api.mail.tm' : ''),
    generatorDomain: provider === 'generator' ? (emailDomain(address) || GENERATOR_DOMAIN) : '',
    generatorSeenIds: [], generatorDeletedIds: [],
    localName: '', createdAt, status: 'active', archivedAt: null, completedAt: null,
    archivedMessageIds: [], profiles
  };
}

function extractDomainItems(data) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== 'object') return [];
  if (Array.isArray(data['hydra:member'])) return data['hydra:member'];
  if (Array.isArray(data.member)) return data.member;
  if (Array.isArray(data.domains)) return data.domains;
  if (Array.isArray(data.data)) return data.data;
  return [];
}

function isUsableMailTmDomain(item) {
  if (!item?.domain) return false;
  const active = ![false, 0, '0', 'false'].includes(item.isActive);
  const isPrivate = [true, 1, '1', 'true'].includes(item.isPrivate);
  return active && !isPrivate;
}

async function getProviderDomains(provider) {
  const found = [];
  let lastError = null;
  for (let page = 1; page <= 3; page += 1) {
    try {
      const { data } = await fetchJson(`${provider.apiBase}/domains?page=${page}&_=${Date.now()}`, {
        headers: { 'Accept': 'application/ld+json, application/json' },
        cache: 'no-store'
      });
      const batch = extractDomainItems(data)
        .filter(isUsableMailTmDomain)
        .map((item) => String(item.domain).trim().toLowerCase())
        .filter(Boolean);
      found.push(...batch);
      if (!batch.length) break;
    } catch (error) {
      lastError = error;
      break;
    }
  }
  const domains = [...new Set(found)];
  if (!domains.length && lastError) throw lastError;
  return domains;
}


function isKnownGeneratorDomain(domain = '') {
  const value = String(domain || '').trim().toLowerCase();
  if (!value) return false;
  return LEGACY_GENERATOR_DOMAINS.includes(value)
    || GENERATOR_FALLBACK_DOMAINS.includes(value)
    || generatorDomainCache.domains.includes(value);
}

function extractGeneratorDomainsFromHtml(html = '') {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  const found = [];
  const add = (raw) => {
    const value = String(raw || '').trim().toLowerCase().replace(/^@/, '');
    if (!/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$/i.test(value)) return;
    if (['generator.email', 'emailfake.com', 'email-fake.com', 'mail-temp.com', 'tempm.com', 'corsproxy.io'].includes(value)) return;
    if (!found.includes(value)) found.push(value);
  };

  doc.querySelectorAll('option').forEach((option) => {
    add(option.value);
    add(option.textContent);
  });
  doc.querySelectorAll('[data-domain]').forEach((node) => add(node.getAttribute('data-domain')));

  // احتياط في حال غيّر الموقع تركيب قائمة النطاقات.
  if (!found.length) {
    const text = doc.body?.innerText || String(html || '');
    const matches = text.match(/\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\b/gi) || [];
    matches.forEach(add);
  }
  return found.slice(0, 40);
}

async function fetchLiveGeneratorDomains(force = false) {
  const freshEnough = generatorDomainCache.domains.length && (Date.now() - generatorDomainCache.fetchedAt) < 15 * 60 * 1000;
  if (!force && freshEnough) return generatorDomainCache.domains;

  let lastError = null;
  for (const home of GENERATOR_HOME_PAGES) {
    const fresh = `${home}${home.includes('?') ? '&' : '?'}_=${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const candidates = [
      `${GENERATOR_CORS_PROXY}${encodeURIComponent(fresh)}`,
      fresh
    ];
    for (const url of candidates) {
      try {
        const html = await fetchTextWithTimeout(url, { headers: { 'Accept': 'text/html,application/xhtml+xml' } }, 18000);
        const domains = extractGeneratorDomainsFromHtml(html)
          .filter((domain) => !LEGACY_GENERATOR_DOMAINS.includes(domain));
        if (domains.length) {
          generatorDomainCache = { domains, fetchedAt: Date.now() };
          return domains;
        }
      } catch (error) {
        lastError = error;
      }
    }
  }

  if (GENERATOR_FALLBACK_DOMAINS.length) {
    generatorDomainCache = { domains: [...GENERATOR_FALLBACK_DOMAINS], fetchedAt: Date.now() };
    return generatorDomainCache.domains;
  }
  throw new Error(lastError?.message || 'تعذر الحصول على نطاق بريد فعال حالياً.');
}

async function getActiveGeneratorDomain() {
  const domains = await fetchLiveGeneratorDomains(false);
  if (!domains.length) throw new Error('لا يوجد نطاق بريد فعال حالياً. حاول لاحقاً.');
  // استخدم نطاقاً من أوائل القائمة الحية وبدّل بينها لتقليل الاعتماد على نطاق واحد.
  const pool = domains.slice(0, Math.min(domains.length, 8));
  const bytes = new Uint32Array(1);
  crypto.getRandomValues(bytes);
  return pool[bytes[0] % pool.length];
}

async function createMailAccount() {
  if (isBusy('create-email')) return;
  setBusy('create-email', true);
  renderHome();
  try {
    let address = '';
    for (let attempt = 0; attempt < 40; attempt += 1) {
      // مطابق لفكرة كود Python: اسم 5 أو 6 خانات + النطاق الثابت 5xu.vn.
      const candidate = `${randomGeneratorUsername()}@${GENERATOR_DOMAIN}`;
      if (!state.emails.some((email) => email.address.toLowerCase() === candidate.toLowerCase())) {
        address = candidate;
        break;
      }
    }
    if (!address) throw new Error('تعذر إنشاء اسم إيميل فريد. حاول مرة أخرى.');

    // Generator.email لا يحتاج تسجيل حساب أو كلمة مرور.
    const email = buildEmailRecord({
      address,
      password: '',
      token: '',
      provider: 'generator',
      apiBase: '',
      createdAt: new Date().toISOString()
    });
    email.generatorDomain = GENERATOR_DOMAIN;
    state.emails.unshift(email);
    state.lastEmailId = email.localId;
    if (!saveState()) return;
    toast('تم إنشاء الإيميل بنجاح.');
    await copyText(address, false);
    ui.selectedEmailId = email.localId;
    ui.returnView = 'home';
    ui.view = 'email-detail';
  } catch (error) {
    toast(error.message || 'تعذر إنشاء الإيميل.', 'error', 6500);
  } finally {
    setBusy('create-email', false);
    render();
  }
}

async function saveExistingAccount() {
  const address = document.getElementById('existingEmail')?.value.trim().toLowerCase();
  const password = document.getElementById('existingPassword')?.value || '';
  const errorEl = document.getElementById('existingError');
  if (!address || !password) {
    if (errorEl) errorEl.textContent = 'أدخل البريد الإلكتروني وكلمة المرور.';
    return;
  }
  if (state.emails.some((email) => email.address.toLowerCase() === address)) {
    if (errorEl) errorEl.textContent = 'هذا الإيميل محفوظ مسبقاً.';
    return;
  }
  const button = document.querySelector('[data-action="save-existing-account"]');
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span> جاري التحقق...'; }
  try {
    const provider = getProvider('mailtm');
    const { data: tokenData } = await fetchJson(`${provider.apiBase}/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ address, password })
    });
    if (!tokenData?.token) throw new Error('لم يتم استخراج Token من الحساب.');
    let account = {};
    try {
      const result = await fetchJson(`${provider.apiBase}/me`, { headers: { 'Authorization': `Bearer ${tokenData.token}`, 'Accept': 'application/json' }, cache: 'no-store' });
      account = result.data || {};
    } catch (_) { account = {}; }
    const email = buildEmailRecord({ mailTmId: account.id || tokenData.id, address, password, token: tokenData.token, provider: provider.id, apiBase: provider.apiBase, createdAt: account.createdAt || new Date().toISOString() });
    state.emails.unshift(email);
    state.lastEmailId = email.localId;
    saveState();
    closeModal();
    toast('تم التحقق من الحساب وحفظه بنجاح.');
    ui.selectedEmailId = email.localId;
    ui.returnView = 'home';
    ui.view = 'email-detail';
    render();
  } catch (error) {
    if (errorEl) errorEl.textContent = error.status === 401 ? 'البريد الإلكتروني أو كلمة المرور غير صحيحة.' : (error.message || 'تعذر الاتصال بالخدمة.');
    if (button) { button.disabled = false; button.textContent = 'التحقق والحفظ'; }
  }
}

async function saveLocalEmail() {
  const address = document.getElementById('localEmailAddress')?.value.trim().toLowerCase() || '';
  const errorEl = document.getElementById('localEmailError');
  if (!isValidEmailAddress(address)) {
    if (errorEl) errorEl.textContent = 'أدخل بريداً إلكترونياً صحيحاً، مثل nhyffga@hi2.in';
    return;
  }
  if (state.emails.some((email) => email.address.toLowerCase() === address)) {
    if (errorEl) errorEl.textContent = 'هذا الإيميل محفوظ مسبقاً.';
    return;
  }

  const provider = isKnownGeneratorDomain(emailDomain(address)) ? 'generator' : 'local';
  const email = buildEmailRecord({
    address,
    password: '',
    token: '',
    mailTmId: '',
    provider,
    createdAt: new Date().toISOString()
  });
  state.emails.unshift(email);
  state.lastEmailId = email.localId;
  if (!saveState()) return;
  closeModal();
  toast('تمت إضافة الإيميل بنجاح.');
  ui.selectedEmailId = email.localId;
  ui.returnView = 'home';
  ui.view = 'email-detail';
  render();
}

async function copyText(text, notify = true) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove();
  }
  if (notify) toast(text.match(/^\d{4,8}$/) ? 'تم نسخ الكود.' : 'تم النسخ بنجاح.');
}

function generatorInboxUrl(email) {
  const address = String(email?.address || '').trim().toLowerCase();
  if (!address || !address.includes('@')) return '';
  // هذا هو رابط الصندوق المطلوب حرفياً:
  // https://generator.email/inbox9/username@5xu.vn
  return `${GENERATOR_BASE}/inbox9/${address}`;
}

function generatorInboxTargets(email) {
  const exact = generatorInboxUrl(email);
  if (!exact) return [];
  return [exact];
}

async function fetchTextWithTimeout(url, options = {}, timeoutMs = 22000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal, cache: 'no-store' });
    if (!response.ok) throw new Error(`فشل جلب صندوق البريد (${response.status}).`);
    return await response.text();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('انتهت مهلة الاتصال بخدمة البريد.');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function isRequestedGeneratorMailboxHtml(html, email) {
  const source = String(html || '');
  if (!source) return false;
  const lower = source.toLowerCase();
  const address = String(email?.address || '').trim().toLowerCase();
  const [username, domain] = address.split('@');

  // عندما توجد رسائل، هذه هي نفس العناصر التي يعتمد عليها كود Python.
  if (/id=["']email-table["']|mess_bodiyy|subj_div_45g45gg|from_div_45g45gg/i.test(source)) return true;

  // عند كون الصندوق فارغاً، يجب أن تكون الصفحة نفسها مرتبطة بعنواننا.
  if (address && lower.includes(address)) return true;
  if (username && domain && lower.includes(username) && lower.includes(domain)) return true;
  return false;
}

async function fetchGeneratorHtml(email) {
  const target = generatorInboxUrl(email);
  if (!target) throw new Error('عنوان الإيميل غير صالح.');

  const freshTarget = `${target}?_=${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let lastError = null;

  // نحاول قراءة نفس رابط inbox9 مباشرة أولاً. إذا منع المتصفح CORS،
  // نقرأ نفس الرابط عبر جسور CORS بدون تغيير عنوان الصندوق نفسه.
  const candidates = [
    freshTarget,
    ...GENERATOR_CORS_BUILDERS.map((build) => build(freshTarget))
  ];

  for (const url of candidates) {
    try {
      const html = await fetchTextWithTimeout(url, {
        headers: { 'Accept': 'text/html,application/xhtml+xml' }
      }, 25000);

      if (isRequestedGeneratorMailboxHtml(html, email)) return html;
      lastError = new Error('تم فتح Generator.email لكن الصفحة التي رجعت ليست صندوق هذا الإيميل.');
    } catch (error) {
      lastError = error;
    }
  }

  throw new Error(lastError?.message || 'تعذر قراءة صندوق Generator.email من المتصفح.');
}

function cleanMessageText(bodyElement) {
  if (!bodyElement) return '';
  const copy = bodyElement.cloneNode(true);
  copy.querySelectorAll('a[href]').forEach((anchor) => {
    const href = normalizeExtractedUrl(anchor.getAttribute('href') || '');
    const title = (anchor.textContent || '').trim();
    if (!href) return;
    const text = document.createTextNode(`\n${title ? `${title}\n` : ''}${href}\n`);
    anchor.replaceWith(text);
  });
  return (copy.textContent || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .join('\n');
}

function stableGeneratorMessageId(sender, subject, body, index) {
  const source = `${sender}|${subject}|${body}|${index}`;
  let hash = 2166136261;
  for (let i = 0; i < source.length; i += 1) {
    hash ^= source.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `gen-${(hash >>> 0).toString(16)}`;
}

function parseGeneratorSender(raw = '') {
  const text = String(raw).trim();
  const angle = text.match(/^(.*?)\s*<([^<>\s]+@[^<>\s]+)>$/);
  if (angle) return { name: angle[1].trim(), address: angle[2].trim() };
  const emailMatch = text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
  return { name: emailMatch ? text.replace(emailMatch[0], '').replace(/[<>]/g, '').trim() : text, address: emailMatch?.[0] || '' };
}

function parseGeneratorMessages(html, email) {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  const table = doc.querySelector('#email-table');
  if (!table) return [];

  const senders = [...table.querySelectorAll('.from_div_45g45gg')];
  const subjects = [...table.querySelectorAll('.subj_div_45g45gg')];
  const bodies = [...table.querySelectorAll('.mess_bodiyy')];
  const count = Math.max(senders.length, subjects.length, bodies.length);
  const now = Date.now();
  const deleted = new Set(email.generatorDeletedIds || []);
  const seen = new Set(email.generatorSeenIds || []);
  const messages = [];

  for (let i = 0; i < count; i += 1) {
    const senderText = senders[i]?.textContent?.trim() || 'غير معروف';
    const subject = subjects[i]?.textContent?.trim() || 'بدون عنوان';
    const bodyElement = bodies[i] || null;
    const text = cleanMessageText(bodyElement);
    const htmlBody = bodyElement?.innerHTML || '';
    const id = stableGeneratorMessageId(senderText, subject, text || htmlBody, i);
    if (deleted.has(id)) continue;
    messages.push({
      id,
      from: parseGeneratorSender(senderText),
      subject,
      intro: text.slice(0, 180),
      text,
      html: htmlBody,
      createdAt: new Date(now - (i * 1000)).toISOString(),
      seen: seen.has(id),
      verifications: [],
      _generator: true
    });
  }
  return messages;
}

async function fetchGeneratorMessages(email, { retryEmpty = false } = {}) {
  const attempts = retryEmpty ? 4 : 1;
  let messages = [];
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const html = await fetchGeneratorHtml(email);
    messages = parseGeneratorMessages(html, email);
    if (messages.length || !retryEmpty || attempt === attempts - 1) break;
    await delay(1800);
  }
  return messages;
}

async function getMessageDetail(email, message) {
  if (!email || !message) return null;
  if (isGeneratorEmail(email)) return message;
  const { data } = await apiForEmail(email, `/messages/${encodeURIComponent(message.id)}`);
  return data;
}

function extractMessageItems(data) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== 'object') return [];
  if (Array.isArray(data['hydra:member'])) return data['hydra:member'];
  if (Array.isArray(data.member)) return data.member;
  if (Array.isArray(data.messages)) return data.messages;
  if (Array.isArray(data.data)) return data.data;
  return [];
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function withCacheBuster(path) {
  return `${path}${path.includes('?') ? '&' : '?'}_=${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function verifyEmailSession(email) {
  const { data } = await apiForEmail(email, withCacheBuster('/me'), { cache: 'no-store' });
  if (data?.address && String(data.address).toLowerCase() !== String(email.address).toLowerCase()) {
    throw new Error('رمز الدخول مرتبط بإيميل مختلف. حدّث Token ثم حاول مرة أخرى.');
  }
  return data;
}

async function fetchMessages(email, { updateUi = true, retryEmpty = false } = {}) {
  if (isGeneratorEmail(email)) {
    const messages = await fetchGeneratorMessages(email, { retryEmpty });
    if (updateUi) ui.inboxMessages = messages;
    return messages;
  }

  if (!isMailTmEmail(email)) throw new Error('هذا الإيميل مضاف للتنظيم فقط ولا يمكن قراءة صندوقه.');

  const attempts = retryEmpty ? 4 : 1;
  let unique = [];
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt === 0) await verifyEmailSession(email);
    const first = await apiForEmail(email, withCacheBuster('/messages?page=1'), { cache: 'no-store' });
    let messages = extractMessageItems(first.data);
    const total = Number(first.data?.['hydra:totalItems'] ?? first.data?.totalItems ?? messages.length);
    const pageSize = Math.max(1, messages.length || 30);
    const pages = Math.min(20, Math.ceil(total / pageSize));
    for (let page = 2; page <= pages; page += 1) {
      const next = await apiForEmail(email, withCacheBuster(`/messages?page=${page}`), { cache: 'no-store' });
      const batch = extractMessageItems(next.data);
      if (!batch.length) break;
      messages = messages.concat(batch);
    }
    unique = [...new Map(messages.filter((message) => message?.id).map((message) => [message.id, message])).values()];
    if (unique.length || !retryEmpty || attempt === attempts - 1) break;
    await delay(1800);
  }
  if (updateUi) ui.inboxMessages = unique;
  return unique;
}

async function openInbox(emailId) {
  const email = getEmail(emailId);
  if (!email) return;
  if (!hasRemoteInbox(email)) { toast('صندوق الوارد متاح للإيميلات المنشأة تلقائياً فقط.', 'info'); return; }
  ui.inboxEmailId = emailId;
  ui.inboxFilter = 'all';
  ui.inboxSearch = '';
  ui.inboxMessages = [];
  ui.inboxLoading = true;
  renderInboxSheet();
  try {
    await fetchMessages(email);
  } catch (error) {
    toast(error.message || 'تعذر جلب الرسائل.', 'error', 5200);
  } finally {
    ui.inboxLoading = false;
    if (ui.inboxEmailId === emailId && !els.sheet.classList.contains('hidden')) renderInboxSheet();
  }
}

async function refreshInbox() {
  const email = getEmail(ui.inboxEmailId);
  if (!email || ui.inboxLoading) return;
  ui.inboxLoading = true;
  renderInboxSheet();
  try {
    await fetchMessages(email, { updateUi: true, retryEmpty: true });
    toast(ui.inboxMessages.length ? 'تم تحديث صندوق الوارد.' : 'لم تصل أي رسالة حتى الآن.', ui.inboxMessages.length ? 'success' : 'info');
  } catch (error) {
    toast(error.message || 'تعذر جلب الرسائل.', 'error', 5200);
  } finally {
    ui.inboxLoading = false;
    if (ui.inboxEmailId) renderInboxSheet();
  }
}

async function openMessage(messageId, archiveId = '') {
  if (archiveId) {
    const archive = state.archivedMessages.find((item) => item.id === archiveId);
    if (!archive) return;
    const email = getEmail(archive.emailId);
    const detail = archive.detail || {
      id: archive.messageId, subject: archive.subject, intro: archive.intro,
      text: archive.text || '', html: archive.html || '', createdAt: archive.createdAt,
      seen: archive.seen, from: { name: archive.fromName, address: archive.fromAddress },
      verifications: archive.verifications || []
    };
    ui.messageDetail = detail;
    renderMessageDetailSheet(detail, email, archive);
    return;
  }
  const email = getEmail(ui.inboxEmailId);
  if (!email) return;
  openSheet('<div class="skeleton" style="height:110px"></div><div class="skeleton" style="height:180px;margin-top:12px"></div>');
  try {
    let summary = ui.inboxMessages.find((item) => item.id === messageId) || null;
    if (!summary && isGeneratorEmail(email)) {
      const messages = await fetchMessages(email, { updateUi: true, retryEmpty: false });
      summary = messages.find((item) => item.id === messageId) || null;
    }
    const data = isGeneratorEmail(email) ? summary : (await apiForEmail(email, `/messages/${encodeURIComponent(messageId)}`)).data;
    if (!data) throw new Error('تعذر العثور على الرسالة.');
    ui.messageDetail = data;
    renderMessageDetailSheet(data, email);
  } catch (error) {
    toast(error.message || 'تعذر فتح الرسالة.', 'error', 5200);
    renderInboxSheet();
  }
}

async function fetchLatestCode(emailId) {
  const email = getEmail(emailId);
  if (!email) return;
  const generatorUrl = isGeneratorEmail(email) ? generatorInboxUrl(email) : '';
  // مهم: هذه الوظيفة لا تفتح Generator.email ولا تغيّر الصفحة نهائياً.
  // يتم بناء رابط الصندوق داخلياً ثم قراءة HTML بالخلفية واستخراج الرسالة والكود والروابط.
  if (!hasRemoteInbox(email)) {
    toast('جلب الكود والروابط يعمل فقط مع الإيميلات التي تم إنشاؤها تلقائياً.', 'info', 5000);
    return;
  }
  openSheet(`
    <div class="sheet-title"><h2>جلب الكود والروابط</h2><button class="close-btn" data-action="close-sheet">×</button></div>
    <div class="skeleton" style="height:120px"></div>
    <p class="helper" style="text-align:center">جاري فحص صندوق البريد وجلب آخر الرسائل...</p>
    ${generatorUrl ? `<a class="btn ghost wide" href="${escapeHTML(generatorUrl)}" target="_blank" rel="noopener noreferrer">فتح صندوق Generator.email</a>` : ''}
  `);
  ui.inboxEmailId = emailId;

  try {
    const messages = await fetchMessages(email, { updateUi: false, retryEmpty: true });
    const newest = [...messages]
      .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))
      .slice(0, 8);

    if (!newest.length) {
      openSheet(`
        <div class="sheet-title"><h2>جلب الكود والروابط</h2><button class="close-btn" data-action="close-sheet">×</button></div>
        ${emptyState('📭', 'لا توجد رسائل حالياً', 'تم فحص رابط صندوق البريد نفسه ولم تظهر رسالة حتى الآن.')}
        ${generatorUrl ? `<a class="btn ghost wide" href="${escapeHTML(generatorUrl)}" target="_blank" rel="noopener noreferrer">فتح الصندوق الأصلي</a>` : ''}
        <button type="button" class="btn primary wide" data-action="refresh-code-only" data-email-id="${email.localId}">تحديث</button>
      `);
      return;
    }

    let code = '';
    let links = [];
    let matchedMessage = null;

    for (const message of newest) {
      const detail = await getMessageDetail(email, message);
      const foundCode = extractFourDigitCode(detail);
      const foundLinks = extractMessageLinks(detail);
      if (foundCode || foundLinks.length) {
        code = foundCode;
        links = foundLinks;
        matchedMessage = detail;
        break;
      }
    }

    if (!code && !links.length) {
      openSheet(`
        <div class="sheet-title"><h2>جلب الكود والروابط</h2><button class="close-btn" data-action="close-sheet">×</button></div>
        ${emptyState('🔎', 'لم يتم العثور على كود أو رابط', 'تم فحص أحدث الرسائل، لكن لا يوجد كود من 4 أرقام أو رابط واضح.')}
        <button type="button" class="btn primary wide" data-action="refresh-code-only" data-email-id="${email.localId}">تحديث</button>
      `);
      return;
    }

    openSheet(`
      <div class="sheet-title"><h2>${code ? 'آخر كود وروابط وصلت' : 'آخر روابط وصلت'}</h2><button class="close-btn" data-action="close-sheet">×</button></div>
      ${code ? `
        <button class="one-tap-code" data-action="copy-text" data-copy="${escapeHTML(code)}">
          <strong>${escapeHTML(code)}</strong>
          <small>اضغط على الكود لنسخه</small>
        </button>
      ` : '<div class="notice no-code-notice">لم يوجد كود من 4 أرقام في هذه الرسالة، لكن تم العثور على روابط.</div>'}
      ${renderFetchedLinks(links)}
      <p class="helper code-source">${escapeHTML(matchedMessage?.subject || 'آخر رسالة وصلت')}</p>
      ${generatorUrl ? `<a class="btn ghost wide" href="${escapeHTML(generatorUrl)}" target="_blank" rel="noopener noreferrer">فتح الصندوق الأصلي</a>` : ''}
      <button type="button" class="btn ghost wide" data-action="refresh-code-only" data-email-id="${email.localId}">تحديث</button>
    `);
  } catch (error) {
    const message = error.message || 'تعذر جلب الكود أو الروابط. تحقق من الإنترنت وحاول مرة أخرى.';
    openSheet(`
      <div class="sheet-title"><h2>جلب الكود والروابط</h2><button class="close-btn" data-action="close-sheet">×</button></div>
      <div class="notice">${escapeHTML(message)}</div>
      <button type="button" class="btn primary wide" data-action="refresh-code-only" data-email-id="${email.localId}">إعادة المحاولة</button>
      ${generatorUrl ? `<a class="btn ghost wide" href="${escapeHTML(generatorUrl)}" target="_blank" rel="noopener noreferrer">فتح الصندوق يدوياً</a>` : ''}
    `);
    toast(message, 'error', 6000);
  }
}

async function startPolling(emailId) {
  const email = getEmail(emailId);
  if (!email) return;
  if (polling) stopPolling(false);
  let baseline = [];
  try { baseline = await fetchMessages(email, { updateUi: false }); } catch (error) { toast(error.message, 'error'); return; }
  polling = {
    emailId,
    knownIds: new Set(baseline.map((m) => m.id)),
    deadline: Date.now() + Math.min(60, Number(state.settings.waitDuration) || 60) * 1000,
    timer: null
  };
  toast('بدأ انتظار وصول الكود.', 'info');
  const tick = async () => {
    if (!polling || polling.emailId !== emailId) return;
    if (Date.now() >= polling.deadline) {
      stopPolling(false);
      toast('لم تصل أي رسالة حتى الآن.', 'info');
      if (ui.inboxEmailId === emailId && !els.sheet.classList.contains('hidden')) renderInboxSheet();
      return;
    }
    try {
      const messages = await fetchMessages(email, { updateUi: true });
      const fresh = messages.filter((m) => !polling.knownIds.has(m.id));
      messages.forEach((m) => polling?.knownIds.add(m.id));
      if (fresh.length) {
        const newest = fresh.sort((a,b) => new Date(b.createdAt)-new Date(a.createdAt))[0];
        stopPolling(false);
        notifyArrival();
        toast('وصلت رسالة جديدة.');
        ui.inboxEmailId = emailId;
        await openMessage(newest.id);
        return;
      }
      if (ui.inboxEmailId === emailId && !els.sheet.classList.contains('hidden')) renderInboxSheet();
    } catch (error) {
      stopPolling(false);
      toast(error.message || 'توقف انتظار الرسائل بسبب خطأ.', 'error');
    }
  };
  polling.timer = setInterval(tick, 5000);
  if (ui.inboxEmailId === emailId && !els.sheet.classList.contains('hidden')) renderInboxSheet();
}

function stopPolling(showToast = true) {
  if (!polling) return;
  clearInterval(polling.timer);
  polling = null;
  if (showToast) toast('تم إيقاف انتظار الرسائل.', 'info');
  if (ui.inboxEmailId && !els.sheet.classList.contains('hidden') && els.sheetContent.querySelector('[data-inbox-root]')) renderInboxSheet();
}

function notifyArrival() {
  if (state.settings.vibration && navigator.vibrate) navigator.vibrate([180, 80, 180]);
  if (state.settings.sound) {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.16, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.32);
      osc.connect(gain); gain.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + 0.34);
    } catch (_) { /* الصوت اختياري */ }
  }
}

async function markMessageSeen(messageId) {
  const email = getEmail(ui.inboxEmailId);
  if (!email) return;
  try {
    if (isGeneratorEmail(email)) {
      if (!email.generatorSeenIds.includes(messageId)) email.generatorSeenIds.push(messageId);
      ui.messageDetail = { ...ui.messageDetail, seen: true };
      ui.inboxMessages = ui.inboxMessages.map((m) => m.id === messageId ? { ...m, seen: true } : m);
      saveState();
      toast('تم تعليم الرسالة كمقروءة محلياً.');
      renderMessageDetailSheet(ui.messageDetail, email);
      return;
    }
    const { data } = await apiForEmail(email, `/messages/${encodeURIComponent(messageId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/merge-patch+json' },
      body: JSON.stringify({ seen: true })
    });
    ui.messageDetail = data || { ...ui.messageDetail, seen: true };
    ui.inboxMessages = ui.inboxMessages.map((m) => m.id === messageId ? { ...m, seen: true } : m);
    toast('تم تعليم الرسالة كمقروءة.');
    renderMessageDetailSheet(ui.messageDetail, email);
  } catch (error) { toast(error.message, 'error'); }
}

async function deleteMessage(messageId) {
  const email = getEmail(ui.inboxEmailId);
  if (!email) return;
  const localOnly = isGeneratorEmail(email);
  const ok = await confirmDialog({
    title:'حذف الرسالة',
    message: localOnly ? 'سيتم إخفاء الرسالة من هذا الموقع فقط. صندوق Generator.email العام لا يوفر حذفاً عبر هذا الموقع.' : 'سيتم حذف الرسالة نهائياً من خدمة البريد. لا يمكن التراجع عن ذلك.',
    confirmText: localOnly ? 'إخفاء الرسالة' : 'حذف نهائياً',
    danger:true
  });
  if (ok !== 'confirm') return;
  try {
    if (localOnly) {
      if (!email.generatorDeletedIds.includes(messageId)) email.generatorDeletedIds.push(messageId);
    } else {
      await apiForEmail(email, `/messages/${encodeURIComponent(messageId)}`, { method:'DELETE' });
    }
    ui.inboxMessages = ui.inboxMessages.filter((m) => m.id !== messageId);
    email.archivedMessageIds = email.archivedMessageIds.filter((id) => id !== messageId);
    state.archivedMessages = state.archivedMessages.filter((m) => !(m.emailId === email.localId && m.messageId === messageId));
    saveState();
    toast(localOnly ? 'تم إخفاء الرسالة محلياً.' : 'تم حذف الرسالة.');
    renderInboxSheet();
  } catch (error) { toast(error.message, 'error'); }
}

async function archiveMessage(messageId) {
  const email = getEmail(ui.inboxEmailId);
  if (!email) return;
  let detail = ui.messageDetail?.id === messageId ? ui.messageDetail : null;
  const summary = ui.inboxMessages.find((m) => m.id === messageId) || {};
  if (!detail) {
    try { detail = isGeneratorEmail(email) ? summary : (await apiForEmail(email, `/messages/${encodeURIComponent(messageId)}`)).data; }
    catch (_) { detail = summary; }
  }
  if (!email.archivedMessageIds.includes(messageId)) email.archivedMessageIds.push(messageId);
  const existing = state.archivedMessages.find((m) => m.emailId === email.localId && m.messageId === messageId);
  if (!existing) {
    state.archivedMessages.unshift({
      id: makeId(), emailId: email.localId, emailAddress: email.address, messageId,
      subject: detail.subject || summary.subject || '', intro: detail.intro || summary.intro || '',
      fromName: detail.from?.name || summary.from?.name || '', fromAddress: detail.from?.address || summary.from?.address || '',
      createdAt: detail.createdAt || summary.createdAt || new Date().toISOString(), seen: Boolean(detail.seen ?? summary.seen),
      text: detail.text || '', html: Array.isArray(detail.html) ? detail.html.join('\n') : (detail.html || ''),
      verifications: detail.verifications || [], detail: clone(detail), archivedAt: new Date().toISOString()
    });
  }
  saveState();
  toast('تمت أرشفة الرسالة محلياً.');
  renderInboxSheet();
}

function restoreArchivedMessage(archiveId) {
  const record = state.archivedMessages.find((m) => m.id === archiveId);
  if (!record) return;
  const email = getEmail(record.emailId);
  if (email) email.archivedMessageIds = email.archivedMessageIds.filter((id) => id !== record.messageId);
  state.archivedMessages = state.archivedMessages.filter((m) => m.id !== archiveId);
  saveState();
  toast('تم استرجاع الرسالة من الأرشيف.');
  if (ui.inboxEmailId) renderInboxSheet(); else renderArchive();
}

function readProfileForm(emailId, profileNumber) {
  const form = els.sheetContent.querySelector('[data-profile-form]');
  const email = getEmail(emailId);
  const profile = getProfile(email, profileNumber);
  if (!form || !profile) return profile;
  form.querySelectorAll('[data-field]').forEach((input) => {
    const field = input.dataset.field;
    if (field in profile) profile[field] = input.value.trim();
  });
  return profile;
}

function saveProfileFromSheet(emailId, profileNumber, notify = true) {
  const profile = readProfileForm(emailId, profileNumber);
  if (!profile) return;
  if (!/^\d{1,12}$/.test(profile.pin)) {
    toast('الرقم السري يجب أن يحتوي على أرقام فقط.', 'error');
    return;
  }
  saveState();
  if (notify) toast('تم حفظ التعديلات.');
  renderEmailDetail();
  openProfileSheet(emailId, profileNumber);
}

function autosaveProfileField(input) {
  const form = input.closest('[data-profile-form]');
  if (!form) return;
  const email = getEmail(form.dataset.emailId);
  const profile = getProfile(email, form.dataset.profileNumber);
  if (!profile || !(input.dataset.field in profile)) return;
  profile[input.dataset.field] = input.value;
  clearTimeout(ui.autoSaveTimer);
  ui.autoSaveTimer = setTimeout(() => saveState({ silent: true }), 350);
}

function markProfileReview(emailId, profileNumber) {
  const email = getEmail(emailId);
  const profile = readProfileForm(emailId, profileNumber) || getProfile(email, profileNumber);
  if (!profile) return;
  profile.status = 'review';
  profile.reservedAt = new Date().toISOString();
  profile.statusChangedAt = profile.reservedAt;
  saveState();
  toast('تم نقل البروفايل إلى قيد المراجعة.');
  renderEmailDetail();
  openProfileSheet(emailId, profileNumber);
}

function cancelProfileReview(emailId, profileNumber) {
  const email = getEmail(emailId);
  const profile = getProfile(email, profileNumber);
  if (!profile) return;
  profile.status = 'available';
  profile.reservedAt = null;
  profile.statusChangedAt = new Date().toISOString();
  saveState();
  toast('تم إلغاء الحجز وإعادة البروفايل إلى المتاح.');
  renderEmailDetail();
  openProfileSheet(emailId, profileNumber);
}

async function markProfileSold(emailId, profileNumber) {
  const email = getEmail(emailId);
  const profile = readProfileForm(emailId, profileNumber) || getProfile(email, profileNumber);
  if (!email || !profile) return;
  const ok = await confirmDialog({ title:'تسجيل عملية بيع', message:`هل أنت متأكد من تسجيل البروفايل رقم ${profile.number} كمباع؟`, confirmText:'نعم، تم البيع' });
  if (ok !== 'confirm') return;
  const now = new Date().toISOString();
  profile.status = 'sold';
  profile.soldAt = now;
  profile.statusChangedAt = now;
  profile.reservedAt = profile.reservedAt || now;
  const existingSale = state.sales.find((sale) => sale.emailId === email.localId && sale.profileNumber === profile.number && !sale.restoredAt);
  if (!existingSale) {
    state.sales.unshift({
      id: makeId(), emailId: email.localId, emailAddress: email.address,
      profileNumber: profile.number, pin: profile.pin,
      customerName: profile.customerName, customerPhone: profile.customerPhone,
      notes: profile.notes, soldAt: now, restoredAt: null
    });
  }
  const allSold = email.profiles.every((p) => p.status === 'sold');
  if (allSold) {
    email.status = 'completed';
    email.completedAt = now;
    const next = state.emails.find((item) => item.status === 'active' && item.localId !== email.localId);
    state.lastEmailId = next?.localId || email.localId;
    saveState();
    closeSheet();
    if (next) {
      ui.selectedEmailId = next.localId;
      ui.returnView = 'home';
      ui.view = 'email-detail';
    } else {
      ui.view = 'sold-emails';
      ui.selectedEmailId = null;
    }
    toast('تم إنهاء هذا الإيميل والانتقال إلى الإيميل التالي.');
    render();
    return;
  }
  saveState();
  closeSheet();
  toast('تم تسجيل عملية البيع.');
  renderEmailDetail();
}

async function restoreProfile(emailId, profileNumber, saleId = '') {
  const email = getEmail(emailId);
  const profile = getProfile(email, profileNumber);
  if (!email || !profile) return;
  const ok = await confirmDialog({ title:'استرجاع البروفايل', message:`سيعود البروفايل رقم ${profile.number} إلى حالة متاح، وسيعود الإيميل إلى الواجهة الرئيسية إن كان مكتملاً.`, confirmText:'استرجاع' });
  if (ok !== 'confirm') return;
  const now = new Date().toISOString();
  profile.status = 'available';
  profile.recoveredAt = now;
  profile.statusChangedAt = now;
  profile.reservedAt = null;
  profile.soldAt = null;
  email.status = 'active';
  email.completedAt = null;
  const sale = saleId ? state.sales.find((item) => item.id === saleId) : state.sales.find((item) => item.emailId === email.localId && item.profileNumber === profile.number && !item.restoredAt);
  if (sale) sale.restoredAt = now;
  state.recoveredProfiles.unshift({ id:makeId(), emailId:email.localId, emailAddress:email.address, profileNumber:profile.number, recoveredAt:now });
  state.lastEmailId = email.localId;
  saveState();
  closeSheet();
  toast('تم استرجاع البروفايل وإعادته إلى المتاح.');
  ui.view = 'email-detail';
  ui.returnView = 'home';
  ui.selectedEmailId = email.localId;
  render();
}

function copyProfileData(emailId, profileNumber) {
  const email = getEmail(emailId);
  const profile = getProfile(email, profileNumber);
  if (!email || !profile) return;
  copyText(`الإيميل: ${email.address}\nالرقم السري للبروفايل ${profile.number}: ${profile.pin}`);
}

function applyProfileStyles(allEmails) {
  const editor = ui.profileEditor;
  if (!editor) return;
  const styles = editor.drafts.map(({number,color,icon,imageData}) => ({number,color,icon,imageData}));
  if (allEmails) {
    state.globalProfileStyles = clone(styles);
    state.emails.forEach((email) => email.profiles.forEach((profile) => {
      const style = styles.find((item) => item.number === profile.number);
      if (style) Object.assign(profile, clone(style));
    }));
  } else {
    const email = getEmail(editor.emailId);
    email?.profiles.forEach((profile) => {
      const style = styles.find((item) => item.number === profile.number);
      if (style) Object.assign(profile, clone(style));
    });
  }
  saveState();
  closeSheet();
  toast(allEmails ? 'تم تطبيق الرموز على جميع الإيميلات.' : 'تم تطبيق الرموز على هذا الإيميل.');
  render();
}

async function compressImage(file) {
  if (!file || !file.type.startsWith('image/')) throw new Error('اختر ملف صورة صالحاً.');
  if (file.size > 12 * 1024 * 1024) throw new Error('حجم الصورة كبير جداً. اختر صورة أصغر من 12MB.');
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('تعذر قراءة الصورة.'));
    reader.readAsDataURL(file);
  });
  const image = await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('تعذر معالجة الصورة.'));
    img.src = dataUrl;
  });
  const max = 256;
  const scale = Math.min(1, max / Math.max(image.width, image.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.width * scale));
  canvas.height = Math.max(1, Math.round(image.height * scale));
  const ctx = canvas.getContext('2d', { alpha: false });
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/jpeg', 0.72);
}

function openExistingModal() {
  openModal(`
    <h2>إضافة حساب بريد قديم</h2>
    <p>أدخل البريد وكلمة المرور. سيتم التحقق منهما عبر API قبل حفظ الحساب.</p>
    <div class="form-grid">
      <div class="field"><label>البريد الإلكتروني</label><input id="existingEmail" class="input" type="email" dir="ltr" autocomplete="username" placeholder="name@domain.com"></div>
      <div class="field"><label>كلمة المرور</label><input id="existingPassword" class="input" type="password" dir="ltr" autocomplete="current-password" placeholder="كلمة المرور"></div>
      <p id="existingError" class="form-error"></p>
      <button class="btn primary wide" data-action="save-existing-account">التحقق والحفظ</button>
      <button class="btn ghost wide" data-action="close-modal">إلغاء</button>
    </div>
  `);
}

function openLocalEmailModal() {
  openModal(`
    <h2>إضافة إيميل</h2>
    <p>أدخل أي بريد إلكتروني لإضافته مع خمسة بروفايلات.</p>
    <div class="form-grid">
      <div class="field"><label>البريد الإلكتروني</label><input id="localEmailAddress" class="input" type="email" dir="ltr" autocomplete="off" placeholder="nhyffga@hi2.in"></div>
      <p id="localEmailError" class="form-error"></p>
      <button class="btn primary wide" data-action="save-local-email">حفظ الإيميل</button>
      <button class="btn ghost wide" data-action="close-modal">إلغاء</button>
    </div>
  `);
  requestAnimationFrame(() => document.getElementById('localEmailAddress')?.focus());
}

async function renameEmail(emailId) {
  const email = getEmail(emailId);
  if (!email) return;
  openModal(`
    <h2>تعديل الاسم المحلي</h2>
    <p>لن يتغير عنوان البريد الحقيقي. الاسم المحلي يساعدك على التنظيم فقط.</p>
    <div class="form-grid"><input id="localEmailName" class="input" value="${escapeHTML(email.localName)}" maxlength="50" placeholder="مثال: حساب نتفلكس الأول"><button class="btn primary wide" data-action="save-email-name" data-email-id="${email.localId}">حفظ</button><button class="btn ghost wide" data-action="close-modal">إلغاء</button></div>
  `);
}

async function archiveEmail(emailId) {
  const email = getEmail(emailId);
  if (!email) return;
  const ok = await confirmDialog({ title:'أرشفة الإيميل', message:'سيختفي الإيميل من الواجهة الرئيسية ويمكن استرجاعه من قسم الأرشيف.', confirmText:'أرشفة' });
  if (ok !== 'confirm') return;
  email.status = 'archived';
  email.archivedAt = new Date().toISOString();
  if (state.lastEmailId === email.localId) state.lastEmailId = state.emails.find((e) => e.status === 'active')?.localId || null;
  saveState();
  toast('تمت أرشفة الإيميل.');
  goView('home');
}

async function deleteEmail(emailId) {
  const email = getEmail(emailId);
  if (!email) return;

  let choice = 'local';
  if (isMailTmEmail(email)) {
    choice = await choiceDialog({
      title:'حذف الإيميل',
      message:'احذفه من الموقع فقط، أو احذفه نهائياً من Mail.tm أيضاً.',
      choices:[
        { value:'local', label:'حذف من الموقع فقط', className:'warning' },
        { value:'remote', label:'حذف من Mail.tm والموقع', className:'danger' }
      ]
    });
    if (!['local','remote'].includes(choice)) return;
  }

  const confirm = await confirmDialog({
    title:'تأكيد الحذف',
    message: choice === 'remote' ? 'سيتم حذف الحساب نهائياً من Mail.tm ولا يمكن التراجع.' : 'سيتم حذف الإيميل وكل بياناته المحلية من هذا المتصفح.',
    confirmText:'حذف الإيميل',
    danger:true
  });
  if (confirm !== 'confirm') return;

  try {
    if (choice === 'remote') {
      if (!email.mailTmId) throw new Error('لا يتوفر ID الحساب المطلوب للحذف النهائي.');
      await apiForEmail(email, `/accounts/${encodeURIComponent(email.mailTmId)}`, { method:'DELETE' });
    }
    state.emails = state.emails.filter((item) => item.localId !== email.localId);
    state.sales = state.sales.filter((item) => item.emailId !== email.localId);
    state.archivedMessages = state.archivedMessages.filter((item) => item.emailId !== email.localId);
    state.recoveredProfiles = state.recoveredProfiles.filter((item) => item.emailId !== email.localId);
    if (state.lastEmailId === email.localId) state.lastEmailId = state.emails.find((item) => item.status === 'active')?.localId || null;
    saveState();
    toast(choice === 'remote' ? 'تم حذف الحساب نهائياً.' : 'تم حذف الإيميل من الموقع.');
    goView(email.status === 'completed' ? 'sold-emails' : 'home');
  } catch (error) {
    toast(error.message || 'تعذر حذف الحساب.', 'error', 6000);
  }
}

async function hashPin(pin) {
  const data = new TextEncoder().encode(`cd-mail-manager:${pin}`);
  if (crypto?.subtle) {
    const hash = await crypto.subtle.digest('SHA-256', data);
    return Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2,'0')).join('');
  }
  return btoa(`cd-mail-manager:${pin}`);
}

async function configurePin() {
  if (state.settings.lockEnabled) {
    openModal(`
      <h2>إيقاف قفل الموقع</h2><p>أدخل رمز PIN الحالي لإيقاف القفل.</p>
      <div class="form-grid"><input id="currentPin" class="input" type="password" inputmode="numeric" maxlength="6" placeholder="PIN الحالي"><p id="pinConfigError" class="form-error"></p><button class="btn danger wide" data-action="disable-pin">إيقاف القفل</button><button class="btn ghost wide" data-action="close-modal">إلغاء</button></div>
    `);
  } else {
    openModal(`
      <h2>تشغيل قفل الموقع</h2><p>اختر رمز PIN من 4 إلى 6 أرقام. سيُطلب عند فتح الصفحة.</p>
      <div class="form-grid"><input id="newPin" class="input" type="password" inputmode="numeric" maxlength="6" placeholder="PIN جديد"><input id="confirmPin" class="input" type="password" inputmode="numeric" maxlength="6" placeholder="تأكيد PIN"><p id="pinConfigError" class="form-error"></p><button class="btn primary wide" data-action="enable-pin">تشغيل القفل</button><button class="btn ghost wide" data-action="close-modal">إلغاء</button></div>
    `);
  }
}

async function enablePin() {
  const pin = document.getElementById('newPin')?.value || '';
  const confirm = document.getElementById('confirmPin')?.value || '';
  const error = document.getElementById('pinConfigError');
  if (!/^\d{4,6}$/.test(pin)) { if (error) error.textContent = 'الرمز يجب أن يكون من 4 إلى 6 أرقام.'; return; }
  if (pin !== confirm) { if (error) error.textContent = 'رمزا PIN غير متطابقين.'; return; }
  state.settings.pinHash = await hashPin(pin);
  state.settings.lockEnabled = true;
  saveState();
  closeModal();
  toast('تم تشغيل قفل الموقع.');
  renderSettings();
}

async function disablePin() {
  const pin = document.getElementById('currentPin')?.value || '';
  const error = document.getElementById('pinConfigError');
  if (await hashPin(pin) !== state.settings.pinHash) { if (error) error.textContent = 'رمز PIN غير صحيح.'; return; }
  state.settings.lockEnabled = false;
  state.settings.pinHash = '';
  saveState();
  closeModal();
  toast('تم إيقاف قفل الموقع.');
  renderSettings();
}

async function unlockApp() {
  const pin = els.unlockPin.value;
  if (await hashPin(pin) === state.settings.pinHash) {
    els.lockScreen.classList.add('hidden');
    els.lockScreen.setAttribute('aria-hidden','true');
    els.app.classList.remove('hidden');
    els.unlockPin.value = '';
    els.unlockError.textContent = '';
    render();
  } else {
    els.unlockError.textContent = 'رمز PIN غير صحيح.';
    els.unlockPin.select();
  }
}

function exportBackup() {
  const payload = { app:'CD Mail Profile Manager', exportedAt:new Date().toISOString(), data:state };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type:'application/json;charset=utf-8' });
  const link = document.createElement('a');
  const stamp = new Date().toISOString().replace(/[:.]/g,'-');
  link.href = URL.createObjectURL(blob);
  link.download = `cd-mail-backup-${stamp}.json`;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  toast('تم تصدير النسخة الاحتياطية.');
}

async function importBackupFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const parsed = JSON.parse(text);
    const data = parsed?.data || parsed;
    if (!data || typeof data !== 'object' || !Array.isArray(data.emails) || !data.settings) throw new Error('ملف النسخة الاحتياطية غير صالح أو تالف.');
    const ok = await confirmDialog({ title:'استيراد نسخة احتياطية', message:`تم العثور على ${data.emails.length} إيميل. سيؤدي الاستيراد إلى استبدال جميع البيانات الحالية.`, confirmText:'استبدال البيانات', danger:true });
    if (ok !== 'confirm') return;
    state = normalizeState(data);
    if (!saveState()) throw new Error('تعذر حفظ النسخة بسبب امتلاء مساحة التخزين المحلية.');
    applyTheme();
    closeSheet(); closeModal();
    ui.view = state.lastEmailId && getEmail(state.lastEmailId) ? 'email-detail' : 'home';
    ui.selectedEmailId = state.lastEmailId;
    ui.returnView = getEmail(state.lastEmailId)?.status === 'completed' ? 'sold-emails' : 'home';
    toast('تم استيراد النسخة الاحتياطية بنجاح.');
    render();
  } catch (error) {
    toast(error.message || 'ملف النسخة الاحتياطية تالف.', 'error', 6000);
  } finally {
    els.importInput.value = '';
  }
}

async function clearAllData() {
  const first = await confirmDialog({ title:'تحذير قوي', message:'سيتم حذف كل الإيميلات، كلمات المرور، Tokens، البروفايلات، الرسائل والإعدادات من هذا المتصفح.', confirmText:'أفهم، متابعة', danger:true });
  if (first !== 'confirm') return;
  const second = await confirmDialog({ title:'التأكيد النهائي', message:'هذه آخر فرصة للتراجع. هل تريد مسح جميع البيانات نهائياً؟', confirmText:'مسح كل شيء الآن', danger:true });
  if (second !== 'confirm') return;
  localStorage.removeItem(STORAGE_KEY);
  state = initialState();
  ui.view = 'home'; ui.selectedEmailId = null;
  applyTheme();
  toast('تم مسح جميع البيانات.');
  render();
}

function filterLiveList(input) {
  const target = document.getElementById(input.dataset.liveFilter);
  if (!target) return;
  const query = input.value.trim().toLowerCase();
  target.querySelectorAll('[data-search-item]').forEach((item) => {
    item.hidden = query && !item.dataset.searchItem.includes(query);
  });
}

function filterSales() {
  const values = {};
  document.querySelectorAll('[data-sales-filter]').forEach((input) => { values[input.dataset.salesFilter] = input.value.trim().toLowerCase(); });
  document.querySelectorAll('[data-sale-item]').forEach((item) => {
    const visible = (!values.email || item.dataset.email.includes(values.email))
      && (!values.profile || item.dataset.profile === values.profile)
      && (!values.customer || item.dataset.customer.includes(values.customer))
      && (!values.date || item.dataset.date === values.date);
    item.hidden = !visible;
  });
}

function copySale(saleId) {
  const sale = state.sales.find((item) => item.id === saleId);
  if (!sale) return;
  copyText(`الإيميل: ${sale.emailAddress}\nالبروفايل: ${sale.profileNumber}\nالرقم السري: ${sale.pin || ''}\nالعميل: ${sale.customerName || 'غير مسجل'}\nرقم العميل: ${sale.customerPhone || 'غير مسجل'}\nتاريخ البيع: ${formatDate(sale.soldAt)}\nالملاحظات: ${sale.notes || 'لا توجد'}`);
}

async function deleteSaleRecord(saleId) {
  const ok = await confirmDialog({ title:'حذف سجل البيع', message:'سيُحذف السجل من قائمة المبيعات محلياً، ولن تتغير حالة البروفايل.', confirmText:'حذف السجل', danger:true });
  if (ok !== 'confirm') return;
  state.sales = state.sales.filter((item) => item.id !== saleId);
  saveState(); toast('تم حذف سجل البيع محلياً.'); renderSales();
}

function restoreSale(saleId) {
  const sale = state.sales.find((item) => item.id === saleId);
  if (!sale) return;
  restoreProfile(sale.emailId, sale.profileNumber, sale.id);
}

function openApiInfo() {
  openModal(`<h2>معلومات البريد</h2><p>عند إنشاء إيميل جديد يستخدم الموقع النطاق 5xu.vn. ولجلب الرسائل يبني رابط الصندوق مباشرة بالشكل https://generator.email/inbox9/EMAIL ثم يقرأ HTML لهذا الصندوق ويستخرج الرسائل والكود والروابط. إذا منع المتصفح القراءة المباشرة بسبب CORS، يستخدم الموقع جسر CORS لنفس رابط inbox9 دون تغيير الإيميل.</p><div class="notice">إذا كان لديك إيميل قديم على نطاق توقف عن العمل، أنشئ إيميلاً جديداً. زر تحديث يعيد قراءة الصندوق ويستخرج كود 4 أرقام والروابط من أحدث الرسائل.</div><button class="btn primary wide" style="margin-top:14px" data-action="close-modal">حسناً</button>`);
}

function openAbout() {
  openModal(`<h2>حول الموقع</h2><p>مدير عربي محلي للإيميلات المؤقتة وملفات البروفايلات. المشروع مكوّن من HTML وCSS وJavaScript فقط، ويستخدم LocalStorage لحفظ البيانات على نفس الجهاز والمتصفح.</p><p>الإصدار: 1.0</p><button class="btn primary wide" data-action="close-modal">إغلاق</button>`);
}

async function handleClick(event) {
  const nav = event.target.closest('[data-view]');
  if (nav) { goView(nav.dataset.view); return; }

  const modalResult = event.target.closest('[data-modal-result]');
  if (modalResult) { closeModal(modalResult.dataset.modalResult); return; }

  const target = event.target.closest('[data-action]');
  if (!target) return;
  // أزرار الموقع ليست روابط تنقل. نمنع أي سلوك افتراضي (مثل submit أو فتح رابط)
  // حتى يبقى التحديث داخل الصفحة فقط.
  event.preventDefault();
  event.stopPropagation();
  const action = target.dataset.action;

  try {
    switch (action) {
      case 'go-settings': goView('settings'); break;
      case 'go-sold-emails': goView('sold-emails'); break;
      case 'focus-sold-search': document.getElementById('soldEmailSearch')?.focus(); break;
      case 'focus-active-search': document.getElementById('activeEmailSearch')?.focus(); break;
      case 'set-add-mode': ui.addMode = target.dataset.mode; renderHome(); break;
      case 'create-email': await createMailAccount(); break;
      case 'open-existing-modal': openExistingModal(); break;
      case 'open-local-email-modal': openLocalEmailModal(); break;
      case 'save-existing-account': await saveExistingAccount(); break;
      case 'save-local-email': await saveLocalEmail(); break;
      case 'close-modal': closeModal('cancel'); break;
      case 'close-sheet': closeSheet(); break;
      case 'copy-text': await copyText(target.dataset.copy || ''); break;
      case 'open-email':
        ui.selectedEmailId = target.dataset.emailId;
        ui.returnView = target.dataset.returnView || 'home';
        ui.view = 'email-detail';
        state.lastEmailId = ui.selectedEmailId;
        saveState({ silent:true });
        render(); window.scrollTo({top:0,behavior:'smooth'});
        break;
      case 'back-from-email': goView(ui.returnView || 'home'); break;
      case 'rename-email': await renameEmail(target.dataset.emailId); break;
      case 'save-email-name': {
        const email = getEmail(target.dataset.emailId);
        if (email) { email.localName = document.getElementById('localEmailName')?.value.trim() || ''; saveState(); closeModal(); toast('تم حفظ الاسم المحلي.'); render(); }
        break;
      }
      case 'refresh-token': {
        const email = getEmail(target.dataset.emailId);
        if (!email || isBusy(`token-${email.localId}`)) break;
        setBusy(`token-${email.localId}`, true); target.disabled = true; target.innerHTML = '<span class="spinner"></span> تحديث';
        try { await refreshEmailToken(email, true); } catch (error) { toast(error.message, 'error', 5500); }
        finally { setBusy(`token-${email.localId}`, false); render(); }
        break;
      }
      case 'archive-email': await archiveEmail(target.dataset.emailId); break;
      case 'delete-email': await deleteEmail(target.dataset.emailId); break;
      case 'restore-email': {
        const email = getEmail(target.dataset.emailId);
        if (email) { email.status='active'; email.archivedAt=null; state.lastEmailId=email.localId; saveState(); toast('تم استرجاع الإيميل.'); renderArchive(); }
        break;
      }
      case 'open-profile': openProfileSheet(target.dataset.emailId, target.dataset.profileNumber); break;
      case 'save-profile': saveProfileFromSheet(target.dataset.emailId, target.dataset.profileNumber); break;
      case 'toggle-profile-pin': {
        const input = document.getElementById('profilePin');
        if (input) { const show = input.type === 'password'; input.type = show ? 'text':'password'; target.textContent = show ? 'إخفاء':'إظهار'; }
        break;
      }
      case 'reset-profile-pin': {
        const email = getEmail(target.dataset.emailId); const profile = getProfile(email,target.dataset.profileNumber);
        if (profile) { profile.pin=profile.defaultPin; saveState(); toast('تم استرجاع الرقم الافتراضي.'); openProfileSheet(email.localId,profile.number); }
        break;
      }
      case 'copy-profile-data': copyProfileData(target.dataset.emailId,target.dataset.profileNumber); break;
      case 'mark-review': markProfileReview(target.dataset.emailId,target.dataset.profileNumber); break;
      case 'cancel-review': cancelProfileReview(target.dataset.emailId,target.dataset.profileNumber); break;
      case 'mark-sold': await markProfileSold(target.dataset.emailId,target.dataset.profileNumber); break;
      case 'restore-profile': await restoreProfile(target.dataset.emailId,target.dataset.profileNumber); break;
      case 'fetch-code':
        // جلب الكود والروابط يتم داخل الموقع فقط، بدون فتح Generator.email.
        await fetchLatestCode(target.dataset.emailId);
        break;
      case 'refresh-code-only':
        // التحديث يعيد قراءة نفس الصندوق بالخلفية فقط.
        await fetchLatestCode(target.dataset.emailId);
        break;
      case 'open-inbox': await openInbox(target.dataset.emailId); break;
      case 'refresh-messages': await refreshInbox(); break;
      case 'set-inbox-filter': ui.inboxFilter=target.dataset.filter; renderInboxSheet(); break;
      case 'start-waiting': await startPolling(target.dataset.emailId); break;
      case 'stop-waiting': stopPolling(); break;
      case 'open-message': await openMessage(target.dataset.messageId,target.dataset.archiveId||''); break;
      case 'back-to-inbox': ui.inboxEmailId ? renderInboxSheet() : closeSheet(); break;
      case 'refresh-current-message': await openMessage(target.dataset.messageId); break;
      case 'mark-message-seen': await markMessageSeen(target.dataset.messageId); break;
      case 'delete-message': await deleteMessage(target.dataset.messageId); break;
      case 'archive-message': await archiveMessage(target.dataset.messageId); break;
      case 'view-archived-message': await openMessage('',target.dataset.archiveId); break;
      case 'restore-archived-message': restoreArchivedMessage(target.dataset.archiveId); break;
      case 'delete-archived-message': {
        const record = state.archivedMessages.find((m) => m.id === target.dataset.archiveId);
        const ok=await confirmDialog({title:'حذف الرسالة المؤرشفة',message:'سيُحذف السجل المحلي فقط، ولن تُحذف الرسالة من Mail.tm.',confirmText:'حذف محلي',danger:true});
        if(ok==='confirm' && record){const email=getEmail(record.emailId);if(email)email.archivedMessageIds=email.archivedMessageIds.filter(id=>id!==record.messageId);state.archivedMessages=state.archivedMessages.filter(m=>m.id!==record.id);saveState();toast('تم حذف الرسالة المؤرشفة محلياً.');renderArchive();}
        break;
      }
      case 'delete-archived-message-remote': {
        const record = state.archivedMessages.find((m) => m.id === target.dataset.archiveId);
        if (!record) break;
        const email = getEmail(record.emailId);
        const ok = await confirmDialog({title:'حذف نهائي من Mail.tm',message:'سيتم حذف الرسالة من Mail.tm ومن الأرشيف المحلي نهائياً.',confirmText:'حذف نهائياً',danger:true});
        if (ok === 'confirm' && email) {
          await apiForEmail(email, `/messages/${encodeURIComponent(record.messageId)}`, {method:'DELETE'});
          email.archivedMessageIds=email.archivedMessageIds.filter(id=>id!==record.messageId);
          state.archivedMessages=state.archivedMessages.filter(m=>m.id!==record.id);
          saveState();toast('تم حذف الرسالة نهائياً.');renderArchive();
        }
        break;
      }
      case 'change-profile-icons': openProfileEditor(target.dataset.emailId,false); break;
      case 'change-profile-icons-global': openProfileEditor(null,true); break;
      case 'choose-profile-icon': {
        const draft=ui.profileEditor?.drafts.find(p=>p.number===Number(target.dataset.profileNumber));
        if(draft){draft.icon=target.dataset.icon;draft.imageData='';renderProfileEditorSheet();}
        break;
      }
      case 'upload-profile-image':
        els.imageInput.dataset.profileNumber=target.dataset.profileNumber; els.imageInput.click(); break;
      case 'remove-profile-image': {
        const draft=ui.profileEditor?.drafts.find(p=>p.number===Number(target.dataset.profileNumber));
        if(draft){draft.imageData='';renderProfileEditorSheet();}
        break;
      }
      case 'reset-profile-style': {
        const n=Number(target.dataset.profileNumber); const draft=ui.profileEditor?.drafts.find(p=>p.number===n);
        if(draft){draft.color=DEFAULT_COLORS[n-1];draft.icon='face';draft.imageData='';renderProfileEditorSheet();}
        break;
      }
      case 'apply-profile-styles-current': applyProfileStyles(false); break;
      case 'apply-profile-styles-all': applyProfileStyles(true); break;
      case 'copy-sale': copySale(target.dataset.saleId); break;
      case 'restore-sale': restoreSale(target.dataset.saleId); break;
      case 'delete-sale-record': await deleteSaleRecord(target.dataset.saleId); break;
      case 'delete-recovery-record':
        state.recoveredProfiles=state.recoveredProfiles.filter(r=>r.id!==target.dataset.recoveryId);saveState();toast('تم حذف سجل الاسترجاع.');renderArchive();break;
      case 'set-accent': state.settings.accent=target.dataset.color;saveState();applyTheme();renderSettings();break;
      case 'toggle-setting':
        if(target.dataset.pinSetting==='true'){await configurePin();}
        else {const key=target.dataset.setting;state.settings[key]=!state.settings[key];saveState();renderSettings();}
        break;
      case 'enable-pin': await enablePin(); break;
      case 'disable-pin': await disablePin(); break;
      case 'export-backup': exportBackup(); break;
      case 'import-backup': els.importInput.click(); break;
      case 'clear-all-data': await clearAllData(); break;
      case 'api-info': openApiInfo(); break;
      case 'about-app': openAbout(); break;
      default: break;
    }
  } catch (error) {
    toast(error.message || 'حدث خطأ غير متوقع.', 'error', 6000);
  }
}

function handleInput(event) {
  const input = event.target;
  if (input.matches('[data-live-filter]')) filterLiveList(input);
  if (input.matches('[data-sales-filter]')) filterSales();
  if (input.matches('[data-inbox-search]')) {
    ui.inboxSearch = input.value;
    const position = input.selectionStart;
    renderInboxSheet();
    requestAnimationFrame(() => {
      const next = els.sheetContent.querySelector('[data-inbox-search]');
      if (next) { next.focus(); next.setSelectionRange(position,position); }
    });
  }
  if (input.matches('.autosave-profile')) autosaveProfileField(input);
}

function handleChange(event) {
  const input = event.target;
  if (input.matches('[data-setting-select]')) {
    const key=input.dataset.settingSelect;
    state.settings[key]=key==='waitDuration'?Number(input.value):input.value;
    saveState(); applyTheme(); renderSettings();
  }
  if (input.matches('[data-profile-color]')) {
    const draft=ui.profileEditor?.drafts.find(p=>p.number===Number(input.dataset.profileColor));
    if(draft){draft.color=input.value;renderProfileEditorSheet();}
  }
}

document.addEventListener('click', handleClick);
document.addEventListener('input', handleInput);
document.addEventListener('change', handleChange);
els.modalBackdrop.addEventListener('click',()=>closeModal('cancel'));
els.sheetBackdrop.addEventListener('click',closeSheet);
els.importInput.addEventListener('change',(event)=>importBackupFile(event.target.files?.[0]));
els.imageInput.addEventListener('change',async(event)=>{
  const file=event.target.files?.[0]; const n=Number(event.target.dataset.profileNumber);
  if(!file||!ui.profileEditor)return;
  try{const data=await compressImage(file);const draft=ui.profileEditor.drafts.find(p=>p.number===n);if(draft){draft.imageData=data;renderProfileEditorSheet();toast('تم ضغط الصورة وإضافتها للمعاينة.');}}
  catch(error){toast(error.message,'error');}
  finally{event.target.value='';}
});

els.unlockBtn.addEventListener('click',unlockApp);
els.unlockPin.addEventListener('keydown',(event)=>{if(event.key==='Enter')unlockApp();});

els.sheet.addEventListener('touchstart',(event)=>{
  if(els.sheet.scrollTop===0&&ui.inboxEmailId)pullStartY=event.touches[0].clientY;
},{passive:true});
els.sheet.addEventListener('touchend',(event)=>{
  if(!pullStartY)return;
  const endY=event.changedTouches[0].clientY;
  if(endY-pullStartY>75&&ui.inboxEmailId&&els.sheetContent.querySelector('[data-inbox-root]'))refreshInbox();
  pullStartY=0;
},{passive:true});

matchMedia('(prefers-color-scheme: light)').addEventListener?.('change',()=>{if(state.settings.theme==='auto')applyTheme();});

async function init() {
  applyTheme();
  const last = state.lastEmailId ? getEmail(state.lastEmailId) : null;
  if (last) {
    ui.view='email-detail'; ui.selectedEmailId=last.localId; ui.returnView=last.status==='completed'?'sold-emails':last.status==='archived'?'archive':'home';
  }
  await new Promise((resolve)=>setTimeout(resolve,420));
  els.splash.style.opacity='0';
  setTimeout(()=>els.splash.classList.add('hidden'),360);
  if(state.settings.lockEnabled&&state.settings.pinHash){
    els.lockScreen.classList.remove('hidden');els.lockScreen.setAttribute('aria-hidden','false');setTimeout(()=>els.unlockPin.focus(),100);
  }else{
    els.app.classList.remove('hidden');render();
  }
}

init();
