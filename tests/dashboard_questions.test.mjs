import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../scripts/dashboard/index.html", import.meta.url), "utf8");
const [markup, script] = html.split("<script>");
const source = script.split("</script>")[0];

function element() {
  const listeners = new Map();
  const attrs = {};
  return {
    value: "",
    hidden: false,
    dataset: {},
    listeners,
    attrs,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(name, handler) { listeners.set(name, handler); },
    setAttribute(name, value) { attrs[name] = value; },
    focus() { this.focused = true; },
    contains() { return false; },
    querySelectorAll() { return []; },
  };
}

function openQuestions({ mobile = false, storage = new Map() } = {}) {
  // The real boot code binds the actions and fetches the run list.
  const elements = new Map(
    [...markup.matchAll(/\bid="([^"]+)"([^>]*)>/g)].map(([, id, tail]) => [
      id, { ...element(), hidden: /\shidden\b/.test(tail) },
    ]),
  );
  const list = elements.get("research-list");
  list.innerHTML = markup.match(/\bid="research-list"[^>]*>([\s\S]*?)<\/div>/)[1];
  const handlers = new Map(), browser = new Map(), requests = [];
  const location = { hash: "", href: "http://localhost/" };
  vm.runInNewContext(source, {
    URL,
    document: {
      body: element(),
      documentElement: element(),
      getElementById: (id) => elements.get(id),
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: (name, handler) => handlers.set(name, handler),
    },
    window: {
      matchMedia: () => ({ matches: mobile, addEventListener() {} }),
      addEventListener: (name, handler) => browser.set(name, handler),
      scrollTo() {},
    },
    location,
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
    setInterval() {},
    fetch(path) {
      return new Promise((resolve) => requests.push({ path, resolve }));
    },
  }, { filename: "scripts/dashboard/index.html" });

  async function respond(status, body, path = "/api/runs") {
    const pending = requests.shift();
    assert.equal(pending?.path, path);
    pending.resolve({ ok: status === 200, status, json: async () => body });
    await new Promise(setImmediate);
  }

  function click(dataset) {
    const target = { dataset, closest: (selector) => selector === "button" ? target : null };
    handlers.get("click")({ target });
  }

  function keydown(key) {
    const event = { key, target: { closest: () => null, matches: () => false }, preventDefault() {} };
    handlers.get("keydown")(event);
  }

  return { list, elements, respond, click, keydown, location, route: browser.get("hashchange") };
}

const runs = [
  { key: "lake", question: "What changes in lake water?", name: "Lake", status: "running", started_at: "2026-06-01T12:00:00Z" },
  { key: "soil", question: "What changes in forest soil?", name: "Soil", status: "failed", started_at: "2026-06-02T12:00:00Z" },
];

test("the question surface opens while the collapsed history loads", async () => {
  const { list, elements, respond } = openQuestions();
  assert.match(list.innerHTML, /Loading your research/);
  assert.equal(elements.get("composer").hidden, false);
  assert.equal(elements.get("questions-panel").hidden, true);
  await respond(200, { runs: [] });
  assert.match(list.innerHTML, /No questions yet/);
});

test("the question list recovers after a failed read", async () => {
  const { list, elements, respond, click } = openQuestions();
  const banner = elements.get("connection-error"), label = elements.get("connection-label");
  await respond(503, { error: "Fixture service unavailable" });
  assert.equal(banner.hidden, false);
  assert.equal(label.textContent, "Connection unavailable");
  assert.match(list.innerHTML, /Your workspace is unavailable/);
  click({ action: "refresh" });
  await respond(200, { runs: [] });
  assert.equal(banner.hidden, true);
  assert.equal(label.textContent, "Workspace connected");
  assert.match(list.innerHTML, /No questions yet/);
});

test("opening, searching, and closing history leaves the current question surface and draft intact", async () => {
  const { list, elements, respond, click, keydown, location } = openQuestions();
  const search = elements.get("research-search"), toggle = elements.get("questions-toggle");
  const draft = elements.get("f-q");
  draft.value = "My unfinished question";
  await respond(200, { runs });
  keydown("/");
  assert.equal(elements.get("questions-panel").hidden, false);
  assert.equal(toggle.attrs["aria-expanded"], "true");
  assert.equal(search.focused, true);
  search.listeners.get("input")({ target: { value: "lake water" } });
  assert.match(list.innerHTML, /lake water/);
  assert.doesNotMatch(list.innerHTML, /forest soil/);
  click({ action: "questions" });
  assert.equal(elements.get("questions-panel").hidden, true);
  assert.equal(toggle.focused, true);
  assert.equal(draft.value, "My unfinished question");
  assert.equal(location.hash, "");
  assert.equal(elements.get("composer").hidden, false);
});

test("selecting a question on mobile closes the panel and opens its evidence view", async () => {
  const { elements, respond, click, route, location, list } = openQuestions({ mobile: true });
  await respond(200, { runs });
  click({ action: "questions" });
  assert.equal(elements.get("main").inert, true);
  assert.equal(elements.get("research-search").focused, undefined);
  click({ open: "lake" });
  assert.equal(location.hash, "#research/lake/overview");
  route();
  assert.equal(elements.get("main").inert, false);
  assert.equal(elements.get("questions-panel").hidden, true);
  assert.equal(elements.get("composer").hidden, true);
  assert.equal(elements.get("detail").hidden, false);
  assert.equal(elements.get("workspace-title").textContent, runs[0].question);
  assert.match(list.innerHTML, /data-open="lake"[^>]*aria-current="true"/);
});

test("a fresh workspace URL can reopen the last question once its list is available", async () => {
  const storage = new Map([["canary-open-question", "soil"]]);
  const { respond, location } = openQuestions({ storage });
  assert.equal(location.hash, "");
  await respond(200, { runs });
  assert.equal(location.hash, "#research/soil/overview");
});
