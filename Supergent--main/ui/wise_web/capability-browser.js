"use strict";
const capabilityBrowser = {kind:"skills", query:"", category:"", offset:0, previous:[], version:0};

function capabilityBrowserTemplate() {
  return `<section class="settings-section capability-browser"><h3>${L("متصفح القدرات", "Capability browser")}</h3><p>${L("اختر النوع والمجموعة. حالة كل مورد توضّح ما يمكن تشغيله وما يحتاج إعدادًا.", "Choose a collection and group. Each resource shows what can run and what needs setup.")}</p><div class="capability-controls"><label>${L("النوع", "Collection")}<select id="capabilityKind">${[["skills",L("المهارات","Skills")],["tools",L("الأدوات وMCP","Tools & MCP")],["resources",L("المصادر والتكاملات","Sources & integrations")]].map(([id,label])=>`<option value="${id}" ${capabilityBrowser.kind === id ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>${L("المجموعة","Group")}<select id="capabilityCategory"><option value="">${L("الكل","All")}</option></select></label><label>${L("بحث","Search")}<input id="capabilityQuery" type="search" maxlength="500" value="${escapeHtml(capabilityBrowser.query)}"></label></div><div id="capabilityResults" role="status"></div><footer class="settings-inline-actions"><button id="capabilityPrevious" type="button" class="secondary-button">${L("السابق","Previous")}</button><span id="capabilityCount"></span><button id="capabilityNext" type="button" class="secondary-button">${L("التالي","Next")}</button></footer></section>`;
}

function capabilityState(item) {
  if(item.status === "ON_DEMAND") return L("يُثبّت عند الحاجة", "Install when needed");
  if(item.status === "PUBLIC_SOURCE_ADAPTER") return L("قراءة مصدر عام", "Public source reader");
  if(item.status === "ADAPTER_READY") return L("متاح للتنفيذ", "Executable adapter");
  if(item.status === "DEPENDENCY_REQUIRED") return L("يحتاج تثبيتًا", "Installation required");
  if(item.status === "DEPLOYMENT_AND_ADAPTER_REQUIRED") return L("يحتاج خدمة ومحوّلًا", "Service and adapter required");
  if(item.status === "MODEL_AND_ADAPTER_REQUIRED") return L("يحتاج نموذجًا ومحوّلًا", "Model and adapter required");
  if(item.status === "ADAPTER_REQUIRED" || item.status === "SOURCE_URL_REQUIRED") return L("يحتاج إعدادًا", "Setup required");
  if(item.status === "REFERENCE_ONLY") return L("مرجع فقط", "Reference only");
  return item.available === true ? L("متاح", "Available") : item.available === false ? L("غير متاح", "Unavailable") : "";
}

async function loadCapabilityBrowser() {
  const host = $("capabilityResults"); if (!host) return;
  const version = ++capabilityBrowser.version;
  host.textContent = L("جارٍ التحميل…","Loading…");
  $("capabilityNext").disabled = $("capabilityPrevious").disabled = true;
  try {
    const query = new URLSearchParams({query:capabilityBrowser.query,category:capabilityBrowser.category,offset:capabilityBrowser.offset,limit:10});
    const result = await api(`/api/v2/capabilities/browse/${capabilityBrowser.kind}?${query}`);
    if (!host.isConnected || version !== capabilityBrowser.version) return;
    $("capabilityCategory").innerHTML = `<option value="">${L("الكل","All")}</option>` + Object.entries(result.groups).map(([id,count])=>`<option value="${escapeHtml(id)}" ${id === capabilityBrowser.category ? "selected" : ""}>${escapeHtml(id)} (${count})</option>`).join("");
    host.innerHTML = result.items.map(item=>`<article class="setting-row"><div><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.description || "")}</small><small>${escapeHtml(item.category)} · ${escapeHtml(capabilityState(item))}</small>${item.limitations ? `<small>${escapeHtml(item.limitations)}</small>` : ""}</div></article>`).join("") || `<p>${L("لا توجد نتائج.","No results.")}</p>`;
    $("capabilityCount").textContent = `${Math.min(capabilityBrowser.offset+1,result.total)}–${Math.min(capabilityBrowser.offset+result.items.length,result.total)} / ${result.total}`;
    $("capabilityPrevious").disabled = capabilityBrowser.previous.length === 0;
    $("capabilityNext").disabled = result.next_offset === null;
    $("capabilityNext").onclick = () => { capabilityBrowser.previous.push(capabilityBrowser.offset); capabilityBrowser.offset = result.next_offset; loadCapabilityBrowser(); };
    $("capabilityPrevious").onclick = () => { capabilityBrowser.offset = capabilityBrowser.previous.pop() || 0; loadCapabilityBrowser(); };
  } catch (error) { if(host.isConnected && version === capabilityBrowser.version) host.textContent = error.message; }
}

let capabilitySearchTimer;
document.addEventListener("input", event => {
  if(event.target.id !== "capabilityQuery") return;
  capabilityBrowser.query = event.target.value; capabilityBrowser.offset = 0; capabilityBrowser.previous = [];
  clearTimeout(capabilitySearchTimer); capabilitySearchTimer = setTimeout(loadCapabilityBrowser,250);
});
document.addEventListener("change", event => {
  if(!["capabilityKind","capabilityCategory"].includes(event.target.id)) return;
  if(event.target.id === "capabilityKind") {capabilityBrowser.kind = event.target.value; capabilityBrowser.category = "";}
  else capabilityBrowser.category = event.target.value;
  capabilityBrowser.offset = 0; capabilityBrowser.previous = []; loadCapabilityBrowser();
});
