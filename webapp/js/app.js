/* Mini App дизайнера-фрилансера: портфолио, обо мне, калькулятор, бриф.
 * Работает и внутри Telegram (через telegram-web-app.js), и в обычном
 * браузере для превью/тестирования — во втором случае отправка заявки
 * не уходит в бота, а показывается на экране как JSON (см. TG.sendData).
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
  themeParams() { return realTG?.themeParams || {}; },
  colorScheme() { return realTG?.colorScheme || "light"; },
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
  screen: "loading",
  history: [],
  filter: "all",
  currentCase: null,
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
  },
  lastPayload: null,
};

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

function navigate(screen, { resetBrief = false, pushHistory = true } = {}) {
  if (pushHistory && state.screen !== "loading") state.history.push(state.screen);
  if (resetBrief) resetBriefState();
  state.screen = screen;
  render();
}

function goBack() {
  const prev = state.history.pop();
  state.screen = prev || "portfolio";
  render();
}

function resetBriefState() {
  state.brief = {
    step: state.brief.serviceId ? 2 : 1,
    openGroupId: null,
    serviceId: state.brief.serviceId,
    serviceName: state.brief.serviceName,
    task: "",
    have: [],
    deadline: null,
    budget: null,
    name: "",
    contactValue: "",
    tzMode: null,
    tzDetails: { goal: "", mustHave: "", avoid: "", references: "" },
    calc: state.brief.calc,
  };
}

// ---- Загрузка данных и старт ----
async function init() {
  TG.ready();
  TG.expand();
  applyTheme();
  TG.onThemeChanged(applyTheme);

  const [pricing, portfolio, about] = await Promise.all([
    fetch("/data/pricing.json").then((r) => r.json()),
    fetch("/data/portfolio.json").then((r) => r.json()),
    fetch("/data/about.json").then((r) => r.json()),
  ]);
  state.pricing = pricing;
  state.portfolio = portfolio;
  state.about = about;

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
    : path.endsWith("/portfolio") ? "portfolio"
    : params.get("screen");
  if (initialScreen === "calculator") state.screen = "calculator";
  else if (initialScreen === "brief") state.screen = "brief";
  else if (initialScreen === "about") state.screen = "about";
  else state.screen = "portfolio";

  render();
}

// ---- Нижнее меню (переключение экранов внутри уже открытого Mini App) ----
// Кнопки в чате Telegram открывают/поднимают уже открытый WebView, но НЕ
// перезагружают его при повторном нажатии — поэтому переключаться между
// портфолио/калькулятором/заявкой нужно средствами самого приложения.
const TAB_SCREENS = [
  { id: "portfolio", icon: "📁", label: "Портфолио" },
  { id: "about", icon: "👤", label: "Обо мне" },
  { id: "calculator", icon: "💰", label: "Калькулятор" },
  { id: "brief", icon: "✍️", label: "Заявка" },
];

function renderTabBar() {
  const active = state.screen === "case" ? "portfolio" : state.screen;
  const items = TAB_SCREENS.map(
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
      state.history = [];
      if (screen === "brief") resetBriefState();
      state.screen = screen;
      render();
    })
  );
}

// ---- Рендер ----
function render() {
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
      TG.backButton.show(goBack);
      break;
    case "calculator":
      content = renderCalculator();
      TG.backButton.show(goBack);
      break;
    case "brief":
      content = renderBrief();
      TG.backButton.show(goBack);
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
        <img src="/${c.cover}" alt="" loading="lazy" />
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
function renderCase() {
  const c = state.currentCase;
  const images = c.images.map((src) => `<img src="/${src}" alt="" />`).join("");
  return `
    <div class="topbar">
      <button class="back-btn" id="back">←</button>
      <h1>${escapeHtml(c.title)}</h1>
    </div>
    <div class="case-images">${images}</div>
    <div class="case-block"><div class="label">Задача</div><p>${escapeHtml(c.task)}</p></div>
    <div class="case-block"><div class="label">Решение</div><p>${escapeHtml(c.solution)}</p></div>
    <div class="case-block"><div class="label">Результат</div><p>${escapeHtml(c.result)}</p></div>
    <button class="btn btn-primary" id="want-similar">Хочу похожий проект</button>
  `;
}

function attachCaseEvents() {
  document.getElementById("back").addEventListener("click", goBack);
  document.getElementById("want-similar").addEventListener("click", () => {
    const service = state.pricing.services.find((s) => s.id === state.currentCase.related_service);
    state.brief.serviceId = service?.id || null;
    state.brief.serviceName = service?.name || null;
    state.brief.calc = null;
    navigate("brief", { resetBrief: true });
  });
}

// ---- Экран: Обо мне ----
function renderAbout() {
  const a = state.about;

  const specItems = a.specialization.map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  const toolChips = a.tools.map((t) => `<span class="chip-static">${escapeHtml(t)}</span>`).join("");

  const featured = state.portfolio.cases.filter((c) => c.featured).slice(0, 3);
  const featuredHTML = featured
    .map(
      (c) => `
      <button class="mini-case" data-case="${c.id}">
        <img src="/${c.cover}" alt="" loading="lazy" />
        <span>${escapeHtml(c.title)}</span>
      </button>`
    )
    .join("");

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
      <button class="back-btn" id="back">←</button>
      <h1>👤 Обо мне</h1>
    </div>

    <div class="about-header">
      <img class="about-avatar" src="/${a.avatar}" alt="" />
      <div class="about-name">${escapeHtml(a.name)}</div>
      <div class="hint">${escapeHtml(a.tagline)}</div>
    </div>

    <div class="about-block">
      <h2>Специализация</h2>
      <ul class="plain-list">${specItems}</ul>
    </div>

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

    ${featuredHTML ? `<div class="about-block"><h2>Избранные кейсы</h2><div class="mini-case-grid">${featuredHTML}</div></div>` : ""}

    ${educationHTML}
    ${linksHTML}

    <button class="btn btn-primary" id="about-cta">Оставить заявку</button>
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
  if (cta) cta.addEventListener("click", () => navigate("brief", { resetBrief: true }));
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
    state.brief.calc = {
      service_id: state.calc.serviceId,
      options: Object.entries(state.calc.options).map(([id, qty]) => ({ id, qty })),
      urgent: state.calc.urgent,
      complex: state.calc.complex,
    };
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

  if (!state.calc.serviceId && state.calc.openGroupId) {
    const group = pricing.groups.find((g) => g.id === state.calc.openGroupId);
    const items = group.service_ids
      .map((id) => pricing.services.find((s) => s.id === id))
      .map((s) => serviceItemHTML(s, s.short_name || s.name))
      .join("");
    return `
      <div class="topbar">
        <button class="back-btn" id="back">←</button>
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
        <button class="back-btn" id="back">←</button>
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
      <button class="back-btn" id="back">←</button>
      <h1>💰 Калькулятор</h1>
    </div>
    <div class="summary-box">
      Услуга: <b>${escapeHtml(service.name)}</b>
      · <a href="#" id="change-service">изменить</a>
    </div>

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
const BRIEF_TOTAL_STEPS = 6;

function attachBriefEvents() {
  const backBtn = document.getElementById("back");
  if (backBtn) backBtn.addEventListener("click", goBack);

  document.querySelectorAll("[data-service-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = el.dataset.servicePick;
      if (id === "unknown") {
        state.brief.serviceId = null;
        state.brief.serviceName = "Не определился с услугой";
      } else {
        const s = state.pricing.services.find((x) => x.id === id);
        state.brief.serviceId = s.id;
        state.brief.serviceName = s.name;
      }
      briefNext();
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

  const taskInput = document.getElementById("task-input");
  if (taskInput) {
    taskInput.addEventListener("input", () => {
      state.brief.task = taskInput.value.slice(0, TASK_MAXLEN);
      const counter = document.getElementById("task-counter");
      counter.textContent = `${state.brief.task.length}/${TASK_MAXLEN}`;
      counter.classList.toggle("over", state.brief.task.length > TASK_MAXLEN);
      document.getElementById("brief-next").disabled = state.brief.task.trim().length < 10;
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
    validateContactStep();
  });
  if (contactInput) contactInput.addEventListener("input", () => {
    state.brief.contactValue = contactInput.value;
    validateContactStep();
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
    });
  }

  const nextBtn = document.getElementById("brief-next");
  if (nextBtn) nextBtn.addEventListener("click", briefNext);
  const prevBtn = document.getElementById("brief-prev");
  if (prevBtn) prevBtn.addEventListener("click", briefPrev);
  const submitBtn = document.getElementById("brief-submit");
  if (submitBtn) submitBtn.addEventListener("click", submitBrief);
}

function isContactStepValid() {
  const b = state.brief;
  const basics = b.name.trim().length > 0 && b.contactValue.trim().length > 0 && b.tzMode !== null;
  if (!basics) return false;
  if (b.tzMode === "form") return b.tzDetails.goal.trim().length > 0;
  return true;
}

function validateContactStep() {
  const btn = document.getElementById("brief-submit");
  if (!btn) return;
  btn.disabled = !isContactStepValid();
}

function briefNext() {
  if (state.brief.step < BRIEF_TOTAL_STEPS) state.brief.step += 1;
  render();
}
function briefPrev() {
  state.brief.step = Math.max(1, state.brief.step - 1);
  render();
}

function submitBrief() {
  const payload = {
    action: "submit_brief",
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
  };
  TG.sendData(payload);
}

function renderProgress(step) {
  let dots = "";
  for (let i = 1; i <= BRIEF_TOTAL_STEPS; i++) {
    dots += `<div class="${i <= step ? "done" : ""}"></div>`;
  }
  return `<div class="step-progress">${dots}</div>`;
}

function renderBrief() {
  const b = state.brief;
  const step = b.step;

  let body = "";

  if (step === 1 && b.openGroupId) {
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
    const items = BUDGET_OPTIONS.map((o) => `<button class="pick" data-budget-pick="${o.id}">${o.label}</button>`).join("");
    body = `<h2>Бюджет</h2><div class="option-buttons">${items}</div>`;
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
      <div class="field">
        <label>Есть подробное ТЗ?</label>
        <div class="option-buttons">
          <button class="pick ${b.tzMode === "form" ? "selected" : ""}" data-tz-pick="form">Да, опишу здесь</button>
          <button class="pick ${b.tzMode === "file" ? "selected" : ""}" data-tz-pick="file">Да, пришлю файл</button>
          <button class="pick ${b.tzMode === "none" ? "selected" : ""}" data-tz-pick="none">Нет</button>
        </div>
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
      <div class="btn-row">
        <button class="btn btn-secondary" id="brief-prev">Назад</button>
        <button class="btn btn-primary" id="brief-submit" ${isContactStepValid() ? "" : "disabled"}>Отправить заявку</button>
      </div>
    `;
    return wrapBrief(body, step);
  }

  return wrapBrief(body, step);
}

function wrapBrief(body, step) {
  const serviceChip = state.brief.serviceName
    ? `<div class="summary-box">Услуга: <b>${escapeHtml(state.brief.serviceName)}</b></div>`
    : "";
  const nav = step > 1 && step !== 2 && step !== 3 && step !== 6
    ? `<div class="btn-row"><button class="btn btn-secondary" id="brief-prev">Назад</button></div>`
    : "";
  return `
    <div class="topbar">
      <button class="back-btn" id="back">←</button>
      <h1>✍️ Заявка</h1>
    </div>
    ${renderProgress(step)}
    ${serviceChip}
    ${body}
    ${nav}
  `;
}

// ---- Экран: подтверждение (только вне Telegram, для тестирования в браузере) ----
function renderSubmitted() {
  return `
    <div class="topbar"><h1>Готово ✅</h1></div>
    <p>Заявка отправлена (режим предпросмотра в браузере — реальная отправка в бота работает только внутри Telegram).</p>
    <p class="hint">Данные, которые ушли бы боту:</p>
    <pre class="summary-box" style="white-space:pre-wrap;word-break:break-word;">${escapeHtml(JSON.stringify(state.lastPayload, null, 2))}</pre>
    <button class="btn btn-primary" id="to-start">В начало</button>
  `;
}

document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "to-start") {
    state.history = [];
    navigate("portfolio", { pushHistory: false, resetBrief: true });
  }
});

init();
