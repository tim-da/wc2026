const state = {
  snapshot: null,
  bracketStatus: null,
  desktopAlerts: false,
  macosNativeAlerts: false,
  filter: "all",
  search: "",
  loading: false,
};

const DESKTOP_ALERTS_KEY = "wc2026DesktopAlerts";
let alertAudioContext = null;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function fmtPct(value) {
  return value == null ? "n/a" : `${Number(value).toFixed(1)}%`;
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
    match: "match market",
    outright: "outright",
    unknown: "unknown",
  }[source] || source;
}

function predictionText(label, pick, pickPct, source) {
  const parts = [`${label}: ${escapeHtml(pick || "n/a")}`];
  if (pickPct != null) parts.push(fmtPct(pickPct));
  if (source) parts.push(escapeHtml(sourceLabel(source)));
  return parts.join(" · ");
}

function currentMarketLine(label, pick, pickPct, source, lockedPick) {
  if (!pick) return "";
  const changedClass = lockedPick && pick !== lockedPick ? " currentChanged" : "";
  return `<div class="currentLine${changedClass}">${predictionText(label, pick, pickPct, source)}</div>`;
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
  const tones = type === "liveScoreChange" ? [880, 1175] : [784, 1046];
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
  if (!city) return "";

  const label = `${location.flag ? `${location.flag} ` : ""}${city}`;
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

function scoreNotification(previous, current) {
  const currentScore = scorePair(current);
  if (!previous || !currentScore) return null;

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
    const previousMatches = state.snapshot?.matches || [];
    const nextSnapshot = await response.json();
    state.snapshot = nextSnapshot;
    render();
    showMatchNotifications(previousMatches, nextSnapshot.matches);
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
  const mode = data.predictionMode === "baselineCsv" ? "outright baseline" : "live outright odds";
  const matchMode = data.sources?.matchMarketBaseline ? " + match markets" : "";
  setText("#refreshStamp", `${stamp.toLocaleString()} · ${mode}${matchMode}${data.cached ? " · cached" : ""}`);
}

function renderProjections(data) {
  const pm = data.projections.polymarket;
  const ks = data.projections.kalshi;
  setText("#pmChampion", pm.champion);
  setText("#pmFinal", `${pm.finalists[0]} vs ${pm.finalists[1]}`);
  setText("#ksChampion", ks.champion);
  setText("#ksFinal", `${ks.finalists[0]} vs ${ks.finalists[1]}`);
}

function isUpset(match) {
  if (!match.status.completed || !match.winner || match.winner === "Draw") return false;
  return match.prediction.polymarketResult === "miss" || match.prediction.kalshiResult === "miss";
}

function matchVisible(match) {
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

  node.innerHTML = matches
    .map((match) => {
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
          <div class="leanLine">
            <span class="leanPick">${predictionText(
              "PM locked",
              match.prediction.polymarketPick,
              match.prediction.polymarketPickPct,
              match.prediction.polymarketSource
            )}</span>
            <span class="tag ${pmResult}">${resultLabel(pmResult)}</span>
          </div>
          ${currentMarketLine(
            "PM now",
            match.prediction.polymarketCurrentPick,
            match.prediction.polymarketCurrentPickPct,
            match.prediction.polymarketCurrentSource,
            match.prediction.polymarketPick
          )}
          <div class="leanLine">
            <span class="leanPick">${predictionText(
              "Kalshi locked",
              match.prediction.kalshiPick,
              match.prediction.kalshiPickPct,
              match.prediction.kalshiSource
            )}</span>
            <span class="tag ${ksResult}">${resultLabel(ksResult)}</span>
          </div>
          ${currentMarketLine(
            "Kalshi now",
            match.prediction.kalshiCurrentPick,
            match.prediction.kalshiCurrentPickPct,
            match.prediction.kalshiCurrentSource,
            match.prediction.kalshiPick
          )}
        </article>
      `;
    })
    .join("");
}

const BLUE_GROUPS = new Set(["A", "C", "E", "G", "I", "K"]);
const QUALIFYING_THIRD_PLACES = 8;

function teamRowClass(group) {
  const groupLetter = String(group || "")
    .trim()
    .replace(/^Group\s+/i, "")
    .slice(0, 1)
    .toUpperCase();
  return BLUE_GROUPS.has(groupLetter) ? "groupBlue" : "";
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

function renderTeams(data) {
  const query = state.search.trim().toLowerCase();
  const filteredRows = [...data.teams].filter(
    (team) => !query || team.displayName.toLowerCase().includes(query) || team.team.toLowerCase().includes(query)
  );
  const groupsWithPoints = new Set(filteredRows.filter(hasPoints).map((team) => team.group));
  const rows = filteredRows.sort((a, b) => {
      const groupCompare = String(a.group).localeCompare(String(b.group));
      if (groupCompare) return groupCompare;

      if (!groupsWithPoints.has(a.group)) {
        const consensusCompare = standingValue(b.consensusPct) - standingValue(a.consensusPct);
        if (consensusCompare) return consensusCompare;
      }

      const standingsCompare = compareStandingRows(a, b);
      if (standingsCompare) return standingsCompare;

      return String(a.displayName).localeCompare(String(b.displayName));
    });
  const thirdQualifiers = thirdPlaceQualifierKeys(rows);

  $("#teamRows").innerHTML = rows
    .map(
      (row) => `
        <tr class="${[teamRowClass(row.group), thirdQualifiers.has(teamKey(row)) ? "thirdQualified" : ""].filter(Boolean).join(" ")}">
          <td>
            <span class="teamCell">
              ${row.logo ? `<img class="logo" src="${escapeHtml(row.logo)}" alt="" />` : ""}
              ${escapeHtml(row.displayName)}
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
      `
    )
    .join("");
}

function renderOdds(data) {
  const top = data.odds.consensus.slice(0, 18);
  const max = top[0]?.pct || 1;
  $("#oddsBars").innerHTML = top
    .map((row) => {
      const width = Math.max(2, (row.pct / max) * 100);
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

function render() {
  const data = state.snapshot;
  if (!data) return;
  renderMetrics(data);
  renderProjections(data);
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

$$(".segment").forEach((button) => {
  button.addEventListener("click", () => {
    $$(".segment").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    renderMatches(state.snapshot);
  });
});

$("#teamSearch").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderTeams(state.snapshot);
});

window.addEventListener("focus", () => {
  loadDesktopAlertCapability().catch((error) => {
    console.error(error);
    renderAlertsStatus();
  });
});

loadDesktopAlertCapability();
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
