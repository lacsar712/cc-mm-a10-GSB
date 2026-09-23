const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let readings = [];
let tasks = [];
let view = "readings";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const rowsEl = document.querySelector("#rows");
const openEl = document.querySelector("#tasks-open");
const expiredEl = document.querySelector("#tasks-expired");
const doneEl = document.querySelector("#tasks-done");

const isWriter = () => role === "writer";

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
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

/* ---------- 视图切换 ---------- */

function showView(name) {
  view = name;
  document.querySelector("#view-readings").hidden = name !== "readings";
  document.querySelector("#view-tasks").hidden = name !== "tasks";
  document.querySelector("#nav-readings").classList.toggle("active", name === "readings");
  document.querySelector("#nav-tasks").classList.toggle("active", name === "tasks");
  if (name === "tasks") return loadTasks();
  if (name === "readings") return loadReadings();
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = isWriter() ? "检查员" : "查看（旁观）";
  document.querySelector("#out").hidden = false;
  form.hidden = !isWriter();
  connect();
  Promise.all([loadReadings(), loadTasks()]);
  // 定时刷新，逾期状态随截止时刻翻转（仅任务页，避免打断行内填写）
  setInterval(() => {
    if (!appBox.hidden && view === "tasks") loadTasks();
  }, 20000);
}

/* ---------- 班测列表 ---------- */

async function loadReadings() {
  readings = await api("/api/readings");
  paintReadings();
}

function taskByAlert() {
  const m = new Map();
  for (const t of tasks) if (!m.has(t.alert_reading_id)) m.set(t.alert_reading_id, t);
  return m;
}

function paintReadings() {
  const alertMap = taskByAlert();
  rowsEl.innerHTML = readings
    .map((r) => {
      let link = `<span class="muted">班测</span>`;
      if (r.retest_task_id) {
        const t = tasks.find((x) => x.id === r.retest_task_id);
        link = `复测任务<button class="linklike" data-act="jump-task" data-id="${r.retest_task_id}">#${r.retest_task_id}</button>`
          + (t ? `（原报警<button class="linklike" data-act="jump-reading" data-id="${t.alert_reading_id}">#${t.alert_reading_id}</button>）` : "");
      } else if (r.level === "报警" && alertMap.has(r.id)) {
        const t = alertMap.get(r.id);
        const tail = t.status === "done" && t.retest_reading_id
          ? `→ 新行<button class="linklike" data-act="jump-reading" data-id="${t.retest_reading_id}">#${t.retest_reading_id}</button>`
          : t.status === "expired" ? `<span class="alarm">已逾期</span>` : `<span class="muted">待复测</span>`;
        link = `复测任务<button class="linklike" data-act="jump-task" data-id="${t.id}">#${t.id}</button> ${tail}`;
      }
      const op = r.level === "报警" && isWriter()
        ? `<button data-act="retest-start" data-id="${r.id}">拉起复测</button>`
        : "";
      return `<tr id="reading-${r.id}">
        <td>#${r.id}</td>
        <td>${esc(r.site)}</td>
        <td>${r.ch4_pct}</td>
        <td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td>
        <td>${esc(r.note)}</td>
        <td>${link}</td>
        <td id="op-${r.id}">${op}</td>
      </tr>`;
    })
    .join("");
}

function highlight(selector) {
  document.querySelectorAll(".hilite").forEach((el) => el.classList.remove("hilite"));
  const el = document.querySelector(selector);
  if (el) {
    el.classList.add("hilite");
    el.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }
}

async function jumpReading(id) {
  await showView("readings");
  highlight(`#reading-${id}`);
}

/* ---------- 复测任务页 ---------- */

async function loadTasks() {
  tasks = await api("/api/retest-tasks");
  paintTasks();
  paintReadings();
}

