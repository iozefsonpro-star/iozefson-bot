/* Паола App — фронтенд без сборки: fetch + vanilla JS */
"use strict";

const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (resp.status === 401) { showLogin(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
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
  const now = new Date();
  $("#today-date").textContent = now.toLocaleDateString("ru-RU",
    { weekday: "long", day: "numeric", month: "long" });
  loadDigest("morning");
  loadHabits();
  loadReminders();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").classList.add("hidden");
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#login-password").value }),
    });
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

/* ---------------- сводка ---------------- */

async function loadDigest(kind) {
  $("#digest-text").textContent = "Загружаю…";
  try {
    const data = await api(`/api/digest?kind=${kind}`);
    $("#digest-text").textContent = data.text;
  } catch (err) {
    $("#digest-text").textContent = "Не удалось загрузить сводку: " + err.message;
  }
}

document.querySelectorAll(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadDigest(btn.dataset.digest);
  });
});

/* ---------------- привычки ---------------- */

async function loadHabits() {
  try {
    const data = await api("/api/habits");
    $("#habits-date").textContent = data.date;
    const list = $("#habits-list");
    list.innerHTML = "";
    $("#habits-empty").classList.toggle("hidden", data.habits.length > 0);
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
          await api("/api/habits/log", {
            method: "POST",
            body: JSON.stringify({ habit_name: h.name, done: cb.checked }),
          });
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
    $("#habits-empty").textContent = "Ошибка загрузки привычек: " + err.message;
    $("#habits-empty").classList.remove("hidden");
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
  try {
    const data = await api("/api/reminders");
    const list = $("#reminders-list");
    list.innerHTML = "";
    $("#reminders-empty").classList.toggle("hidden", data.reminders.length > 0);
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
    $("#reminders-empty").textContent = "Ошибка: " + err.message;
    $("#reminders-empty").classList.remove("hidden");
  }
}

$("#reminder-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#reminder-text").value.trim();
  const when = $("#reminder-when").value; // YYYY-MM-DDTHH:MM (локальное время)
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

/* ---------------- чат-аналитика ---------------- */

function addMsg(cls, text) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  $("#chat-log").append(div);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return div;
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  addMsg("user", message);
  const pending = addMsg("thinking", "Паола думает…");
  $("#chat-send").disabled = true;
  try {
    const data = await api("/api/chat", { method: "POST",
                                          body: JSON.stringify({ message }) });
    pending.remove();
    addMsg("assistant", data.reply);
    // после действий агента данные слева могли измениться
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

document.querySelectorAll(".qa").forEach((btn) => {
  btn.addEventListener("click", () => {
    const input = $("#chat-input");
    input.value = btn.dataset.prefill;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
});

$("#chat-reset").addEventListener("click", async () => {
  try { await api("/api/chat/reset", { method: "POST" }); } catch (_) {}
  $("#chat-log").innerHTML = "";
  addMsg("assistant", "Новый диалог. Слушаю!");
});

/* ---------------- старт ---------------- */

(async function init() {
  try {
    await api("/api/habits"); // проверка сессии
    showApp();
  } catch (_) {
    showLogin();
  }
})();
