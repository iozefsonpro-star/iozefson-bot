/* Паола App — фронтенд без сборки: fetch + vanilla JS. Редизайн «Небо». */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let OVERVIEW = { projects: [], chats: [], modes: {} };
let CURRENT_CHAT = null;
let CHIPS_CTX = { type: "standalone" };   // или {type:"project", id}
let WEEK_OFFSET = 0;
let CAL_DAYS = 0;
let ANALYTICS_MODE = "review";   // review — итоги недели; plan — план следующей
let TASKS_CACHE = null;

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (resp.status === 401) { showLogin(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    let detail = "";
    try { detail = (await resp.json()).detail || ""; } catch (_) {}
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

/* ---------------- мини-markdown для ответов Паолы ---------------- */

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMd(text) {
  let t = esc(text);
  t = t.replace(/```([\s\S]*?)```/g, (_, c) => `<code>${c.trim()}</code>`);
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|\s)\*([^*\n]+)\*(?=\s|[.,;:!?)]|$)/g, "$1<i>$2</i>");
  t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/(https?:\/\/[^\s<)]+)/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>');
  t = t.replace(/^[-•]\s+/gm, "• ");
  return t;
}

/* ---------------- вход / выход ---------------- */

function showLogin() {
  $("#login-screen").classList.remove("hidden");
  $("#app").classList.add("hidden");
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  const now = new Date();
  $("#home-meta").textContent = now.toLocaleDateString("ru-RU",
    { weekday: "long", day: "numeric", month: "long" });
  const h = now.getHours();
  const word = h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро"
             : h < 18 ? "Добрый день" : "Добрый вечер";
  $("#home-greeting").innerHTML = `${word},<br>Юлия`;
  loadCalendar(0);
  loadTasks();
  loadHabits();
  loadReminders();
  loadOverview();
  loadAnalytics();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").classList.add("hidden");
  try {
    await api("/api/login", { method: "POST",
      body: JSON.stringify({ password: $("#login-password").value }) });
    $("#login-password").value = "";
    showApp();
  } catch (_) {
    $("#login-error").classList.remove("hidden");
  }
});

$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  showLogin();
});

/* ---------------- навигация по вкладкам ---------------- */

function switchView(name) {
  ["home", "chat", "projects", "analytics"].forEach((v) => {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
  });
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  if (name === "chat" && !CURRENT_CHAT) openDefaultChat();
  if (name === "analytics") loadAnalyticsView();
  window.scrollTo(0, 0);
}

$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

/* ---------------- календарь ---------------- */

function fmtDay(iso) {
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "long" });
}

async function loadCalendar(days) {
  CAL_DAYS = days;
  const box = $("#calendar-body");
  box.textContent = "Загружаю…";
  try {
    const data = await api(`/api/calendar?days=${days}`);
    box.innerHTML = "";
    if (!data.configured) {
      box.textContent = "Календарь не подключён: заполни GOOGLE_TOKEN_JSON в переменных сервиса.";
      return;
    }
    const longEvents = data.long || [];
    if (!data.days.length && !longEvents.length) {
      box.textContent = days === 0 && data.hidden_past_today > 0
        ? "Встречи на сегодня закончились — остаток дня можно отдать фокусу."
        : days > 0
        ? "На неделе событий нет — окно для глубокой работы."
        : "Сегодня встреч нет — день можно отдать фокусу.";
      return;
    }
    if (!data.days.length) {
      const e = document.createElement("div");
      e.className = "cal-empty";
      e.textContent = days === 0 && data.hidden_past_today > 0
        ? "Встречи на сегодня закончились — остаток дня можно отдать фокусу."
        : days > 0
        ? "На неделе встреч нет — окно для глубокой работы."
        : "Встреч сегодня нет.";
      box.append(e);
    }
    for (const day of data.days) {
      if (days > 0) {
        const h = document.createElement("div");
        h.className = "cal-day";
        h.textContent = fmtDay(day.date);
        box.append(h);
      }
      for (const ev of day.events) {
        const line = document.createElement("div");
        line.className = "cal-event";
        const time = document.createElement("span");
        time.className = "cal-time";
        time.textContent = `${ev.emoji} ${ev.time}`;
        const title = document.createElement("span");
        title.textContent = ev.title;
        line.append(time, title);
        box.append(line);
      }
    }
    // Долгие события (курсы, периоды) — как «В процессе» у бота: после встреч.
    if (longEvents.length) {
      const h = document.createElement("div");
      h.className = "cal-day";
      h.textContent = "В процессе";
      box.append(h);
      for (const ev of longEvents) {
        const line = document.createElement("div");
        line.className = "cal-event";
        const mark = document.createElement("span");
        mark.className = "cal-time";
        mark.textContent = "📚";
        const title = document.createElement("span");
        title.textContent = ev.label || ev.title;
        line.append(mark, title);
        box.append(line);
      }
    }
  } catch (err) {
    box.textContent = "Не удалось загрузить календарь: " + err.message;
  }
}

