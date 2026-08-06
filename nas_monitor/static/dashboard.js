const REFRESH_MS = 20000;
const STATUSBAR_REFRESH_MS = 2000;
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
// In-app confirm dialog - replaces window.confirm() everywhere. Native
// confirm() is a browser-chrome prompt, not part of the page's own DOM;
// firing one while another <dialog> is still open (a routine occurrence
// here - e.g. confirming a delete from within an edit form) is exactly
// the kind of situation that made a password manager extension flag
// this page as possibly interfering with it. A <dialog> stacked on a
// <dialog> is a normal, well-supported pattern and doesn't involve any
// browser-native prompt at all.
// --------------------------------------------------------------------

const confirmDialogEl = document.getElementById("confirm-dialog");
const confirmForm = document.getElementById("confirm-form");
const confirmMessageEl = document.getElementById("confirm-message");
const confirmCancelBtn = document.getElementById("confirm-cancel");
const confirmOkBtn = document.getElementById("confirm-ok");

let confirmResolve = null;

function settleConfirm(value) {
  if (confirmResolve) {
    const resolve = confirmResolve;
    confirmResolve = null;
    resolve(value);
  }
}

// danger=true gives the OK button the same red treatment as other
// destructive actions in the app, so a delete confirm looks like one.
function confirmDialog(message, { danger = false } = {}) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    confirmMessageEl.textContent = message;
    confirmOkBtn.classList.toggle("danger", danger);
    confirmDialogEl.showModal();
  });
}

confirmForm.addEventListener("submit", () => {
  settleConfirm(true);
  confirmDialogEl.close();
});
confirmCancelBtn.addEventListener("click", () => {
  confirmDialogEl.close();
  settleConfirm(false);
});
// Covers Esc and any other way the dialog closes without going through
// the two explicit buttons above.
confirmDialogEl.addEventListener("close", () => settleConfirm(false));

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
// Title glow easter egg - lightsaber-style, ignites at random moments
// while the tab is open (not on every load - the point is that it's a
// surprise, not a fixed decoration). Dark theme always glows red;
// light theme picks green/blue/purple at random each time.
// --------------------------------------------------------------------

const appTitle = document.getElementById("app-title");
const TITLE_GLOWS = {
  red: { core: "#fff", mid: "#ff3b3b", out: "#ff0000" },
  green: { core: "#eafff0", mid: "#3ddc6a", out: "#1fb851" },
  blue: { core: "#eaf4ff", mid: "#4fa3ff", out: "#2b7fe0" },
  purple: { core: "#fdf2ff", mid: "#c13bf5", out: "#8a0fd1" },
};

function glowShadow(c) {
  return `0 0 5px ${c.core}, 0 0 14px ${c.mid}, 0 0 30px ${c.mid}, 0 0 55px ${c.out}, 0 0 95px ${c.out}`;
}

function randomBetween(minMs, maxMs) {
  return minMs + Math.random() * (maxMs - minMs);
}

function igniteTitle() {
  const isDark = currentTheme() === "dark";
  const key = isDark ? "red" : ["green", "blue", "purple"][Math.floor(Math.random() * 3)];
  const c = TITLE_GLOWS[key];

  appTitle.style.transition = "text-shadow 0.2s ease-out, color 0.2s ease-out";
  appTitle.style.color = c.out;
  appTitle.style.textShadow = glowShadow(c);

  setTimeout(() => {
    appTitle.style.transition = "text-shadow 1.8s ease-in, color 1.8s ease-in";
    appTitle.style.textShadow = "";
    appTitle.style.color = "";
    // Clearing the inline color lets CSS (which tracks the current
    // theme) take back over. Freezing a captured getComputedStyle()
    // value here instead was the actual bug - it looked fine until the
    // theme was switched afterward, at which point the title stayed
    // stuck at whichever color the OLD theme had, occasionally landing
    // on dark-on-dark or light-on-light and going nearly invisible.
  }, 2500);
}

function scheduleNextGlow() {
  setTimeout(() => {
    igniteTitle();
    scheduleNextGlow();
  }, randomBetween(4 * 60 * 1000, 15 * 60 * 1000));
}

// First ignition is sooner (so it's not a 15-minute wait to ever see it)
// but still never on the very first paint - that would just be a static
// decoration, not a surprise.
setTimeout(igniteTitle, randomBetween(45 * 1000, 4 * 60 * 1000));
scheduleNextGlow();

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
// Auth: redirect to the login page if a session expires mid-use (any
// API call coming back 401), and the account settings dialog (change
// password, session duration, logout).
// --------------------------------------------------------------------

const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await nativeFetch(...args);
  if (res.status === 401 && !String(args[0]).startsWith("/login")) {
    window.location.href = "/login";
  }
  return res;
};

const accountToggleBtn = document.getElementById("account-toggle");
const accountDialog = document.getElementById("account-dialog");
const accountLoggedInAs = document.getElementById("account-logged-in-as");
const accountDialogClose = document.getElementById("account-dialog-close");
const changePasswordForm = document.getElementById("change-password-form");
const changePasswordError = document.getElementById("change-password-error");
const sessionDurationForm = document.getElementById("session-duration-form");
const sessionDurationSelect = document.getElementById("session-duration-select");
const sessionDurationCustomLabel = document.getElementById("session-duration-custom-label");
const sessionDurationCustomInput = document.getElementById("session-duration-custom");
const sessionDurationError = document.getElementById("session-duration-error");
const logoutBtn = document.getElementById("logout-btn");

