"use strict";

const $ = (id) => document.getElementById(id);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const requestKey = () => `wise-${crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
const list = (value, key) => Array.isArray(value) ? value : Array.isArray(value?.[key]) ? value[key] : [];

const PROVIDER_LOGOS = Object.freeze({
  openai: "openai.svg", azure: "azure.svg", openrouter: "openrouter.svg", groq: "groq.svg",
  together: "together.svg", deepseek: "deepseek.svg", mistral: "mistral.svg", fireworks: "fireworks.svg",
  anyscale: "anyscale.svg", perplexity: "perplexity.svg", xai: "xai.svg", cohere: "cohere.svg",
  ollama: "ollama.svg", "lm-studio": "lmstudio.svg", vllm: "vllm.svg", anthropic: "anthropic.svg",
  gemini: "gemini.svg",
});

const copy = {
  ar: {
    messaging: "المراسلة",
    newChat: "محادثة جديدة", tasks: "المهام", schedules: "المهام المجدولة", integrations: "التكاملات",
    media: "الوسائط والمخرجات", conversations: "المحادثات", settings: "الإعدادات", work: "العمل",
    attention: "التنبيهات", outputs: "المخرجات", details: "التفاصيل", draft: "مسودة محلية",
    saved: "محادثة محفوظة", workspace: "مساحة WISE", chooseModel: "اختيار نموذج", send: "إرسال",
    prompt: "أرسل رسالة إلى WISE…", commandSearch: "ابحث عن أمر أو قسم…", commands: "أوامر WISE",
    connected: "متصل وجاهز", offline: "غير متصل", loading: "جارٍ الاتصال…",
  },
  en: {
    messaging: "Messaging",
    newChat: "New chat", tasks: "Tasks", schedules: "Scheduled", integrations: "Integrations",
    media: "Media & outputs", conversations: "Conversations", settings: "Settings", work: "Work",
    attention: "Attention", outputs: "Outputs", details: "Details", draft: "Local draft",
    saved: "Saved conversation", workspace: "WISE workspace", chooseModel: "Choose model", send: "Send",
    prompt: "Message WISE…", commandSearch: "Search commands or sections…", commands: "WISE commands",
    connected: "Connected and ready", offline: "Offline", loading: "Connecting…",
  },
};
const language = () => (state?.settings?.ui_language || document.documentElement.lang || "ar").toLowerCase().startsWith("en") ? "en" : "ar";
const t = (key) => copy[language()]?.[key] || copy.ar[key] || key;
const L = (ar, en) => language() === "ar" ? ar : en;

const state = {
  sessions: [], currentSession: null, currentSessionTitle: "", sessionsQuery: "",
  tasks: [], activeTask: null, schedules: [], channels: [], providers: [], localModels: [],
  pending: [], previews: [], voice: {}, telemetry: {}, healthDetails: {}, settings: {}, system: {}, resources: {},
  mcp: [], knowledge: [], attachments: [], selectedModel: null, currentView: "chat",
  providerModels: {}, providerModelStatus: {}, modelQuery: "",
  modelMenuProviderId: null, settingsProviderId: null, settingsProviderModels: [], settingsProviderResult: null, settingsProviderDraft: null,
  settingsLocalModelId: null, settingsLocalModelResult: null, settingsPreviousTab: "general",
  settingsRenderVersion: 0,
  taskPlanExpanded: false, sessionMenuTarget: null, settingsTab: "general", sending: false,
  chatMode: "normal", selectedMcp: null, slashCommands: [], slashIndex: 0,
};

function applyLanguage(nextLanguage = language()) {
  const lang = nextLanguage === "en" ? "en" : "ar";
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
  const labels = [
    [".nav-item[data-view='chat'] span", "newChat"], [".nav-item[data-view='tasks'] span", "tasks"],
    [".nav-item[data-view='schedules'] span", "schedules"],
    [".nav-item[data-view='messaging'] span", "messaging"],
    [".nav-item[data-view='media'] span", "media"], [".section-heading > span", "conversations"],
    ["#settingsTitle", "settings"], ["#commandTitle", "commands"],
  ];
  labels.forEach(([selector, key]) => { const node = document.querySelector(selector); if (node) node.textContent = t(key); });
  if ($("prompt")) $("prompt").placeholder = t("prompt");
  if ($("sendButton")) $("sendButton").setAttribute("aria-label", t("send"));
  if ($("commandInput")) $("commandInput").placeholder = t("commandSearch");
  if ($("modelLabel") && !state.selectedModel) $("modelLabel").textContent = t("chooseModel");
}

function updateWindowState() {
  const restored = window.outerWidth < screen.availWidth - 24 || window.outerHeight < screen.availHeight - 24;
  document.documentElement.dataset.wiseWindow = restored ? "restored" : "maximized";
}

function isHiddenLegacyProvider(item) {
  const id = String(item?.id || item?.provider_id || "").toLowerCase();
  return id === "opencode_zen" || id === "opencode_go";
}

async function api(path, options = {}) {
  const headers = { ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.error || `فشل الطلب (${response.status})`);
  return body;
}

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("toastStack").append(node);
  setTimeout(() => node.remove(), 4200);
}

function formatTime(value) {
  if (!value) return "الآن";
  const date = new Date(Number(value) > 10_000_000_000 ? Number(value) : Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(document.documentElement.lang === "ar" ? "ar-IQ" : "en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function sessionId(row) { return row?.session_id || row?.id; }
function sessionTitle(row) { return row?.title || row?.metadata?.title || row?.last_query || "محادثة جديدة"; }
function statusOf(row) { return String(row?.status || row?.state || row?.task_status || "غير معروف").toLowerCase(); }
function isActiveStatus(value) { return ["active", "running", "in_progress", "processing", "waiting_human", "paused"].includes(String(value).toLowerCase()); }

function setCoreStatus(ok, detail = "") {
  const button = $("coreStatus");
  button.classList.toggle("online", ok);
  button.classList.toggle("offline", !ok);
  button.querySelector("small").textContent = detail || (ok ? t("connected") : t("offline"));
}

function blankConversation({ focus = true } = {}) {
  saveDraft();
  state.currentSession = null;
  state.currentSessionTitle = "";
  $("pageTitle").textContent = t("newChat");
  $("pageSubtitle").textContent = t("draft");
  $("messageList").replaceChildren();
  $("welcome").hidden = false;
  $("chatView").classList.add("empty-chat");
  $("prompt").value = localStorage.getItem("wise:draft:new") || "";
  autoGrow();
  syncComposerState();
  showView("chat");
  renderSessions();
  renderTaskPlan();
  if (focus) $("prompt").focus();
}

function saveDraft() {
  const key = state.currentSession ? `wise:draft:${state.currentSession}` : "wise:draft:new";
  const value = $("prompt")?.value || "";
  if (value) localStorage.setItem(key, value); else localStorage.removeItem(key);
}

async function loadSessions() {
  try {
    const suffix = state.sessionsQuery ? `&query=${encodeURIComponent(state.sessionsQuery)}` : "";
    state.sessions = await api(`/api/v2/sessions?limit=150${suffix}`);
    renderSessions();
  } catch (error) {
    $("sessionList").innerHTML = `<div class="empty-list">تعذر تحميل المحادثات.</div>`;
  }
}

function renderSessions() {
  const host = $("sessionList");
  host.replaceChildren();
  if (!state.sessions.length) {
    host.innerHTML = `<div class="empty-list">لا توجد محادثات محفوظة بعد.<br>تُحفظ أول محادثة عند إرسال رسالة.</div>`;
    return;
  }
  state.sessions.forEach((row) => {
    const id = sessionId(row);
    const button = document.createElement("button");
    button.className = `session-row${id === state.currentSession ? " active" : ""}`;
    button.dataset.sessionId = id;
    button.setAttribute("role", "listitem");
    button.innerHTML = `<span class="session-copy"><b>${escapeHtml(sessionTitle(row))}</b><small>${escapeHtml(formatTime(row.updated_at || row.created_at))}</small></span><span class="session-more" role="button" tabindex="0" aria-label="خيارات المحادثة">•••</span>`;
    button.addEventListener("click", (event) => {
      if (event.target.closest(".session-more")) return openSessionMenu(event, row);
      openSession(id, sessionTitle(row));
    });
    button.querySelector(".session-more").addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) openSessionMenu(event, row); });
    host.append(button);
  });
}

async function openSession(id, title) {
  if (state.sending) return;
  saveDraft();
  state.currentSession = id;
  state.currentSessionTitle = title;
  $("pageTitle").textContent = title;
  $("pageSubtitle").textContent = "محادثة محفوظة";
  $("welcome").hidden = true;
  $("chatView").classList.remove("empty-chat");
  $("messageList").innerHTML = `<div class="thinking-row"><span class="thinking-dots"><i></i><i></i><i></i></span>تحميل المحادثة…</div>`;
  $("prompt").value = localStorage.getItem(`wise:draft:${id}`) || "";
  autoGrow();
  syncComposerState();
  showView("chat");
  renderSessions();
  try {
    const history = await api(`/api/v2/sessions/${encodeURIComponent(id)}/history?limit=500`);
    $("messageList").replaceChildren();
    history.forEach((item) => addMessage(item.role === "user" ? "user" : "assistant", item.content || item.text || "", { time: item.timestamp, model: item.model_name || item.metadata?.model_name, silent: true }));
    scrollChat();
  } catch (error) {
    $("messageList").innerHTML = `<div class="empty-state"><div>${icon("alert")}<h3>تعذر تحميل المحادثة</h3><p>${escapeHtml(error.message)}</p></div></div>`;
  }
}

function addMessage(role, text, options = {}) {
  $("welcome").hidden = true;
  $("chatView").classList.remove("empty-chat");
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const name = role === "user" ? L("أنت", "You") : options.model || L("النموذج غير مسجّل", "Model not recorded");
  article.innerHTML = `<div class="message-body"><div class="message-head"><b>${escapeHtml(name)}</b><time>${escapeHtml(formatTime(options.time || Date.now()))}</time></div><div class="message-text"></div>${options.note ? `<div class="message-meta">${escapeHtml(options.note)}</div>` : ""}</div>`;
  const content = article.querySelector(".message-text");
  if(role === "user") content.textContent = text;
  else renderMessageLinks(content,String(text));
  article.querySelector(".message-text").dir = "auto";
  $("messageList").append(article);
  if (!options.silent) scrollChat();
  return article;
}

function renderMessageLinks(host,text) {
  // No HTML parser or innerHTML: untrusted model text cannot become script.
  const links = /\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)<>]{1,2048})\)/g;
  let offset = 0;
  for(const match of text.matchAll(links)) {
    let url; try {url = new URL(match[2]);} catch(_) {continue;}
    if(url.username || url.password) continue;
    host.append(document.createTextNode(text.slice(offset,match.index)));
    const link = document.createElement("a"); link.href = url.href; link.textContent = match[1];
    link.target = "_blank"; link.rel = "noopener noreferrer";
    link.onclick = async event => {
      if(window.pywebview?.api?.open_external_url) {
        event.preventDefault();
        const result = await window.pywebview.api.open_external_url(url.href);
        if(result !== true && result?.ok !== true) toast(L("تعذر فتح الرابط.","Could not open link."),"error");
      }
    };
    host.append(link); offset = match.index + match[0].length;
  }
  host.append(document.createTextNode(text.slice(offset)));
}

function addToolActivity(milestones = []) {
  const actual = milestones.filter((item) => item.stage === "Tools");
  if (!actual.length) return;
  const details = document.createElement("details");
  details.className = "tool-card";
  details.innerHTML = `<summary>${icon("chevron")}<span>${L("تفاصيل تنفيذ المهمة", "Execution details")}</span></summary><div class="tool-content"></div>`;
  details.querySelector(".tool-content").textContent = actual.map((item) => `${item.title}${item.status ? ` — ${item.status}` : ""}`).join("\n");
  $("messageList").append(details);
}

function activityLabel(event) {
  const tool = String(event?.title || "").toLowerCase();
  if (event?.stage === "Tools") {
    if (/shell|terminal|execute_python|code_execution/.test(tool)) return ["terminal", L("الطرفية", "Terminal")];
    if (/write_file|edit|patch/.test(tool)) return ["edit", L("تعديل", "Edit")];
    if (/search|fetch|query-docs|resolve-library/.test(tool)) return ["search", L("بحث", "Search")];
    if (tool === "read_skill") return ["file", L("قراءة المهارة", "Read skill")];
    if (tool.startsWith("mcp.")) return ["link", "MCP"];
    return ["file", L("قراءة", "Read")];
  }
  return ["spark", L("يفكّر", "Thinking")];
}

function updateLiveActivity(event) {
  const host = $("thinking");
  if (!host) return;
  const [glyph, label] = activityLabel(event);
  host.querySelector(".activity-current").innerHTML = `${icon(glyph)}<span>${escapeHtml(label)}</span>`;
  // Only real tool executions get a collapsible log; recommendations and
  // private model reasoning are not presented as performed work.
  if (event.stage === "Tools") {
    // The first real action should reveal its task controls immediately,
    // rather than waiting for the background twenty-second refresh.
    scheduleTaskRefresh();
    let details = host.querySelector("details");
    if (!details) {
      details = document.createElement("details");
      details.className = "live-activity-details";
      details.innerHTML = `<summary>${icon("chevron")} ${L("عرض التنفيذ", "Show activity")}</summary><div class="live-activity-log"></div>`;
      host.append(details);
    }
    const row = document.createElement("div");
    row.textContent = `${event.title} — ${event.status}`;
    details.querySelector(".live-activity-log").append(row);
  }
  scrollChat();
}

let taskRefreshTimer = null;
function scheduleTaskRefresh() {
  if (taskRefreshTimer !== null) return;
  taskRefreshTimer = setTimeout(() => {
    taskRefreshTimer = null;
    loadTasks().catch(() => {});
  }, 150);
}

function setSending(value) {
  state.sending = value;
  $("prompt").disabled = value;
  syncComposerState();
  $("thinking")?.remove();
  if (value) {
    const node = document.createElement("div");
    node.id = "thinking";
    node.className = "thinking-row";
    node.innerHTML = `<div class="activity-current">${icon("spark")}<span>${L("يفكّر", "Thinking")}</span></div>`;
    $("messageList").append(node);
    scrollChat();
  }
}

async function requestChatTurn(payload) {
  const response = await fetch("/api/v2/chat/events", {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": requestKey() },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  if (!response.body) throw new Error(L("تعذّر استقبال تحديثات الطلب", "Response stream unavailable"));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let separator;
      while ((separator = buffer.indexOf("\n\n")) >= 0) {
        const packet = buffer.slice(0, separator); buffer = buffer.slice(separator + 2);
        if (!packet.startsWith("data: ")) continue;
        const event = JSON.parse(packet.slice(6));
        if (event.type === "milestone") updateLiveActivity(event);
        if (event.type === "error") throw new Error(event.message);
        if (event.type === "done") return event.response;
      }
      if (done) throw new Error(L("انقطع الاتصال. تحقق من المحادثة قبل إعادة الإرسال.", "Connection interrupted. Check the conversation before resending."));
    }
  } finally { await reader.cancel().catch(() => {}); }
}

async function ensureSession() {
  if (state.currentSession) return state.currentSession;
  const created = await api("/api/v2/sessions/new", { method: "POST", body: "{}" });
  state.currentSession = sessionId(created);
  state.currentSessionTitle = "محادثة جديدة";
  $("pageSubtitle").textContent = "محادثة محفوظة";
  return state.currentSession;
}

async function sendMessage() {
  const text = $("prompt").value.trim();
  if (!text || state.sending) return;
  const attachmentNames = state.attachments.map((item) => item.name);
  $("prompt").value = "";
  localStorage.removeItem(state.currentSession ? `wise:draft:${state.currentSession}` : "wise:draft:new");
  autoGrow();
  syncComposerState();
  addMessage("user", text, { note: attachmentNames.length ? `مرفقات: ${attachmentNames.join("، ")}` : "" });
  setSending(true);
  try {
    const id = await ensureSession();
    const uploaded = state.attachments.length ? await uploadAttachments(state.attachments) : [];
    const response = await requestChatTurn({ message: text, session_id: id, modality: "chat", mode: state.chatMode, selected_mcp: state.selectedMcp, max_steps: 8, attachments: uploaded });
    setSending(false);
    if (response.session_id) state.currentSession = response.session_id;
    addToolActivity(response.milestones || []);
    addMessage("assistant", response.reply_text || response.answer || response.error || "اكتملت المهمة دون نص للعرض.", { model: response.model_name });
    state.attachments = [];
    renderAttachments();
    await Promise.allSettled([loadSessions(), loadTasks(), loadApprovals()]);
    const current = state.sessions.find((row) => sessionId(row) === state.currentSession);
    if (current) {
      state.currentSessionTitle = sessionTitle(current);
      $("pageTitle").textContent = state.currentSessionTitle;
    }
  } catch (error) {
    setSending(false);
    addMessage("assistant", `تعذر إكمال الطلب: ${error.message}`, { note: "خطأ قابل لإعادة المحاولة" });
    toast(error.message, "error");
  } finally {
    $("prompt").disabled = false;
    $("prompt").focus();
  }
}

async function uploadAttachments(files) {
  const results = [];
  for (const file of files) {
    if (file.size > 15 * 1024 * 1024) throw new Error(`الملف ${file.name} أكبر من 15MB`);
    const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1] || ""); reader.onerror = reject; reader.readAsDataURL(file); });
    results.push(await api("/api/v2/attachments", { method: "POST", body: JSON.stringify({ name: file.name, mime_type: file.type || "application/octet-stream", data_base64: data }) }));
  }
  return results;
}

function renderAttachments() {
  const host = $("attachmentList");
  host.hidden = !state.attachments.length;
  host.innerHTML = state.attachments.map((file, index) => `<span class="attachment-chip">${icon("file")}<span>${escapeHtml(file.name)}</span><button type="button" data-remove-attachment="${index}" aria-label="إزالة">×</button></span>`).join("");
}

async function loadTasks() {
  const scope = state.currentSession ? `?session_id=${encodeURIComponent(state.currentSession)}` : "";
  const [tasksResult, activeResult] = await Promise.allSettled([api("/api/v2/tasks?limit=100"), api(`/api/v2/tasks/active${scope}`)]);
  state.tasks = tasksResult.status === "fulfilled" ? list(tasksResult.value, "tasks") : [];
  state.activeTask = activeResult.status === "fulfilled" ? activeResult.value : null;
  const activeCount = state.tasks.filter((task) => isActiveStatus(statusOf(task))).length || (state.activeTask ? 1 : 0);
  if ($("taskCount")) { $("taskCount").hidden = !activeCount; $("taskCount").textContent = activeCount; }
  renderTasks();
  renderTaskPlan();
}

function renderTasks() {
  const host = $("tasksGrid");
  if (!host) return;
  if (!state.tasks.length) return host.innerHTML = emptyCard("tasks", "لا توجد مهام بعد", "عندما يحول WISE طلباً إلى مهمة متعددة الخطوات ستظهر هنا.");
  host.innerHTML = state.tasks.map((task) => {
    const status = statusOf(task), id = task.task_id || task.id || "";
    const statusClass = status.includes("complete") ? "success" : status.includes("fail") || status.includes("cancel") ? "danger" : isActiveStatus(status) ? "warning" : "";
    return `<article class="data-card"><header><h3>${escapeHtml(task.title || task.goal || task.query || `مهمة ${id}`)}</h3><span class="badge ${statusClass}">${escapeHtml(status)}</span></header><p>${escapeHtml(task.description || task.criteria || task.last_message || "مهمة يديرها محرك WISE")}</p><footer><span>${escapeHtml(formatTime(task.updated_at || task.created_at))}</span>${isActiveStatus(status) ? `<span class="inline-actions"><button class="secondary-button" data-task-control="pause" data-task-id="${escapeHtml(id)}">إيقاف مؤقت</button><button class="danger-button" data-task-control="cancel" data-task-id="${escapeHtml(id)}">إلغاء</button></span>` : ""}</footer></article>`;
  }).join("");
}

async function controlTask(id, action) {
  try {
    const result = await api(`/api/v2/tasks/${encodeURIComponent(id)}/control`, { method: "POST", body: JSON.stringify({ action }) });
    if (result.ok === false) throw new Error(result.message || "تعذر التحكم بالمهمة");
    toast(action === "cancel" ? "أُلغيت المهمة" : "تحدّثت حالة المهمة", "success");
    await loadTasks();
  } catch (error) { toast(error.message, "error"); }
}

async function loadSchedules() {
  try { state.schedules = list(await api("/admin/schedules"), "schedules"); } catch { state.schedules = []; }
  const host = $("schedulesGrid");
  if (!state.schedules.length) return host.innerHTML = emptyCard("clock", "لا توجد مهام مجدولة", "أنشئ مهمة متكررة لمتابعة شيء أو تنفيذ عمل في موعد محدد.");
  host.innerHTML = state.schedules.map((item) => `<article class="data-card"><header><h3>${escapeHtml(item.name || "مهمة مجدولة")}</h3><span class="badge ${item.enabled === false ? "" : "success"}">${item.enabled === false ? "متوقفة" : "فعالة"}</span></header><p>${escapeHtml(item.target || item.prompt || "")}</p><footer><span dir="ltr">${escapeHtml(item.cron || item.rrule || "")}</span><button class="danger-button" data-delete-schedule="${escapeHtml(item.id || item.schedule_id)}">حذف</button></footer></article>`).join("");
}

async function createSchedule() {
  const name = prompt("اسم المهمة المجدولة:"); if (!name) return;
  const target = prompt("ماذا تريد من WISE أن يفعل؟"); if (!target) return;
  const cron = prompt("تعبير Cron (مثال: 0 9 * * *):", "0 9 * * *"); if (!cron) return;
  try { await api("/admin/schedules", { method: "POST", body: JSON.stringify({ name, target, cron, tz: Intl.DateTimeFormat().resolvedOptions().timeZone, enabled: true }) }); toast("أُنشئت المهمة المجدولة", "success"); await loadSchedules(); } catch (error) { toast(error.message, "error"); }
}

async function loadIntegrations() {
  const results = await Promise.allSettled([api("/admin/channels"), api("/admin/mcp/servers"), api("/api/v2/models/external")]);
  state.channels = results[0].status === "fulfilled" ? list(results[0].value, "configured") : [];
  state.mcpCatalog = list(await api("/admin/mcp/catalog").catch(() => ({entries: []})), "entries");
  state.mcp = results[1].status === "fulfilled" ? list(results[1].value, "servers") : [];
  if (results[2].status === "fulfilled") state.providers = list(results[2].value, "providers").filter((item) => !isHiddenLegacyProvider(item));
  renderIntegrations();
}

function renderIntegrations() {
  const host = $("integrationsGrid");
  if (!host) return;
  const rows = [
    ...state.providers.map((item) => ({ name: item.name || item.id, detail: item.configured_model || item.default_model || "مزود نموذج", enabled: item.is_configured, type: "Model" })),
    ...state.channels.map((item) => ({ name: item.name || item.id || item.channel, detail: item.type || "قناة مراسلة", enabled: item.enabled !== false, type: "Channel" })),
    ...state.mcp.map((item) => ({ name: item.name || item.id, detail: `${item.tools_count || item.tool_count || 0} أدوات`, enabled: Boolean(item.alive), type: "MCP" })),
  ];
  host.innerHTML = rows.length ? rows.map((item) => `<article class="data-card"><header><h3>${escapeHtml(item.name)}</h3><span class="badge ${item.enabled ? "success" : ""}">${item.enabled ? "متصل" : "غير متصل"}</span></header><p>${escapeHtml(item.detail)}</p><footer><span>${escapeHtml(item.type)}</span><button class="secondary-button" data-open-settings="integrations">إدارة</button></footer></article>`).join("") : emptyCard("link", "لا توجد تكاملات متصلة", "اربط مزود نموذج، قناة مراسلة أو خادم MCP من الإعدادات.");
}

async function loadMedia() {
  try { state.previews = list(await api("/admin/preview/list"), "previews"); } catch { state.previews = []; }
  $("mediaGrid").innerHTML = state.previews.length ? state.previews.map((item) => `<article class="data-card"><header><h3>${escapeHtml(item.name || item.path || "معاينة")}</h3><span class="badge success">نشطة</span></header><p>${escapeHtml(item.url || item.path || "")}</p><footer><span>Preview</span>${item.url ? `<a class="secondary-button" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">فتح</a>` : ""}</footer></article>`).join("") : emptyCard("media", "لا توجد مخرجات بعد", "الملفات والمعاينات والوسائط التي ينشئها WISE ستظهر هنا.");
}

async function loadApprovals() {
  try { state.pending = list(await api("/admin/mcp/pending"), "pending"); } catch { state.pending = []; }
  renderTaskPlan();
}

async function resolveApproval(id, approved) {
  try { const result = await api(`/admin/mcp/confirm/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ approved, reason: approved ? "Approved in WISE interface" : "Rejected in WISE interface" }) }); if (!result.resolved) throw new Error(result.error || "تعذر حسم الموافقة"); toast(approved ? "تمت الموافقة" : "تم الرفض", "success"); await loadApprovals(); } catch (error) { toast(error.message, "error"); }
}

