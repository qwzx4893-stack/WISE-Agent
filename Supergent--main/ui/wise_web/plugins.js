"use strict";
// Default store: service connections. Packages remain on their explicit route;
// neither surface exposes the agent's native tools or skill inventory.
const pluginsState={mode:"services",query:"",category:"",source:"",offset:0,next:null,version:0,attempt:null,timer:null,detail:null,busy:false,items:[],loading:false};
const pluginsApi=(path="",options={})=>api(`/api/v2/plugins${path}`,{...options,headers:{...options.headers,"X-Wise-Action":"plugins"}});
function brandLogo(name,url,className="") {
  const initials=String(name||"?").split(/[\s()-]+/).filter(Boolean).slice(0,2).map(w=>w[0]).join("").toUpperCase();
  return `<span class="brand-logo ${className}" aria-hidden="true"><span class="brand-logo-fallback">${escapeHtml(initials)}</span>${url?`<img src="${escapeHtml(url)}" alt="" width="36" height="36" loading="lazy" referrerpolicy="no-referrer">`:""}</span>`;
}
function bindBrandLogos(root) {
  root.querySelectorAll(".brand-logo img").forEach(img=>{
    const ready=()=>img.parentElement.classList.toggle("is-loaded",img.naturalWidth>0);
    img.onload=ready;img.onerror=()=>{
      img.parentElement.classList.remove("is-loaded");img.dataset.retrySrc=img.getAttribute("src").split('?wise_retry=')[0];
      const retries=Number(img.dataset.retries||0);if(retries<2){img.dataset.retries=String(retries+1);setTimeout(()=>{if(img.isConnected&&navigator.onLine)img.src=img.dataset.retrySrc+'?wise_retry='+(retries+1);},1500*(retries+1));}
    };
    if(img.complete)ready();
  });
}
const pluginLogo=item=>brandLogo(item.display_name||item.name,item.logo||(!item.id.startsWith('pkg_')?'/api/v2/plugins/logos/'+encodeURIComponent(item.id):''),"plugin-logo");
const pluginBack=()=>`<button class="icon-button plugin-back" data-plugin-action="back" aria-label="${L("العودة","Back")}">${icon("arrow-up")}</button>`;
const pluginStatus=s=>({ENABLED:L("مثبتة","Installed"),CONNECTED:L("متصل","Connected"),AVAILABLE:L("متاحة","Available"),DISABLED:L("معطّلة","Disabled"),FAILED:L("تعذر الاتصال","Connection failed"),AUTH_REQUIRED:L("يحتاج مصادقة","Authorization required"),INSTALLED:L("غير متصل","Not connected")}[s]||s);
const pluginGroupLabel=name=>({'All MCPs':L('كل MCPs','All MCPs'),Featured:L('الأبرز','Featured'),Development:L('التطوير','Development'),Security:L('الأمان','Security'),Design:L('التصميم','Design'),Research:L('البحث','Research'),'Data & AI':L('البيانات والذكاء الاصطناعي','Data & AI'),Automation:L('الأتمتة','Automation'),Productivity:L('الإنتاجية','Productivity'),Databases:L('قواعد البيانات','Databases'),Business:L('الأعمال','Business'),'Local apps':L('تطبيقات الجهاز','Local apps'),'Local packages':L('حزم محلية','Local packages'),'More plugins':L('إضافات أخرى','More plugins')}[name]||name);
const connectionType=item=>item.connection_type==='manual'?L('إعداد خارجي','External setup'):item.connection_type==='local'?L('تطبيق على جهازك','Application on your computer'):item.local_execution||['bundle','local-package'].includes(item.connection_type)?L('حزمة محلية','Local package'):L('خدمة سحابية','Cloud service');
let pluginSearchTimer;
let pluginScrollObserver;
function pausePluginPolling(){clearTimeout(pluginsState.timer);pluginsState.timer=null;pluginScrollObserver?.disconnect();}
function pluginCard(item){return `<button class="plugin-card plugin-card-open" data-plugin-id="${escapeHtml(item.id)}">${pluginLogo(item)}<span class="plugin-card-copy"><strong>${escapeHtml(item.display_name||item.name)}</strong><span>${escapeHtml(item.description)}</span><small>${item.source==='community'?L('المجتمع','Community')+' · ':''}${item.connection_type?connectionType(item):escapeHtml(item.publisher||item.marketplace)}${item.installed||item.status==='CONNECTED'?` · ${pluginStatus(item.status)}`:""}</small></span></button>`;}
function watchPluginScroll(){
  pluginScrollObserver?.disconnect();const sentinel=$("pluginScrollSentinel");if(!sentinel||pluginsState.next===null)return;
  pluginScrollObserver=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting))loadMorePlugins();},{rootMargin:"320px"});pluginScrollObserver.observe(sentinel);
}
function pluginRoute(id=""){
  const prefix=pluginsState.mode==='packages'?'#plugins/packages':'#plugins';
  const atCategory=location.hash.includes('/category/');
  history.pushState(null,'',id?(id.startsWith('pkg_')?'#plugins/'+encodeURIComponent(id):'#plugins/service/'+encodeURIComponent(id)):prefix+(!atCategory&&pluginsState.category?'/category/'+encodeURIComponent(pluginsState.category):''));loadPlugins();
}
async function loadPlugins(){
  const host=$("pluginsContent");if(!host||state.currentView!=="plugins")return;
  clearTimeout(pluginSearchTimer);pausePluginPolling();const version=++pluginsState.version;
  const parts=location.hash.split('/');let id="";
  try{
    pluginsState.mode=parts[1]==='packages'||parts[1]?.startsWith('pkg_')?'packages':'services';
    id=decodeURIComponent(parts[1]==='service'?parts[2]||'':parts[1]==='packages'?parts[2]||'':parts[1]||'');
  }catch(_){history.replaceState(null,"","#plugins");}
  if(id&&id!=="category")return loadPluginDetail(id,version);
  if(id==="category"){try{pluginsState.category=decodeURIComponent(parts[pluginsState.mode==='packages'?3:2]||'');}catch(_){pluginsState.category='';}}else pluginsState.category='';
  pluginsState.detail=null;pluginsState.items=[];pluginsState.offset=0;
  host.innerHTML=`<div class="plugins-library">${pluginsState.category?pluginBack():''}<header class="plugins-heading"><div><h2>${escapeHtml(pluginsState.category||'MCP Market')}</h2><p>${L("اتصل بخدمات الشركات وخوادم المجتمع.","Connect company services and community servers.")}</p></div>${!pluginsState.category?`<button class="secondary-button market-browse" data-plugin-category="All MCPs">${L('تصفح الكل','Browse all')}</button>`:''}</header><div class="plugins-filters"><label class="plugins-search">${icon("search")}<span class="sr-only">${L("البحث","Search")}</span><input id="pluginSearch" type="search" maxlength="300" value="${escapeHtml(pluginsState.query)}" placeholder="${L("ابحث عن MCP أو ناشر…","Search MCPs or publishers…")}"></label><span id="marketCount" class="market-count"></span></div><p id="pluginDirectoryState" class="plugins-service-note" aria-live="polite"></p><div id="pluginResults" aria-busy="true"><div class="plugins-skeleton"></div></div><div id="pluginScrollSentinel" aria-hidden="true"></div></div>`;
  if(pluginsState.category)host.querySelector('.plugins-heading h2').textContent=pluginGroupLabel(pluginsState.category);
  await loadPluginCatalog(version);
}
async function loadPluginCatalog(version=++pluginsState.version,refresh=false,append=false){
  if(state.currentView!=="plugins"||pluginsState.detail)return;
  const results=$("pluginResults");if(!results)return;
  results.setAttribute("aria-busy","true");pluginsState.loading=true;if(!append)pluginsState.offset=0;
  try{
    const params=new URLSearchParams({query:pluginsState.query.trim(),category:pluginsState.category,source:pluginsState.source,offset:pluginsState.offset,limit:48,refresh});
    const r=await pluginsApi((pluginsState.mode==='services'?'/connections':'')+"?"+params);if(version!==pluginsState.version||!results.isConnected)return;
    if($('marketCount'))$('marketCount').textContent=Number(r.total).toLocaleString(document.documentElement.lang)+' MCPs';
    pluginsState.next=r.next_offset;pluginsState.items=append?[...pluginsState.items,...r.items]:r.items;
    const home=!pluginsState.query.trim()&&!pluginsState.category;
    if(home)pluginsState.next=null;
    $("pluginDirectoryState").textContent=pluginsState.mode==='services'?(r.warning?L('الفهرس المحفوظ متاح. يُستكمل البحث عبر الإنترنت عند الاتصال.','Cached listings are available. Online search supplements the catalog.'):r.syncing?L('جارٍ تحديث دليل MCP تلقائيًا…','Updating the MCP directory automatically…'):''):r.warning||(r.syncing?L("جارٍ تحديث فهارس المتاجر…","Updating marketplace catalogs…"):"");
    results.innerHTML=pluginsState.items.length?(home?`<div class="plugin-sections">${(r.sections||[]).map(group=>`<section class="plugin-section"><header><h3><button data-plugin-category="${escapeHtml(group.name)}">${escapeHtml(group.name)}<span aria-hidden="true">›</span></button></h3></header><div class="plugins-grid">${group.items.map(pluginCard).join('')}</div></section>`).join('')}</div>`:`<div class="plugins-grid">${pluginsState.items.map(pluginCard).join('')}</div>`):`<div class="plugins-empty"><h3>${r.syncing?L("تُحمّل المتاجر","Loading marketplaces"):L("لا توجد إضافات مطابقة","No matching plugins")}</h3><p>${r.syncing?L("سيظهر الفهرس تلقائيًا عند اكتمال التحميل.","The catalog will appear automatically when loading finishes."):L("جرّب كلمة أخرى.","Try another search.")}</p><button class="secondary-button" data-plugin-action="reset">${L("مسح البحث","Clear search")}</button></div>`;
    bindBrandLogos(results);
    results.querySelectorAll('[data-plugin-category]').forEach(b=>b.firstChild.textContent=pluginGroupLabel(b.dataset.pluginCategory));
    if(r.warning&&pluginsState.mode==='packages')$("pluginDirectoryState").innerHTML=`${escapeHtml(r.warning)} <button class="secondary-button" data-plugin-action="retry">${L("إعادة المحاولة","Retry")}</button>`;
    if(r.syncing&&!append&&home)pluginsState.timer=setTimeout(()=>loadPluginCatalog(++pluginsState.version),10000);
  }catch(e){if(version===pluginsState.version&&results.isConnected)results.innerHTML=`<div class="plugins-empty" role="alert"><p>${escapeHtml(e.message)}</p><button class="secondary-button" data-plugin-action="retry">${L("إعادة المحاولة","Retry")}</button></div>`;}
  finally{if(version===pluginsState.version){pluginsState.loading=false;results.setAttribute("aria-busy","false");watchPluginScroll();}}
}
function packageConnection(server,item){
  const connection=item.connections.find(c=>c.name===server.name);
  const fields=!connection?server.fields.map(f=>`<label>${escapeHtml(f.label)}<input name="${escapeHtml(f.id)}" type="password" maxlength="4096" required autocomplete="new-password"></label>`).join(""):"";
  const trust=server.local_execution?`<label class="plugin-trust"><input name="trusted_local" type="checkbox" required>${L("أسمح بتشغيل كود الإضافة محليًا بصلاحيات حسابي.","Allow this plugin's code to run locally with my account permissions.")}</label>`:"";
  const authorize=connection?.status==="AUTH_REQUIRED"?`<button class="primary-button" type="button" data-plugin-action="authorize" data-server="${escapeHtml(server.name)}">${L("مصادقة في المتصفح","Authorize in browser")}</button>`:"";
  const advanced=connection?`<details class="plugin-advanced"><summary>${L("إعداد الاتصال","Connection settings")}</summary><label for="pluginOAuth_${escapeHtml(server.name)}">${L("معرّف عميل OAuth مسجل (إذا طلبته الخدمة)","Registered OAuth client ID (only if required by the service)")}</label><input id="pluginOAuth_${escapeHtml(server.name)}" maxlength="2048" autocomplete="off"><div class="plugin-account-actions"><button class="secondary-button" data-plugin-action="authorize" data-server="${escapeHtml(server.name)}">${L("مصادقة","Authorize")}</button><button class="secondary-button" data-plugin-action="reset-connection" data-server="${escapeHtml(server.name)}">${L("إعادة إعداد الاتصال","Reset connection")}</button></div></details>`:"";
  return `<section class="package-connection"><h4>${escapeHtml(server.name)} <small>${connection?pluginStatus(connection.status):L("غير متصل","Not connected")}</small></h4><form class="packageConnectForm connection-form" data-server="${escapeHtml(server.name)}">${fields}${trust}<button class="secondary-button" type="submit">${connection?L("إعادة الاتصال","Reconnect"):L("اتصال","Connect")}</button>${authorize}</form>${advanced}${connection?.tools.length?`<details><summary>${connection.tools.length} ${L("أداة متصلة","connected tools")}</summary><ul>${connection.tools.map(t=>`<li>${escapeHtml(t.name)}</li>`).join("")}</ul></details>`:""}</section>`;
}
async function loadPluginDetail(id,version,prepare=false){
  if(!id.startsWith('pkg_'))return loadServiceDetail(id,version);
  const host=$("pluginsContent");if(!prepare)host.innerHTML=`<div class="plugin-detail">${pluginBack()}<div class="plugins-skeleton"></div></div>`;else renderPluginAuth(L('جارٍ تنزيل الحزمة والتحقق منها…','Downloading and checking package…'));
  try{
    if(!prepare){
      const item=await pluginsApi(`/packages/${encodeURIComponent(id)}/overview`);if(version!==pluginsState.version||state.currentView!=="plugins")return;
      pluginsState.detail=item;
      host.innerHTML=`<div class="plugin-detail plugin-overview">${pluginBack()}<header class="plugin-detail-heading">${pluginLogo(item)}<div><h2 tabindex="-1">${escapeHtml(item.display_name)}</h2><p>${escapeHtml(item.publisher)} · ${escapeHtml(item.category)}</p></div></header><p class="plugin-description">${escapeHtml(item.description||L('تفاصيل إضافية متاحة من ناشر الإضافة.','Further details are available from the publisher.'))}</p><div class="plugin-overview-actions"><button class="primary-button" data-plugin-action="prepare">${item.installed?L('إدارة الإضافة','Manage plugin'):L('تثبيت الإضافة','Install plugin')}</button><a href="${escapeHtml(item.repository)}" data-plugin-docs="${escapeHtml(item.repository)}">${L('معلومات الناشر','Publisher information')}</a></div><p id="pluginAuthState" role="status" aria-live="polite"></p><p class="plugin-permission-note">${L('يمكنك تصفّح المعلومات دون تنزيل الحزمة. نتحقق من محتواها قبل التثبيت.','Browse without downloading the package. Its contents are checked before installation.')}</p></div>`;
      bindBrandLogos(host);host.querySelector('h2')?.focus({preventScroll:true});return;
    }
    const item=await pluginsApi(`/packages/${encodeURIComponent(id)}`);if(version!==pluginsState.version||state.currentView!=="plugins")return;
    pluginsState.detail=item;const plan=item.plan,active=item.installed_plan||plan;
    host.innerHTML=`<div class="plugin-detail">${pluginBack()}<header class="plugin-detail-heading">${pluginLogo(item)}<div><h2 tabindex="-1">${escapeHtml(item.display_name)}</h2><p>${escapeHtml(item.publisher||item.marketplace)} · ${escapeHtml(item.category)} · ${pluginStatus(item.status)}</p></div></header><p class="plugin-description">${escapeHtml(item.description)}</p><div class="plugin-detail-layout"><section class="plugin-connection-pane"><h3>${L("التثبيت","Installation")}</h3><p>${L("راجع الحزمة والناشر قبل تثبيتها. اتصالات الخدمات تُعدّ بعد التثبيت.","Review the package and publisher before installing. Set up service connections after installation.")}</p><p>${L("الإصدار","Version")}: ${escapeHtml(plan.version)}</p>${plan.unsupported.length?`<div class="package-compatibility" role="note"><strong>${L("توافق جزئي","Partial compatibility")}</strong><ul>${plan.unsupported.map(s=>`<li>${escapeHtml(s)}</li>`).join("")}</ul><p>${L("هذه المكونات لن تُفعّل في وايز.","These components will not be activated in WISE.")}</p></div>`:""}${plan.can_install?`<form id="pluginInstallForm" class="connection-form"><label class="plugin-trust"><input name="trusted_publisher" type="checkbox" required>${L("راجعت الحزمة وأثق بناشرها.","I reviewed this package and trust its publisher.")}</label>${plan.unsupported.length?`<label class="plugin-trust"><input name="accept_partial" type="checkbox" required>${L("أوافق على تثبيت المكونات المدعومة فقط.","Install supported components only.")}</label>`:""}<button class="primary-button" type="submit">${item.installed?(item.installed_commit!==item.commit?L("تحديث","Update"):item.status==="DISABLED"?L("تفعيل","Enable"):L("مثبتة","Installed")):L("تثبيت الإضافة","Install plugin")}</button></form>`:`<p role="status">${L("هذه الحزمة تحتاج محوّلًا لمكونات غير مدعومة حاليًا.","This package needs an adapter for unsupported components.")}</p>`}${item.installed?`<div class="plugin-account-actions">${item.status==="ENABLED"?`<button class="secondary-button" data-plugin-action="disable">${L("تعطيل","Disable")}</button>`:""}<button class="secondary-button" data-plugin-action="uninstall">${L("إزالة","Uninstall")}</button></div>`:""}<div id="pluginAuthState" role="status" aria-live="polite"></div><details class="plugin-advanced"><summary>${L("المصدر والإصدار","Package provenance")}</summary><p>${escapeHtml(item.marketplace)}</p><code class="plugin-endpoint">${escapeHtml(item.commit)}</code><a href="${escapeHtml(item.repository)}" data-plugin-docs="${escapeHtml(item.repository)}">${L("مستودع الإضافة","Plugin repository")}</a></details></section><section class="plugin-capabilities"><h3>${L("محتوى الإضافة","Plugin contents")}</h3>${active.workflows.length?`<h4>${L("مسارات عمل","Workflows")}</h4><ul>${active.workflows.map(w=>`<li><div><strong>${escapeHtml(w.name)}</strong><p>${escapeHtml(w.description)}</p></div></li>`).join("")}</ul>`:""}${active.servers.length?`<h4>${L("اتصالات مضمّنة","Bundled connections")}</h4>${item.installed&&item.status==="ENABLED"?active.servers.map(s=>packageConnection(s,item)).join(""):`<ul>${active.servers.map(s=>`<li>${escapeHtml(s.name)}</li>`).join("")}</ul>`}`:""}${!active.workflows.length&&!active.servers.length?`<p>${L("لا يوجد محتوى مدعوم في هذه الحزمة.","No supported components in this package.")}</p>`:""}<p class="plugin-permission-note">${L("تُتاح قدرات الإضافة للوكيل بعد تفعيلها وتبقى تحت سياسة الصلاحيات. التثبيت لا يشغّل scripts أو hooks تلقائيًا.","Enabled plugin capabilities remain under WISE permission policy. Installation never automatically executes scripts or hooks.")}</p></section></div></div>`;
    if(item.warning)host.querySelector(".plugin-description").insertAdjacentHTML("afterend",`<p role="status">${escapeHtml(item.warning)}</p>`);
    bindBrandLogos(host);host.querySelector("h2")?.focus({preventScroll:true});
    const pending=await pluginsApi("/pending");if(version!==pluginsState.version||pluginsState.busy)return;
    const ids=new Set(item.connections.map(c=>c.id));pluginsState.attempt=pending.attempts.find(a=>ids.has(a.integration))||null;if(pluginsState.attempt)resumePluginPolling();
  }catch(e){if(version===pluginsState.version){if(prepare){renderPluginAuth(L('تعذر تجهيز الحزمة. معلوماتها ما زالت متاحة؛ تحقق من الاتصال ثم جرّب التثبيت مجددًا.','Could not prepare the package. Its information remains available; check your connection and try installing again.'));host.querySelector('[data-plugin-action="prepare"]')?.removeAttribute('disabled');}else host.innerHTML=`<div class="plugin-detail">${pluginBack()}<p role="alert">${escapeHtml(e.message)}</p><button class="secondary-button" data-plugin-action="detail-retry">${L("إعادة المحاولة","Retry")}</button></div>`;}}
}
function renderPluginAuth(message,pending=false){const node=$("pluginAuthState");if(node)node.innerHTML=`<p>${escapeHtml(message)}</p>${pending?`<div class="plugin-account-actions"><button class="secondary-button" data-plugin-action="launch">${L("فتح المتصفح","Open browser")}</button><button class="secondary-button" data-plugin-action="cancel">${L("إلغاء","Cancel")}</button></div>`:""}`;}
async function resumePluginPolling(){
  pausePluginPolling();const a=pluginsState.attempt;if(!a||state.currentView!=="plugins")return;
  try{const r=await pluginsApi(`/attempts/${encodeURIComponent(a.id)}`);if(a!==pluginsState.attempt)return;
    if(["PENDING","EXCHANGING"].includes(r.status)){renderPluginAuth(L("أكمل المصادقة في متصفح النظام…","Complete authorization in your system browser…"),true);pluginsState.timer=setTimeout(resumePluginPolling,2500);}
    else{pluginsState.attempt=null;if(r.status==="OK")loadPlugins();else renderPluginAuth(r.error||r.status);}
  }catch(e){if(a!==pluginsState.attempt)return;renderPluginAuth(e.message,true);pluginsState.timer=setTimeout(resumePluginPolling,8000);}
}
function loadMorePlugins(){if(pluginsState.loading||pluginsState.next===null)return;pluginsState.offset=pluginsState.next;return loadPluginCatalog(++pluginsState.version,false,true);}
document.addEventListener("input",e=>{if(e.target.id!=="pluginSearch")return;pluginsState.query=e.target.value;clearTimeout(pluginSearchTimer);pluginSearchTimer=setTimeout(()=>loadPluginCatalog(),250);});
document.addEventListener("change",e=>{if(e.target.id!=="pluginCategory")return;pluginsState.category=e.target.value;loadPluginCatalog();});
document.addEventListener("submit",async e=>{
  if(e.target.id==='serviceConnectForm')return submitServiceConnection(e);
  if(!["pluginInstallForm","pluginMarketplaceForm"].includes(e.target.id)&&!e.target.matches(".packageConnectForm"))return;e.preventDefault();if(pluginsState.busy)return;
  const form=e.target,button=form.querySelector('[type="submit"]'),version=pluginsState.version;pluginsState.busy=true;button.disabled=true;
  try{
    if(form.id==="pluginMarketplaceForm"){
      await pluginsApi("/marketplaces",{method:"POST",body:JSON.stringify({repository:form.elements.repository.value})});if(version===pluginsState.version)pluginRoute();
    }else if(form.id==="pluginInstallForm"){
      await pluginsApi(`/packages/${pluginsState.detail.id}/install`,{method:"POST",body:JSON.stringify({commit:pluginsState.detail.commit,trusted_publisher:form.elements.trusted_publisher.checked,accept_partial:!!form.elements.accept_partial?.checked})});if(version===pluginsState.version)loadPlugins();
    }else{
      const name=form.dataset.server,fields={};const server=pluginsState.detail.installed_plan.servers.find(s=>s.name===name);
      server.fields.forEach(f=>{if(form.elements[f.id])fields[f.id]=form.elements[f.id].value;});
      const r=await pluginsApi(`/packages/${pluginsState.detail.id}/connections/${encodeURIComponent(name)}/connect`,{method:"POST",body:JSON.stringify({fields,trusted_local:!!form.elements.trusted_local?.checked})});
      form.querySelectorAll('input[type="password"]').forEach(n=>n.value="");if(version!==pluginsState.version)return;
      if(r.status==="AUTH_REQUIRED")await authorizePlugin(name);else loadPlugins();
    }
  }catch(error){if(version!==pluginsState.version)return;renderPluginAuth(error.message);const feedback=$("pluginMarketplaceFeedback");if(feedback)feedback.textContent=error.message;}
  finally{pluginsState.busy=false;if(button.isConnected)button.disabled=false;}
});
async function authorizePlugin(name){
  const version=pluginsState.version;
  const r=await pluginsApi(`/packages/${pluginsState.detail.id}/connections/${encodeURIComponent(name)}/authorize`,{method:"POST",body:JSON.stringify({client_id:$("pluginOAuth_"+name)?.value.trim()||""})});if(version!==pluginsState.version)return;
  if(r.status==="PUBLIC"){renderPluginAuth(r.message);return;}
  pluginsState.attempt={id:r.id,integration:r.integration};renderPluginAuth(r.browser_opened?L("بانتظار المصادقة…","Waiting for authorization…"):L("تعذر فتح المتصفح؛ استخدم زر فتح المتصفح.","Browser could not open; use Open browser."),true);resumePluginPolling();
}