$$("[data-cal-days]").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("[data-cal-days]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadCalendar(parseInt(btn.dataset.calDays, 10));
  });
});

$("#cal-refresh").addEventListener("click", async () => {
  const btn = $("#cal-refresh");
  btn.classList.add("spinning");
  btn.disabled = true;
  await loadCalendar(CAL_DAYS);
  btn.classList.remove("spinning");
  btn.disabled = false;
});

/* ---------------- задачи + карточка-инициатива ---------------- */

function fmtTaskDate(iso) {
  // "YYYY-MM-DD…" → "DD/MM" (Юлия предпочитает день/месяц)
  const p = iso.slice(0, 10).split("-");
  return p.length === 3 ? `${p[2]}/${p[1]}` : iso;
}

function taskLi(t) {
  const li = document.createElement("li");
  li.className = "task";

  const check = document.createElement("input");
  check.type = "checkbox";
  check.className = "t-check";
  check.setAttribute("aria-label", "Отметить выполненной: " + t.title);
  check.addEventListener("change", () => completeTask(t.id, li, check));

  const title = document.createElement("span");
  title.className = "t-title";
  title.textContent = t.title;

  li.append(check, title);

  const meta = document.createElement("span");
  meta.className = "t-meta";
  // Дата — сама и есть control переноса: тап открывает перенос дедлайна.
  // Без даты показываем плашку-приглашение «+ дата» с тем же действием.
  const dateBtn = document.createElement("button");
  dateBtn.type = "button";
  dateBtn.className = "t-date" + (t.deadline ? "" : " t-date-empty");
  dateBtn.textContent = t.deadline ? fmtTaskDate(t.deadline) : "+ дата";
  dateBtn.setAttribute("aria-label", "Перенести дедлайн: " + t.title);
  dateBtn.addEventListener("click", () => openRescheduleDialog(t));
  meta.append(dateBtn);
  if (t.zone) {
    const tag = document.createElement("span");
    tag.className = "t-tag";
    tag.textContent = t.zone;   // с эмодзи зоны
    meta.append(tag);
  }
  li.append(meta);

  return li;
}

async function completeTask(taskId, li, check) {
  check.disabled = true;
  try {
    await api(`/api/tasks/${taskId}/complete`, { method: "POST" });
    li.classList.add("t-done");
    setTimeout(() => loadTasks(), 220);   // короткая анимация — потом обновляем список
  } catch (err) {
    check.checked = false;
    check.disabled = false;
    alert("Не удалось закрыть задачу: " + err.message);
  }
}

let RESCHED_TASK = null;

function openRescheduleDialog(t) {
  RESCHED_TASK = t;
  $("#dlg-resched-title").textContent = t.title;
  $("#dlg-resched-date").value = (t.deadline || "").slice(0, 10)
    || new Date().toISOString().slice(0, 10);
  $("#dlg-resched").showModal();
}

$("#dlg-resched-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  if (!RESCHED_TASK) return;
  const newDate = $("#dlg-resched-date").value;
  if (!newDate) return;
  try {
    await api(`/api/tasks/${RESCHED_TASK.id}/reschedule`, {
      method: "POST", body: JSON.stringify({ new_date: newDate }),
    });
    await loadTasks();
  } catch (err) {
    alert("Не удалось перенести: " + err.message);
  }
});

