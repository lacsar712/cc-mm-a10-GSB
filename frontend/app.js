const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let readingsCache = [];
let tasksCache = { open: [], overdue: [], done: [] };

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const viewReadings = document.querySelector("#view-readings");
const viewTasks = document.querySelector("#view-tasks");
const taskMsg = document.querySelector("#task-msg");

function esc(v) {
  return String(v).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function fmtDeadline(iso) {
  return new Date(iso).toLocaleString();
}

function paintReadings(list) {
  const busy = new Set(
    [...tasksCache.open, ...tasksCache.overdue].map((t) => t.source_reading_id),
  );
  const doneMap = new Map(tasksCache.done.map((t) => [t.source_reading_id, t]));
  rows.innerHTML = list
    .map((r) => {
      const task = doneMap.get(r.id);
      let action = "";
      if (role === "writer" && r.level === "报警" && !busy.has(r.id)) {
        action = `<button data-raise="${r.id}">拉起复测</button>`;
      } else if (busy.has(r.id)) {
        action = `<span class="muted">复测中</span>`;
      } else if (task) {
        action = `<a class="link" data-jump="${task.retest_reading_id}">新行 #${task.retest_reading_id}</a>`;
      }
      return `<tr data-row="${r.id}">
        <td>#${r.id}${r.is_retest ? '<span class="tag">复测</span>' : ""}</td>
        <td>${esc(r.site)}</td><td>${r.ch4_pct}</td>
        <td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td>
        <td>${esc(r.note)}</td>${role === "writer" ? `<td>${action}</td>` : ""}</tr>`;
    })
    .join("");
}

function taskCard(t, state) {
  const overdueCls = state === "overdue" ? " overdue" : "";
  const head = `<a class="link" data-jump="${t.source_reading_id}">#${t.source_reading_id}</a>
    ${esc(t.source_site)} 原测 ${t.source_ch4_pct}%`;
  if (state === "done") {
    return `<div class="task-card">
      <div>原报警 ${head} → 新班测行
        <a class="link" data-jump="${t.retest_reading_id}">#${t.retest_reading_id}</a>
        复测 ${t.retest_ch4_pct}%（${t.retest_level}）</div>
      <div class="muted">${fmtDeadline(t.created_at)} 由 ${esc(t.created_by)} 提交</div>
    </div>`;
  }
  const submit =
    state === "open" && role === "writer"
      ? `<form data-submit="${t.id}">
          <input type="number" step="0.01" placeholder="复测甲烷 %" required />
          <button>提交复测</button>
        </form>`
      : state === "overdue"
        ? '<div class="alarm">已逾期，不能再提交</div>'
        : "";
  return `<div class="task-card${overdueCls}">
    <div>原报警 ${head}</div>
    <div class="muted">截止：${fmtDeadline(t.deadline)}　${state === "overdue" ? '<span class="alarm">已逾期</span>' : ""}</div>
    ${submit}
  </div>`;
}

function paintTasks() {
  document.querySelector("#tasks-open").innerHTML =
    tasksCache.open.map((t) => taskCard(t, "open")).join("") || '<p class="muted">暂无</p>';
  document.querySelector("#tasks-overdue").innerHTML =
    tasksCache.overdue.map((t) => taskCard(t, "overdue")).join("") || '<p class="muted">暂无</p>';
  document.querySelector("#tasks-done").innerHTML =
    tasksCache.done.map((t) => taskCard(t, "done")).join("") || '<p class="muted">暂无</p>';
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function loadReadings() {
  readingsCache = await api("/api/readings");
  paintReadings(readingsCache);
}

async function loadTasks() {
  tasksCache = await api("/api/retest-tasks");
  paintTasks();
  if (!viewReadings.hidden) paintReadings(readingsCache);
}

async function loadAll() {
  await Promise.all([loadReadings(), loadTasks()]);
}

function showView(name) {
  const readings = name === "readings";
  viewReadings.hidden = !readings;
  viewTasks.hidden = readings;
  document.querySelector("#nav-readings").classList.toggle("active", readings);
  document.querySelector("#nav-tasks").classList.toggle("active", !readings);
  if (!readings) loadTasks();
}

async function jumpToReading(id) {
  showView("readings");
  try {
    await loadReadings();
  } catch {
    paintReadings(readingsCache);
  }
  const el = document.querySelector(`tr[data-row="${id}"]`);
  if (el) {
    el.classList.add("flash");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  document.querySelector("#th-actions").style.display = role === "writer" ? "" : "none";
  connect();
  loadAll();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    loadReadings().then(loadTasks);
  };
}

function defaultDeadline() {
  const d = new Date(Date.now() + 2 * 60 * 60 * 1000);
  d.setSeconds(0, 0);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

rows.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-raise]");
  if (!btn) return;
  const sourceId = Number(btn.dataset.raise);
  const picked = prompt("截止时刻（格式 2026-09-23T18:00，本地时间）", defaultDeadline());
  if (picked === null) return;
  if (isNaN(Date.parse(picked))) {
    live.textContent = "截止时刻格式不正确";
    return;
  }
  const deadline = new Date(picked).toISOString();
  try {
    await api("/api/retest-tasks", {
      method: "POST",
      body: JSON.stringify({ source_reading_id: sourceId, deadline }),
    });
    live.textContent = "复测任务已拉起";
    await loadTasks();
  } catch (err) {
    live.textContent = err.message;
  }
});

document.querySelector("#view-tasks").addEventListener("submit", async (e) => {
  const f = e.target.closest("[data-submit]");
  if (!f) return;
  e.preventDefault();
  const taskId = f.dataset.submit;
  try {
    await api(`/api/retest-tasks/${taskId}/submit`, {
      method: "POST",
      body: JSON.stringify({ ch4_pct: Number(f.querySelector("input").value) }),
    });
    taskMsg.textContent = "复测已提交，已生成新班测行";
    await loadTasks();
  } catch (err) {
    taskMsg.textContent = err.message;
  }
});

document.querySelector("#view-tasks").addEventListener("click", (e) => {
  const a = e.target.closest("[data-jump]");
  if (a) jumpToReading(Number(a.dataset.jump));
});

rows.addEventListener("click", (e) => {
  const a = e.target.closest("[data-jump]");
  if (a) jumpToReading(Number(a.dataset.jump));
});

document.querySelector("#nav-readings").onclick = () => showView("readings");
document.querySelector("#nav-tasks").onclick = () => showView("tasks");

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
