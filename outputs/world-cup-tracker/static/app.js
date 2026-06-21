const state = {
  snapshot: null,
  bracketStatus: null,
  desktopAlerts: false,
  macosNativeAlerts: false,
  filter: "all",
  search: "",
  team: "",
  group: "",
  teamsView: "group",
  date: "",
  calYM: null,
  pushActive: false,
  loading: false,
};

const DESKTOP_ALERTS_KEY = "wc2026DesktopAlerts";
let alertAudioContext = null;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function fmtPct(value) {
  return value == null ? "n/a" : `${Number(value).toFixed(1)}%`;
}

function fmtUsd(value) {
  const n = Number(value);
  if (value == null || !Number.isFinite(n) || n <= 0) return "";
  if (n >= 1e6) return `$${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `$${Math.round(n / 1e3)}k`;
  return `$${Math.round(n)}`;
}

function fmtScore(value) {
  return value == null ? "-" : String(Number(value));
}

function fmtDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function isToday(value) {
  if (!value) return false;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return false;

  const today = new Date();
  return (
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate()
  );
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function resultLabel(result) {
  return {
    hit: "Hit",
    miss: "Miss",
    draw: "Draw",
    pending: "Pending",
    unknown: "n/a",
  }[result] || result;
}

function sourceLabel(source) {
  return {
    match: "", // dropped: the USD figure already signals it's a match market
    outright: "outright",
    unknown: "unknown",
  }[source] ?? source;
}

function predictionText(label, pick, pickPct, source, volume, total) {
  const parts = [`${label}: ${escapeHtml(pick || "n/a")}`];
  if (pickPct != null) parts.push(fmtPct(pickPct));
  const src = source ? sourceLabel(source) : "";
  if (src) parts.push(escapeHtml(src));
  const usd = fmtUsd(volume);
  if (usd) {
    const totalUsd = fmtUsd(total);
    if (totalUsd) {
      // Highlight the total when the money share on this outcome is below its price.
      const underBacked = pickPct != null && total > 0 && volume / total < pickPct / 100;
      const totalPart = underBacked
        ? `<span class="totalHot">${totalUsd}</span><span class="hotMark" aria-hidden="true">!</span>`
        : totalUsd;
      parts.push(`${usd} of ${totalPart}`);
    } else {
      parts.push(usd);
    }
  }
  return parts.join(" | ");
}

function currentMarketLine(label, pick, pickPct, source, lockedPick, volume, total) {
  // Always render the row (a placeholder before kick-off) so every card reserves
  // the same height and the dividers line up across adjacent cards in the grid.
  if (!pick) return `<div class="currentLine currentEmpty">${label}: awaiting kick-off</div>`;
  const changedClass = lockedPick && pick !== lockedPick ? " currentChanged" : "";
  return `<div class="currentLine${changedClass}">${predictionText(label, pick, pickPct, source, volume, total)}</div>`;
}

function predictionGroup(lockedLabel, lockedPick, lockedPct, lockedSource, result, nowLine, lockedVolume, lockedTotal) {
  return `
        <div class="predGroup">
          <div class="predBody">
            <div class="leanPick">${predictionText(lockedLabel, lockedPick, lockedPct, lockedSource, lockedVolume, lockedTotal)}</div>
            ${nowLine}
          </div>
          <span class="tag ${result}">${resultLabel(result)}</span>
        </div>`;
}

function accuracy(hits, misses) {
  const total = hits + misses;
  return total ? `${Math.round((hits / total) * 100)}%` : "n/a";
}

function accuracyValue(hits, misses) {
  const total = hits + misses;
  return total ? hits / total : null;
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function renderBracketStatus(status) {
  const node = $("#bracketButton");
  if (!node) return;

  node.classList.toggle("needsGeneration", Boolean(status?.changed));
  node.title = status?.changed
    ? "Team composition changed since latest FINALS generation"
    : "Open current finals bracket";
}

async function loadBracketStatus() {
  const response = await fetch(`/api/bracket-status?ts=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Bracket status failed: ${response.status}`);
  state.bracketStatus = await response.json();
  renderBracketStatus(state.bracketStatus);
}

function loadBracketStatusQuietly() {
  loadBracketStatus().catch((error) => {
    console.error(error);
  });
}

function alertAudioSupported() {
  return Boolean(window.AudioContext || window.webkitAudioContext);
}

function getAlertAudioContext() {
  if (!alertAudioSupported()) return null;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!alertAudioContext) alertAudioContext = new AudioContextClass();
  return alertAudioContext;
}

async function unlockAlertSound() {
  const context = getAlertAudioContext();
  if (context?.state === "suspended") {
    await context.resume();
  }
}

function playAlertTone(context, frequency, start, duration) {
  const oscillator = context.createOscillator();
  const gain = context.createGain();

  oscillator.type = "sine";
  oscillator.frequency.setValueAtTime(frequency, start);
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(0.07, start + 0.018);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);

  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start(start);
  oscillator.stop(start + duration + 0.02);
}

function playAlertSound(type) {
  const context = getAlertAudioContext();
  if (!context || context.state === "suspended") return;

  const now = context.currentTime;
  const tonesByType = {
    liveScoreChange: [880, 1175],
    kickoff: [659, 988],
    favoriteChange: [988, 1319],
  };
  const tones = tonesByType[type] || [784, 1046];
  playAlertTone(context, tones[0], now, 0.16);
  playAlertTone(context, tones[1], now + 0.12, 0.22);
}

function desktopAlertsSupported() {
  return state.macosNativeAlerts || ("Notification" in window && window.isSecureContext);
}

function desktopAlertsEnabled() {
  const browserReady = "Notification" in window && window.isSecureContext && Notification.permission === "granted";
  return state.desktopAlerts && (state.macosNativeAlerts || browserReady);
}

function saveDesktopAlerts(enabled) {
  state.desktopAlerts = enabled;
  try {
    localStorage.setItem(DESKTOP_ALERTS_KEY, enabled ? "true" : "false");
  } catch (error) {
    console.error(error);
  }
  renderAlertsStatus();
}

function loadDesktopAlertPreference() {
  try {
    state.desktopAlerts = localStorage.getItem(DESKTOP_ALERTS_KEY) === "true";
  } catch (error) {
    state.desktopAlerts = false;
    console.error(error);
  }
  if (!state.macosNativeAlerts && (!("Notification" in window) || !window.isSecureContext || Notification.permission !== "granted")) {
    state.desktopAlerts = false;
  }
  renderAlertsStatus();
}

async function loadDesktopAlertCapability() {
  try {
    const response = await fetch("/api/desktop-alerts/capability", { cache: "no-store" });
    if (!response.ok) throw new Error(`Desktop alert capability failed: ${response.status}`);
    const capability = await response.json();
    state.macosNativeAlerts = Boolean(capability.macosNative);
  } catch (error) {
    state.macosNativeAlerts = false;
    console.error(error);
  }
  loadDesktopAlertPreference();
}

function renderAlertsStatus() {
  const node = $("#alertsButton");
  if (!node) return;

  const browserSupported = "Notification" in window && window.isSecureContext;
  const supported = state.macosNativeAlerts || browserSupported;
  const denied = !state.macosNativeAlerts && browserSupported && Notification.permission === "denied";
  const enabled = desktopAlertsEnabled();

  node.disabled = !supported;
  node.classList.toggle("alertsOn", enabled);
  node.classList.toggle("alertsBlocked", denied);
  node.textContent = enabled ? "ALERTS ON" : denied ? "BLOCKED" : "ALERTS";
  node.title = !supported
    ? "Desktop alerts are unavailable in this browser"
    : denied
      ? "Notifications are blocked in browser settings"
      : enabled
        ? state.macosNativeAlerts
          ? "macOS score alerts enabled"
          : "Browser score alerts enabled"
        : state.macosNativeAlerts
          ? "Enable macOS score alerts"
          : "Enable browser score alerts";
}

async function sendNativeDesktopNotification(notification, force = false) {
  if (!state.macosNativeAlerts || (!force && !desktopAlertsEnabled())) return false;

  try {
    const response = await fetch("/api/desktop-alert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: notification.title, text: notification.text, type: notification.type }),
    });
    return response.ok;
  } catch (error) {
    console.error(error);
    return false;
  }
}

function sendBrowserDesktopNotification(notification, force = false) {
  if (!force && !desktopAlertsEnabled()) return;
  // When Web Push is active the background poller delivers the OS notification, so
  // skip the foreground one to avoid double-buzzing (the in-page toast still shows).
  if (!force && state.pushActive) return;
  if (!("Notification" in window) || !window.isSecureContext || Notification.permission !== "granted") return;

  try {
    const desktopNotification = new Notification(`World Cup: ${notification.title}`, {
      body: notification.text,
      tag: `wc2026-${notification.type}-${Date.now()}`,
      renotify: false,
      silent: false,
    });

    desktopNotification.onclick = () => {
      window.focus();
      desktopNotification.close();
    };

    setTimeout(() => desktopNotification.close(), 12_000);
  } catch (error) {
    console.error(error);
  }
}

function sendDesktopNotification(notification, force = false) {
  if (state.macosNativeAlerts) {
    sendNativeDesktopNotification(notification, force).then((sent) => {
      if (!sent && !force) {
        sendBrowserDesktopNotification(notification, force);
      }
    });
    return;
  }

  sendBrowserDesktopNotification(notification, force);
}

async function toggleDesktopAlerts() {
  await unlockAlertSound();

  if (!desktopAlertsSupported()) {
    showToast({ type: "finalScore", title: "Desktop alerts unavailable", text: "This browser cannot show system notifications for this page." });
    return;
  }

  if (desktopAlertsEnabled()) {
    saveDesktopAlerts(false);
    unsubscribePush();
    showToast({ type: "finalScore", title: "Desktop alerts off", text: "Score updates will stay inside this page." });
    return;
  }

  if (state.macosNativeAlerts) {
    saveDesktopAlerts(true);
    const notification = { type: "finalScore", title: "Desktop alerts on", text: "macOS score alerts are enabled." };
    showToast(notification);
    sendDesktopNotification(notification, true);
    return;
  }

  if (Notification.permission === "denied") {
    saveDesktopAlerts(false);
    showToast({ type: "finalScore", title: "Notifications blocked", text: "Allow notifications for this site in browser settings to use desktop alerts." });
    return;
  }

  if (Notification.permission !== "granted") {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      saveDesktopAlerts(false);
      showToast({ type: "finalScore", title: "Desktop alerts off", text: "Notifications were not allowed." });
      return;
    }
  }

  saveDesktopAlerts(true);
  const notification = { type: "finalScore", title: "Desktop alerts on", text: "Score updates will appear even when you are in another app." };
  showToast(notification);
  sendDesktopNotification(notification, true);
  subscribePush();
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

async function subscribePush() {
  const vapidKey = document.body.dataset.vapidKey;
  if (!vapidKey || !("serviceWorker" in navigator) || !("PushManager" in window)) return;
  if (!window.isSecureContext || Notification.permission !== "granted") return;
  try {
    const registration = await navigator.serviceWorker.register("/sw.js");
    const subscription =
      (await registration.pushManager.getSubscription()) ||
      (await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      }));
    const response = await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: subscription.toJSON() }),
    });
    if (!response.ok) throw new Error(`Push registration failed: ${response.status}`);
    state.pushActive = true;
  } catch (error) {
    console.error("push subscribe failed", error);
  }
}

async function unsubscribePush() {
  state.pushActive = false;
  if (!("serviceWorker" in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.getRegistration();
    const subscription = registration && (await registration.pushManager.getSubscription());
    if (!subscription) return;
    const endpoint = subscription.endpoint;
    await subscription.unsubscribe();
    await fetch("/api/push/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }),
    });
  } catch (error) {
    console.error("push unsubscribe failed", error);
  }
}

function matchKey(match) {
  return match.id || `${match.date}|${match.home?.team}|${match.away?.team}`;
}

function scorePair(match) {
  const home = match.home?.score;
  const away = match.away?.score;
  if (home == null || away == null) return null;
  return { home: Number(home), away: Number(away) };
}

function matchMap(matches = []) {
  const map = {};
  matches.forEach((match) => {
    map[matchKey(match)] = match;
  });
  return map;
}

function scoreLine(match) {
  const home = match.home?.displayName || match.home?.team || "Home";
  const away = match.away?.displayName || match.away?.team || "Away";
  return `${home} ${fmtScore(match.home?.score)}-${fmtScore(match.away?.score)} ${away}`;
}

function matchLocation(match) {
  const location = match.location || {};
  const city = location.city || match.venue;
  const stage = match.stageLabel ? `(${match.stageLabel})` : "";
  if (!city && !stage) return "";

  const cityLabel = city ? `${location.flag ? `${location.flag} ` : ""}${city}` : "";
  const label = [cityLabel, stage].filter(Boolean).join(" ");
  const titleParts = [match.venue, location.city && location.countryCode ? location.countryCode : null].filter(Boolean);
  return `<span class="matchLocation" title="${escapeHtml(titleParts.join(" · "))}">${escapeHtml(label)}</span>`;
}

function normalizeMinute(value) {
  const text = String(value || "").trim();
  if (!text || /scheduled/i.test(text) || /^ft$/i.test(text) || /^final/i.test(text)) return null;

  const clockMatch = text.match(/^(\d{1,3})(?::\d{2})?$/);
  if (clockMatch) return `${clockMatch[1]}'`;
  return /\d/.test(text) ? text : null;
}