function renderTaskPlan() {
  const dock = $("taskPlanDock");
  if (!dock) return;
  const task = state.activeTask;
  const taskSession = task?.session_id || task?.sessionId || task?.context_variables?.session_id;
  const visibleTask = task && (!taskSession || taskSession === state.currentSession) ? task : null;
  const approvals = state.pending || [];
  dock.hidden = !visibleTask && !approvals.length;
  if (dock.hidden) {
    document.documentElement.style.setProperty("--composer-clearance", "174px");
    return;
  }

  const rawSteps = visibleTask?.milestones || visibleTask?.steps || visibleTask?.plan || [];
  const steps = rawSteps.length ? rawSteps : visibleTask ? [{ title: visibleTask.title || visibleTask.semantic_goal || visibleTask.goal || visibleTask.query || L("مهمة WISE", "WISE task"), status: statusOf(visibleTask) }] : [];
  const completed = steps.filter((step) => /complete|completed|done|success/i.test(String(step?.status || ""))).length;
  $("taskPlanTitle").textContent = visibleTask?.title || visibleTask?.semantic_goal || visibleTask?.goal || visibleTask?.query || L("موافقة مطلوبة", "Approval required");
  $("taskPlanMeta").textContent = approvals.length
    ? L(`${approvals.length} بانتظار قرارك`, `${approvals.length} awaiting your decision`)
    : L(`${completed} من ${steps.length} مكتملة`, `${completed} of ${steps.length} complete`);
  $("taskPlanList").innerHTML = steps.map((step, index) => {
    const status = String(step?.status || "");
    const done = /complete|completed|done|success/i.test(status);
    const active = !done && /active|running|progress|processing|current/i.test(status);
    return `<div class="task-plan-step ${done ? "done" : active ? "active" : ""}"><span class="task-step-index">${done ? icon("check") : index + 1}</span><span><b>${escapeHtml(step?.title || step?.name || step?.stage || L("خطوة", "Step"))}</b>${status ? `<small>${escapeHtml(status)}</small>` : ""}</span></div>`;
  }).join("");
  const approvalsHost = $("taskPlanApprovals");
  approvalsHost.hidden = !approvals.length;
  approvalsHost.innerHTML = approvals.map((item) => {
    const id = item.call_id || item.id;
    return `<article class="task-plan-approval"><span>${icon("alert")}<b>${escapeHtml(item.tool || item.name || item.description || L("إجراء حساس", "Sensitive action"))}</b></span><div class="inline-actions"><button class="primary-button" data-approval="approve" data-call-id="${escapeHtml(id)}">${L("موافقة", "Approve")}</button><button class="secondary-button" data-approval="reject" data-call-id="${escapeHtml(id)}">${L("رفض", "Reject")}</button></div></article>`;
  }).join("");
  $("taskPlanToggle").setAttribute("aria-expanded", String(state.taskPlanExpanded));
  $("taskPlanBody").hidden = !state.taskPlanExpanded;
  dock.classList.toggle("expanded", state.taskPlanExpanded);
  document.documentElement.style.setProperty("--composer-clearance", state.taskPlanExpanded ? "430px" : "218px");
}

