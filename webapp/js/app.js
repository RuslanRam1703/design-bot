/* Mini App дизайнера-фрилансера: портфолио, обо мне, калькулятор, бриф.
 * Работает и внутри Telegram (через telegram-web-app.js), и в обычном
 * браузере для превью/тестирования портфолио/about/калькулятора. Отправка
 * заявки (submitBrief) идёт через authenticated POST /api/leads (см.
 * bot/webserver.py::handle_create_lead) — в обычном браузере без реального
 * Telegram initData сервер её отклонит (401), тем же принципом, что и
 * "Мои заявки" (TG.initData() пустая вне настоящего Telegram).
 */

const HAVE_OPTIONS = [
  { id: "text", label: "Готовый текст" },
  { id: "references", label: "Референсы" },
  { id: "brand", label: "Фирменный стиль" },
  { id: "materials", label: "Готовые материалы" },
  { id: "old_design", label: "Старый дизайн" },
  { id: "none", label: "Ничего нет" },
];

const DEADLINE_OPTIONS = [
  { id: "asap", label: "Как можно скорее" },
  { id: "2weeks", label: "1–2 недели" },
  { id: "month", label: "В течение месяца" },
  { id: "unknown", label: "Срок не определён" },
];

const BUDGET_OPTIONS = [
  { id: "lt20", label: "До 20 000 ₽" },
  { id: "20-40", label: "20 000–40 000 ₽" },
  { id: "40-70", label: "40 000–70 000 ₽" },
  { id: "70-100", label: "70 000–100 000 ₽" },
  { id: "gt100", label: "Более 100 000 ₽" },
  { id: "undecided", label: "Не определился" },
];

const TASK_MAXLEN = 500;

// ---- Обёртка над Telegram WebApp SDK с фолбэком для обычного браузера ----
const realTG = window.Telegram && window.Telegram.WebApp;

const TG = {
  ready() { realTG?.ready(); },
  expand() { realTG?.expand(); },
  // requestFullscreen() пробовали для лайтбокса (Bot API 8.0+) — в реальном
  // Telegram-клиенте exitFullscreen() при закрытии лайтбокса закрывал весь
  // Mini App целиком, а не только лайтбокс (живой баг, воспроизвести и
  // продиагностировать точнее без реального Telegram нельзя). Убрано —
  // лайтбокс работает в обычном (не полноэкранном) режиме Mini App.
  themeParams() { return realTG?.themeParams || {}; },
  colorScheme() { return realTG?.colorScheme || "light"; },
  // initData — подписанный Telegram'ом пакет (user/auth_date/hash), сервер
  // проверяет подпись перед тем как поверить user_id (см. "Мои заявки",
  // bot/telegram_auth.py). Вне настоящего Telegram (обычный браузер) пусто —
  // это ожидаемо, а не ошибка, экран "Мои заявки" должен это учитывать.
  initData() { return realTG?.initData || ""; },
  onThemeChanged(cb) { realTG?.onEvent("themeChanged", cb); },
  backButton: {
    show(onClick) {
      if (!realTG) return;
      realTG.BackButton.show();
      realTG.BackButton.offClick(TG.backButton._current);
      TG.backButton._current = onClick;
      realTG.BackButton.onClick(onClick);
    },
    hide() { realTG?.BackButton.hide(); },
    _current: null,
  },
  sendData(payload) {
    if (realTG && realTG.sendData) {
      realTG.sendData(JSON.stringify(payload));
      // Telegram сам закрывает Mini App после sendData().
    } else {
      console.log("[dev] sendData:", payload);
      state.lastPayload = payload;
      navigate("submitted");
    }
  },
};

function applyTheme() {
  const theme = TG.themeParams();
  // Только акцентные цвета берём из Telegram (это согласованные пары).
  // Фон/текст — наша палитра по colorScheme, см. style.css :root[data-theme].
  const map = {
    link_color: "--tg-theme-link-color",
    button_color: "--tg-theme-button-color",
    button_text_color: "--tg-theme-button-text-color",
  };
  for (const [key, cssVar] of Object.entries(map)) {
    if (theme[key]) document.documentElement.style.setProperty(cssVar, theme[key]);
  }
  if (realTG) document.documentElement.dataset.theme = TG.colorScheme();
}

// ---- Состояние приложения ----
const state = {
  pricing: null,
  portfolio: null,
  about: null,
  uiConfig: null,
  screen: "loading",
  history: [],
  filter: "all",
  currentCase: null,
  myLeads: { status: "idle", items: [], selected: null, ownerHistoryExpanded: false }, // status: idle | loading | loaded | error | no-telegram
  calc: { serviceId: null, openGroupId: null, options: {}, urgent: false, complex: false },
  brief: {
    step: 1,
    openGroupId: null,
    serviceId: null,
    serviceName: null,
    task: "",
    have: [],
    deadline: null,
    budget: null,
    name: "",
    contactValue: "",
    tzMode: null, // null | "none" | "file" | "form"
    tzDetails: { goal: "", mustHave: "", avoid: "", references: "" },
    calc: null,
    source: "direct", // "direct" | "case" | "calculator" | "about" — откуда пришли в заявку, для дизайнера в уведомлении
    sourceCaseId: null,
    sourceCaseTitle: null,
    // Заказ (Order Builder) — конфигурация услуги теперь часть шага 1 брифа,
    // а не отдельного экрана калькулятора (см. renderBrief() step 1).
    orderOptions: {}, // { [optionId]: qty } — рабочее состояние, аналог calc.options
    urgent: false,
    complex: false,
    draftId: null, // генерируется при первом использовании — см. generateDraftId()/init()
  },
  lastPayload: null,
  lastLeadResult: null, // { lead_id, attach_tz, price_range } — ответ POST /api/leads, для renderSubmitted()
  briefSubmitError: null, // текст ошибки под кнопкой "Отправить заявку", если POST /api/leads не удался
  // submitting — единственный источник правды "отправка уже идёт" (НЕ прямое
  // DOM-свойство disabled на кнопке): render() каждый раз пересоздаёт всю
  // разметку шага 7 заново (app.innerHTML = ...), поэтому disabled,
  // выставленный напрямую на элементе, немедленно терялся при следующем
  // render() — кнопка визуально снова становилась кликабельной за доли
  // секунды, и повторные тапы порождали несколько POST /api/leads на один
  // клик (см. аудит). Теперь renderBrief() сам читает этот флаг.
  submitting: false,
  // supplement — полностью отдельное состояние для режима "Дополнить
  // информацию к уже существующей заявке" (см. аудит). Намеренно НЕ часть
  // state.brief и никогда не проходит через persistBriefDraft()/
  // restoreBriefDraft()/clearBriefDraft() — обычный черновик новой заявки
  // не должен ни читаться, ни перезаписываться этим режимом.
  supplement: null, // { leadId, comment, additionalRequirements, references, contact, wantsFile, submitting, error, sent, supplementId }
  // briefEntryPending — true только сразу после того, как init() восстановил
  // из localStorage РЕАЛЬНО начатый черновик (см. briefDraftHasProgress).
  // Пока true, вкладка "Заказать" показывает выбор "Продолжить"/"Начать
  // новую" вместо того, чтобы сразу открыть Order Builder на сохранённом
  // шаге — иначе клиент, не желающий продолжать старую заявку, не имел
  // понятного способа начать новую (см. UX-аудит про восстановленный
  // step 7). НЕ персистится — это чисто сессионное "решение ещё не
  // принято", не часть самого черновика. Осознанные "новый заход"-пути
  // (CTA из кейса/калькулятора/about, "В начало", "Начать новую заявку" из
  // supplement) сбрасывают этот флаг сами — см. resetBriefState().
  briefEntryPending: false,
};

function generateDraftId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Услуги вида "Графический дизайн — X" схлопываются в одну категорию с
// подэкраном выбора типа — иначе выглядят как три разные услуги.
function getMenuEntries(pricing) {
  const groups = pricing.groups || [];
  const serviceToGroup = {};
  for (const g of groups) for (const id of g.service_ids) serviceToGroup[id] = g;

  const entries = [];
  const insertedGroups = new Set();
  for (const s of pricing.services) {
    const g = serviceToGroup[s.id];
    if (!g) {
      entries.push({ kind: "service", service: s });
    } else if (!insertedGroups.has(g.id)) {
      entries.push({ kind: "group", group: g });
      insertedGroups.add(g.id);
    }
  }
  return entries;
}

function navigate(screen, { resetBrief = false, hardReset = false, pushHistory = true } = {}) {
  if (pushHistory && state.screen !== "loading") state.history.push(state.screen);
  if (resetBrief) resetBriefState({ hardReset });
  state.screen = screen;
  render();
}

function goBack() {
  const prev = state.history.pop();
  state.screen = prev || "portfolio";
  render();
}

// Черновик брифа переживает переключение вкладок и Back в рамках одной
// сессии WebView через сам state, но не переживает полный перезапуск
// WebView (Telegram иногда его уничтожает и создаёт заново). localStorage
// закрывает именно этот разрыв — не заменяет resetBriefState/навигацию,
// а просто восстанавливает state.brief при новом заходе в init().
const BRIEF_DRAFT_STORAGE_KEY = "designAssistant.briefDraft";

function persistBriefDraft() {
  try {
    localStorage.setItem(BRIEF_DRAFT_STORAGE_KEY, JSON.stringify(state.brief));
  } catch (e) {
    // приватный режим браузера / переполненная квота — черновик просто не переживёт перезапуск
  }
}

// render() уже вызывает persistBriefDraft() первым делом (см. ниже), но
// свободнотекстовые поля (task/name/contact/tz-детали) намеренно НЕ вызывают
// render() на каждый input — полная перерисовка DOM на каждое нажатие сбила
// бы фокус и позицию курсора в textarea/input. Из-за этого набранный текст
// не попадал в localStorage до следующей навигационной кнопки — если
// Telegram пересоздаст WebView раньше (см. коммент выше про "не переживает
// полный перезапуск"), только что напечатанное терялось (Stage C аудит).
// Debounce — тот же persistBriefDraft(), просто с задержкой и БЕЗ render():
// ни один DOM-узел не трогается, фокус/курсор не задеты.
let _briefPersistDebounceTimer = null;
function persistBriefDraftDebounced() {
  clearTimeout(_briefPersistDebounceTimer);
  _briefPersistDebounceTimer = setTimeout(persistBriefDraft, 400);
}

// Возвращает true, только если в localStorage реально лежал корректно
// разбираемый черновик и он был влит в state.brief — это и есть сигнал
// "существует восстановленный draft" для briefEntryPending (см. init()).
function restoreBriefDraft() {
  let parseOk = null;
  try {
    const saved = localStorage.getItem(BRIEF_DRAFT_STORAGE_KEY);
    if (saved) {
      state.brief = { ...state.brief, ...JSON.parse(saved) };
      parseOk = true;
    }
  } catch (e) {
    // повреждённые данные в хранилище — остаёмся с дефолтным пустым брифом
    parseOk = false;
  }
  return parseOk === true;
}

// "Есть реальный прогресс" — иначе экран выбора "Продолжить/Начать новую"
// показывался бы даже для нетронутого дефолтного черновика (шаг 1, ничего
// не выбрано), что не несёт клиенту никакой пользы и выглядело бы как
// лишний вопрос на пустом месте.
function briefDraftHasProgress(b) {
  return b.step > 1 || !!b.serviceId;
}

function clearBriefDraft() {
  try {
    localStorage.removeItem(BRIEF_DRAFT_STORAGE_KEY);
  } catch (e) {
    // нечего чистить, если setItem выше и так не сработал
  }
}

