const state = {
  snapshot: null,
  filter: "all",
  search: "",
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

function accuracy(hits, misses) {
  const total = hits + misses;
  return total ? `${Math.round((hits / total) * 100)}%` : "n/a";
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

async function loadSnapshot() {
  setText("#refreshStamp", "Refreshing...");
  const response = await fetch("/api/snapshot");
  if (!response.ok) throw new Error(`Snapshot failed: ${response.status}`);
  state.snapshot = await response.json();
  render();
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

  const stamp = new Date(data.generatedAt);
  const mode = data.predictionMode === "baselineCsv" ? "baseline" : "live odds";
  setText("#refreshStamp", `${stamp.toLocaleString()} · ${mode}${data.cached ? " · cached" : ""}`);
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

function renderUpsets(data) {
  const upsets = data.matches.filter(isUpset).slice(0, 8);
  const node = $("#upsets");
  if (!upsets.length) {
    node.innerHTML = `<span class="tag">No decisive upsets yet</span>`;
    return;
  }

  node.innerHTML = upsets
    .map((match) => {
      const score = `${fmtScore(match.home.score)}-${fmtScore(match.away.score)}`;
      return `<span class="pill">${match.winner} ${score}</span>`;
    })
    .join("");
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
      const statusClass = match.status.state === "in" ? "live" : "";
      const pmResult = match.prediction.polymarketResult;
      const ksResult = match.prediction.kalshiResult;
      return `
        <article class="match">
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
            <span>PM: ${match.prediction.polymarketPick || "n/a"}</span>
            <span class="tag ${pmResult}">${resultLabel(pmResult)}</span>
          </div>
          <div class="leanLine">
            <span>Kalshi: ${match.prediction.kalshiPick || "n/a"}</span>
            <span class="tag ${ksResult}">${resultLabel(ksResult)}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderTeams(data) {
  const query = state.search.trim().toLowerCase();
  const rows = [...data.teams]
    .filter((team) => !query || team.displayName.toLowerCase().includes(query) || team.team.toLowerCase().includes(query))
    .sort((a, b) => {
      const groupCompare = String(a.group).localeCompare(String(b.group));
      if (groupCompare) return groupCompare;
      return (a.groupRank || 99) - (b.groupRank || 99);
    });

  $("#teamRows").innerHTML = rows
    .map(
      (row) => `
        <tr>
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
  renderUpsets(data);
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