function emptyCard(iconName, title, detail) { return `<div class="empty-state"><div>${icon(iconName)}<h3>${escapeHtml(title)}</h3><p>${escapeHtml(detail)}</p></div></div>`; }

async function loadRuntime() {
  const results = await Promise.allSettled([api("/health"), api("/api/v2/system/telemetry"), api("/api/v2/voice/status"), api("/api/v2/settings"), api("/api/v2/system-integration"), api("/health/detailed")]);
  const healthy = results[0].status === "fulfilled";
  state.telemetry = results[1].status === "fulfilled" ? results[1].value : {};
  state.voice = results[2].status === "fulfilled" ? results[2].value : {};
  state.settings = results[3].status === "fulfilled" ? results[3].value : {};
  state.system = results[4].status === "fulfilled" ? results[4].value : {};
  state.healthDetails = results[5].status === "fulfilled" ? results[5].value : {};
  applyLanguage(state.settings.ui_language || document.documentElement.lang);
  setCoreStatus(healthy, healthy ? `${state.telemetry.cpu_percent ?? 0}% CPU · ${state.telemetry.ram_used_gb ?? 0} GB` : (language() === "ar" ? "الخدمة غير متاحة" : "Service unavailable"));
  $("voiceButton").classList.toggle("active", Boolean(state.voice.is_running));
  renderTaskPlan();
}

function providerIdOf(item) {
  const explicit = String(item?.id || item?.provider_id || "").toLowerCase();
  if (explicit) return explicit;
  const source = String(item?.provider || "").toLowerCase();
  return Object.keys(PROVIDER_LOGOS).find((id) => source.includes(id)) || (source.includes("google") ? "gemini" : source.includes("claude") ? "anthropic" : source.includes("محلي") ? "local" : "custom");
}

function providerMark(item, extraClass = "") {
  const id = providerIdOf(item);
  const logo = PROVIDER_LOGOS[id];
  const classes = `provider-mark ${extraClass}`.trim();
  if (logo) return `<span class="${classes}" data-provider="${escapeHtml(id)}" aria-hidden="true"><img src="./assets/providers/${logo}" alt=""></span>`;
  return `<span class="${classes}" data-provider="${escapeHtml(id || "custom")}" aria-hidden="true">${icon(id === "local" ? "inspector" : "spark")}</span>`;
}

function syncSelectedModel() {
  const selected = state.selectedModel;
  $("modelLabel").textContent = selected?.model || t("chooseModel");
  const mark = $("modelProviderMark");
  const id = selected ? providerIdOf(selected) : "custom";
  const logo = selected ? PROVIDER_LOGOS[id] : null;
  mark.dataset.provider = id;
  mark.innerHTML = logo ? `<img src="./assets/providers/${logo}" alt="">` : icon(selected?.kind === "local" ? "inspector" : "spark");
  mark.title = selected?.provider || t("chooseModel");
}

async function loadModels() {
  const results = await Promise.allSettled([api("/api/v2/models/external"), api("/api/v2/models/local")]);
  state.providers = results[0].status === "fulfilled" ? list(results[0].value, "providers").filter((item) => !isHiddenLegacyProvider(item)) : [];
  state.localModels = results[1].status === "fulfilled" ? list(results[1].value, "models") : [];
  const entries = [
    ...state.providers.filter((item) => item.is_configured).map((item) => ({ kind: "external", id: item.id || item.provider_id, provider: item.name || item.id, model: item.configured_model || item.default_model || "افتراضي", active: Boolean(item.is_active) })),
    ...state.localModels.map((item) => ({ kind: "local", id: item.id || item.model_id || item.name, provider: "محلي", model: item.name || item.id, active: Boolean(item.is_active) })),
  ];
  const active = entries.find((item) => item.active) || entries[0] || null;
  state.selectedModel = active ? { ...active, label: `${active.provider} · ${active.model}` } : null;
  syncSelectedModel();
  renderModelMenu();
}

function providerCanFetchWithoutKey(provider) {
  return ["ollama", "lm-studio", "vllm", "litellm-proxy"].includes(providerIdOf(provider));
}

async function fetchProviderModels(provider, { force = false, apiKey = null, baseUrl = null } = {}) {
  const id = providerIdOf(provider);
  if (!force && state.providerModels[id]?.length) return state.providerModels[id];
  state.providerModelStatus[id] = "loading";
  renderModelMenu();
  try {
    const useDraft = apiKey !== null || baseUrl !== null;
    const result = useDraft
      ? await api("/api/v2/models/external/fetch_models", { method: "POST", body: JSON.stringify({ provider_id: id, api_key: apiKey || null, base_url: baseUrl || null }) })
      : await api(`/api/v2/models/external/fetch_models?provider_id=${encodeURIComponent(id)}`);
    if (result.ok === false) throw new Error(result.message || L("تعذر جلب النماذج", "Could not load models"));
    state.providerModels[id] = [...new Set(list(result, "models"))];
    state.providerModelStatus[id] = "ready";
    renderModelMenu();
    return state.providerModels[id];
  } catch (error) {
    state.providerModelStatus[id] = "error";
    renderModelMenu();
    throw error;
  }
}

async function hydrateModelCatalog() {
  const eligible = state.providers.filter((provider) => provider.is_configured || providerCanFetchWithoutKey(provider));
  await Promise.allSettled(eligible.map((provider) => fetchProviderModels(provider)));
}

function modelMatchesQuery(provider, model) {
  const query = state.modelQuery.trim().toLocaleLowerCase();
  return !query || `${provider.name || provider.id} ${model}`.toLocaleLowerCase().includes(query);
}

function menuProviderRows() {
  const query = state.modelQuery.trim().toLocaleLowerCase();
  const providers = state.providers.filter((provider) => {
    const id = providerIdOf(provider);
    const label = `${provider.name || ""} ${provider.id || provider.provider_id || ""}`.toLocaleLowerCase();
    const models = state.providerModels[id] || [provider.configured_model || provider.default_model || ""];
    return !query || label.includes(query) || models.some((model) => String(model).toLocaleLowerCase().includes(query));
  });
  const rows = providers.map((provider) => {
    const id = providerIdOf(provider);
    const configured = Boolean(provider.is_configured || providerCanFetchWithoutKey(provider));
    const detail = provider.configured_model || (configured ? L("اختر نموذجاً", "Choose a model") : L("يحتاج إلى ربط", "Needs setup"));
    const expanded = state.modelMenuProviderId === id || Boolean(query && provider.is_configured);
    const body = expanded ? menuProviderDetail(id)?.body || "" : "";
    return `<section class="model-provider-group ${expanded ? "expanded" : ""}"><button class="model-provider-row" data-open-model-provider="${escapeHtml(id)}" role="menuitem" aria-expanded="${expanded}"><span>${providerMark(provider, "model-icon")}</span><span class="model-provider-copy"><b>${escapeHtml(provider.name || id)}</b><small>${escapeHtml(detail)}</small></span><span class="model-provider-arrow">${icon("chevron")}</span></button>${expanded ? `<div class="model-inline-options">${body}</div>` : ""}</section>`;
  }).join("");
  const localExpanded = state.modelMenuProviderId === "local";
  const local = state.localModels.length ? `<section class="model-provider-group ${localExpanded ? "expanded" : ""}"><button class="model-provider-row" data-open-model-provider="local" role="menuitem" aria-expanded="${localExpanded}"><span>${providerMark({ id: "local" }, "model-icon")}</span><span class="model-provider-copy"><b>${L("النماذج المحلية", "Local models")}</b><small>${state.localModels.length} ${L("نموذج", "models")}</small></span><span class="model-provider-arrow">${icon("chevron")}</span></button>${localExpanded ? `<div class="model-inline-options">${menuProviderDetail("local")?.body || ""}</div>` : ""}</section>` : "";
  return rows || local ? `${rows}${local}` : `<div class="model-group-state">${L("لا توجد مزودات مطابقة.", "No matching providers.")}</div>`;
}

function menuProviderDetail(providerId) {
  if (providerId === "local") {
    const models = state.localModels.filter((item) => modelMatchesQuery({ name: L("محلي", "Local") }, item.name || item.id));
    return {
      title: L("النماذج المحلية", "Local models"),
      subtitle: L("هذا الجهاز", "This device"),
      logo: providerMark({ id: "local" }, "model-icon"),
      body: models.map((item) => { const id = item.id || item.model_id || item.name; const model = item.name || item.id; const active = Boolean(item.is_active); return `<button class="model-option ${active ? "active" : ""}" role="menuitem" data-model-kind="local" data-model-id="${escapeHtml(id)}" data-model-name="${escapeHtml(model)}"><span><b>${escapeHtml(model)}</b></span>${active ? icon("check") : ""}</button>`; }).join("") || `<div class="model-group-state">${L("لا توجد نماذج مطابقة.", "No matching models.")}</div>`,
    };
  }
  const provider = state.providers.find((item) => providerIdOf(item) === providerId);
  if (!provider) return null;
  const status = state.providerModelStatus[providerId];
  const configuredModel = provider.configured_model || provider.default_model;
  const models = state.providerModels[providerId]?.length ? state.providerModels[providerId] : (provider.is_configured && configuredModel ? [configuredModel] : []);
  const visibleModels = models.filter((model) => modelMatchesQuery(provider, model));
  let body = "";
  if (status === "loading") body = `<div class="model-group-state"><span class="mini-spinner"></span>${L("جلب النماذج…", "Loading models…")}</div>`;
  else if (!provider.is_configured && !providerCanFetchWithoutKey(provider)) body = `<button class="model-setup" data-configure-provider="${escapeHtml(providerId)}">${L("إعداد هذا المزود", "Set up this provider")}</button>`;
  else if (!visibleModels.length) body = `<div class="model-group-state">${status === "error" ? L("تعذر جلب القائمة؛ افتح الإعدادات للتحقق.", "Could not load models; open settings to check.") : L("لا توجد نماذج مطابقة.", "No matching models.")}</div>`;
  else body = visibleModels.map((model) => {
    const active = state.selectedModel?.kind === "external" && state.selectedModel.id === providerId && state.selectedModel.model === model;
    return `<button class="model-option ${active ? "active" : ""}" role="menuitem" data-model-kind="external" data-model-id="${escapeHtml(providerId)}" data-model-name="${escapeHtml(model)}"><span><b>${escapeHtml(model)}</b></span>${active ? icon("check") : ""}</button>`;
  }).join("");
  return { title: provider.name || providerId, subtitle: provider.is_configured ? L("نماذج متاحة", "Available models") : L("إعداد المزود", "Provider setup"), logo: providerMark(provider, "model-icon"), body };
}

function renderModelMenu() {
  const host = $("modelMenu");
  const existingSearch = $("modelSearch");
  if (existingSearch && host.contains(existingSearch)) {
    // Preserve focused input and IME composition through keystrokes/catalog updates.
    if (existingSearch.value !== state.modelQuery) existingSearch.value = state.modelQuery;
    host.querySelector(".model-menu-scroll").innerHTML = menuProviderRows();
    return;
  }
  host.innerHTML = `<div class="model-menu-head"><span class="model-menu-heading"><span><b>${L("النماذج", "Models")}</b><small>${L("المزوّد ثم النموذج", "Provider, then model")}</small></span></span><span class="model-menu-utilities"><button class="quiet-icon" data-open-settings="models" title="${L("إدارة المزودين", "Manage providers")}" aria-label="${L("إدارة مزودي النماذج", "Manage model providers")}">${icon("settings")}</button></span></div><label class="model-search">${icon("search")}<input id="modelSearch" type="search" value="${escapeHtml(state.modelQuery)}" placeholder="${L("ابحث عن مزود أو نموذج…", "Search providers or models…")}" aria-label="${L("البحث في النماذج", "Search models")}" autocomplete="off"></label><div class="model-menu-scroll model-provider-list">${menuProviderRows()}</div>`;
}

async function selectModel(kind, id, model) {
  try {
    const result = kind === "local" ? await api(`/api/v2/models/local/${encodeURIComponent(id)}/activate`, { method: "POST" }) : await api("/api/v2/models/external", { method: "POST", body: JSON.stringify({ provider_id: id, model, set_as_active: true }) });
    if (result.ok === false) throw new Error(result.message || result.error || "تعذر اختيار النموذج");
    $("modelMenu").hidden = true;
    toast("تم تغيير النموذج", "success");
    await Promise.all([loadModels(), loadRuntime()]);
    if (!$("settingsModal").hidden && state.settingsTab === "models") await renderSettings();
  } catch (error) { toast(error.message, "error"); }
}