function resetBriefState({ hardReset = false } = {}) {
  // hardReset: true — только "В начало" после отправки заявки. Обычный
  // resetBriefState (CTA из кейса/калькулятора/about) намеренно переносит
  // service/calc в свежий шаг 1 — это и есть предзаполнение. Но "В начало" —
  // осознанное "забыть всё и начать с чистого листа": без него услуга (и
  // вся её конфигурация опций/цены) от предыдущего заказа оставалась бы
  // выбранной на шаге 1 при следующем обычном заходе через таб "Заявка".
  state.brief = {
    // Услуга теперь настраивается на самом шаге 1 (Order) — если она уже
    // предзаполнена (кейс/калькулятор), шаг 1 сразу покажет её конфигурацию
    // (опции/цену), а не перепрыгивает к шагу 2, как раньше.
    step: 1,
    openGroupId: null,
    serviceId: hardReset ? null : state.brief.serviceId,
    serviceName: hardReset ? null : state.brief.serviceName,
    task: "",
    have: [],
    deadline: null,
    budget: null,
    name: "",
    contactValue: "",
    tzMode: null,
    tzDetails: { goal: "", mustHave: "", avoid: "", references: "" },
    calc: hardReset ? null : state.brief.calc,
    source: hardReset ? "direct" : (state.brief.source || "direct"),
    sourceCaseId: hardReset ? null : (state.brief.sourceCaseId || null),
    sourceCaseTitle: hardReset ? null : (state.brief.sourceCaseTitle || null),
    orderOptions: hardReset ? {} : (state.brief.orderOptions || {}),
    urgent: hardReset ? false : (state.brief.urgent || false),
    complex: hardReset ? false : (state.brief.complex || false),
    // resetBriefState всегда означает осознанный новый заход в заявку
    // (кейс/калькулятор/about) — новый draftId, чтобы не перезаписать
    // предыдущую, уже отправленную заявку при следующем submit.
    draftId: generateDraftId(),
  };
  // Любой resetBriefState() — это уже осознанный выбор пользователя (CTA
  // из кейса/калькулятора/about, "В начало", "Начать новую заявку"), так
  // что экран выбора "Продолжить/Начать новую" для него неактуален —
  // решение по факту уже принято этим самым вызовом.
  state.briefEntryPending = false;
}

// ---- Загрузка данных и старт ----
async function init() {
  TG.ready();
  TG.expand();
  applyTheme();
  TG.onThemeChanged(applyTheme);
  const restored = restoreBriefDraft();
  if (!state.brief.draftId) state.brief.draftId = generateDraftId();
  // См. state.briefEntryPending — восстановленный черновик БЕЗ реального
  // прогресса (пустой шаг 1) не требует спрашивать "продолжить или новую":
  // спрашивать там нечего.
  state.briefEntryPending = restored && briefDraftHasProgress(state.brief);

  const [pricing, portfolio, about, uiConfig] = await Promise.all([
    fetch("/data/pricing.json").then((r) => r.json()),
    fetch("/data/portfolio.json").then((r) => r.json()),
    fetch("/data/about.json").then((r) => r.json()),
    fetch("/data/ui_config.json").then((r) => r.json()),
  ]);
  state.pricing = pricing;
  state.portfolio = portfolio;
  state.about = about;
  state.uiConfig = uiConfig;

  // Путь (/calculator, /brief, /about) — основной способ понять, с какого
  // экрана запущено приложение: кнопки меню бота теперь ведут на разные
  // пути, а не на один "/" с разным ?screen=, — так надёжнее (Telegram
  // может переиспользовать уже открытый WebView и не перечитывать
  // query-строку при повторном запуске). ?screen= оставлен как запасной
  // вариант для ручного тестирования в браузере.
  const path = window.location.pathname.replace(/\/+$/, "");
  const params = new URLSearchParams(window.location.search);
  const initialScreen = path.endsWith("/calculator") ? "calculator"
    : path.endsWith("/brief") ? "brief"
    : path.endsWith("/about") ? "about"
    : path.endsWith("/myleads") ? "myleads"
    : path.endsWith("/portfolio") ? "portfolio"
    : params.get("screen");
  if (initialScreen === "calculator") state.screen = "calculator";
  else if (initialScreen === "brief") state.screen = "brief";
  else if (initialScreen === "about") state.screen = "about";
  else if (initialScreen === "myleads") state.screen = "myleads";
  else state.screen = "portfolio";

  // Если админ выключил именно этот экран в /admin -> Меню и навигация —
  // не показываем пустой/битый вид, откатываемся на портфолио.
  const menu = state.uiConfig.menu || {};
  if (menu[state.screen] === false) state.screen = "portfolio";

  render();
}

// ---- Нижнее меню (переключение экранов внутри уже открытого Mini App) ----
// Кнопки в чате Telegram открывают/поднимают уже открытый WebView, но НЕ
// перезагружают его при повторном нажатии — поэтому переключаться между
// портфолио/калькулятором/заявкой нужно средствами самого приложения.
// Калькулятор больше не отдельная вкладка — конфигурация услуги и цены
// теперь часть шага 1 заявки (Order Builder, см. renderBrief()). Экран
// /calculator и команда бота остаются рабочими (не удалены), просто не
// навязываются как равноценная альтернатива заявке в постоянной навигации.
const TAB_SCREENS = [
  { id: "portfolio", icon: "📁", label: "Портфолио" },
  { id: "about", icon: "👤", label: "Обо мне" },
  { id: "brief", icon: "✍️", label: "Заказать" },
  { id: "myleads", icon: "📋", label: "Мои заявки" },
];

function renderTabBar() {
  const menu = (state.uiConfig && state.uiConfig.menu) || {};
  const visibleTabs = TAB_SCREENS.filter((t) => menu[t.id] !== false);
  const active = state.screen === "case" ? "portfolio" : state.screen;
  const items = visibleTabs.map(
    (t) => `
      <button class="tab-item ${t.id === active ? "active" : ""}" data-tab="${t.id}">
        <span class="tab-icon">${t.icon}</span>
        <span class="tab-label">${t.label}</span>
      </button>`
  ).join("");
  return `<nav class="tab-bar">${items}</nav>`;
}

function attachTabBarEvents() {
  document.querySelectorAll("[data-tab]").forEach((el) =>
    el.addEventListener("click", () => {
      const screen = el.dataset.tab;
      if (screen === state.screen) return;
      if (screen === "brief") {
        // В отличие от остальных табов, "Заявка" может быть заполнена
        // частично — заходим туда как обычной навигацией (сохраняя ответы
        // и историю для корректного "назад"), а не как в свежий таб.
        // Осознанный сброс брифа уже есть отдельно — там, где он оправдан
        // намерением пользователя: CTA в кейсе/калькуляторе/about (resetBrief: true).
        navigate(screen);
        return;
      }
      if (screen === "myleads") {
        // Обычный вход через таб не должен повторно открывать деталь
        // заявки, оставшуюся выбранной с прошлого визита (state.myLeads.selected
        // переживает переключение на другой таб и обратно — сам этот
        // обработчик его не трогал) — иначе renderMyLeads() снова открыл бы
        // тот же detail вместо списка, да ещё и с устаревшим status
        // (status оставался "loaded", рефетч не запускался). Тот же паттерн
        // "вернуться в 'Мои заявки' с чистого листа", что уже используется
        // в closeSupplementAfterSubmit() (Product Readiness audit, Batch 3).
        state.myLeads.selected = null;
        state.myLeads.status = "idle";
      }
      state.history = [];
      state.screen = screen;
      render();
    })
  );
}

// ---- Рендер ----
function render() {
  persistBriefDraft();
  const app = document.getElementById("app");
  let content;
  let showTabBar = true;

  switch (state.screen) {
    case "portfolio":
      content = renderPortfolio();
      TG.backButton.hide();
      break;
    case "case":
      content = renderCase();
      TG.backButton.show(goBack);
      break;
    case "about":
      content = renderAbout();
      // Как и "myleads" выше — About достижим ТОЛЬКО через таб (history
      // всегда пуст в этот момент), кроме случая, когда что-то реально
      // запушило экран перед ним (сейчас такого пути нет, но проверяем по
      // факту, а не хардкодим "всегда hide", на случай будущего входа с
      // историей). Раньше BackButton показывался безусловно — goBack() на
      // пустом history падал в захардкоженный fallback "portfolio", даже
      // если пользователь пришёл не оттуда (Product Readiness audit, Batch 3).
      if (state.history.length > 0) TG.backButton.show(goBack);
      else TG.backButton.hide();
      break;
    case "calculator":
      content = renderCalculator();
      // Тот же принцип, что и у "about" выше — калькулятор достижим только
      // deep-link'ом (history пуст), см. renderCalculator()'s "←" ниже.
      if (state.history.length > 0) TG.backButton.show(goBack);
      else TG.backButton.hide();
      break;
    case "brief":
      content = renderBrief();
      // В отличие от about/calculator, brief достижим и с историей (CTA из
      // кейса/калькулятора/about, таб), и без (myleads/supplement "начать
      // новую" — pushHistory:false) — тот же принцип, что и у "about", но
      // здесь условие реально бывает в обе стороны, не только теоретически.
      if (state.history.length > 0) TG.backButton.show(goBack);
      else TG.backButton.hide();
      break;
    case "myleads":
      content = renderMyLeads();
      // Список — свой собственный уровень (как portfolio), нативный
      // BackButton скрыт. Деталь заявки (state.myLeads.selected) — это уже
      // не самый верхний уровень внутри этого экрана: нативный BackButton
      // показан и делает то же самое, что и in-app "← К списку заявок"
      // (см. closeMyLeadDetail) — раньше был скрыт даже здесь.
      if (state.myLeads.selected) TG.backButton.show(closeMyLeadDetail);
      else TG.backButton.hide();
      break;
    case "supplement":
      content = renderSupplement();
      // Форма (ещё не отправлено) — обычный уровень, topbar "←" и нативный
      // BackButton оба ведут на goBack(). После отправки
      // (state.supplement.sent) renderSupplement() намеренно убирает topbar —
      // единственная видимая in-app навигация #supplement-back-to-lead;
      // нативный BackButton должен делать то же самое (см.
      // closeSupplementAfterSubmit), а не goBack(), который мог бы вернуть
      // через устаревшую state.history без сброса/рефетча "Мои заявки" —
      // тот же класс несоответствия, что уже исправлен для "myleads"
      // (см. closeMyLeadDetail).
      TG.backButton.show(state.supplement && state.supplement.sent ? closeSupplementAfterSubmit : goBack);
      break;
    case "submitted":
      content = renderSubmitted();
      TG.backButton.hide();
      showTabBar = false;
      break;
    default:
      content = "<div class=\"empty-state\">Загрузка…</div>";
      showTabBar = false;
  }

  app.innerHTML = content + (showTabBar ? renderTabBar() : "");

  switch (state.screen) {
    case "portfolio": attachPortfolioEvents(); break;
    case "case": attachCaseEvents(); break;
    case "about": attachAboutEvents(); break;
    case "calculator": attachCalculatorEvents(); break;
    case "brief": attachBriefEvents(); break;
    case "myleads": attachMyLeadsEvents(); break;
    case "supplement": attachSupplementEvents(); break;
  }
  if (showTabBar) attachTabBarEvents();

  window.scrollTo(0, 0);
}

// Экранирует и текст, и значения атрибутов (используется в value="...") —
// textContent/innerHTML этого не даёт, т.к. кавычки в текстовых узлах не кодируются.
function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Batch 3 — портфолио/about-изображения теперь бывают двух видов: старые
// демо-SVG (относительный путь вида "img/portfolio/demo_case_1.svg",
// отдаётся тем же локальным static-роутом, что и раньше) и новые R2-загрузки
// (полный публичный URL, см. bot/r2_storage.py). Различаем по "http" в
// начале строки — ничего в JSON-схеме не меняется, миграция не нужна:
// старые записи как были относительными путями, так и остаются.
function imageSrc(path) {
  return path.startsWith("http") ? path : `/${path}`;
}

// ---- Экран: Портфолио ----
function renderPortfolio() {
  const { types, cases } = state.portfolio;
  const filtered = state.filter === "all" ? cases : cases.filter((c) => c.type === state.filter);

  const chips = [{ id: "all", label: "Все" }, ...types.map((t) => ({ id: t.id, label: t.label }))]
    .map(
      (t) =>
        `<button class="chip ${t.id === state.filter ? "active" : ""}" data-filter="${t.id}">${escapeHtml(t.label)}</button>`
    )
    .join("");

  const cards = filtered
    .map(
      (c) => `
      <button class="card" data-case="${c.id}">
        ${c.cover ? `<img src="${imageSrc(c.cover)}" alt="" loading="lazy" />` : '<div class="card-cover-empty"></div>'}
        <div class="card-title">${escapeHtml(c.title)}</div>
      </button>`
    )
    .join("");

  return `
    <div class="topbar"><h1>📁 Портфолио</h1></div>
    <div class="chips">${chips}</div>
    <div class="grid">${cards || '<div class="empty-state">Пока нет кейсов в этой категории</div>'}</div>
  `;
}