async function loadServiceDetail(id,version){
  const host=$('pluginsContent');host.innerHTML=`<div class="plugin-detail">${pluginBack()}<div class="plugins-skeleton"></div></div>`;
  try{
    const item=await pluginsApi('/integrations/'+encodeURIComponent(id));if(version!==pluginsState.version||state.currentView!=='plugins')return;
    pluginsState.detail=item;
    const required=item.fields.filter(f=>f.required),optional=item.fields.filter(f=>!f.required);
    const fieldInput=serviceFieldInput;
    const local=item.connection_type==='local';
    const executable=!!item.local_execution;
    const connected=item.status==='CONNECTED';
    host.innerHTML=`<div class="plugin-detail plugin-overview service-overview">${pluginBack()}<header class="plugin-detail-heading">${pluginLogo(item)}<div><h2 tabindex="-1">${escapeHtml(item.display_name||item.name)}</h2><p>${connectionType(item)} · ${pluginGroupLabel(item.category||item.categories[0])}</p></div></header><p class="plugin-description">${escapeHtml(item.description)}</p>${item.setup_note?`<p class="plugins-service-note">${escapeHtml(item.setup_note)}</p>`:''}${executable?`<details class="plugin-advanced"><summary>${L('الناشر والإصدار','Publisher and version')}</summary><p>${escapeHtml(item.publisher)} · ${escapeHtml(item.version)}</p>${item.bundle_sha256?`<code class="plugin-endpoint">SHA256: ${escapeHtml(item.bundle_sha256)}</code>`:''}</details>`:''}<section class="service-connection"><p class="service-status">${pluginStatus(item.status)}</p>${connected?`<details class="service-tools"><summary>${item.tools.length} ${L('أدوات متاحة للوكيل','tools available to the agent')}</summary><ul>${item.tools.map(t=>`<li><strong>${escapeHtml(t.name)}</strong><p>${escapeHtml(t.description)}</p></li>`).join('')}</ul></details>`:''}${local?`<div class="service-readiness" role="status"></div><button class="secondary-button" data-plugin-action="service-probe">${L('فحص جاهزية التطبيق','Check application readiness')}</button>`:''}<form id="serviceConnectForm" class="connection-form">${item.installed&&executable?'':required.map(fieldInput).join('')}${optional.length&&!(item.installed&&executable)?`<details class="plugin-advanced"><summary>${executable?L('خيارات إضافية','Additional options'):L('استخدام مفتاح وصول بدل تسجيل الدخول','Use an access token instead of sign-in')}</summary>${optional.map(fieldInput).join('')}</details>`:''}${local||executable?`<label class="plugin-trust"><input name="trusted_local" type="checkbox" required>${executable?L('أثق بالناشر وأسمح بتشغيل الكود بصلاحيات حسابي.','I trust the publisher and allow code to run with my account permissions.'):L('أسمح لوايز بالاتصال بالتطبيق المحلي.','Allow WISE to connect to this local application.')}</label>`:''}<div class="plugin-overview-actions"><button class="primary-button" type="submit">${connected?L('إعادة الاتصال','Reconnect'):executable&&!item.installed?L('تثبيت واتصال','Install and connect'):local?L('ربط التطبيق','Connect application'):L('اتصال','Connect')}</button>${item.docs_url?`<a href="${escapeHtml(item.docs_url)}" data-plugin-docs="${escapeHtml(item.docs_url)}">${L('دليل الخدمة','Service guide')}</a>`:''}${item.installed?`<button class="secondary-button" type="button" data-plugin-action="service-disconnect">${L('قطع الاتصال','Disconnect')}</button>`:''}</div></form><div id="pluginAuthState" role="status" aria-live="polite"></div>${!local&&!executable&&item.auth_mode!=='token'?`<details class="plugin-advanced"><summary>${L('إعدادات متقدمة','Advanced settings')}</summary><label for="serviceOAuthClient">${L('معرّف عميل OAuth مسجل عند الحاجة','Registered OAuth client ID, if required')}</label><input id="serviceOAuthClient" maxlength="2048" autocomplete="off"><label for="serviceOAuthMetadata">${L('رابط مستند عميل وايز المنشور، إن وُجد','Published WISE client metadata URL, if available')}</label><input id="serviceOAuthMetadata" type="url" maxlength="2048" placeholder="https://your-domain.example/wise-client.json" autocomplete="off"><button class="secondary-button" data-plugin-action="service-authorize">${L('تسجيل الدخول','Sign in')}</button></details>`:''}</section></div>`;
    refineMarketDetail(host,item);
    setServiceBusy(pluginsState.busy);bindBrandLogos(host);host.querySelector('h2')?.focus({preventScroll:true});
    // Restore an in-progress login after navigating away, without launching
    // a second browser or starting an unauthorized connection on page load.
    try{
      const pending=await pluginsApi('/pending');if(version!==pluginsState.version)return;
      const attempt=pending.attempts.find(a=>a.integration===id);if(attempt){pluginsState.attempt=attempt;resumePluginPolling();}
    }catch(_){if(version===pluginsState.version)renderPluginAuth(L('تعذّر تحديث حالة تسجيل الدخول. يمكنك محاولة الاتصال مجددًا.','Could not refresh sign-in status. You can try connecting again.'));}
  }catch(e){if(version===pluginsState.version)host.innerHTML=`<div class="plugin-detail">${pluginBack()}<p role="alert">${escapeHtml(e.message)}</p><button class="secondary-button" data-plugin-action="detail-retry">${L('إعادة المحاولة','Retry')}</button></div>`;}
}