// limit: показать первые N задач + кнопку «показать все» (0 = без ограничения)
function taskGroup(label, tasks, cls, limit = 0) {
  if (!tasks.length) return null;
  const div = document.createElement("div");
  div.className = "task-group" + (cls ? " " + cls : "");
  const h = document.createElement("h3");
  h.textContent = `${label} · ${tasks.length}`;
  const ul = document.createElement("ul");
  ul.className = "task-list";
  const collapsed = limit > 0 && tasks.length > limit;
  const shown = collapsed ? tasks.slice(0, limit) : tasks;
  shown.forEach((t) => ul.append(taskLi(t)));
  div.append(h, ul);
  if (collapsed) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "show-more";
    more.textContent = `показать все (${tasks.length})`;
    more.addEventListener("click", () => {
      tasks.slice(limit).forEach((t) => ul.append(taskLi(t)));
      more.remove();
    });
    div.append(more);
  }
  return div;
}

function renderNudge() {
  const card = $("#nudge-card");
  if (!TASKS_CACHE) { card.classList.add("hidden"); return; }
  const today = new Date().toISOString().slice(0, 10);
  if (localStorage.getItem("paola_nudge_dismissed") === today) {
    card.classList.add("hidden");
    return;
  }
  const n = TASKS_CACHE.overdue.length;
  const txt = $("#nudge-text");
  if (n > 0) {
    txt.innerHTML = `У тебя <b>${n} просроченных задач</b>. Разберём их за 10 минут — предложу по каждой: закрыть, перенести или отпустить.`;
    $("#nudge-go").textContent = "Давай разберём";
  } else if (TASKS_CACHE.today.length > 0) {
    txt.innerHTML = `Просроченного нет. На сегодня <b>${TASKS_CACHE.today.length} задач</b> — помочь расставить порядок?`;
    $("#nudge-go").textContent = "Помоги с планом";
  } else {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
}

$("#nudge-later").addEventListener("click", () => {
  localStorage.setItem("paola_nudge_dismissed", new Date().toISOString().slice(0, 10));
  $("#nudge-card").classList.add("hidden");
});

$("#nudge-go").addEventListener("click", async () => {
  const overdue = TASKS_CACHE ? TASKS_CACHE.overdue.length : 0;
  const msg = overdue > 0
    ? "Давай разберём мои просроченные задачи: покажи их и по каждой предложи — закрыть, перенести (на какую дату) или отпустить. Пойдём по одной."
    : "Помоги расставить приоритеты в задачах на сегодня: что делать первым и почему.";
  switchView("chat");
  await openDefaultChat();
  await sendChatMessage(msg);
});

async function loadTasks() {
  const box = $("#tasks-body");
  box.textContent = "Загружаю…";
  try {
    const data = await api("/api/tasks");
    TASKS_CACHE = data;
    box.innerHTML = "";
    $("#tasks-meta").textContent =
      `${data.overdue.length + data.today.length + data.other.length} активных`;
    const groups = [
      taskGroup("Просрочено", data.overdue, "overdue"),
      taskGroup("Сегодня", data.today, ""),
      taskGroup("Остальные", data.other, "", 5),
    ].filter(Boolean);
    if (!groups.length) box.innerHTML = '<div class="soft-card">Активных задач нет — красота.</div>';
    else groups.forEach((g) => box.append(g));
    renderNudge();
  } catch (err) {
    box.textContent = "Ошибка загрузки задач: " + err.message;
    TASKS_CACHE = null;
  }
}

/* ---------------- привычки ---------------- */

async function loadHabits() {
  const empty = $("#habits-empty");
  empty.className = "empty hidden";
  try {
    const data = await api("/api/habits");
    $("#habits-date").textContent = data.date;
    const list = $("#habits-list");
    list.innerHTML = "";
    if (!data.habits.length) {
      empty.textContent = data.configured
        ? "Активных привычек нет — добавь строки в базу Notion «Привычки»."
        : "Трекер не настроен: заполни NOTION_HABITS_DB_ID и NOTION_HABIT_LOG_DB_ID.";
      empty.classList.remove("hidden");
      return;
    }
    for (const h of data.habits) {
      const li = document.createElement("li");
      li.className = "habit" + (h.done_today ? " done" : "");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = h.done_today;
      cb.setAttribute("aria-label", h.name);
      cb.addEventListener("change", async () => {
        cb.disabled = true;
        try {
          await api("/api/habits/log", { method: "POST",
            body: JSON.stringify({ habit_name: h.name, done: cb.checked }) });
          await loadHabits();
        } catch (err) {
          cb.checked = !cb.checked;
          cb.disabled = false;
          alert("Не удалось сохранить: " + err.message);
        }
      });
      const name = document.createElement("span");
      name.className = "h-name";
      name.textContent = h.name;
      const streak = document.createElement("span");
      streak.className = "h-streak";
      streak.textContent = h.streak > 0 ? `${h.streak} дн.` : "";
      li.append(cb, name, streak);
      list.append(li);
    }
  } catch (err) {
    empty.textContent = "Ошибка загрузки привычек: " + err.message;
    empty.className = "empty error";
  }
}

/* ---------------- напоминания ---------------- */

function fmtWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit",
                                     hour: "2-digit", minute: "2-digit" });
}