function attachPortfolioEvents() {
  document.querySelectorAll(".chip").forEach((el) =>
    el.addEventListener("click", () => {
      state.filter = el.dataset.filter;
      render();
    })
  );
  document.querySelectorAll(".card").forEach((el) =>
    el.addEventListener("click", () => {
      state.currentCase = state.portfolio.cases.find((c) => c.id === el.dataset.case);
      navigate("case");
    })
  );
}

// ---- Экран: Кейс ----
// Разделы (sections) — гибкое содержимое вместо жёстких task/solution/
// result: разные типы кейсов (лендинг/брендинг/UX-UI/графика) описываются
// по-разному (см. /admin -> Кейсы -> Разделы). Кейсы, ещё не переведённые
// на sections, продолжают рендериться по task/solution/result — так они
// выглядели и раньше, ничего не ломается для уже существующих данных.
function renderCaseContent(c) {
  if (c.sections && c.sections.length) {
    return c.sections.map((s) => {
      if (s.type === "gallery") {
        const imgs = (s.images || []).map((src) => `<img src="${imageSrc(src)}" alt="" />`).join("");
        return `<div class="case-block"><div class="label">${escapeHtml(s.title)}</div><div class="case-section-gallery">${imgs || '<p class="hint">Пока нет изображений</p>'}</div></div>`;
      }
      return `<div class="case-block"><div class="label">${escapeHtml(s.title)}</div><p>${escapeHtml(s.content || "")}</p></div>`;
    }).join("");
  }
  return `
    <div class="case-block"><div class="label">Задача</div><p>${escapeHtml(c.task || "")}</p></div>
    <div class="case-block"><div class="label">Решение</div><p>${escapeHtml(c.solution || "")}</p></div>
    <div class="case-block"><div class="label">Результат</div><p>${escapeHtml(c.result || "")}</p></div>
  `;
}

function renderCase() {
  const c = state.currentCase;
  const hasImages = c.images && c.images.length > 0;
  const images = hasImages
    ? c.images.map((src, i) => `<img src="${imageSrc(src)}" alt="" data-lightbox-index="${i}" />`).join("")
    : `<div class="case-images-empty">Пока нет изображений</div>`;
  // external_url — необязательное поле (см. bot/content_store.py -> CASE_FIELD_LABELS);
  // ссылка показывается, только если дизайнер её заполнил для этого конкретного кейса.
  const externalLink = c.external_url
    ? `<a class="external-case-link" href="${escapeHtml(c.external_url)}" target="_blank" rel="noopener">Смотреть подробнее ↗</a>`
    : "";
  return `
    <div class="topbar">
      <button class="back-btn" id="back">←</button>
      <h1>${escapeHtml(c.title)}</h1>
    </div>
    <div class="case-images">${images}</div>
    ${renderCaseContent(c)}
    ${externalLink}
    <button class="btn btn-primary" id="want-similar">Хочу похожий проект</button>
  `;
}

function attachCaseEvents() {
  document.getElementById("back").addEventListener("click", goBack);
  const c = state.currentCase;
  document.querySelectorAll("[data-lightbox-index]").forEach((el) =>
    el.addEventListener("click", () => openLightbox(c.images.map(imageSrc), Number(el.dataset.lightboxIndex)))
  );
  document.getElementById("want-similar").addEventListener("click", () => {
    // order_template.service_id (см. data/portfolio.json) — переносим ТОЛЬКО
    // услугу, не опции. order_template.options — статические demo-данные:
    // ни один код в bot/content_store.py их не создаёт и не обновляет, и
    // поля order_template нет в bot/admin_keyboards.py::CASE_FIELD_LABELS —
    // то есть дизайнер не может ни увидеть, ни осознанно задать их через
    // /admin. Раз нет доказательства, что конкретная опция реально относится
    // к этому кейсу, Order Builder не должен её предзаполнять (см. UX-аудит
    // про самопроизвольно отмеченные чекбоксы) — сам кейс при этом не
    // меняется, заказ — независимая копия.
    const template = c.order_template;
    const serviceId = template?.service_id || c.related_service;
    const service = state.pricing.services.find((s) => s.id === serviceId);
    state.brief.serviceId = service?.id || null;
    state.brief.serviceName = service?.name || null;
    state.brief.calc = null;
    state.brief.source = "case";
    state.brief.sourceCaseId = c.id;
    state.brief.sourceCaseTitle = c.title;
    state.brief.orderOptions = {};
    state.brief.urgent = false;
    state.brief.complex = false;
    navigate("brief", { resetBrief: true });
  });
}

// ---- Lightbox: просмотр изображений кейса крупным планом (несколько
// изображений — стрелки/свайп/счётчик; одно — просто открыть/закрыть).
// Без requestFullscreen() — пробовали раньше, в реальном Telegram-клиенте
// exitFullscreen() при закрытии закрывал весь Mini App целиком, а не
// только картинку (живой баг, воспроизведённый пользователем). Работает
// в обычном (не полноэкранном) режиме Mini App. ----
let lightboxEl = null;
let lightboxImages = [];
let lightboxIndex = 0;

function openLightbox(images, index) {
  lightboxImages = images;
  lightboxIndex = index;
  if (!lightboxEl) {
    lightboxEl = document.createElement("div");
    lightboxEl.className = "lightbox";
    lightboxEl.innerHTML = `
      <button class="lightbox-close" aria-label="Закрыть">✕</button>
      <button class="lightbox-prev" aria-label="Предыдущее">‹</button>
      <img alt="" />
      <button class="lightbox-next" aria-label="Следующее">›</button>
      <div class="lightbox-counter"></div>
    `;
    document.body.appendChild(lightboxEl);
    lightboxEl.querySelector(".lightbox-close").addEventListener("click", closeLightbox);
    lightboxEl.querySelector(".lightbox-prev").addEventListener("click", (e) => { e.stopPropagation(); lightboxStep(-1); });
    lightboxEl.querySelector(".lightbox-next").addEventListener("click", (e) => { e.stopPropagation(); lightboxStep(1); });
    lightboxEl.addEventListener("click", (e) => { if (e.target === lightboxEl) closeLightbox(); });
    let touchStartX = null;
    lightboxEl.addEventListener("touchstart", (e) => { touchStartX = e.touches[0].clientX; });
    lightboxEl.addEventListener("touchend", (e) => {
      if (touchStartX === null) return;
      const dx = e.changedTouches[0].clientX - touchStartX;
      if (Math.abs(dx) > 40) lightboxStep(dx > 0 ? -1 : 1);
      touchStartX = null;
    });
  }
  renderLightboxImage();
  lightboxEl.classList.add("open");
}

function lightboxStep(delta) {
  lightboxIndex = (lightboxIndex + delta + lightboxImages.length) % lightboxImages.length;
  renderLightboxImage();
}

function renderLightboxImage() {
  lightboxEl.querySelector("img").src = lightboxImages[lightboxIndex];
  const multi = lightboxImages.length > 1;
  lightboxEl.querySelector(".lightbox-counter").textContent = multi ? `${lightboxIndex + 1} / ${lightboxImages.length}` : "";
  lightboxEl.querySelector(".lightbox-prev").style.display = multi ? "" : "none";
  lightboxEl.querySelector(".lightbox-next").style.display = multi ? "" : "none";
}

function closeLightbox() {
  if (lightboxEl) lightboxEl.classList.remove("open");
}

document.addEventListener("keydown", (e) => {
  if (!lightboxEl || !lightboxEl.classList.contains("open")) return;
  if (e.key === "Escape") closeLightbox();
  else if (e.key === "ArrowLeft") lightboxStep(-1);
  else if (e.key === "ArrowRight") lightboxStep(1);
});

// ---- Экран: Обо мне ----
function renderAbout() {
  const a = state.about;

  const specItems = a.specialization.map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  const toolChips = a.tools.map((t) => `<span class="chip-static">${escapeHtml(t)}</span>`).join("");
  // Навыки — профессиональные компетенции (UX-исследования, прототипирование...)
  // отдельно от Инструментов (софт) — раньше это было смешано в одном списке.
  const skillsHTML = a.skills && a.skills.length
    ? `
      <div class="about-block">
        <h2>Навыки</h2>
        <div class="chips-static">${a.skills.map((s) => `<span class="chip-static">${escapeHtml(s)}</span>`).join("")}</div>
      </div>`
    : "";

  // Опыт работы (resume-style записи) — необязательный, дополняющий блок:
  // строка "Опыт: N лет" выше остаётся кратким резюме, experience[] даёт
  // детализацию по местам/проектам, если дизайнер её заполнил.
  const experienceHTML = a.experience && a.experience.length
    ? `
      <div class="about-block">
        <h2>Опыт работы</h2>
        ${a.experience.map((e) => `
          <div class="experience-entry">
            <div class="experience-role">${escapeHtml(e.role)} — ${escapeHtml(e.company)}</div>
            <div class="hint">${escapeHtml(e.period)}</div>
            ${e.description ? `<p>${escapeHtml(e.description)}</p>` : ""}
          </div>`).join("")}
      </div>`
    : "";

  const educationHTML = a.education && a.education.enabled && a.education.items.length
    ? `
      <div class="about-block">
        <h2>Образование</h2>
        <ul class="plain-list">${a.education.items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>
      </div>`
    : "";

  const linksHTML = a.links && a.links.length
    ? `
      <div class="about-block">
        <h2>Ссылки</h2>
        <div class="links-list">${a.links.map((l) => `<a href="${escapeHtml(l.url)}" target="_blank" rel="noopener">${escapeHtml(l.label)}</a>`).join("")}</div>
      </div>`
    : "";

  return `
    <div class="topbar">
      ${state.history.length > 0 ? '<button class="back-btn" id="back">←</button>' : ""}
      <h1>👤 Обо мне</h1>
    </div>

    <div class="about-header">
      <img class="about-avatar" src="${imageSrc(a.avatar)}" alt="" />
      <div class="about-name">${escapeHtml(a.name)}</div>
      <div class="hint">${escapeHtml(a.tagline)}</div>
      ${a.location ? `<div class="hint">📍 ${escapeHtml(a.location)}</div>` : ""}
    </div>

    <div class="about-block">
      <h2>Специализация</h2>
      <ul class="plain-list">${specItems}</ul>
    </div>

    ${skillsHTML}

    <div class="about-block">
      <h2>Инструменты</h2>
      <div class="chips-static">${toolChips}</div>
    </div>

    <div class="about-block">
      <h2>Опыт</h2>
      <p>Опыт в дизайне: ${escapeHtml(a.experience_years)}</p>
      <p class="hint">${escapeHtml(a.experience_text)}</p>
    </div>

    <div class="about-block">
      <h2>Подход к работе</h2>
      <p>${escapeHtml(a.approach)}</p>
    </div>

    ${experienceHTML}

    ${educationHTML}
    ${linksHTML}

    <div class="btn-row">
      <button class="btn btn-secondary" id="about-portfolio-cta">Смотреть портфолио</button>
      <button class="btn btn-primary" id="about-cta">Оставить заявку</button>
    </div>
  `;
}

function attachAboutEvents() {
  const backBtn = document.getElementById("back");
  if (backBtn) backBtn.addEventListener("click", goBack);

  document.querySelectorAll("[data-case]").forEach((el) =>
    el.addEventListener("click", () => {
      state.currentCase = state.portfolio.cases.find((c) => c.id === el.dataset.case);
      navigate("case");
    })
  );

  const cta = document.getElementById("about-cta");
  if (cta) cta.addEventListener("click", () => {
    // resetBrief:true (см. resetBriefState) переносит serviceId/serviceName/
    // calc/orderOptions/urgent/complex как есть, если явно не обнулить —
    // без этого сюда протекла бы конфигурация, оставшаяся от предыдущего
    // "Похожий заказ" в кейсе (см. production-аудит, P2-9). У About своей
    // услуги нет, так что просто обнуляем — как и Case CTA чуть выше по
    // файлу обнуляет sourceCaseId/sourceCaseTitle для своего source.
    state.brief.serviceId = null;
    state.brief.serviceName = null;
    state.brief.calc = null;
    state.brief.orderOptions = {};
    state.brief.urgent = false;
    state.brief.complex = false;
    state.brief.source = "about";
    state.brief.sourceCaseId = null;
    state.brief.sourceCaseTitle = null;
    navigate("brief", { resetBrief: true });
  });

  const portfolioCta = document.getElementById("about-portfolio-cta");
  if (portfolioCta) portfolioCta.addEventListener("click", () => navigate("portfolio"));
}

