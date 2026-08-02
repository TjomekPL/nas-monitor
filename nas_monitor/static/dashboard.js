const REFRESH_MS = 20000;
const t = (key, params) => window.i18n.t(key, params);
const localeForLang = () => (window.i18n.currentLanguage() === "pl" ? "pl-PL" : "en-US");

function apiErrorMessage(data, res) {
  if (data && data.error_code) return window.i18n.errorText(data.error_code, data.error_context);
  return t("msg.httpError", { status: res.status });
}

function warningsText(warnings) {
  if (!warnings || !warnings.length) return "";
  return warnings.map((w) => window.i18n.warningText(w.code, w.context)).join(" ");
}

// --------------------------------------------------------------------
// Toast notifications - non-blocking, unlike window.alert() which
// freezes the whole JS event loop (including the 20s polling timers)
// until dismissed. A held-open alert let overdue polls pile up and fire
// in a burst on dismiss, which is exactly what caused a visible flash of
// stale data right after closing one.
// --------------------------------------------------------------------

function showToast(message, isError) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = "toast" + (isError ? " toast-error" : "");
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.classList.add("visible"), 10);
  setTimeout(() => {
    toast.classList.remove("visible");
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

// --------------------------------------------------------------------
// Theme toggle (light/dark) - the inline script in <head> already applied
// any saved choice before first paint; this just wires up the button and
// falls back to the OS preference when nothing has been explicitly chosen.
// --------------------------------------------------------------------

const SUN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5Z"/></svg>';

const themeToggleBtn = document.getElementById("theme-toggle");

function currentTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "light" || explicit === "dark") return explicit;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyThemeIcon() {
  themeToggleBtn.innerHTML = currentTheme() === "dark" ? SUN_ICON : MOON_ICON;
}

themeToggleBtn.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("nas-monitor-theme", next);
  applyThemeIcon();
});

applyThemeIcon();

// --------------------------------------------------------------------
// Language toggle - cycles through every language that has a loaded
// dictionary (currently PL/EN; adding a language automatically makes it
// part of the cycle, see nas_monitor/static/i18n/).
// --------------------------------------------------------------------

const langToggleBtn = document.getElementById("lang-toggle");

function applyLangLabel() {
  langToggleBtn.textContent = window.i18n.currentLanguage().toUpperCase();
}

langToggleBtn.addEventListener("click", () => {
  const langs = window.i18n.availableLanguages();
  const idx = langs.indexOf(window.i18n.currentLanguage());
  const next = langs[(idx + 1) % langs.length];
  window.i18n.setLanguage(next);
});

window.i18n.onLanguageChange(() => {
  applyLangLabel();
  rerenderEverything();
});

applyLangLabel();

// --------------------------------------------------------------------
// Tabs
// --------------------------------------------------------------------

const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanels = document.querySelectorAll(".tab-panel");

function activateTab(name) {
  let matched = false;
  tabButtons.forEach((btn) => {
    const isMatch = btn.dataset.tab === name;
    btn.classList.toggle("active", isMatch);
    if (isMatch) matched = true;
  });
  tabPanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.tab === name));
  if (matched) localStorage.setItem("nas-monitor-tab", name);
}

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

activateTab(localStorage.getItem("nas-monitor-tab") || tabButtons[0].dataset.tab);

const raidContainer = document.getElementById("raid-container");
const disksContainer = document.getElementById("disks-container");
const lastUpdatedEl = document.getElementById("last-updated");
const connDot = document.getElementById("conn-dot");

const raidTemplate = document.getElementById("raid-card-template");
const diskTemplate = document.getElementById("disk-card-template");

function fmtTemp(c) {
  return c === null || c === undefined ? "\u2013" : `${c}\u00b0C`;
}

function fmtHours(h) {
  if (h === null || h === undefined) return "\u2013";
  const days = Math.floor(h / 24);
  return t("msg.hoursDays", { hours: h, days });
}

function emptyState(container, text) {
  container.innerHTML = `<p class="empty-state">${text}</p>`;
}

let lastRaidData = [];
let lastDisksData = [];

function renderRaid(arrays) {
  raidContainer.innerHTML = "";
  if (!arrays.length) {
    emptyState(raidContainer, t("msg.empty.raid"));
    return;
  }
  for (const arr of arrays) {
    const node = raidTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(node);
    node.querySelector(".badge").classList.add(arr.health || "unknown");
    node.querySelector(".name").textContent = arr.name;
    node.querySelector(".level").textContent = (arr.level || "").toUpperCase();
    node.querySelector(".state").textContent = arr.array_state || (arr.active ? "active" : "inactive");
    node.querySelector(".path").textContent = arr.path;
    const devices = (arr.devices || []).map((d) => d.device).filter(Boolean);
    node.querySelector(".devices").textContent = devices.length ? devices.join(", ") : (arr.num_devices ? t("msg.diskCount", { count: arr.num_devices }) : "\u2013");

    const progressRow = node.querySelector(".progress-row");
    if (arr.progress_percent !== null && arr.progress_percent !== undefined) {
      progressRow.classList.add("visible");
      node.querySelector(".progress").textContent = `${arr.progress_action} ${arr.progress_percent.toFixed(1)}%`;
    }

    if (arr.error) {
      const err = node.querySelector(".error");
      err.textContent = arr.error;
      err.classList.add("visible");
    }

    raidContainer.appendChild(node);
  }
}