async function toggleVoice() {
  try { const start = !state.voice.is_running; const result = await api(start ? "/api/v2/voice/start" : "/api/v2/voice/stop", { method: "POST", body: "{}" }); if (result.ok === false) throw new Error(result.message || "تعذر تشغيل الصوت"); toast(start ? "بدأ وضع الصوت" : "توقف وضع الصوت", "success"); await loadRuntime(); } catch (error) { toast(error.message, "error"); }
}

function showView(name) {
  state.currentView = name;
  qsa("[data-view-panel]").forEach((panel) => { const active = panel.dataset.viewPanel === name; panel.hidden = !active; panel.classList.toggle("active", active); });
  qsa(".nav-item[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  const titles = { chat: state.currentSessionTitle || t("newChat"), tasks: t("tasks"), schedules: t("schedules"), media: t("media"), messaging:t("messaging") };
  $("pageTitle").textContent = titles[name];
  $("pageSubtitle").textContent = name === "chat" ? (state.currentSession ? t("saved") : t("draft")) : t("workspace");
  if (name === "tasks") loadTasks();
  if (name === "schedules") loadSchedules();
  if (name === "media") loadMedia();
  if (name === "messaging") { $("messagingTitle").textContent = t("messaging"); loadMessaging(); }
  closeDrawers();
}

const settingsTabs = [
  ["general", "settings", "عام", "General", "workspace"],
  ["models", "spark", "النماذج والمزودون", "Models & providers", "workspace"],
  ["voice", "mic", "الصوت", "Voice", "workspace"],
  ["computer", "inspector", "الكمبيوتر", "Computer", "system"],
  ["integrations", "link", "التكاملات", "Integrations", "system"],
  ["knowledge", "file", "المعرفة والمهارات", "Knowledge & skills", "system"],
  ["performance", "tasks", "الأداء والتوكنز", "Performance & tokens", "system"],
  ["health", "check", "الصحة والتشخيص", "Health & diagnostics", "system"],
];

const settingsDescriptions = {
  general: ["اللغة والسلوك وحدود الاستقلالية.", "Language, behavior, and autonomy boundaries."],
  models: ["اربط مزوداً، اختبره، واختر النموذج الفعلي المستخدم.", "Connect a provider, verify it, and choose the model WISE actually uses."],
  voice: ["الاستماع والنطق والمقاطعة في المحادثة الصوتية.", "Listening, speech, and interruption behavior for voice conversations."],
  computer: ["تكامل Windows والصلاحيات الاختيارية والعمليات الإدارية.", "Optional Windows integration, permissions, and administrative operations."],
  integrations: ["خوادم MCP والاتصالات الخارجية. المراسلة في القائمة الرئيسية.", "MCP servers and external connections. Messaging is in the main sidebar."],
  knowledge: ["مصادر المعرفة والمهارات التي يستطيع WISE استخدامها.", "Knowledge sources and skills available to WISE."],
  performance: ["استهلاك السياق والذاكرة وإعدادات التشغيل.", "Context, memory, and runtime efficiency controls."],
  health: ["حالة الخدمات والموارد والتشخيص المباشر.", "Live service, resource, and diagnostic status."],
};

async function openSettings(tab = "general") {
  if (tab !== "models") { state.settingsProviderId = null; state.settingsProviderDraft = null; state.settingsLocalModelId = null; }
  state.settingsPreviousTab = state.settingsTab;
  state.settingsTab = tab;
  $("settingsModal").hidden = false;
  $("settingsContent").setAttribute("aria-busy", "true");
  $("settingsContent").innerHTML = `<div class="skeleton-list"><i></i><i></i><i></i></div>`;
  document.body.style.overflow = "hidden";
  await loadRuntime().catch(() => undefined);
  await renderSettings();
}
function closeSettings() { $("settingsModal").hidden = true; document.body.style.overflow = ""; state.settingsProviderDraft = null; state.settingsLocalModelId = null; }

async function renderSettings() {
  const version = ++state.settingsRenderVersion;
  $("settingsNav").innerHTML = settingsNavTemplate();
  const tab = settingsTabs.find(([id]) => id === state.settingsTab);
  $("settingsHeading").textContent = tab ? L(tab[2], tab[3]) : t("settings");
  $("settingsDescription").textContent = L(...(settingsDescriptions[state.settingsTab] || settingsDescriptions.general));
  const host = $("settingsContent");
  host.setAttribute("aria-busy", "true");
  host.innerHTML = `<div class="skeleton-list"><i></i><i></i><i></i></div>`;
  try {
    if (state.settingsTab === "models") await loadModels();
    if (state.settingsTab === "integrations") await loadIntegrations();
    if (state.settingsTab === "knowledge") { const result = await api("/admin/knowledge/sources").catch(() => ({ sources: [] })); state.knowledge = list(result, "sources"); }
    if (state.settingsTab === "performance") state.resources = await api("/admin/resources").catch(() => ({}));
    if (version !== state.settingsRenderVersion) return;
    const oldIndex = settingsTabs.findIndex(([id]) => id === state.settingsPreviousTab);
    const newIndex = settingsTabs.findIndex(([id]) => id === state.settingsTab);
    const direction = newIndex >= oldIndex ? "forward" : "backward";
    host.innerHTML = `<div class="settings-page-enter ${direction}">${settingsTemplate(state.settingsTab)}</div>`;
    host.setAttribute("aria-busy", "false");
    state.settingsPreviousTab = state.settingsTab;
    host.scrollTop = 0;
    if (state.settingsTab === "knowledge") loadCapabilityBrowser();
  } catch (error) { if (version === state.settingsRenderVersion) { host.innerHTML = `<div class="notice">${escapeHtml(error.message)}</div>`; host.setAttribute("aria-busy", "false"); } }
}

function settingsNavTemplate() {
  const groups = [
    ["workspace", L("مساحة العمل", "Workspace")],
    ["system", L("النظام", "System")],
  ];
  return groups.map(([groupId, label]) => {
    const rows = settingsTabs.filter(([, , , , group]) => group === groupId);
    return `<section class="settings-nav-group" aria-label="${escapeHtml(label)}"><span>${escapeHtml(label)}</span>${rows.map(([id, iconName, ar, en]) => `<button class="${state.settingsTab === id ? "active" : ""}" data-settings-tab="${id}" ${state.settingsTab === id ? 'aria-current="page"' : ""}>${icon(iconName)}<b>${L(ar, en)}</b></button>`).join("")}</section>`;
  }).join("");
}

function settingsTemplate(tab) {
  const cfg = state.settings || {}, sys = state.system || {}, resource = state.resources?.settings || {};
  if (tab === "general") return `<section class="settings-section"><header class="settings-section-title"><div><h3>${L("التجربة", "Experience")}</h3><p>${L("قيم محفوظة فعلياً في إعدادات WISE وتُطبّق بعد الحفظ.", "These values are stored in WISE settings and applied after saving.")}</p></div></header><div class="settings-group">${settingSelect("language", L("لغة الواجهة", "Interface language"), L("تغيّر اللغة واتجاه الواجهة بالكامل.", "Changes the interface language and direction."), [["ar","العربية"],["en","English"]], cfg.ui_language || "ar")}${settingSelect("autonomy", L("مستوى الاستقلالية", "Autonomy level"), L("يحدد متى ينفذ WISE ومتى يطلب قرارك.", "Controls when WISE acts and when it asks you."), [["cautious",L("حذر","Cautious")],["balanced",L("متوازن","Balanced")],["autonomous",L("مستقل","Autonomous")]], cfg.autonomy_level || "balanced")}${settingToggle("headless", L("المتصفح المعزول", "Isolated browser"), L("يستخدم WISE متصفحه الخاص ما لم تطلب متصفح جهازك.", "Uses WISE's isolated browser unless you request your device browser."), cfg.browser_headless)}</div><header class="settings-section-title secondary"><div><h3>${L("السلامة", "Safety")}</h3><p>${L("حدود التأكيد ومفتاح الإيقاف الفوري.", "Confirmation boundaries and the emergency stop key.")}</p></div></header><div class="settings-group">${settingSelect("securityStrictness", L("صرامة الأمان", "Security strictness"), L("يرفع أو يخفض مقدار التأكيد المطلوب قبل الإجراءات الحساسة.", "Adjusts confirmation requirements for sensitive actions."), [["low",L("منخفضة","Low")],["medium",L("متوسطة","Medium")],["high",L("عالية","High")]], cfg.security_strictness || "medium")}${settingSelect("emergencyStopKey", L("مفتاح الإيقاف", "Emergency stop key"), L("يوقف التحكم التفاعلي فوراً.", "Immediately stops interactive control."), [["Escape","Escape"],["Pause","Pause"],["F12","F12"]], cfg.emergency_stop_key || "Escape")}</div><div class="settings-savebar"><span>${L("لا تُحفظ التغييرات حتى تضغط حفظ.", "Changes are saved only when you press Save.")}</span><button class="primary-button" data-save-settings="general">${L("حفظ التغييرات", "Save changes")}</button></div></section>`;
  if (tab === "models") {
    if (state.settingsProviderId) return providerSettingsTemplate(state.settingsProviderId);
    if (state.settingsLocalModelId) return localModelSettingsTemplate(state.settingsLocalModelId);
    return modelProvidersTemplate();
  }
  if (tab === "voice") return `<section class="settings-section"><header class="settings-section-title"><div><h3>${L("المحادثة الصوتية", "Voice conversation")}</h3><p>${L("إعدادات حقيقية لمحرك الاستماع والنطق وسلوك المقاطعة.", "Real controls for listening, speech, and interruption behavior.")}</p></div></header><div class="settings-group">${settingToggle("bargeIn", L("المقاطعة أثناء الكلام", "Barge-in"), L("أوقف نطق WISE عندما تبدأ بالكلام.", "Stop WISE speaking when you start talking."), cfg.voice_barge_in)}${settingSelect("voiceMode", L("وضع الاستماع", "Listening mode"), L("اضغط للتحدث أو اكتشاف صوت مستمر.", "Use push-to-talk or continuous voice detection."), [["push_to_talk",L("اضغط للتحدث","Push to talk")],["continuous_vad",L("استماع مستمر","Continuous listening")]], cfg.voice_mode || "push_to_talk")}${settingNumber("vadThreshold", L("حساسية اكتشاف الصوت", "Voice detection threshold"), L("قيمة أقل تلتقط الكلام الهادئ بصورة أسرع.", "Lower values detect quieter speech sooner."), cfg.voice_vad_threshold ?? 0.5, { min: 0.1, max: 0.95, step: 0.05 })}${settingSelect("sttLanguage", L("لغة التعرف", "Recognition language"), L("كشف تلقائي أو لغة محددة.", "Use automatic detection or a specific language."), [["auto",L("تلقائي","Auto")],["ar","العربية"],["en","English"]], cfg.stt_language || "auto")}${settingSelect("sttEngine", L("محرك الاستماع", "Speech-to-text engine"), L("يعمل Whisper على المعالج لتقليل استهلاك VRAM.", "Whisper runs on CPU to preserve VRAM."), [["whisper_cpu","Whisper CPU"]], cfg.stt_engine || "whisper_cpu")}${settingSelect("sttModel", L("حجم نموذج الاستماع", "Speech model size"), L("النماذج الأكبر أدق لكنها أبطأ.", "Larger models are more accurate but slower."), [["tiny","Tiny"],["base","Base"],["small","Small"],["medium","Medium"]], cfg.stt_model || "base")}</div><header class="settings-section-title secondary"><div><h3>${L("النطق", "Speech output")}</h3><p>${L("المحرك والصوت ومدة إبقائه في الذاكرة.", "Engine, voice, and memory residency.")}</p></div></header><div class="settings-group">${settingSelect("ttsEngine", L("محرك الصوت", "Voice engine"), L("IndexTTS للصوت المستنسخ، وWindows SAPI كبديل خفيف.", "IndexTTS uses the cloned voice; Windows SAPI is the lightweight fallback."), [["indextts","IndexTTS"],["windows_sapi","Windows SAPI"]], cfg.tts_engine || "indextts")}${settingText("ttsVoice", L("معرّف الصوت", "Voice identifier"), L("اسم الصوت الذي يستخدمه المحرك الحالي.", "Voice name used by the active engine."), cfg.tts_voice || "")}${settingNumber("ttsIdleUnload", L("تحرير الصوت بعد الخمول", "Unload voice after idle"), L("بالثواني؛ يحرر الذاكرة عندما لا توجد محادثة صوتية.", "Seconds before releasing memory while voice is idle."), cfg.tts_idle_unload_seconds ?? 120, { min: 15, max: 3600, step: 15 })}</div><div class="notice mic-calibration" id="micCalibration">${L("اختبر الميكروفون قبل تشغيل المحادثة الصوتية.", "Test the microphone before starting voice mode.")}<div class="mic-meter"><i></i></div></div><div class="settings-savebar"><span></span><span class="settings-inline-actions"><button class="secondary-button" data-calibrate-mic>${L("اختبار الميكروفون", "Test microphone")}</button><button class="secondary-button" data-toggle-voice>${state.voice.is_running ? L("إيقاف الصوت", "Stop voice") : L("تشغيل الصوت", "Start voice")}</button><button class="primary-button" data-save-settings="voice">${L("حفظ التغييرات", "Save changes")}</button></span></div></section>`;
  if (tab === "computer") {
    const integration = sys.settings || {};
    const enabled = Boolean(integration.enabled ?? sys.enabled ?? sys.active);
    const uacOperations = sys.uac?.operations || [];
    const uacAvailable = Boolean(enabled && sys.uac?.available);
    const uacRows = uacOperations.map((item) => `<div class="setting-row"><div><b>${escapeHtml(item.title || item.id)}</b><small>${escapeHtml(item.description || "")}</small></div><button class="secondary-button" data-uac-operation="${escapeHtml(item.id)}" data-uac-title="${escapeHtml(item.title || item.id)}" ${uacAvailable ? "" : "disabled"}>${L("طلب موافقة Windows", "Request Windows approval")}</button></div>`).join("");
    const uacRuns = [...(sys.uac?.active_runs || []), ...(sys.uac?.recent_runs || [])].slice(0, 6);
    const uacRunRows = uacRuns.map((run) => {
      const stateLabel = ({ awaiting_approval: L("بانتظار موافقة Windows", "Awaiting Windows approval"), running: L("قيد التشغيل", "Running"), completed: L("اكتملت", "Completed"), failed: L("فشلت", "Failed"), cancelled: L("أُلغي الطلب", "Cancelled") })[run.state] || run.state;
      const detail = run.exit_code == null ? (run.error || run.operation_id) : `${run.operation_id} · exit ${run.exit_code}`;
      return `<div class="setting-row"><div><b>${escapeHtml(run.title || run.operation_id)}</b><small>${escapeHtml(detail)}</small></div><span class="status-text">${escapeHtml(stateLabel)}</span></div>`;
    }).join("");
    return `<section class="settings-section"><h3>Super Computer</h3><div class="notice">${L("هذا وضع اختياري يعمل داخل جلسة Windows. العمليات شديدة الحساسية تبقى واضحة وتتطلب موافقة UAC من Windows.", "This optional mode runs inside your Windows session. Highly sensitive operations stay visible and require Windows UAC approval.")}</div>${settingToggle("superEnabled", L("تفعيل وضع Super Computer", "Enable Super Computer mode"), L("يفعّل التكامل العميق المقيد بسياسات الأمان داخل جلسة المالك.", "Enables policy-constrained system integration inside the owner's session."), enabled)}${settingToggle("startAtLogin", L("التشغيل بعد تسجيل الدخول", "Start after sign-in"), L("يشغّل WISE في الخلفية داخل جلسة Windows من دون صلاحية دائمة مرتفعة.", "Starts WISE in the Windows user session without persistent elevation."), integration.start_at_login ?? true)}${settingToggle("backgroundMode", L("وضع الخلفية", "Background mode"), L("يبقى خاملاً بخفة عندما لا توجد مهمة.", "Stays lightweight while idle."), integration.background_mode ?? sys.background_mode)}${settingToggle("hostBrowser", L("متصفح الجهاز", "Device browser"), L("يسمح باستخدام Brave وحساباتك فقط عندما تطلب ذلك بوضوح.", "Uses Brave with your signed-in accounts only when you explicitly ask."), integration.host_browser_enabled ?? sys.host_browser_enabled)}${settingToggle("computerControl", L("التحكم بالكمبيوتر", "Computer control"), L("الوصول التنفيذي ضمن سياسات الأمان والمسارات المسموحة.", "Execution access within safety policies and allowed paths."), integration.computer_control_enabled ?? cfg.computer_control_enabled)}<div class="settings-actions"><button class="primary-button" data-save-settings="computer">${L("حفظ وتطبيق", "Save & apply")}</button></div></section><section class="settings-section"><h3>${L("صيانة Windows عبر UAC", "Windows maintenance through UAC")}</h3><div class="notice">${enabled ? L("كل عملية ثابتة ومراجعة مسبقاً. يعرض Windows نافذة UAC ثم يراقب WISE العملية والنتيجة الفعلية من دون قبول النافذة نيابة عنك.", "Every operation is fixed and pre-reviewed. Windows shows UAC, then WISE monitors the real process and result without accepting the prompt for you.") : L("فعّل وضع Super Computer أولاً لإتاحة عمليات الصيانة الرسمية.", "Enable Super Computer mode before requesting official maintenance operations.")}</div>${uacRows || `<div class="setting-row"><div><b>${L("لا توجد عمليات متاحة", "No operations available")}</b><small>${L("يتطلب هذا القسم Windows وواجهة UAC متاحة.", "This section requires Windows with UAC available.")}</small></div></div>`}</section>${uacRunRows ? `<section class="settings-section"><h3>${L("سجل العمليات الإدارية", "Administrative operation history")}</h3>${uacRunRows}</section>` : ""}`;
  }
  if (tab === "integrations") return integrationsSettingsTemplate();
  if (tab === "knowledge") return `${capabilityBrowserTemplate()}<details class="settings-section knowledge-source-list"><summary>${L("حالة مصادر المعرفة", "Knowledge source status")} · ${state.knowledge.length}</summary>${state.knowledge.map((item) => `<div class="setting-row"><div><b>${escapeHtml(item.name || item.id)}</b><small>${escapeHtml(item.description || item.category || L("مصدر معرفة", "Knowledge source"))}</small></div><span>${!item.last_checked ? L("لم يُفحص", "Not checked") : item.available ? L("متاح عند آخر فحص", "Available at last check") : L("تعذر الاتصال عند آخر فحص", "Unavailable at last check")}</span></div>`).join("") || `<div class="notice">${L("لا توجد مصادر معرفة مسجلة.", "No registered knowledge sources.")}</div>`}</details>`;
  if (tab === "performance") return `<section class="settings-section"><header class="settings-section-title"><div><h3>${L("السياق والطلبات", "Context & requests")}</h3><p>${L("تقليل الاستهلاك مع إبقاء جودة الإجابة.", "Reduce resource use while preserving answer quality.")}</p></div></header><div class="settings-group">${settingToggle("compression", L("ضغط السياق", "Context compression"), L("يقلل التوكنز مع الحفاظ على المعلومات المهمة.", "Reduces tokens while preserving important information."), resource.compression_enabled)}${settingSelect("compressionMethod", L("طريقة الضغط", "Compression method"), L("يختار Auto أقوى محرك متاح.", "Auto selects the strongest available engine."), [["auto",L("تلقائي","Auto")],["llmlingua2","LLMLingua 2"],["longllmlingua","LongLLMLingua"],["none",L("بدون","None")]], resource.compression_method || "auto")}${settingNumber("compressionRatio", L("نسبة الضغط", "Compression ratio"), L("قيمة أقل تعني سياقاً أصغر. النطاق الآمن 0.35–0.8.", "A lower value means a smaller context. Safe range: 0.35–0.8."), resource.compression_ratio ?? 0.55, { min: 0.35, max: 0.8, step: 0.05 })}</div><header class="settings-section-title secondary"><div><h3>${L("تشغيل النموذج المحلي", "Local model runtime")}</h3><p>${L("تُحفظ هذه القيم لتشغيل النموذج المحلي التالي.", "These values are stored for the next local model runtime.")}</p></div></header><div class="settings-group">${settingNumber("contextWindow", L("نافذة السياق", "Context window"), L("عدد التوكنز التي يستطيع النموذج المحلي الاحتفاظ بها.", "Tokens retained by the local model."), cfg.context_window ?? 8192, { min: 512, max: 131072, step: 512 })}${settingNumber("gpuLayers", L("طبقات GPU", "GPU layers"), L("استخدم 0 للمعالج فقط، أو ارفعها حسب VRAM المتاحة.", "Use 0 for CPU-only, or increase based on available VRAM."), cfg.gpu_layers ?? 33, { min: 0, max: 200, step: 1 })}</div><div class="settings-savebar"><span>${L("تُطبّق إعدادات التشغيل عند تحميل النموذج المحلي.", "Runtime settings apply when the local model loads.")}</span><button class="primary-button" data-save-settings="performance">${L("حفظ التغييرات", "Save changes")}</button></div></section>`;
  return `<section class="settings-section"><h3>${L("الحالة الحالية", "Current status")}</h3><div class="setting-row"><div><b>WISE Core</b><small>${state.telemetry.active_window || "Desktop"}</small></div><span class="status-text">${t("connected")}</span></div><div class="setting-row"><div><b>${L("النموذج", "Model")}</b><small>${escapeHtml(state.telemetry.active_model || state.selectedModel?.label || L("غير محمل", "Not loaded"))}</small></div></div><div class="setting-row"><div><b>${L("استخدام CPU والذاكرة", "CPU and memory usage")}</b><small>${escapeHtml(state.telemetry.cpu_percent ?? "—")}% · ${escapeHtml(state.telemetry.ram_used_gb ?? "—")} / ${escapeHtml(state.telemetry.ram_total_gb ?? "—")} GB</small></div><button class="secondary-button" data-run-health>${L("تحديث", "Refresh")}</button></div></section>`;
}

function modelProvidersTemplate() {
  const connected = state.providers.filter((item) => item.is_configured).length;
  const activeName = state.selectedModel?.label || L("لا يوجد نموذج نشط", "No active model");
  const providers = state.providers.map((item) => {
    const id = escapeHtml(providerIdOf(item));
    const configured = Boolean(item.is_configured);
    const model = item.configured_model || item.default_model || L("لم يُحدد نموذج", "No model selected");
    return `<button class="provider-directory-row ${configured ? "configured" : ""}" data-open-provider-settings="${id}" role="listitem">${providerMark(item, "provider-settings-logo")}<span class="provider-settings-copy"><b>${escapeHtml(item.name || item.id)}</b><small>${escapeHtml(model)}</small></span><span class="provider-connection-state">${configured ? L("متصل", "Connected") : L("إعداد", "Set up")}</span><svg aria-hidden="true"><use href="#i-chevron"/></svg></button>`;
  }).join("");
  const localModels = state.localModels.map((item) => {
    const id = escapeHtml(item.id || item.model_id || item.name);
    const params = item.runtime_params || {};
    const detail = `${item.file_size_mb || 0} MB · ${params.n_ctx || 4096} ctx${item.is_active ? ` · ${L("نشط", "Active")}` : ""}`;
    return `<button class="provider-directory-row local-model-row ${item.is_active ? "configured" : ""}" data-open-local-settings="${id}" role="listitem">${providerMark({ id: "local" }, "provider-settings-logo")}<span class="provider-settings-copy"><b>${escapeHtml(item.name || item.id)}</b><small>${escapeHtml(detail)}</small></span><span class="provider-connection-state">${item.is_active ? L("قيد الاستخدام", "In use") : L("إعداد", "Configure")}</span><svg aria-hidden="true"><use href="#i-chevron"/></svg></button>`;
  }).join("");
  return `<section class="settings-section provider-settings-section"><header class="settings-hero"><div><span class="settings-kicker">${connected} / ${state.providers.length} ${L("مزود متصل", "providers connected")}</span><h3>${L("النماذج السحابية", "Cloud models")}</h3><p>${L("افتح أي مزود لإدخال الاتصال، اختبار القائمة الحية، ثم اختيار النموذج الفعلي.", "Open a provider to enter its connection, test the live catalog, and choose the actual model.")}</p></div><span class="settings-hero-model">${providerMark(state.selectedModel || { id: "custom" })}<span><small>${L("النموذج الحالي", "Current model")}</small><b>${escapeHtml(activeName)}</b></span></span></header><div class="provider-directory" role="list">${providers || `<div class="notice">${L("لم يعثر WISE على مزودين.", "WISE found no providers.")}</div>`}</div></section><section class="settings-section local-model-section"><div class="settings-section-head"><div><h3>${L("النماذج المحلية", "Local models")}</h3><p>${L("افتح النموذج لضبط الذاكرة والسياق والمعالج ثم فعّله.", "Open a model to tune memory, context, and CPU settings, then activate it.")}</p></div><button class="secondary-button" data-import-local>${icon("plus")}${L("استيراد GGUF", "Import GGUF")}</button></div><div class="provider-directory">${localModels || `<div class="notice">${L("لا توجد نماذج محلية مستوردة.", "No local models have been imported.")}</div>`}</div></section>`;
}

function providerSettingsTemplate(providerId) {
  const provider = state.providers.find((item) => providerIdOf(item) === providerId) || { id: providerId, name: providerId };
  const models = state.settingsProviderModels.length ? state.settingsProviderModels : (state.providerModels[providerId] || []);
  const configuredModel = provider.configured_model || provider.default_model || "";
  const draft = state.settingsProviderDraft || {
    baseUrl: provider.base_url || "", apiKey: "", model: configuredModel,
    customModel: "", setActive: Boolean(provider.is_active),
  };
  const modelValues = [...new Set([configuredModel, ...models].filter(Boolean))];
  const result = state.settingsProviderResult;
  const resultHtml = result ? `<p class="provider-form-result ${escapeHtml(result.kind)}">${escapeHtml(result.text)}</p>` : `<p class="provider-hint">${escapeHtml(provider.notes || L("احفظ البيانات محلياً في مخزن WISE الآمن. يمكنك ترك المفتاح فارغاً للاحتفاظ بالمفتاح الموجود.", "Credentials are stored in WISE's secure local store. Leave the key blank to keep the saved one."))}</p>`;
  const disconnect = provider.is_configured ? `<button class="danger-button provider-disconnect" type="button" data-disconnect-provider="${escapeHtml(providerId)}">${L("فصل المزود", "Disconnect")}</button>` : "";
  return `<section class="settings-section provider-setup-page"><button class="settings-back" data-back-provider-settings>${icon("arrow-up")}<span>${L("كل المزودين", "All providers")}</span></button><header class="provider-setup-heading">${providerMark(provider, "provider-setup-logo")}<span><span class="settings-kicker">${L("اتصال النموذج", "Model connection")}</span><h3>${escapeHtml(provider.name || providerId)}</h3><p>${provider.is_configured ? L("حدّث الاتصال أو النموذج، واختبر المسودة قبل الحفظ.", "Update the connection or model, then test this draft before saving.") : L("أدخل اتصال المزود، اجلب النماذج، ثم اختر النموذج المطلوب.", "Enter the provider connection, load its models, then choose one.")}</p></span></header><form id="settingsProviderForm" class="provider-settings-form"><input id="settingsProviderId" type="hidden" value="${escapeHtml(providerId)}"><section class="provider-form-section"><span class="provider-form-section-label">${L("الاتصال", "Connection")}</span><label class="provider-field"><span>${L("عنوان API", "API endpoint")}</span><input class="field" id="settingsProviderBaseUrl" type="url" inputmode="url" autocomplete="url" value="${escapeHtml(draft.baseUrl)}" placeholder="https://…"></label><label class="provider-field"><span>${L("مفتاح API", "API key")}</span><input class="field" id="settingsProviderApiKey" type="password" autocomplete="new-password" value="${escapeHtml(draft.apiKey)}" placeholder="${provider.has_api_key ? L("اتركه فارغاً للاحتفاظ بالمفتاح المحفوظ", "Leave blank to keep the saved key") : "••••••••••••"}"><small>${L("يُحفظ محلياً فقط، ولا يظهر بعد الحفظ.", "Stored locally only; never shown after saving.")}</small></label></section><section class="provider-form-section"><div class="provider-model-heading"><span class="provider-form-section-label">${L("النموذج", "Model")}</span><button class="secondary-button" type="button" data-fetch-settings-provider>${icon("refresh")}<span>${L("جلب النماذج", "Load models")}</span></button></div><label class="provider-field"><span>${L("اختر من النماذج المتاحة", "Choose an available model")}</span><select class="field provider-model-select" id="settingsProviderModel">${modelValues.length ? modelValues.map((model) => `<option value="${escapeHtml(model)}" ${model === draft.model ? "selected" : ""}>${escapeHtml(model)}</option>`).join("") : `<option value="">${L("اجلب النماذج أولاً", "Load models first")}</option>`}</select></label><label class="provider-field"><span>${L("أو اكتب اسم نموذج يدوي", "Or enter a model name manually")}</span><input class="field" id="settingsProviderCustomModel" type="text" autocomplete="off" value="${escapeHtml(draft.customModel)}" placeholder="${L("اختياري", "Optional")}"></label></section><label class="provider-activation"><input class="toggle" id="settingsProviderSetActive" type="checkbox" ${draft.setActive ? "checked" : ""}><span><b>${L("استخدمه كنموذج نشط", "Make this the active model")}</b><small>${L("يمكنك حفظ الاتصال دون تغيير النموذج الحالي.", "You can save this connection without changing the current model.")}</small></span></label>${resultHtml}<footer class="provider-settings-actions">${disconnect}<span class="provider-actions-spacer"></span><button class="secondary-button" type="button" data-test-settings-provider>${L("اختبار الاتصال", "Test connection")}</button><button class="primary-button" type="submit">${L("حفظ التغييرات", "Save changes")}</button></footer></form></section>`;
}

function localModelSettingsTemplate(modelId) {
  const model = state.localModels.find((item) => String(item.id || item.model_id || item.name) === String(modelId));
  if (!model) return `<div class="notice">${L("تعذر العثور على النموذج المحلي.", "Local model could not be found.")}</div>`;
  const params = model.runtime_params || {};
  const result = state.settingsLocalModelResult;
  const feedback = result ? `<p class="provider-form-result ${escapeHtml(result.kind)}">${escapeHtml(result.text)}</p>` : "";
  return `<section class="settings-section provider-setup-page"><button class="settings-back" data-back-local-settings>${icon("arrow-up")}<span>${L("كل النماذج", "All models")}</span></button><header class="provider-setup-heading">${providerMark({ id: "local" }, "provider-setup-logo")}<span><span class="settings-kicker">${L("نموذج محلي حقيقي", "Real local model")}</span><h3>${escapeHtml(model.name || model.id)}</h3><p>${escapeHtml(model.file_path || model.path || "")}</p></span></header><form id="settingsLocalModelForm" class="provider-settings-form"><input id="settingsLocalModelId" type="hidden" value="${escapeHtml(modelId)}"><section class="provider-form-section local-runtime-grid"><span class="provider-form-section-label">${L("إعدادات التشغيل", "Runtime settings")}</span>${settingNumber("localContext", L("نافذة السياق", "Context window"), L("تؤثر في الذاكرة وطول المحادثة.", "Controls memory use and conversation length."), params.n_ctx ?? 4096, { min: 512, max: 131072, step: 512 })}${settingNumber("localGpuLayers", L("طبقات GPU", "GPU layers"), L("استخدم 0 للمعالج فقط.", "Use 0 for CPU-only."), params.n_gpu_layers ?? 33, { min: 0, max: 200, step: 1 })}${settingNumber("localThreads", L("خيوط المعالج", "CPU threads"), L("عدد الخيوط المستخدمة عند التشغيل على CPU.", "CPU threads available to local inference."), params.threads ?? 8, { min: 1, max: 256, step: 1 })}${settingNumber("localTemperature", L("الحرارة", "Temperature"), L("قيمة أقل لنتائج أكثر ثباتاً.", "Lower values produce more deterministic output."), params.temperature ?? 0.2, { min: 0, max: 2, step: 0.05 })}</section>${feedback}<footer class="provider-settings-actions"><button class="danger-button" type="button" data-delete-local="${escapeHtml(modelId)}">${L("إزالة من WISE", "Remove from WISE")}</button><span class="provider-actions-spacer"></span>${model.is_active ? `<span class="active-model-label">${L("النموذج النشط", "Active model")}</span>` : `<button class="secondary-button" type="button" data-activate-local="${escapeHtml(modelId)}">${L("تفعيل النموذج", "Activate model")}</button>`}<button class="primary-button" type="submit">${L("حفظ إعدادات التشغيل", "Save runtime settings")}</button></footer></form></section>`;
}

function openProviderSettings(id) {
  state.settingsTab = "models";
  state.settingsLocalModelId = null;
  state.settingsProviderId = id;
  state.settingsProviderModels = state.providerModels[id] || [];
  state.settingsProviderResult = null;
  const provider = state.providers.find((item) => providerIdOf(item) === id) || { id };
  state.settingsProviderDraft = { baseUrl: provider.base_url || "", apiKey: "", model: provider.configured_model || provider.default_model || "", customModel: "", setActive: Boolean(provider.is_active) };
  return renderSettings();
}

function openLocalModelSettings(id) {
  state.settingsTab = "models";
  state.settingsProviderId = null;
  state.settingsProviderDraft = null;
  state.settingsLocalModelId = id;
  state.settingsLocalModelResult = null;
  return renderSettings();
}

function captureSettingsProviderDraft() {
  const id = $("settingsProviderId")?.value;
  if (!id) return state.settingsProviderDraft;
  const draft = {
    baseUrl: $("settingsProviderBaseUrl")?.value.trim() || "",
    apiKey: $("settingsProviderApiKey")?.value || "",
    model: $("settingsProviderModel")?.value.trim() || "",
    customModel: $("settingsProviderCustomModel")?.value.trim() || "",
    setActive: Boolean($("settingsProviderSetActive")?.checked),
  };
  state.settingsProviderDraft = draft;
  return draft;
}

async function fetchSettingsProviderModels({ announce = true } = {}) {
  const id = $("settingsProviderId")?.value;
  const provider = state.providers.find((item) => providerIdOf(item) === id) || { id };
  const draft = captureSettingsProviderDraft();
  const button = document.querySelector("[data-fetch-settings-provider]");
  const previous = button?.innerHTML;
  if (button) { button.disabled = true; button.innerHTML = `<span class="mini-spinner"></span><span>${L("جارٍ الجلب…", "Loading…")}</span>`; }
  try {
    const models = await fetchProviderModels(provider, { force: true, apiKey: draft.apiKey.trim() || null, baseUrl: draft.baseUrl.trim() || null });
    state.settingsProviderModels = models;
    state.settingsProviderResult = { kind: "success", text: L(`تم جلب ${models.length} نموذج.`, `Loaded ${models.length} models.`) };
    if (!models.includes(draft.model) && models.length && !draft.customModel) draft.model = models[0];
    renderSettings();
    if (announce) toast(L(`تم جلب ${models.length} نموذج`, `Loaded ${models.length} models`), "success");
    return models;
  } catch (error) {
    state.settingsProviderResult = { kind: "error", text: error.message };
    renderSettings();
    if (announce) toast(error.message, "error");
    return [];
  } finally { if (button) { button.disabled = false; button.innerHTML = previous; } }
}

async function saveSettingsProvider(event) {
  event.preventDefault();
  const id = $("settingsProviderId").value;
  const draft = captureSettingsProviderDraft();
  const model = draft.customModel || draft.model || null;
  const submit = $("settingsProviderForm").querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const result = await api("/api/v2/models/external", { method: "POST", body: JSON.stringify({ provider_id: id, api_key: draft.apiKey.trim() || null, base_url: draft.baseUrl.trim() || null, model, set_as_active: draft.setActive }) });
    if (result.ok === false) throw new Error(result.message || L("تعذر حفظ المزود", "Could not save provider"));
    delete state.providerModels[id]; delete state.providerModelStatus[id];
    state.settingsProviderResult = { kind: "success", text: draft.setActive ? L("تم الحفظ واختيار هذا المزود للاستخدام.", "Saved and selected for use.") : L("تم حفظ الاتصال دون تغيير النموذج النشط.", "Connection saved without changing the active model.") };
    await Promise.all([loadModels(), loadRuntime()]);
    await renderSettings();
    toast(draft.setActive ? L("تم ربط المزود واختياره", "Provider connected and selected") : L("تم حفظ اتصال المزود", "Provider connection saved"), "success");
  } catch (error) { state.settingsProviderResult = { kind: "error", text: error.message }; renderSettings(); toast(error.message, "error"); }
  finally { if (submit) submit.disabled = false; }
}