// ---- Экран: Калькулятор ----
function attachCalculatorEvents() {
  const backBtn = document.getElementById("back");
  if (backBtn) backBtn.addEventListener("click", goBack);

  document.querySelectorAll("[data-service]").forEach((el) =>
    el.addEventListener("click", () => {
      state.calc.serviceId = el.dataset.service;
      state.calc.options = {};
      render();
    })
  );

  document.querySelectorAll("[data-group]").forEach((el) =>
    el.addEventListener("click", () => {
      state.calc.openGroupId = el.dataset.group;
      render();
    })
  );

  const backToCategories = document.getElementById("back-to-categories");
  if (backToCategories) backToCategories.addEventListener("click", (e) => {
    e.preventDefault();
    state.calc.openGroupId = null;
    render();
  });

  const changeBtn = document.getElementById("change-service");
  if (changeBtn) changeBtn.addEventListener("click", () => {
    const currentGroup = (state.pricing.groups || []).find((g) => g.service_ids.includes(state.calc.serviceId));
    state.calc.serviceId = null;
    state.calc.openGroupId = currentGroup ? currentGroup.id : null;
    render();
  });

  document.querySelectorAll("[data-option-toggle]").forEach((el) =>
    el.addEventListener("change", () => {
      const id = el.dataset.optionToggle;
      if (el.checked) state.calc.options[id] = 1;
      else delete state.calc.options[id];
      render();
    })
  );

  document.querySelectorAll("[data-qty-plus]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.qtyPlus;
      state.calc.options[id] = (state.calc.options[id] || 1) + 1;
      render();
    })
  );
  document.querySelectorAll("[data-qty-minus]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.qtyMinus;
      const cur = state.calc.options[id] || 1;
      if (cur <= 1) delete state.calc.options[id];
      else state.calc.options[id] = cur - 1;
      render();
    })
  );

  const urgentToggle = document.getElementById("urgent-toggle");
  if (urgentToggle) urgentToggle.addEventListener("change", () => {
    state.calc.urgent = urgentToggle.checked;
    render();
  });
  const complexToggle = document.getElementById("complex-toggle");
  if (complexToggle) complexToggle.addEventListener("change", () => {
    state.calc.complex = complexToggle.checked;
    render();
  });

  const submitBtn = document.getElementById("submit-with-calc");
  if (submitBtn) submitBtn.addEventListener("click", () => {
    const result = calculatePrice(state.pricing, state.calc.serviceId, state.calc.options, state.calc.urgent, state.calc.complex);
    state.brief.serviceId = result.service.id;
    state.brief.serviceName = result.service.name;
    // orderOptions/urgent/complex — рабочее состояние для шага 1 брифа
    // (Order Builder); calc — снимок в формате payload, вычисляется заново
    // при выходе с шага 1 (см. order-next), храним здесь только для полноты.
    state.brief.orderOptions = { ...state.calc.options };
    state.brief.urgent = state.calc.urgent;
    state.brief.complex = state.calc.complex;
    state.brief.calc = {
      service_id: state.calc.serviceId,
      options: Object.entries(state.calc.options).map(([id, qty]) => ({ id, qty })),
      urgent: state.calc.urgent,
      complex: state.calc.complex,
    };
    state.brief.source = "calculator";
    state.brief.sourceCaseId = null;
    state.brief.sourceCaseTitle = null;
    navigate("brief", { resetBrief: true });
  });
}

function serviceItemHTML(s, label) {
  return `
    <button class="service-item" data-service="${s.id}">
      <div>
        <div class="name">${escapeHtml(label ?? s.name)}</div>
        <div class="price">от ${formatMoney(s.base_price)} · ${s.term_min}–${s.term_max} дн.</div>
      </div>
      <div>›</div>
    </button>`;
}

function renderCalculator() {
  const { pricing } = state;
  // Калькулятор достижим только deep-link'ом — history пуст (тот же
  // принцип, что и in-app "←" в renderAbout()). Один общий флаг для всех
  // трёх return-точек ниже, а не дублировать условие в каждой.
  const backBtnHTML = state.history.length > 0 ? '<button class="back-btn" id="back">←</button>' : "";

  if (!state.calc.serviceId && state.calc.openGroupId) {
    const group = pricing.groups.find((g) => g.id === state.calc.openGroupId);
    const items = group.service_ids
      .map((id) => pricing.services.find((s) => s.id === id))
      .map((s) => serviceItemHTML(s, s.short_name || s.name))
      .join("");
    return `
      <div class="topbar">
        ${backBtnHTML}
        <h1>💰 Калькулятор</h1>
      </div>
      <p class="section-lead"><a href="#" id="back-to-categories">← Все категории</a></p>
      <h2>${escapeHtml(group.label)}</h2>
      <p class="hint">Выберите тип задачи:</p>
      <div class="service-list">${items}</div>
    `;
  }

  if (!state.calc.serviceId) {
    const items = getMenuEntries(pricing)
      .map((entry) => {
        if (entry.kind === "service") return serviceItemHTML(entry.service);
        const g = entry.group;
        return `
          <button class="service-item" data-group="${g.id}">
            <div>
              <div class="name">${escapeHtml(g.label)}</div>
              <div class="price">${g.service_ids.length} варианта</div>
            </div>
            <div>›</div>
          </button>`;
      })
      .join("");
    return `
      <div class="topbar">
        ${backBtnHTML}
        <h1>💰 Калькулятор</h1>
      </div>
      <p class="section-lead">Выберите услугу:</p>
      <div class="service-list">${items}</div>
    `;
  }

  const service = state.pricing.services.find((s) => s.id === state.calc.serviceId);
  const options = calcServiceOptions(pricing, service.id);
  const result = calculatePrice(pricing, service.id, state.calc.options, state.calc.urgent, state.calc.complex);

  const optionRows = options
    .map((o) => {
      const checked = Boolean(state.calc.options[o.id]);
      const qty = state.calc.options[o.id] || 1;
      const qtyControl = o.multipliable && checked
        ? `<div class="qty-control">
             <button type="button" data-qty-minus="${o.id}">−</button>
             <span>${qty}</span>
             <button type="button" data-qty-plus="${o.id}">+</button>
           </div>`
        : "";
      return `
        <div class="option-row">
          <label class="option-main">
            <input type="checkbox" data-option-toggle="${o.id}" ${checked ? "checked" : ""} />
            <div>
              <div class="option-name">${escapeHtml(o.name)}</div>
              <div class="option-price">+${formatMoney(o.price)}, +${formatDays(o.days)} дн.${o.multipliable ? " · можно несколько" : ""}</div>
            </div>
          </label>
          ${qtyControl}
        </div>`;
    })
    .join("");

  return `
    <div class="topbar">
      ${backBtnHTML}
      <h1>💰 Калькулятор</h1>
    </div>
    <div class="summary-box">
      Услуга: <b>${escapeHtml(service.name)}</b>
      · <a href="#" id="change-service">изменить</a>
    </div>
    ${service.includes ? `<p class="hint">В базовую стоимость входит: ${escapeHtml(service.includes)}</p>` : ""}

    ${optionRows}

    <div class="toggle-row">
      <span>Срочный проект (+25%)</span>
      <label class="switch"><input type="checkbox" id="urgent-toggle" ${state.calc.urgent ? "checked" : ""} /><span class="track"></span></label>
    </div>
    <div class="toggle-row">
      <span>Высокая сложность (+20%)</span>
      <label class="switch"><input type="checkbox" id="complex-toggle" ${state.calc.complex ? "checked" : ""} /><span class="track"></span></label>
    </div>

    <div class="result-box">
      <div class="price">${formatMoney(result.priceFrom)} – ${formatMoney(result.priceTo)}</div>
      <div class="term">${formatDays(result.termFrom)}–${formatDays(result.termTo)} рабочих дней</div>
      <div class="hint">Точная сумма — предварительная, дизайнер подтвердит после брифа</div>
    </div>

    <button class="btn btn-primary" id="submit-with-calc">Отправить заявку с этим расчётом</button>
  `;
}

// ---- Экран: Бриф ----
// 7 шагов: шаг "ТЗ" вынесен из "Контактов" в отдельный шаг — раньше один
// экран одновременно спрашивал имя, контакт, режим ТЗ и (условно) 4 поля
// под-формы, то есть 3-4 решения сразу на шаге с наибольшей значимостью.
const BRIEF_TOTAL_STEPS = 7;

