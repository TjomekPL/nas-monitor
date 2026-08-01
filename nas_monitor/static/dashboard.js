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