function renderDisks(disks) {
  disksContainer.innerHTML = "";
  if (!disks.length) {
    emptyState(disksContainer, t("msg.empty.disks"));
    return;
  }
  for (const disk of disks) {
    const node = diskTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(node);
    node.querySelector(".badge").classList.add(disk.health || "unknown");
    node.querySelector(".name").textContent = disk.path;
    node.querySelector(".model").textContent = disk.model || "";
    node.querySelector(".size").textContent = disk.size;
    node.querySelector(".serial").textContent = disk.serial;

    const smart = disk.smart || {};
    node.querySelector(".temp").textContent = fmtTemp(smart.temperature_c);
    node.querySelector(".hours").textContent = fmtHours(smart.power_on_hours);

    if (smart.error) {
      const err = node.querySelector(".error");
      err.textContent = smart.error;
      err.classList.add("visible");
    }

    disksContainer.appendChild(node);
  }
}

async function refresh() {
  if (addUserDialog.open) return; // avoid DOM churn while a password field is focused
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastRaidData = data.raid || [];
    lastDisksData = data.disks || [];
    renderRaid(lastRaidData);
    renderDisks(lastDisksData);
    lastUpdatedEl.textContent = t("msg.lastUpdated", { time: new Date().toLocaleTimeString(localeForLang()) });
    connDot.classList.remove("stale");
  } catch (err) {
    connDot.classList.add("stale");
    lastUpdatedEl.textContent = t("msg.connectionError", { detail: err.message });
  }
}

// --------------------------------------------------------------------
// Users
// --------------------------------------------------------------------

const usersContainer = document.getElementById("users-container");
const userRowTemplate = document.getElementById("user-row-template");
const addUserDialog = document.getElementById("add-user-dialog");
const addUserForm = document.getElementById("add-user-form");
const addUserBtn = document.getElementById("add-user-btn");
const addUserCancel = document.getElementById("add-user-cancel");
const addUserError = document.getElementById("add-user-error");
const groupsChecklist = document.getElementById("groups-checklist");
const dialogTitle = document.getElementById("user-dialog-title");
const usernameLabel = document.getElementById("username-label");
const passwordLabel = document.getElementById("password-label");
const usernameInput = document.getElementById("new-username");
const usernamePreview = document.getElementById("username-preview");
const passwordInput = document.getElementById("new-password");
const submitBtn = document.getElementById("add-user-submit");

let knownGroups = [];
let editingUsername = null; // null = create mode, otherwise the account being edited

