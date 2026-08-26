"use strict";

// Minimal single-page UI. No framework, no build step: the Pi only ever serves
// three static files, and the live video never passes through Python.

const state = {
  session: null,
  offset: 0,
  pageSize: 24,
  personOnly: false,
  pc: null,
  currentEvent: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: "same-origin", headers: {} }, options);
  opts.headers = Object.assign({ "X-Requested-With": "SecureCam" }, opts.headers);
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  if (response.status === 401 && state.session) {
    showLogin("Your session expired. Please sign in again.");
    throw new Error("session expired");
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const detail = payload.hint ? `${payload.error} - ${payload.hint}` : payload.error || response.statusText;
    throw new Error(detail);
  }
  return payload;
}

// --- authentication --------------------------------------------------------

function showLogin(message) {
  state.session = null;
  stopLive();
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  const error = $("login-error");
  error.textContent = message || "";
  error.classList.toggle("hidden", !message);
}

async function showApp(session) {
  state.session = session;
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("who").textContent = `${session.username} (${session.role})`;
  const isAdmin = session.permissions.includes("manage_users");
  document.querySelectorAll(".admin-only").forEach((el) => el.classList.toggle("hidden", !isAdmin));
  await loadEvents(true);
  switchView("live");
}

async function bootstrap() {
  try {
    const info = await api("/api/info");
    $("device-name").textContent = info.name;
    $("login-device").textContent = `${info.name} - ${info.device_id}`;
    document.title = `SecureCam - ${info.name}`;
  } catch (err) {
    $("login-device").textContent = "Cannot reach the camera API.";
  }
  try {
    await showApp(await api("/api/session"));
  } catch (err) {
    showLogin("");
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/login", {
      method: "POST",
      body: { username: $("username").value, password: $("password").value },
    });
    $("password").value = "";
    await showApp(await api("/api/session"));
  } catch (err) {
    showLogin(err.message);
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
  } catch (err) {
    /* signing out locally is enough */
  }
  showLogin("");
});

// --- navigation ------------------------------------------------------------

function switchView(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.add("hidden"));
  $(`view-${name}`).classList.remove("hidden");
  if (name === "status") loadStatus();
  if (name === "users") loadUsers();
  if (name !== "live") stopLive();
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
});

// --- live view (WHEP) ------------------------------------------------------

async function startLive() {
  stopLive();
  const status = $("live-status");
  status.textContent = "Connecting...";
  try {
    const ticket = await api("/api/stream/ticket", { method: "POST" });
    const pc = new RTCPeerConnection({ iceServers: [] });
    state.pc = pc;
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (event) => {
      $("live-video").srcObject = event.streams[0];
    };
    pc.onconnectionstatechange = () => {
      status.textContent = `Connection: ${pc.connectionState}`;
      if (pc.connectionState === "failed") {
        status.textContent =
          "Connection failed. The browser could not reach the camera's WebRTC port. " +
          "Check that port 8889 (TCP and UDP) is reachable, or connect through Tailscale.";
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitForIceGathering(pc);

    const response = await fetch(ticket.whep_url, {
      method: "POST",
      headers: {
        "Content-Type": "application/sdp",
        Authorization: "Basic " + btoa(`${ticket.username}:${ticket.password}`),
      },
      body: pc.localDescription.sdp,
    });
    if (!response.ok) {
      throw new Error(`the camera refused the WebRTC handshake (HTTP ${response.status})`);
    }
    await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
    status.textContent = "Live";
  } catch (err) {
    status.textContent = err.message;
    stopLive();
  }
}

function waitForIceGathering(pc) {
  // MediaMTX accepts a complete offer, so gather everything before posting.
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", done);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, 3000);
  });
}

function stopLive() {
  if (state.pc) {
    state.pc.close();
    state.pc = null;
  }
  const video = $("live-video");
  if (video) video.srcObject = null;
}

$("live-start").addEventListener("click", startLive);
$("live-stop").addEventListener("click", () => {
  stopLive();
  $("live-status").textContent = "Stopped";
});

// --- events ----------------------------------------------------------------

function formatTime(value) {
  if (!value) return "unknown";
  const date = new Date(value);
  return isNaN(date) ? value : date.toLocaleString();
}

function badges(event) {
  const parts = [];
  if (event.person_detected === true) parts.push('<span class="badge person">PERSON</span>');
  else if (event.person_detected === false) parts.push('<span class="badge clear">no person</span>');
  if (event.ai_state === "pending" || event.notification_state === "pending") {
    parts.push('<span class="badge pending">queued</span>');
  }
  if (!event.has_recording) parts.push('<span class="badge novideo">no video</span>');
  return parts.join("");
}