function minuteLabel(match) {
  const status = match.status || {};
  const candidates = [status.displayClock, status.shortDetail, status.detail];
  for (const candidate of candidates) {
    const minute = normalizeMinute(candidate);
    if (minute) return minute;
  }

  const clock = Number(status.clock);
  if (Number.isFinite(clock) && clock > 0) return `${Math.floor(clock / 60)}'`;
  return null;
}

function timedScoreLine(match) {
  const minute = minuteLabel(match);
  return minute ? `${minute}: ${scoreLine(match)}` : scoreLine(match);
}

function kickoffText(match) {
  const home = match.home?.displayName || match.home?.team || "Home";
  const away = match.away?.displayName || match.away?.team || "Away";
  const parts = [`${home} vs ${away}`];
  if (match.stageLabel) parts.push(match.stageLabel); // "Group A" or e.g. "1/16 Finals"
  const city = (match.location || {}).city || match.venue;
  if (city) parts.push(city);
  return parts.join(" · ");
}

function scoreNotification(previous, current) {
  if (!previous) return null;

  // Kick-off: the match flipped to live since the previous poll.
  if (current.status?.state === "in" && previous.status?.state !== "in" && !current.status?.completed) {
    return { type: "kickoff", title: "Kick-off", text: kickoffText(current) };
  }

  const currentScore = scorePair(current);
  if (!currentScore) return null;

  if (current.status.completed && !previous.status?.completed) {
    return { type: "finalScore", title: "Final score", text: timedScoreLine(current) };
  }

  if (current.status.state !== "in") return null;

  const previousScore = scorePair(previous);
  if (!previousScore || (previousScore.home === currentScore.home && previousScore.away === currentScore.away)) return null;

  const scorers = [];
  if (currentScore.home > previousScore.home) scorers.push(current.home?.displayName || current.home?.team);
  if (currentScore.away > previousScore.away) scorers.push(current.away?.displayName || current.away?.team);
  const minuteText = minuteLabel(current);
  const scorerText = scorers.filter(Boolean).length ? `${scorers.filter(Boolean).join(" and ")} scored. ` : "";
  return { type: "liveScoreChange", title: "Score change", text: `${minuteText ? `${minuteText}: ` : ""}${scorerText}${scoreLine(current)}` };
}