function settingToggle(id, title, detail, checked) { return `<div class="setting-row"><div><b>${title}</b><small>${detail}</small></div><input class="toggle setting-control" id="${id}" type="checkbox" ${checked ? "checked" : ""}></div>`; }
function settingSelect(id, title, detail, options, current) { return `<div class="setting-row"><div><b>${title}</b><small>${detail}</small></div><select class="field setting-control" id="${id}">${options.map(([value,label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join("")}</select></div>`; }
function settingNumber(id, title, detail, value, { min, max, step }) { return `<label class="setting-row" for="${id}"><div><b>${title}</b><small>${detail}</small></div><input class="field setting-control numeric-control" id="${id}" type="number" min="${min}" max="${max}" step="${step}" value="${escapeHtml(value)}"></label>`; }
function settingText(id, title, detail, value) { return `<label class="setting-row" for="${id}"><div><b>${title}</b><small>${detail}</small></div><input class="field setting-control" id="${id}" type="text" autocomplete="off" value="${escapeHtml(value)}"></label>`; }
function integrationRow(name, detail, connected) { return `<div class="setting-row"><div><b>${escapeHtml(name)}</b><small>${escapeHtml(detail)}</small></div><span class="status-text">${connected ? L("متصل", "Connected") : L("غير متصل", "Disconnected")}</span></div>`; }