async function loadEvents(reset) {
  if (reset) {
    state.offset = 0;
    $("events-list").innerHTML = "";
  }
  const query = new URLSearchParams({ limit: state.pageSize, offset: state.offset });
  if (state.personOnly) query.set("person", "true");
  const data = await api(`/api/events?${query.toString()}`);
  const list = $("events-list");
  data.events.forEach((event) => {
    const card = document.createElement("button");
    card.className = "event";
    card.innerHTML =
      `<img alt="Snapshot" loading="lazy" src="/api/events/${encodeURIComponent(event.event_id)}/snapshot">` +
      `<div class="meta">${badges(event)}<br>${formatTime(event.started_at)}<br>` +
      `<span class="muted small">${event.duration_seconds ? Math.round(event.duration_seconds) + "s" : event.status}</span></div>`;
    card.addEventListener("click", () => openEvent(event.event_id));
    list.appendChild(card);
  });
  state.offset += data.events.length;
  $("events-more").disabled = data.events.length < state.pageSize;
  if (!list.children.length) {
    list.innerHTML = '<p class="muted">No events recorded yet.</p>';
  }
}

$("events-refresh").addEventListener("click", () => loadEvents(true));
$("events-more").addEventListener("click", () => loadEvents(false));
$("filter-person").addEventListener("change", (e) => {
  state.personOnly = e.target.checked;
  loadEvents(true);
});

async function openEvent(eventId) {
  const event = await api(`/api/events/${encodeURIComponent(eventId)}`);
  state.currentEvent = eventId;
  $("event-title").textContent = `${formatTime(event.started_at)} - ${event.status}`;
  const video = $("event-video");
  if (event.recording.state === "completed") {
    video.classList.remove("hidden");
    video.src = `/api/events/${encodeURIComponent(eventId)}/video`;
  } else {
    video.classList.add("hidden");
    video.removeAttribute("src");
  }
  $("event-meta").textContent = JSON.stringify(event, null, 2);
  $("event-dialog").showModal();
}

$("event-close").addEventListener("click", () => {
  $("event-video").pause();
  $("event-dialog").close();
});

$("event-delete").addEventListener("click", async () => {
  if (!state.currentEvent || !window.confirm("Delete this event and its video permanently?")) return;
  try {
    await api(`/api/events/${encodeURIComponent(state.currentEvent)}`, { method: "DELETE" });
    $("event-dialog").close();
    await loadEvents(true);
  } catch (err) {
    window.alert(err.message);
  }
});

// --- status ----------------------------------------------------------------

async function loadStatus() {
  const container = $("status-checks");
  const warnings = $("status-warnings");
  container.innerHTML = "";
  warnings.innerHTML = "";
  try {
    const health = await api("/api/health");
    (health.checks || []).forEach((check) => {
      const box = document.createElement("div");
      box.className = `check ${check.status}`;
      box.innerHTML =
        `<h3>${check.name} - ${check.status}</h3><div>${check.message || ""}</div>` +
        (check.remedy ? `<div class="muted small">${check.remedy}</div>` : "");
      container.appendChild(box);
    });
    const system = health.system || {};
    const summary = document.createElement("div");
    summary.className = "check unknown";
    summary.innerHTML =
      `<h3>system</h3><div>${system.model || ""}</div>` +
      `<div class="muted small">uptime ${health.uptime || "?"}, CPU ${system.cpu_temp_celsius ?? "?"} C, ` +
      `load ${system.cpu_load_1m ?? "?"}</div>`;
    container.appendChild(summary);
  } catch (err) {
    warnings.innerHTML = `<div class="warning">${err.message}</div>`;
  }

  if (state.session && state.session.permissions.includes("manage_device")) {
    try {
      const report = await api("/api/diagnostics");
      (report.warnings || []).forEach((text) => {
        const box = document.createElement("div");
        box.className = "warning";
        box.textContent = text;
        warnings.appendChild(box);
      });
    } catch (err) {
      /* diagnostics are optional for the status view */
    }
  }
}

// --- users -----------------------------------------------------------------

async function loadUsers() {
  const container = $("users-list");
  try {
    const data = await api("/api/users");
    const rows = data.users
      .map(
        (user) =>
          `<tr><td>${user.username}</td><td>${user.role || "-"}</td>` +
          `<td>${user.enabled ? "enabled" : "disabled"}</td>` +
          `<td>${user.last_login_at ? formatTime(user.last_login_at) : "never"}</td>` +
          `<td><button class="danger" data-user="${user.username}">Delete</button></td></tr>`
      )
      .join("");
    container.innerHTML =
      "<table><tr><th>User</th><th>Role on this camera</th><th>State</th><th>Last login</th><th></th></tr>" +
      rows +
      "</table>";
    container.querySelectorAll("button[data-user]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm(`Delete user ${button.dataset.user}?`)) return;
        try {
          await api(`/api/users/${encodeURIComponent(button.dataset.user)}`, { method: "DELETE" });
          loadUsers();
        } catch (err) {
          window.alert(err.message);
        }
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="warning">${err.message}</div>`;
  }
}

$("user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("user-error");
  error.classList.add("hidden");
  try {
    await api("/api/users", {
      method: "POST",
      body: {
        username: $("new-username").value,
        password: $("new-password").value,
        role: $("new-role").value,
      },
    });
    $("new-username").value = "";
    $("new-password").value = "";
    loadUsers();
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove("hidden");
  }
});

bootstrap();
