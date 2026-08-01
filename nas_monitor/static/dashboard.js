const REFRESH_MS = 20000;

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
  return `${h} h (~${days} dni)`;
}

function emptyState(container, text) {
  container.innerHTML = `<p class="empty-state">${text}</p>`;
}

function renderRaid(arrays) {
  raidContainer.innerHTML = "";
  if (!arrays.length) {
    emptyState(raidContainer, "Brak wykrytych macierzy RAID na tym hoście.");
    return;
  }
  for (const arr of arrays) {
    const node = raidTemplate.content.cloneNode(true);
    node.querySelector(".badge").classList.add(arr.health || "unknown");
    node.querySelector(".name").textContent = arr.name;
    node.querySelector(".level").textContent = (arr.level || "").toUpperCase();
    node.querySelector(".state").textContent = arr.array_state || (arr.active ? "active" : "inactive");
    node.querySelector(".path").textContent = arr.path;
    const devices = (arr.devices || []).map((d) => d.device).filter(Boolean);
    node.querySelector(".devices").textContent = devices.length ? devices.join(", ") : (arr.num_devices ? `${arr.num_devices} urządzeń` : "\u2013");

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
    emptyState(disksContainer, "Nie wykryto dysków (lsblk niedostępny lub brak uprawnień).");
    return;
  }
  for (const disk of disks) {
    const node = diskTemplate.content.cloneNode(true);
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
    renderRaid(data.raid || []);
    renderDisks(data.disks || []);
    lastUpdatedEl.textContent = `zaktualizowano ${new Date().toLocaleTimeString("pl-PL")}`;
    connDot.classList.remove("stale");
  } catch (err) {
    connDot.classList.add("stale");
    lastUpdatedEl.textContent = `błąd połączenia (${err.message})`;
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
    emptyState(usersContainer, "Nie wykryto żadnych kont użytkowników.");
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Użytkownik</th><th>Logowanie/SSH</th><th>Dostęp SMB</th><th>Grupy</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const u of usersList) {
    const row = userRowTemplate.content.cloneNode(true);
    const displayName = u.display_name || u.username;
    row.querySelector(".display-name").textContent = displayName;
    row.querySelector(".display-name").title = displayName;
    const subEl = row.querySelector(".username.sub");
    if (displayName !== u.username) {
      subEl.textContent = `konto: ${u.username}`;
    } else {
      subEl.remove();
    }

    const loginPill = row.querySelector(".login-cell .pill");
    loginPill.textContent = u.can_login ? "tak" : "nie";
    loginPill.classList.add(u.can_login ? "pill-warn" : "pill-ok");

    const smbPill = row.querySelector(".smb-cell .pill");
    smbPill.textContent = u.has_smb ? "tak" : "nie";
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
    p.textContent = "Brak wykrytych grup - możesz utworzyć nową poniżej.";
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
    emptyState(usersContainer, `Błąd wczytywania użytkowników (${err.message})`);
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
    : `zostanie utworzone jako konto systemowe: ${resolved} (nazwa "${raw}" zostaje jako etykieta)`;
}
usernameInput.addEventListener("input", updateUsernamePreview);

function openUserDialog(mode, user) {
  addUserForm.reset();
  addUserError.textContent = "";
  usernamePreview.textContent = "";

  if (mode === "edit") {
    editingUsername = user.username;
    dialogTitle.textContent = `Edytuj: ${user.display_name || user.username}`;
    usernameLabel.querySelector(".label-text").textContent = "Nazwa (etykieta)";
    usernameInput.value = user.display_name || user.username;
    usernameInput.disabled = false; // still editable - it's just the display name now
    usernamePreview.textContent = `konto systemowe: ${user.username} (nie do zmiany tutaj)`;
    passwordLabel.querySelector(".label-text").textContent = "Nowe hasło SMB";
    passwordInput.required = false;
    passwordInput.placeholder = "zostaw puste, aby nie zmieniać";
    document.getElementById("new-allow-login").checked = user.can_login;
    renderGroupsChecklist(lastKnownGroupsData, user.groups);
    submitBtn.textContent = "Zapisz zmiany";
  } else {
    editingUsername = null;
    dialogTitle.textContent = "Nowy użytkownik";
    usernameLabel.querySelector(".label-text").textContent = "Nazwa użytkownika";
    usernameInput.disabled = false;
    passwordLabel.querySelector(".label-text").textContent = "Hasło SMB";
    passwordInput.required = true;
    passwordInput.placeholder = "";
    renderGroupsChecklist(lastKnownGroupsData);
    submitBtn.textContent = "Utwórz";
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
    confirmMsg = `Zapisać zmiany dla "${editingUsername}"?`;
    url = `/api/users/${encodeURIComponent(editingUsername)}/update`;
    body = { display_name: nameField, groups, allow_login: allowLogin, password };
  } else {
    const resolvedAccount = nameField.toLowerCase();
    const accountNote = resolvedAccount !== nameField ? ` (konto systemowe: ${resolvedAccount})` : "";
    confirmMsg = allowLogin
      ? `Utworzyć użytkownika "${nameField}"${accountNote} z możliwością logowania/SSH?`
      : `Utworzyć użytkownika "${nameField}"${accountNote} bez możliwości logowania (tylko SMB)?`;
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
      addUserError.textContent = data.error || `Błąd HTTP ${res.status}`;
      return;
    }
    addUserDialog.close();
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    addUserError.textContent = `Błąd połączenia: ${err.message}`;
  } finally {
    submitBtn.disabled = false;
  }
});