async function loadReminders() {
  const empty = $("#reminders-empty");
  empty.className = "empty hidden";
  try {
    const data = await api("/api/reminders");
    const list = $("#reminders-list");
    list.innerHTML = "";
    if (!data.reminders.length) {
      empty.textContent = data.configured
        ? "Ожидающих напоминаний нет."
        : "Напоминания не настроены: заполни NOTION_REMINDERS_DB_ID.";
      empty.classList.remove("hidden");
      return;
    }
    for (const r of data.reminders) {
      const li = document.createElement("li");
      li.className = "reminder";
      const when = document.createElement("span");
      when.className = "r-when";
      when.textContent = fmtWhen(r.when);
      const text = document.createElement("span");
      text.textContent = r.text;
      li.append(when, text);
      list.append(li);
    }
  } catch (err) {
    empty.textContent = "Ошибка: " + err.message;
    empty.className = "empty error";
  }
}

$("#reminder-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#reminder-text").value.trim();
  const when = $("#reminder-when").value;
  if (!text || !when) return;
  try {
    await api("/api/reminders", { method: "POST",
                                  body: JSON.stringify({ text, when }) });
    $("#reminder-text").value = "";
    $("#reminder-when").value = "";
    await loadReminders();
  } catch (err) {
    alert("Не удалось создать напоминание: " + err.message);
  }
});

/* ---------------- обзор чатов ---------------- */

async function loadOverview(selectChatId) {
  OVERVIEW = await api("/api/overview");
  renderChips();
  renderProjects();
  if (selectChatId) await openChat(selectChatId);
}

function chatsInContext() {
  if (CHIPS_CTX.type === "project") {
    const p = OVERVIEW.projects.find((x) => x.id === CHIPS_CTX.id);
    return p ? p.chats : [];
  }
  return OVERVIEW.chats;
}

function renderChips() {
  const box = $("#chat-chips");
  box.innerHTML = "";
  if (CHIPS_CTX.type === "project") {
    const p = OVERVIEW.projects.find((x) => x.id === CHIPS_CTX.id);
    const back = document.createElement("button");
    back.type = "button";
    back.className = "chip";
    back.textContent = "← чаты";
    back.addEventListener("click", () => {
      CHIPS_CTX = { type: "standalone" };
      renderChips();
    });
    box.append(back);
    if (p) {
      const label = document.createElement("button");
      label.type = "button";
      label.className = "chip active";
      label.textContent = `📁 ${p.name}`;
      box.append(label);
    }
  }
  for (const c of chatsInContext()) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (CURRENT_CHAT && CURRENT_CHAT.id === c.id ? " active" : "");
    const icon = (OVERVIEW.modes[c.mode] || "").split(" ")[0];
    chip.textContent = `${icon} ${c.title}`;
    chip.addEventListener("click", () => openChat(c.id));
    box.append(chip);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.className = "chip chip-add";
  add.textContent = "＋ новый";
  add.addEventListener("click", () =>
    openChatDialog(CHIPS_CTX.type === "project" ? CHIPS_CTX.id : null));
  box.append(add);
}

async function openDefaultChat() {
  if (CURRENT_CHAT) return;
  if (!OVERVIEW.chats.length && !OVERVIEW.projects.length) await loadOverview();
  const first = OVERVIEW.chats.find((c) => c.mode === "assistant") || OVERVIEW.chats[0];
  if (first) await openChat(first.id);
}

