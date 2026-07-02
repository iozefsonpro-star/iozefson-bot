/* Паола App — фронтенд без сборки: fetch + vanilla JS */
"use strict";

const $ = (sel) => document.querySelector(sel);

let OVERVIEW = { projects: [], chats: [], modes: {} };
let CURRENT_CHAT = null; // {id, mode, title, ...}

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

/* ---------------- вход ---------------- */

function showLogin() {
  $("#login-screen").classList.remove("hidden");
  $("#app").classList.add("hidden");
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#today-date").textContent = new Date().toLocaleDateString("ru-RU",
    { weekday: "long", day: "numeric", month: "long" });
  loadCalendar(0);
  loadTasks();
  loadHabits();
  loadReminders();
  loadOverview();
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

/* ---------------- календарь ---------------- */

function fmtDay(iso) {
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "long" });
}

async function loadCalendar(days) {
  const box = $("#calendar-body");
  box.textContent = "Загружаю…";
  try {
    const data = await api(`/api/calendar?days=${days}`);
    box.innerHTML = "";
    if (!data.configured) {
      box.textContent = "Календарь не подключён: заполни GOOGLE_TOKEN_JSON в переменных сервиса.";
      return;
    }
    if (!data.days.length) {
      box.textContent = days > 0 ? "На неделе событий нет." : "Сегодня событий нет.";
      return;
    }
    for (const day of data.days) {
      const h = document.createElement("div");
      h.className = "cal-day";
      h.textContent = fmtDay(day.date);
      box.append(h);
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
  } catch (err) {
    box.textContent = "Не удалось загрузить календарь: " + err.message;
  }
}

document.querySelectorAll(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadCalendar(parseInt(btn.dataset.calDays, 10));
  });
});

/* ---------------- задачи ---------------- */

function taskLi(t) {
  const li = document.createElement("li");
  li.className = "task";
  const icon = { "❗ Важное": "🔴", "✅ Обычное": "🟢", "🔜 Когда-нибудь": "⚪" }[t.priority] || "⚪";
  const title = document.createElement("span");
  title.className = "t-title";
  title.textContent = `${icon} ${t.title}`;
  const meta = document.createElement("span");
  meta.className = "t-meta";
  meta.textContent = [t.deadline ? t.deadline.slice(0, 10) : "", t.zone]
    .filter(Boolean).join(" · ");
  li.append(title, meta);
  return li;
}

function taskGroup(label, tasks, cls) {
  if (!tasks.length) return null;
  const div = document.createElement("div");
  div.className = "task-group" + (cls ? " " + cls : "");
  const h = document.createElement("h3");
  h.textContent = `${label} (${tasks.length})`;
  const ul = document.createElement("ul");
  ul.className = "task-list";
  tasks.forEach((t) => ul.append(taskLi(t)));
  div.append(h, ul);
  return div;
}

async function loadTasks() {
  const box = $("#tasks-body");
  box.textContent = "Загружаю…";
  try {
    const data = await api("/api/tasks");
    box.innerHTML = "";
    const groups = [
      taskGroup("⚠️ Просрочено", data.overdue, "overdue"),
      taskGroup("Сегодня", data.today, ""),
      taskGroup("Остальные активные", data.other, ""),
    ].filter(Boolean);
    if (!groups.length) {
      box.textContent = "Активных задач нет.";
    } else {
      groups.forEach((g) => box.append(g));
    }
  } catch (err) {
    box.textContent = "Ошибка загрузки задач: " + err.message;
  }
}

$("#tasks-refresh").addEventListener("click", loadTasks);

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
      streak.textContent = h.streak > 0 ? `🔥 ${h.streak} дн.` : "";
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

/* ---------------- диалоги: рельса ---------------- */

function railItem(chat) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rail-item" + (CURRENT_CHAT && CURRENT_CHAT.id === chat.id ? " active" : "");
  const modeIcon = (OVERVIEW.modes[chat.mode] || "").split(" ")[0];
  btn.textContent = `${modeIcon} ${chat.title}`;
  btn.addEventListener("click", () => openChat(chat.id));
  return btn;
}