async function removeSmbAccess(username, displayName) {
  if (!window.confirm(`Usunąć dostęp SMB dla "${displayName}"? Konto systemowe zostanie zachowane.`)) return;
  try {
    const res = await fetch(`/api/users/${encodeURIComponent(username)}/remove-smb`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(data.error || `Błąd HTTP ${res.status}`, true);
      return;
    }
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    showToast(`Błąd połączenia: ${err.message}`, true);
  }
}

async function deleteUser(username, displayName) {
  const removeHome = window.confirm(
    `Usunąć użytkownika "${displayName}" (konto: ${username})? To usuwa konto systemowe i dostęp SMB.\n\n` +
    `Kliknij OK, aby usunąć BEZ katalogu domowego (bezpieczniej), Anuluj aby przerwać.`
  );
  if (!removeHome) return;
  try {
    const res = await fetch(`/api/users/${encodeURIComponent(username)}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remove_home: false }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(data.error || `Błąd HTTP ${res.status}`, true);
      return;
    }
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    showToast(`Błąd połączenia: ${err.message}`, true);
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

const PERMISSION_LABELS = { none: "Brak dostępu", ro: "Tylko odczyt", rw: "Odczyt i zapis" };

let editingShareName = null;

function formatPermissionsSummary(permissions) {
  const entries = Object.entries(permissions || {});
  if (!entries.length) return "\u2013 (nikt nie ma dostępu)";
  return entries
    .map(([user, level]) => {
      const u = lastKnownUsersData.find((x) => x.username === user);
      const label = u ? (u.display_name || u.username) : user;
      return `${label} (${level === "rw" ? "RW" : "R"})`;
    })
    .join(", ");
}

function renderShares(sharesList) {
  sharesContainer.innerHTML = "";
  if (!sharesList.length) {
    emptyState(sharesContainer, "Brak udziałów - dodaj pierwszy przyciskiem powyżej.");
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Udział</th><th>Komentarz</th><th>Dostęp</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const sh of sharesList) {
    const row = shareRowTemplate.content.cloneNode(true);
    const shareLabel = sh.name + (sh.managed ? "" : " (spoza tego narzędzia)");
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
    p.textContent = "Brak wykrytych użytkowników - dodaj ich najpierw w sekcji Użytkownicy.";
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
      warn.textContent = "(brak hasła SMB)";
      info.appendChild(warn);
    }
    row.appendChild(info);

    const select = document.createElement("select");
    select.dataset.username = u.username;
    select.className = "permission-select";
    for (const [value, label] of Object.entries(PERMISSION_LABELS)) {
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
    renderShares(data.shares || []);
  } catch (err) {
    emptyState(sharesContainer, `Błąd wczytywania udziałów (${err.message})`);
  }
}

function updateSharePathPreview() {
  const raw = shareNameInput.value.trim().toLowerCase();
  sharePathPreview.textContent = raw ? `ścieżka: /srv/${raw}` : "";
}
shareNameInput.addEventListener("input", updateSharePathPreview);

function openShareDialog(mode, share) {
  shareForm.reset();
  shareError.textContent = "";
  populateSharePermissionsList(mode === "edit" ? share.permissions : undefined);

  if (mode === "edit") {
    editingShareName = share.name;
    shareDialogTitle.textContent = `Edytuj udział: ${share.name}`;
    shareNameInput.value = share.name;
    shareNameInput.disabled = true; // renaming not supported - delete + recreate instead
    sharePathPreview.textContent = `ścieżka: ${share.path} (nie do zmiany tutaj)`;
    shareCommentInput.value = share.comment || "";
    shareSubmitBtn.textContent = "Zapisz zmiany";
  } else {
    editingShareName = null;
    shareDialogTitle.textContent = "Nowy udział";
    shareNameInput.disabled = false;
    sharePathPreview.textContent = "";
    shareSubmitBtn.textContent = "Utwórz";
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
    confirmMsg = `Zapisać zmiany dla udziału "${editingShareName}"?\n\nDostęp: ${summary}`;
    url = `/api/shares/${encodeURIComponent(editingShareName)}/update`;
    body = { comment, permissions };
  } else {
    const name = shareNameInput.value.trim().toLowerCase();
    confirmMsg = `Utworzyć udział "${name}" pod /srv/${name}?\n\nDostęp: ${summary}`;
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
      shareError.textContent = data.error || `Błąd HTTP ${res.status}`;
      return;
    }
    if (data.share && data.share.warning) {
      showToast(data.share.warning, true);
    }
    shareDialog.close();
    await loadShares();
  } catch (err) {
    shareError.textContent = `Błąd połączenia: ${err.message}`;
  } finally {
    shareSubmitBtn.disabled = false;
  }
});

async function deleteShare(name) {
  const deleteFiles = window.confirm(
    `Usunąć udział "${name}"?\n\n` +
    `OK = usuń tylko z Samby, ZOSTAW pliki na dysku (bezpieczne).\n` +
    `Anuluj = przerwij.\n\n` +
    `(Skasowanie też plików nie jest tu jeszcze dostępne z tego dialogu - ` +
    `celowo, żeby nie skasować czyichś danych jednym kliknięciem.)`
  );
  if (!deleteFiles) return;
  try {
    const res = await fetch(`/api/shares/${encodeURIComponent(name)}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delete_files: false }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(data.error || `Błąd HTTP ${res.status}`, true);
      return;
    }
    await loadShares();
  } catch (err) {
    showToast(`Błąd połączenia: ${err.message}`, true);
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

function renderSshKeys(keysList) {
  sshKeysContainer.innerHTML = "";
  if (!keysList.length) {
    emptyState(sshKeysContainer, "Nie wykryto żadnych kont użytkowników.");
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Użytkownik</th><th>Klucz</th><th>Wysłano na</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const k of keysList) {
    if (k.error) continue; // user lookup failed - skip silently, shouldn't normally happen
    const row = sshKeyRowTemplate.content.cloneNode(true);
    const u = lastKnownUsersData.find((x) => x.username === k.username);
    const keyLabel = (u && (u.display_name || u.username)) || k.username;
    const keyNameEl = row.querySelector(".display-name");
    keyNameEl.textContent = keyLabel;
    keyNameEl.title = keyLabel;

    const pill = row.querySelector(".key-cell .pill");
    pill.textContent = k.has_key ? "jest" : "brak";
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
          ? `Aktualny klucz wysłany na ${dep.remote_user}@${dep.host}`
          : `Nieaktualne - klucz wygenerowano ponownie od czasu wysłania na ${dep.remote_user}@${dep.host}`;
        const label = document.createElement("span");
        label.textContent = dep.display_name;
        pillEl.appendChild(label);
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.textContent = "\u00d7";
        removeBtn.title = "Usuń z tego urządzenia";
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
        generateBtn.title = "To konto ma wyłączone logowanie/SSH - włącz je najpierw w edycji użytkownika";
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
    renderSshKeys(data.keys || []);
  } catch (err) {
    emptyState(sshKeysContainer, `Błąd wczytywania kluczy (${err.message})`);
  }
}

async function generateSshKey(username) {
  if (!window.confirm(`Wygenerować nową parę kluczy SSH dla "${username}"?`)) return;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(username)}/generate`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(data.error || `Błąd HTTP ${res.status}`, true);
      return;
    }
    await loadSshKeys();
  } catch (err) {
    showToast(`Błąd połączenia: ${err.message}`, true);
  }
}

async function deleteSshKey(username) {
  if (!window.confirm(`Usunąć klucz SSH dla "${username}"? Będzie trzeba go ponownie wysłać wszędzie, gdzie był zainstalowany.`)) return;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(username)}/delete`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(data.error || `Błąd HTTP ${res.status}`, true);
      return;
    }
    await loadSshKeys();
  } catch (err) {
    showToast(`Błąd połączenia: ${err.message}`, true);
  }
}