function attachBriefEvents() {
  if (state.briefEntryPending) {
    const continueBtn = document.getElementById("brief-entry-continue");
    if (continueBtn) continueBtn.addEventListener("click", () => {
      // "Продолжить" — ничего в самом черновике не трогаем (тот же
      // draftId, те же ответы, тот же шаг), просто снимаем вопрос.
      state.briefEntryPending = false;
      render();
    });
    const newBtn = document.getElementById("brief-entry-new");
    if (newBtn) newBtn.addEventListener("click", () => {
      clearBriefDraft();
      resetBriefState({ hardReset: true }); // сама снимает briefEntryPending
      render();
    });
    return;
  }

  const backBtn = document.getElementById("back");
  if (backBtn) backBtn.addEventListener("click", goBack);

  document.querySelectorAll("[data-service-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.servicePick;
      if (id === "unknown") {
        // Нет услуги — конфигурировать нечего, сразу к вопросу о задаче.
        // calc/orderOptions/urgent/complex явно сбрасываем: этот путь идёт
        // напрямую в briefNext(), минуя order-next (где calc обычно
        // пересчитывается заново) — без сброса здесь конфигурация ранее
        // выбранной и брошенной услуги тихо уехала бы в payload (см.
        // production-аудит, P1-4).
        state.brief.serviceId = null;
        state.brief.serviceName = "Не определился с услугой";
        state.brief.calc = null;
        state.brief.orderOptions = {};
        state.brief.urgent = false;
        state.brief.complex = false;
        briefNext();
      } else {
        // Услуга выбрана — остаёмся на шаге 1, он сразу покажет её
        // конфигурацию (опции/цену), не перепрыгиваем вперёд.
        const s = state.pricing.services.find((x) => x.id === id);
        state.brief.serviceId = s.id;
        state.brief.serviceName = s.name;
        state.brief.orderOptions = {};
        state.brief.urgent = false;
        state.brief.complex = false;
        render();
      }
    })
  );

  document.querySelectorAll("[data-group-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      state.brief.openGroupId = el.dataset.groupPick;
      render();
    })
  );

  const briefBackToCategories = document.getElementById("brief-back-to-categories");
  if (briefBackToCategories) briefBackToCategories.addEventListener("click", (e) => {
    e.preventDefault();
    state.brief.openGroupId = null;
    render();
  });

  // ---- Шаг 1 (Order): опции/коэффициенты/цена выбранной услуги — тот же
  // движок расчёта, что и в calculator.js, но состояние своё (brief.order*),
  // чтобы не путать с отдельным экраном /calculator.
  document.querySelectorAll("[data-order-option-toggle]").forEach((el) =>
    el.addEventListener("change", () => {
      const id = el.dataset.orderOptionToggle;
      if (el.checked) state.brief.orderOptions[id] = 1;
      else delete state.brief.orderOptions[id];
      render();
    })
  );
  document.querySelectorAll("[data-order-qty-plus]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.orderQtyPlus;
      state.brief.orderOptions[id] = (state.brief.orderOptions[id] || 1) + 1;
      render();
    })
  );
  document.querySelectorAll("[data-order-qty-minus]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.orderQtyMinus;
      const cur = state.brief.orderOptions[id] || 1;
      if (cur <= 1) delete state.brief.orderOptions[id];
      else state.brief.orderOptions[id] = cur - 1;
      render();
    })
  );
  const orderUrgentToggle = document.getElementById("order-urgent-toggle");
  if (orderUrgentToggle) orderUrgentToggle.addEventListener("change", () => {
    state.brief.urgent = orderUrgentToggle.checked;
    render();
  });
  const orderComplexToggle = document.getElementById("order-complex-toggle");
  if (orderComplexToggle) orderComplexToggle.addEventListener("change", () => {
    state.brief.complex = orderComplexToggle.checked;
    render();
  });
  const orderChangeService = document.getElementById("order-change-service");
  if (orderChangeService) orderChangeService.addEventListener("click", (e) => {
    e.preventDefault();
    // Тот же сброс, что и в ветке "unknown" выше, и по той же причине:
    // "изменить услугу" возвращает на экран выбора без прохода через
    // order-next, где calc обычно пересчитывается (см. P1-4).
    state.brief.serviceId = null;
    state.brief.serviceName = null;
    state.brief.calc = null;
    state.brief.orderOptions = {};
    state.brief.urgent = false;
    state.brief.complex = false;
    render();
  });
  const orderNext = document.getElementById("order-next");
  if (orderNext) orderNext.addEventListener("click", () => {
    // Снимок для payload — тот же формат, что раньше собирала кнопка
    // "Отправить с этим расчётом" на экране /calculator (см. lead.py —
    // формат заявки для дизайнера не менялся).
    state.brief.calc = {
      service_id: state.brief.serviceId,
      options: Object.entries(state.brief.orderOptions).map(([id, qty]) => ({ id, qty })),
      urgent: state.brief.urgent,
      complex: state.brief.complex,
    };
    briefNext();
  });

  const taskInput = document.getElementById("task-input");
  if (taskInput) {
    taskInput.addEventListener("input", () => {
      state.brief.task = taskInput.value.slice(0, TASK_MAXLEN);
      const counter = document.getElementById("task-counter");
      counter.textContent = `${state.brief.task.length}/${TASK_MAXLEN}`;
      counter.classList.toggle("over", state.brief.task.length > TASK_MAXLEN);
      document.getElementById("brief-next").disabled = state.brief.task.trim().length < 10;
      persistBriefDraftDebounced();
    });
  }

  document.querySelectorAll("[data-have-toggle]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.haveToggle;
      if (id === "none") {
        state.brief.have = el.classList.contains("selected") ? [] : ["none"];
      } else {
        state.brief.have = state.brief.have.filter((h) => h !== "none");
        if (state.brief.have.includes(id)) state.brief.have = state.brief.have.filter((h) => h !== id);
        else state.brief.have.push(id);
      }
      render();
    })
  );

  document.querySelectorAll("[data-deadline-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      state.brief.deadline = el.dataset.deadlinePick;
      briefNext();
    })
  );

  document.querySelectorAll("[data-budget-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      state.brief.budget = el.dataset.budgetPick;
      briefNext();
    })
  );

  const nameInput = document.getElementById("contact-name");
  const contactInput = document.getElementById("contact-value");
  if (nameInput) nameInput.addEventListener("input", () => {
    state.brief.name = nameInput.value;
    validateNameContactStep();
    persistBriefDraftDebounced();
  });
  if (contactInput) contactInput.addEventListener("input", () => {
    state.brief.contactValue = contactInput.value;
    validateNameContactStep();
    persistBriefDraftDebounced();
  });
  document.querySelectorAll("[data-tz-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      state.brief.tzMode = el.dataset.tzPick;
      render();
    })
  );

  const tzFieldMap = { "tz-goal": "goal", "tz-must-have": "mustHave", "tz-avoid": "avoid", "tz-references": "references" };
  for (const [elId, key] of Object.entries(tzFieldMap)) {
    const el = document.getElementById(elId);
    if (el) el.addEventListener("input", () => {
      state.brief.tzDetails[key] = el.value;
      validateContactStep();
      persistBriefDraftDebounced();
    });
  }

  const nextBtn = document.getElementById("brief-next");
  if (nextBtn) nextBtn.addEventListener("click", briefNext);
  const prevBtn = document.getElementById("brief-prev");
  if (prevBtn) prevBtn.addEventListener("click", briefPrev);
  const submitBtn = document.getElementById("brief-submit");
  if (submitBtn) submitBtn.addEventListener("click", submitBrief);
}

function isNameContactValid(b) {
  return b.name.trim().length > 0 && b.contactValue.trim().length > 0;
}

function validateNameContactStep() {
  const btn = document.getElementById("brief-next");
  if (!btn) return;
  btn.disabled = !isNameContactValid(state.brief);
}

function isContactStepValid() {
  const b = state.brief;
  const basics = isNameContactValid(b) && b.tzMode !== null;
  if (!basics) return false;
  if (b.tzMode === "form") return b.tzDetails.goal.trim().length > 0;
  return true;
}

function validateContactStep() {
  const btn = document.getElementById("brief-submit");
  if (!btn) return;
  btn.disabled = !isContactStepValid();
}

// Бюджет, подсказанный уже выполненным расчётом в калькуляторе — чтобы не
// переспрашивать абстрактный диапазон, когда точная цена уже известна.
// state.brief.calc хранит серверный payload-формат (service_id/options/...),
// поэтому пересчитываем через тот же calculatePrice(), что и сам калькулятор.
function inferBudgetFromCalc(calc) {
  if (!calc || !calc.service_id || !state.pricing) return null;
  const selected = {};
  for (const o of calc.options || []) selected[o.id] = o.qty;
  const result = calculatePrice(state.pricing, calc.service_id, selected, calc.urgent, calc.complex);
  if (!result) return null;
  const price = result.priceFrom;
  if (price < 20000) return "lt20";
  if (price < 40000) return "20-40";
  if (price < 70000) return "40-70";
  if (price < 100000) return "70-100";
  return "gt100";
}

function briefNext() {
  if (state.brief.step < BRIEF_TOTAL_STEPS) state.brief.step += 1;
  render();
}
function briefPrev() {
  state.brief.step = Math.max(1, state.brief.step - 1);
  render();
}

async function submitBrief() {
  // Ранний выход, если отправка уже в процессе — сама по себе кнопка НЕ
  // защищает от повторного вызова (см. комментарий у state.submitting),
  // поэтому проверка нужна и здесь, на уровне функции.
  if (state.submitting) return;

  const payload = {
    action: "submit_brief",
    mode: "new",
    service_id: state.brief.serviceId,
    service_name: state.brief.serviceName,
    task_description: state.brief.task.trim(),
    have: state.brief.have,
    deadline: state.brief.deadline,
    budget: state.brief.budget,
    contact: `${state.brief.name.trim()} — ${state.brief.contactValue.trim()}`,
    attach_tz: state.brief.tzMode === "file",
    tz_details: state.brief.tzMode === "form" ? {
      goal: state.brief.tzDetails.goal.trim(),
      must_have: state.brief.tzDetails.mustHave.trim(),
      avoid: state.brief.tzDetails.avoid.trim(),
      references: state.brief.tzDetails.references.trim(),
    } : null,
    calc: state.brief.calc,
    source: state.brief.source || "direct",
    source_case_id: state.brief.sourceCaseId || null,
    source_case_title: state.brief.sourceCaseTitle || null,
    // draft_id — тот же на протяжении одного заказа (включая "Дополнить
    // информацию" после отправки); бэкенд обновляет существующую заявку по
    // этому id вместо создания дубликата — см. content_store.add_lead().
    draft_id: state.brief.draftId,
  };

  // Telegram.WebApp.sendData() официально работает только для Mini App,
  // запущенного через KeyboardButton.web_app
  // (https://core.telegram.org/bots/webapps: "This method is only
  // available for Mini Apps launched via a Keyboard button") — мы ушли от
  // этой кнопки ради initData (см. bot/keyboards.py), значит sendData()
  // для submit_brief больше не рабочий путь. POST /api/leads использует ту
  // же проверенную identity, что уже работает для "Мои заявки".
  state.submitting = true;
  state.briefSubmitError = null;
  render();

  try {
    const res = await fetch("/api/leads", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": TG.initData() },
      body: JSON.stringify(payload),
    });
    if (res.status === 409) {
      // Batch 2 — draft_id из localStorage совпал с уже закрытой
      // (DONE/CANCELLED) заявкой (см. content_store.add_lead, упсерт по
      // draft_id) — черновик НЕ трогаем (та же ветка catch ниже его бы
      // тоже не тронула), просто отдельное, понятное сообщение вместо
      // generic "проверьте соединение", которое было бы неверным здесь.
      state.submitting = false;
      state.briefSubmitError = "Эта заявка уже закрыта. Начните новую заявку.";
      render();
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const result = await res.json();
    // Черновик чистим только после подтверждённого успеха с сервера — в
    // отличие от sendData() (без ответа вообще), у HTTP есть реальное
    // подтверждение, что заявка сохранена. Порядок важен: navigate() сам
    // вызывает render(), а render() всегда persistBriefDraft() первым
    // делом — если чистить localStorage ДО navigate(), этот render()
    // тут же перезапишет его тем же (ещё не сброшенным) state.brief,
    // молча отменяя очистку. Поэтому сначала даём render() отработать,
    // потом чистим.
    //
    // state.brief сбрасываем ПОСЛЕ этого — отправленная заявка живёт
    // только в "Мои заявки" и НЕ должна оставаться "текущим драфтом":
    // раньше state.brief нарочно не трогали ("нужен для Дополнить
    // информацию в рамках сессии"), но supplement-режим (см. openSupplementFor)
    // больше не читает state.brief вообще — он адресуется по lead_id через
    // отдельный state.supplement. Без этого сброса повторный заход на таб
    // "Заказать" в той же сессии WebView показывал шаг 7 только что
    // отправленной заявки вместо чистого нового черновика (см. аудит).
    // renderSubmitted() читает только lastPayload/lastLeadResult, не
    // state.brief — сброс здесь никак не меняет уже показанный экран
    // "Готово", поэтому повторный render() не нужен.
    state.submitting = false;
    state.lastPayload = payload;
    state.lastLeadResult = result; // { lead_id, created, attach_tz, price_range }
    state.history = [];
    navigate("submitted", { pushHistory: false });
    clearBriefDraft();
    resetBriefState({ hardReset: true });
  } catch (e) {
    // Черновик НЕ теряем при ошибке — пользователь остаётся на том же шаге
    // с уже заполненными полями и может просто попробовать ещё раз.
    state.submitting = false;
    state.briefSubmitError = "Не получилось отправить заявку. Проверьте соединение и попробуйте ещё раз.";
    render();
  }
}

function renderProgress(step) {
  let dots = "";
  for (let i = 1; i <= BRIEF_TOTAL_STEPS; i++) {
    dots += `<div class="${i <= step ? "done" : ""}"></div>`;
  }
  return `<div class="step-progress">${dots}</div>`;
}

// Отдельный экран-развилка перед Order Builder — показывается только если
// init() восстановил реально начатый черновик (см. state.briefEntryPending)
// и клиент зашёл сюда обычной вкладкой "Заказать" (а не осознанным CTA,
// который сам уже сбросил флаг через resetBriefState()). Без этого экрана
// у клиента, не желающего продолжать старую заявку, не было понятного
// способа начать новую — единственный "В начало" жил только на экране
// "Готово", которого к этому моменту уже нет (см. UX-аудит).
function renderBriefEntryChoice() {
  return `
    <div class="topbar"><h1>✍️ Заказать</h1></div>
    <div class="case-block">
      <p>У вас есть незавершённая заявка. Продолжить её или начать новую?</p>
    </div>
    <div class="btn-row">
      <button class="btn btn-primary" id="brief-entry-continue">Продолжить</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-secondary" id="brief-entry-new">Начать новую заявку</button>
    </div>
  `;
}

