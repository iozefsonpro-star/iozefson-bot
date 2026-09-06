/* Паола App — фронтенд без сборки: fetch + vanilla JS. Редизайн «Небо». */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let WEEK_OFFSET = 0;
let CAL_SCOPE = "today";
let ANALYTICS_MODE = "review";   // review — итоги недели; plan — план следующей

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

/* ---------------- мини-markdown для ответов Паолы (аналитика) ---------------- */

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
  loadCalendar("today");
  loadTasks();
  loadHabits();
  loadDayStatus();
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
  ["home", "analytics"].forEach((v) => {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
  });
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  if (name === "analytics") loadAnalyticsView();
  window.scrollTo(0, 0);
}

$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

/* ---------------- дата (локальная, для быстрых кнопок дедлайна/завтра) ---------------- */

function isoDateLocal(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* ---------------- календарь ---------------- */

function fmtDay(iso) {
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "long" });
}

function calEmptyText(scope, hiddenPastToday) {
  if (scope === "today") {
    return hiddenPastToday > 0
      ? "Встречи на сегодня закончились — остаток дня можно отдать фокусу."
      : "Сегодня встреч нет — день можно отдать фокусу.";
  }
  if (scope === "tomorrow") return "Завтра встреч нет — день свободен.";
  return "На неделе событий нет — окно для глубокой работы.";
}

async function loadCalendar(scope) {
  CAL_SCOPE = scope;
  const box = $("#calendar-body");
  box.textContent = "Загружаю…";
  try {
    const qs = scope === "week" ? "scope=week&days=6" : `scope=${scope}`;
    const data = await api(`/api/calendar?${qs}`);
    box.innerHTML = "";
    if (!data.configured) {
      box.textContent = "Календарь не подключён: заполни GOOGLE_TOKEN_JSON в переменных сервиса.";
      return;
    }
    const longEvents = data.long || [];
    const multiDay = scope === "week";
    if (!data.days.length && !longEvents.length) {
      box.textContent = calEmptyText(scope, data.hidden_past_today);
    } else if (!data.days.length) {
      const e = document.createElement("div");
      e.className = "cal-empty";
      e.textContent = calEmptyText(scope, data.hidden_past_today);
      box.append(e);
    }
    for (const day of data.days) {
      if (multiDay) {
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

$$("[data-cal-scope]").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("[data-cal-scope]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadCalendar(btn.dataset.calScope);
  });
});

$("#cal-refresh").addEventListener("click", async () => {
  const btn = $("#cal-refresh");
  btn.classList.add("spinning");
  btn.disabled = true;
  await loadCalendar(CAL_SCOPE);
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

  const del = document.createElement("button");
  del.type = "button";
  del.className = "t-delete";
  del.textContent = "✕";
  del.setAttribute("aria-label", "Удалить задачу: " + t.title);
  del.addEventListener("click", () => deleteTask(t, li));
  li.append(del);

  return li;
}

async function deleteTask(t, li) {
  if (!confirm(`Удалить задачу «${t.title}»?`)) return;
  try {
    await api(`/api/tasks/${t.id}`, { method: "DELETE" });
    li.remove();
    loadDayStatus();
  } catch (err) {
    alert("Не удалось удалить: " + err.message);
  }
}

async function completeTask(taskId, li, check) {
  check.disabled = true;
  try {
    await api(`/api/tasks/${taskId}/complete`, { method: "POST" });
    li.classList.add("t-done");
    setTimeout(() => { loadTasks(); loadDayStatus(); }, 220);   // короткая анимация — потом обновляем список
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
  const clearing = e.submitter && e.submitter.value === "clear";
  const newDate = clearing ? null : $("#dlg-resched-date").value;
  if (!clearing && !newDate) return;
  try {
    await api(`/api/tasks/${RESCHED_TASK.id}/reschedule`, {
      method: "POST", body: JSON.stringify({ new_date: newDate }),
    });
    await loadTasks();
    await loadDayStatus();
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

async function loadDayStatus() {
  const card = $("#day-status-card");
  try {
    const d = await api("/api/day-status");
    const txt = $("#day-status-text");
    const focus = d.focus_zone ? `Фокус: <b>${esc(d.focus_zone)}</b>. ` : "";
    if (d.mode === "morning") {
      const meet = d.meetings_today === 1 ? "1 встреча"
        : `${d.meetings_today} встреч`;
      const overdueClause = d.overdue > 0
        ? `Просрочено: <b>${d.overdue}</b>. ` : "Просроченного нет. ";
      txt.innerHTML =
        `Сегодня: <b>${d.today_total} задач</b> с дедлайном, ${meet}. ` +
        focus + overdueClause +
        `Без даты: <b>${d.undated}</b>.`;
    } else {
      const overdueClause = d.overdue > 0
        ? `Просрочено <b>${d.overdue}</b> — стоит пересмотреть дедлайн.`
        : "Просроченных нет — всё по плану.";
      txt.innerHTML =
        `На сегодня было запланировано <b>${d.today_total} задач</b>, ` +
        `выполнено <b>${d.today_done}</b>. ${focus}${overdueClause}`;
    }
    card.classList.remove("hidden");
  } catch (_) {
    card.classList.add("hidden");
  }
}

async function loadTasks() {
  const box = $("#tasks-body");
  box.textContent = "Загружаю…";
  try {
    const data = await api("/api/tasks");
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
  } catch (err) {
    box.textContent = "Ошибка загрузки задач: " + err.message;
  }
}

/* ---------------- новая задача ---------------- */

function openTaskDialog() {
  $("#dlg-task-title").value = "";
  $("#dlg-task-zone").value = "💼 Бизнес";
  $$("#dlg-task-priority .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.priority === "✅ Обычное"));
  $("#dlg-task-date").value = "";
  $$(".task-date-quick [data-quick-date]").forEach((b) =>
    b.classList.toggle("active", b.dataset.quickDate === "none"));
  $("#dlg-task-comment").value = "";
  $(".dlg-comment").removeAttribute("open");
  $("#dlg-task").showModal();
}

$("#new-task-btn").addEventListener("click", openTaskDialog);

$$("#dlg-task-priority .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("#dlg-task-priority .seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
  });
});

$$(".task-date-quick [data-quick-date]").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".task-date-quick [data-quick-date]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const kind = btn.dataset.quickDate;
    if (kind === "today") $("#dlg-task-date").value = isoDateLocal(new Date());
    else if (kind === "tomorrow") $("#dlg-task-date").value = isoDateLocal(new Date(Date.now() + 86400000));
    else $("#dlg-task-date").value = "";
  });
});