function ensureToastStack() {
  let stack = $("#toastStack");
  if (stack) return stack;

  stack = document.createElement("div");
  stack.id = "toastStack";
  stack.className = "toastStack";
  stack.setAttribute("aria-live", "polite");
  stack.setAttribute("aria-atomic", "false");
  document.body.append(stack);
  return stack;
}

function showToast(notification) {
  const stack = ensureToastStack();
  const toast = document.createElement("div");
  toast.className = `toast ${notification.type}`;

  const copy = document.createElement("div");
  const title = document.createElement("strong");
  const text = document.createElement("span");
  title.textContent = notification.title;
  text.textContent = notification.text;
  copy.append(title, text);

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.setAttribute("aria-label", "Dismiss notification");
  closeButton.textContent = "x";
  closeButton.addEventListener("click", () => toast.remove());

  toast.append(copy, closeButton);
  stack.prepend(toast);
  playAlertSound(notification.type);

  setTimeout(() => {
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 220);
  }, 9000);
}

function favoriteTeam(snapshot) {
  return snapshot?.odds?.consensus?.[0]?.team || null;
}

function showFavoriteNotification(previous, current) {
  const prev = favoriteTeam(previous);
  const next = favoriteTeam(current);
  // Only alert on an actual change between two known snapshots (never on first load).
  if (!prev || !next || prev === next) return;

  const notification = { type: "favoriteChange", title: "New favorite", text: `We have a new favorite - ${next}` };
  showToast(notification);
  sendDesktopNotification(notification);
}

function showMatchNotifications(previousMatches = [], currentMatches = []) {
  if (!previousMatches.length) return;

  const previousByKey = matchMap(previousMatches);
  currentMatches.forEach((match) => {
    const notification = scoreNotification(previousByKey[matchKey(match)], match);
    if (notification) {
      showToast(notification);
      sendDesktopNotification(notification);
    }
  });
}

function markBestPerformance(summary) {
  const pmNode = $("#pmPerformanceCard");
  const ksNode = $("#ksPerformanceCard");
  if (!pmNode || !ksNode) return;

  pmNode.classList.remove("bestPerformance");
  ksNode.classList.remove("bestPerformance");

  const pmAccuracy = accuracyValue(summary.polymarketHits, summary.polymarketMisses);
  const ksAccuracy = accuracyValue(summary.kalshiHits, summary.kalshiMisses);
  if (pmAccuracy == null || ksAccuracy == null || pmAccuracy === ksAccuracy) return;

  (pmAccuracy > ksAccuracy ? pmNode : ksNode).classList.add("bestPerformance");
}

async function loadSnapshot() {
  if (state.loading) return;
  state.loading = true;
  setText("#refreshStamp", "Refreshing...");
  try {
    const response = await fetch("/api/snapshot");
    if (!response.ok) throw new Error(`Snapshot failed: ${response.status}`);
    const previousSnapshot = state.snapshot;
    const previousMatches = previousSnapshot?.matches || [];
    const nextSnapshot = await response.json();
    state.snapshot = nextSnapshot;
    render();
    showMatchNotifications(previousMatches, nextSnapshot.matches);
    showFavoriteNotification(previousSnapshot, nextSnapshot);
  } finally {
    state.loading = false;
  }
}

function renderMetrics(data) {
  const summary = data.summary;
  const pm = data.leaders.polymarket;
  const ks = data.leaders.kalshi;

  setText("#pmLeader", pm?.team || "n/a");
  setText("#pmLeaderPrice", fmtPct(pm?.midPct));
  setText("#ksLeader", ks?.team || "n/a");
  setText("#ksLeaderPrice", fmtPct(ks?.midPct));
  setText("#completedCount", String(summary.completed));
  setText("#liveCount", `${summary.live} live, ${summary.draws} draws`);
  setText("#hitRate", accuracy(summary.polymarketHits + summary.kalshiHits, summary.polymarketMisses + summary.kalshiMisses));

  setText("#pmAccuracy", accuracy(summary.polymarketHits, summary.polymarketMisses));
  setText("#pmRecord", `${summary.polymarketHits}-${summary.polymarketMisses}`);
  setText("#ksAccuracy", accuracy(summary.kalshiHits, summary.kalshiMisses));
  setText("#ksRecord", `${summary.kalshiHits}-${summary.kalshiMisses}`);
  markBestPerformance(summary);

  const stamp = new Date(data.generatedAt);
  const mode = {
    baselineCsv: "outright baseline",
    mixedMarkets: "live + baseline outright odds",
    liveMarkets: "live outright odds",
  }[data.predictionMode] || "outright odds";
  const matchMode = data.sources?.matchMarketBaseline ? " + match markets" : "";
  setText("#refreshStamp", `${stamp.toLocaleString()} · ${mode}${matchMode}${data.cached ? " · cached" : ""}`);
}