function refineMarketDetail(host,item){
  const detail=host.querySelector('.service-overview');if(!detail)return;
  detail.classList.add('market-detail');
  detail.querySelector('.plugin-detail-heading p').textContent=item.publisher||item.publisher_id||connectionType(item);
  const layout=document.createElement('div');layout.className='market-detail-layout';
  const info=document.createElement('section');info.className='market-information';
  info.innerHTML=`<h3>${L('ما الذي يقدمه؟','What it does')}</h3>`;
  const description=detail.querySelector('.plugin-description');description.setAttribute('dir','auto');info.append(description);
  const fact=(label,value)=>value?`<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`:'';
  info.insertAdjacentHTML('beforeend',`<dl class="market-facts">${fact(L('المصدر','Source'),item.source==='community'?L('دليل MCP · إدراج من الناشر','MCP Registry · publisher listing'):item.source==='bundle'?L('حزمة مستوردة','Imported bundle'):L('اتصال مُراجع','Reviewed connection'))}${fact(L('النوع','Type'),connectionType(item))}${fact(L('الإصدار','Version'),item.version==='service'?L('خدمة متصلة','Hosted service'):item.version)}${fact(L('الناشر','Publisher'),item.publisher_id||item.publisher)}${fact(L('طريقة الاتصال','Connection method'),item.local_execution?L('تشغيل محلي بعد الموافقة','Local execution after approval'):item.connection_type==='local'?L('جسر التطبيق المحلي','Local application bridge'):item.auth_mode==='token'?L('مفتاح وصول','Access token'):item.auth_mode==='public'?L('دون حساب','No account required'):L('تسجيل الدخول أو إعداد الخدمة','Sign-in or service setup'))}</dl><div class="market-source-links">${[['docs_url',L('دليل الإعداد','Setup guide')],['repository_url',L('المستودع','Repository')],['registry_url',L('المصدر في الدليل','Registry listing')]].filter(([key])=>item[key]).map(([key,label])=>`<a href="${escapeHtml(item[key])}" data-plugin-docs="${escapeHtml(item[key])}">${label}</a>`).join('')}</div>`);
  const provenance=detail.querySelector(':scope > .plugin-advanced');if(provenance)info.append(provenance);
  if(!item.can_connect)info.querySelector('.market-facts > div:last-child dd').textContent=L('وفق دليل الناشر','As documented by the publisher');
  if(item.updated_at){const date=new Date(item.updated_at);if(!Number.isNaN(date.getTime()))info.querySelector('.market-facts').insertAdjacentHTML('beforeend',fact(L('آخر تحديث في الدليل','Registry updated'),new Intl.DateTimeFormat(document.documentElement.lang,{dateStyle:'medium'}).format(date)));}
  const connection=detail.querySelector('.service-connection');connection.classList.add('market-connection');
  connection.insertAdjacentHTML('afterbegin',`<h3>${L('الاتصال','Connection')}</h3>`);
  const note=detail.querySelector(':scope > .plugins-service-note');if(note){note.setAttribute('dir','auto');connection.insertBefore(note,connection.querySelector('.service-status'));}
  if(!item.can_connect){
    connection.querySelector('#serviceConnectForm')?.remove();
    connection.querySelector('.plugin-advanced')?.remove();
    connection.insertAdjacentHTML('beforeend',`<p class="market-setup-message">${L('يحتاج إعدادًا يدويًا وفق دليل الناشر. لا يتوفر اتصال مباشر لهذا النوع حاليًا.','Follow the publisher guide for manual setup. Direct connection is not supported for this type yet.')}</p>`);
  }
  if(item.endpoint){const endpoint=document.createElement('details');endpoint.className='plugin-advanced';endpoint.innerHTML=`<summary>${L('عنوان الخادم','Server address')}</summary><code class="plugin-endpoint">${escapeHtml(item.endpoint)}</code>`;info.append(endpoint);}
  layout.append(info,connection);detail.append(layout);
}

