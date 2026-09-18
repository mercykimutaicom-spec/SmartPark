/* app.js — SmartPark KE frontend for the Flask REST API (app.py). */
(function () {
  "use strict";

  let activeZone = "car";
  let pendingSession = null;
  let selectedMethod = "mpesa";
  let knownSlotStatus = {};   // slot_number -> status, used to diff-flash changed cells only
  let lastAvailable = null;    // for the count-up animation
  let activitySessions = [];
  let activityPage = 1;
  const activityPageSize = 10;
  let paymentPollTimer = null;
  let paypalPending = null;
  let currentUser = null;

  // Clock
  function tickClock() {
    document.getElementById("clock").textContent =
      new Date().toLocaleTimeString("en-KE", {
        timeZone: "Africa/Nairobi",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        hour12: true,
      });
  }
  setInterval(tickClock, 1000);
  tickClock();

  const navigationLinks = [...document.querySelectorAll(".topnav__link")];
  const navigationSections = navigationLinks
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  if ("IntersectionObserver" in window) {
    const navigationObserver = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      navigationLinks.forEach((link) => {
        link.classList.toggle("is-active", link.getAttribute("href") === `#${visible.target.id}`);
      });
    }, { rootMargin: "-96px 0px -55% 0px", threshold: [0.1, 0.4, 0.8] });
    navigationSections.forEach((section) => navigationObserver.observe(section));
  }

  // Toasts
  function toast(message, type = "success") {
    const stack = document.getElementById("toast-stack");
    const el = document.createElement("div");
    el.className = `toast toast--${type}`;
    const icon = type === "success"
      ? '<svg width="16" height="16" viewBox="0 0 20 20" fill="none"><path d="M4 10.5l4 4 8-9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
      : '<svg width="16" height="16" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="7.5" stroke="currentColor" stroke-width="1.6"/><path d="M10 6v5M10 13.2v.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
    el.innerHTML = `<span class="toast__icon">${icon}</span><span>${message}</span>`;
    stack.appendChild(el);
    setTimeout(() => {
      el.classList.add("is-leaving");
      el.addEventListener("animationend", () => el.remove(), { once: true });
    }, 4200);
  }

  // Barrier scene: arm lift + car drive-through (one orchestrated moment)
  function playBarrierSequence(caption) {
    const scene = document.getElementById("barrier");
    const arm = document.getElementById("armGroup");
    const captionEl = document.getElementById("barrier-caption");

    clearTimeout(playBarrierSequence._drive);
    clearTimeout(playBarrierSequence._close);
    scene.classList.remove("is-driving");

    arm.setAttribute("transform", "rotate(-64 101.5 84)");
    scene.classList.add("is-open");
    captionEl.textContent = caption;
    captionEl.classList.add("is-open");

    // restart the car's drive-through animation even if one is already mid-flight
    void scene.offsetWidth;
    playBarrierSequence._drive = setTimeout(() => scene.classList.add("is-driving"), 260);

    playBarrierSequence._close = setTimeout(() => {
      arm.setAttribute("transform", "rotate(0 101.5 84)");
      scene.classList.remove("is-open", "is-driving");
      captionEl.textContent = "Barrier closed";
      captionEl.classList.remove("is-open");
    }, 3400);
  }

  // Count-up animation for the hero stat
  function animateCount(el, from, to, duration = 500) {
    if (from === to) { el.textContent = to; return; }
    const start = performance.now();
    function step(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = Math.round(from + (to - from) * eased);
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  // Slot map + hero stats
  async function refreshSlots() {
    try {
      const res = await fetch("/api/slots");
      const data = await res.json();
      renderHero(data.stats);
      renderGrid(data.slots);
    } catch (e) {
      console.error("Failed to load slots", e);
    }
  }

  async function refreshRates() {
    try {
      const res = await fetch("/api/rates");
      const data = await res.json();
      renderRates(data.rates || [], data.vat_rate);
    } catch (error) {
      console.error("Failed to load parking rates", error);
    }
  }

  async function refreshAuth() {
    try {
      const response = await fetch("/api/auth/me");
      const data = await response.json();
      currentUser = data.user;
      document.getElementById("auth-btn").textContent = currentUser ? `Sign out (${currentUser.username})` : "Sign in";
      document.getElementById("profile-btn").hidden = !currentUser;
      if (currentUser) setDashboardAccess(true);
      else showLoginModal();
    } catch (error) {
      currentUser = null;
      document.getElementById("auth-btn").textContent = "Sign in";
      showLoginModal();
    }
  }

  function showLoginModal() {
    setDashboardAccess(false);
    document.getElementById("login-modal").hidden = false;
    document.getElementById("login-close").hidden = true;
    document.querySelector("#login-form input[name=username]").focus();
  }

  function setDashboardAccess(authenticated) {
    document.querySelector("main.shell").hidden = !authenticated;
    document.querySelector(".foot").hidden = !authenticated;
    document.getElementById("profile-btn").hidden = !authenticated;
    if (authenticated) return;
    document.getElementById("stat-available").textContent = "0";
    document.getElementById("stat-breakdown").replaceChildren();
    document.getElementById("slot-grid").replaceChildren();
    document.getElementById("rates-list").replaceChildren();
    document.getElementById("activity-list").innerHTML = '<p class="activity__empty">Sign in to view activity.</p>';
    activitySessions = [];
  }

  document.getElementById("auth-btn").addEventListener("click", async () => {
    if (!currentUser) {
      showLoginModal();
      return;
    }
    await fetch("/api/auth/logout", { method: "POST" });
    currentUser = null;
    document.getElementById("auth-btn").textContent = "Sign in";
    setDashboardAccess(false);
    showLoginModal();
    toast("Signed out.", "success");
  });

  document.getElementById("login-close").addEventListener("click", () => {
    document.getElementById("login-modal").hidden = true;
  });

  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const response = await fetch("/api/auth/login", {
      method: "POST", headers: jsonHeaders(),
      body: JSON.stringify({ username: form.username.value, password: form.password.value }),
    });
    const data = await response.json();
    if (!data.ok) {
      toast(data.error, "error");
      return;
    }
    currentUser = data.user;
    setDashboardAccess(true);
    document.getElementById("login-modal").hidden = true;
    document.getElementById("auth-btn").textContent = `Sign out (${currentUser.username})`;
    document.getElementById("profile-btn").hidden = false;
    document.getElementById("login-close").hidden = false;
    toast("Signed in successfully.", "success");
  });

  document.querySelectorAll(".report-btn").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!currentUser) {
        event.preventDefault();
        showLoginModal();
      }
    });
  });

  document.getElementById("profile-btn").addEventListener("click", async () => {
    await loadProfile();
    document.getElementById("profile-modal").hidden = false;
  });
  document.getElementById("profile-close").addEventListener("click", () => {
    document.getElementById("profile-modal").hidden = true;
  });

  async function loadProfile() {
    const response = await fetch("/api/profile");
    if (response.status === 401) {
      currentUser = null;
      setDashboardAccess(false);
      showLoginModal();
      return;
    }
    const data = await response.json();
    if (!data.user) return;
    document.getElementById("profile-username").value = data.user.username;
    document.getElementById("profile-sessions").innerHTML = data.sessions.map((item) => {
      const revoked = item.revoked;
      const current = item.current;
      const label = current ? "This session" : (revoked ? "Revoked" : "Revoke");
      return `<div class="profile-session"><div><strong>${escapeHtml(item.user_agent || "Browser session")}${current ? " · this browser" : ""}</strong><span>${escapeHtml(item.last_seen)}${revoked ? " · revoked" : ""}</span></div><button class="page-btn" data-session-id="${item.id}" ${revoked || current ? "disabled" : ""}>${label}</button></div>`;
    }).join("");
    document.querySelectorAll("#profile-sessions [data-session-id]").forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const revokeResponse = await fetch(`/api/profile/sessions/${button.dataset.sessionId}`, { method: "DELETE" });
        const revokeData = await revokeResponse.json().catch(() => ({}));
        if (!revokeResponse.ok || !revokeData.ok) {
          toast(revokeData.error || "Could not revoke that session.", "error");
          button.disabled = false;
          return;
        }
        toast("Session revoked.", "success");
      } catch (error) {
        console.error("Revoke session failed", error);
        toast("Could not revoke that session.", "error");
        button.disabled = false;
        return;
      }
      loadProfile();
    }));
  }

  document.getElementById("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const response = await fetch("/api/profile", { method: "PUT", headers: jsonHeaders(), body: JSON.stringify(Object.fromEntries(new FormData(form))) });
    const data = await response.json();
    if (!data.ok) { toast(data.error, "error"); return; }
    currentUser.username = data.username;
    document.getElementById("auth-btn").textContent = `Sign out (${data.username})`;
    toast("Account details updated.", "success");
    form.reset();
    document.getElementById("profile-username").value = data.username;
  });

  function renderRates(rates, vatRate) {
    if (vatRate != null) document.getElementById("vat-rate").value = vatRate;
    document.getElementById("rates-list").innerHTML = rates.map((rate, index) => {
      const isFinal = rate.max_minutes >= 2147483647;
      return `<div class="rate-row">
        <span class="rate-row__step">${String(index + 1).padStart(2, "0")}</span>
        <label>Maximum minutes
          <input class="rate-limit" type="number" min="1" value="${rate.max_minutes}" ${isFinal ? "readonly" : ""}>
        </label>
        <label>Fee (KES)
          <input class="rate-fee" type="number" min="0" value="${rate.fee_amount}">
        </label>
        <span class="rate-row__caption">${isFinal ? "Longest-stay rate" : "Up to this duration"}</span>
      </div>`;
    }).join("");
  }

  document.getElementById("save-rates").addEventListener("click", async () => {
    const button = document.getElementById("save-rates");
    const rates = [...document.querySelectorAll(".rate-row")].map((row) => ({
      max_minutes: row.querySelector(".rate-limit").value,
      fee_amount: row.querySelector(".rate-fee").value,
    }));
    const vatRate = document.getElementById("vat-rate").value;
    button.disabled = true;
    try {
      const res = await fetch("/api/rates", { method: "PUT", headers: jsonHeaders(), body: JSON.stringify({ rates, vat_rate: vatRate }) });
      const data = await res.json();
      if (!data.ok) {
        toast(data.error, "error");
        return;
      }
      renderRates(data.rates, data.vat_rate);
      toast("Parking rates updated successfully.", "success");
    } catch (error) {
      toast("Unable to update parking rates.", "error");
    } finally {
      button.disabled = false;
    }
  });

  function renderHero(stats) {
    let totalAvail = 0;
    const breakdown = document.getElementById("stat-breakdown");
    breakdown.innerHTML = "";
    Object.entries(stats).forEach(([type, s]) => {
      totalAvail += s.available;
      const row = document.createElement("div");
      row.className = "breakdown-row";
      row.innerHTML = `<span>${capitalize(type)}</span><b>${s.available} / ${s.total} free</b>`;
      breakdown.appendChild(row);
    });
    const el = document.getElementById("stat-available");
    const prev = lastAvailable === null ? totalAvail : lastAvailable;
    animateCount(el, prev, totalAvail);
    lastAvailable = totalAvail;
  }

  function renderGrid(slots) {
    const grid = document.getElementById("slot-grid");
    const visible = slots.filter((s) => s.vehicle_type === activeZone);
    const isFirstRender = grid.childElementCount === 0;

    grid.innerHTML = "";
    visible.forEach((s) => {
      const cell = document.createElement("div");
      const free = s.status === "available";
      cell.className = "slot " + (free ? "slot--free" : "slot--occupied");
      cell.textContent = s.slot_number;
      cell.title = free ? "Available" : "Occupied";

      // Only flash a cell whose status actually changed since the last poll —
      // motion tied to a real state change, not a decorative entrance effect.
      const key = s.vehicle_type + ":" + s.slot_number;
      if (!isFirstRender && knownSlotStatus[key] && knownSlotStatus[key] !== s.status) {
        cell.classList.add("slot--flash");
      }
      knownSlotStatus[key] = s.status;
      grid.appendChild(cell);
    });
  }

  document.getElementById("zone-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    btn.classList.add("is-active");
    activeZone = btn.dataset.type;
    refreshSlots();
  });

  // Activity feed
  function parseActivityDate(value) {
    if (!value) return null;
    // Session records created before timezone support are naive UTC strings.
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatActivityTime(value) {
    const date = parseActivityDate(value);
    return date
      ? date.toLocaleTimeString("en-KE", {
        timeZone: "Africa/Nairobi",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      })
      : "—";
  }

  function formatActivityDate(value) {
    const date = parseActivityDate(value);
    return date
      ? date.toLocaleDateString("en-KE", {
        timeZone: "Africa/Nairobi",
        day: "2-digit",
        month: "short",
        year: "numeric",
      })
      : "—";
  }

  function renderActivity() {
    const list = document.getElementById("activity-list");
    const range = document.getElementById("activity-range");
    const pageLabel = document.getElementById("activity-page");
    const previous = document.getElementById("activity-prev");
    const next = document.getElementById("activity-next");
    const search = document.getElementById("activity-search").value.trim().toLowerCase();
    const sortBy = document.getElementById("activity-sort").value;
    const order = document.getElementById("activity-order").value === "asc" ? 1 : -1;
    const filtered = activitySessions
      .filter((s) => [s.vehicle, s.slot_number, s.status, s.entry_time, s.exit_time]
        .some((value) => String(value || "").toLowerCase().includes(search)))
      .sort((a, b) => {
        let comparison;
        if (sortBy === "status") {
          comparison = String(a.status).localeCompare(String(b.status));
        } else if (sortBy === "fee") {
          comparison = (a.fee_charged ?? -1) - (b.fee_charged ?? -1);
        } else {
          comparison = parseActivityDate(a.entry_time).getTime() - parseActivityDate(b.entry_time).getTime();
        }
        return comparison * order;
      });

    const totalPages = Math.max(1, Math.ceil(filtered.length / activityPageSize));
    activityPage = Math.min(activityPage, totalPages);
    const start = (activityPage - 1) * activityPageSize;
    const pageSessions = filtered.slice(start, start + activityPageSize);
    const end = Math.min(start + pageSessions.length, filtered.length);
    range.textContent = filtered.length ? `Showing ${start + 1}–${end} of ${filtered.length} records` : "Showing 0 records";
    pageLabel.textContent = `Page ${activityPage} of ${totalPages}`;
    previous.disabled = activityPage === 1;
    next.disabled = activityPage === totalPages;

    if (!filtered.length) {
      list.innerHTML = `<p class="activity__empty">${activitySessions.length ? "No matching activity." : "No activity yet today."}</p>`;
      return;
    }
      const head = `<div class="activity__row activity__row--head">
        <span>Vehicle</span><span>Slot</span><span>Date</span><span>Checked in</span><span>Checkout</span><span>Fee</span><span>Status</span><span>Receipt no.</span>
      </div>`;
      const rows = pageSessions.map((s) => {
        const activityDate = formatActivityDate(s.entry_time);
        const entryTime = formatActivityTime(s.entry_time);
        const checkoutTime = formatActivityTime(s.exit_time);
        const feeText = s.fee_charged != null ? `KES ${s.fee_charged}` : "—";
        return `<div class="activity__row">
          <span class="plate">${s.vehicle}</span>
          <span class="meta">Slot ${s.slot_number}</span>
          <span class="meta">${activityDate}</span>
          <span class="meta">${entryTime}</span>
          <span class="meta">${checkoutTime}</span>
          <span class="meta">${feeText}</span>
          <span class="activity__status activity__status--${s.status}">${s.status.replace("_", " ")}${s.overstay ? " &#9888; overstay" : ""}</span>
          <span class="meta">${s.receipt_number || "—"}</span>
        </div>`;
      }).join("");
    list.innerHTML = head + rows;
  }

  async function refreshActivity() {
    try {
      const res = await fetch("/api/activity");
      const data = await res.json();
      activitySessions = data.sessions;
      renderActivity();
    } catch (e) {
      console.error("Failed to load activity", e);
    }
  }

  ["activity-search", "activity-sort", "activity-order"].forEach((id) => {
    document.getElementById(id).addEventListener("input", () => { activityPage = 1; renderActivity(); });
    document.getElementById(id).addEventListener("change", () => { activityPage = 1; renderActivity(); });
  });

  document.getElementById("activity-prev").addEventListener("click", () => {
    if (activityPage > 1) { activityPage -= 1; renderActivity(); }
  });
  document.getElementById("activity-next").addEventListener("click", () => {
    activityPage += 1;
    renderActivity();
  });

  // Attendant tools: trie plate search
  document.getElementById("plate-search").addEventListener("click", async () => {
    const prefix = document.getElementById("plate-prefix").value.trim();
    const box = document.getElementById("plate-results");
    if (!prefix) { toast("Type a plate prefix (e.g. KDA).", "error"); return; }
    try {
      const res = await fetch(`/api/plates?prefix=${encodeURIComponent(prefix)}`);
      if (res.status === 401 || res.status === 403) {
        toast("Sign in as manager to search plates.", "error");
        return;
      }
      const data = await res.json();
      if (!data.ok) { toast(data.error, "error"); return; }
      box.hidden = false;
      box.innerHTML = data.matches.length
        ? data.matches.map((m) => `<div class="plate-results__row"><strong>${escapeHtml(m.plate_number)}</strong><span>${m.status === "active" ? `parked — slot ${m.slot_number}` : "not currently parked"}</span></div>`).join("")
        : '<p class="activity__empty">No plates match that prefix.</p>';
    } catch (err) {
      toast("Plate search failed.", "error");
    }
  });

  // Attendant tools: maintenance + barrier override
  async function postOverride(url, payload, successMessage) {
    try {
      const res = await fetch(url, { method: "POST", headers: jsonHeaders(), body: JSON.stringify(payload) });
      const data = await res.json();
      if (res.status === 401 || res.status === 403) {
        toast("Manager sign-in required for overrides.", "error");
        playChime("error");
        return;
      }
      if (!data.ok) { toast(data.error, "error"); playChime("error"); return; }
      toast(successMessage, "success");
      playChime("success");
      refreshSlots();
    } catch (err) {
      toast("Override failed — is the server running?", "error");
      playChime("error");
    }
  }
  document.getElementById("maintenance-off").addEventListener("click", () => {
    const slot = document.getElementById("maintenance-slot").value;
    const reason = document.getElementById("maintenance-reason").value.trim();
    if (!slot || !reason) { toast("Slot number and reason are required.", "error"); return; }
    postOverride(`/api/slots/${slot}/maintenance`, { out_of_service: true, reason }, `Slot ${slot} marked out of service.`);
  });
  document.getElementById("maintenance-on").addEventListener("click", () => {
    const slot = document.getElementById("maintenance-slot").value;
    const reason = document.getElementById("maintenance-reason").value.trim();
    if (!slot || !reason) { toast("Slot number and reason are required.", "error"); return; }
    postOverride(`/api/slots/${slot}/maintenance`, { out_of_service: false, reason }, `Slot ${slot} back in service.`);
  });
  document.getElementById("override-open").addEventListener("click", () => {
    const sessionId = document.getElementById("override-session").value;
    const reason = document.getElementById("override-reason").value.trim();
    if (!sessionId || !reason) { toast("Session ID and reason are required.", "error"); return; }
    postOverride("/api/barrier/override", { session_id: sessionId, reason }, "Barrier open signal sent (audited).");
  });

  // Analytics (manager)
  async function loadAnalytics() {
    try {
      const res = await fetch("/api/analytics");
      if (res.status === 401 || res.status === 403) return; // section stays empty
      const data = await res.json();
      if (!data.ok) return;
      const a = data.analytics;
      document.getElementById("analytics-window").textContent =
        `Last ${a.window_days} days · KES ${a.total_revenue} total${data.overstays ? ` · ${data.overstays} overstay alert${data.overstays > 1 ? "s" : ""}` : ""}`;
      const dayMax = Math.max(1, ...a.revenue_by_day.map((d) => d.amount));
      const hourMax = Math.max(1, ...a.revenue_by_hour_today.map((d) => d.amount));
      const bar = (label, value, max, caption) =>
        `<div class="analytics__row"><span class="analytics__label">${label}</span>
           <span class="analytics__bar"><i style="width:${Math.round((value / max) * 100)}%"></i></span>
           <span class="analytics__value">${caption}</span></div>`;
      document.getElementById("analytics-grid").innerHTML = `
        <div class="panel card"><h3>Revenue by day</h3>
          ${a.revenue_by_day.length ? a.revenue_by_day.map((d) => bar(d.date, d.amount, dayMax, `KES ${d.amount}`)).join("") : '<p class="activity__empty">No paid sessions in this window.</p>'}
        </div>
        <div class="panel card"><h3>Revenue by hour (today)</h3>
          ${a.revenue_by_hour_today.length ? a.revenue_by_hour_today.map((d) => bar(`${String(d.hour).padStart(2, "0")}:00`, d.amount, hourMax, `KES ${d.amount}`)).join("") : '<p class="activity__empty">No payments yet today.</p>'}
        </div>
        <div class="panel card"><h3>Occupancy by zone</h3>
          ${a.occupancy_by_zone.map((z) => `<div class="plate-results__row"><strong>Zone ${escapeHtml(z.zone)}</strong><span>${z.occupied} occupied · ${z.available} free${z.maintenance ? ` · ${z.maintenance} out of service` : ""} · ${Math.round(z.utilization * 100)}% full</span></div>`).join("")}
        </div>
        <div class="panel card"><h3>Insights</h3>
          <div class="plate-results__row"><strong>Busiest zone</strong><span>${a.busiest_zone ? `Zone ${escapeHtml(a.busiest_zone.zone)} (${a.busiest_zone.sessions} sessions)` : "—"}</span></div>
          ${a.revenue_by_method.map((m) => `<div class="plate-results__row"><strong>${escapeHtml(m.method)}</strong><span>KES ${m.amount}</span></div>`).join("")}
          ${a.avg_duration_by_zone.map((z) => `<div class="plate-results__row"><strong>Zone ${escapeHtml(z.zone)} avg stay</strong><span>${z.avg_minutes} min</span></div>`).join("")}
        </div>`;
    } catch (err) {
      console.error("Analytics load failed", err);
    }
  }
  loadAnalytics();

  // Sound cues (WebAudio — no assets needed)
  let audioCtx = null;
  function playChime(kind = "success") {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const now = audioCtx.currentTime;
      const notes = kind === "success" ? [[523.25, 0], [659.25, 0.12], [783.99, 0.24]]
        : kind === "alert" ? [[440, 0], [554.37, 0.15]]
        : [[220, 0], [174.61, 0.16]];
      notes.forEach(([freq, offset]) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = kind === "error" ? "sawtooth" : "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(0.08, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.22);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now + offset);
        osc.stop(now + offset + 0.25);
      });
    } catch (err) { /* audio is best-effort; never block the flow */ }
  }

  // Entry ticket modal
  let lastTicketCode = "";
  function showEntryTicket(data) {
    lastTicketCode = data.ticket_code || "";
    if (!lastTicketCode) return;
    document.getElementById("ticket-qr").src = data.ticket_qr;
    document.getElementById("ticket-code").textContent = lastTicketCode;
    const openLink = document.getElementById("ticket-open");
    if (data.ticket_url) { openLink.href = data.ticket_url; openLink.hidden = false; }
    else { openLink.hidden = true; }
    document.getElementById("ticket-modal").hidden = false;
  }
  document.getElementById("ticket-close").addEventListener("click", () => {
    document.getElementById("ticket-modal").hidden = true;
  });
  document.getElementById("ticket-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(lastTicketCode);
      toast("Ticket code copied.", "success");
    } catch (err) {
      toast("Copy failed — select the code manually.", "error");
    }
  });

  // Exit: ticket code -> plate
  document.getElementById("use-ticket").addEventListener("click", async () => {
    const input = document.getElementById("ticket-code");
    const code = input.value.trim();
    if (!code) { toast("Paste or scan a ticket code first.", "error"); return; }
    try {
      const res = await fetch(`/api/ticket/${encodeURIComponent(code)}`);
      const data = await res.json();
      if (!data.ok) { toast(data.error, "error"); playChime("error"); return; }
      const exitPlate = document.querySelector("#exit-form input[name=plate_number]");
      exitPlate.value = data.plate_number;
      toast(`Ticket OK — ${data.plate_number} in slot ${data.slot_number}.`, "success");
      playChime("success");
    } catch (err) {
      toast("Could not resolve that ticket.", "error");
      playChime("error");
    }
  });

  // Entry form
  const entryForm = document.getElementById("entry-form");
  entryForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector("button[type=submit]");
    const payload = {
      plate_number: form.plate_number.value,
      vehicle_type: form.vehicle_type.value,
      owner_phone: form.owner_phone.value,
    };
    submitBtn.disabled = true;
    try {
      const res = await fetch("/api/entry", { method: "POST", headers: jsonHeaders(), body: JSON.stringify(payload) });
      const data = await res.json();
      if (!data.ok) {
        toast(data.error, "error");
        playChime("error");
        return;
      }
      toast(`${data.session.vehicle} checked in — slot ${data.session.slot_number}.`, "success");
      playChime("success");
      playBarrierSequence(`Entering — slot ${data.session.slot_number}`);
      showEntryTicket(data);
      form.reset();
      refreshSlots();
      refreshActivity();
    } catch (err) {
      toast("Network error — is the server running?", "error");
    } finally {
      submitBtn.disabled = false;
    }
  });

  // Exit form
  const exitForm = document.getElementById("exit-form");
  exitForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector("button[type=submit]");
    const plate = form.plate_number.value;
    document.getElementById("fee-box").hidden = true;
    submitBtn.disabled = true;

    try {
      const res = await fetch("/api/exit", { method: "POST", headers: jsonHeaders(), body: JSON.stringify({ plate_number: plate }) });
      const data = await res.json();
      if (!data.ok) {
        toast(data.error, "error");
        playChime("error");
        return;
      }
      const s = data.session;
      if (!data.payment_required) {
        toast(`Free exit (${s.duration_minutes} min) — safe travels!`, "success");
        playChime("success");
        playBarrierSequence("Exiting — safe travels");
        form.reset();
        refreshSlots();
        refreshActivity();
        return;
      }
      pendingSession = s;
      document.getElementById("fee-duration").textContent = `${s.duration_minutes} min`;
      document.getElementById("fee-subtotal").textContent = `KES ${s.subtotal_amount ?? s.fee_charged}`;
      document.getElementById("fee-vat-rate").textContent = s.vat_rate ?? 0;
      document.getElementById("fee-vat").textContent = `KES ${s.vat_amount ?? 0}`;
      document.getElementById("fee-amount").textContent = `KES ${s.total_amount ?? s.fee_charged}`;
      document.getElementById("stk-phone").textContent = s.owner_phone || "—";
      document.getElementById("stk-phone-row").hidden = selectedMethod !== "mpesa";
      document.getElementById("fee-box").hidden = false;
    } catch (err) {
      toast("Network error — is the server running?", "error");
    } finally {
      submitBtn.disabled = false;
    }
  });

  document.getElementById("fee-methods").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    document.querySelectorAll("#fee-methods .chip").forEach((c) => c.classList.remove("is-active"));
    chip.classList.add("is-active");
    selectedMethod = chip.dataset.method;
    updatePaymentActionLabel();
    if (pendingSession) {
      document.getElementById("stk-phone-row").hidden = selectedMethod !== "mpesa";
    }
  });

  function updatePaymentActionLabel() {
    const labels = {
      mpesa: "Send STK push & pay",
      cash: "Confirm cash payment & lift barrier",
      card: "Open PayPal & pay",
    };
    document.getElementById("pay-btn-label").textContent = labels[selectedMethod];
  }

  document.getElementById("pay-btn").addEventListener("click", async (e) => {
    if (!pendingSession) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await fetch("/api/pay", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ session_id: pendingSession.id, method: selectedMethod }),
      });
      const data = await res.json();
      if (!data.ok) {
        toast(data.error, "error");
        return;
      }
      if (selectedMethod === "mpesa") {
        document.getElementById("fee-box").hidden = true;
        showPaymentModal("waiting");
        pollPaymentStatus(pendingSession.id);
        return;
      }
      if (selectedMethod === "card") {
        paypalPending = { sessionId: pendingSession.id, orderId: data.payment.order_id };
        document.getElementById("fee-box").hidden = true;
        showPaymentModal("paypal", `Approve the PayPal payment in the new window, then return here.`);
        window.open(data.payment.approval_url, "_blank", "noopener");
        return;
      }
      const paymentMessage = data.payment?.initiated
        ? `STK push sent to ${data.payment.phone}`
        : `Payment received (KES ${data.session.fee_charged})`;
      toast(`${paymentMessage} — safe travels!`, "success");
      showReceipt(data.receipt);
      playBarrierSequence("Exiting — safe travels");
      document.getElementById("fee-box").hidden = true;
      exitForm.reset();
      pendingSession = null;
      refreshSlots();
      refreshActivity();
    } catch (err) {
      toast("Network error — is the server running?", "error");
    } finally {
      btn.disabled = false;
    }
  });

  function showPaymentModal(state, message) {
    const modal = document.getElementById("payment-modal");
    const icon = document.getElementById("payment-modal-icon");
    const title = document.getElementById("payment-modal-title");
    const detail = document.getElementById("payment-modal-message");
    const close = document.getElementById("payment-modal-close");
    modal.hidden = false;
    close.hidden = state === "waiting";
    close.textContent = state === "paypal" ? "I approved PayPal - check payment" : "Close";
    if (state === "success") {
      icon.textContent = "✓";
      title.textContent = "Payment successful";
      detail.textContent = message || "Your payment was confirmed. The barrier is opening.";
    } else if (state === "failed") {
      icon.textContent = "!";
      title.textContent = "Payment not completed";
      detail.textContent = message || "The payment was cancelled or declined. Please try again.";
    } else if (state === "paypal") {
      icon.textContent = "$";
      title.textContent = "Complete payment in PayPal";
      detail.textContent = message || "Approve the payment in PayPal, then return here.";
    } else {
      icon.textContent = "⌛";
      title.textContent = "Waiting for payment";
      detail.textContent = message || "Check your phone and enter your M-Pesa PIN to continue.";
    }
  }

  function closePaymentModal() {
    clearTimeout(paymentPollTimer);
    document.getElementById("payment-modal").hidden = true;
  }

  async function capturePayPalPayment() {
    if (!paypalPending) return;
    const close = document.getElementById("payment-modal-close");
    close.disabled = true;
    close.textContent = "Checking PayPal...";
    try {
      const res = await fetch(`/api/paypal/capture/${paypalPending.sessionId}`, {
        method: "POST", headers: jsonHeaders(), body: JSON.stringify({ order_id: paypalPending.orderId }),
      });
      const data = await res.json();
      if (!data.ok) {
        showPaymentModal("failed", data.error);
        return;
      }
      showPaymentModal("success");
      playBarrierSequence("Payment confirmed - exiting");
      showReceipt(data.receipt);
      paypalPending = null;
      pendingSession = null;
      exitForm.reset();
      refreshSlots();
      refreshActivity();
    } catch (error) {
      showPaymentModal("failed", "Unable to confirm PayPal payment. Please try again.");
    } finally {
      close.disabled = false;
    }
  }

  async function pollPaymentStatus(sessionId) {
    try {
      const res = await fetch(`/api/payment-status/${sessionId}`);
      const data = await res.json();
      if (!data.ok || data.status === "failed") {
        showPaymentModal("failed", data.message || data.error);
        return;
      }
      if (data.status === "success") {
        showPaymentModal("success");
        playBarrierSequence("Payment confirmed — exiting");
        showReceipt(data.receipt);
        pendingSession = null;
        exitForm.reset();
        refreshSlots();
        refreshActivity();
        return;
      }
      paymentPollTimer = setTimeout(() => pollPaymentStatus(sessionId), 3000);
    } catch (error) {
      paymentPollTimer = setTimeout(() => pollPaymentStatus(sessionId), 5000);
    }
  }

  document.getElementById("payment-modal-close").addEventListener("click", () => {
    if (paypalPending) capturePayPalPayment();
    else closePaymentModal();
  });

  function escapeHtml(value) {
    return String(value ?? "—").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;",
    })[character]);
  }

  function receiptDateTime(value) {
    return `${formatActivityDate(value)} ${formatActivityTime(value)}`;
  }

  function showReceipt(receipt) {
    if (!receipt) return;
    const details = [
      ["Reg number", receipt.registration_number],
      ["Phone number", receipt.phone_number],
      ["Vehicle type", receipt.vehicle_type],
      ["Check-in", receiptDateTime(receipt.checkin)],
      ["Checkout", receiptDateTime(receipt.checkout)],
      ["Date and time", receiptDateTime(receipt.date_time)],
      ["Subtotal", `${receipt.currency} ${receipt.subtotal_amount}`],
      [`VAT (${receipt.vat_rate}%)`, `${receipt.currency} ${receipt.vat_amount}`],
      ["Amount paid", `${receipt.currency} ${receipt.total_amount}`],
      ["Method of payment", receipt.payment_method],
    ];
    document.getElementById("receipt-number").textContent = receipt.receipt_number;
    document.getElementById("receipt-business-name").textContent = receipt.business_name;
    document.getElementById("receipt-business-details").textContent = [receipt.business_address, receipt.kra_pin ? `KRA PIN: ${receipt.kra_pin}` : ""].filter(Boolean).join(" | ");
    document.getElementById("receipt-details").innerHTML = details.map(([label, value]) =>
      `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
    document.getElementById("receipt-verified").textContent = receipt.verified ? "Verified payment" : "Unverified";
    document.getElementById("receipt-qr").src = receipt.qr_code;
    document.getElementById("receipt-modal").hidden = false;
  }

  document.getElementById("receipt-close").addEventListener("click", () => {
    document.getElementById("receipt-modal").hidden = true;
  });
  document.getElementById("receipt-print").addEventListener("click", () => window.print());

  // Helpers
  function jsonHeaders() { return { "Content-Type": "application/json" }; }
  function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  // Boot
  refreshSlots();
  refreshAuth();
  refreshRates();
  refreshActivity();
  setInterval(refreshSlots, 4000);
  setInterval(refreshActivity, 6000);
})();
