"use strict";

function integrationsSettingsTemplate() {
  const feedback = state.integrationFeedback ? `<div class="notice" role="status">${escapeHtml(state.integrationFeedback)}</div>` : "";
  const mcpRows = state.mcp.map(item => `<div class="setting-row connection-row"><div><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.alive ? `${item.tools || 0} ${L("أدوات متاحة", "tools available")}` : item.last_error || L("غير متصل", "Disconnected"))}</small></div><span class="settings-inline-actions"><button class="secondary-button" data-mcp-action="${item.alive ? "stop" : "start"}" data-mcp-name="${escapeHtml(item.name)}">${item.alive ? L("إيقاف", "Stop") : L("اتصال", "Connect")}</button>${item.transport === "http" && (item.oauth_optional || item.oauth_required || !item.alive) ? `<button class="secondary-button" data-mcp-action="authorize" data-mcp-name="${escapeHtml(item.name)}">${item.oauth_optional ? L("ربط حساب", "Link account") : L("مصادقة", "Authorize")}</button>` : ""}</span></div>`).join("");
  const catalog = (state.mcpCatalog || []).map(item => `<div class="setting-row connection-row"><div><b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.description)}</small></div><button class="secondary-button" data-mcp-catalog="${escapeHtml(item.name)}">${L("إعداد", "Set up")}</button></div>`).join("");
  return `${feedback}
    <section class="settings-section"><h3>${L("الخوادم المتصلة", "Configured servers")}</h3>${mcpRows || `<div class="notice">${L("لا توجد خوادم مسجلة.", "No servers registered.")}</div>`}</section>
    <section class="settings-section"><h3>${L("إضافة MCP", "Add MCP")}</h3><form id="mcpImportForm" class="connection-form"><label for="mcpConfig">${L("الصق إعداد MCP بصيغة JSON", "Paste MCP configuration JSON")}</label><textarea id="mcpConfig" rows="7" dir="ltr" spellcheck="false" required placeholder='{"mcpServers":{"server":{"url":"https://.../mcp"}}}'></textarea>
      <details><summary>${L("خيارات متقدمة", "Advanced options")}</summary><label for="mcpClientId">${L("OAuth client ID (إذا طلبته الخدمة)", "OAuth client ID (if required by the service)")}</label><input id="mcpClientId" autocomplete="off"><label class="connection-checkbox"><input id="mcpTrustedLocal" type="checkbox">${L("أثق بمصدر الخادم المحلي وأوافق على تشغيله بصلاحيات حساب Windows دون عزل", "I trust this local server and allow running it as my Windows user without isolation")}</label><label class="connection-checkbox"><input id="mcpReplaceExisting" type="checkbox">${L("استبدال الخادم الموجود بنفس الاسم", "Replace an existing server with this name")}</label></details>
      <footer class="settings-inline-actions"><button class="primary-button" type="submit">${L("حفظ واتصال", "Save & connect")}</button></footer><div id="mcpImportResult" role="status"></div></form></section>
    <details class="settings-section"><summary>${L("خوادم مقترحة من مصادر رسمية", "Suggested official servers")}</summary>${catalog}</details>`;
}

function reserveAuthWindow() {
  // Reserve synchronously during the click, before awaiting HTTP. Opening a
  // popup after await is blocked by browsers. Native WISE uses its Brave bridge.
  return window.pywebview?.api?.open_external_url ? null : window.open("about:blank", "_blank");
}

