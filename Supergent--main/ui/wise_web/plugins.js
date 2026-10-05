"use strict";
// No authorization URL or credential enters this renderer.
const pluginsState = {query:"", category:"", connected:false, offset:0, next:null, version:0, attempt:null, timer:null, detail:null, busy:false, items:[], observer:null, loading:false};
const pluginsApi = (path="", options={}) => api(`/api/v2/plugins${path}`, {...options, headers:{...options.headers,"X-Wise-Action":"plugins"}});
function brandLogo(name, url, className="") {
  const initials=String(name || "?").split(/[\s()-]+/).filter(Boolean).slice(0,2).map(word=>word[0]).join("").toUpperCase();
  return `<span class="brand-logo ${className}" aria-hidden="true"><span class="brand-logo-fallback">${escapeHtml(initials)}</span>${url ? `<img src="${escapeHtml(url)}" alt="" width="36" height="36" loading="lazy" referrerpolicy="no-referrer">` : ""}</span>`;
}
function bindBrandLogos(root) {
  root.querySelectorAll(".brand-logo img").forEach(img=>{
    const ready=()=>img.parentElement.classList.toggle("is-loaded",img.naturalWidth>0);
    img.onload=ready;
    img.onerror=()=>img.parentElement.classList.remove("is-loaded");
    if(img.complete) ready();
  });
}
const pluginLogo = item => brandLogo(item.name,item.logo,"plugin-logo");
const pluginStatus = status => ({CONNECTED:L("متصل","Connected"), REAUTH_REQUIRED:L("أعد الاتصال","Reconnect required"), DISCONNECTED:L("غير متصل","Not connected"), UNVERIFIED:L("تعذر التحقق من الاتصال","Connection could not be verified")}[status] || L("غير متصل","Not connected"));
function pausePluginPolling() { clearTimeout(pluginsState.timer); pluginsState.timer=null; }
function pluginRoute(id="") { history.pushState(null,"", "#plugins"+(id ? "/"+encodeURIComponent(id) : "")); loadPlugins(); }