function renderUsers(usersList) {
  usersContainer.innerHTML = "";
  if (!usersList.length) {
    emptyState(usersContainer, t("msg.empty.users"));
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>${t("ui.users.colUser")}</th><th>${t("ui.users.colLogin")}</th><th>${t("ui.users.colSmb")}</th><th>${t("ui.users.colGroups")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const u of usersList) {
    const row = userRowTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(row);
    const displayName = u.display_name || u.username;
    row.querySelector(".display-name").textContent = displayName;
    row.querySelector(".display-name").title = displayName;
    const subEl = row.querySelector(".username.sub");
    if (displayName !== u.username) {
      subEl.textContent = t("msg.accountLabel", { username: u.username });
    } else {
      subEl.remove();
    }

    const loginPill = row.querySelector(".login-cell .pill");
    loginPill.textContent = u.can_login ? t("msg.yes") : t("msg.no");
    loginPill.classList.add(u.can_login ? "pill-warn" : "pill-ok");

    const smbPill = row.querySelector(".smb-cell .pill");
    smbPill.textContent = u.has_smb ? t("msg.yes") : t("msg.no");
    smbPill.classList.add(u.has_smb ? "pill-ok" : "pill-neutral");

    row.querySelector(".groups").textContent = u.groups && u.groups.length ? u.groups.join(", ") : "\u2013";

    row.querySelector(".edit-btn").addEventListener("click", () => openUserDialog("edit", u));

    const smbRemoveBtn = row.querySelector(".smb-remove-btn");
    if (u.has_smb) {
      smbRemoveBtn.addEventListener("click", () => removeSmbAccess(u.username, displayName));
    } else {
      smbRemoveBtn.remove();
    }

    row.querySelector(".delete-btn").addEventListener("click", () => deleteUser(u.username, displayName));

    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  usersContainer.appendChild(table);
}

function renderGroupsChecklist(groups, checkedNames) {
  knownGroups = groups;
  const checked = new Set(checkedNames || []);
  groupsChecklist.innerHTML = "";
  if (!groups.length) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = t("ui.addUserDialog.noGroupsHint");
    groupsChecklist.appendChild(p);
    return;
  }
  for (const g of groups) {
    const label = document.createElement("label");
    label.className = "inline";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = g.name;
    cb.name = "group";
    cb.checked = checked.has(g.name);
    label.appendChild(cb);
    label.append(` ${g.name}`);
    groupsChecklist.appendChild(label);
  }
}

let lastKnownGroupsData = [];
let lastKnownUsersData = [];

async function loadUsers() {
  if (addUserDialog.open) return; // don't rebuild the table under an open form
  try {
    const res = await fetch("/api/users");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastKnownUsersData = data.users || [];
    renderUsers(lastKnownUsersData);
    lastKnownGroupsData = data.groups || [];
    renderGroupsChecklist(lastKnownGroupsData);
  } catch (err) {
    emptyState(usersContainer, t("msg.loadErrorUsers", { detail: err.message }));
  }
}

function updateUsernamePreview() {
  if (editingUsername) return; // preview only makes sense while naming a new account
  const raw = usernameInput.value.trim();
  if (!raw) {
    usernamePreview.textContent = "";
    return;
  }
  const resolved = raw.toLowerCase();
  usernamePreview.textContent = resolved === raw
    ? ""
    : t("ui.addUserDialog.accountPreview", { account: resolved, raw });
}
usernameInput.addEventListener("input", updateUsernamePreview);

function openUserDialog(mode, user) {
  addUserForm.reset();
  addUserError.textContent = "";
  usernamePreview.textContent = "";

  if (mode === "edit") {
    editingUsername = user.username;
    dialogTitle.textContent = t("ui.addUserDialog.titleEdit", { name: user.display_name || user.username });
    usernameLabel.querySelector(".label-text").textContent = t("ui.addUserDialog.usernameLabelEdit");
    usernameInput.value = user.display_name || user.username;
    usernameInput.disabled = false; // still editable - it's just the display name now
    usernamePreview.textContent = t("ui.addUserDialog.accountFixedPreview", { account: user.username });
    passwordLabel.querySelector(".label-text").textContent = t("ui.addUserDialog.passwordLabelEdit");
    passwordInput.required = false;
    passwordInput.placeholder = t("ui.addUserDialog.passwordPlaceholderEdit");
    document.getElementById("new-allow-login").checked = user.can_login;
    renderGroupsChecklist(lastKnownGroupsData, user.groups);
    submitBtn.textContent = t("ui.addUserDialog.saveBtn");
  } else {
    editingUsername = null;
    dialogTitle.textContent = t("ui.addUserDialog.titleNew");
    usernameLabel.querySelector(".label-text").textContent = t("ui.addUserDialog.usernameLabelNew");
    usernameInput.disabled = false;
    passwordLabel.querySelector(".label-text").textContent = t("ui.addUserDialog.passwordLabelNew");
    passwordInput.required = true;
    passwordInput.placeholder = "";
    renderGroupsChecklist(lastKnownGroupsData);
    submitBtn.textContent = t("ui.addUserDialog.createBtn");
  }

  addUserDialog.showModal();
}

addUserBtn.addEventListener("click", () => openUserDialog("create"));
addUserCancel.addEventListener("click", () => addUserDialog.close());

addUserForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  addUserError.textContent = "";

  const nameField = usernameInput.value.trim();
  const password = passwordInput.value;
  const allowLogin = document.getElementById("new-allow-login").checked;
  const newGroupName = document.getElementById("new-group-name").value.trim();

  const groups = Array.from(groupsChecklist.querySelectorAll("input[name='group']:checked")).map((cb) => cb.value);
  if (newGroupName) groups.push(newGroupName);

  let confirmMsg, url, body;
  if (editingUsername) {
    confirmMsg = t("msg.confirmSaveUser", { name: editingUsername });
    url = `/api/users/${encodeURIComponent(editingUsername)}/update`;
    body = { display_name: nameField, groups, allow_login: allowLogin, password };
  } else {
    const resolvedAccount = nameField.toLowerCase();
    const accountNote = resolvedAccount !== nameField ? t("msg.accountNote", { account: resolvedAccount }) : "";
    confirmMsg = allowLogin
      ? t("msg.confirmCreateUserWithLogin", { name: nameField, note: accountNote })
      : t("msg.confirmCreateUserNoLogin", { name: nameField, note: accountNote });
    url = "/api/users/create";
    body = { username: nameField, password, groups, allow_login: allowLogin };
  }
  if (!window.confirm(confirmMsg)) return;

  submitBtn.disabled = true;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      addUserError.textContent = apiErrorMessage(data, res);
      return;
    }
    addUserDialog.close();
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    addUserError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    submitBtn.disabled = false;
  }
});