function serviceFieldInput(f){
  const name=escapeHtml(f.id),label=escapeHtml(f.label),value=f.secret?'':f.default;
  if(f.type==='boolean')return `<label class="plugin-trust"><input name="${name}" type="checkbox" ${value===true?'checked':''}>${label}</label>`;
  if(f.multiple)return `<label>${label}<textarea name="${name}" ${f.required?'required':''} rows="3" maxlength="16000" placeholder="${L('قيمة واحدة في كل سطر','One value per line')}">${escapeHtml(Array.isArray(value)?value.join('\n'):value||'')}</textarea></label>`;
  if(f.choices?.length)return `<label>${label}<select name="${name}" ${f.required?'required':''}><option value=""></option>${f.choices.map(v=>`<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('')}</select></label>`;
  return `<label>${label}<input name="${name}" type="${f.secret?'password':f.type==='number'?'number':'text'}" value="${escapeHtml(value??'')}" maxlength="4096" ${f.required?'required':''} ${f.type==='number'?`step="any" ${f.min!=null?`min="${Number(f.min)}"`:''} ${f.max!=null?`max="${Number(f.max)}"`:''}`:''} autocomplete="off"></label>`;
}

function serviceAuthorization(r){
  if(!r||r.status==='PUBLIC')return renderPluginAuth(r?.message||'');
  pluginsState.attempt={id:r.id,integration:r.integration};renderPluginAuth(r.browser_opened?L('أكمل تسجيل الدخول في المتصفح…','Complete sign-in in your browser…'):L('استخدم فتح المتصفح لإكمال تسجيل الدخول.','Use Open browser to finish signing in.'),true);resumePluginPolling();
}

function setServiceBusy(busy){
  const pane=document.querySelector('.service-connection');if(!pane)return;
  pane.setAttribute('aria-busy',String(busy));pane.querySelectorAll('button,input,textarea,select').forEach(node=>node.disabled=busy);
}

async function submitServiceConnection(e){
  e.preventDefault();if(pluginsState.busy)return;
  const form=e.target,b=form.querySelector('[type="submit"]'),version=pluginsState.version,item=pluginsState.detail;
  pluginsState.busy=true;setServiceBusy(true);renderPluginAuth(item.local_execution&&!item.installed?L('جارٍ تثبيت الحزمة والتحقق من الاتصال…','Installing package and verifying connection…'):L('جارٍ الاتصال…','Connecting…'));
  const fields={};item.fields.forEach(f=>{const input=form.elements[f.id];if(!input)return;if(f.type==='boolean')fields[f.id]=input.checked;else if(f.multiple&&input.value.trim())fields[f.id]=input.value.split('\n').map(v=>v.trim()).filter(Boolean);else if(input.value)fields[f.id]=f.type==='number'?input.valueAsNumber:input.value;});
  try{
    const r=await pluginsApi('/connections/'+encodeURIComponent(item.id)+'/connect',{method:'POST',body:JSON.stringify({fields,trusted_local:!!form.elements.trusted_local?.checked})});
    if(version!==pluginsState.version)return;
    await loadServiceDetail(item.id,version);if(version!==pluginsState.version)return;
    if(r.authorization)serviceAuthorization(r.authorization);
    else if(r.authorization_error)renderPluginAuth(r.authorization_error);
    else if(r.connection.status!=='CONNECTED')renderPluginAuth(item.connection_type==='local'?L('افتح التطبيق وفعّل الاتصال المحلي من إعداداته، ثم حاول مجددًا.','Open the application and enable its local connection, then try again.'):r.connection.error||L('راجع إعدادات الخدمة ثم حاول مجددًا.','Review the service setup, then try again.'));
  }catch(error){if(version===pluginsState.version)renderPluginAuth(error.message);}
  finally{form.querySelectorAll('input[type="password"]').forEach(input=>input.value='');pluginsState.busy=false;setServiceBusy(false);if(b.isConnected)b.disabled=false;}
}
document.addEventListener("click",async e=>{
  const b=e.target.closest("#pluginsView button");if(!b)return;
  if(b.dataset.pluginId)return pluginRoute(b.dataset.pluginId);
  if(b.dataset.pluginCategory){pluginsState.query='';history.pushState(null,'',(pluginsState.mode==='packages'?'#plugins/packages':'#plugins')+'/category/'+encodeURIComponent(b.dataset.pluginCategory));return loadPlugins();}
  if(b.hasAttribute("data-plugin-source")){pluginsState.source=b.dataset.pluginSource;pluginsState.category="";return loadPlugins();}
  const action=b.dataset.pluginAction;
  if(action==="back")return pluginRoute();if(action==="more")return loadMorePlugins();
  if(action==="prepare"){b.disabled=true;return loadPluginDetail(pluginsState.detail.id,pluginsState.version,true);}
  if(action==="retry")return loadPluginCatalog(++pluginsState.version,true);if(action==="detail-retry")return loadPlugins();
  if(action==="reset"){pluginsState.query="";pluginsState.category="";return loadPlugins();}
  if(action==="marketplace"){
    pausePluginPolling();++pluginsState.version;$("pluginsContent").innerHTML=`<div class="plugin-detail">${pluginBack()}<h2>${L("إضافة متجر إضافات","Add plugin marketplace")}</h2><p>${L("أضف مستودع متجر يحتوي حزم إضافات. راجع ناشره قبل التثبيت.","Add a repository containing a plugin marketplace. Review its publisher before installing.")}</p><form id="pluginMarketplaceForm" class="connection-form"><label for="pluginMarketplaceRepo">${L("مستودع GitHub العام","Public GitHub repository")}</label><input id="pluginMarketplaceRepo" name="repository" maxlength="300" required placeholder="owner/repository" autocomplete="off"><button class="primary-button" type="submit">${L("إضافة المتجر","Add marketplace")}</button><p id="pluginMarketplaceFeedback" role="status"></p></form></div>`;return;
  }
  if(!action||pluginsState.busy)return;pluginsState.busy=true;b.disabled=true;setServiceBusy(true);const version=pluginsState.version;
  try{
    if(action==='service-authorize'){
      const r=await pluginsApi('/integrations/'+encodeURIComponent(pluginsState.detail.id)+'/authorize',{method:'POST',body:JSON.stringify({client_id:$('serviceOAuthClient')?.value.trim()||'',client_metadata_url:$('serviceOAuthMetadata')?.value.trim()||''})});if(version===pluginsState.version)serviceAuthorization(r);
    }else if(action==='service-probe'){
      const result=await pluginsApi('/connections/'+encodeURIComponent(pluginsState.detail.id)+'/readiness');if(version===pluginsState.version)document.querySelector('.service-readiness').textContent=result.ready?L('المنفذ جاهز. اضغط ربط التطبيق للتحقق من MCP.','Bridge reachable. Press Connect application to verify MCP.'):L('افتح Figma، ثم Dev Mode، وفعّل خادم MCP المحلي.','Open Figma, enter Dev Mode, and enable its local MCP server.');
    }else if(action==='service-disconnect'){
      if(!confirm(L('قطع هذا الاتصال وإزالة بياناته من وايز؟ لن يُحذف حساب الخدمة.','Disconnect and remove its credentials from WISE? The service account is not deleted.')))return;
      await pluginsApi('/integrations/'+encodeURIComponent(pluginsState.detail.id),{method:'DELETE'});if(version===pluginsState.version)loadPlugins();
    }else if(action==="authorize")await authorizePlugin(b.dataset.server);
    else if(action==="reset-connection"){
      if(!confirm(L("إزالة بيانات هذا الاتصال المحلي لإعداده مجددًا؟ لا يتأثر حساب الخدمة.","Remove this local connection setup to configure it again? The service account is not deleted.")))return;
      await pluginsApi(`/packages/${pluginsState.detail.id}/connections/${encodeURIComponent(b.dataset.server)}`,{method:"DELETE"});if(version===pluginsState.version)loadPlugins();
    }
    else if(action==="launch"){const r=await pluginsApi(`/attempts/${pluginsState.attempt.id}/launch`,{method:"POST"});if(!r.browser_opened)throw Error(L("تعذر فتح المتصفح","Could not open browser"));}
    else if(action==="cancel"){await pluginsApi(`/attempts/${pluginsState.attempt.id}/cancel`,{method:"POST"});if(version!==pluginsState.version)return;pluginsState.attempt=null;pausePluginPolling();renderPluginAuth(L("أُلغيت المصادقة","Authorization cancelled"));}
    else if(action==="disable"||action==="uninstall"){
      if(action==="uninstall"&&!confirm(L("إزالة الإضافة واتصالاتها المحلية؟ لا تُحذف بيانات حساباتك الخارجية.","Remove plugin and its local connections? External account data is not deleted.")))return;
      await pluginsApi(`/packages/${pluginsState.detail.id}`+(action==="disable"?"/disable":""),{method:action==="disable"?"POST":"DELETE"});if(version!==pluginsState.version)return;if(action==="uninstall")pluginRoute();else loadPlugins();
    }
  }catch(error){if(version===pluginsState.version)renderPluginAuth(error.message,!!pluginsState.attempt);}
  finally{pluginsState.busy=false;setServiceBusy(false);if(b.isConnected)b.disabled=false;}
});
addEventListener("popstate",()=>{if(location.hash.startsWith("#plugins"))showView("plugins");else if(state.currentView==="plugins")showView("chat");});
document.addEventListener("click",async e=>{
  const link=e.target.closest("[data-plugin-docs]");if(!link)return;e.preventDefault();
  if(window.pywebview?.api?.open_external_url){if(!await window.pywebview.api.open_external_url(link.dataset.pluginDocs))toast(L("تعذر فتح المتصفح","Could not open browser"),"error");}
  else window.open(link.dataset.pluginDocs,"_blank","noopener,noreferrer");
});
addEventListener("online",()=>document.querySelectorAll(".brand-logo img[data-retry-src]").forEach(img=>{const src=img.dataset.retrySrc;delete img.dataset.retrySrc;img.src=src;}));