/* ---------------- чат ---------------- */

function addMsgRow(role, text, opts = {}) {
  const row = document.createElement("div");
  row.className = "msg-row " + role;
  if (role === "assistant") {
    const orb = document.createElement("span");
    orb.className = "orb orb-xs";
    orb.textContent = "П";
    row.append(orb);
  }
  const div = document.createElement("div");
  div.className = "msg " + (opts.error ? "error" : role);
  if (role === "assistant" && !opts.error) div.innerHTML = renderMd(text);
  else div.textContent = text;
  row.append(div);
  $("#chat-log").append(row);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return row;
}

function addTyping() {
  const row = document.createElement("div");
  row.className = "msg-row assistant";
  const orb = document.createElement("span");
  orb.className = "orb orb-xs";
  orb.textContent = "П";
  const t = document.createElement("div");
  t.className = "typing";
  t.innerHTML = "<i></i><i></i><i></i>";
  row.append(orb, t);
  $("#chat-log").append(row);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return row;
}

const MODE_HINTS = {
  translator: "Текст на итальянском/английском — исправлю и объясню правки. «Переведи…» — переведу. Или попроси помочь с письмом.",
  research:   "Назови тему — сделаю ресерч с источниками.",
  board:      "Опиши идею — соберу совет директоров.",
  business:   "Опиши бизнес — разберу модель по полочкам.",
  assistant:  "Спроси или поручи: задачи, календарь, привычки, напоминания…",
};

async function openChat(chatId, silent = false) {
  try {
    const chat = await api(`/api/chats/${chatId}`);
    CURRENT_CHAT = chat;
    localStorage.setItem("paola_chat", chat.id);
    if (chat.project_id) CHIPS_CTX = { type: "project", id: chat.project_id };
    $("#chat-status").textContent = chat.project_name
      ? `${(OVERVIEW.modes[chat.mode] || "").replace(/^[^\s]+\s/, "")} · «${chat.project_name}»`
      : "на связи";
    $("#chat-input").placeholder = MODE_HINTS[chat.mode] || "Напишите Паоле…";
    const log = $("#chat-log");
    log.innerHTML = "";
    if (!chat.messages.length) addMsgRow("assistant", MODE_HINTS[chat.mode] || "Слушаю!");
    chat.messages.forEach((m) =>
      addMsgRow(m.role === "user" ? "user" : "assistant", m.content));
    renderChips();
  } catch (err) {
    // Чат мог быть удалён (в т.ч. при редеплое без Volume) — молча сбрасываем
    // сохранённый id и не показываем ошибку на старте.
    if (silent || /не найден|404/i.test(err.message)) {
      CURRENT_CHAT = null;
      localStorage.removeItem("paola_chat");
      if (!silent) await openDefaultChat();
      return;
    }
    alert("Не удалось открыть чат: " + err.message);
  }
}

async function sendChatMessage(message) {
  if (!CURRENT_CHAT) return;
  addMsgRow("user", message);
  const typing = addTyping();
  $("#chat-status").textContent = "печатает…";
  $("#chat-send").disabled = true;
  try {
    const data = await api(`/api/chats/${CURRENT_CHAT.id}/messages`,
      { method: "POST", body: JSON.stringify({ message }) });
    typing.remove();
    addMsgRow("assistant", data.reply);
    loadOverview();
    loadTasks(); loadHabits(); loadReminders();
  } catch (err) {
    typing.remove();
    addMsgRow("assistant", "Ошибка: " + err.message, { error: true });
  } finally {
    $("#chat-status").textContent = "на связи";
    $("#chat-send").disabled = false;
  }
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  input.style.height = "";
  await sendChatMessage(message);
  input.focus();
});

$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#chat-input").addEventListener("input", (e) => {
  e.target.style.height = "";
  e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px";
});

/* ---------------- голосовой ввод (Web Speech API) ---------------- */

