/* app.js — ParkFlow frontend for the Flask REST API (app.py). */
(function () {
  "use strict";

  // byId returns the element or null when the current subpage does not have it.
  function byId(id) { return document.getElementById(id); }
  // onX binds a handler only when the element exists on the current subpage.
  function onGet(id, event, handler) {
    const node = byId(id);
    if (node) node.addEventListener(event, handler);
  }
  function onAll(selector, event, handler) {
    document.querySelectorAll(selector).forEach((node) => node.addEventListener(event, handler));
  }
  // These mutate one element; safe to call when it lives on another subpage.
  function setText(id, value) {
    const node = byId(id);
    if (node) node.textContent = value;
  }
  function setHidden(id, value) {
    const node = byId(id);
    if (node) node.hidden = value;
  }
  function setValue(id, value) {
    const node = byId(id);
    if (node) node.value = value;
  }
  function setHtml(id, value) {
    const node = byId(id);
    if (node) node.innerHTML = value;
  }

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

  // Server-owned copy of the "lot is full" wording, so the UI can never drift
  // from what the API actually tells a driver.
  const LOT_FULL_MESSAGE = "Parking is currently full, please try again later.";

  // Clock
  function tickClock() {
    const node = byId("clock");
    if (!node) return;
    node.textContent =
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

  // Toasts
  function toast(message, type = "success") {
    const stack = byId("toast-stack");
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
  // No-op on subpages without the overview barrier scene.
  function playBarrierSequence(caption) {
    const scene = byId("barrier");
    const arm = byId("armGroup");
    const captionEl = byId("barrier-caption");
    if (!scene || !arm || !captionEl) return;

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
      renderCapacity(data);
    } catch (e) {
      console.error("Failed to load slots", e);
    }
  }

  // Full-lot state for the entry panel. available_total is the whole-lot
  // figure from the server; at zero the entry door is shut and stays shut
  // until a bay is released.
  function renderCapacity(data) {
    const banner = byId("lot-full");
    const note = byId("capacity-note");
    const submit = byId("entry-submit");
    if (!banner && !note && !submit) return; // not on the entry subpage
    const available = Number.isFinite(data.available_total)
      ? data.available_total
      : Object.values(data.stats || {}).reduce((sum, s) => sum + s.available, 0);
    if (banner) banner.hidden = available !== 0;
    if (note) {
      note.textContent = available === 0
        ? "No bays free right now."
        : `${available} ${available === 1 ? "bay" : "bays"} free right now.`;
    }
    if (submit) {
      submit.disabled = available === 0;
      submit.title = available === 0 ? LOT_FULL_MESSAGE : "";
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

  function renderRates(rates, vatRate) {
    // No-op on subpages without the rates panel.
    if (!byId("rates-list")) return;
    setValue("vat-rate", vatRate != null ? vatRate : byId("vat-rate").value);
    setHtml("rates-list", rates.map((rate, index) => {
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
    }).join(""));
  }

  onGet("save-rates", "click", async () => {
    const button = byId("save-rates");
    const rates = [...document.querySelectorAll(".rate-row")].map((row) => ({
      max_minutes: row.querySelector(".rate-limit").value,
      fee_amount: row.querySelector(".rate-fee").value,
    }));
    const vatRate = byId("vat-rate").value;
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
    // No-op on subpages without the hero counters.
    const breakdown = byId("stat-breakdown");
    if (!breakdown) return;
    let totalAvail = 0;
    breakdown.innerHTML = "";
    Object.entries(stats).forEach(([type, s]) => {
      totalAvail += s.available;
      const row = document.createElement("div");
      row.className = "breakdown-row";
      row.innerHTML = `<span>${capitalize(type)}</span><b>${s.available} / ${s.total} free</b>`;
      breakdown.appendChild(row);
    });
    const el = byId("stat-available");
    const prev = lastAvailable === null ? totalAvail : lastAvailable;
    animateCount(el, prev, totalAvail);
    lastAvailable = totalAvail;
  }

  function renderGrid(slots) {
    // No-op on subpages without the slot map.
    const grid = byId("slot-grid");
    if (!grid) return;
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

  onGet("zone-tabs", "click", (e) => {
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
    // No-op on subpages without the activity feed.
    const list = byId("activity-list");
    if (!list) return;
    const range = byId("activity-range");
    const pageLabel = byId("activity-page");
    const previous = byId("activity-prev");
    const next = byId("activity-next");
    const search = byId("activity-search").value.trim().toLowerCase();
    const sortBy = byId("activity-sort").value;
    const order = byId("activity-order").value === "asc" ? 1 : -1;
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
    const node = byId(id);
    if (!node) return;
    node.addEventListener("input", () => { activityPage = 1; renderActivity(); });
    node.addEventListener("change", () => { activityPage = 1; renderActivity(); });
  });

  onGet("activity-prev", "click", () => {
    if (activityPage > 1) { activityPage -= 1; renderActivity(); }
  });
  onGet("activity-next", "click", () => {
    activityPage += 1;
    renderActivity();
  });

  // Attendant tools: trie plate search
  onGet("plate-search", "click", async () => {
    const prefix = byId("plate-prefix").value.trim();
    const box = byId("plate-results");
    if (!prefix) { toast("Type a plate prefix (e.g. KDA).", "error"); return; }
    try {
      const res = await fetch(`/api/plates?prefix=${encodeURIComponent(prefix)}`);
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
    const qr = byId("ticket-qr");
    if (qr) qr.src = data.ticket_qr;
    setText("ticket-code", lastTicketCode);
    const openLink = byId("ticket-open");
    if (openLink) {
      if (data.ticket_url) { openLink.href = data.ticket_url; openLink.hidden = false; }
      else { openLink.hidden = true; }
    }
    setHidden("ticket-modal", false);
  }
  onGet("ticket-close", "click", () => {
    setHidden("ticket-modal", true);
  });
  onGet("ticket-copy", "click", async () => {
    try {
      await navigator.clipboard.writeText(lastTicketCode);
      toast("Ticket code copied.", "success");
    } catch (err) {
      toast("Copy failed — select the code manually.", "error");
    }
  });

  // Exit: ticket code -> plate. NB the input is #exit-ticket-code; #ticket-code
  // is the read-only <code> in the entry-ticket modal, which showEntryTicket
  // fills in — sharing one id would make byId() return the wrong element.
  onGet("use-ticket", "click", async () => {
    const input = byId("exit-ticket-code");
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

  // After a successful entry from another subpage, the barrier scene and the
  // exit panel live elsewhere, so hand the driver the ticket details to scan.
  function maybePromptExit() {
    if (byId("exit-form") || !lastTicketCode) return;
    toast(`Checked in — scan this ticket at the exit panel: ${lastTicketCode}`, "success");
  }

  // Entry form
  const entryForm = byId("entry-form");
  if (entryForm) entryForm.addEventListener("submit", async (e) => {
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
        // A full lot always reads the same way, whatever the API reported.
        toast(data.full ? LOT_FULL_MESSAGE : data.error, "error");
        playChime("error");
        if (data.full) refreshSlots();
        return;
      }
      toast(`${data.session.vehicle} checked in — slot ${data.session.slot_number}.`, "success");
      playChime("success");
      playBarrierSequence(`Entering — slot ${data.session.slot_number}`);
      showEntryTicket(data);
      form.reset();
      refreshSlots();
      refreshActivity();
      maybePromptExit();
    } catch (err) {
      toast("Network error — is the server running?", "error");
    } finally {
      submitBtn.disabled = false;
    }
  });

  // Exit form
  const exitForm = byId("exit-form");
  if (exitForm) exitForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector("button[type=submit]");
    const plate = form.plate_number.value;
    setHidden("fee-box", true);
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
      setText("fee-duration", `${s.duration_minutes} min`);
      setText("fee-subtotal", `KES ${s.subtotal_amount ?? s.fee_charged}`);
      setText("fee-vat-rate", s.vat_rate ?? 0);
      setText("fee-vat", `KES ${s.vat_amount ?? 0}`);
      setText("fee-amount", `KES ${s.total_amount ?? s.fee_charged}`);
      setText("stk-phone", s.owner_phone || "—");
      setHidden("stk-phone-row", selectedMethod !== "mpesa");
      setHidden("fee-box", false);
    } catch (err) {
      toast("Network error — is the server running?", "error");
    } finally {
      submitBtn.disabled = false;
    }
  });

  onGet("fee-methods", "click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    document.querySelectorAll("#fee-methods .chip").forEach((c) => c.classList.remove("is-active"));
    chip.classList.add("is-active");
    selectedMethod = chip.dataset.method;
    updatePaymentActionLabel();
    if (pendingSession) {
      setHidden("stk-phone-row", selectedMethod !== "mpesa");
    }
  });

  function updatePaymentActionLabel() {
    const labels = {
      mpesa: "Send STK push & pay",
      cash: "Confirm cash payment & lift barrier",
      card: "Open PayPal & pay",
    };
    setText("pay-btn-label", labels[selectedMethod]);
  }

  onGet("pay-btn", "click", async (e) => {
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
        setHidden("fee-box", true);
        showPaymentModal("waiting");
        pollPaymentStatus(pendingSession.id);
        return;
      }
      if (selectedMethod === "card") {
        paypalPending = { sessionId: pendingSession.id, orderId: data.payment.order_id };
        setHidden("fee-box", true);
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
      setHidden("fee-box", true);
      if (exitForm) exitForm.reset();
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
    const modal = byId("payment-modal");
    const icon = byId("payment-modal-icon");
    const title = byId("payment-modal-title");
    const detail = byId("payment-modal-message");
    const close = byId("payment-modal-close");
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
    setHidden("payment-modal", true);
  }

  async function capturePayPalPayment() {
    if (!paypalPending) return;
    const close = byId("payment-modal-close");
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
      if (exitForm) exitForm.reset();
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
        if (exitForm) exitForm.reset();
        refreshSlots();
        refreshActivity();
        return;
      }
      paymentPollTimer = setTimeout(() => pollPaymentStatus(sessionId), 3000);
    } catch (error) {
      paymentPollTimer = setTimeout(() => pollPaymentStatus(sessionId), 5000);
    }
  }

  onGet("payment-modal-close", "click", () => {
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
    setText("receipt-number", receipt.receipt_number);
    setText("receipt-business-name", receipt.business_name);
    setText("receipt-business-details", [receipt.business_address, receipt.kra_pin ? `KRA PIN: ${receipt.kra_pin}` : ""].filter(Boolean).join(" | "));
    setHtml("receipt-details", details.map(([label, value]) =>
      `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join(""));
    setText("receipt-verified", receipt.verified ? "Verified payment" : "Unverified");
    const qr = byId("receipt-qr");
    if (qr) qr.src = receipt.qr_code;
    setHidden("receipt-modal", false);
  }

  onGet("receipt-close", "click", () => {
    setHidden("receipt-modal", true);
  });
  onGet("receipt-print", "click", () => window.print());

  // Helpers
  function jsonHeaders() { return { "Content-Type": "application/json" }; }
  function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  // Top bar: the kiosk is ONE page, so a nav link just scrolls to its own
  // section. The active link then follows the attendant down the page.
  (function initSectionNav() {
    const links = Array.from(document.querySelectorAll(".topnav__link"));
    const sections = links
      .map((link) => document.getElementById(link.dataset.nav))
      .filter(Boolean);
    if (!sections.length) return; // no top bar (e.g. the public scan pages)

    function markActive(anchor) {
      links.forEach((link) => {
        link.classList.toggle("is-active", link.dataset.nav === anchor);
      });
    }

    links.forEach((link) => {
      link.addEventListener("click", () => markActive(link.dataset.nav));
    });

    // Highlight whichever section owns the reading line, so scrolling by hand
    // keeps the top bar in step. rAF-throttled: scroll fires far too often.
    let queued = false;
    window.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        const line = window.scrollY + window.innerHeight * 0.35;
        let current = sections[0];
        sections.forEach((section) => {
          if (section.offsetTop <= line) current = section;
        });
        markActive(current.id);
      });
    }, { passive: true });

    // Opening /#slots must land on the Slots link, not on Overview.
    const hash = window.location.hash.slice(1);
    if (hash && document.getElementById(hash)) markActive(hash);
  })();

  // Boot
  // One page carries every panel, so nothing needs a sign-in step.
  refreshSlots();
  refreshRates();
  refreshActivity();
  // Polled panels re-poll on a timer; empty timer calls no-op via their guards.
  setInterval(refreshSlots, 4000);
  setInterval(refreshActivity, 6000);
})();