function renderProjections(data) {
  const pm = data.projections.polymarket;
  const ks = data.projections.kalshi;
  const thirdLine = (p) =>
    p.thirdPlace ? `${p.thirdPlace}${p.fourthPlace ? ` (vs. ${p.fourthPlace})` : ""}` : "--";
  setText("#pmRunnerUp", `2. ${pm.runnerUp || "--"}`);
  setText("#pmThird", `3. ${thirdLine(pm)}`);
  setText("#ksRunnerUp", `2. ${ks.runnerUp || "--"}`);
  setText("#ksThird", `3. ${thirdLine(ks)}`);
}

function isUpset(match) {
  if (!match.status.completed || !match.winner || match.winner === "Draw") return false;
  return match.prediction.polymarketResult === "miss" || match.prediction.kalshiResult === "miss";
}

function matchVisible(match) {
  if (state.team && match.home?.team !== state.team && match.away?.team !== state.team) return false;
  if (state.group && match.group !== state.group) return false;
  if (state.date && localDateKey(match.date) !== state.date) return false;
  if (state.filter === "today") return isToday(match.date);
  if (state.filter === "live") return match.status.state === "in";
  if (state.filter === "completed") return match.status.completed;
  if (state.filter === "upset") return isUpset(match);
  return true;
}

function renderMatches(data) {
  const node = $("#matchList");
  const matches = data.matches.filter(matchVisible);
  if (!matches.length) {
    node.innerHTML = `<div class="empty">No matches in this view.</div>`;
    return;
  }

  node.innerHTML = matches.map(matchCardHtml).join("");
}

function matchCardHtml(match) {
      const isPending = !match.status.completed && match.status.state !== "in";
      const isLive = match.status.state === "in";
      const statusClass = isLive ? "live" : isPending ? "pending" : "";
      const matchClass = isLive ? "match liveMatch" : isPending ? "match pending" : "match";
      const pmResult = match.prediction.polymarketResult;
      const ksResult = match.prediction.kalshiResult;
      return `
        <article class="${matchClass}">
          <div class="matchTop">
            <span class="matchMeta">
              <span>${fmtDate(match.date)}</span>
              ${matchLocation(match)}
            </span>
            <span class="tag ${statusClass}">${escapeHtml(match.status.shortDetail || match.status.detail || "Scheduled")}</span>
          </div>
          <div class="teamLine">
            <span class="teamName">${escapeHtml(match.home.displayName || match.home.team)}</span>
            <span class="score">${fmtScore(match.home.score)}</span>
          </div>
          <div class="teamLine">
            <span class="teamName">${escapeHtml(match.away.displayName || match.away.team)}</span>
            <span class="score">${fmtScore(match.away.score)}</span>
          </div>
          ${predictionGroup(
            "PM locked",
            match.prediction.polymarketPick,
            match.prediction.polymarketPickPct,
            match.prediction.polymarketSource,
            pmResult,
            currentMarketLine(
              "PM now",
              match.prediction.polymarketCurrentPick,
              match.prediction.polymarketCurrentPickPct,
              match.prediction.polymarketCurrentSource,
              match.prediction.polymarketPick,
              match.prediction.polymarketCurrentVolume,
              match.prediction.polymarketCurrentTotal
            ),
            match.prediction.polymarketPickVolume,
            match.prediction.polymarketPickTotal
          )}
          ${predictionGroup(
            "Kalshi locked",
            match.prediction.kalshiPick,
            match.prediction.kalshiPickPct,
            match.prediction.kalshiSource,
            ksResult,
            currentMarketLine(
              "Kalshi now",
              match.prediction.kalshiCurrentPick,
              match.prediction.kalshiCurrentPickPct,
              match.prediction.kalshiCurrentSource,
              match.prediction.kalshiPick,
              match.prediction.kalshiCurrentVolume,
              match.prediction.kalshiCurrentTotal
            ),
            match.prediction.kalshiPickVolume,
            match.prediction.kalshiPickTotal
          )}
        </article>
      `;
}

const BLUE_GROUPS = new Set(["A", "C", "E", "G", "I", "K"]);
const QUALIFYING_THIRD_PLACES = 8;

function groupLetterOf(value) {
  return String(value || "")
    .trim()
    .replace(/^Group\s+/i, "")
    .slice(0, 1)
    .toUpperCase();
}

function teamRowClass(group) {
  return BLUE_GROUPS.has(groupLetterOf(group)) ? "groupBlue" : "";
}

function standingValue(value) {
  return value == null ? -Infinity : Number(value);
}

function hasPoints(row) {
  return Number(row.points || 0) > 0;
}

function teamKey(row) {
  return `${row.group || ""}::${row.team || row.displayName || ""}`;
}

function compareStandingRows(a, b) {
  const pointsCompare = standingValue(b.points) - standingValue(a.points);
  if (pointsCompare) return pointsCompare;

  const goalDiffCompare = standingValue(b.gd) - standingValue(a.gd);
  if (goalDiffCompare) return goalDiffCompare;

  const goalsForCompare = standingValue(b.gf) - standingValue(a.gf);
  if (goalsForCompare) return goalsForCompare;

  return 0;
}

function thirdPlaceQualifierKeys(rows) {
  const byGroup = new Map();
  rows.forEach((row) => {
    const group = row.group || "";
    if (!byGroup.has(group)) byGroup.set(group, []);
    byGroup.get(group).push(row);
  });

  const thirdPlaceRows = Array.from(byGroup.values())
    .map((groupRows) => groupRows[2])
    .filter(Boolean);

  thirdPlaceRows.sort((a, b) => {
    const standingsCompare = compareStandingRows(a, b);
    if (standingsCompare) return standingsCompare;

    const consensusCompare = standingValue(b.consensusPct) - standingValue(a.consensusPct);
    if (consensusCompare) return consensusCompare;

    return String(a.displayName).localeCompare(String(b.displayName));
  });

  return new Set(thirdPlaceRows.slice(0, QUALIFYING_THIRD_PLACES).map(teamKey));
}

function standingsComparator(groupsWithPoints) {
  return (a, b) => {
    const groupCompare = String(a.group).localeCompare(String(b.group));
    if (groupCompare) return groupCompare;
    if (!groupsWithPoints.has(a.group)) {
      const consensusCompare = standingValue(b.consensusPct) - standingValue(a.consensusPct);
      if (consensusCompare) return consensusCompare;
    }
    const standingsCompare = compareStandingRows(a, b);
    if (standingsCompare) return standingsCompare;
    // Teams dead-even on points/GD/goals: break by market consensus, not alphabetically.
    const consensusCompare = standingValue(b.consensusPct) - standingValue(a.consensusPct);
    if (consensusCompare) return consensusCompare;
    return String(a.displayName).localeCompare(String(b.displayName));
  };
}

