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
  const question = elements.get("f-q");
  const form = elements.get("research-form");
  form.reset = () => { question.value = ""; };
  elements.get("f-target").value = "50";
  elements.get("f-corrections").value = "3";
  const dialog = elements.get("launch-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.listeners.get("close")(); };
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
      removeItem: (key) => storage.delete(key),
    },
    setInterval() {},
    setTimeout() {},
    clearTimeout() {},
    fetch(path, options) {
      return new Promise((resolve) => requests.push({ path, method: options?.method || "GET", body: options?.body, resolve }));
    },
  }, { filename: "scripts/dashboard/index.html" });

  async function respond(status, body, path = "/api/runs", method = "GET") {
    const pending = requests.shift();
    assert.equal(pending?.path, path);
    assert.equal(pending.method, method);
    pending.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
    await new Promise(setImmediate);
    return pending.body && JSON.parse(pending.body);
  }

  function click(dataset) {
    const target = { dataset, closest: (selector) => selector === "button" ? target : null };
    handlers.get("click")({ target });
  }

  function keydown(key) {
    const event = { key, target: { closest: () => null, matches: () => false }, preventDefault() {} };
    handlers.get("keydown")(event);
  }

  return { list, elements, respond, click, keydown, location, requests, route: browser.get("hashchange") };
}

const runs = [
  { key: "lake", question: "What changes in lake water?", name: "Lake", status: "running", started_at: "2026-06-01T12:00:00Z" },
  { key: "soil", question: "What changes in forest soil?", name: "Soil", status: "failed", started_at: "2026-06-02T12:00:00Z" },
];
const service = { source_configuration: { local_text: true, epo: true }, pdf_extraction: { engine: "fixture" } };
const compiled = {
  source_yaml: 'question: What changes in lake water?\n',
  resolved: { research: { model: "research-model", effort: "xhigh" },
    audit: { model: "audit-model", effort: "xhigh", max_correction_rounds: 3 },
    retrieval: { corpus: "fresh", selection: "coverage", target_papers: 50 } },
};

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

test("New question overrides history restoration even while the first list request is pending", async () => {
  const storage = new Map([["canary-open-question", "soil"]]);
  const { respond, location, click, route, elements } = openQuestions({ storage });
  click({ action: "new" });
  route();
  await respond(200, { runs });
  assert.equal(location.hash, "#research");
  assert.equal(elements.get("composer").hidden, false);
  assert.equal(elements.get("f-q").focused, true);
});

test("New question retries a failed keyword usage lookup", async () => {
  const { respond, elements, click } = openQuestions();
  const endpoint = "/api/sources/openalex/status";
  const focus = elements.get("f-q").listeners.get("focus");
  await respond(200, { runs });
  focus();
  click({ action: "new" });
  await respond(503, { error: "Fixture service unavailable" }, endpoint);
  const usage = elements.get("question-usage");
  assert.match(usage.innerHTML, /Keyword usage unavailable/);
  click({ action: "new" });
  await respond(200, { keyword_searches_today: 9 }, endpoint);
  assert.match(usage.innerHTML, /9 Canary keyword requests today/);
});

test("editing the next draft during post-submit history refresh keeps the composer open", async () => {
  const storage = new Map();
  const { respond, elements, location } = openQuestions({ storage });
  await respond(200, { runs });
  const form = elements.get("research-form"), question = elements.get("f-q");
  question.value = "What changes in lake water?";
  form.listeners.get("input")({ target: question });
  form.listeners.get("submit")({ preventDefault() {} });
  await respond(200, service, "/api/status");
  await respond(200, compiled, "/api/runs/compile", "POST");
  const submit = elements.get("launch-next").listeners.get("click")();
  await respond(201, { key: "lake" }, "/api/runs", "POST");
  assert.equal(question.value, "");
  question.value = "What changes in river water?";
  form.listeners.get("input")({ target: question });
  await respond(200, { runs });
  await submit;
  assert.equal(location.hash, "");
  assert.equal(elements.get("composer").hidden, false);
  assert.equal(JSON.parse(storage.get("canary-research-draft")).q, question.value);
});