(function initVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = $("#mic-btn");
  if (!SR || !mic) return;            // браузер не поддерживает — микрофон остаётся скрытым
  mic.classList.remove("hidden");

  const rec = new SR();
  rec.lang = "ru-RU";
  rec.interimResults = true;
  rec.continuous = false;
  let listening = false;
  let baseText = "";

  mic.addEventListener("click", () => {
    if (listening) { rec.stop(); return; }
    baseText = $("#chat-input").value.trim();
    try { rec.start(); } catch (_) {}
  });

  rec.addEventListener("start", () => {
    listening = true;
    mic.classList.add("recording");
  });

  rec.addEventListener("result", (e) => {
    let text = "";
    for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
    const input = $("#chat-input");
    input.value = (baseText ? baseText + " " : "") + text.trim();
    input.dispatchEvent(new Event("input"));
  });

  const stop = () => { listening = false; mic.classList.remove("recording"); $("#chat-input").focus(); };
  rec.addEventListener("end", stop);
  rec.addEventListener("error", stop);
})();

$("#chat-delete").addEventListener("click", async () => {
  if (!CURRENT_CHAT) return;
  if (!confirm(`Удалить чат «${CURRENT_CHAT.title}» вместе с историей?`)) return;
  try {
    await api(`/api/chats/${CURRENT_CHAT.id}`, { method: "DELETE" });
    CURRENT_CHAT = null;
    localStorage.removeItem("paola_chat");
    await loadOverview();
    await openDefaultChat();
  } catch (err) {
    alert("Не удалось удалить: " + err.message);
  }
});

/* ---------------- проекты ---------------- */

function renderProjects() {
  const box = $("#projects-list");
  box.innerHTML = "";
  $("#projects-empty").classList.toggle("hidden", OVERVIEW.projects.length > 0);
  for (const p of OVERVIEW.projects) {
    const card = document.createElement("div");
    card.className = "project-card";
    const name = document.createElement("div");
    name.className = "project-name";
    name.textContent = `📁 ${p.name}`;
    card.append(name);
    if (p.description) {
      const d = document.createElement("p");
      d.className = "project-desc";
      d.textContent = p.description;
      card.append(d);
    }
    const chats = document.createElement("div");
    chats.className = "project-chats";
    for (const c of p.chats) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      const icon = (OVERVIEW.modes[c.mode] || "").split(" ")[0];
      chip.textContent = `${icon} ${c.title}`;
      chip.addEventListener("click", async () => {
        CHIPS_CTX = { type: "project", id: p.id };
        switchView("chat");
        await openChat(c.id);
      });
      chats.append(chip);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.className = "chip chip-add";
    add.textContent = "＋ чат";
    add.addEventListener("click", () => openChatDialog(p.id));
    chats.append(add);
    card.append(chats);
    box.append(card);
  }
}

/* ---------------- аналитика ---------------- */

function weekLabel(a) {
  const f = (iso) => new Date(iso + "T00:00:00")
    .toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  return `${f(a.week_start)} — ${f(a.week_end)}${a.offset === 0 ? " · текущая" : ""}`;
}

async function loadAnalytics() {
  const box = $("#analytics-body");
  box.textContent = "Загружаю…";
  $("#recommend-box").classList.add("hidden");
  $("#week-next").disabled = WEEK_OFFSET >= 0;
  try {
    const a = await api(`/api/analytics?offset=${WEEK_OFFSET}`);
    $("#week-label").textContent = weekLabel(a);
    box.innerHTML = "";

    const hero = document.createElement("div");
    hero.className = "focus-hero";
    hero.innerHTML = a.focus_zone
      ? `<div class="fh-label">Фокус недели</div>
         <div class="fh-zone">${esc(a.focus_zone)}</div>
         <div class="fh-total">закрыто задач: ${a.total_done}</div>`
      : `<div class="fh-label">Фокус недели</div>
         <div class="fh-zone">—</div>
         <div class="fh-total">закрытых задач на этой неделе нет</div>`;
    box.append(hero);

    if (a.zones.length) {
      const sect = document.createElement("div");
      sect.className = "section";
      sect.innerHTML = "<h2 style='margin:14px 0 10px'>По сферам жизни</h2>";
      const max = a.zones[0].count;
      for (const z of a.zones) {
        const row = document.createElement("div");
        row.className = "zone-row";
        row.innerHTML =
          `<div class="zone-top"><span>${esc(z.zone)}</span>` +
          `<span class="z-count">${z.count}</span></div>` +
          `<div class="zone-bar"><i style="width:${Math.round(z.count / max * 100)}%"></i></div>`;
        row.title = z.titles.join("\n");
        sect.append(row);
      }
      box.append(sect);
    }

    if (a.carry_over && a.carry_over.length) {
      const sect = document.createElement("div");
      sect.className = "section";
      sect.innerHTML =
        `<h2 style='margin:14px 0 10px'>Переносится · ${a.carry_total}</h2>`;
      for (const z of a.carry_over) {
        const grp = document.createElement("div");
        grp.className = "carry-zone";
        grp.innerHTML = `<div class="carry-zone-name">${esc(z.zone)}</div>`;
        const ul = document.createElement("ul");
        ul.className = "carry-list";
        for (const title of z.titles) {
          const li = document.createElement("li");
          li.textContent = title;
          ul.append(li);
        }
        grp.append(ul);
        sect.append(grp);
      }
      box.append(sect);
    }

    if (a.habits_week.length) {
      const sect = document.createElement("div");
      sect.className = "section";
      sect.innerHTML = "<h2 style='margin:14px 0 10px'>Привычки за неделю</h2>";
      for (const h of a.habits_week) {
        const row = document.createElement("div");
        row.className = "habit-week";
        row.innerHTML = `<span>${esc(h.name)}</span>` +
                        `<span class="hw-days">${h.days_done} / 7</span>`;
        sect.append(row);
      }
      box.append(sect);
    }
  } catch (err) {
    box.textContent = "Ошибка загрузки аналитики: " + err.message;
  }
}