async function openAccountDialog() {
  changePasswordForm.reset();
  changePasswordError.textContent = "";
  sessionDurationError.textContent = "";
  try {
    const res = await nativeFetch("/api/auth/status");
    const data = await res.json();
    accountLoggedInAs.textContent = data.username ? t("ui.accountDialog.loggedInAs", { username: data.username }) : "";
    const minutes = data.session_duration_minutes;
    const presetValues = ["5", "15", "30", "60", "720", "1440", "10080"];
    if (minutes === null || minutes === undefined) {
      sessionDurationSelect.value = "";
    } else if (presetValues.includes(String(minutes))) {
      sessionDurationSelect.value = String(minutes);
    } else {
      sessionDurationSelect.value = "custom";
      sessionDurationCustomInput.value = Math.round(minutes / 60);
    }
    sessionDurationCustomLabel.style.display = sessionDurationSelect.value === "custom" ? "block" : "none";
  } catch (err) {
    // status fetch failing shouldn't block opening the dialog - the
    // forms below will just surface their own errors on submit
  }
  accountDialog.showModal();
  checkForUpdate();
}

accountToggleBtn.addEventListener("click", openAccountDialog);
accountDialogClose.addEventListener("click", () => accountDialog.close());

sessionDurationSelect.addEventListener("change", () => {
  sessionDurationCustomLabel.style.display = sessionDurationSelect.value === "custom" ? "block" : "none";
});

changePasswordForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  changePasswordError.textContent = "";

  const currentPassword = document.getElementById("current-password").value;
  const newPassword = document.getElementById("new-password").value;
  const confirmPassword = document.getElementById("new-password-confirm").value;

  if (newPassword !== confirmPassword) {
    changePasswordError.textContent = t("ui.accountDialog.passwordMismatch");
    return;
  }

  try {
    const res = await fetch("/api/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      changePasswordError.textContent = apiErrorMessage(data, res);
      return;
    }
    changePasswordForm.reset();
    showToast(t("ui.accountDialog.passwordChanged"));
  } catch (err) {
    changePasswordError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  }
});

sessionDurationForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  sessionDurationError.textContent = "";

  let minutes = null;
  if (sessionDurationSelect.value === "custom") {
    const customHours = parseInt(sessionDurationCustomInput.value, 10);
    if (!customHours || customHours <= 0) {
      sessionDurationError.textContent = window.i18n.errorText("auth.invalid_session_duration");
      return;
    }
    minutes = customHours * 60;
  } else if (sessionDurationSelect.value !== "") {
    minutes = parseInt(sessionDurationSelect.value, 10);
  }

  try {
    const res = await fetch("/api/auth/session-duration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      sessionDurationError.textContent = apiErrorMessage(data, res);
      return;
    }
    showToast(t("ui.accountDialog.sessionDurationSaved"));
  } catch (err) {
    sessionDurationError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  }
});

logoutBtn.addEventListener("click", async () => {
  try {
    await fetch("/logout", { method: "POST" });
  } catch (err) {
    // fall through to redirect regardless - worst case the session
    // just expires naturally server-side
  }
  window.location.href = "/login";
});

// --------------------------------------------------------------------
// Tabs
// --------------------------------------------------------------------

const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanels = document.querySelectorAll(".tab-panel");

const tabRefreshFns = {
  disks: () => refresh(),
  users: () => loadUsers(),
  groups: () => loadGroups(),
  certs: () => loadSshKeys(),
  shares: () => loadShares(),
  network: () => loadNetwork(),
  log: () => loadLog(),
};

function activateTab(name, { refresh: shouldRefresh = false } = {}) {
  let matched = false;
  tabButtons.forEach((btn) => {
    const isMatch = btn.dataset.tab === name;
    btn.classList.toggle("active", isMatch);
    if (isMatch) matched = true;
  });
  tabPanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.tab === name));
  if (matched) {
    localStorage.setItem("nas-monitor-tab", name);
    // Every tab otherwise only refreshes on its own independent timer -
    // without this, data changed from a DIFFERENT tab (e.g. adding
    // someone to a group while editing a user) wouldn't show up here
    // until that timer's next cycle, up to REFRESH_MS later. Only wired
    // to actual clicks, not the initial page-load restore below - each
    // tab's own load*() call further down the file already handles its
    // first load, and calling it from here instead would run before
    // that load function's own dialog-guard consts are even declared.
    if (shouldRefresh) {
      const refreshFn = tabRefreshFns[name];
      if (refreshFn) refreshFn();
    }
  }
}

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab, { refresh: true }));
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

// IEC binary units (KiB/MiB/GiB/TiB - factor of 1024), matching
// monitor.py's _human_size() on the backend - both compute the exact
// same way, this just needs its own implementation since it runs in
// the browser, not Python.
function formatBytesIec(bytes) {
  if (bytes === null || bytes === undefined) return "\u2013";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let size = bytes;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i++;
  }
  return i === 0 ? `${Math.round(size)} ${units[i]}` : `${size.toFixed(1)} ${units[i]}`;
}