function renderRail() {
  const box = $("#rail-list");
  box.innerHTML = "";

  for (const p of OVERVIEW.projects) {
    const sec = document.createElement("div");
    sec.className = "rail-section";
    const name = document.createElement("span");
    name.textContent = `📁 ${p.name}`;
    const add = document.createElement("button");
    add.type = "button";
    add.className = "mini-btn";
    add.textContent = "＋";
    add.title = `Новый чат в проекте «${p.name}»`;
    add.addEventListener("click", () => openChatDialog(p.id));
    sec.append(name, add);
    box.append(sec);
    p.chats.forEach((c) => box.append(railItem(c)));
  }

  if (OVERVIEW.chats.length) {
    const sec = document.createElement("div");
    sec.className = "rail-section";
    sec.textContent = "Чаты";
    box.append(sec);
    OVERVIEW.chats.forEach((c) => box.append(railItem(c)));
  }

  // быстрое создание: один клик — новый чат нужного типа
  const sec = document.createElement("div");
  sec.className = "rail-section";
  sec.textContent = "Новый чат";
  box.append(sec);
  for (const [mode, label] of Object.entries(OVERVIEW.modes)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rail-item quick-new";
    btn.textContent = `＋ ${label}`;
    btn.addEventListener("click", async () => {
      try {
        const chat = await api("/api/chats", { method: "POST",
          body: JSON.stringify({ mode, project_id: null, title: "" }) });
        await loadOverview(chat.id);
      } catch (err) {
        alert("Не удалось создать чат: " + err.message);
      }
    });
    box.append(btn);
  }
}

async function loadOverview(selectChatId) {
  OVERVIEW = await api("/api/overview");
  renderRail();
  const wanted = selectChatId || localStorage.getItem("paola_chat");
  const all = [...OVERVIEW.chats, ...OVERVIEW.projects.flatMap((p) => p.chats)];
  const target = all.find((c) => c.id === wanted) || all[0];
  if (target && (!CURRENT_CHAT || CURRENT_CHAT.id !== target.id || selectChatId)) {
    await openChat(target.id);
  }
}

/* ---------------- диалоги: чат ---------------- */

function addMsg(cls, text) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  $("#chat-log").append(div);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return div;
}

const MODE_HINTS = {
  translator: "Вставь текст — переведу. Можно указать направление и стиль.",
  research:   "Назови тему — сделаю ресерч с источниками.",
  board:      "Опиши идею — соберу совет директоров.",
  business:   "Опиши бизнес или вставь данные — разберу модель.",
  assistant:  "Спроси или поручи: задачи, календарь, привычки, напоминания…",
};

async function openChat(chatId) {
  try {
    const chat = await api(`/api/chats/${chatId}`);
    CURRENT_CHAT = chat;
    localStorage.setItem("paola_chat", chat.id);
    $("#chat-title").textContent = chat.title;
    $("#chat-mode").textContent = (OVERVIEW.modes[chat.mode] || chat.mode)
      + (chat.project_name ? ` · проект «${chat.project_name}»` : "");
    $("#chat-input").placeholder = MODE_HINTS[chat.mode] || "Напиши…";
    const log = $("#chat-log");
    log.innerHTML = "";
    if (!chat.messages.length) {
      addMsg("assistant", MODE_HINTS[chat.mode] || "Слушаю!");
    }
    chat.messages.forEach((m) => addMsg(m.role === "user" ? "user" : "assistant", m.content));
    renderRail();
  } catch (err) {
    alert("Не удалось открыть чат: " + err.message);
  }
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!CURRENT_CHAT) return;
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  addMsg("user", message);
  const pending = addMsg("thinking", "Паола думает…");
  $("#chat-send").disabled = true;
  try {
    const data = await api(`/api/chats/${CURRENT_CHAT.id}/messages`,
      { method: "POST", body: JSON.stringify({ message }) });
    pending.remove();
    addMsg("assistant", data.reply);
    loadOverview(CURRENT_CHAT.id); // название чата могло обновиться
    loadTasks();
    loadHabits();
    loadReminders();
  } catch (err) {
    pending.remove();
    addMsg("error", "Ошибка: " + err.message);
  } finally {
    $("#chat-send").disabled = false;
    input.focus();
  }
});

$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#chat-delete").addEventListener("click", async () => {
  if (!CURRENT_CHAT) return;
  if (!confirm(`Удалить чат «${CURRENT_CHAT.title}» вместе с историей?`)) return;
  try {
    await api(`/api/chats/${CURRENT_CHAT.id}`, { method: "DELETE" });
    CURRENT_CHAT = null;
    localStorage.removeItem("paola_chat");
    await loadOverview();
  } catch (err) {
    alert("Не удалось удалить: " + err.message);
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

$("#new-chat-btn").addEventListener("click", () => openChatDialog());

$("#dlg-chat-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  try {
    const chat = await api("/api/chats", { method: "POST", body: JSON.stringify({
      mode: $("#dlg-chat-mode").value,
      project_id: $("#dlg-chat-project").value || null,
      title: $("#dlg-chat-title").value,
    }) });
    await loadOverview(chat.id);
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
    await loadOverview(CURRENT_CHAT && CURRENT_CHAT.id);
  } catch (err) {
    alert("Не удалось создать проект: " + err.message);
  }
});

/* ---------------- старт ---------------- */

(async function init() {
  try {
    await api("/api/overview"); // проверка сессии
    showApp();
  } catch (_) {
    showLogin();
  }
})();