/* --- переключатель Итоги / План и рендер плана недели --- */

function planWeekLabel(p) {
  const f = (iso) => new Date(iso + "T00:00:00")
    .toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  return `Следующая неделя · ${f(p.week_start)} — ${f(p.week_end)}`;
}

function loadAnalyticsView() {
  const isPlan = ANALYTICS_MODE === "plan";
  $("#analytics-review").classList.toggle("hidden", isPlan);
  $("#analytics-plan").classList.toggle("hidden", !isPlan);
  $$("[data-analytics-mode]").forEach((b) =>
    b.classList.toggle("active", b.dataset.analyticsMode === ANALYTICS_MODE));
  if (isPlan) loadPlan(); else loadAnalytics();
}

async function loadPlan() {
  const box = $("#plan-body");
  box.textContent = "Загружаю…";
  try {
    const p = await api("/api/plan");
    $(".plan-label").textContent = planWeekLabel(p);
    box.innerHTML = "";

    const hero = document.createElement("div");
    hero.className = "focus-hero";
    hero.innerHTML = p.focus_zone
      ? `<div class="fh-label">Фокус следующей недели</div>
         <div class="fh-zone">${esc(p.focus_zone)}</div>
         <div class="fh-total">задач в плане: ${p.total_tasks}</div>`
      : `<div class="fh-label">Следующая неделя</div>
         <div class="fh-zone">чисто</div>
         <div class="fh-total">задач с дедлайном пока нет</div>`;
    box.append(hero);

    // Календарь Пн–Пт
    const calSect = document.createElement("div");
    calSect.className = "section";
    calSect.innerHTML = "<h2 style='margin:14px 0 10px'>Календарь недели</h2>";
    if (!p.calendar_configured) {
      const e = document.createElement("p");
      e.className = "empty";
      e.textContent = "Календарь не подключён.";
      calSect.append(e);
    } else {
      const WD = ["Пн", "Вт", "Ср", "Чт", "Пт"];
      p.calendar.forEach((day, i) => {
        const d = document.createElement("div");
        d.className = "plan-day";
        const dd = new Date(day.date + "T00:00:00").toLocaleDateString(
          "ru-RU", { day: "numeric", month: "short" });
        let inner = `<div class="plan-day-head">${WD[i]} · ${dd}</div>`;
        if (day.events.length) {
          inner += day.events.map((ev) =>
            `<div class="plan-ev"><span class="cal-time">${ev.emoji} ${esc(ev.time)}</span>` +
            `<span>${esc(ev.title)}</span></div>`).join("");
        } else {
          inner += `<div class="plan-ev free">свободно</div>`;
        }
        d.innerHTML = inner;
        calSect.append(d);
      });
    }
    box.append(calSect);

    // Задачи по зонам
    if (p.zones.length) {
      const sect = document.createElement("div");
      sect.className = "section";
      sect.innerHTML = "<h2 style='margin:14px 0 10px'>Задачи по сферам</h2>";
      for (const z of p.zones) {
        const grp = document.createElement("div");
        grp.className = "carry-zone";
        grp.innerHTML = `<div class="carry-zone-name">${esc(z.zone)}</div>`;
        const ul = document.createElement("ul");
        ul.className = "carry-list";
        for (const title of z.titles) {
          const li = document.createElement("li");
          li.textContent = title;
          ul.append(li);
        }
        grp.append(ul);
        sect.append(grp);
      }
      box.append(sect);
    }

    // Очередь
    if (p.queue.length) {
      const sect = document.createElement("div");
      sect.className = "section";
      sect.innerHTML = "<h2 style='margin:14px 0 10px'>Висит в очереди</h2>";
      const ul = document.createElement("ul");
      ul.className = "carry-list";
      for (const title of p.queue) {
        const li = document.createElement("li");
        li.textContent = title;
        ul.append(li);
      }
      sect.append(ul);
      box.append(sect);
    }
  } catch (err) {
    box.textContent = "Ошибка загрузки плана: " + err.message;
  }
}