// One shared usage-bar renderer for both the disk cards and the RAID
// array cards - same "usage" shape from the backend either way (see
// monitor.get_filesystem_usage()), so this never needs to know which
// kind of card it's being used in.
function renderUsageBar(container, usage) {
  if (!usage || !usage.mounted || !usage.total_bytes) {
    container.innerHTML = "";
    return;
  }
  const pct = Math.round((usage.used_bytes / usage.total_bytes) * 100);
  const level = pct >= 90 ? "critical" : pct >= 80 ? "warning" : "ok";
  container.innerHTML = `
    <div class="usage-bar-track"><div class="usage-bar-fill ${level}" style="width:${Math.min(pct, 100)}%"></div></div>
    <div class="usage-bar-label">${t("ui.usageBar.label", { used: formatBytesIec(usage.used_bytes), total: formatBytesIec(usage.total_bytes), percent: pct })}</div>
  `;
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
    renderUsageBar(node.querySelector(".usage-bar"), arr.usage);

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
    renderUsageBar(node.querySelector(".usage-bar"), disk.usage);

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
// Raw disks - unmounted, not part of any RAID array. Format/wipe live
// here; disks with a real mounted filesystem show as cards above
// instead (see renderDisks) once they're actually in use.
// --------------------------------------------------------------------

const rawDisksContainer = document.getElementById("raw-disks-container");
const diskActionDialog = document.getElementById("disk-action-dialog");
const diskActionForm = document.getElementById("disk-action-form");
const diskActionTitle = document.getElementById("disk-action-title");
const diskActionDevice = document.getElementById("disk-action-device");
const diskActionFsRow = document.getElementById("disk-action-fs-row");
const diskActionFsSelect = document.getElementById("disk-action-fs");
const diskActionWarning = document.getElementById("disk-action-warning");
const diskActionConfirmLabel = document.getElementById("disk-action-confirm-label");
const diskActionConfirmInput = document.getElementById("disk-action-confirm-input");
const diskActionError = document.getElementById("disk-action-error");
const diskActionCancel = document.getElementById("disk-action-cancel");
const diskActionSubmitBtn = document.getElementById("disk-action-submit");
let diskActionState = null; // { device, name, kind: "format" | "wipe" }

function renderRawDisks(disks) {
  rawDisksContainer.innerHTML = "";
  if (!disks.length) {
    emptyState(rawDisksContainer, t("msg.empty.rawDisks"));
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>${t("ui.rawDisks.colName")}</th><th>${t("ui.rawDisks.colSize")}</th><th>${t("ui.rawDisks.colModel")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const d of disks) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td class="mono">${d.name}</td>
      <td>${d.size}</td>
      <td>${d.model}${d.transport === "usb" ? ` <span class="pill pill-warn">USB</span>` : ""}</td>
    `;
    const actions = document.createElement("td");
    actions.className = "row-actions";

    const checkBtn = document.createElement("button");
    checkBtn.type = "button";
    checkBtn.className = "link-btn";
    checkBtn.textContent = t("ui.rawDisks.checkBtn");
    checkBtn.addEventListener("click", () => checkRawDiskStatus(d, checkBtn));
    actions.appendChild(checkBtn);

    const formatBtn = document.createElement("button");
    formatBtn.type = "button";
    formatBtn.className = "link-btn";
    formatBtn.textContent = t("ui.rawDisks.formatBtn");
    formatBtn.addEventListener("click", () => openDiskActionDialog(d, "format"));
    actions.appendChild(formatBtn);

    const wipeBtn = document.createElement("button");
    wipeBtn.type = "button";
    wipeBtn.className = "link-btn danger";
    wipeBtn.textContent = t("ui.rawDisks.wipeBtn");
    wipeBtn.addEventListener("click", () => openDiskActionDialog(d, "wipe"));
    actions.appendChild(wipeBtn);

    row.appendChild(actions);
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  rawDisksContainer.appendChild(table);
}

async function loadRawDisks() {
  if (diskActionDialog.open) return;
  try {
    const res = await fetch("/api/disks/raw");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderRawDisks(data.disks || []);
  } catch (err) {
    emptyState(rawDisksContainer, t("msg.connectionErrorDetail", { detail: err.message }));
  }
}

async function checkRawDiskStatus(disk, button) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = t("ui.rawDisks.checking");
  try {
    const res = await fetch(`/api/disks/${encodeURIComponent(disk.name)}/smart`);
    const data = await res.json();
    if (!data.available) {
      showToast(t("ui.rawDisks.smartUnavailable", { name: disk.name }), true);
      return;
    }
    const tempPart = data.temperature_c != null ? t("ui.rawDisks.smartTemp", { temp: data.temperature_c }) : "";
    showToast(t("ui.rawDisks.smartResult", { name: disk.name, health: data.health, temp: tempPart }));
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function openDiskActionDialog(disk, kind) {
  diskActionState = { device: disk.path, name: disk.name, kind };
  diskActionError.textContent = "";
  diskActionConfirmInput.value = "";
  diskActionDevice.textContent = `${disk.name} (${disk.size}, ${disk.model})`;

  if (kind === "format") {
    diskActionTitle.textContent = t("ui.diskActionDialog.formatTitle");
    diskActionFsRow.style.display = "block";
    diskActionWarning.textContent = t("ui.diskActionDialog.formatWarning");
    diskActionSubmitBtn.textContent = t("ui.diskActionDialog.formatBtn");
  } else {
    diskActionTitle.textContent = t("ui.diskActionDialog.wipeTitle");
    diskActionFsRow.style.display = "none";
    diskActionWarning.textContent = t("ui.diskActionDialog.wipeWarning");
    diskActionSubmitBtn.textContent = t("ui.diskActionDialog.wipeBtn");
  }
  if (disk.transport === "usb") {
    diskActionWarning.textContent += " " + t("ui.diskActionDialog.usbWarning");
  }
  diskActionConfirmLabel.textContent = t("ui.diskActionDialog.confirmLabel", { name: disk.name });
  diskActionSubmitBtn.disabled = true;
  diskActionDialog.showModal();
}

diskActionConfirmInput.addEventListener("input", () => {
  diskActionSubmitBtn.disabled = !diskActionState || diskActionConfirmInput.value.trim() !== diskActionState.name;
});

diskActionCancel.addEventListener("click", () => diskActionDialog.close());

diskActionForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!diskActionState || diskActionConfirmInput.value.trim() !== diskActionState.name) return;
  diskActionError.textContent = "";
  diskActionSubmitBtn.disabled = true;
  const { name, kind } = diskActionState;
  try {
    const url = kind === "format" ? `/api/disks/${encodeURIComponent(name)}/format` : `/api/disks/${encodeURIComponent(name)}/wipe`;
    const body = kind === "format" ? { filesystem: diskActionFsSelect.value } : undefined;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      diskActionError.textContent = apiErrorMessage(data, res);
      diskActionSubmitBtn.disabled = false;
      return;
    }
    diskActionDialog.close();
    showToast(kind === "format" ? t("ui.diskActionDialog.formatSuccess", { name }) : t("ui.diskActionDialog.wipeSuccess", { name }));
    await loadRawDisks();
    await refresh();
  } catch (err) {
    diskActionError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
    diskActionSubmitBtn.disabled = false;
  }
});

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
  table.innerHTML = `<thead><tr><th>${t("ui.users.colUser")}</th><th>${t("ui.users.colSmb")}</th><th>${t("ui.users.colGroups")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const u of usersList) {
    const row = userRowTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(row);
    const displayName = u.display_name || u.username;
    const nameEl = row.querySelector(".display-name");
    nameEl.textContent = displayName;
    // The system account name almost always differs from the display
    // name (Polish characters and capitalization get normalized away),
    // so showing it as a permanent second line was really "always show
    // it" in practice - useful when troubleshooting (SSH, file
    // ownership, smbpasswd), not useful to see on every page load.
    nameEl.title = t("msg.accountLabel", { username: u.username });

    const smbPill = row.querySelector(".smb-cell .pill");
    if (!u.has_smb) {
      smbPill.textContent = t("msg.no");
      smbPill.classList.add("pill-neutral");
    } else if (u.smb_disabled) {
      smbPill.textContent = t("ui.users.smbDisabledPill");
      smbPill.classList.add("pill-warn");
    } else {
      smbPill.textContent = t("msg.yes");
      smbPill.classList.add("pill-ok");
    }

    row.querySelector(".groups").textContent = u.groups && u.groups.length ? u.groups.join(", ") : "\u2013";

    row.querySelector(".edit-btn").addEventListener("click", () => openUserDialog("edit", u));

    const toggleActiveBtn = row.querySelector(".toggle-active-btn");
    if (!u.has_smb) {
      toggleActiveBtn.remove();
    } else if (u.smb_disabled) {
      toggleActiveBtn.textContent = t("ui.users.enableBtn");
      toggleActiveBtn.addEventListener("click", () => enableUser(u.username, displayName));
    } else {
      toggleActiveBtn.textContent = t("ui.users.disableBtn");
      toggleActiveBtn.addEventListener("click", () => disableUser(u.username, displayName));
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
    // The shares summary lists every known user (see
    // renderPermissionsSummary) - loadShares() and loadUsers() poll on
    // independent timers, so whichever finishes first would otherwise
    // render with a stale/empty user list until its own next 20s cycle.
    // Re-render shares here too, using whatever share data is already
    // available, so it's never more than one of these two calls behind.
    if (lastSharesData.length) renderShares(lastSharesData);
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
  const newGroupName = document.getElementById("new-group-name").value.trim();

  const groups = Array.from(groupsChecklist.querySelectorAll("input[name='group']:checked")).map((cb) => cb.value);
  if (newGroupName) groups.push(newGroupName);

  let confirmMsg, url, body;
  if (editingUsername) {
    confirmMsg = t("msg.confirmSaveUser", { name: editingUsername });
    url = `/api/users/${encodeURIComponent(editingUsername)}/update`;
    body = { display_name: nameField, groups, password };
  } else {
    const resolvedAccount = nameField.toLowerCase();
    const accountNote = resolvedAccount !== nameField ? t("msg.accountNote", { account: resolvedAccount }) : "";
    confirmMsg = t("msg.confirmCreateUser", { name: nameField, note: accountNote });
    url = "/api/users/create";
    body = { username: nameField, password, groups };
  }
  if (!(await confirmDialog(confirmMsg))) return;

  submitBtn.disabled = true;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      let text = apiErrorMessage(data, res);
      if (data.note_code) text += " " + window.i18n.noteText(data.note_code);
      addUserError.textContent = text;
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

async function disableUser(username, displayName) {
  if (!(await confirmDialog(t("msg.confirmDisableUser", { name: displayName })))) return;
  try {
    const res = await fetch(`/api/users/${encodeURIComponent(username)}/disable`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadUsers();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

async function enableUser(username, displayName) {
  try {
    const res = await fetch(`/api/users/${encodeURIComponent(username)}/enable`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadUsers();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

async function deleteUser(username, displayName) {
  const confirmed = await confirmDialog(t("msg.confirmDeleteUser", { name: displayName, username }), { danger: true });
  if (!confirmed) return;
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
    if (data.note_code) showToast(window.i18n.noteText(data.note_code), true);
    await loadUsers();
    await loadSshKeys();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

// --------------------------------------------------------------------
// Groups (general system groups - NOT <share>_access groups, which
// stay auto-managed from the Shares tab and never appear here)
// --------------------------------------------------------------------

const groupsContainer = document.getElementById("groups-container");
const groupRowTemplate = document.getElementById("group-row-template");
const addGroupBtn = document.getElementById("add-group-btn");
const groupDialog = document.getElementById("group-dialog");
const groupForm = document.getElementById("group-form");
const groupCancel = document.getElementById("group-cancel");
const groupError = document.getElementById("group-error");
const groupNameInput = document.getElementById("new-group-dialog-name");
const groupSubmitBtn = document.getElementById("group-submit");

const groupMembersDialog = document.getElementById("group-members-dialog");
const groupMembersForm = document.getElementById("group-members-form");
const groupMembersCancel = document.getElementById("group-members-cancel");
const groupMembersError = document.getElementById("group-members-error");
const groupMembersGroupName = document.getElementById("group-members-group-name");
const groupMembersChecklist = document.getElementById("group-members-checklist");
const groupMembersSubmitBtn = document.getElementById("group-members-submit");
let editingGroupName = null;

function renderGroups(groupsList) {
  groupsContainer.innerHTML = "";
  if (!groupsList.length) {
    emptyState(groupsContainer, t("msg.empty.groups"));
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>${t("ui.groups.colName")}</th><th>${t("ui.groups.colMembers")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const g of groupsList) {
    const row = groupRowTemplate.content.cloneNode(true);
    window.i18n.applyTranslations(row);
    row.querySelector(".display-name").textContent = g.name;
    row.querySelector(".members").textContent = g.members && g.members.length ? g.members.join(", ") : t("ui.groups.noMembers");
    row.querySelector(".edit-members-btn").addEventListener("click", () => openGroupMembersDialog(g));
    row.querySelector(".delete-btn").addEventListener("click", () => deleteGroup(g.name));
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  groupsContainer.appendChild(table);
}

async function loadGroups() {
  if (groupDialog.open || groupMembersDialog.open) return;
  try {
    const res = await fetch("/api/groups");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderGroups(data.groups || []);
  } catch (err) {
    emptyState(groupsContainer, t("msg.loadErrorGroups", { detail: err.message }));
  }
}

addGroupBtn.addEventListener("click", () => {
  groupForm.reset();
  groupError.textContent = "";
  groupDialog.showModal();
});
groupCancel.addEventListener("click", () => groupDialog.close());

groupForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  groupError.textContent = "";
  const name = groupNameInput.value.trim();

  if (!(await confirmDialog(t("msg.confirmCreateGroup", { name })))) return;

  groupSubmitBtn.disabled = true;
  try {
    const res = await fetch("/api/groups/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      groupError.textContent = apiErrorMessage(data, res);
      return;
    }
    groupDialog.close();
    await loadGroups();
    await loadUsers(); // the new group should show up in the user-edit checklist too
  } catch (err) {
    groupError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    groupSubmitBtn.disabled = false;
  }
});

async function deleteGroup(name) {
  if (!(await confirmDialog(t("msg.confirmDeleteGroup", { name }), { danger: true }))) return;
  try {
    const res = await fetch(`/api/groups/${encodeURIComponent(name)}/delete`, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    await loadGroups();
    await loadUsers();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  }
}

function openGroupMembersDialog(group) {
  editingGroupName = group.name;
  groupMembersError.textContent = "";
  groupMembersGroupName.textContent = group.name;
  const currentMembers = new Set(group.members || []);
  groupMembersChecklist.innerHTML = "";
  if (!lastKnownUsersData.length) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = t("ui.groupMembersDialog.noUsersHint");
    groupMembersChecklist.appendChild(p);
  } else {
    for (const u of lastKnownUsersData) {
      const label = document.createElement("label");
      label.className = "inline";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = u.username;
      cb.name = "member";
      cb.checked = currentMembers.has(u.username);
      label.appendChild(cb);
      label.append(` ${u.display_name || u.username}`);
      groupMembersChecklist.appendChild(label);
    }
  }
  groupMembersDialog.showModal();
}

groupMembersCancel.addEventListener("click", () => groupMembersDialog.close());

groupMembersForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  groupMembersError.textContent = "";
  const usernames = Array.from(groupMembersChecklist.querySelectorAll('input[name="member"]:checked')).map((cb) => cb.value);

  groupMembersSubmitBtn.disabled = true;
  try {
    const res = await fetch(`/api/groups/${encodeURIComponent(editingGroupName)}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ usernames }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      groupMembersError.textContent = apiErrorMessage(data, res);
      return;
    }
    groupMembersDialog.close();
    await loadGroups();
    await loadUsers();
  } catch (err) {
    groupMembersError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    groupMembersSubmitBtn.disabled = false;
  }
});

loadGroups();
setInterval(loadGroups, REFRESH_MS);

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
const shareGroupGrantsList = document.getElementById("share-group-grants-list");
const shareSubmitBtn = document.getElementById("share-submit");

function permissionLabels() {
  return { none: t("ui.shares.permNone"), ro: t("ui.shares.permRo"), rw: t("ui.shares.permRw") };
}

let editingShareName = null;
let lastSharesData = [];

function summarizeGrantedAccess(permissions, groupGrants) {
  const groupEntries = Object.entries(groupGrants || {}).map(
    ([group, level]) => `@${group} (${level === "rw" ? t("ui.shares.permSummaryRw") : t("ui.shares.permSummaryRo")})`
  );
  const userEntries = Object.entries(permissions || {}).map(([user, level]) => {
    const u = lastKnownUsersData.find((x) => x.username === user);
    const label = u ? (u.display_name || u.username) : user;
    return `${label} (${level === "rw" ? t("ui.shares.permSummaryRw") : t("ui.shares.permSummaryRo")})`;
  });
  const entries = [...groupEntries, ...userEntries];
  return entries.length ? entries.join(", ") : t("ui.shares.noAccess");
}

function renderPermissionsSummary(container, permissions, groupGrants) {
  container.innerHTML = "";
  const perms = permissions || {};
  const grants = groupGrants || {};

  function levelInfo(level) {
    if (level === "rw") return { text: t("ui.shares.permSummaryRw"), cls: "perm-rw" };
    if (level === "ro") return { text: t("ui.shares.permSummaryRo"), cls: "perm-ro" };
    return { text: t("ui.shares.permSummaryNa"), cls: "perm-na" };
  }

  function appendTag(label, level) {
    const info = levelInfo(level);
    const span = document.createElement("span");
    span.className = `perm-tag ${info.cls}`;
    span.textContent = `${label} (${info.text})`;
    container.appendChild(span);
  }

  for (const [group, level] of Object.entries(grants)) {
    appendTag(`@${group}`, level);
  }
  // Every known user gets a tag, not just the ones with access - RO/RW
  // are colored to stand out, NA (red) makes it just as visible who's
  // explicitly excluded, which used to be invisible (removing someone's
  // access just made them disappear from this list entirely).
  for (const u of lastKnownUsersData) {
    appendTag(u.display_name || u.username, perms[u.username]);
  }

  if (!container.children.length) {
    container.textContent = t("ui.shares.noAccess");
  }
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
    renderPermissionsSummary(row.querySelector(".share-users"), sh.permissions, sh.group_grants);

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

function populateShareGroupGrantsList(existingGrants) {
  const current = existingGrants || {};
  shareGroupGrantsList.innerHTML = "";
  if (!lastKnownGroupsData.length) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = t("ui.shareDialog.noGroupsHint");
    shareGroupGrantsList.appendChild(p);
    return;
  }
  for (const g of lastKnownGroupsData) {
    const row = document.createElement("div");
    row.className = "permission-row";

    const info = document.createElement("div");
    info.className = "permission-info";
    const name = document.createElement("span");
    name.className = "permission-user mono";
    name.textContent = g.name;
    info.appendChild(name);
    row.appendChild(info);

    const select = document.createElement("select");
    select.dataset.group = g.name;
    select.className = "permission-select group-grant-select";
    for (const [value, label] of Object.entries(permissionLabels())) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      select.appendChild(opt);
    }
    select.value = current[g.name] || "none";
    row.appendChild(select);

    shareGroupGrantsList.appendChild(row);
  }
}

function collectShareGroupGrants() {
  const grants = {};
  shareGroupGrantsList.querySelectorAll(".group-grant-select").forEach((select) => {
    if (select.value !== "none") {
      grants[select.dataset.group] = select.value;
    }
  });
  return grants;
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
  populateShareGroupGrantsList(mode === "edit" ? share.group_grants : undefined);

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
  const groupGrants = collectShareGroupGrants();

  let url, body, confirmMsg;
  const summary = summarizeGrantedAccess(permissions, groupGrants);
  if (editingShareName) {
    confirmMsg = t("msg.confirmSaveShare", { name: editingShareName, summary });
    url = `/api/shares/${encodeURIComponent(editingShareName)}/update`;
    body = { comment, permissions, group_grants: groupGrants };
  } else {
    const name = shareNameInput.value.trim().toLowerCase();
    confirmMsg = t("msg.confirmCreateShare", { name, summary });
    url = "/api/shares/create";
    body = { name, comment, permissions, group_grants: groupGrants };
  }
  if (!(await confirmDialog(confirmMsg))) return;

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
  const confirmed = await confirmDialog(t("msg.confirmDeleteShare", { name }), { danger: true });
  if (!confirmed) return;
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
  // The backend always returns exactly one entry here now (the
  // dedicated sync account, auto-created on first load) - no filtering
  // needed, unlike the old per-user model this replaced.
  const visibleKeys = keysList.filter((k) => !k.error_code);
  if (!visibleKeys.length) {
    emptyState(sshKeysContainer, t("msg.empty.sshKeys"));
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>${t("ui.certs.colUser")}</th><th>${t("ui.certs.colKey")}</th><th>${t("ui.certs.colSentTo")}</th><th></th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const k of visibleKeys) {
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
      // The list is already filtered to has_key || can_login, so
      // reaching here (no key) means can_login is guaranteed true.
      generateBtn.addEventListener("click", () => generateSshKey(k.username));
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
  if (!(await confirmDialog(t("msg.confirmGenerateKey", { username })))) return;
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
  if (!(await confirmDialog(t("msg.confirmDeleteKey", { username }), { danger: true }))) return;
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

  if (!(await confirmDialog(t("msg.confirmDeployKey", { user: remoteUser, host: remoteHost })))) return;

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

  if (!(await confirmDialog(t("msg.confirmRemoveDeployment", { user: remote_user, host }), { danger: true }))) return;

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
  return `${kindLabel}, ${t(`net.bus.${type.bus}`)}`;
}

function ifaceStateLabel(iface) {
  if (iface.state !== "up" && iface.effective_up) return t("ui.network.stateAssumedUp");
  return iface.state;
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
    const addrLines = (iface.addresses || []).map((a) => `${a.address}/${a.prefixlen}`).join("<br>");
    const maskLines = (iface.addresses || []).map((a) => a.netmask).join("<br>");
    const dnsLine = (iface.dns_servers || []).join(", ");
    const typeLabel = formatIfaceType(iface.type);
    card.innerHTML = `
      <div class="card-head">
        <span class="badge ${iface.effective_up ? 'ok' : 'unknown'}"></span>
        <span class="name mono">${iface.name}</span>
        <span class="level">${typeLabel ? `(${typeLabel})` : ""} ${ifaceStateLabel(iface)}</span>
      </div>
      <dl class="facts">
        <div><dt>${t("ui.network.ip")}</dt><dd class="mono">${addrLines || "\u2013"}</dd></div>
        <div><dt>${t("ui.network.netmask")}</dt><dd class="mono">${maskLines || "\u2013"}</dd></div>
        <div><dt>${t("ui.network.gateway")}</dt><dd class="mono">${iface.gateway || "\u2013"}</dd></div>
        <div><dt>${t("ui.network.dnsServers")}</dt><dd class="mono">${dnsLine || "\u2013"}</dd></div>
        <div><dt>${t("ui.network.mac")}</dt><dd class="mono">${iface.mac || "\u2013"}</dd></div>
      </dl>
    `;
    ifaceCards.appendChild(card);

    if (data.backend === "networkmanager") {
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "link-btn";
      editBtn.style.marginTop = "10px";
      editBtn.textContent = t("ui.network.editBtn");
      editBtn.addEventListener("click", () => openNetworkEditDialog(iface));
      card.appendChild(editBtn);
    }
  }
  networkContainer.appendChild(ifaceCards);
}

async function loadNetwork() {
  if (networkEditDialog.open) return;
  try {
    const res = await fetch("/api/network");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    lastNetworkData = data;
    renderNetwork(data);
    if (data.pending_change) {
      showPendingBanner(data.pending_change.token, data.pending_change.created_at);
    } else {
      hidePendingBanner();
    }
  } catch (err) {
    emptyState(networkContainer, t("msg.loadErrorNetwork", { detail: err.message }));
  }
}

// loadNetwork() is invoked once all the dialog element refs below exist -
// see the bottom of this section. It references networkEditDialog, which
// isn't declared yet at this point in the file.

// --------------------------------------------------------------------
// Network settings edit dialog + the 30s auto-revert confirm banner.
// The whole point of the banner is that confirmation is a courtesy, not
// the actual safety mechanism - the real revert timer lives server-side
// (see network_mutate.py) and fires regardless of whether this tab, or
// any tab, is even open. The banner is re-derived from the server's
// pending_change on every loadNetwork() poll (not just right after
// applying), so it reappears correctly even after a plain page reload
// mid-window, from this browser or another one.
// --------------------------------------------------------------------

const networkEditDialog = document.getElementById("network-edit-dialog");
const networkEditForm = document.getElementById("network-edit-form");
const networkEditTitle = document.getElementById("network-edit-title");
const networkEditCancel = document.getElementById("network-edit-cancel");
const networkEditError = document.getElementById("network-edit-error");
const networkEditIp = document.getElementById("network-edit-ip");
const networkEditPrefix = document.getElementById("network-edit-prefix");
const networkEditGateway = document.getElementById("network-edit-gateway");
const networkEditDns = document.getElementById("network-edit-dns");
const networkEditSubmit = document.getElementById("network-edit-submit");

const networkPendingBanner = document.getElementById("network-pending-banner");
const networkPendingCountdown = document.getElementById("network-pending-countdown");
const networkPendingConfirmBtn = document.getElementById("network-pending-confirm-btn");

let editingInterface = null;
let editingOldIp = null;
let pendingChangeTimer = null;
let pendingChangeToken = null;

function openNetworkEditDialog(iface) {
  networkEditForm.reset();
  networkEditError.textContent = "";
  editingInterface = iface.name;
  const addr = (iface.addresses || [])[0];
  editingOldIp = addr ? addr.address : null;
  networkEditTitle.textContent = t("ui.networkEditDialog.title", { interface: iface.name });
  networkEditIp.value = addr ? addr.address : "";
  networkEditPrefix.value = addr ? addr.prefixlen : "";
  networkEditGateway.value = iface.gateway || "";
  networkEditDns.value = (iface.dns_servers || []).join(", ");
  networkEditDialog.showModal();
}

networkEditCancel.addEventListener("click", () => networkEditDialog.close());

networkEditForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  networkEditError.textContent = "";

  const ip = networkEditIp.value.trim();
  const prefixlen = parseInt(networkEditPrefix.value, 10);
  const gateway = networkEditGateway.value.trim();
  const dns = networkEditDns.value.split(",").map((s) => s.trim()).filter(Boolean);

  networkEditSubmit.disabled = true;
  try {
    const res = await fetch(`/api/network/${encodeURIComponent(editingInterface)}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, prefixlen, gateway, dns }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      networkEditError.textContent = apiErrorMessage(data, res);
      return;
    }
    networkEditDialog.close();

    // Only follow the browser to the new address if this tab is
    // actually connected via the address that just changed - editing
    // some other interface than the one you're browsing through
    // shouldn't yank you anywhere.
    if (window.location.hostname === editingOldIp) {
      window.location.href = `http://${data.new_host}/`;
      return;
    }
    await loadNetwork();
  } catch (err) {
    networkEditError.textContent = t("msg.connectionErrorDetail", { detail: err.message });
  } finally {
    networkEditSubmit.disabled = false;
  }
});

function hidePendingBanner() {
  clearInterval(pendingChangeTimer);
  pendingChangeTimer = null;
  pendingChangeToken = null;
  networkPendingBanner.style.display = "none";
}

function showPendingBanner(token, createdAtIso) {
  pendingChangeToken = token;
  networkPendingBanner.style.display = "flex";

  function computeRemaining() {
    if (!createdAtIso) return 30;
    const elapsed = (Date.now() - new Date(createdAtIso).getTime()) / 1000;
    return Math.max(0, Math.round(30 - elapsed));
  }

  networkPendingCountdown.textContent = computeRemaining();
  clearInterval(pendingChangeTimer);
  pendingChangeTimer = setInterval(() => {
    const remaining = computeRemaining();
    if (remaining <= 0) {
      hidePendingBanner();
      loadNetwork();
      return;
    }
    networkPendingCountdown.textContent = remaining;
  }, 1000);
}

networkPendingConfirmBtn.addEventListener("click", async () => {
  if (!pendingChangeToken) return;
  networkPendingConfirmBtn.disabled = true;
  try {
    const res = await fetch("/api/network/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: pendingChangeToken }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(apiErrorMessage(data, res), true);
      return;
    }
    hidePendingBanner();
    showToast(t("msg.networkChangeConfirmed"));
    await loadNetwork();
  } catch (err) {
    showToast(t("msg.connectionErrorDetail", { detail: err.message }), true);
  } finally {
    networkPendingConfirmBtn.disabled = false;
  }
});

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
  // (before the log started storing params for interpolation) - shown
  // verbatim, since there's no code to re-translate it from. category/
  // action existed in BOTH schemas, so params is the only reliable
  // marker of "this is a new-style entry" (new entries always have it,
  // even as an empty object; old entries never have the key at all).
  if (ev.params !== undefined) {
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
  if (!(await confirmDialog(t("msg.logClearConfirm"), { danger: true }))) return;
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
  if (lastUpdateCheck) renderUpdateInfo(lastUpdateCheck);
}

// --------------------------------------------------------------------
// Statusbar - CPU/disk/network, polled independently of everything
// else above (much shorter interval) since it lives outside <main> and
// stays visible across every tab.
// --------------------------------------------------------------------

function formatRate(rate) {
  if (!rate) return "\u2013";
  const val = Number.isInteger(rate.value) ? rate.value : rate.value.toFixed(1);
  return `${val} <span class="unit">${rate.unit}</span>`;
}

async function loadStatusbar() {
  const dot = document.getElementById("statusbar-dot");
  try {
    const res = await fetch("/api/system-stats");
    const data = await res.json();
    if (!data.available) {
      // Keep the plain "unavailable" text in all four value boxes - they
      // have fixed reserved widths (see CSS) sized for short numbers, so
      // splicing the longer error detail into one of them would just get
      // clipped. The detail goes in a title tooltip instead.
      const detail = data.error_code ? window.i18n.errorText(data.error_code) : "";
      for (const id of ["stat-cpu-val", "stat-mem-val", "stat-disk-val", "stat-net-val"]) {
        const el = document.getElementById(id);
        el.textContent = t("ui.statusbar.unavailable");
        el.title = detail;
      }
      if (dot) dot.style.background = "var(--crit)";
      return;
    }
    document.getElementById("stat-cpu-val").textContent = `${data.cpu_percent.toFixed(1)}%`;
    document.getElementById("stat-cpu-val").title = "";
    const memEl = document.getElementById("stat-mem-val");
    memEl.textContent = `${data.mem_percent.toFixed(1)}%`;
    memEl.title = `${data.mem_used_gib} / ${data.mem_total_gib} GiB`;
    const diskEl = document.getElementById("stat-disk-val");
    diskEl.title = "";
    diskEl.innerHTML =
      `<span class="up">R ${formatRate(data.disk_read)}</span><span class="sep">/</span><span class="down">W ${formatRate(data.disk_write)}</span>`;
    const netEl = document.getElementById("stat-net-val");
    netEl.title = "";
    netEl.innerHTML =
      `<span class="up">\u2191 ${formatRate(data.net_up)}</span><span class="sep">/</span><span class="down">\u2193 ${formatRate(data.net_down)}</span>`;
    if (dot) dot.style.background = "var(--ok)";
  } catch (err) {
    if (dot) dot.style.background = "var(--crit)";
  }
}

// --------------------------------------------------------------------
// Update check/apply - version shown in the statusbar, checked once on
// load; the account dialog's "Sprawdź aktualizacje" button re-checks
// on demand. Each check does a real `git fetch` against GitHub on the
// backend, so - unlike the rest of the statusbar - this isn't polled
// continuously.
// --------------------------------------------------------------------

const updateCurrentVersionEl = document.getElementById("update-current-version");
const updateStatusEl = document.getElementById("update-status");
const updateErrorEl = document.getElementById("update-error");
const updateCheckBtn = document.getElementById("update-check-btn");
const updateApplyBtn = document.getElementById("update-apply-btn");
const statusbarVersionBtn = document.getElementById("statusbar-version-btn");
const statusbarVersionVal = document.getElementById("statusbar-version-val");
const statusbarUpdateBadge = document.getElementById("statusbar-update-badge");

let lastUpdateCheck = null;

function renderUpdateInfo(data) {
  lastUpdateCheck = data;
  if (!data.git_managed) {
    statusbarVersionVal.textContent = "\u2013";
    statusbarUpdateBadge.style.display = "none";
    updateCurrentVersionEl.textContent = t("ui.accountDialog.notGitManaged");
    updateStatusEl.style.display = "none";
    updateApplyBtn.style.display = "none";
    return;
  }

  statusbarVersionVal.textContent = data.current_version || "\u2013";
  updateCurrentVersionEl.textContent = t("ui.accountDialog.currentVersion", { version: data.current_version || "?" });

  if (data.error_code) {
    statusbarUpdateBadge.style.display = "none";
    updateStatusEl.style.display = "none";
    updateErrorEl.textContent = window.i18n.errorText(data.error_code, data.error_context);
    updateApplyBtn.style.display = "none";
    return;
  }
  updateErrorEl.textContent = "";

  if (data.update_available) {
    statusbarUpdateBadge.style.display = "inline-block";
    updateStatusEl.style.display = "block";
    updateStatusEl.textContent = t("ui.accountDialog.updateAvailableStatus", { version: data.latest_version || "?" });
    updateApplyBtn.style.display = "inline-block";
    updateApplyBtn.textContent = t("ui.accountDialog.applyUpdateBtn", { version: data.latest_version || "?" });
    updateApplyBtn.disabled = false;
  } else {
    statusbarUpdateBadge.style.display = "none";
    updateStatusEl.style.display = "block";
    updateStatusEl.textContent = t("ui.accountDialog.upToDate");
    updateApplyBtn.style.display = "none";
  }
}

async function checkForUpdate() {
  updateStatusEl.style.display = "block";
  updateStatusEl.textContent = t("ui.accountDialog.checkingUpdate");
  updateErrorEl.textContent = "";
  updateApplyBtn.style.display = "none";
  try {
    const res = await fetch("/api/update/check");
    const data = await res.json();
    renderUpdateInfo(data);
  } catch (err) {
    updateStatusEl.style.display = "none";
    updateErrorEl.textContent = t("err._unknown");
  }
}

function waitForRestartThenReload() {
  // install.sh runs in the background after apply_update() returns (see
  // update_manager.py) and restarts the service as its own last step -
  // that can take anywhere from a few seconds to a couple of minutes
  // depending on whether new system packages need downloading, so this
  // polls indefinitely rather than giving up after a fixed number of
  // tries. Reloads every open tab once the service answers again, so
  // none of them keep running stale JS against an already-updated
  // backend.
  const poll = () => {
    fetch("/api/auth/status")
      .then(() => window.location.reload())
      .catch(() => setTimeout(poll, 1500));
  };
  setTimeout(poll, 2500);
}

async function applyUpdate() {
  updateApplyBtn.disabled = true;
  updateCheckBtn.disabled = true;
  updateErrorEl.textContent = "";
  updateStatusEl.style.display = "block";
  updateStatusEl.textContent = t("ui.accountDialog.applyingUpdate");
  try {
    const res = await fetch("/api/update/apply", { method: "POST" });
    const data = await res.json();
    if (!data.success) {
      updateErrorEl.textContent = window.i18n.errorText(data.error_code, data.error_context);
      updateApplyBtn.disabled = false;
      updateCheckBtn.disabled = false;
      return;
    }
    updateStatusEl.textContent = t("ui.accountDialog.applySuccess", { version: data.version || "?" });
    waitForRestartThenReload();
  } catch (err) {
    updateErrorEl.textContent = t("err._unknown");
    updateApplyBtn.disabled = false;
    updateCheckBtn.disabled = false;
  }
}

updateCheckBtn.addEventListener("click", checkForUpdate);
updateApplyBtn.addEventListener("click", applyUpdate);
statusbarVersionBtn.addEventListener("click", openAccountDialog);

// Kick off polling now that everything above is declared.
loadUsers();
setInterval(loadUsers, REFRESH_MS);
loadSshKeys();
setInterval(loadSshKeys, REFRESH_MS);
refresh();
setInterval(refresh, REFRESH_MS);
loadRawDisks();
setInterval(loadRawDisks, REFRESH_MS);
loadStatusbar();
setInterval(loadStatusbar, STATUSBAR_REFRESH_MS);
checkForUpdate();
