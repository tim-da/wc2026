const state = {
  snapshot: null,
  filter: "all",
  search: "",
  loading: false,
};

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
  const parts = [`${label}: ${pick || "n/a"}`];
  if (pickPct != null) parts.push(fmtPct(pickPct));
  if (source) parts.push(sourceLabel(source));
  return parts.join(" · ");
}

function currentMarketLine(label, pick, pickPct, source) {
  if (!pick) return "";
  return `<div class="currentLine">${predictionText(label, pick, pickPct, source)}</div>`;
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
    state.snapshot = await response.json();
    render();
  } finally {
    state.loading = false;
  }
}

function renderMetrics(data) {
  const summary = data.summary;
  const pm = data.leaders.polymarket;
  const ks = data.leaders.kalshi;

  setText("#pmLeader", pm.team);
  setText("#pmLeaderPrice", fmtPct(pm.midPct));
  setText("#ksLeader", ks.team);
  setText("#ksLeaderPrice", fmtPct(ks.midPct));
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
      const statusClass = match.status.state === "in" ? "live" : isPending ? "pending" : "";
      const matchClass = isPending ? "match pending" : "match";
      const pmResult = match.prediction.polymarketResult;
      const ksResult = match.prediction.kalshiResult;
      return `
        <article class="${matchClass}">
          <div class="matchTop">
            <span>${fmtDate(match.date)}</span>
            <span class="tag ${statusClass}">${match.status.shortDetail || match.status.detail || "Scheduled"}</span>
          </div>
          <div class="teamLine">
            <span class="teamName">${match.home.displayName || match.home.team}</span>
            <span class="score">${fmtScore(match.home.score)}</span>
          </div>
          <div class="teamLine">
            <span class="teamName">${match.away.displayName || match.away.team}</span>
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
            match.prediction.polymarketCurrentSource
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
            match.prediction.kalshiCurrentSource
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
              ${row.logo ? `<img class="logo" src="${row.logo}" alt="" />` : ""}
              ${row.displayName}
            </span>
          </td>
          <td>${row.group || ""}</td>
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
          <span class="barLabel">${row.team}</span>
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

loadSnapshot().catch((error) => {
  console.error(error);
  setText("#refreshStamp", "Load failed");
});

setInterval(() => {
  loadSnapshot().catch((error) => {
    console.error(error);
    setText("#refreshStamp", "Refresh failed");
  });
}, 60_000);