function taskCard(t) {
  const oldLink = `<button class="linklike" data-act="jump-reading" data-id="${t.alert_reading_id}">原报警 #${t.alert_reading_id}</button>`;
  let chain, body;
  if (t.status === "done") {
    chain = `${oldLink} → 新班测行 <button class="linklike" data-act="jump-reading" data-id="${t.retest_reading_id}">#${t.retest_reading_id}</button>`;
    body = `<p>${chain}</p>
      <p class="muted">${esc(t.submitted_by || "")} 于 ${fmtTime(t.submitted_at)} 提交复测</p>`;
  } else if (t.status === "expired") {
    chain = oldLink;
    body = `<p>${chain}</p>
      <p class="alarm">已逾期，截止时刻 ${fmtTime(t.deadline)}</p>`;
  } else {
    chain = oldLink;
    const submit = isWriter()
      ? `<span> 复测甲烷 <input type="number" step="0.01" id="retest-ch4-${t.id}" style="width:90px" />
         <button data-act="retest-submit" data-id="${t.id}">提交复测</button></span>`
      : "";
    body = `<p>${chain}</p>
      <p>截止时刻 ${fmtTime(t.deadline)} ${submit}</p>`;
  }
  return `<div class="card ${t.status === "expired" ? "overdue" : ""}" id="task-${t.id}">
    <strong>#${t.id} ${esc(t.site)}</strong>
    ${body}
    <p class="muted">由 ${esc(t.created_by)} 于 ${fmtTime(t.created_at)} 拉起</p>
  </div>`;
}

function paintTasks() {
  const open = tasks.filter((t) => t.status === "open");
  const expired = tasks.filter((t) => t.status === "expired");
  const done = tasks.filter((t) => t.status === "done");
  openEl.innerHTML = open.length ? open.map(taskCard).join("") : `<span class="muted">暂无</span>`;
  expiredEl.innerHTML = expired.length ? expired.map(taskCard).join("") : `<span class="muted">暂无</span>`;
  doneEl.innerHTML = done.length ? done.map(taskCard).join("") : `<span class="muted">暂无</span>`;
}

async function jumpTask(id) {
  await showView("tasks");
  highlight(`#task-${id}`);
}

/* ---------- 拉起复测（行内内联表单） ---------- */

function localDeadlineDefault() {
  const d = new Date(Date.now() + 2 * 60 * 60 * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function startRetest(readingId) {
  document.querySelector(`#op-${readingId}`).innerHTML =
    `<input type="datetime-local" id="deadline-${readingId}" value="${localDeadlineDefault()}" step="60" />
     <button data-act="retest-ok" data-id="${readingId}">确定</button>
     <button data-act="retest-cancel" data-id="${readingId}">取消</button>`;
}

async function confirmRetest(readingId) {
  const v = document.querySelector(`#deadline-${readingId}`).value;
  if (!v) { live.textContent = "请选择截止时刻"; return; }
  const deadline = new Date(v).toISOString();
  try {
    const t = await api("/api/retest-tasks", {
      method: "POST",
      body: JSON.stringify({ alert_reading_id: readingId, deadline }),
    });
    live.textContent = `复测任务 #${t.id} 已拉起，截止 ${fmtTime(t.deadline)}`;
    await loadTasks();
    jumpTask(t.id);
  } catch (err) {
    live.textContent = err.message;
    paintReadings();
  }
}

async function submitRetest(taskId) {
  const input = document.querySelector(`#retest-ch4-${taskId}`);
  const ch4 = Number(input.value);
  if (!input.value || Number.isNaN(ch4)) { live.textContent = "请填写复测甲烷值"; return; }
  try {
    const res = await api(`/api/retest-tasks/${taskId}/submit`, {
      method: "POST",
      body: JSON.stringify({ ch4_pct: ch4 }),
    });
    live.textContent = `复测已提交：新班测行 #${res.reading.id}`;
    await loadTasks();
    jumpTask(taskId);
  } catch (err) {
    live.textContent = err.message;
    loadTasks();
  }
}

/* ---------- 实时推送 ---------- */

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    loadReadings();
    loadTasks();
  };
}

/* ---------- 事件 ---------- */

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
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
    form.reset();
  } catch (err) {
    live.textContent = err.message;
  }
};

document.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  switch (btn.dataset.act) {
    case "jump-task": jumpTask(id); break;
    case "jump-reading": jumpReading(id); break;
    case "retest-start": startRetest(id); break;
    case "retest-cancel": paintReadings(); break;
    case "retest-ok": confirmRetest(id); break;
    case "retest-submit": submitRetest(id); break;
  }
});

document.querySelector("#nav-readings").onclick = () => showView("readings");
document.querySelector("#nav-tasks").onclick = () => showView("tasks");

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