// Qualification dot per team: green = top 2 in group, orange = qualifying 3rd,
// none if not qualifying, gray when the group has no games yet (outright order).
function qualificationDots(teams) {
  const groupsWithPoints = new Set(teams.filter(hasPoints).map((team) => team.group));
  const sorted = [...teams].sort(standingsComparator(groupsWithPoints));
  const thirdQualifiers = thirdPlaceQualifierKeys(sorted);
  const dots = new Map();
  let currentGroup = null;
  let position = 0;
  for (const row of sorted) {
    if (row.group !== currentGroup) {
      currentGroup = row.group;
      position = 1;
    } else {
      position += 1;
    }
    const hasGames = groupsWithPoints.has(row.group);
    let color = "";
    if (position <= 2) color = hasGames ? "green" : "gray";
    else if (position === 3 && thirdQualifiers.has(teamKey(row))) color = hasGames ? "orange" : "gray";
    dots.set(teamKey(row), color);
  }
  return dots;
}

function renderTeams(data) {
  if (state.teamsView === "overall") {
    renderTeamsOverall(data);
    return;
  }
  renderTeamsGroup(data);
}

// When the Matches panel is scoped to Today or a specific date, return the set
// of teams playing that day so the Teams section can scope to them too. Knockout
// teams keep their original group (data.teams always carries it). Returns null
// when no day filter is active (no scoping).
function matchdayTeams(data) {
  let dateKey = null;
  if (state.date) dateKey = state.date;
  else if (state.filter === "today") dateKey = localDateKey(new Date());
  if (!dateKey) return null;
  const teams = new Set();
  (data.matches || []).forEach((match) => {
    if (localDateKey(match.date) !== dateKey) return;
    if (match.home?.team) teams.add(match.home.team);
    if (match.away?.team) teams.add(match.away.team);
  });
  return teams;
}

// The groups (whole groups) that have a team playing on the active day. Used by
// the By-group view to show full groups, not just the playing teams.
function matchdayGroups(data) {
  const teams = matchdayTeams(data);
  if (!teams) return null;
  const groups = new Set();
  (data.teams || []).forEach((team) => {
    if (teams.has(team.team)) groups.add(team.group);
  });
  return groups;
}

