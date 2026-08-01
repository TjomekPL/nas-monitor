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

refresh();
setInterval(refresh, REFRESH_MS);

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

let knownGroups = [];

function renderUsers(usersList) {
  usersContainer.innerHTML = "";
  if (!usersList.length) {
    emptyState(usersContainer, "Nie wykryto żadnych kont użytkowników.");
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Użytkownik</th><th>Logowanie/SSH</th><th>Dostęp SMB</th><th>Grupy</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const u of usersList) {
    const row = userRowTemplate.content.cloneNode(true);
    row.querySelector(".username").textContent = u.username;

    const loginPill = row.querySelector(".login-cell .pill");
    loginPill.textContent = u.can_login ? "tak" : "nie";
    loginPill.classList.add(u.can_login ? "pill-warn" : "pill-ok");

    const smbPill = row.querySelector(".smb-cell .pill");
    smbPill.textContent = u.has_smb ? "tak" : "nie";
    smbPill.classList.add(u.has_smb ? "pill-ok" : "pill-neutral");

    row.querySelector(".groups").textContent = u.groups && u.groups.length ? u.groups.join(", ") : "\u2013";
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  usersContainer.appendChild(table);
}

function renderGroupsChecklist(groups) {
  knownGroups = groups;
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
    label.appendChild(cb);
    label.append(` ${g.name}`);
    groupsChecklist.appendChild(label);
  }
}

async function loadUsers() {
  try {
    const res = await fetch("/api/users");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderUsers(data.users || []);
    renderGroupsChecklist(data.groups || []);
  } catch (err) {
    emptyState(usersContainer, `Błąd wczytywania użytkowników (${err.message})`);
  }
}

addUserBtn.addEventListener("click", () => {
  addUserForm.reset();
  addUserError.textContent = "";
  addUserDialog.showModal();
});

addUserCancel.addEventListener("click", () => addUserDialog.close());

addUserForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  addUserError.textContent = "";

  const username = document.getElementById("new-username").value.trim();
  const password = document.getElementById("new-password").value;
  const allowLogin = document.getElementById("new-allow-login").checked;
  const newGroupName = document.getElementById("new-group-name").value.trim();

  const groups = Array.from(groupsChecklist.querySelectorAll("input[name='group']:checked")).map((cb) => cb.value);
  if (newGroupName) groups.push(newGroupName);

  const confirmMsg = allowLogin
    ? `Utworzyć użytkownika "${username}" z możliwością logowania/SSH?`
    : `Utworzyć użytkownika "${username}" bez możliwości logowania (tylko SMB)?`;
  if (!window.confirm(confirmMsg)) return;

  const submitBtn = document.getElementById("add-user-submit");
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/users/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, groups, allow_login: allowLogin }),
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

loadUsers();
setInterval(loadUsers, REFRESH_MS);