async function saveLocalModelSettings(event) {
  event.preventDefault();
  const form = $("settingsLocalModelForm");
  const id = $("settingsLocalModelId")?.value;
  const submit = form?.querySelector('[type="submit"]');
  if (!id || !form) return;
  if (submit) submit.disabled = true;
  try {
    const payload = {
      n_ctx: Number($("localContext").value),
      n_gpu_layers: Number($("localGpuLayers").value),
      threads: Number($("localThreads").value),
      temperature: Number($("localTemperature").value),
    };
    const result = await api(`/api/v2/models/local/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) });
    if (result.ok === false) throw new Error(result.message || result.error || L("تعذر حفظ إعدادات النموذج", "Could not save model settings"));
    state.settingsLocalModelResult = { kind: "success", text: L("حُفظت إعدادات التشغيل الفعلية لهذا النموذج.", "The runtime settings for this model were saved.") };
    await loadModels();
    await renderSettings();
    toast(L("حُفظت إعدادات النموذج", "Model settings saved"), "success");
  } catch (error) {
    state.settingsLocalModelResult = { kind: "error", text: error.message };
    await renderSettings();
    toast(error.message, "error");
  } finally { if (submit) submit.disabled = false; }
}

async function disconnectProvider(id) {
  if (!confirm(L("فصل هذا المزود وإزالة بيانات اتصاله المحفوظة؟", "Disconnect this provider and remove its saved connection?"))) return;
  try {
    const result = await api(`/api/v2/models/external/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (result.ok === false) throw new Error(result.message || result.error || L("تعذر فصل المزود", "Could not disconnect provider"));
    state.settingsProviderId = null;
    state.settingsProviderDraft = null;
    delete state.providerModels[id];
    await Promise.all([loadModels(), loadRuntime()]);
    await renderSettings();
    toast(L("تم فصل المزود", "Provider disconnected"), "success");
  } catch (error) { toast(error.message, "error"); }
}

async function saveSettingsSection(section) {
  try {
    if (section === "general") {
      const saved = await api("/api/v2/settings", { method: "PATCH", body: JSON.stringify({ ui_language: $("language").value, autonomy_level: $("autonomy").value, browser_headless: $("headless").checked, security_strictness: $("securityStrictness").value, emergency_stop_key: $("emergencyStopKey").value }) });
      if (saved.ok === false) throw new Error(saved.error || saved.message || L("تعذر حفظ الإعدادات العامة", "Could not save general settings"));
    }
    if (section === "voice") {
      const saved = await api("/api/v2/settings", { method: "PATCH", body: JSON.stringify({ voice_barge_in: $("bargeIn").checked, voice_mode: $("voiceMode").value, voice_vad_threshold: Number($("vadThreshold").value), stt_language: $("sttLanguage").value, stt_engine: $("sttEngine").value, stt_model: $("sttModel").value, tts_engine: $("ttsEngine").value, tts_voice: $("ttsVoice").value.trim(), tts_idle_unload_seconds: Number($("ttsIdleUnload").value) }) });
      if (saved.ok === false) throw new Error(saved.error || saved.message || L("تعذر حفظ إعدادات الصوت", "Could not save voice settings"));
    }
    if (section === "computer") {
      const current = state.system.settings || {};
      const active = Boolean(current.enabled ?? state.system.enabled ?? state.system.active);
      const target = $("superEnabled").checked;
      const updates = {
        start_at_login: $("startAtLogin").checked,
        background_mode: $("backgroundMode").checked,
        host_browser_enabled: $("hostBrowser").checked,
        computer_control_enabled: $("computerControl").checked,
      };
      const sensitiveChange = target !== active
        || updates.start_at_login !== Boolean(current.start_at_login)
        || updates.host_browser_enabled !== Boolean(current.host_browser_enabled)
        || updates.computer_control_enabled !== Boolean(current.computer_control_enabled);
      if (sensitiveChange && !confirm(L(
        "سيغيّر هذا تكامل WISE مع جلسة Windows أو صلاحيات التحكم المسموحة. هل تريد المتابعة؟",
        "This changes WISE integration with your Windows session or its allowed control surface. Continue?",
      ))) return;

      if (active && !target) {
        const deactivated = await api("/api/v2/system-integration/deactivate", { method: "POST", body: JSON.stringify({ confirmed: true, remove_autostart: true }) });
        if (deactivated.ok === false) throw new Error(deactivated.error || L("تعذر تعطيل الوضع", "Could not disable the mode"));
      }
      const configured = await api("/api/v2/system-integration", { method: "PATCH", body: JSON.stringify({ ...updates, confirmed: sensitiveChange }) });
      if (configured.ok === false) throw new Error(configured.error || L("تعذر حفظ إعدادات النظام", "Could not save system settings"));
      if (!active && target) {
        const activated = await api("/api/v2/system-integration/activate", { method: "POST", body: JSON.stringify({ confirmed: true, remove_autostart: false }) });
        if (activated.ok === false) throw new Error(activated.error || L("تعذر تفعيل الوضع", "Could not enable the mode"));
      }
    }
    if (section === "performance") {
      const outcomes = await Promise.all([
        api("/admin/resources", { method: "POST", body: JSON.stringify({ compression_enabled: $("compression").checked, compression_method: $("compressionMethod").value, compression_ratio: Number($("compressionRatio").value) }) }),
        api("/api/v2/settings", { method: "PATCH", body: JSON.stringify({ context_window: Number($("contextWindow").value), gpu_layers: Number($("gpuLayers").value) }) }),
      ]);
      const failed = outcomes.find((item) => item?.ok === false);
      if (failed) throw new Error(failed.error || failed.message || L("تعذر حفظ إعدادات الأداء", "Could not save performance settings"));
    }
    toast("تم حفظ الإعدادات", "success");
    await Promise.all([loadRuntime(), loadModels()]);
    await renderSettings();
  } catch (error) { toast(error.message, "error"); }
}

async function requestUacOperation(id, title) {
  const promptText = L(
    `سيطلب Windows موافقتك عبر UAC لتشغيل: ${title}. لن يستطيع WISE قبول النافذة نيابة عنك. هل تتابع؟`,
    `Windows will ask for your UAC approval to run: ${title}. WISE cannot accept the prompt for you. Continue?`,
  );
  if (!confirm(promptText)) return;
  try {
    const intent = await api("/api/v2/system-integration/uac/intent", {
      method: "POST",
      body: JSON.stringify({ operation: id }),
    });
    if (intent.ok === false || !intent.intent_token) throw new Error(intent.error || L("تعذر إنشاء تفويض UAC مؤقت", "Could not create a temporary UAC intent"));
    const result = await api("/api/v2/system-integration/uac/request", {
      method: "POST",
      body: JSON.stringify({ operation: id, confirmed: true, intent_token: intent.intent_token }),
    });
    if (result.ok === false) throw new Error(result.error || L("رفض Windows طلب UAC", "Windows rejected the UAC request"));
    toast(L("وافَق Windows وبدأ WISE مراقبة العملية الإدارية.", "Windows approved the request and WISE is monitoring the operation."), "success");
    await loadRuntime();
    await renderSettings();
  } catch (error) { toast(error.message, "error"); }
}

function configureProvider(id) {
  if ($("settingsModal").hidden) $("settingsModal").hidden = false;
  document.body.style.overflow = "hidden";
  return openProviderSettings(id);
}

async function testProvider(id) {
  const button = document.querySelector(`[data-test-provider="${CSS.escape(id)}"]`);
  const previous = button?.textContent;
  if (button) { button.disabled = true; button.textContent = L("جارٍ الاختبار…", "Testing…"); }
  try {
    const result = await api(`/api/v2/models/external/fetch_models?provider_id=${encodeURIComponent(id)}`);
    if (result.ok === false) throw new Error(result.message || L("فشل الاتصال بالمزود", "Provider connection failed"));
    toast(L(`نجح الاتصال · ${result.models?.length || 0} نموذج`, `Connection succeeded · ${result.models?.length || 0} models`), "success");
  } catch (error) { toast(error.message, "error"); }
  finally { if (button) { button.disabled = false; button.textContent = previous; } }
}

async function importLocalModel() {
  const filePath = prompt(L("المسار الكامل لملف النموذج المحلي:", "Full path to the local model file:"));
  if (!filePath) return;
  const name = prompt(L("اسم اختياري للنموذج:", "Optional model name:"), "") || null;
  try {
    const result = await api("/api/v2/models/local/import", { method: "POST", body: JSON.stringify({ file_path: filePath, name, set_default: false }) });
    if (result.ok === false) throw new Error(result.message);
    toast(L("تم استيراد النموذج", "Model imported"), "success");
    await loadModels(); await renderSettings();
  } catch (error) { toast(error.message, "error"); }
}

async function deleteLocalModel(id) {
  if (!confirm(L("إزالة هذا النموذج من WISE؟ لن يُحذف الملف الأصلي من القرص.", "Remove this model from WISE? The original file will remain on disk."))) return;
  try {
    const result = await api(`/api/v2/models/local/${encodeURIComponent(id)}?delete_file=false`, { method: "DELETE" });
    if (result.ok === false) throw new Error(result.message);
    if (state.settingsLocalModelId === id) state.settingsLocalModelId = null;
    toast(L("أُزيل النموذج", "Model removed"), "success");
    await loadModels(); await renderSettings();
  } catch (error) { toast(error.message, "error"); }
}

async function calibrateMicrophone() {
  const panel = $("micCalibration");
  const meter = panel?.querySelector(".mic-meter > i");
  if (!navigator.mediaDevices?.getUserMedia) return toast(L("المتصفح لا يتيح فحص الميكروفون.", "Microphone testing is unavailable in this browser."), "error");
  let stream;
  let context;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
    context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    const started = performance.now();
    let peak = 0;
    await new Promise((resolve) => {
      const tick = () => {
        analyser.getByteTimeDomainData(samples);
        const rms = Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length);
        peak = Math.max(peak, rms);
        if (meter) meter.style.setProperty("--level", `${Math.min(100, Math.round(rms * 260))}%`);
        if (performance.now() - started < 3500) requestAnimationFrame(tick); else resolve();
      };
      tick();
    });
    const quality = peak > .12 ? L("ممتاز", "Excellent") : peak > .045 ? L("جيد", "Good") : L("منخفض", "Low");
    if (panel) panel.firstChild.textContent = L(`اكتملت المعايرة: مستوى الإدخال ${quality}.`, `Calibration complete: input level is ${quality}.`);
    toast(L("اكتملت معايرة الميكروفون", "Microphone calibration complete"), "success");
  } catch (error) { toast(L("تعذر الوصول إلى الميكروفون: ", "Could not access the microphone: ") + error.message, "error"); }
  finally { stream?.getTracks().forEach((track) => track.stop()); if (context) await context.close().catch(() => {}); }
}