$$("[data-analytics-mode]").forEach((btn) => {
  btn.addEventListener("click", () => {
    ANALYTICS_MODE = btn.dataset.analyticsMode;
    loadAnalyticsView();
  });
});

$("#week-prev").addEventListener("click", () => { WEEK_OFFSET--; loadAnalytics(); });
$("#week-next").addEventListener("click", () => {
  if (WEEK_OFFSET < 0) { WEEK_OFFSET++; loadAnalytics(); }
});

$("#recommend-btn").addEventListener("click", async () => {
  const btn = $("#recommend-btn");
  btn.disabled = true;
  btn.textContent = "Паола анализирует…";
  try {
    const data = await api(`/api/analytics/recommendation?offset=${WEEK_OFFSET}`,
                           { method: "POST" });
    $("#recommend-text").innerHTML = renderMd(data.recommendation);
    $("#recommend-box").classList.remove("hidden");
  } catch (err) {
    alert("Не получилось: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Рекомендации Паолы";
  }
});

/* ---------------- новый чат / проект ---------------- */

function openChatDialog(projectId) {
  const modeSel = $("#dlg-chat-mode");
  modeSel.innerHTML = "";
  for (const [mode, label] of Object.entries(OVERVIEW.modes)) {
    const opt = document.createElement("option");
    opt.value = mode;
    opt.textContent = label;
    modeSel.append(opt);
  }
  const projSel = $("#dlg-chat-project");
  projSel.innerHTML = '<option value="">— без проекта —</option>';
  for (const p of OVERVIEW.projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    projSel.append(opt);
  }
  if (projectId) projSel.value = projectId;
  $("#dlg-chat-title").value = "";
  $("#dlg-chat").showModal();
}

$("#dlg-chat-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  try {
    const projectId = $("#dlg-chat-project").value || null;
    const chat = await api("/api/chats", { method: "POST", body: JSON.stringify({
      mode: $("#dlg-chat-mode").value,
      project_id: projectId,
      title: $("#dlg-chat-title").value,
    }) });
    CHIPS_CTX = projectId ? { type: "project", id: projectId } : { type: "standalone" };
    await loadOverview(chat.id);
    switchView("chat");
  } catch (err) {
    alert("Не удалось создать чат: " + err.message);
  }
});

$("#new-project-btn").addEventListener("click", () => {
  $("#dlg-project-name").value = "";
  $("#dlg-project-desc").value = "";
  $("#dlg-project").showModal();
});

$("#dlg-project-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  const name = $("#dlg-project-name").value.trim();
  if (!name) return;
  try {
    await api("/api/projects", { method: "POST", body: JSON.stringify({
      name, description: $("#dlg-project-desc").value.trim(),
    }) });
    await loadOverview();
  } catch (err) {
    alert("Не удалось создать проект: " + err.message);
  }
});

/* ---------------- старт ---------------- */

(async function init() {
  try {
    await loadOverview();
    showApp();
    const saved = localStorage.getItem("paola_chat");
    if (saved) { try { await openChat(saved, true); } catch (_) {} }
  } catch (_) {
    showLogin();
  }
})();