function openDeployDialog(username) {
  deployKeyForm.reset();
  deployKeyError.textContent = "";
  deployingKeyForUsername = username;
  deployKeyTitle.textContent = `Wyślij klucz "${username}" na zdalne urządzenie`;
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

  if (!window.confirm(`Zainstalować klucz na ${remoteUser}@${remoteHost}?`)) return;

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
      deployKeyError.textContent = data.error || `Błąd HTTP ${res.status}`;
      return;
    }
    deployKeyDialog.close();
    showToast("Klucz zainstalowany poprawnie.");
  } catch (err) {
    deployKeyError.textContent = `Błąd połączenia: ${err.message}`;
  } finally {
    deployKeySubmitBtn.disabled = false;
  }
});

function openRemoveDeploymentDialog(username, deployment) {
  removeDeploymentForm.reset();
  removeDeploymentError.textContent = "";
  removingDeployment = { username, host: deployment.host, remote_user: deployment.remote_user };
  removeDeploymentTitle.textContent = `Usuń klucz z ${deployment.remote_user}@${deployment.host}`;
  removeDeploymentDialog.showModal();
}

removeDeploymentCancel.addEventListener("click", () => removeDeploymentDialog.close());

removeDeploymentForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  removeDeploymentError.textContent = "";

  const password = removeDeploymentPasswordInput.value;
  const { username, host, remote_user } = removingDeployment;

  if (!window.confirm(`Na pewno usunąć klucz z ${remote_user}@${host}? Ten host straci dostęp bez hasła.`)) return;

  removeDeploymentSubmitBtn.disabled = true;
  try {
    const res = await fetch(`/api/ssh-keys/${encodeURIComponent(username)}/deployments/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote_host: host, remote_user, remote_password: password }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      removeDeploymentError.textContent = data.error || `Błąd HTTP ${res.status}`;
      return;
    }
    removeDeploymentDialog.close();
    await loadSshKeys();
  } catch (err) {
    removeDeploymentError.textContent = `Błąd połączenia: ${err.message}`;
  } finally {
    removeDeploymentSubmitBtn.disabled = false;
  }
});

loadShares();
setInterval(loadShares, REFRESH_MS);

// Kick off polling now that everything above is declared.
loadUsers();
setInterval(loadUsers, REFRESH_MS);
loadSshKeys();
setInterval(loadSshKeys, REFRESH_MS);
refresh();
setInterval(refresh, REFRESH_MS);