const commandItems = [
  ["new", "edit", "newChat", "Ctrl N"], ["schedules", "clock", "schedules", ""],
  ["integrations", "link", "integrations", ""], ["media", "media", "media", ""],
  ["settings", "settings", "settings", "Ctrl ,"], ["voice", "mic", null, ""],
];

function renderCommandPalette(query = "") {
  const normalized = query.trim().toLocaleLowerCase();
  const rows = commandItems.filter(([id, , key]) => `${id} ${key ? t(key) : L("الصوت", "Voice")}`.toLocaleLowerCase().includes(normalized));
  $("commandList").innerHTML = rows.map(([id, iconName, key, shortcut], index) => `<button class="command-item${index === 0 ? " active" : ""}" role="option" data-command="${id}">${icon(iconName)}<span>${key ? t(key) : L("تشغيل أو إيقاف الصوت", "Start or stop voice")}</span><small>${shortcut}</small></button>`).join("") || `<div class="empty-list">${L("لا توجد نتائج", "No matching commands")}</div>`;
}
function openCommandPalette() { $("commandPalette").hidden = false; $("commandInput").value = ""; renderCommandPalette(); requestAnimationFrame(() => $("commandInput").focus()); }
function closeCommandPalette() { $("commandPalette").hidden = true; }
async function runCommand(id) {
  closeCommandPalette();
  if (id === "new") return blankConversation();
  if (["tasks", "schedules", "media"].includes(id)) return showView(id);
  if (id === "integrations") return openSettings("integrations");
  if (id === "settings") return openSettings();
  if (id === "voice") return toggleVoice();
}