function renderTeamsGroup(data) {
  const query = state.search.trim().toLowerCase();
  // By-group view shows whole groups that have a team playing that day.
  const dayGroups = matchdayGroups(data);
  const filteredRows = [...data.teams].filter(
    (team) =>
      (!state.team || team.team === state.team) &&
      (!state.group || groupLetterOf(team.group) === state.group) &&
      (!dayGroups || dayGroups.has(team.group)) &&
      (!query || team.displayName.toLowerCase().includes(query) || team.team.toLowerCase().includes(query))
  );
  const groupsWithPoints = new Set(filteredRows.filter(hasPoints).map((team) => team.group));
  const rows = filteredRows.sort(standingsComparator(groupsWithPoints));
  // When the table is filtered to a subset of groups, stripe consecutive group
  // blocks light-grey / white by their order instead of the default A/C/E tint.
  const filtersActive = Boolean(state.team || state.group || dayGroups || query);
  const groupOrder = new Map();
  rows.forEach((row) => {
    if (!groupOrder.has(row.group)) groupOrder.set(row.group, groupOrder.size);
  });
  const dots = qualificationDots(data.teams);
  const liveTeams = new Set();
  (data.matches || []).forEach((match) => {
    if (match.status?.state === "in") {
      if (match.home?.team) liveTeams.add(match.home.team);
      if (match.away?.team) liveTeams.add(match.away.team);
    }
  });

  const body = rows
    .map((row) => {
      const dotColor = dots.get(teamKey(row));
      const dot = `<span class="qualDot qual-${dotColor || "none"}" aria-hidden="true"></span>`;
      const live = liveTeams.has(row.team) ? `<span class="liveBadge">LIVE</span>` : "";
      const rowClass = filtersActive
        ? (groupOrder.get(row.group) % 2 === 0 ? "groupStripe" : "")
        : teamRowClass(row.group);
      return `
        <tr class="${rowClass}">
          <td>
            <span class="teamCell">
              ${dot}
              ${row.logo ? `<img class="logo" src="${escapeHtml(row.logo)}" alt="" />` : ""}
              ${escapeHtml(row.displayName)}
              ${live}
            </span>
          </td>
          <td>${escapeHtml(row.group || "")}</td>
          <td class="num">${row.points ?? "-"}</td>
          <td class="num">${row.gd ?? "-"}</td>
          <td class="num">${fmtPct(row.polymarketPct)}</td>
          <td class="num">${fmtPct(row.kalshiPct)}</td>
          <td class="num">${fmtPct(row.consensusPct)}</td>
          <td class="num">${row.polymarketRank ?? "-"}</td>
          <td class="num">${row.kalshiRank ?? "-"}</td>
        </tr>
      `;
    })
    .join("");
  $("#teamsTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Team</th>
          <th>Group</th>
          <th class="num">Pts</th>
          <th class="num">GD</th>
          <th class="num">PM %</th>
          <th class="num">Kalshi %</th>
          <th class="num">Avg %</th>
          <th class="num">PM Rank</th>
          <th class="num">K Rank</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

// Per-team record across ALL completed matches (group + knockout): win=3, draw=1.
function teamGames(data) {
  const byTeam = new Map();
  const ensure = (team, displayName, logo, group) => {
    let rec = byTeam.get(team);
    if (!rec) {
      rec = { team, displayName: displayName || team, logo: logo || "", group: group || "", w: 0, d: 0, l: 0, gf: 0, ga: 0, gd: 0, pts: 0, history: [] };
      byTeam.set(team, rec);
    }
    return rec;
  };
  (data.teams || []).forEach((t) => ensure(t.team, t.displayName, t.logo, t.group));
  const matches = [...(data.matches || [])].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  matches.forEach((match) => {
    const completed = !!(match.status && match.status.completed);
    [[match.home, match.away], [match.away, match.home]].forEach(([side, opp]) => {
      if (!side || !side.team) return;
      // Only real, qualified teams are seeded; skip knockout placeholders ("Group A Winner", etc.).
      const rec = byTeam.get(side.team);
      if (!rec) return;
      let result = "scheduled";
      if (completed) {
        if (side.winner) result = "win";
        else if (opp.winner) result = "loss";
        else result = "draw";
        if (result === "win") { rec.w += 1; rec.pts += 3; }
        else if (result === "draw") { rec.d += 1; rec.pts += 1; }
        else rec.l += 1;
        if (side.score != null) rec.gf += Number(side.score);
        if (opp.score != null) rec.ga += Number(opp.score);
      }
      rec.history.push({
        result,
        oppName: opp.displayName || opp.team || "",
        oppLogo: opp.logo || "",
        matchId: matchKey(match),
      });
    });
  });
  const list = [...byTeam.values()];
  list.forEach((rec) => { rec.gd = rec.gf - rec.ga; });
  return list;
}

// A team is definitively out only when it can no longer finish in the top 3 of
// its group — 4th place never qualifies, not even as a best-third. (ESPN's
// "Eliminated" note just marks the current last place, which is not the same.)
function eliminatedTeams(data) {
  const teams = data.teams || [];
  const remaining = {};
  teams.forEach((t) => { remaining[t.team] = 0; });
  (data.matches || []).forEach((match) => {
    if (!match.group) return; // group-stage games only
    if (match.status && match.status.completed) return;
    [match.home, match.away].forEach((side) => {
      if (side && side.team && remaining[side.team] != null) remaining[side.team] += 1;
    });
  });
  const byGroup = {};
  teams.forEach((t) => { (byGroup[t.group] = byGroup[t.group] || []).push(t); });
  const out = new Set();
  Object.values(byGroup).forEach((group) => {
    group.forEach((team) => {
      const maxPoints = (team.points || 0) + 3 * (remaining[team.team] || 0);
      // Rivals already guaranteed to finish above this team (more points than it can ever reach).
      const lockedAbove = group.filter((o) => o.team !== team.team && (o.points || 0) > maxPoints).length;
      if (lockedAbove >= 3) out.add(team.team);
    });
  });
  return out;
}

function renderTeamsOverall(data) {
  const query = state.search.trim().toLowerCase();
  // Rank every team globally first, so the # stays fixed even when filtered.
  const ranked = teamGames(data).sort(
    (a, b) => b.pts - a.pts || b.gd - a.gd || b.gf - a.gf || a.displayName.localeCompare(b.displayName)
  );
  const globalRank = new Map(ranked.map((rec, i) => [rec.team, i + 1]));
  const dayTeams = matchdayTeams(data);
  const rows = ranked.filter(
    (rec) =>
      (!state.team || rec.team === state.team) &&
      (!state.group || groupLetterOf(rec.group) === state.group) &&
      (!dayTeams || dayTeams.has(rec.team)) &&
      (!query || rec.displayName.toLowerCase().includes(query) || rec.team.toLowerCase().includes(query))
  );
  const liveTeams = new Set();
  (data.matches || []).forEach((match) => {
    if (match.status?.state === "in") {
      if (match.home?.team) liveTeams.add(match.home.team);
      if (match.away?.team) liveTeams.add(match.away.team);
    }
  });
  const eliminated = eliminatedTeams(data);
  const consensus = new Map((data.teams || []).map((t) => [t.team, t.consensusPct]));

  const body = rows
    .map((rec) => {
      const live = liveTeams.has(rec.team) ? `<span class="liveBadge">LIVE</span>` : "";
      const history = rec.history
        .map((h) => {
          const flag = h.oppLogo
            ? `<img class="histFlag" src="${escapeHtml(h.oppLogo)}" alt="" />`
            : `<span class="histFlag histFlagEmpty"></span>`;
          const label = h.result === "scheduled" ? `vs ${h.oppName}` : `${RESULT_WORD[h.result]} vs ${h.oppName}`;
          return `<button class="histDot hist-${h.result}" type="button" data-match="${escapeHtml(h.matchId)}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${flag}<span class="histPip"></span></button>`;
        })
        .join("");
      const gd = rec.gd > 0 ? `+${rec.gd}` : `${rec.gd}`;
      return `
        <tr class="${eliminated.has(rec.team) ? "outRow" : ""}">
          <td class="num rankCol">${globalRank.get(rec.team)}</td>
          <td>
            <span class="teamCell">
              ${rec.logo ? `<img class="logo" src="${escapeHtml(rec.logo)}" alt="" />` : ""}
              ${escapeHtml(rec.displayName)}
              ${live}
            </span>
          </td>
          <td>${escapeHtml(rec.group || "")}</td>
          <td class="num">${rec.pts}</td>
          <td class="num">${rec.w}</td>
          <td class="num">${rec.d}</td>
          <td class="num">${rec.l}</td>
          <td class="num">${rec.gf}</td>
          <td class="num">${gd}</td>
          <td class="num">${fmtPct(consensus.get(rec.team))}</td>
          <td><div class="histStrip">${history || "<span class='histNone'>—</span>"}</div></td>
        </tr>
      `;
    })
    .join("");
  $("#teamsTable").innerHTML = `
    <table class="overallTable">
      <thead>
        <tr>
          <th class="rankCol">#</th>
          <th>Team</th>
          <th>Group</th>
          <th class="num">Pts</th>
          <th class="num">W</th>
          <th class="num">D</th>
          <th class="num">L</th>
          <th class="num">GF</th>
          <th class="num">GD</th>
          <th class="num">Odds</th>
          <th>History</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

const RESULT_WORD = { win: "Won", draw: "Drew", loss: "Lost", scheduled: "Scheduled" };

function renderOdds(data) {
  const globalMax = data.odds.consensus[0]?.pct || 1;
  let scoped = data.odds.consensus;
  if (state.team) {
    scoped = scoped.filter((row) => row.team === state.team);
  } else if (state.group) {
    const groupTeams = new Set(
      (data.teams || []).filter((t) => groupLetterOf(t.group) === state.group).map((t) => t.team)
    );
    scoped = scoped.filter((row) => groupTeams.has(row.team));
  }
  const top = scoped.slice(0, 18);
  if (!top.length) {
    $("#oddsBars").innerHTML = `<div class="empty">No consensus odds in this view.</div>`;
    return;
  }
  $("#oddsBars").innerHTML = top
    .map((row) => {
      const width = Math.max(2, (row.pct / globalMax) * 100);
      return `
        <div class="barRow">
          <span class="barLabel">${escapeHtml(row.team)}</span>
          <span class="barTrack"><span class="barFill" style="width:${width}%"></span></span>
          <span class="barValue">${fmtPct(row.pct)}</span>
        </div>
      `;
    })
    .join("");
}

function teamFocusLookup(data) {
  // Map of lowercased display name / normalized name -> normalized team.
  const map = new Map();
  (data.teams || []).forEach((row) => {
    if (!row.team) return;
    map.set(row.team.toLowerCase(), row.team);
    if (row.displayName) map.set(row.displayName.toLowerCase(), row.team);
  });
  return map;
}

function renderTeamFocusOptions(data) {
  const datalist = $("#teamFocusOptions");
  if (!datalist) return;
  const labels = Array.from(
    new Set((data.teams || []).map((row) => row.displayName || row.team).filter(Boolean))
  ).sort((a, b) => a.localeCompare(b));
  datalist.innerHTML = labels.map((label) => `<option value="${escapeHtml(label)}"></option>`).join("");
}

function applyTeamFocus(value) {
  const data = state.snapshot;
  const raw = String(value || "").trim();
  const lookup = data ? teamFocusLookup(data) : new Map();
  // Narrow only on an exact match; partial typing keeps the full view.
  state.team = raw ? lookup.get(raw.toLowerCase()) || "" : "";

  // Team and group focus are mutually exclusive.
  if (state.team) resetGroupFocus();

  const clearButton = $("#teamFocusClear");
  if (clearButton) clearButton.hidden = !state.team;

  if (!data) return;
  renderMatches(data);
  renderTeams(data);
  renderOdds(data);
}

function groupFocusLabels(data) {
  return Array.from(new Set((data.teams || []).map((row) => row.group).filter(Boolean))).sort((a, b) =>
    a.localeCompare(b)
  );
}

function groupFocusLookup(data) {
  // Map of lowercased "Group A" / "A" -> group letter.
  const map = new Map();
  groupFocusLabels(data).forEach((label) => {
    const letter = groupLetterOf(label);
    map.set(label.toLowerCase(), letter);
    map.set(letter.toLowerCase(), letter);
  });
  return map;
}

function renderGroupFocusOptions(data) {
  const datalist = $("#groupFocusOptions");
  if (!datalist) return;
  datalist.innerHTML = groupFocusLabels(data)
    .map((label) => `<option value="${escapeHtml(label)}"></option>`)
    .join("");
}

function resetTeamFocus() {
  state.team = "";
  const input = $("#teamFocus");
  if (input) input.value = "";
  const clearButton = $("#teamFocusClear");
  if (clearButton) clearButton.hidden = true;
}

function resetGroupFocus() {
  state.group = "";
  const input = $("#groupFocus");
  if (input) input.value = "";
  const clearButton = $("#groupFocusClear");
  if (clearButton) clearButton.hidden = true;
}

function applyGroupFocus(value) {
  const data = state.snapshot;
  const raw = String(value || "").trim();
  const lookup = data ? groupFocusLookup(data) : new Map();
  // Narrow only when the value resolves to a known group; partial typing keeps the full view.
  state.group = raw ? lookup.get(raw.toLowerCase()) || "" : "";

  // Team and group focus are mutually exclusive.
  if (state.group) resetTeamFocus();

  const clearButton = $("#groupFocusClear");
  if (clearButton) clearButton.hidden = !state.group;

  if (!data) return;
  renderMatches(data);
  renderTeams(data);
  renderOdds(data);
}

function localDateKey(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function fmtDateKeyLabel(key) {
  const [y, m, d] = key.split("-").map(Number);
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric" }).format(new Date(y, m - 1, d));
}

function gameDateSet(data) {
  const set = new Set();
  (data?.matches || []).forEach((m) => {
    const k = localDateKey(m.date);
    if (k) set.add(k);
  });
  return set;
}

function renderCal(dates) {
  if (!state.calYM) return;
  const { y, m } = state.calYM;
  setText("#calTitle", new Intl.DateTimeFormat("en", { month: "long", year: "numeric" }).format(new Date(y, m, 1)));
  const dow = $(".dateCalDow");
  if (dow) dow.innerHTML = ["S", "M", "T", "W", "T", "F", "S"].map((d) => `<span>${d}</span>`).join("");

  const firstDay = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const todayKey = localDateKey(new Date());
  const cells = [];
  for (let i = 0; i < firstDay; i++) cells.push(`<div class="calDay empty"></div>`);
  for (let d = 1; d <= daysInMonth; d++) {
    const key = `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const has = dates.has(key);
    const classes = ["calDay", has ? "hasGames" : "", key === todayKey ? "today" : "", state.date === key ? "selected" : ""]
      .filter(Boolean)
      .join(" ");
    cells.push(`<div class="${classes}" data-date="${has ? key : ""}">${d}</div>`);
  }
  $("#calGrid").innerHTML = cells.join("");

  const months = [...new Set([...dates].map((k) => k.slice(0, 7)))].sort();
  const curYM = `${y}-${String(m + 1).padStart(2, "0")}`;
  $("#calPrev").disabled = !months.length || curYM <= months[0];
  $("#calNext").disabled = !months.length || curYM >= months[months.length - 1];
}

function renderDateControl(data) {
  const dates = gameDateSet(data);
  setText("#dateButton", state.date ? fmtDateKeyLabel(state.date) : "All dates");
  $("#dateButton").classList.toggle("dateActive", Boolean(state.date));

  if (!state.calYM) {
    const months = [...new Set([...dates].map((k) => k.slice(0, 7)))].sort();
    let ym = state.date ? state.date.slice(0, 7) : localDateKey(new Date()).slice(0, 7);
    if (!state.date && months.length) {
      if (ym < months[0]) ym = months[0];
      else if (ym > months[months.length - 1]) ym = months[months.length - 1];
    }
    const [yy, mm] = ym.split("-").map(Number);
    state.calYM = { y: yy, m: mm - 1 };
  }
  if (!$("#dateCal").hidden) renderCal(dates);
}

function shiftMonth(delta) {
  const dates = gameDateSet(state.snapshot);
  const months = [...new Set([...dates].map((k) => k.slice(0, 7)))].sort();
  let { y, m } = state.calYM;
  m += delta;
  if (m < 0) {
    m = 11;
    y -= 1;
  } else if (m > 11) {
    m = 0;
    y += 1;
  }
  const curYM = `${y}-${String(m + 1).padStart(2, "0")}`;
  if (months.length && (curYM < months[0] || curYM > months[months.length - 1])) return;
  state.calYM = { y, m };
  renderCal(dates);
}

function openCal() {
  const cal = $("#dateCal");
  if (!cal) return;
  cal.hidden = false;
  $("#dateButton").setAttribute("aria-expanded", "true");
  renderCal(gameDateSet(state.snapshot));
}

function closeCal() {
  const cal = $("#dateCal");
  if (cal) cal.hidden = true;
  $("#dateButton").setAttribute("aria-expanded", "false");
}

function render() {
  const data = state.snapshot;
  if (!data) return;
  renderMetrics(data);
  renderProjections(data);
  renderTeamFocusOptions(data);
  renderGroupFocusOptions(data);
  renderDateControl(data);
  renderMatches(data);
  renderTeams(data);
  renderOdds(data);
}

$("#refreshButton").addEventListener("click", () => {
  loadSnapshot().catch((error) => {
    console.error(error);
    setText("#refreshStamp", "Refresh failed");
  });
  loadBracketStatusQuietly();
});

$("#bracketButton").addEventListener("click", () => {
  window.open(`/bracket?ts=${Date.now()}`, "_blank", "noopener");
  renderBracketStatus({ changed: false });
  setTimeout(loadBracketStatusQuietly, 2500);
});

$("#alertsButton").addEventListener("click", () => {
  toggleDesktopAlerts().catch((error) => {
    console.error(error);
    showToast({ type: "finalScore", title: "Desktop alerts failed", text: "The browser could not update notification settings." });
  });
});

$$(".segment[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    $$(".segment[data-filter]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    // "Today" and the date picker are both day-selectors — clear the date when Today wins.
    if (state.filter === "today" && state.date) {
      state.date = "";
      renderDateControl(state.snapshot);
    }
    renderMatches(state.snapshot);
    renderTeams(state.snapshot); // Teams scope follows the Today/date filter
  });
});