async function loadPlugins() {
  const host=$("pluginsContent"); if(!host || state.currentView!=="plugins") return;
  // A debounced library request must not supersede a user navigating into
  // app details. This also protects early clicks while the app is booting.
  clearTimeout(pluginSearchTimer);
  pluginsState.observer?.disconnect();
  pausePluginPolling(); const version=++pluginsState.version;
  if(!pluginsState.attempt) {
    try { const pending=await pluginsApi("/pending"); pluginsState.attempt=pending.attempts[0] || null; }
    catch (_) { /* Catalog shows the actionable service error. */ }
    if(version!==pluginsState.version || state.currentView!=="plugins") return;
  }
  let id=""; try {id=decodeURIComponent(location.hash.split("/")[1] || "");} catch (_) {history.replaceState(null,"","#plugins");}
  if(id) return loadPluginDetail(id,version);
  pluginsState.detail=null; pluginsState.offset=0; pluginsState.items=[]; pluginsState.observer?.disconnect();
  host.innerHTML=`<div class="plugins-library"><header class="plugins-heading"><div><h2>Plugins</h2><p>${L("اربط تطبيقاتك واختر ما يستطيع WISE الوصول إليه.","Connect your apps and choose what WISE can access.")}</p></div><button class="secondary-button" data-plugin-action="refresh">${icon("refresh")}${L("تحديث","Refresh")}</button></header><div class="plugins-filters"><label class="plugins-search">${icon("search")}<span class="sr-only">${L("ابحث عن تطبيق","Search apps")}</span><input id="pluginSearch" type="search" maxlength="300" value="${escapeHtml(pluginsState.query)}" placeholder="${L("ابحث عن تطبيق…","Search apps…")}"></label><label><span class="sr-only">${L("التصنيف","Category")}</span><select id="pluginCategory"><option value="">${L("كل التصنيفات","All categories")}</option></select></label><label class="plugins-connected"><input id="pluginConnected" type="checkbox" ${pluginsState.connected ? "checked" : ""}>${L("المتصلة فقط","Connected only")}</label></div><div id="pluginResults" aria-live="polite" aria-busy="true"><div class="plugins-skeleton" aria-label="${L("جارٍ التحميل","Loading")}"></div></div><footer class="plugins-pagination"><button class="secondary-button" data-plugin-action="previous">${L("السابق","Previous")}</button><span id="pluginCount"></span><button class="secondary-button" data-plugin-action="next">${L("التالي","Next")}</button></footer></div>`;
  const footer=host.querySelector(".plugins-pagination");
  footer.innerHTML=`<span id="pluginCount" aria-live="polite"></span><button class="secondary-button" data-plugin-action="more">${L("عرض المزيد","Show more")}</button>`;
  await loadPluginCatalog(version);
  if(pluginsState.attempt && version===pluginsState.version) {
    const pending=document.createElement("button"); pending.className="secondary-button plugin-pending";
    pending.textContent=L("متابعة ربط الحساب","Continue account connection"); pending.onclick=()=>pluginRoute(pluginsState.attempt.integration);
    host.querySelector(".plugins-heading")?.append(pending);
  }
}
async function loadPluginCatalog(version=++pluginsState.version,refresh=false,append=false) {
  if(state.currentView!=="plugins" || location.hash.split("/")[1]) return;
  const results=$("pluginResults"); if(!results) return; results.setAttribute("aria-busy","true");
  if(!append) pluginsState.offset=0;
  pluginsState.loading=true;
  try {
    const params=new URLSearchParams({query:pluginsState.query.trim(),category:pluginsState.category,connected:pluginsState.connected,offset:pluginsState.offset,limit:24,refresh});
    const result=await pluginsApi("?"+params); if(!results.isConnected || version!==pluginsState.version) return;
    pluginsState.next=result.next_offset;
    pluginsState.items=append ? [...pluginsState.items,...result.items] : result.items;
    const more=document.querySelector('[data-plugin-action="more"]');
    more.disabled=result.next_offset===null; more.hidden=result.next_offset===null;
    $("pluginCount").textContent=result.total ? L(`عرض ${pluginsState.items.length} من ${result.total} تطبيق`, `Showing ${pluginsState.items.length} of ${result.total} apps`) : "";
    $("pluginCategory").innerHTML=`<option value="">${L("كل التصنيفات","All categories")}</option>`+result.categories.map(value=>`<option value="${escapeHtml(value)}" ${value===pluginsState.category ? "selected" : ""}>${escapeHtml(value)}</option>`).join("");
    const notice=!result.configured ? L("تصفّح مكتبة التطبيقات. ربط الحسابات يحتاج إعداد Nango في الخادم أولاً.","Browse the app library. Account connections require Nango backend setup first.") : result.warning ? L("مكتبة التطبيقات متاحة؛ تعذر التحقق من خدمة الاتصال: ","The app library is available; connection service verification failed: ")+result.warning : "";
    results.innerHTML=(notice ? `<p class="plugins-service-note" role="status">${escapeHtml(notice)}</p>` : "")+(pluginsState.items.length ? `<div class="plugins-grid">${pluginsState.items.map(item=>`<article class="plugin-card"><button class="plugin-card-open" data-plugin-id="${escapeHtml(item.id)}" aria-label="${escapeHtml(item.name)}">${pluginLogo(item)}<span class="plugin-card-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.description || item.categories.join(" · "))}</span></span></button><div class="plugin-card-footer"><span>${item.accounts.some(a=>a.status==="CONNECTED") ? pluginStatus("CONNECTED") : item.can_connect ? L("جاهز للربط","Ready to connect") : L("يحتاج إعداداً","Setup required")}</span><button class="secondary-button" data-plugin-id="${escapeHtml(item.id)}">${item.accounts.length ? L("إدارة","Manage") : L("عرض","View")}</button></div></article>`).join("")}</div>` : `<div class="plugins-empty"><h3>${L("لا توجد تطبيقات مطابقة","No matching apps")}</h3><p>${L("جرّب اسماً آخر أو امسح التصنيف للبحث في المكتبة كاملة.","Try another name or clear the category to search the whole library.")}</p><button class="secondary-button" data-plugin-action="reset">${L("مسح البحث","Clear filters")}</button></div>`);
    bindBrandLogos(results);
    pluginsState.observer?.disconnect();
    if(result.next_offset!==null && typeof IntersectionObserver!=="undefined") {
      pluginsState.observer=new IntersectionObserver(entries=>{
        if(entries.some(entry=>entry.isIntersecting) && !pluginsState.loading && state.currentView==="plugins" && !pluginsState.detail) loadMorePlugins();
      },{root:$("pluginsView"),rootMargin:"120px",threshold:0});
      pluginsState.observer.observe(more);
    }
  } catch(error) {if(results.isConnected && version===pluginsState.version) results.innerHTML=`<div class="plugins-empty" role="alert"><h3>${L("تعذر تحميل التطبيقات","Could not load apps")}</h3><p>${escapeHtml(error.message)}</p><button class="secondary-button" data-plugin-action="refresh">${L("إعادة المحاولة","Retry")}</button></div>`;}
  finally {if(results.isConnected && version===pluginsState.version) {results.setAttribute("aria-busy","false"); pluginsState.loading=false;}}
}
async function loadPluginDetail(id,version) {
  const host=$("pluginsContent");
  pluginsState.observer?.disconnect();
  host.innerHTML=`<div class="plugin-detail"><button class="secondary-button" data-plugin-action="back">${L("العودة إلى Plugins","Back to Plugins")}</button><div class="plugins-skeleton"></div></div>`;
  try {
    const item=await pluginsApi(`/integrations/${encodeURIComponent(id)}`); if(version!==pluginsState.version || state.currentView!=="plugins") return;
    pluginsState.detail=item;
    host.innerHTML=`<div class="plugin-detail"><button class="secondary-button plugin-back" data-plugin-action="back">${L("العودة إلى Plugins","Back to Plugins")}</button><header class="plugin-detail-heading">${pluginLogo(item)}<div><h2 tabindex="-1">${escapeHtml(item.name)}</h2><p>${escapeHtml(item.categories.join(" · "))}</p></div></header><p class="plugin-description">${escapeHtml(item.description)}</p><section class="plugin-capabilities"><h3>${L("ما يستطيع WISE فعله","What WISE can do")}</h3>${item.tools.length ? `<ul>${item.tools.map(tool=>`<li>${icon("check")}${escapeHtml(tool)}</li>`).join("")}</ul>` : `<p>${L("يمكن ربط الحساب وإدارته. أدوات هذا التطبيق لم تُنفّذ بعد.","Account connection and management are available. Agent tools for this app are not implemented yet.")}</p>`}<p class="plugin-permission-note">${L("الاتصال ليس إذنًا مفتوحًا. الكتابة والإرسال والحذف تحتاج أذونات ومحوّلات منفصلة.","Connecting is not unrestricted permission. Write, send and delete actions need separate adapters and approvals.")}</p></section><section class="plugin-accounts"><h3>${L("الحسابات المتصلة","Connected accounts")}</h3>${item.accounts.map(a=>`<div class="plugin-account"><div><strong>${escapeHtml(a.label)}</strong><span>${pluginStatus(a.status)}</span></div><div class="plugin-account-actions"><button class="secondary-button" data-plugin-action="reconnect" data-account="${escapeHtml(a.id)}">${L("إعادة الاتصال","Reconnect")}</button><button class="secondary-button" data-plugin-action="disconnect" data-account="${escapeHtml(a.id)}">${L("فصل","Disconnect")}</button></div></div>`).join("") || `<p>${L("لا يوجد حساب متصل.","No connected accounts.")}</p>`}</section><div class="plugin-connect-row"><button class="primary-button" data-plugin-action="connect">${L(item.accounts.length ? "ربط حساب آخر" : "ربط الحساب",item.accounts.length ? "Connect another account" : "Connect account")}</button><span>${escapeHtml(item.auth_mode || "")}</span></div><div id="pluginAuthState" role="status" aria-live="polite"></div></div>`;
    bindBrandLogos(host);
    if(!item.can_connect) {
      host.querySelector('[data-plugin-action="connect"]').disabled=true;
      const note=document.createElement("p"); note.className="plugins-service-note";
      note.textContent=L("هذا التطبيق موجود في مكتبة Nango. أضفه إلى بيئة Nango في الخادم لتفعيل ربط الحساب. أدوات الوكيل ليست متاحة بمجرد إدراج التطبيق.","This app is in the Nango library. Configure it in your backend Nango environment to enable account connection. Catalog inclusion does not provide agent tools.");
      host.querySelector(".plugin-connect-row").after(note);
      const capabilityText=host.querySelector(".plugin-capabilities > p");
      if(capabilityText) capabilityText.textContent=L("لم تُفعّل أدوات الوكيل لهذا التطبيق بعد.","Agent tools for this app are not enabled yet.");
    }
    if(/^https:\/\/(docs\.)?nango\.dev\//.test(item.docs_url || "")) {
      const link=document.createElement("a"); link.className="plugin-docs"; link.href=item.docs_url;
      link.textContent=L("دليل إعداد التطبيق","App setup guide"); link.dataset.pluginDocs=item.docs_url;
      host.querySelector(".plugin-connect-row").append(link);
    }
    host.querySelector("h2")?.focus({preventScroll:true});
    if(pluginsState.attempt?.integration===id) resumePluginPolling();
  } catch(error) {if(version===pluginsState.version) host.innerHTML=`<div class="plugin-detail"><button class="secondary-button" data-plugin-action="back">${L("العودة إلى Plugins","Back to Plugins")}</button><p role="alert">${escapeHtml(error.message)}</p><button class="secondary-button" data-plugin-id="${escapeHtml(id)}">${L("إعادة المحاولة","Retry")}</button></div>`;}
}
function renderPluginAuth(message,pending=true) {
  const node=$("pluginAuthState"); if(!node) return;
  node.innerHTML=`<p>${escapeHtml(message)}</p>${pending ? `<div class="plugin-account-actions"><button class="secondary-button" data-plugin-action="launch">${L("فتح المتصفح مجددًا","Open browser again")}</button><button class="secondary-button" data-plugin-action="cancel">${L("إلغاء","Cancel")}</button></div>` : ""}`;
}
async function resumePluginPolling() {
  pausePluginPolling(); const attempt=pluginsState.attempt; if(!attempt || state.currentView!=="plugins") return;
  try {
    const result=await pluginsApi(`/attempts/${encodeURIComponent(attempt.id)}`); if(pluginsState.attempt!==attempt) return;
    if(result.status==="PENDING") {renderPluginAuth(L("أكمل الربط في متصفح النظام. سيتحقق WISE من الاتصال تلقائيًا.","Complete authorization in your system browser. WISE will verify the connection automatically.")); pluginsState.timer=setTimeout(resumePluginPolling,2500);}
    else {pluginsState.attempt=null; if(result.status==="CONNECTED") {toast(L("تم ربط الحساب","Account connected")); loadPlugins();} else renderPluginAuth(result.status==="EXPIRED" ? L("انتهت مهلة المصادقة؛ أعد المحاولة.","Authorization expired. Connect again.") : result.status==="CANCELLED" ? L("أُلغيت المصادقة.","Authorization cancelled.") : L("تعذرت المصادقة؛ تحقق من الخدمة وأعد المحاولة.","Authorization failed. Check the service and try again."),false);}
  } catch(error) {renderPluginAuth(error.message); if(pluginsState.attempt===attempt && state.currentView==="plugins") pluginsState.timer=setTimeout(resumePluginPolling,8000);}
}
let pluginSearchTimer;
function loadMorePlugins() {
  if(pluginsState.loading || pluginsState.next===null) return;
  pluginsState.offset=pluginsState.next;
  return loadPluginCatalog(++pluginsState.version,false,true);
}
document.addEventListener("input",event=>{if(event.target.id!=="pluginSearch") return; pluginsState.query=event.target.value; pluginsState.offset=0; clearTimeout(pluginSearchTimer); pluginSearchTimer=setTimeout(()=>loadPluginCatalog(),250);});
document.addEventListener("change",event=>{if(!["pluginCategory","pluginConnected"].includes(event.target.id)) return; if(event.target.id==="pluginCategory") pluginsState.category=event.target.value; else pluginsState.connected=event.target.checked; pluginsState.offset=0; loadPluginCatalog();});
document.addEventListener("click",async event=>{
  const button=event.target.closest("#pluginsView button"); if(!button) return;
  if(button.dataset.pluginId) return pluginRoute(button.dataset.pluginId);
  const action=button.dataset.pluginAction;
  if(action==="back") return pluginRoute();
  if(action==="more") return loadMorePlugins();
  if(action==="reset") {pluginsState.query=""; pluginsState.category=""; pluginsState.connected=false; return loadPlugins();}
  if(action==="refresh") return loadPluginCatalog(++pluginsState.version,true);
  if(action==="previous" || action==="next") {pluginsState.offset=action==="next" ? pluginsState.next : Math.max(0,pluginsState.offset-24); return loadPluginCatalog();}
  if(pluginsState.busy) return; pluginsState.busy=true; button.disabled=true;
  try {
    if(action==="connect" || action==="reconnect") {
      if(pluginsState.attempt) throw new Error(L("ألغِ محاولة الربط الحالية أولًا.","Cancel the current authorization first."));
      const item=pluginsState.detail;
      if(!item?.can_connect) throw new Error(L("أعدّ هذا التكامل في Nango أولاً.","Configure this integration in Nango first."));
      const result=await pluginsApi(`/integrations/${encodeURIComponent(item.id)}/connect`,{method:"POST",body:JSON.stringify({account:button.dataset.account || null})});
      pluginsState.attempt={id:result.id,integration:item.id}; renderPluginAuth(result.browser_opened ? L("بانتظار الربط في المتصفح…","Waiting for browser authorization…") : L("تعذر فتح المتصفح. اضغط فتح المتصفح مجددًا.","Browser could not open. Select Open browser again.")); resumePluginPolling();
    } else if(action==="cancel" && pluginsState.attempt) {await pluginsApi(`/attempts/${pluginsState.attempt.id}/cancel`,{method:"POST"}); pausePluginPolling(); pluginsState.attempt=null; renderPluginAuth(L("أُلغيت المصادقة.","Authorization cancelled."),false);}
    else if(action==="launch" && pluginsState.attempt) {const result=await pluginsApi(`/attempts/${pluginsState.attempt.id}/launch`,{method:"POST"}); if(!result.browser_opened) throw new Error(L("تعذر فتح المتصفح الافتراضي للنظام.","Could not open your default system browser."));}
    else if(action==="disconnect") {if(!confirm(L("فصل الحساب وإزالة اتصال Nango؟","Disconnect this account and remove its Nango connection?"))) return; await pluginsApi(`/accounts/${encodeURIComponent(button.dataset.account)}`,{method:"DELETE"}); toast(L("تم فصل الحساب","Account disconnected")); loadPlugins();}
  } catch(error) {renderPluginAuth(error.message,!!pluginsState.attempt); toast(error.message,"error");}
  finally {pluginsState.busy=false; if(button.isConnected) button.disabled=false;}
});
addEventListener("popstate",()=>{if(location.hash.startsWith("#plugins")) showView("plugins"); else if(state.currentView==="plugins") showView("chat");});
document.addEventListener("click",async event=>{
  const link=event.target.closest("[data-plugin-docs]"); if(!link) return;
  event.preventDefault();
  if(window.pywebview?.api?.open_external_url) {
    if(!await window.pywebview.api.open_external_url(link.dataset.pluginDocs)) toast(L("تعذر فتح المتصفح.","Could not open browser."),"error");
  } else window.open(link.dataset.pluginDocs,"_blank","noopener,noreferrer");
});
