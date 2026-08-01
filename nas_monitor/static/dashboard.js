const REFRESH_MS = 20000;

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

async function loadUsers() {
  if (addUserDialog.open) return; // don't rebuild the table under an open form
  try {
    const res = await fetch("/api/users");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderUsers(data.users || []);
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
      window.alert(data.error || `Błąd HTTP ${res.status}`);
      return;
    }
    await loadUsers();
  } catch (err) {
    window.alert(`Błąd połączenia: ${err.message}`);
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
      window.alert(data.error || `Błąd HTTP ${res.status}`);
      return;
    }
    await loadUsers();
  } catch (err) {
    window.alert(`Błąd połączenia: ${err.message}`);
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
const shareNameLabel = document.getElementById("share-name-label");
const shareNameInput = document.getElementById("share-name");
const sharePathPreview = document.getElementById("share-path-preview");
const shareCommentInput = document.getElementById("share-comment");
const shareGroupSelect = document.getElementById("share-group");
const shareReadOnlyInput = document.getElementById("share-read-only");
const shareSubmitBtn = document.getElementById("share-submit");

let editingShareName = null;

function renderShares(sharesList) {
  sharesContainer.innerHTML = "";
  if (!sharesList.length) {
    emptyState(sharesContainer, "Brak udziałów - dodaj pierwszy przyciskiem powyżej.");
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Udział</th><th>Komentarz</th><th>Tryb</th><th>Grupa</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const sh of sharesList) {
    const row = shareRowTemplate.content.cloneNode(true);
    row.querySelector(".display-name").textContent = sh.name + (sh.managed ? "" : " (spoza tego narzędzia)");
    row.querySelector(".path").textContent = sh.path;
    row.querySelector(".comment").textContent = sh.comment || "\u2013";

    const pill = row.querySelector(".mode-cell .pill");
    pill.textContent = sh.read_only ? "odczyt" : "odczyt/zapis";
    pill.classList.add(sh.read_only ? "pill-neutral" : "pill-ok");

    row.querySelector(".group").textContent = sh.groups && sh.groups.length ? sh.groups.join(", ") : "\u2013";

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

function populateShareGroupOptions(selectedGroup) {
  shareGroupSelect.innerHTML = "";
  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "(brak - bez ograniczeń dostępu)";
  shareGroupSelect.appendChild(noneOpt);
  for (const g of lastKnownGroupsData) {
    const opt = document.createElement("option");
    opt.value = g.name;
    opt.textContent = g.name;
    if (g.name === selectedGroup) opt.selected = true;
    shareGroupSelect.appendChild(opt);
  }
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
  populateShareGroupOptions(mode === "edit" ? (share.groups || [])[0] : undefined);

  if (mode === "edit") {
    editingShareName = share.name;
    shareDialogTitle.textContent = `Edytuj udział: ${share.name}`;
    shareNameInput.value = share.name;
    shareNameInput.disabled = true; // renaming not supported - delete + recreate instead
    sharePathPreview.textContent = `ścieżka: ${share.path} (nie do zmiany tutaj)`;
    shareCommentInput.value = share.comment || "";
    shareReadOnlyInput.checked = !!share.read_only;
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
  const group = shareGroupSelect.value || null;
  const readOnly = shareReadOnlyInput.checked;

  let url, body, confirmMsg;
  if (editingShareName) {
    confirmMsg = `Zapisać zmiany dla udziału "${editingShareName}"?`;
    url = `/api/shares/${encodeURIComponent(editingShareName)}/update`;
    body = { comment, group, read_only: readOnly };
  } else {
    const name = shareNameInput.value.trim().toLowerCase();
    confirmMsg = `Utworzyć udział "${name}" pod /srv/${name}?`;
    url = "/api/shares/create";
    body = { name, comment, group, read_only: readOnly };
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
      window.alert(data.share.warning);
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
      window.alert(data.error || `Błąd HTTP ${res.status}`);
      return;
    }
    await loadShares();
  } catch (err) {
    window.alert(`Błąd połączenia: ${err.message}`);
  }
}

loadShares();
setInterval(loadShares, REFRESH_MS);

// Kick off polling now that everything above is declared.
loadUsers();
setInterval(loadUsers, REFRESH_MS);
refresh();
setInterval(refresh, REFRESH_MS);
