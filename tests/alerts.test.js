// Tests for the four pop-up alerts in static/app.js.
// app.js is a browser script (not a module), so we run it in a vm sandbox with
// minimal DOM/fetch stubs, then call its alert functions directly. After load we
// swap showToast / sendDesktopNotification for spies (they are top-level function
// declarations, i.e. global bindings, so internal callers see the replacements).
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const APP_PATH = path.join(__dirname, "..", "outputs", "world-cup-tracker", "static", "app.js");
const SRC = fs.readFileSync(APP_PATH, "utf8");

function loadApp() {
  const noop = () => {};
  const makeEl = () =>
    new Proxy(
      {},
      {
        get(target, prop) {
          if (prop === "classList") return { toggle: noop, add: noop, remove: noop, contains: () => false };
          if (["addEventListener", "removeEventListener", "append", "prepend", "remove", "setAttribute", "focus"].includes(prop)) return noop;
          if (prop === "style" || prop === "dataset") return {};
          return target[prop];
        },
        set(target, prop, value) {
          target[prop] = value;
          return true;
        },
      }
    );

  function Notification() {}
  Notification.permission = "default";
  Notification.requestPermission = async () => "default";

  const toasts = [];
  const desktop = [];

  const sandbox = {
    console: { log: noop, error: noop, warn: noop },
    setTimeout: noop,
    setInterval: noop,
    clearTimeout: noop,
    clearInterval: noop,
    fetch: () => Promise.reject(new Error("stub fetch")),
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    Notification,
    document: {
      querySelector: () => makeEl(),
      querySelectorAll: () => [],
      createElement: () => makeEl(),
      addEventListener: noop,
      removeEventListener: noop,
      body: makeEl(),
    },
  };
  sandbox.window = { addEventListener: noop, isSecureContext: true, Notification, focus: noop, open: noop };

  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: "app.js" });

  sandbox.showToast = (n) => toasts.push(n);
  sandbox.sendDesktopNotification = (n) => desktop.push(n);

  return { app: sandbox, toasts, desktop };
}

const { app, toasts, desktop } = loadApp();
const reset = () => {
  toasts.length = 0;
  desktop.length = 0;
};

// ---- Alert 1: kick-off ----
test("kick-off: fires on pre -> in with teams, group and city", () => {
  const prev = { status: { state: "pre", completed: false }, home: { score: 0 }, away: { score: 0 } };
  const cur = {
    status: { state: "in", completed: false },
    home: { team: "Saudi Arabia", displayName: "Saudi Arabia", score: 0 },
    away: { team: "Uruguay", displayName: "Uruguay", score: 0 },
    stageLabel: "Group H",
    location: { city: "Miami Gardens" },
  };
  const n = app.scoreNotification(prev, cur);
  assert.equal(n.type, "kickoff");
  assert.equal(n.title, "Kick-off");
  assert.ok(n.text.includes("Saudi Arabia vs Uruguay"));
  assert.ok(n.text.includes("Group H"));
  assert.ok(n.text.includes("Miami Gardens"));
});

test("kick-off: does not fire when already live, or with no previous", () => {
  const live = { status: { state: "in", completed: false }, home: { team: "A", score: 0 }, away: { team: "B", score: 0 } };
  assert.equal(app.scoreNotification(live, live), null); // already live, no score change
  assert.equal(app.scoreNotification(null, live), null); // first sight
});

// ---- Alert 2: score change (goal) ----
test("score change: fires on a goal while live, names the scorer", () => {
  const prev = { status: { state: "in", completed: false }, home: { score: 0 }, away: { score: 0 } };
  const cur = {
    status: { state: "in", completed: false },
    home: { team: "Saudi Arabia", displayName: "Saudi Arabia", score: 1 },
    away: { team: "Uruguay", displayName: "Uruguay", score: 0 },
  };
  const n = app.scoreNotification(prev, cur);
  assert.equal(n.type, "liveScoreChange");
  assert.equal(n.title, "Score change");
  assert.ok(n.text.includes("Saudi Arabia"));
});

test("score change: no alert when the score is unchanged while live", () => {
  const a = { status: { state: "in", completed: false }, home: { score: 1 }, away: { score: 1 } };
  const b = { status: { state: "in", completed: false }, home: { score: 1 }, away: { score: 1 } };
  assert.equal(app.scoreNotification(a, b), null);
});

// ---- Alert 3: final score ----
test("final score: fires when a match completes", () => {
  const prev = { status: { state: "in", completed: false }, home: { score: 1 }, away: { score: 0 } };
  const cur = {
    status: { state: "post", completed: true },
    home: { team: "Saudi Arabia", displayName: "Saudi Arabia", score: 1 },
    away: { team: "Uruguay", displayName: "Uruguay", score: 0 },
  };
  const n = app.scoreNotification(prev, cur);
  assert.equal(n.type, "finalScore");
  assert.equal(n.title, "Final score");
  assert.ok(n.text.includes("1-0"));
});

// ---- Alert 4: new favorite ----
test("favoriteTeam: reads the top consensus team, null when absent", () => {
  assert.equal(app.favoriteTeam({ odds: { consensus: [{ team: "Spain" }, { team: "France" }] } }), "Spain");
  assert.equal(app.favoriteTeam({ odds: { consensus: [] } }), null);
  assert.equal(app.favoriteTeam(null), null);
});

test("new favorite: fires only when the leader actually changes", () => {
  reset();
  app.showFavoriteNotification({ odds: { consensus: [{ team: "Brazil" }] } }, { odds: { consensus: [{ team: "Spain" }] } });
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].type, "favoriteChange");
  assert.ok(toasts[0].text.includes("We have a new favorite - Spain"));
  assert.equal(desktop.length, 1); // also pushed as a desktop notification

  reset();
  app.showFavoriteNotification({ odds: { consensus: [{ team: "Spain" }] } }, { odds: { consensus: [{ team: "Spain" }] } });
  assert.equal(toasts.length, 0, "unchanged leader -> no alert");

  reset();
  app.showFavoriteNotification(null, { odds: { consensus: [{ team: "Spain" }] } });
  assert.equal(toasts.length, 0, "first load -> no alert");
});

// ---- End-to-end emit path: toast + desktop notification ----
test("showMatchNotifications: emits both a toast and a desktop notification on kick-off", () => {
  reset();
  const prev = [{ id: "1", status: { state: "pre", completed: false }, home: { team: "A", score: 0 }, away: { team: "B", score: 0 } }];
  const cur = [
    {
      id: "1",
      status: { state: "in", completed: false },
      home: { team: "A", displayName: "A", score: 0 },
      away: { team: "B", displayName: "B", score: 0 },
      stageLabel: "Group A",
      location: { city: "Dallas" },
    },
  ];
  app.showMatchNotifications(prev, cur);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].type, "kickoff");
  assert.equal(desktop.length, 1);
});

test("showMatchNotifications: no alerts on the very first load (no previous matches)", () => {
  reset();
  app.showMatchNotifications([], [{ id: "1", status: { state: "in" }, home: {}, away: {} }]);
  assert.equal(toasts.length, 0);
});