$("#teamSearch").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderTeams(state.snapshot);
});

const teamFocusInput = $("#teamFocus");
["input", "change"].forEach((evt) =>
  teamFocusInput.addEventListener(evt, (event) => applyTeamFocus(event.target.value))
);
$("#teamFocusClear").addEventListener("click", () => {
  teamFocusInput.value = "";
  applyTeamFocus("");
  teamFocusInput.focus();
});

const groupFocusInput = $("#groupFocus");
["input", "change"].forEach((evt) =>
  groupFocusInput.addEventListener(evt, (event) => applyGroupFocus(event.target.value))
);
$("#groupFocusClear").addEventListener("click", () => {
  groupFocusInput.value = "";
  applyGroupFocus("");
  groupFocusInput.focus();
});

$("#dateButton").addEventListener("click", (event) => {
  event.stopPropagation();
  if ($("#dateCal").hidden) openCal();
  else closeCal();
});
$("#calPrev").addEventListener("click", () => shiftMonth(-1));
$("#calNext").addEventListener("click", () => shiftMonth(1));
$("#calGrid").addEventListener("click", (event) => {
  const cell = event.target.closest(".calDay");
  const key = cell && cell.dataset.date;
  if (!key) return;
  state.date = key === state.date ? "" : key;
  // Picking a date overrides the "Today" segment so the two don't conflict.
  if (state.date && state.filter === "today") {
    state.filter = "all";
    $$(".segment[data-filter]").forEach((item) => item.classList.toggle("active", item.dataset.filter === "all"));
  }
  closeCal();
  renderDateControl(state.snapshot);
  renderMatches(state.snapshot);
  renderTeams(state.snapshot);
});
$("#dateClear").addEventListener("click", () => {
  state.date = "";
  closeCal();
  renderDateControl(state.snapshot);
  renderMatches(state.snapshot);
  renderTeams(state.snapshot);
});
document.addEventListener("click", (event) => {
  if (!$("#dateCal").hidden && !event.target.closest(".dateFocus")) closeCal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#dateCal").hidden) closeCal();
});