function renderBrief() {
  if (state.briefEntryPending) {
    return renderBriefEntryChoice();
  }

  const b = state.brief;
  const step = b.step;

  let body = "";

  if (step === 1 && b.serviceId) {
    // Order Builder: конфигурация услуги (опции/срочность/сложность/цена)
    // прямо на шаге 1 — раньше это был отдельный экран /calculator, теперь
    // часть заявки, с предзаполнением из кейса/калькулятора, если пришли оттуда.
    const service = state.pricing.services.find((s) => s.id === b.serviceId);
    const options = calcServiceOptions(state.pricing, service.id);
    const result = calculatePrice(state.pricing, service.id, b.orderOptions, b.urgent, b.complex);
    const optionRows = options
      .map((o) => {
        const checked = Boolean(b.orderOptions[o.id]);
        const qty = b.orderOptions[o.id] || 1;
        const qtyControl = o.multipliable && checked
          ? `<div class="qty-control">
               <button type="button" data-order-qty-minus="${o.id}">−</button>
               <span>${qty}</span>
               <button type="button" data-order-qty-plus="${o.id}">+</button>
             </div>`
          : "";
        return `
          <div class="option-row">
            <label class="option-main">
              <input type="checkbox" data-order-option-toggle="${o.id}" ${checked ? "checked" : ""} />
              <div>
                <div class="option-name">${escapeHtml(o.name)}</div>
                <div class="option-price">+${formatMoney(o.price)}, +${formatDays(o.days)} дн.${o.multipliable ? " · можно несколько" : ""}</div>
              </div>
            </label>
            ${qtyControl}
          </div>`;
      })
      .join("");
    body = `
      <h2>Собираем проект</h2>
      <div class="summary-box">Услуга: <b>${escapeHtml(service.name)}</b> · <a href="#" id="order-change-service">изменить</a></div>
      ${service.includes ? `<p class="hint">В базовую стоимость входит: ${escapeHtml(service.includes)}</p>` : ""}
      ${optionRows}
      <div class="toggle-row">
        <span>Срочный проект (+25%)</span>
        <label class="switch"><input type="checkbox" id="order-urgent-toggle" ${b.urgent ? "checked" : ""} /><span class="track"></span></label>
      </div>
      <div class="toggle-row">
        <span>Высокая сложность (+20%)</span>
        <label class="switch"><input type="checkbox" id="order-complex-toggle" ${b.complex ? "checked" : ""} /><span class="track"></span></label>
      </div>
      <div class="result-box">
        <div class="price">${formatMoney(result.priceFrom)} – ${formatMoney(result.priceTo)}</div>
        <div class="term">${formatDays(result.termFrom)}–${formatDays(result.termTo)} рабочих дней</div>
        <div class="hint">Предварительная оценка стоимости по вашим параметрам</div>
      </div>
      <button class="btn btn-primary" id="order-next">Далее</button>
    `;
    return wrapBrief(body, step);
  } else if (step === 1 && b.openGroupId) {
    const group = state.pricing.groups.find((g) => g.id === b.openGroupId);
    const items = group.service_ids
      .map((id) => state.pricing.services.find((s) => s.id === id))
      .map((s) => `<button class="pick" data-service-pick="${s.id}">${escapeHtml(s.short_name || s.name)}</button>`)
      .join("");
    body = `
      <p class="section-lead"><a href="#" id="brief-back-to-categories">← Все варианты</a></p>
      <h2>${escapeHtml(group.label)}</h2>
      <div class="option-buttons">${items}</div>
    `;
  } else if (step === 1) {
    const items = getMenuEntries(state.pricing)
      .map((entry) =>
        entry.kind === "service"
          ? `<button class="pick" data-service-pick="${entry.service.id}">${escapeHtml(entry.service.name)}</button>`
          : `<button class="pick" data-group-pick="${entry.group.id}">${escapeHtml(entry.group.label)}</button>`
      )
      .join("");
    body = `
      <h2>Что нужно?</h2>
      <div class="option-buttons">${items}<button class="pick" data-service-pick="unknown">Не знаю, что нужно</button></div>
    `;
  } else if (step === 2) {
    body = `
      <h2>Расскажите о задаче</h2>
      <p class="hint">Пара предложений, ~300–500 символов</p>
      <div class="field">
        <textarea id="task-input" rows="6" maxlength="${TASK_MAXLEN}" placeholder="Например: нужен лендинг для запуска нового продукта, аудитория — молодые родители...">${escapeHtml(b.task)}</textarea>
        <div class="char-counter" id="task-counter">${b.task.length}/${TASK_MAXLEN}</div>
      </div>
      <div class="btn-row">
        <button class="btn btn-secondary" id="brief-prev">Назад</button>
        <button class="btn btn-primary" id="brief-next" ${b.task.trim().length < 10 ? "disabled" : ""}>Далее</button>
      </div>
    `;
    return wrapBrief(body, step);
  } else if (step === 3) {
    const items = HAVE_OPTIONS.map(
      (o) => `<button class="pick ${b.have.includes(o.id) ? "selected" : ""}" data-have-toggle="${o.id}">${o.label}${b.have.includes(o.id) ? " ✓" : ""}</button>`
    ).join("");
    body = `
      <h2>Что уже есть?</h2>
      <p class="hint">Можно выбрать несколько</p>
      <div class="option-buttons">${items}</div>
      <div class="btn-row">
        <button class="btn btn-secondary" id="brief-prev">Назад</button>
        <button class="btn btn-primary" id="brief-next" ${b.have.length === 0 ? "disabled" : ""}>Далее</button>
      </div>
    `;
    return wrapBrief(body, step);
  } else if (step === 4) {
    const items = DEADLINE_OPTIONS.map((o) => `<button class="pick" data-deadline-pick="${o.id}">${o.label}</button>`).join("");
    body = `<h2>Когда нужно?</h2><div class="option-buttons">${items}</div>`;
  } else if (step === 5) {
    const inferredBudget = inferBudgetFromCalc(b.calc);
    // Нейтральная формулировка — раньше "похоже на X, можно подтвердить"
    // звучало как навязанный ответ; бюджет и предварительная оценка (шаг 1)
    // это разные вещи (см. аудит: ожидание клиента vs расчёт по scope), а
    // не одно и то же под двумя именами.
    const hint = inferredBudget
      ? `<p class="hint">Предварительная оценка попадает в диапазон «${BUDGET_OPTIONS.find((o) => o.id === inferredBudget).label}» — совпадает с вашими ожиданиями?</p>`
      : "";
    const items = BUDGET_OPTIONS.map(
      (o) => `<button class="pick ${o.id === (b.budget || inferredBudget) ? "selected" : ""}" data-budget-pick="${o.id}">${o.label}</button>`
    ).join("");
    body = `
      <h2>Какой бюджет вы планируете на проект?</h2>
      <p class="hint">Это ваш финансовый ориентир — он может отличаться от предварительной оценки выше. Дизайнер учтёт оба значения.</p>
      ${hint}
      <div class="option-buttons">${items}</div>
    `;
  } else if (step === 6) {
    body = `
      <h2>Контакты</h2>
      <div class="field">
        <label>Имя</label>
        <input type="text" id="contact-name" value="${escapeHtml(b.name)}" placeholder="Как к вам обращаться" />
      </div>
      <div class="field">
        <label>Telegram или телефон</label>
        <input type="text" id="contact-value" value="${escapeHtml(b.contactValue)}" placeholder="@username или +7..." />
      </div>
      <div class="btn-row">
        <button class="btn btn-secondary" id="brief-prev">Назад</button>
        <button class="btn btn-primary" id="brief-next" ${isNameContactValid(b) ? "" : "disabled"}>Далее</button>
      </div>
    `;
    return wrapBrief(body, step);
  } else if (step === 7) {
    body = `
      <h2>Есть подробное ТЗ?</h2>
      <div class="option-buttons">
        <button class="pick ${b.tzMode === "form" ? "selected" : ""}" data-tz-pick="form">Да, опишу здесь</button>
        <button class="pick ${b.tzMode === "file" ? "selected" : ""}" data-tz-pick="file">Да, пришлю файл</button>
        <button class="pick ${b.tzMode === "none" ? "selected" : ""}" data-tz-pick="none">Нет</button>
      </div>
      ${b.tzMode === "form" ? `
      <div class="field">
        <label>Какую задачу должен решить результат?</label>
        <input type="text" id="tz-goal" value="${escapeHtml(b.tzDetails.goal)}" placeholder="Например: увеличить заявки с сайта" />
      </div>
      <div class="field">
        <label>Что обязательно должно быть? (необязательно)</label>
        <input type="text" id="tz-must-have" value="${escapeHtml(b.tzDetails.mustHave)}" placeholder="Например: форма заявки в первом экране" />
      </div>
      <div class="field">
        <label>Чего избегать? (необязательно)</label>
        <input type="text" id="tz-avoid" value="${escapeHtml(b.tzDetails.avoid)}" placeholder="Например: строгие тёмные тона" />
      </div>
      <div class="field">
        <label>Ссылки на референсы (необязательно)</label>
        <input type="text" id="tz-references" value="${escapeHtml(b.tzDetails.references)}" placeholder="Ссылки на примеры, которые нравятся" />
      </div>
      ` : ""}
      ${state.briefSubmitError ? `<p class="error-text">${escapeHtml(state.briefSubmitError)}</p>` : ""}
      <div class="btn-row">
        <button class="btn btn-secondary" id="brief-prev" ${state.submitting ? "disabled" : ""}>Назад</button>
        <button class="btn btn-primary" id="brief-submit" ${(state.submitting || !isContactStepValid()) ? "disabled" : ""}>${state.submitting ? "Отправляю…" : "Отправить заявку"}</button>
      </div>
    `;
    return wrapBrief(body, step);
  }

  return wrapBrief(body, step);
}

function wrapBrief(body, step) {
  // Шаг 1 сам показывает услугу в своей summary-box (с "изменить") — общий
  // чип здесь был бы дублем прямо над ним.
  const serviceChip = state.brief.serviceName && step !== 1
    ? `<div class="summary-box">Услуга: <b>${escapeHtml(state.brief.serviceName)}</b></div>`
    : "";
  const nav = step > 1 && step !== 2 && step !== 3 && step !== 6 && step !== 7
    ? `<div class="btn-row"><button class="btn btn-secondary" id="brief-prev">Назад</button></div>`
    : "";
  // Тот же принцип, что и у about/calculator — brief не всегда достижим с
  // историей (см. render()'s "brief" case).
  const backBtnHTML = state.history.length > 0 ? '<button class="back-btn" id="back">←</button>' : "";
  return `
    <div class="topbar">
      ${backBtnHTML}
      <h1>✍️ Заказать</h1>
    </div>
    ${renderProgress(step)}
    ${serviceChip}
    ${body}
    ${nav}
  `;
}

// ---- Экран: Мои заявки ----
// Клиент видит ТОЛЬКО свои заявки. user_id никогда не передаётся с клиента
// напрямую — сервер проверяет подпись Telegram initData и сам достаёт
// authenticated user_id оттуда (см. bot/telegram_auth.py, /api/my-leads).
// Вне настоящего Telegram (обычный браузер) initData пусто — это ожидаемо,
// а не ошибка: сервер вернёт 401, экран должен показать понятное
// объяснение, а не выглядеть сломанным.
// Должно буква в букву совпадать с bot/lead.py::CLIENT_STATUS_LABELS —
// иначе текст Telegram-уведомления о смене статуса разойдётся с тем, что
// клиент увидит здесь же, открыв Mini App (см. аудит про статусы).
const MY_LEAD_STATUS_LABELS = {
  NEW: "🆕 Заявка получена",
  VIEWED: "👀 На рассмотрении",
  IN_PROGRESS: "💬 В работе",
  WAITING_CLIENT: "⏸ Нужно ваше действие",
  DONE: "✅ Завершено",
  CANCELLED: "❌ Отменено",
};

// Общая для списка и детали — гарантирует одинаковую раскраску статуса
// в обоих местах структурно, а не просто "по договорённости" (см. UX-аудит
// про статусы). Текст WAITING_CLIENT уже говорит "нужно ваше действие" —
// отдельный secondary hint с тем же смыслом был бы дублированием, поэтому
// его нет: сам лейбл статуса уже несёт нужную информацию.
function myLeadStatusClass(status) {
  if (status === "WAITING_CLIENT") return "status-warning";
  if (status === "DONE") return "status-success";
  if (status === "CANCELLED") return "status-muted";
  return "";
}

// Batch 2 — DONE/CANCELLED терминальны для клиента (hard-block, см.
// bot/content_store.py::TERMINAL_LEAD_STATUSES, тот же список статусов).
// Используется только для UI-подсказки здесь; backend — единственный
// реальный источник правды, блокирует запись независимо от этого.
function isLeadClosed(status) {
  return status === "DONE" || status === "CANCELLED";
}