async function openMcpAuthorization(auth, reservedWindow = null) {
  if (auth?.ok && auth.requires_auth === false) {
    reservedWindow?.close(); toast(L("هذا الخادم متاح دون تسجيل دخول.", "This server is available without login.")); return;
  }
  if (!auth?.ok || !auth.auth_url) {
    reservedWindow?.close();
    throw Error(auth?.error || L("الخادم لم يقدّم رابط مصادقة. قد لا يحتاج حساباً.", "The server supplied no authorization link; it may not require an account."));
  }
  const url = new URL(auth.auth_url);
  if (url.protocol !== "https:" || url.username || url.password) { reservedWindow?.close(); throw Error("Unsafe authorization URL"); }
  const host = document.createElement("div"); host.id = "mcpAuthFeedback"; host.className = "mcp-auth-feedback"; host.setAttribute("role", "status");
  $("mcpAuthFeedback")?.remove(); $("settingsContent").prepend(host);
  const anchor = document.createElement("a"); anchor.href = auth.auth_url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
  anchor.textContent = L("متابعة المصادقة في المتصفح", "Continue authorization in browser"); host.append(anchor);
  try {
    if (window.pywebview?.api?.open_external_url) {
      const opened = await window.pywebview.api.open_external_url(auth.auth_url);
      if (opened !== true && opened?.ok !== true) throw Error(opened?.error || "Browser launch failed");
    } else if (reservedWindow && !reservedWindow.closed) { reservedWindow.opener = null; reservedWindow.location = auth.auth_url; }
    else throw Error("Popup blocked");
  } catch (_) { toast(L("تعذّر فتح Brave؛ استخدم رابط المصادقة.", "Could not open Brave; use the authorization link."), "error"); }
  // Do not hold the click handler for six minutes or discard the manual link.
  (async () => {
    for (let attempts = 0; attempts < 120 && host.isConnected && !$("settingsModal").hidden; attempts++) {
      await new Promise(resolve => setTimeout(resolve, 3000));
      try {
        const result = await api(`/admin/mcp/authorization/status?state=${encodeURIComponent(auth.state)}`);
        if (!["pending", "exchanging"].includes(result.status)) {
          state.integrationFeedback = result.status === "ok" ? L("تمت المصادقة والاتصال.", "Authorized and connected.") : result.error || result.status;
          await loadIntegrations(); await loadSlashCommands(); await renderSettings(); return;
        }
      } catch (error) { toast(error.message, "error"); return; }
    }
    if (host.isConnected) host.append(document.createTextNode(L(" انتهت فترة الانتظار؛ أعد المحاولة إذا لم يكتمل الدخول.", " Waiting expired; retry if login did not complete.")));
  })();
}

async function saveMcpImport(event) {
  event.preventDefault();
  const button = event.target.querySelector('[type="submit"]'); button.disabled = true;
  const resultHost = $("mcpImportResult"); resultHost.textContent = L("جارٍ الاتصال…", "Connecting…");
  const reserved = reserveAuthWindow(); let authorizationStarted = false;
  try {
    const result = await api("/admin/mcp/import", {method: "POST", body: JSON.stringify({config: $("mcpConfig").value,
      trusted_local: $("mcpTrustedLocal").checked, replace_existing: $("mcpReplaceExisting").checked, client_id: $("mcpClientId").value.trim()})});
    state.integrationFeedback = result.servers.map(s => `${s.name}: ${s.alive ? L("متصل", "Connected") : s.authorization?.error || s.error || L("تم الحفظ", "Saved")}`).join(" · ");
    // Clear pasted secrets after successful persistence, not on validation failure.
    $("mcpConfig").value = "";
    await loadIntegrations();
    resultHost.textContent = state.integrationFeedback;
    await renderSettings();
    await loadSlashCommands();
    for (const server of result.servers) if (server.authorization?.ok) { await openMcpAuthorization(server.authorization, reserved); authorizationStarted = true; break; }
  } catch (error) { resultHost.textContent = error.message; }
  finally { if (!authorizationStarted) reserved?.close(); button.disabled = false; }
}

document.addEventListener("submit", event => {
  if (event.target.id === "mcpImportForm") saveMcpImport(event);
});
document.addEventListener("click", async event => {
  const entry = event.target.closest("[data-mcp-catalog]");
  if (entry) {
    const item = state.mcpCatalog.find(s => s.name === entry.dataset.mcpCatalog);
    $("mcpConfig").value = JSON.stringify({mcpServers: {[item.name]: item.config}}, null, 2);
    $("mcpConfig").focus(); $("mcpConfig").scrollIntoView({block: "center"});
  }
  const action = event.target.closest("[data-mcp-action]");
  if (action) {
    action.disabled = true;
    const reserved = action.dataset.mcpAction === "authorize" ? reserveAuthWindow() : null;
    try {
      const result = await api(`/admin/mcp/servers/${encodeURIComponent(action.dataset.mcpName)}/${action.dataset.mcpAction}`, {method: "POST", body: JSON.stringify({client_id: $("mcpClientId")?.value.trim() || ""})});
      if (result.error) throw Error(result.error);
      if (action.dataset.mcpAction === "authorize") await openMcpAuthorization(result, reserved);
      else { await loadIntegrations(); await loadSlashCommands(); await renderSettings(); }
    } catch (error) { reserved?.close(); state.integrationFeedback = error.message; await renderSettings(); toast(error.message, "error"); } finally { action.disabled = false; }
  }
});