test("the launch dialog validates exact limits and context before submitting the question", async () => {
  const { respond, elements, requests } = openQuestions();
  await respond(200, { runs });
  elements.get("f-q").value = "What changes in lake water?";
  elements.get("f-target").value = "32";
  elements.get("f-corrections").value = "0";
  elements.get("f-selection").value = "citation_graph";
  elements.get("f-research-state").value = '{"reviewed_papers":[]}';
  elements.get("f-continue-research").checked = true;
  elements.get("research-form").listeners.get("submit")({ preventDefault() {} });
  const start = elements.get("launch-next");
  assert.equal(start.disabled, true);
  await respond(200, service, "/api/status");
  const resolved = { ...compiled.resolved, retrieval: { ...compiled.resolved.retrieval,
    selection: "citation_graph", target_papers: 32 }, audit: { ...compiled.resolved.audit, max_correction_rounds: 0 } };
  const preview = await respond(200, { ...compiled, resolved }, "/api/runs/compile", "POST");
  assert.deepEqual(preview.spec, {
    question: "What changes in lake water?",
    retrieval: { selection: "citation_graph", target_papers: 32 },
    audit: { max_correction_rounds: 0 }, research_state: { reviewed_papers: [] }, continue_research: true,
  });
  assert.match(elements.get("launch-plan").innerHTML, /Citation-guided/);
  assert.equal(requests.length, 0);
  assert.equal(start.disabled, false);
  const submission = start.listeners.get("click")();
  const sent = await respond(201, { key: "lake" }, "/api/runs", "POST");
  await respond(200, { runs });
  await submission;
  assert.deepEqual(sent.spec, preview.spec);
  assert.equal(elements.get("launch-dialog").open, false);
});

test("a compiler error keeps launch disabled until validation succeeds", async () => {
  const { respond, elements, requests } = openQuestions();
  await respond(200, { runs });
  elements.get("f-q").value = "What changes in lake water?";
  elements.get("research-form").listeners.get("submit")({ preventDefault() {} });
  await respond(200, service, "/api/status");
  await respond(400, { error: "Fixture declaration error" }, "/api/runs/compile", "POST");
  assert.equal(elements.get("launch-next").disabled, true);
  assert.equal(elements.get("launch-error").textContent, "Fixture declaration error");
  assert.equal(requests.length, 0);
  elements.get("launch-retry").listeners.get("click")();
  await respond(200, compiled, "/api/runs/compile", "POST");
  assert.equal(elements.get("launch-next").disabled, false);
  assert.equal(elements.get("launch-error").hidden, true);
});

test("keyword search keeps discovery separate from starting a research run", async () => {
  const { respond, elements, requests } = openQuestions();
  await respond(200, { runs });
  elements.get("keyword-query").value = "Lake water";
  elements.get("keyword-limit").value = "2";
  const submit = elements.get("keyword-form").listeners.get("submit")({ preventDefault() {} });
  const sent = await respond(200, {
    query: "Lake water", status: "available", matched: 6, results: [{ title: "An indexed paper",
      url: "https://openalex.org/W1", authors: ["A. Reader"], year: 2025, venue: "Journal", abstract: "Indexed text." }],
  }, "/api/sources/openalex/search", "POST");
  await submit;
  assert.deepEqual(sent, { query: "Lake water", limit: 2 });
  assert.match(elements.get("keyword-status").textContent, /Showing 1 of 6 matches/);
  assert.match(elements.get("keyword-results").innerHTML, /Indexed abstract/);
  assert.equal(requests.length, 0);
});

test("YAML import fills the same draft controls without submitting a run", async () => {
  const { respond, click, elements, requests } = openQuestions();
  await respond(200, { runs });
  click({ action: "setup-research" });
  await respond(200, service, "/api/status");
  const input = elements.get("declaration-file");
  const yaml = "version: 1\nquestion: What changes in lake water?\n";
  input.files = [{ size: yaml.length, text: async () => yaml }];
  const imported = input.listeners.get("change")({ target: input });
  await new Promise(setImmediate);
  const resolved = { ...compiled.resolved, question: "What changes in lake water?",
    retrieval: { ...compiled.resolved.retrieval, target_papers: 40 },
    audit: { ...compiled.resolved.audit, max_correction_rounds: 1 } };
  const sent = await respond(200, { ...compiled, resolved }, "/api/runs/compile", "POST");
  await imported;
  assert.deepEqual(sent, { yaml });
  assert.equal(elements.get("f-q").value, resolved.question);
  assert.equal(elements.get("f-target").value, 40);
  assert.equal(elements.get("f-corrections").value, 1);
  assert.equal(requests.length, 0);
});