// created_at всегда есть; updated_at показываем отдельной строкой только
// если день реально отличается от дня создания — иначе "Обновлено" в тот
// же день, что и "Создана", не несёт клиенту новой информации.
function myLeadDateLines(lead) {
  const createdDate = (lead.created_at || "").slice(0, 10);
  const updatedDate = (lead.updated_at || "").slice(0, 10);
  const updatedDiffers = updatedDate && updatedDate !== createdDate;
  return { createdDate, updatedDate, updatedDiffers };
}

async function fetchMyLeads() {
  const initData = TG.initData();
  state.myLeads.status = "loading";
  render();
  try {
    // Раньше при пустом initData запрос вообще не уходил на сервер — это
    // означало отсутствие каких-либо логов, когда initData пуст у реального
    // клиента в реальном Telegram (см. диагностику initData). Теперь запрос
    // уходит всегда, с диагностическими заголовками (не влияют на решение
    // сервера пустить/не пустить — только на то, что попадает в лог).
    const res = await fetch("/api/my-leads", {
      headers: {
        "X-Telegram-Init-Data": initData,
        "X-Debug-Platform": realTG?.platform || "",
        "X-Debug-Version": realTG?.version || "",
        "X-Debug-Has-Hash": String(!!window.location.hash),
        // Только булево наличие подстроки — не сам hash и не его
        // содержимое, чтобы отличить "Telegram не передал tgWebAppData"
        // от "передал, но SDK/наш код прочитал его неправильно".
        "X-Debug-Hash-Has-TgWebAppData": String(window.location.hash.includes("tgWebAppData")),
        // Проверка гипотезы про HMAC mismatch: Telegram.WebApp.initData —
        // это decodeURIComponent()-нутая строка (см. реальный SDK), может
        // содержать не-ASCII (например, кириллицу в first_name), а Fetch
        // API молча обрезает такие символы в значении заголовка до младшего
        // байта (WHATWG "isomorphic encode") — если это происходит здесь,
        // именно это и портит байты до проверки HMAC на сервере. Сам
        // initData не логируем — только результат теста на ASCII.
        "X-Debug-InitData-Ascii-Only": String(/^[\x00-\x7F]*$/.test(initData)),
      },
    });
    if (res.status === 401) {
      state.myLeads.status = initData ? "error" : "no-telegram";
      render();
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.myLeads.items = await res.json();
    state.myLeads.status = "loaded";
  } catch (e) {
    state.myLeads.status = "error";
  }
  render();
}

function renderMyLeads() {
  const m = state.myLeads;
  const header = `<div class="topbar"><h1>📋 Мои заявки</h1></div>`;

  if (m.selected) return header.replace("📋 Мои заявки", "📋 Заявка") + renderMyLeadDetail(m.selected);

  if (m.status === "idle") {
    // Первый заход на экран в этой сессии — запускаем загрузку и сразу
    // показываем лоадер, сам fetch довызовет render() по завершении.
    setTimeout(fetchMyLeads, 0);
    return header + `<div class="empty-state">Загрузка…</div>`;
  }
  if (m.status === "loading") {
    return header + `<div class="empty-state">Загрузка…</div>`;
  }
  if (m.status === "no-telegram") {
    return header + `<div class="empty-state">Раздел «Мои заявки» доступен только внутри Telegram — здесь бот не может подтвердить, кто вы.</div>`;
  }
  if (m.status === "error") {
    return header + `<div class="empty-state">Не получилось загрузить заявки. Попробуйте открыть раздел ещё раз.</div>`;
  }
  if (!m.items.length) {
    return header + `<div class="empty-state">Заявок пока нет — оформите заявку на вкладке «Заказать».</div>`;
  }
  const cards = m.items.map((lead) => {
    const service = lead.payload?.service_name || "Без услуги";
    const price = lead.calc_summary
      ? `${formatMoney(lead.calc_summary.price_from)} – ${formatMoney(lead.calc_summary.price_to)}`
      : "";
    const status = MY_LEAD_STATUS_LABELS[lead.status] || lead.status;
    const { createdDate, updatedDate, updatedDiffers } = myLeadDateLines(lead);
    const dateLabel = updatedDiffers ? `Обновлено ${updatedDate}` : createdDate;
    return `
      <button class="lead-card" data-lead-id="${lead.id}">
        <div class="lead-card-top"><span>№${lead.id}</span><span>${escapeHtml(dateLabel)}</span></div>
        <div class="lead-card-service">${escapeHtml(service)}</div>
        ${price ? `<div class="hint">${escapeHtml(price)}</div>` : ""}
        <div class="lead-card-status ${myLeadStatusClass(lead.status)}">${escapeHtml(status)}</div>
      </button>`;
  }).join("");
  return header + `<div class="lead-card-list">${cards}</div>`;
}

// Те же ключи, что bot/lead.py::SUPPLEMENT_FIELD_LABELS — только заполненные
// поля показываем, пустые пропускаем (см. supplement-форма, где часть полей
// необязательна).
const MY_LEAD_SUPPLEMENT_FIELD_LABELS = {
  comment: "Комментарий",
  additional_requirements: "Доп. требования",
  references: "Референсы",
  contact: "Контакты",
};

function renderMyLeadDetail(lead) {
  const p = lead.payload || {};
  const status = MY_LEAD_STATUS_LABELS[lead.status] || lead.status;
  const { createdDate, updatedDate, updatedDiffers } = myLeadDateLines(lead);
  const priceLine = lead.calc_summary
    ? `<div class="result-box"><div class="price">${formatMoney(lead.calc_summary.price_from)} – ${formatMoney(lead.calc_summary.price_to)}</div><div class="hint">Точная сумма — предварительная</div></div>`
    : "";
  const optionsLine = lead.calc_summary && lead.calc_summary.selected_options && lead.calc_summary.selected_options.length
    ? `<div class="case-block"><div class="label">Опции</div><p>${lead.calc_summary.selected_options.map((o) => escapeHtml(o.name) + (o.qty > 1 ? ` ×${o.qty}` : "")).join(", ")}</p></div>`
    : "";

  // owner_messages — append-only ответы дизайнера (bot/handlers/admin.py::
  // lead_reply_send), отдельный поток, никак не связанный с payload/
  // supplements/materials. Блок вообще не рендерится, если ответов нет —
  // не показываем пустую секцию заявкам, которым дизайнер ещё не отвечал.
  const ownerMessages = lead.owner_messages || [];
  let ownerCommentBlock = "";
  if (ownerMessages.length) {
    const last = ownerMessages[ownerMessages.length - 1];
    const earlier = ownerMessages.slice(0, -1);
    const historyToggle = earlier.length
      ? `<button class="btn btn-secondary" id="my-lead-owner-history-toggle">${state.myLeads.ownerHistoryExpanded ? "Скрыть историю" : `Показать историю (${ownerMessages.length})`}</button>`
      : "";
    const historyList = state.myLeads.ownerHistoryExpanded && earlier.length
      ? `<div class="case-block">${earlier.slice().reverse().map((m) => `<p><span class="hint">${escapeHtml((m.sent_at || "").slice(0, 10))}</span> — ${escapeHtml(m.text)}</p>`).join("")}</div>`
      : "";
    ownerCommentBlock = `
      <div class="case-block">
        <div class="label">Комментарий дизайнера</div>
        <p><span class="hint">${escapeHtml((last.sent_at || "").slice(0, 10))}</span> — ${escapeHtml(last.text)}</p>
      </div>
      ${historyToggle}
      ${historyList}
    `;
  }

  // supplements[] — append-only дополнения САМОГО клиента (bot/webserver.py::
  // _handle_lead_supplement), не путать с owner_messages (ответы дизайнера) —
  // отдельная секция, показываем только заполненные поля каждого дополнения.
  const supplements = lead.supplements || [];
  const supplementsBlock = supplements.length
    ? `<div class="case-block"><div class="label">Дополнения (${supplements.length})</div>${supplements.map((s) => {
        const fields = s.fields || {};
        const fieldLines = Object.entries(MY_LEAD_SUPPLEMENT_FIELD_LABELS)
          .filter(([key]) => fields[key])
          .map(([key, label]) => `<p><b>${escapeHtml(label)}:</b> ${escapeHtml(fields[key])}</p>`)
          .join("");
        return `<div style="margin-top:8px"><span class="hint">${escapeHtml((s.created_at || "").slice(0, 10))}</span>${fieldLines}</div>`;
      }).join("")}</div>`
    : "";

  // materials[] — присланные боту файлы/фото (bot/handlers/webapp.py::
  // handle_tz_file). Пока только факт получения (тип + дата) — без preview/
  // download, без привязки к конкретному облачному хранилищу (см. аудит).
  const materials = lead.materials || [];
  const materialsBlock = materials.length
    ? `<div class="case-block"><div class="label">Материалы (${materials.length})</div>${materials.map((m) => {
        const kindLabel = m.kind === "photo" ? "Фото" : "Файл";
        const date = (m.received_at || "").slice(0, 10);
        return `<p>${escapeHtml(kindLabel)} · <span class="hint">${escapeHtml(date)}</span></p>`;
      }).join("")}</div>`
    : "";

  return `
    <button class="btn btn-secondary" id="my-lead-back">← К списку заявок</button>
    <div class="case-block">
      <div class="label">Заявка №${lead.id}</div>
      <p class="${myLeadStatusClass(lead.status)}">${escapeHtml(status)}</p>
      ${updatedDiffers ? `<p class="hint">Обновлено ${escapeHtml(updatedDate)}</p>` : ""}
      <p class="hint">Создана ${escapeHtml(createdDate)}</p>
    </div>
    <div class="case-block"><div class="label">Услуга</div><p>${escapeHtml(p.service_name || "—")}</p></div>
    ${optionsLine}
    ${priceLine}
    ${p.task_description ? `<div class="case-block"><div class="label">Задача</div><p>${escapeHtml(p.task_description)}</p></div>` : ""}
    ${p.budget ? `<div class="case-block"><div class="label">Бюджет</div><p>${escapeHtml(BUDGET_OPTIONS.find((b) => b.id === p.budget)?.label || p.budget)}</p></div>` : ""}
    ${p.contact ? `<div class="case-block"><div class="label">Контакты</div><p>${escapeHtml(p.contact)}</p></div>` : ""}
    ${ownerCommentBlock}
    ${supplementsBlock}
    ${materialsBlock}
    ${isLeadClosed(lead.status)
      ? `<p class="hint">Заявка закрыта — дополнить её нельзя. Если нужно что-то уточнить, напишите нам в чат.</p>`
      : `<button class="btn btn-primary" id="my-lead-continue">Дополнить информацию</button>`}
    <button class="btn btn-secondary" id="my-lead-start-new">Начать новую заявку</button>
  `;
}

// Список <-> деталь внутри экрана "myleads" — отдельное под-состояние
// (state.myLeads.selected), никогда не проходящее через navigate()/
// state.history (тот же экран "myleads" всё это время) — поэтому
// закрытие детали НЕ переиспользует goBack() (он бы вместо возврата к
// списку заявок ушёл на screen, который был до входа в "Мои заявки"
// вообще, минуя список). Вынесено в отдельную функцию, чтобы у кнопки
// "← К списку заявок" и нативного Telegram BackButton было ровно одно,
// общее определение "закрыть деталь" — см. render()/attachMyLeadsEvents().
function closeMyLeadDetail() {
  state.myLeads.selected = null;
  render();
}

function attachMyLeadsEvents() {
  document.querySelectorAll("[data-lead-id]").forEach((el) =>
    el.addEventListener("click", () => {
      const lead = state.myLeads.items.find((l) => l.id === Number(el.dataset.leadId));
      state.myLeads.selected = lead;
      state.myLeads.ownerHistoryExpanded = false; // новая заявка — сворачиваем историю прошлой
      render();
    })
  );
  const backBtn = document.getElementById("my-lead-back");
  if (backBtn) backBtn.addEventListener("click", closeMyLeadDetail);

  const ownerHistoryToggle = document.getElementById("my-lead-owner-history-toggle");
  if (ownerHistoryToggle) ownerHistoryToggle.addEventListener("click", () => {
    state.myLeads.ownerHistoryExpanded = !state.myLeads.ownerHistoryExpanded;
    render();
  });

  const continueBtn = document.getElementById("my-lead-continue");
  if (continueBtn) continueBtn.addEventListener("click", () => {
    openSupplementFor(state.myLeads.selected);
  });

  const startNewBtn = document.getElementById("my-lead-start-new");
  if (startNewBtn) startNewBtn.addEventListener("click", () => {
    // Осознанный уход в обычный Order Builder "с нуля" — та же семантика,
    // что и "В начало"/supplement-screen "Начать новую заявку": НЕ
    // supplement, НЕ трогает текущий lead, свежий draftId через уже
    // существующий resetBriefState({hardReset:true}).
    state.myLeads.selected = null;
    state.myLeads.ownerHistoryExpanded = false;
    state.history = [];
    navigate("brief", { pushHistory: false, resetBrief: true, hardReset: true });
  });
}

// ---- Экран: Дополнение к существующей заявке ----
// Отдельный режим, намеренно НЕ переиспользующий 7-шаговый Order Builder —
// раньше "Дополнить информацию" перестраивало state.brief из уже
// сохранённого lead.payload и отправляло клиента заново проходить весь
// бриф с draftId старой заявки, из-за чего POST /api/leads (mode="new")
// полностью перезаписывал payload заявки без какой-либо истории изменений
// (см. аудит). Теперь дополнение — это отдельный append-only supplement на
// lead["supplements"] (bot/content_store.py::add_lead_supplement),
// адресуемый строго по lead_id, а не по draft_id.
function openSupplementFor(lead) {
  // Сохраняем незавершённый draft при повторном открытии ДЛЯ ТОЙ ЖЕ заявки
  // (E2E MVP audit, Batch 4) — раньше эта функция безусловно обнуляла
  // state.supplement на каждый вызов, из-за чего уход на другой таб и
  // возврат сюда стирали уже введённый текст. Для ДРУГОЙ заявки (leadId не
  // совпадает) или уже отправленного draft (sent) поведение не меняется —
  // ниже по-прежнему инициализируется с чистого листа.
  if (state.supplement && state.supplement.leadId === lead.id && !state.supplement.sent) {
    navigate("supplement");
    return;
  }
  const p = (lead && lead.payload) || {};
  state.supplement = {
    leadId: lead.id,
    comment: "",
    additionalRequirements: "",
    references: "",
    contact: p.contact || "",
    wantsFile: false,
    submitting: false,
    error: null,
    sent: false,
    supplementId: null,
  };
  navigate("supplement");
}

function renderSupplement() {
  const s = state.supplement;
  if (!s) return `<div class="empty-state">Заявка не выбрана.</div>`;

  if (s.sent) {
    // Тот же паттерн, что и renderSubmitted()'s tzLine — тот случай уже
    // корректно объясняет клиенту, что файл нужно отправить отдельным
    // сообщением в чат с ботом, а не через Mini App; здесь тот же
    // actionable-текст, только вводная фраза под формулировку чекбокса
    // самого supplement-экрана ("Пришлю файл следующим сообщением", а не
    // "файл ТЗ" из брифа).
    const wantsFileLine = s.wantsFile
      ? `<p class="hint">Вы отметили, что пришлёте файл — отправьте его следующим сообщением прямо в этот чат с ботом (не через Mini App).</p>`
      : "";
    return `
      <div class="topbar"><h1>Дополнение отправлено ✅</h1></div>
      <div class="case-block"><div class="label">Заявка №${s.leadId}</div><p>Дизайнер увидит дополнение в чате.</p></div>
      ${wantsFileLine}
      <div class="btn-row">
        <button class="btn btn-primary" id="supplement-back-to-lead">Вернуться к заявке</button>
      </div>
    `;
  }

  return `
    <div class="topbar">
      <button class="back-btn" id="back">←</button>
      <h1>Дополнение к заявке №${s.leadId}</h1>
    </div>
    <div class="field">
      <label>Что хотите добавить/изменить?</label>
      <textarea id="supp-comment" rows="4" placeholder="Опишите, что нужно уточнить или добавить">${escapeHtml(s.comment)}</textarea>
    </div>
    <div class="field">
      <label>Дополнительные требования (необязательно)</label>
      <textarea id="supp-additional" rows="3">${escapeHtml(s.additionalRequirements)}</textarea>
    </div>
    <div class="field">
      <label>Референсы (необязательно)</label>
      <textarea id="supp-references" rows="3">${escapeHtml(s.references)}</textarea>
    </div>
    <div class="field">
      <label>Контакты</label>
      <input type="text" id="supp-contact" value="${escapeHtml(s.contact)}" placeholder="@username или +7..." />
    </div>
    <div class="field">
      <button type="button" class="pick ${s.wantsFile ? "selected" : ""}" id="supp-wants-file">
        📎 ${s.wantsFile ? "Пришлю файл следующим сообщением ✓" : "Хочу приложить файл"}
      </button>
    </div>
    ${s.error ? `<p class="error-text">${escapeHtml(s.error)}</p>` : ""}
    <div class="btn-row">
      <button class="btn btn-primary" id="supplement-submit" ${s.submitting ? "disabled" : ""}>${s.submitting ? "Отправляю…" : "Отправить дополнение"}</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-secondary" id="supplement-start-new">Начать новую заявку</button>
    </div>
  `;
}

// Дополнение отправлено (state.supplement.sent) — единственная видимая
// in-app навигация здесь #supplement-back-to-lead, НЕ topbar "←"/goBack()
// (тот сознательно убран из этой ветки renderSupplement()). Вынесено в
// отдельную функцию по тому же принципу, что и closeMyLeadDetail (Batch 13):
// у in-app кнопки и нативного Telegram BackButton должно быть ровно одно,
// общее определение "вернуться к заявке после отправки" — см. render().
function closeSupplementAfterSubmit() {
  // status: "idle" — перезапросить список, чтобы карточка заявки
  // отражала только что отправленное дополнение.
  state.myLeads.status = "idle";
  state.myLeads.selected = null;
  state.history = [];
  navigate("myleads", { pushHistory: false });
}

function attachSupplementEvents() {
  const s = state.supplement;
  if (!s) return;

  if (s.sent) {
    const backToLeadBtn = document.getElementById("supplement-back-to-lead");
    if (backToLeadBtn) backToLeadBtn.addEventListener("click", closeSupplementAfterSubmit);
    return;
  }

  const backBtn = document.getElementById("back");
  if (backBtn) backBtn.addEventListener("click", goBack);

  const commentEl = document.getElementById("supp-comment");
  if (commentEl) commentEl.addEventListener("input", () => { state.supplement.comment = commentEl.value; });
  const additionalEl = document.getElementById("supp-additional");
  if (additionalEl) additionalEl.addEventListener("input", () => { state.supplement.additionalRequirements = additionalEl.value; });
  const referencesEl = document.getElementById("supp-references");
  if (referencesEl) referencesEl.addEventListener("input", () => { state.supplement.references = referencesEl.value; });
  const contactEl = document.getElementById("supp-contact");
  if (contactEl) contactEl.addEventListener("input", () => { state.supplement.contact = contactEl.value; });

  const wantsFileBtn = document.getElementById("supp-wants-file");
  if (wantsFileBtn) wantsFileBtn.addEventListener("click", () => {
    state.supplement.wantsFile = !state.supplement.wantsFile;
    render();
  });

  const startNewBtn = document.getElementById("supplement-start-new");
  if (startNewBtn) startNewBtn.addEventListener("click", () => {
    // Явный уход в обычный Order Builder "с нуля" — сюда клиента приводит
    // осознанный выбор поменять состав заказа (услугу/срок/бюджет), что
    // supplement-форма намеренно не поддерживает (см. аудит: "Если
    // клиенту нужно изменить состав/параметры заказа — это новая заявка").
    state.history = [];
    navigate("brief", { pushHistory: false, resetBrief: true, hardReset: true });
  });

  const submitBtn = document.getElementById("supplement-submit");
  if (submitBtn) submitBtn.addEventListener("click", submitSupplement);
}

async function submitSupplement() {
  const s = state.supplement;
  if (!s || s.submitting) return;

  s.submitting = true;
  s.error = null;
  render();

  const fields = {
    comment: s.comment.trim(),
    additional_requirements: s.additionalRequirements.trim(),
    references: s.references.trim(),
    contact: s.contact.trim(),
  };

  try {
    const res = await fetch("/api/leads", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": TG.initData() },
      body: JSON.stringify({ mode: "supplement", lead_id: s.leadId, fields, wants_file: s.wantsFile }),
    });
    if (res.status === 409) {
      // Batch 2 — заявку закрыли (DONE/CANCELLED), пока клиент заполнял
      // экран; отдельное, содержательное сообщение вместо generic "не
      // получилось отправить" ниже — тот случай про сеть/сервер, этот
      // про реальное состояние заявки, которое клиенту стоит понимать.
      state.supplement.submitting = false;
      state.supplement.error = "Эта заявка уже закрыта — дополнить её нельзя.";
      render();
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const result = await res.json();
    state.supplement.submitting = false;
    state.supplement.sent = true;
    state.supplement.supplementId = result.supplement_id;
    render();
  } catch (e) {
    state.supplement.submitting = false;
    state.supplement.error = "Не получилось отправить дополнение. Проверьте соединение и попробуйте ещё раз.";
    render();
  }
}

// ---- Экран: подтверждение после успешного POST /api/leads ----
// Раньше этот экран видел только dev-фоллбэк вне Telegram (sendData() сама
// закрывала Mini App у настоящего клиента, подтверждение шло отдельным
// сообщением в чате) — теперь HTTP-ответ не закрывает Mini App сама, так
// что этот экран стал боевым: показывает номер заявки и сумму расчёта
// прямо здесь, плюс переход в "Мои заявки" (см. п.8 требований — успех
// подтверждается внутри Mini App, уведомление владельцу в чат остаётся
// отдельно, см. bot/webserver.py::handle_create_lead).
function renderSubmitted() {
  const p = state.lastPayload || {};
  const result = state.lastLeadResult || {};
  const priceLine = result.price_range
    ? `<div class="result-box"><div class="price">${formatMoney(result.price_range.from)} – ${formatMoney(result.price_range.to)}</div><div class="hint">Точная сумма — предварительная, дизайнер подтвердит в чате</div></div>`
    : "";
  const tzLine = result.attach_tz
    ? `<p class="hint">Вы отметили, что пришлёте файл ТЗ — отправьте его следующим сообщением прямо в этот чат с ботом (не через Mini App).</p>`
    : "";
  return `
    <div class="topbar"><h1>Готово ✅</h1></div>
    <div class="case-block"><div class="label">Заявка №${result.lead_id ?? "—"}</div><p>Услуга: ${escapeHtml(p.service_name || "не указана")}</p></div>
    ${priceLine}
    ${tzLine}
    <p>Я свяжусь с вами в ближайшее время.</p>
    <div class="btn-row">
      <button class="btn btn-secondary" id="add-more-info">Дополнить информацию</button>
      <button class="btn btn-primary" id="to-my-leads">Мои заявки</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-secondary" id="to-start">В начало</button>
    </div>
  `;
}

document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "to-start") {
    // Осознанный уход "в начало" — вот здесь черновик действительно можно
    // очистить: пользователь явно закончил с этим заказом.
    clearBriefDraft();
    state.history = [];
    navigate("portfolio", { pushHistory: false, resetBrief: true, hardReset: true });
  }
  if (e.target && e.target.id === "add-more-info") {
    // Раньше вело обратно в 7-шаговый Order Builder на том же (последнем)
    // шаге — теперь единый supplement-режим, тот же что из "Мои заявки"
    // (см. openSupplementFor/аудит).
    const result = state.lastLeadResult || {};
    if (result.lead_id) {
      openSupplementFor({ id: result.lead_id, payload: state.lastPayload || {} });
    }
  }
  if (e.target && e.target.id === "to-my-leads") {
    // status сбрасываем в "idle", чтобы fetchMyLeads() перезапросил список —
    // иначе только что созданной заявки не было бы видно до следующего
    // захода (state.myLeads уже мог быть "loaded" с прошлого раза).
    state.myLeads.status = "idle";
    state.history = [];
    navigate("myleads", { pushHistory: false });
  }
});

init();
