"use strict";
let messagingVersion=0, selectedMessagingService=null;
async function loadMessaging() {
  const host=$("messagingContent"), version=++messagingVersion;
  host.innerHTML=`<p role="status">${L("جارٍ تحميل التطبيقات…","Loading apps…")}</p>`;
  try {
    const [result,channels]=await Promise.all([api("/api/v2/messaging/services"),api("/admin/channels")]);
    if(version!==messagingVersion || state.currentView!=="messaging") return;
    state.messagingServices=result.services || []; state.channels=channels.configured || [];
    if(state.channels.length) {
      await api("/api/v2/messaging/conversations/sync",{method:"POST",headers:{"X-Wise-Action":"plugins"},body:"{}"});
      await loadSessions();
      if(version!==messagingVersion || state.currentView!=="messaging") return;
    }
    host.innerHTML=`<div class="messaging-layout"><section class="messaging-browser"><label class="plugins-search">${icon("search")}<input id="messagingSearch" type="search" autocomplete="off" placeholder="${L("ابحث عن تطبيق…","Search apps…")}" aria-label="${L("ابحث عن تطبيق","Search apps")}"></label><div id="messagingApps" role="list"></div></section><section id="messagingEditor" class="messaging-editor"></section></div><section class="messaging-connections"><h3>${L("اتصالاتك","Your connections")}</h3><div id="messagingConnections"></div></section>`;
    $("messagingSearch").oninput=renderMessagingApps;
    renderMessagingApps(); renderMessagingConnections();
    if(!state.messagingServices.some(app=>app.id===selectedMessagingService)) selectedMessagingService=state.messagingServices[0]?.id;
    if(selectedMessagingService) renderMessagingEditor(selectedMessagingService);
    else $("messagingEditor").innerHTML=`<p>${L("موصلات المراسلة غير متاحة. ثبّت اعتمادية Apprise من إعدادات النظام.","Messaging connectors are unavailable. Install the Apprise dependency in system settings.")}</p>`;
  } catch(error) {if(version===messagingVersion) host.innerHTML=`<p role="alert">${escapeHtml(error.message)}</p><button class="secondary-button" data-messaging-retry>${L("إعادة المحاولة","Retry")}</button>`;}
}

function renderMessagingApps() {
  const query=$("messagingSearch").value.trim().toLowerCase();
  const apps=state.messagingServices.filter(app=>`${app.name} ${app.id}`.toLowerCase().includes(query));
  $("messagingApps").innerHTML=apps.map(app=>`<button type="button" class="messaging-app" aria-pressed="${selectedMessagingService===app.id}" data-messaging-service="${escapeHtml(app.id)}">${brandLogo(app.name,app.icon_url,"messaging-logo")}<span>${escapeHtml(app.name)}</span></button>`).join("") || `<p role="status">${L("لا يوجد تطبيق مطابق.","No matching app.")}</p>`;
  bindBrandLogos($("messagingApps"));
}

function renderMessagingConnections() {
  const host=$("messagingConnections"); if(!host) return;
  const rows=state.channels.map(channel=>({...channel,app:state.messagingServices.find(app=>app.protocols.includes(channel.scheme))})).filter(row=>row.app);
  host.innerHTML=rows.map(item=>`<div class="messaging-connection"><span>${brandLogo(item.app.name,item.app.icon_url,"messaging-logo")}<span><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.app.name)} · ${L("محفوظ؛ الإرسال غير متحقق","Saved; delivery not verified")}</small></span></span><div class="inline-actions"><button class="secondary-button" data-messaging-open="${escapeHtml(item.name)}">${L("المحادثة","Conversation")}</button><button class="quiet-icon" data-messaging-edit="${escapeHtml(item.name)}" data-service="${escapeHtml(item.app.id)}" aria-label="${L("تعديل الاتصال","Edit connection")}">${icon("edit")}</button><button class="quiet-icon" data-messaging-remove="${escapeHtml(item.name)}" aria-label="${L("إزالة الاتصال","Remove connection")}">${icon("trash")}</button></div></div>`).join("") || `<p>${L("لم تربط تطبيقًا بعد.","No apps connected yet.")}</p>`;
  bindBrandLogos(host);
}

