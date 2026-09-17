const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(resolve(__dirname, "../../static/js/bulk-confirmation.js"), "utf8");

function setup(responses) {
  const elements = {};
  for (const id of ["bulk-confirmation-run", "bulk-progress", "bulk-progress-label",
    "bulk-error", "bulk-resume"]) {
    elements[id] = {
      textContent: "", hidden: true, action: "/batch/send/", listeners: {},
      addEventListener(name, listener) { this.listeners[name] = listener; },
    };
  }
  const statuses = Array.from({length: 3}, () => ({textContent: "En attente"}));
  const rows = statuses.map((status, i) => ({
    dataset: {bulkReference: `group-${i}`}, querySelector: () => status,
  }));
  const requests = [];
  const windowListeners = {};
  const context = {
    document: {
      getElementById: (id) => elements[id], querySelectorAll: () => rows,
    },
    window: {addEventListener: (name, listener) => { windowListeners[name] = listener; }},
    FormData: class {
      constructor() { this.values = {token: "signed-plan", csrfmiddlewaretoken: "csrf"}; }
      set(name, value) { this.values[name] = value; }
    },
    fetch: async (url, options) => {
      requests.push({url, ...options});
      const result = responses.shift();
      if (result instanceof Error) throw result;
      return {ok: true, redirected: false, json: async () => result, ...result};
    },
  };
  vm.runInNewContext(source, context);
  return {elements, statuses, requests, windowListeners};
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("processes every group sequentially and reports sent, failed and skipped separately", async () => {
  const app = setup([
    {status: "sent", message: "Mail envoyé"},
    {status: "failed", message: "Validé, SMTP en échec"},
    {status: "skipped", message: "À vérifier"},
  ]);
  await settle();
  assert.deepEqual(app.requests.map((r) => r.body.values.reference),
    ["group-0", "group-1", "group-2"]);
  assert.ok(app.requests.every((r) => r.method === "POST" &&
    r.body.values.csrfmiddlewaretoken === "csrf"));
  assert.equal(app.elements["bulk-progress"].value, 3);
  assert.match(app.elements["bulk-progress-label"].textContent, /Traitement terminé/);
  assert.match(app.elements["bulk-progress-label"].textContent, /1 validés avec mail envoyé/);
  assert.match(app.elements["bulk-progress-label"].textContent, /1 validés avec échec/);
  assert.match(app.elements["bulk-progress-label"].textContent, /1 à vérifier/);
  assert.equal(app.elements["bulk-resume"].hidden, true);
});

test("network interruption pauses the lot; resume never replays known completed groups", async () => {
  const app = setup([
    {status: "sent", message: "Mail envoyé"},
    new Error("Connexion perdue"),
    {status: "skipped", message: "Déjà traité : vérifier la fiche"},
    {status: "sent", message: "Mail envoyé"},
  ]);
  await settle();
  assert.equal(app.requests.length, 2);
  assert.equal(app.elements["bulk-resume"].hidden, false);
  assert.match(app.elements["bulk-error"].textContent, /Connexion perdue/);
  await app.elements["bulk-resume"].listeners.click();
  assert.deepEqual(app.requests.map((r) => r.body.values.reference),
    ["group-0", "group-1", "group-1", "group-2"]);
  assert.equal(app.elements["bulk-progress"].value, 3);
  assert.equal(app.elements["bulk-error"].hidden, true);
});

test("expired login stops immediately and gives an actionable explanation", async () => {
  const app = setup([{redirected: true}]);
  await settle();
  assert.equal(app.requests.length, 1);
  assert.equal(app.elements["bulk-resume"].hidden, false);
  assert.match(app.elements["bulk-error"].textContent, /session a expiré/);
  let blocked = false;
  app.windowListeners.beforeunload({preventDefault() { blocked = true; }});
  assert.equal(blocked, false);
});
