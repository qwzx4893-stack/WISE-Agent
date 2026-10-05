"use strict";

async function loadMessaging() {
  const host = $("messagingContent");
  host.innerHTML = `<p role="status">${L("جارٍ تحميل التطبيقات…", "Loading apps…")}</p>`;
  try {
    const [result, channels] = await Promise.all([api("/api/v2/messaging/services"), api("/admin/channels")]);
    state.messagingServices = result.services || []; state.channels = channels.configured || [];
    host.innerHTML = `<div class="messaging-layout"><section class="messaging-browser"><label>${L("ابحث عن تطبيق", "Find an app")}<input id="messagingSearch" type="search" autocomplete="off"></label><div id="messagingApps" role="list"></div></section><section id="messagingEditor" class="messaging-editor"><p>${L("اختر تطبيقاً لعرض حقول الاتصال الخاصة به.", "Choose an app to see its connection fields.")}</p></section></div><section class="messaging-connections"><h3>${L("الاتصالات المحفوظة", "Saved connections")}</h3>${state.channels.map(item => `<div class="setting-row"><div><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.scheme)} — ${L("محفوظ؛ لم يُتحقق من الإرسال", "Saved; delivery unverified")}</small></div><button class="secondary-button" data-messaging-remove="${escapeHtml(item.name)}">${L("إزالة", "Remove")}</button></div>`).join("") || `<p>${L("لا توجد اتصالات محفوظة.", "No saved connections.")}</p>`}</section>`;
    $("messagingSearch").oninput = renderMessagingApps; renderMessagingApps();
  } catch (error) { host.replaceChildren(); const node = document.createElement("p"); node.textContent = error.message; host.append(node); }
}

function renderMessagingApps() {
  const query = $("messagingSearch").value.trim().toLowerCase();
  const apps=state.messagingServices.filter(app => `${app.name} ${app.id}`.toLowerCase().includes(query));
  $("messagingApps").innerHTML = apps.map(app => `<button type="button" class="messaging-app" data-messaging-service="${escapeHtml(app.id)}">${brandLogo(app.name,app.icon_url,"messaging-logo")}<span>${escapeHtml(app.name)}</span></button>`).join("") || `<p role="status">${L("لا توجد تطبيقات مطابقة؛ جرّب اسماً آخر.","No matching apps. Try another name.")}</p>`;
  bindBrandLogos($("messagingApps"));
  $("messagingApps").querySelectorAll("[data-messaging-service]").forEach(button => button.onclick = () => renderMessagingEditor(button.dataset.messagingService));
}

function renderMessagingEditor(id) {
  const app = state.messagingServices.find(item => item.id === id);
  if (!app) return;
  const labels = {bot_token:["توكن البوت", "Bot token"], targets:["المستلمون (افصل بفاصلة)", "Recipients (comma separated)"], webhook_id:["معرّف Webhook", "Webhook ID"], webhook_token:["توكن Webhook", "Webhook token"]};
  const fields = app.fields.map(field => {
    const label = labels[field.id] ? L(...labels[field.id]) : field.label;
    const attrs = `id="messaging-${escapeHtml(field.id)}" name="${escapeHtml(field.id)}" ${field.required ? "required" : ""}`;
    return `<label for="messaging-${escapeHtml(field.id)}">${escapeHtml(label)}</label>${field.choices.length ? `<select ${attrs}>${field.choices.map(value => `<option>${escapeHtml(value)}</option>`).join("")}</select>` : `<input ${attrs} type="${field.secret ? "password" : "text"}" autocomplete="off" dir="ltr" maxlength="4096" value="${escapeHtml(field.default ?? "")}">`}`;
  }).join("");
  $("messagingEditor").innerHTML = `<h3>${escapeHtml(app.name)}</h3><p>${L("إرسال رسائل وإشعارات عبر الحساب أو Webhook الخاص بك. استقبال المحادثات غير مدعوم بهذا الموصل.", "Send messages and notifications using your credentials or webhook. This connector does not receive conversations.")}</p><a href="${escapeHtml(app.setup_url)}" data-messaging-setup="${escapeHtml(app.setup_url)}">${L("فتح تعليمات المصادقة الرسمية", "Open official authentication instructions")}</a><form id="messagingConnectionForm" class="connection-form" data-service="${escapeHtml(id)}"><label for="messagingConnectionName">${L("اسم الاتصال", "Connection name")}</label><input id="messagingConnectionName" pattern="[A-Za-z][A-Za-z0-9_]{0,63}" required value="${escapeHtml(id.replace(/[^\w]/g, "_"))}">${fields}<button class="primary-button" type="submit">${L("حفظ الاتصال", "Save connection")}</button><p id="messagingFeedback" role="status"></p></form>`;
}

async function saveMessagingConnection(event) {
  event.preventDefault(); const form = event.target, button = form.querySelector('[type="submit"]'); button.disabled = true;
  const fields = Object.fromEntries(new FormData(form));
  try {
    await api("/api/v2/messaging/connections", {method:"POST", body:JSON.stringify({service:form.dataset.service, name:$("messagingConnectionName").value.trim(), fields})});
    form.querySelectorAll('input[type="password"]').forEach(node => node.value = "");
    toast(L("حُفظ الاتصال. لم تُرسل أي رسالة.", "Connection saved. No message was sent."), "success"); await loadMessaging();
  } catch (error) { $("messagingFeedback").textContent = error.message; }
  finally { button.disabled = false; }
}

document.addEventListener("submit", event => { if (event.target.id === "messagingConnectionForm") saveMessagingConnection(event); });
document.addEventListener("click", async event => {
  const setup = event.target.closest("[data-messaging-setup]");
  if (setup) {
    event.preventDefault();
    if (window.pywebview?.api?.open_external_url) {
      if (!await window.pywebview.api.open_external_url(setup.dataset.messagingSetup)) toast(L("تعذر فتح المتصفح.", "Could not open browser."), "error");
    } else window.open(setup.dataset.messagingSetup, "_blank", "noopener,noreferrer");
  }
  const remove = event.target.closest("[data-messaging-remove]");
  if (remove && confirm(L("إزالة هذا الاتصال؟", "Remove this connection?"))) {
    try { await api(`/admin/channels/${encodeURIComponent(remove.dataset.messagingRemove)}`, {method:"DELETE"}); await loadMessaging(); }
    catch (error) { toast(error.message, "error"); }
  }
  if (event.target.closest("#refreshMessaging")) await loadMessaging();
});