async function removeSmbAccess(username, displayName) {
  if (!window.confirm(t("msg.confirmRemoveSmb", { name: displayName }))) return;
  try {
    const res = await fetch(`/api/users/${encodeURIComponent(username)}/remove-smb`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

async function deleteUser(username, displayName) {
  const removeHome = window.confirm(t("msg.confirmDeleteUser", { name: displayName, username }));
  if (!removeHome) return;
  try {
    const res = await fetch(`/api/users/${encodeURIComponent(username)}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remove_home: false }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

// --------------------------------------------------------------------
// Shares
// --------------------------------------------------------------------

const sharesContainer = document.getElementById("shares-container");
const shareRowTemplate = document.getElementById("share-row-template");
const shareDialog = document.getElementById("share-dialog");
const shareForm = document.getElementById("share-form");
const addShareBtn = document.getElementById("add-share-btn");
const shareCancel = document.getElementById("share-cancel");
const shareError = document.getElementById("share-error");
const shareDialogTitle = document.getElementById("share-dialog-title");
const shareNameInput = document.getElementById("share-name");
const sharePathPreview = document.getElementById("share-path-preview");
const shareCommentInput = document.getElementById("share-comment");
const sharePermissionsList = document.getElementById("share-permissions-list");
const shareSubmitBtn = document.getElementById("share-submit");

function permissionLabels() {
  return { none: t("ui.shares.permNone"), ro: t("ui.shares.permRo"), rw: t("ui.shares.permRw") };
}

let editingShareName = null;
let lastSharesData = [];

function formatPermissionsSummary(permissions) {
  const entries = Object.entries(permissions || {});
  if (!entries.length) return t("ui.shares.noAccess");
  return entries
    .map(([user, level]) => {
      const u = lastKnownUsersData.find((x) => x.username === user);
      const label = u ? (u.display_name || u.username) : user;
      return `${label} (${level === "rw" ? t("ui.shares.permSummaryRw") : t("ui.shares.permSummaryRo")})`;
    })
    .join(", ");
}

function renderShares(sharesList) {
  sharesContainer.innerHTML = "";
  if (!sharesList.length) {
    emptyState(sharesContainer, t("msg.empty.shares"));
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>${t("ui.shares.colShare")}</th><th>${t("ui.shares.colComment")}</th><th>${t("ui.shares.colAccess")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const sh of sharesList) {
    const row = shareRowTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(row);
    const shareLabel = sh.name + (sh.managed ? "" : t("ui.shares.notManagedSuffix"));
    const shareNameEl = row.querySelector(".display-name");
    shareNameEl.textContent = shareLabel;
    shareNameEl.title = shareLabel;
    row.querySelector(".path").textContent = sh.path;
    row.querySelector(".comment").textContent = sh.comment || "\u2013";
    row.querySelector(".share-users").textContent = formatPermissionsSummary(sh.permissions);

    const editBtn = row.querySelector(".edit-btn");
    const deleteBtn = row.querySelector(".delete-btn");
    if (sh.managed) {
      editBtn.addEventListener("click", () => openShareDialog("edit", sh));
      deleteBtn.addEventListener("click", () => deleteShare(sh.name));
    } else {
      // shares defined directly in the main smb.conf (not by this tool)
      // aren't safe to rewrite through the managed-file mechanism
      editBtn.remove();
      deleteBtn.remove();
    }

    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  sharesContainer.appendChild(table);
}

function populateSharePermissionsList(existingPermissions) {
  const current = existingPermissions || {};
  sharePermissionsList.innerHTML = "";
  if (!lastKnownUsersData.length) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = t("ui.shareDialog.noUsersHint");
    sharePermissionsList.appendChild(p);
    return;
  }
  for (const u of lastKnownUsersData) {
    const row = document.createElement("div");
    row.className = "permission-row";

    const info = document.createElement("div");
    info.className = "permission-info";

    const name = document.createElement("span");
    name.className = "permission-user";
    const displayName = u.display_name || u.username;
    name.textContent = displayName;
    name.title = displayName;
    info.appendChild(name);

    if (!u.has_smb) {
      const warn = document.createElement("span");
      warn.className = "field-hint";
      warn.style.color = "var(--warn)";
      warn.textContent = t("ui.shares.noSmbPasswordHint");
      info.appendChild(warn);
    }
    row.appendChild(info);

    const select = document.createElement("select");
    select.dataset.username = u.username;
    select.className = "permission-select";
    for (const [value, label] of Object.entries(permissionLabels())) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      select.appendChild(opt);
    }
    select.value = current[u.username] || "none";
    row.appendChild(select);

    sharePermissionsList.appendChild(row);
  }
}

function collectSharePermissions() {
  const permissions = {};
  sharePermissionsList.querySelectorAll(".permission-select").forEach((select) => {
    if (select.value !== "none") {
      permissions[select.dataset.username] = select.value;
    }
  });
  return permissions;
}

async function loadShares() {
  if (shareDialog.open) return;
  try {
    const res = await fetch("/api/shares");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastSharesData = data.shares || [];
    renderShares(lastSharesData);
  } catch (err) {
    emptyState(sharesContainer, t("msg.loadErrorShares", { detail: err.message }));
  }
}

function updateSharePathPreview() {
  const raw = shareNameInput.value.trim().toLowerCase();
  sharePathPreview.textContent = raw ? t("ui.shareDialog.pathPreview", { name: raw }) : "";
}
shareNameInput.addEventListener("input", updateSharePathPreview);

function openShareDialog(mode, share) {
  shareForm.reset();
  shareError.textContent = "";
  populateSharePermissionsList(mode === "edit" ? share.permissions : undefined);

  if (mode === "edit") {
    editingShareName = share.name;
    shareDialogTitle.textContent = t("ui.shareDialog.titleEdit", { name: share.name });
    shareNameInput.value = share.name;
    shareNameInput.disabled = true; // renaming not supported - delete + recreate instead
    sharePathPreview.textContent = t("ui.shareDialog.pathPreviewFixed", { path: share.path });
    shareCommentInput.value = share.comment || "";
    shareSubmitBtn.textContent = t("ui.shareDialog.saveBtn");
  } else {
    editingShareName = null;
    shareDialogTitle.textContent = t("ui.shareDialog.titleNew");
    shareNameInput.disabled = false;
    sharePathPreview.textContent = "";
    shareSubmitBtn.textContent = t("ui.shareDialog.createBtn");
  }

  shareDialog.showModal();
}

addShareBtn.addEventListener("click", () => openShareDialog("create"));
shareCancel.addEventListener("click", () => shareDialog.close());

shareForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  shareError.textContent = "";

  const comment = shareCommentInput.value.trim();
  const permissions = collectSharePermissions();

  let url, body, confirmMsg;
  const summary = formatPermissionsSummary(permissions);
  if (editingShareName) {
    confirmMsg = t("msg.confirmSaveShare", { name: editingShareName, summary });
    url = `/api/shares/${encodeURIComponent(editingShareName)}/update`;
    body = { comment, permissions };
  } else {
    const name = shareNameInput.value.trim().toLowerCase();
    confirmMsg = t("msg.confirmCreateShare", { name, summary });
    url = "/api/shares/create";
    body = { name, comment, permissions };
  }
  if (!window.confirm(confirmMsg)) return;

  shareSubmitBtn.disabled = true;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      shareError.textContent = apiErrorMessage(data, res);
      return;
    }
    if (data.share && data.share.warnings && data.share.warnings.length) {
      showToast(warningsText(data.share.warnings), true);
    }
    shareDialog.close();
    await loadShares();
  } catch (err) {
    shareError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    shareSubmitBtn.disabled = false;
  }
});

async function deleteShare(name) {
  const deleteFiles = window.confirm(t("msg.confirmDeleteShare", { name }));
  if (!deleteFiles) return;
  try {
    const res = await fetch(`/api/shares/${encodeURIComponent(name)}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delete_files: false }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    if (data.share && data.share.warnings && data.share.warnings.length) {
      showToast(warningsText(data.share.warnings), true);
    }
    await loadShares();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

// --------------------------------------------------------------------
// SSH keys ("Certyfikaty")
// --------------------------------------------------------------------

const sshKeysContainer = document.getElementById("ssh-keys-container");
const sshKeyRowTemplate = document.getElementById("ssh-key-row-template");
const deployKeyDialog = document.getElementById("deploy-key-dialog");
const deployKeyForm = document.getElementById("deploy-key-form");
const deployKeyTitle = document.getElementById("deploy-key-title");
const deployKeyCancel = document.getElementById("deploy-key-cancel");
const deployKeyError = document.getElementById("deploy-key-error");
const deployHostInput = document.getElementById("deploy-host");
const deployDisplayNameInput = document.getElementById("deploy-display-name");
const deployRemoteUserInput = document.getElementById("deploy-remote-user");
const deployPasswordInput = document.getElementById("deploy-password");
const deployKeySubmitBtn = document.getElementById("deploy-key-submit");

const removeDeploymentDialog = document.getElementById("remove-deployment-dialog");
const removeDeploymentForm = document.getElementById("remove-deployment-form");
const removeDeploymentTitle = document.getElementById("remove-deployment-title");
const removeDeploymentCancel = document.getElementById("remove-deployment-cancel");
const removeDeploymentError = document.getElementById("remove-deployment-error");
const removeDeploymentPasswordInput = document.getElementById("remove-deployment-password");
const removeDeploymentSubmitBtn = document.getElementById("remove-deployment-submit");

let deployingKeyForUsername = null;
let removingDeployment = null; // { username, host, remote_user }
let lastSshKeysData = [];

function renderSshKeys(keysList) {
  sshKeysContainer.innerHTML = "";
  if (!keysList.length) {
    emptyState(sshKeysContainer, t("msg.empty.sshKeys"));
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>${t("ui.certs.colUser")}</th><th>${t("ui.certs.colKey")}</th><th>${t("ui.certs.colSentTo")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const k of keysList) {
    if (k.error_code) continue; // user lookup failed - skip silently, shouldn't normally happen
    const row = sshKeyRowTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(row);
    const u = lastKnownUsersData.find((x) => x.username === k.username);
    const keyLabel = (u && (u.display_name || u.username)) || k.username;
    const keyNameEl = row.querySelector(".display-name");
    keyNameEl.textContent = keyLabel;
    keyNameEl.title = keyLabel;

    const pill = row.querySelector(".key-cell .pill");
    pill.textContent = k.has_key ? t("ui.certs.keyPresent") : t("ui.certs.keyAbsent");
    pill.classList.add(k.has_key ? "pill-ok" : "pill-neutral");

    const depCell = row.querySelector(".deployments-cell");
    const deployments = k.deployments || [];
    if (!deployments.length) {
      depCell.textContent = "\u2013";
    } else {
      for (const dep of deployments) {
        const pillEl = document.createElement("span");
        pillEl.className = "deployment-pill " + (dep.is_current ? "current" : "stale");
        pillEl.title = dep.is_current
          ? t("ui.certs.deploymentCurrentTitle", { user: dep.remote_user, host: dep.host })
          : t("ui.certs.deploymentStaleTitle", { user: dep.remote_user, host: dep.host });
        const label = document.createElement("span");
        label.textContent = dep.display_name;
        pillEl.appendChild(label);
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.textContent = "\u00d7";
        removeBtn.title = t("ui.certs.removeFromDeviceTitle");
        removeBtn.addEventListener("click", () => openRemoveDeploymentDialog(k.username, dep));
        pillEl.appendChild(removeBtn);
        depCell.appendChild(pillEl);
      }
    }

    const generateBtn = row.querySelector(".generate-btn");
    const deployBtn = row.querySelector(".deploy-btn");
    const deleteBtn = row.querySelector(".delete-key-btn");

    if (k.has_key) {
      generateBtn.remove();
      deployBtn.addEventListener("click", () => openDeployDialog(k.username));
      deleteBtn.addEventListener("click", () => deleteSshKey(k.username));
    } else {
      deployBtn.remove();
      deleteBtn.remove();
      if (!k.can_login) {
        generateBtn.disabled = true;
        generateBtn.title = t("ui.certs.loginDisabledHint");
      } else {
        generateBtn.addEventListener("click", () => generateSshKey(k.username));
      }
    }

    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  sshKeysContainer.appendChild(table);
}

async function loadSshKeys() {
  if (deployKeyDialog.open || removeDeploymentDialog.open) return;
  try {
    const res = await fetch("/api/ssh-keys");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastSshKeysData = data.keys || [];
    renderSshKeys(lastSshKeysData);
  } catch (err) {
    emptyState(sshKeysContainer, t("msg.loadErrorSshKeys", { detail: err.message }));
  }
}

async function generateSshKey(username) {
  if (!window.confirm(t("msg.confirmGenerateKey", { username }))) return;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(username)}/generate`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadSshKeys();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

async function deleteSshKey(username) {
  if (!window.confirm(t("msg.confirmDeleteKey", { username }))) return;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(username)}/delete`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadSshKeys();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

function openDeployDialog(username) {
  deployKeyForm.reset();
  deployKeyError.textContent = "";
  deployingKeyForUsername = username;
  deployKeyTitle.textContent = t("ui.deployKeyDialog.titleTarget", { username });
  deployKeyDialog.showModal();
}

deployKeyCancel.addEventListener("click", () => deployKeyDialog.close());

deployKeyForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  deployKeyError.textContent = "";

  const remoteHost = deployHostInput.value.trim();
  const displayName = deployDisplayNameInput.value.trim();
  const remoteUser = deployRemoteUserInput.value.trim();
  const remotePassword = deployPasswordInput.value;

  if (!window.confirm(t("msg.confirmDeployKey", { user: remoteUser, host: remoteHost }))) return;

  deployKeySubmitBtn.disabled = true;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(deployingKeyForUsername)}/deploy`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        remote_host: remoteHost,
        remote_user: remoteUser,
        remote_password: remotePassword,
        display_name: displayName,
      }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      deployKeyError.textContent = apiErrorMessage(data, res);
      return;
    }
    deployKeyDialog.close();
    showToast(t("msg.keyDeployed"));
    await loadSshKeys();
  } catch (err) {
    deployKeyError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    deployKeySubmitBtn.disabled = false;
  }
});

function openRemoveDeploymentDialog(username, deployment) {
  removeDeploymentForm.reset();
  removeDeploymentError.textContent = "";
  removingDeployment = { username, host: deployment.host, remote_user: deployment.remote_user };
  removeDeploymentTitle.textContent = t("ui.removeDeploymentDialog.titleTarget", { user: deployment.remote_user, host: deployment.host });
  removeDeploymentDialog.showModal();
}

removeDeploymentCancel.addEventListener("click", () => removeDeploymentDialog.close());

removeDeploymentForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  removeDeploymentError.textContent = "";

  const password = removeDeploymentPasswordInput.value;
  const { username, host, remote_user } = removingDeployment;

  if (!window.confirm(t("msg.confirmRemoveDeployment", { user: remote_user, host }))) return;

  removeDeploymentSubmitBtn.disabled = true;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(username)}/deployments/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote_host: host, remote_user, remote_password: password }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      removeDeploymentError.textContent = apiErrorMessage(data, res);
      return;
    }
    removeDeploymentDialog.close();
    await loadSshKeys();
  } catch (err) {
    removeDeploymentError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    removeDeploymentSubmitBtn.disabled = false;
  }
});

loadShares();
setInterval(loadShares, REFRESH_MS);

// --------------------------------------------------------------------
// Network ("Sieć") - read-only detection for now
// --------------------------------------------------------------------

const networkContainer = document.getElementById("network-container");
let lastNetworkData = null;

function formatIfaceType(type) {
  if (!type || !type.kind) return "";
  const kindLabel = t(`net.kind.${type.kind}`);
  if (!type.bus) return kindLabel;
  return `${kindLabel} (${t(`net.bus.${type.bus}`)})`;
}

function renderNetwork(data) {
  networkContainer.innerHTML = "";

  const summary = document.createElement("div");
  summary.className = "cards";

  const overview = document.createElement("div");
  overview.className = "card";
  overview.innerHTML = `
    <div class="card-head"><span class="name">${t("ui.network.overviewCardTitle")}</span></div>
    <dl class="facts">
      <div><dt>${t("ui.network.hostname")}</dt><dd class="mono">${data.hostname || "\u2013"}</dd></div>
      <div><dt>${t("ui.network.managedBy")}</dt><dd>${t(`net.backend.${data.backend}`)}</dd></div>
      <div><dt>${t("ui.network.dnsServers")}</dt><dd class="mono">${(data.dns_servers || []).join(", ") || "\u2013"}</dd></div>
    </dl>
  `;
  summary.appendChild(overview);
  networkContainer.appendChild(summary);

  if (data.error_code) {
    const err = document.createElement("p");
    err.className = "error visible";
    err.textContent = window.i18n.errorText(data.error_code, data.error_context);
    networkContainer.appendChild(err);
  }

  if (!data.interfaces || !data.interfaces.length) {
    emptyState(networkContainer, t("msg.empty.networkInterfaces"));
    return;
  }

  const ifaceCards = document.createElement("div");
  ifaceCards.className = "cards";
  for (const iface of data.interfaces) {
    const card = document.createElement("div");
    card.className = "card";
    const addr = iface.addresses[0];
    const typeLabel = formatIfaceType(iface.type);
    card.innerHTML = `
      <div class="card-head">
        <span class="badge ${iface.effective_up ? 'ok' : 'unknown'}"></span>
        <span class="name mono">${iface.name}</span>
        <span class="level">${typeLabel ? `(${typeLabel})` : ""} ${iface.state}</span>
      </div>
      <dl class="facts">
        <div><dt>${t("ui.network.ip")}</dt><dd class="mono">${addr ? addr.address + "/" + addr.prefixlen : "\u2013"}</dd></div>
        <div><dt>${t("ui.network.netmask")}</dt><dd class="mono">${addr ? addr.netmask : "\u2013"}</dd></div>
        <div><dt>${t("ui.network.gateway")}</dt><dd class="mono">${iface.gateway || "\u2013"}</dd></div>
        <div><dt>${t("ui.network.mac")}</dt><dd class="mono">${iface.mac || "\u2013"}</dd></div>
      </dl>
    `;
    ifaceCards.appendChild(card);
  }
  networkContainer.appendChild(ifaceCards);
}

async function loadNetwork() {
  try {
    const res = await fetch("/api/network");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastNetworkData = data;
    renderNetwork(data);
  } catch (err) {
    emptyState(networkContainer, t("msg.loadErrorNetwork", { detail: err.message }));
  }
}

loadNetwork();
setInterval(loadNetwork, REFRESH_MS);

// --------------------------------------------------------------------
// Log operacji - collapsed-by-default entries; full raw message only
// shown once expanded, with a copy button. Time-range filter + a
// configurable retention cap (persisted server-side).
// --------------------------------------------------------------------

const logContainer = document.getElementById("log-container");
const logEntryTemplate = document.getElementById("log-entry-template");
const logClearBtn = document.getElementById("log-clear-btn");
const logFilterSince = document.getElementById("log-filter-since");
const logFilterUntil = document.getElementById("log-filter-until");
const logFilterClearBtn = document.getElementById("log-filter-clear-btn");
const logMaxEntriesInput = document.getElementById("log-max-entries");

const expandedLogIds = new Set();
let lastLogEvents = [];

function localInputToIso(value) {
  // <input type="datetime-local"> gives local time with no offset
  // (e.g. "2026-08-02T10:30"); interpret it as local and convert to a
  // real UTC ISO timestamp so it compares correctly against stored
  // event timestamps.
  if (!value) return null;
  const d = new Date(value);
  if (isNaN(d.getTime())) return null;
  return d.toISOString();
}

function fmtLogTime(iso) {
  try {
    return new Date(iso).toLocaleString(localeForLang(), {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function logEntrySummary(ev) {
  // Older, pre-i18n entries persisted a plain "summary" string directly
  // (before the log started storing category/action/params) - shown
  // verbatim, since there's no code to re-translate it from.
  if (ev.category && ev.action) {
    return window.i18n.logSummary(ev.category, ev.action, ev.status, ev.params || {});
  }
  return ev.summary || "";
}

function renderLog(events) {
  logContainer.innerHTML = "";
  if (!events.length) {
    emptyState(logContainer, t("msg.empty.log"));
    return;
  }
  for (const ev of events) {
    const node = logEntryTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(node);
    const article = node.querySelector(".log-entry");
    const isOk = ev.status === "success";
    const pill = node.querySelector(".status-pill");
    pill.classList.add(isOk ? "pill-ok" : "pill-crit");
    pill.textContent = isOk ? t("ui.log.statusSuccess") : t("ui.log.statusFailure");
    const summaryText = logEntrySummary(ev);
    node.querySelector(".log-summary").textContent = summaryText;
    node.querySelector(".log-time").textContent = fmtLogTime(ev.timestamp);

    const messageEl = node.querySelector(".log-message");
    messageEl.textContent = ev.message && ev.message.trim() ? ev.message : t("ui.log.noDetail");

    if (expandedLogIds.has(ev.id)) article.classList.add("expanded");

    node.querySelector(".log-entry-head").addEventListener("click", () => {
      const nowExpanded = article.classList.toggle("expanded");
      if (nowExpanded) expandedLogIds.add(ev.id);
      else expandedLogIds.delete(ev.id);
    });

    node.querySelector(".log-copy-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(`${summaryText}\n${fmtLogTime(ev.timestamp)}\n\n${messageEl.textContent}`);
        showToast(t("msg.copiedToClipboard"));
      } catch {
        showToast(t("msg.copyFailed"), true);
      }
    });

    logContainer.appendChild(node);
  }
}

async function loadLog() {
  try {
    const params = new URLSearchParams();
    const since = localInputToIso(logFilterSince.value);
    const until = localInputToIso(logFilterUntil.value);
    if (since) params.set("since", since);
    if (until) params.set("until", until);

    const res = await fetch(`/api/log?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastLogEvents = data.events || [];
    renderLog(lastLogEvents);
    if (document.activeElement !== logMaxEntriesInput) {
      logMaxEntriesInput.value = data.max_entries;
    }
  } catch (err) {
    emptyState(logContainer, t("msg.loadErrorLog", { detail: err.message }));
  }
}

logFilterSince.addEventListener("change", loadLog);
logFilterUntil.addEventListener("change", loadLog);

logFilterClearBtn.addEventListener("click", () => {
  logFilterSince.value = "";
  logFilterUntil.value = "";
  loadLog();
});

logClearBtn.addEventListener("click", async () => {
  if (!confirm(t("msg.logClearConfirm"))) return;
  try {
    const res = await fetch("/api/log/clear", { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(apiErrorMessage(data, res));
    expandedLogIds.clear();
    await loadLog();
  } catch (err) {
    showToast(t("msg.logClearFailed", { detail: err.message }), true);
  }
});

logMaxEntriesInput.addEventListener("change", async () => {
  const value = parseInt(logMaxEntriesInput.value, 10);
  if (!value || value < 10 || value > 1000) {
    showToast(t("msg.logMaxEntriesInvalid"), true);
    await loadLog();
    return;
  }
  try {
    const res = await fetch("/api/log/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_entries: value }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(apiErrorMessage(data, res));
    showToast(t("msg.logMaxEntriesSaved"));
    await loadLog();
  } catch (err) {
    showToast(t("msg.logMaxEntriesSaveFailed", { detail: err.message }), true);
  }
});

loadLog();
setInterval(loadLog, REFRESH_MS);

// --------------------------------------------------------------------
// Re-render every section's cached data with the newly selected
// language - called once by the i18n language-change listener above.
// Static markup (data-i18n elements already in the DOM) is retranslated
// by window.i18n.setLanguage() itself before this runs.
// --------------------------------------------------------------------

function rerenderEverything() {
  renderRaid(lastRaidData);
  renderDisks(lastDisksData);
  renderUsers(lastKnownUsersData);
  renderGroupsChecklist(lastKnownGroupsData);
  renderShares(lastSharesData);
  renderSshKeys(lastSshKeysData);
  if (lastNetworkData) renderNetwork(lastNetworkData);
  renderLog(lastLogEvents);
}

// Kick off polling now that everything above is declared.
loadUsers();
setInterval(loadUsers, REFRESH_MS);
loadSshKeys();
setInterval(loadSshKeys, REFRESH_MS);
refresh();
setInterval(refresh, REFRESH_MS);