function renderMessagingEditor(id,connection="") {
  const app=state.messagingServices.find(item=>item.id===id); if(!app) return;
  selectedMessagingService=id; renderMessagingApps();
  const labels={bot_token:["توكن البوت","Bot token"],targets:["معرّفات المستلمين (افصل بفاصلة)","Recipient IDs (comma separated)"],webhook_id:["معرّف Webhook","Webhook ID"],webhook_token:["توكن Webhook","Webhook token"]};
  const fields=app.fields.map(field=>{
    const label=labels[field.id] ? L(...labels[field.id]) : field.label;
    const attrs=`id="messaging-${escapeHtml(field.id)}" name="${escapeHtml(field.id)}" ${field.required ? "required" : ""}`;
    return `<div class="messaging-field"><label for="messaging-${escapeHtml(field.id)}">${escapeHtml(label)}</label>${field.choices.length ? `<select ${attrs}>${field.choices.map(value=>`<option>${escapeHtml(value)}</option>`).join("")}</select>` : `<input ${attrs} type="${field.secret ? "password" : "text"}" autocomplete="${field.secret ? "new-password" : "off"}" dir="ltr" maxlength="4096" value="${escapeHtml(field.secret ? "" : field.default ?? "")}">`}</div>`;
  }).join("");
  $("messagingEditor").innerHTML=`<header class="messaging-detail-heading">${brandLogo(app.name,app.icon_url,"plugin-logo")}<span><h3>${escapeHtml(app.name)}</h3><p>${L("إرسال عبر البوت أو موصل الخدمة","Send through the service's bot or connector")}</p></span></header><p class="messaging-intro">${L("أدخل بيانات موصلك. ستظهر محادثته باسم التطبيق في الشريط الجانبي. هذا الموصل للإرسال فقط؛ لا يستقبل رسائل التطبيق.","Enter your connector details. Its conversation will appear with the app name in the sidebar. This connector sends only; it does not receive the app's messages.")}</p><a class="plugin-docs" href="${escapeHtml(app.setup_url)}" data-messaging-setup="${escapeHtml(app.setup_url)}">${L("دليل الإعداد","Setup guide")}</a><form id="messagingConnectionForm" class="connection-form" data-service="${escapeHtml(id)}"><div class="messaging-field"><label for="messagingConnectionName">${L("اسم الاتصال","Connection name")}</label><input id="messagingConnectionName" pattern="[A-Za-z][A-Za-z0-9_]{0,63}" required value="${escapeHtml(connection || id.replace(/[^\w]/g,"_"))}" ${connection ? "readonly" : ""}></div>${fields}<footer><button class="primary-button" type="submit">${L(connection ? "حفظ التغييرات" : "حفظ الاتصال",connection ? "Save changes" : "Save connection")}</button><span>${L("لن تُرسل رسالة عند الحفظ.","Saving does not send a message.")}</span></footer><p id="messagingFeedback" role="status"></p></form>`;
  bindBrandLogos($("messagingEditor"));
}

async function saveMessagingConnection(event) {
  event.preventDefault(); const form=event.target, button=form.querySelector('[type="submit"]'); button.disabled=true;
  try {
    const name=$("messagingConnectionName").value.trim();
    if(state.channels.some(row=>row.name===name.toLowerCase()) && !confirm(L(`استبدال بيانات اتصال «${name}»؟`, `Replace the credentials for “${name}”?`))) return;
    await api("/api/v2/messaging/connections",{method:"POST",headers:{"X-Wise-Action":"plugins"},body:JSON.stringify({service:form.dataset.service,name,fields:Object.fromEntries(new FormData(form))})});
    form.querySelectorAll('input[type="password"]').forEach(node=>node.value="");
    $("messagingFeedback").textContent=L("حُفظ الاتصال. محادثته متاحة في الشريط الجانبي.","Connection saved. Its conversation is available in the sidebar.");
    await Promise.all([refreshMessagingConnections(),loadSessions()]);
  } catch(error) {if(form.isConnected) $("messagingFeedback").textContent=error.message;}
  finally {button.disabled=false;}
}
async function refreshMessagingConnections() {
  if(state.currentView!=="messaging" || document.hidden) return;
  const result=await api("/admin/channels"); state.channels=result.configured || []; renderMessagingConnections();
}
document.addEventListener("submit",event=>{if(event.target.id==="messagingConnectionForm") saveMessagingConnection(event);});
document.addEventListener("click",async event=>{
  const service=event.target.closest("[data-messaging-service]"); if(service) renderMessagingEditor(service.dataset.messagingService);
  const edit=event.target.closest("[data-messaging-edit]"); if(edit) renderMessagingEditor(edit.dataset.service,edit.dataset.messagingEdit);
  const setup=event.target.closest("[data-messaging-setup]");
  if(setup) {
    event.preventDefault();
    if(window.pywebview?.api?.open_external_url) {if(!await window.pywebview.api.open_external_url(setup.dataset.messagingSetup)) toast(L("تعذر فتح المتصفح.","Could not open browser."),"error");}
    else window.open(setup.dataset.messagingSetup,"_blank","noopener,noreferrer");
  }
  const open=event.target.closest("[data-messaging-open]");
  if(open) {await loadSessions(); const row=state.sessions.find(item=>item.metadata?.channel===open.dataset.messagingOpen); if(row) openSession(sessionId(row),sessionTitle(row)); else toast(L("أعد حفظ الاتصال لإنشاء محادثته.","Save the connection again to create its conversation."),"error");}
  const remove=event.target.closest("[data-messaging-remove]");
  if(remove && confirm(L("إزالة الاتصال؟ سيبقى سجل المحادثة محفوظًا.","Remove this connection? Its conversation history will be kept."))) {
    try {await api(`/admin/channels/${encodeURIComponent(remove.dataset.messagingRemove)}`,{method:"DELETE"}); await refreshMessagingConnections();}
    catch(error) {toast(error.message,"error");}
  }
  if(event.target.closest("[data-messaging-retry]")) loadMessaging();
});
addEventListener("online",()=>refreshMessagingConnections().catch(()=>{}));
setInterval(()=>refreshMessagingConnections().catch(()=>{}),30_000);