const messageModal = $("#messageModal");

function openMessageModal() {
  if (!messageModal) return;
  messageModal.hidden = false;
  $("#messageText").focus();
}

function closeMessageModal() {
  if (messageModal) messageModal.hidden = true;
}

$("#messageButton").addEventListener("click", openMessageModal);
$("#messageClose").addEventListener("click", closeMessageModal);
$("#messageCancel").addEventListener("click", closeMessageModal);
messageModal.addEventListener("click", (event) => {
  if (event.target === messageModal) closeMessageModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && messageModal && !messageModal.hidden) closeMessageModal();
});

// Match-detail modal — reuses the match card; opened from history dots.
const matchModal = $("#matchModal");

function openMatchModal(matchId) {
  const data = state.snapshot;
  if (!matchModal || !data) return;
  const match = (data.matches || []).find((m) => matchKey(m) === matchId);
  if (!match) return;
  $("#matchModalBody").innerHTML = matchCardHtml(match);
  matchModal.hidden = false;
}

function closeMatchModal() {
  if (matchModal) matchModal.hidden = true;
}

if (matchModal) {
  $("#matchClose").addEventListener("click", closeMatchModal);
  matchModal.addEventListener("click", (event) => {
    if (event.target === matchModal) closeMatchModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !matchModal.hidden) closeMatchModal();
  });
}

// History dots in the overall teams table open that match's card.
$("#teamsTable").addEventListener("click", (event) => {
  const dot = event.target.closest(".histDot");
  if (!dot) return;
  openMatchModal(dot.dataset.match);
});

// Teams view switcher (By group / Overall).
$$("[data-teamsview]").forEach((button) => {
  button.addEventListener("click", () => {
    if (state.teamsView === button.dataset.teamsview) return;
    $$("[data-teamsview]").forEach((item) => item.classList.toggle("active", item === button));
    state.teamsView = button.dataset.teamsview;
    if (state.snapshot) renderTeams(state.snapshot);
  });
});

$("#messageForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#messageText").value.trim();
  const email = $("#messageEmail").value.trim();
  if (!message) return;

  // Web3Forms free tier accepts client-side submissions only; the access key is
  // injected into the page by the server (kept out of the repo).
  const accessKey = document.body.dataset.web3formsKey;
  if (!accessKey) {
    showToast({ type: "liveScoreChange", title: "Messaging unavailable", text: "The message form isn't configured yet." });
    return;
  }

  const sendButton = $("#messageSend");
  sendButton.disabled = true;
  try {
    const payload = {
      access_key: accessKey,
      subject: "World Cup Tracker — new message",
      from_name: "World Cup Tracker",
      message,
    };
    if (email) {
      payload.replyto = email;
      payload.email = email;
    }
    const response = await fetch("https://api.web3forms.com/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.message || `Message failed: ${response.status}`);
    $("#messageText").value = "";
    $("#messageEmail").value = "";
    closeMessageModal();
    showToast({ type: "finalScore", title: "Message sent", text: "Thanks — your message is on its way." });
  } catch (error) {
    console.error(error);
    showToast({ type: "liveScoreChange", title: "Message not sent", text: "Something went wrong. Please try again later." });
  } finally {
    sendButton.disabled = false;
  }
});

window.addEventListener("focus", () => {
  loadDesktopAlertCapability().catch((error) => {
    console.error(error);
    renderAlertsStatus();
  });
});

loadDesktopAlertCapability().then(() => {
  if (desktopAlertsEnabled()) subscribePush();
});
loadSnapshot().catch((error) => {
  console.error(error);
  setText("#refreshStamp", "Load failed");
});
loadBracketStatusQuietly();

setInterval(() => {
  loadSnapshot().catch((error) => {
    console.error(error);
    setText("#refreshStamp", "Refresh failed");
  });
  loadBracketStatusQuietly();
}, 60_000);