function openSessionMenu(event, row) {
  event.preventDefault(); event.stopPropagation();
  state.sessionMenuTarget = row;
  const menu = $("sessionMenu");
  menu.hidden = false;
  const x = Math.min(innerWidth - 190, Math.max(8, event.clientX - 160));
  const y = Math.min(innerHeight - 140, Math.max(8, event.clientY));
  menu.style.left = `${x}px`; menu.style.top = `${y}px`;
}
function closeSessionMenu() { $("sessionMenu").hidden = true; state.sessionMenuTarget = null; }
async function sessionAction(action) {
  const row = state.sessionMenuTarget; if (!row) return;
  const id = sessionId(row); closeSessionMenu();
  try {
    if (action === "rename") { const title = prompt("اسم المحادثة الجديد:", sessionTitle(row)); if (!title) return; await api(`/api/v2/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title }) }); if (state.currentSession === id) { state.currentSessionTitle = title; $("pageTitle").textContent = title; } }
    if (action === "archive") { await api(`/api/v2/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ archived: true }) }); if (state.currentSession === id) blankConversation({ focus: false }); }
    if (action === "delete") { if (!confirm("هل تريد حذف هذه المحادثة وسجلها نهائياً؟")) return; await api(`/api/v2/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }); if (state.currentSession === id) blankConversation({ focus: false }); }
    toast(action === "delete" ? "حُذفت المحادثة" : "تم تحديث المحادثة", "success"); await loadSessions();
  } catch (error) { toast(error.message, "error"); }
}

function autoGrow() { const box = $("prompt"); box.style.height = "auto"; box.style.height = `${Math.min(box.scrollHeight, 200)}px`; }
function syncComposerState() {
  const ready = Boolean($("prompt")?.value.trim()) && !state.sending;
  $("composerForm")?.classList.toggle("has-content", ready);
  if ($("sendButton")) $("sendButton").disabled = !ready;
}
function scrollChat() { requestAnimationFrame(() => { $("chatScroll").scrollTop = $("chatScroll").scrollHeight; }); }
function openSidebar() {
  $("appShell").classList.remove("sidebar-collapsed");
  localStorage.setItem("wise:sidebar-collapsed", "false");
  if (matchMedia("(max-width: 899px)").matches) { $("appShell").classList.add("sidebar-open"); $("drawerOverlay").hidden = false; }
}
function collapseSidebar() {
  if (matchMedia("(max-width: 899px)").matches) return closeDrawers();
  $("appShell").classList.add("sidebar-collapsed");
  localStorage.setItem("wise:sidebar-collapsed", "true");
}
function toggleSidebar() { $("appShell").classList.contains("sidebar-collapsed") ? openSidebar() : collapseSidebar(); }
function closeDrawers() { $("appShell").classList.remove("sidebar-open"); $("drawerOverlay").hidden = true; }

function positionModelMenu() {
  const menu = $("modelMenu");
  if (menu.hidden) return;
  const button = $("modelButton").getBoundingClientRect();
  const gap = 4;
  const width = Math.min(286, innerWidth - 16);
  menu.style.width = `${width}px`;
  menu.style.maxHeight = `${Math.max(168, Math.min(272, button.top - 12))}px`;
  const height = menu.getBoundingClientRect().height;
  const left = Math.max(8, Math.min(button.left, innerWidth - width - 8));
  const top = Math.max(8, button.top - height - gap);
  menu.style.left = `${left}px`;
  menu.style.right = "auto";
  menu.style.top = `${top}px`;
  menu.style.bottom = "auto";
}

async function openModelMenu() {
  state.modelQuery = "";
  state.modelMenuProviderId = null;
  renderModelMenu();
  $("modelMenu").hidden = false;
  $("modelButton").setAttribute("aria-expanded", "true");
  requestAnimationFrame(() => { positionModelMenu(); $("modelSearch")?.focus(); });
}

function closeModelMenu() {
  $("modelMenu").hidden = true;
  $("modelButton").setAttribute("aria-expanded", "false");
}

async function openModelProvider(providerId) {
  state.modelMenuProviderId = state.modelMenuProviderId === providerId ? null : providerId;
  state.modelQuery = "";
  renderModelMenu();
  requestAnimationFrame(() => { positionModelMenu(); $("modelSearch")?.focus(); });
  if (!state.modelMenuProviderId) return;
  if (providerId === "local") return;
  const provider = state.providers.find((item) => providerIdOf(item) === providerId);
  if (provider && (provider.is_configured || providerCanFetchWithoutKey(provider))) {
    await fetchProviderModels(provider).catch(() => undefined);
    requestAnimationFrame(positionModelMenu);
  }
}

async function loadSlashCommands() {
  const result = await api("/api/v2/chat/commands");
  state.slashCommands = result.commands || [];
  // A user can type '/' before the asynchronous command catalog arrives.
  // Refresh that open query too, rather than requiring another keystroke.
  renderSlashMenu();
}

function renderSlashMenu() {
  const host = $("slashMenu");
  const match = $("prompt").value.match(/^\/([^\s]*)$/);
  host.hidden = !match;
  if (!match) return;
  const rows = state.slashCommands.filter(item => `${item.id} ${item.name}`.toLowerCase().includes(match[1].toLowerCase()));
  state.slashIndex = 0;
  host.innerHTML = rows.length ? rows.map((item, i) => `<button type="button" role="option" aria-selected="${i === 0}" data-slash-id="${escapeHtml(item.id)}" ${item.available ? "" : "disabled"}>${icon(item.kind === "mcp" ? "link" : "search")}<span><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.kind === "mcp" ? (item.available ? L("خادم متصل", "Connected MCP") : L("اتصل به من الإعدادات أولاً", "Connect in settings first")) : item.description)}</small></span></button>`).join("") : `<p>${L("لا توجد أوامر مطابقة", "No matching commands")}</p>`;
  host.querySelectorAll("[data-slash-id]").forEach(button => button.onclick = () => {
    const item = state.slashCommands.find(row => row.id === button.dataset.slashId);
    if (!item?.available) return;
    if (item.kind === "mcp") { state.selectedMcp = item.server; state.chatMode = "normal"; }
    else { state.chatMode = item.mode; state.selectedMcp = null; }
    $("prompt").value = ""; host.hidden = true;
    const selected = $("composerSelection"); selected.hidden = state.chatMode === "normal" && !state.selectedMcp;
    selected.innerHTML = `<span>${escapeHtml(item.name)}</span><button type="button" aria-label="${L("إلغاء الاختيار", "Clear selection")}">${icon("close")}</button>`;
    selected.querySelector("button").onclick = () => { state.chatMode = "normal"; state.selectedMcp = null; selected.hidden = true; };
    autoGrow(); syncComposerState(); $("prompt").focus();
  });
}

function bindEvents() {
  // The compact header stays attached to the composer; the plan expands up.
  $("taskPlanDock").append($("taskPlanToggle"));
  $("composerForm").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(); });
  $("prompt").addEventListener("input", () => { autoGrow(); saveDraft(); syncComposerState(); renderSlashMenu(); });
  $("prompt").addEventListener("keydown", (event) => {
    if (!$("slashMenu").hidden && ["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key)) {
      event.preventDefault();
      const rows = qsa("[data-slash-id]", $("slashMenu"));
      if (event.key === "Escape") { $("slashMenu").hidden = true; return; }
      if (event.key === "Enter") { rows[state.slashIndex]?.click(); return; }
      state.slashIndex = (state.slashIndex + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % (rows.length || 1);
      rows.forEach((row, i) => row.setAttribute("aria-selected", String(i === state.slashIndex)));
      rows[state.slashIndex]?.scrollIntoView({block: "nearest"}); return;
    }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); }
  });
  $("brandButton").onclick = () => blankConversation();
  qsa(".nav-item[data-view]").forEach((button) => button.onclick = () => button.dataset.view === "chat" ? blankConversation() : showView(button.dataset.view));
  $("settingsButton").onclick = () => openSettings(); $("closeSettings").onclick = closeSettings;
  document.addEventListener("submit", (event) => {
    if (event.target.id === "settingsProviderForm") saveSettingsProvider(event);
    if (event.target.id === "settingsLocalModelForm") saveLocalModelSettings(event);
  });
  $("commandButton").onclick = openCommandPalette;
  $("commandInput").oninput = (event) => renderCommandPalette(event.target.value);
  $("commandPalette").addEventListener("click", (event) => { if (event.target === $("commandPalette")) closeCommandPalette(); });
  $("settingsModal").addEventListener("click", (event) => { if (event.target === $("settingsModal")) closeSettings(); });
  $("openSidebar").onclick = openSidebar; $("closeSidebar").onclick = closeDrawers; $("drawerOverlay").onclick = closeDrawers;
  $("collapseSidebar").onclick = collapseSidebar;
  $("sessionSearchButton").onclick = () => { $("sessionSearchField").hidden = !$("sessionSearchField").hidden; if (!$("sessionSearchField").hidden) $("sessionSearch").focus(); };
  let searchTimer; $("sessionSearch").oninput = (event) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.sessionsQuery = event.target.value.trim(); loadSessions(); }, 220); };
  $("modelButton").onclick = () => $("modelMenu").hidden ? openModelMenu() : closeModelMenu();
  $("taskPlanToggle").onclick = () => { state.taskPlanExpanded = !state.taskPlanExpanded; renderTaskPlan(); };
  $("voiceButton").onclick = toggleVoice;
  $("attachButton").onclick = () => $("fileInput").click();
  $("fileInput").onchange = (event) => { state.attachments.push(...event.target.files); event.target.value = ""; renderAttachments(); };
  $("newSchedule").onclick = createSchedule;
  qsa("[data-refresh]").forEach((button) => button.onclick = () => button.dataset.refresh === "tasks" ? loadTasks() : loadMedia());
  document.addEventListener("click", async (event) => {
    if (!event.target.closest("#sessionMenu") && !event.target.closest(".session-more")) closeSessionMenu();
    if (!event.target.closest("#modelMenu") && !event.target.closest("#modelButton")) closeModelMenu();
    const remove = event.target.closest("[data-remove-attachment]"); if (remove) { state.attachments.splice(Number(remove.dataset.removeAttachment), 1); renderAttachments(); }
    const task = event.target.closest("[data-task-control]"); if (task) controlTask(task.dataset.taskId, task.dataset.taskControl);
    const approval = event.target.closest("[data-approval]"); if (approval) resolveApproval(approval.dataset.callId, approval.dataset.approval === "approve");
    const schedule = event.target.closest("[data-delete-schedule]"); if (schedule && confirm("حذف هذه المهمة المجدولة؟")) { try { await api(`/admin/schedules/${encodeURIComponent(schedule.dataset.deleteSchedule)}`, { method: "DELETE" }); await loadSchedules(); } catch (error) { toast(error.message, "error"); } }
    const settings = event.target.closest("[data-open-settings]"); if (settings) openSettings(settings.dataset.openSettings);
    const tab = event.target.closest("[data-settings-tab]"); if (tab) { state.settingsPreviousTab = state.settingsTab; state.settingsTab = tab.dataset.settingsTab; state.settingsProviderId = null; state.settingsProviderResult = null; state.settingsProviderDraft = null; state.settingsLocalModelId = null; state.settingsLocalModelResult = null; renderSettings(); }
    const save = event.target.closest("[data-save-settings]"); if (save) saveSettingsSection(save.dataset.saveSettings);
    const provider = event.target.closest("[data-configure-provider]"); if (provider) configureProvider(provider.dataset.configureProvider);
    const providerSettings = event.target.closest("[data-open-provider-settings]"); if (providerSettings) openProviderSettings(providerSettings.dataset.openProviderSettings);
    if (event.target.closest("[data-back-provider-settings]")) { state.settingsProviderId = null; state.settingsProviderResult = null; renderSettings(); }
    const localSettings = event.target.closest("[data-open-local-settings]"); if (localSettings) openLocalModelSettings(localSettings.dataset.openLocalSettings);
    if (event.target.closest("[data-back-local-settings]")) { state.settingsLocalModelId = null; state.settingsLocalModelResult = null; renderSettings(); }
    if (event.target.closest("[data-fetch-settings-provider]")) fetchSettingsProviderModels();
    if (event.target.closest("[data-test-settings-provider]")) fetchSettingsProviderModels({ announce: true });
    const disconnect = event.target.closest("[data-disconnect-provider]"); if (disconnect) disconnectProvider(disconnect.dataset.disconnectProvider);
    const menuProvider = event.target.closest("[data-open-model-provider]"); if (menuProvider) openModelProvider(menuProvider.dataset.openModelProvider);
    if (event.target.closest("[data-model-menu-back]")) { state.modelMenuProviderId = null; state.modelQuery = ""; renderModelMenu(); requestAnimationFrame(() => { positionModelMenu(); $("modelSearch")?.focus(); }); }
    const providerTest = event.target.closest("[data-test-provider]"); if (providerTest) testProvider(providerTest.dataset.testProvider);
    const providerRefresh = event.target.closest("[data-refresh-provider]"); if (providerRefresh) { const providerItem = state.providers.find((item) => providerIdOf(item) === providerRefresh.dataset.refreshProvider); if (providerItem) fetchProviderModels(providerItem, { force: true }).catch((error) => toast(error.message, "error")); }
    const uacOperation = event.target.closest("[data-uac-operation]"); if (uacOperation) requestUacOperation(uacOperation.dataset.uacOperation, uacOperation.dataset.uacTitle || uacOperation.dataset.uacOperation);
    const local = event.target.closest("[data-activate-local]"); if (local) selectModel("local", local.dataset.activateLocal, "");
    const localDelete = event.target.closest("[data-delete-local]"); if (localDelete) deleteLocalModel(localDelete.dataset.deleteLocal);
    if (event.target.closest("[data-import-local]")) importLocalModel();
    if (event.target.closest("[data-calibrate-mic]")) calibrateMicrophone();
    const model = event.target.closest("[data-model-id]"); if (model) selectModel(model.dataset.modelKind, model.dataset.modelId, model.dataset.modelName);
    const command = event.target.closest("[data-command]"); if (command) runCommand(command.dataset.command);
    const action = event.target.closest("[data-session-action]"); if (action) sessionAction(action.dataset.sessionAction);
    if (event.target.closest("[data-toggle-voice]")) toggleVoice();
    if (event.target.closest("[data-run-health]")) { await loadRuntime(); toast("تحدّثت حالة النظام", "success"); renderSettings(); }
  });
  document.addEventListener("input", (event) => {
    if (event.target.id === "modelSearch") { state.modelQuery = event.target.value; renderModelMenu(); requestAnimationFrame(positionModelMenu); }
    if (["settingsProviderBaseUrl", "settingsProviderApiKey", "settingsProviderModel", "settingsProviderCustomModel"].includes(event.target.id)) captureSettingsProviderDraft();
  });
  document.addEventListener("change", (event) => {
    if (event.target.id === "settingsProviderSetActive") captureSettingsProviderDraft();
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openCommandPalette(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") { event.preventDefault(); blankConversation(); }
    if ((event.ctrlKey || event.metaKey) && event.key === ",") { event.preventDefault(); openSettings(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "b") { event.preventDefault(); toggleSidebar(); }
    if (event.key === "Enter" && !$("commandPalette").hidden && document.activeElement === $("commandInput")) { event.preventDefault(); $("commandList").querySelector(".command-item")?.click(); }
    if (event.key === "Escape") { closeDrawers(); closeSessionMenu(); closeCommandPalette(); closeModelMenu(); if (!$("settingsModal").hidden) closeSettings(); }
  });
  addEventListener("beforeunload", saveDraft);
  addEventListener("resize", () => { positionModelMenu(); if (!matchMedia("(max-width: 899px)").matches) closeDrawers(); }, { passive: true });
}

async function boot() {
  bindEvents();
  if (localStorage.getItem("wise:sidebar-collapsed") === "true" && !matchMedia("(max-width: 899px)").matches) $("appShell").classList.add("sidebar-collapsed");
  updateWindowState();
  addEventListener("resize", updateWindowState, { passive: true });
  applyLanguage(document.documentElement.lang);
  blankConversation({ focus: false });
  await Promise.allSettled([loadSessions(), loadModels(), loadRuntime(), loadTasks(), loadApprovals(), loadMedia(), loadSlashCommands()]);
  if (location.hash.startsWith("#settings")) {
    const tab = location.hash.split("/")[1] || "general";
    if (settingsTabs.some(([id]) => id === tab)) await openSettings(tab);
  } else if (location.hash === "#commands") {
    openCommandPalette();
  }
  setInterval(() => Promise.allSettled([loadRuntime(), loadTasks(), loadApprovals()]), 20_000);
}

boot();