$("#dlg-task-date").addEventListener("input", () => {
  $$(".task-date-quick [data-quick-date]").forEach((b) => b.classList.remove("active"));
});

$("#dlg-task-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  const title = $("#dlg-task-title").value.trim();
  if (!title) return;
  const priorityBtn = $("#dlg-task-priority .seg-btn.active");
  try {
    await api("/api/tasks", { method: "POST", body: JSON.stringify({
      title,
      zone: $("#dlg-task-zone").value,
      priority: priorityBtn ? priorityBtn.dataset.priority : "✅ Обычное",
      deadline: $("#dlg-task-date").value || null,
      comment: $("#dlg-task-comment").value.trim() || null,
    }) });
    await loadTasks();
    await loadDayStatus();
  } catch (err) {
    alert("Не удалось создать задачу: " + err.message);
  }
});

/* ---------------- голосовой ввод для задачи (Web Speech API) ---------------- */

(function initTaskVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = $("#task-mic-btn");
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
    baseText = $("#dlg-task-title").value.trim();
    try { rec.start(); } catch (_) {}
  });

  rec.addEventListener("start", () => {
    listening = true;
    mic.classList.add("recording");
  });

  rec.addEventListener("result", (e) => {
    let text = "";
    for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
    $("#dlg-task-title").value = (baseText ? baseText + " " : "") + text.trim();
  });

  const stop = () => { listening = false; mic.classList.remove("recording"); };
  rec.addEventListener("end", stop);
  rec.addEventListener("error", stop);
})();

/* ---------------- привычки ---------------- */

function checkboxHabitLi(h) {
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
  return li;
}

function numberHabitLi(h) {
  const goalMet = h.goal_number != null && h.value_today != null && h.value_today >= h.goal_number;
  const li = document.createElement("li");
  li.className = "habit habit-number" + (goalMet ? " goal-met" : "");
  const name = document.createElement("span");
  name.className = "h-name";
  name.textContent = h.name;

  const goalBox = document.createElement("div");
  goalBox.className = "h-goal";
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.className = "h-value-input";
  input.value = h.value_today ?? "";
  input.setAttribute("aria-label", h.name);
  const label = document.createElement("span");
  label.className = "h-goal-label";
  label.textContent = h.goal_number != null
    ? `/ ${h.goal_number}${h.unit ? " " + h.unit : ""}` : (h.unit || "");
  input.addEventListener("change", async () => {
    if (input.value === "") return;
    const value = Number(input.value);
    input.disabled = true;
    try {
      const done = h.goal_number != null ? value >= h.goal_number : true;
      await api("/api/habits/log", { method: "POST",
        body: JSON.stringify({ habit_name: h.name, done, value }) });
      await loadHabits();
    } catch (err) {
      input.disabled = false;
      alert("Не удалось сохранить: " + err.message);
    }
  });
  goalBox.append(input, label);

  const streak = document.createElement("span");
  streak.className = "h-streak";
  streak.textContent = h.streak > 0 ? `${h.streak} дн.` : "";
  li.append(name, goalBox, streak);
  return li;
}

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
      list.append(h.type === "Число" ? numberHabitLi(h) : checkboxHabitLi(h));
    }
  } catch (err) {
    empty.textContent = "Ошибка загрузки привычек: " + err.message;
    empty.className = "empty error";
  }
}

/* ---------------- аналитика (Неделя): итоги / план ---------------- */

function weekLabel(a) {
  const f = (iso) => new Date(iso + "T00:00:00")
    .toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  return `${f(a.week_start)} — ${f(a.week_end)}${a.offset === 0 ? " · текущая" : ""}`;
}

async function loadAnalytics() {
  const box = $("#analytics-body");
  box.textContent = "Загружаю…";
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

/* ---------------- старт ---------------- */

(async function init() {
  try {
    await api("/api/day-status");
    showApp();
  } catch (_) {
    showLogin();
  }
})();
