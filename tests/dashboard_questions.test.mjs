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

function openQuestions({ mobile = false, storage = new Map(), href = "http://localhost/" } = {}) {
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
  const location = { hash: new URL(href).hash, href, assign(url) { this.href = url.toString(); } };
  vm.runInNewContext(source, {
    URL, crypto: globalThis.crypto, Option: class { constructor(label, value) { this.label = label; this.value = value; } },
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

test("review decisions show their evidence and download the selected edition", async () => {
  const { elements, respond, click, route, requests } = openQuestions();
  await respond(200, { runs });
  click({ open: "lake" });
  route();
  while (requests.length) {
    const path = requests[0].path;
    await respond(200, path.endsWith("/bundle") ? {bundle: {state:"running"}, research: {kind:"question"}} : {}, path);
  }
  const dialog = elements.get("review-dialog"), select = elements.get("review-edition");
  dialog.showModal = () => { dialog.open = true; };
  select.replaceChildren = () => {};
  elements.get("review-entries").replaceChildren = () => {};
  click({ action: "review-decisions" });
  const endpoint = "/api/runs/lake/review-decisions";
  const record = {id:"one", recorded_at:"2026-01-01", decision:{subject:"Report A", judgement:"include",
    status:"accepted", reason:"Methods meet criteria", evidence:["Methods <2>"],
    proposed_by:null, decided_by:{kind:"person",name:"S. Lee",role:"screener"}, checked_by:null}};
  const correction = {...record, id:"two", replaces:"one", decision:{...record.decision, judgement:"exclude"}};
  await respond(200, {last_entry:"two", entries:[record, correction]}, endpoint);
  const entries = elements.get("review-entries").innerHTML;
  assert.match(entries, /<summary>Replaced · Report A · include \(accepted\)<\/summary>/);
  assert.match(entries, /<dt>Replaced by<\/dt><dd><code>two<\/code>/);
  assert.match(entries, /<summary>Report A · exclude \(accepted\)<\/summary>/);
  assert.equal(entries.split("<dt>Subject ID</dt><dd><code>one</code>").length - 1, 2);
  assert.equal(elements.get("review-status").textContent, "This copy has no compact bundle file to verify.");
  assert.match(elements.get("review-entries").innerHTML, /Methods &lt;2&gt;/);
  assert.match(elements.get("review-entries").innerHTML, /S. Lee · screener \(self-declared\)/);
  assert.equal(elements.get("review-download").href, endpoint + "?last_entry=two");
  select.value = "one";
  select.listeners.get("change")();
  await respond(200, {last_entry:"one", entries:[record]}, endpoint + "?last_entry=one");
  assert.doesNotMatch(elements.get("review-entries").innerHTML, /Replaced/);
  assert.match(elements.get("review-entries").innerHTML, /<summary>Report A · include \(accepted\)<\/summary>/);
  select.value = "initial";
  select.listeners.get("change")();
  await respond(200, {last_entry:"initial",entries:[]}, endpoint + "?last_entry=initial");
  assert.match(elements.get("review-entries").innerHTML, /No decisions/);
  const input = elements.get("review-input");
  const request = {request_id:"correct", expected_last_entry:"one", decision:record.decision, replaces:"one"};
  input.files = [{text: async () => JSON.stringify(request)}];
  input.listeners.get("change")();
  await new Promise(setImmediate);
  const sent = await respond(409, {error:"Another edition was saved"}, endpoint, "POST");
  assert.deepEqual(sent, request);
  assert.equal(elements.get("review-status").textContent, "Another edition was saved");
  const parallel = {...record, id:"three", decision:{...record.decision, subject_id:"one", judgement:"exclude"}};
  input.files = [{text: async () => JSON.stringify(parallel.decision)}];
  input.listeners.get("change")();
  await new Promise(setImmediate);
  const filled = await respond(200, {last_entry:"three"}, endpoint, "POST");
  assert.deepEqual(filled.decision, parallel.decision);
  assert.equal(filled.expected_last_entry, "two");
  assert.match(filled.request_id, /^dashboard-[0-9a-f]{32}$/);
  assert.equal("replaces" in filled, false);
  await respond(200, {last_entry:"three", entries:[record, correction, parallel]}, endpoint);
  const current = elements.get("review-entries").innerHTML;
  assert.equal(current.split("One of 2 current · ").length - 1, 2);
  assert.match(current, /<summary>Replaced · Report A · include/);
  assert.match(current, /Also current for this subject<\/dt><dd><code>three<\/code>/);
  assert.match(current, /Also current for this subject<\/dt><dd><code>two<\/code>/);
  input.listeners.get("change")();
  await new Promise(setImmediate);
  const repeated = await respond(200, {last_entry:"four"}, endpoint, "POST");
  assert.notEqual(repeated.request_id, filled.request_id);
  assert.equal(repeated.expected_last_entry, "three");
  await respond(200, {last_entry:"four", entries:[record, correction, parallel, {...parallel, id:"four"}]}, endpoint);
  click({ action: "review-decisions" });
  const separate = {...record, id:"other"};
  await respond(200, {last_entry:"other", entries:[record, separate]}, endpoint);
  const sameName = elements.get("review-entries").innerHTML;
  assert.match(sameName, /Report A appears under 2 different subject IDs/);
  assert.doesNotMatch(sameName, /One of 2 current/);
});

test("checklist route uses the dashboard shell and filters the recorded evidence", async () => {
  const { elements, respond, location, route } = openQuestions();
  await respond(200, { runs });
  location.hash = "#research/lake/checklist";
  route();
  const item = {id:"24a", title:"Registration", heading:"Registration and protocol", status:"complete",
    elements:[{requirement:"Describe registration", status:"complete", basis:"reported",
      passages:[{supplier:"author", text:"Unregistered <review>", evidence:["entry", "Protocol"]}]}]};
  await respond(200, {question:"Lake water", citation:"Page et al.", counts:[], items:[item]}, "/api/runs/lake/prisma/checklist");
  const content = elements.get("checklist-content");
  assert.equal(elements.get("checklist-view").hidden, false);
  assert.equal(elements.get("detail").hidden, true);
  assert.match(content.innerHTML, /Complete · 1/);
  assert.match(content.innerHTML, /Unregistered &lt;review&gt;/);
  assert.match(content.innerHTML, /review-decisions\?last_entry=entry/);
  const empty = element(), filter = element(), all = element(), row = element();
  elements.set("checklist-empty", empty);
  filter.dataset = {checklistFilter:"missing"};
  all.dataset = {checklistFilter:"all"};
  row.dataset = {checklistStatus:"complete"};
  content.querySelectorAll = selector => selector.includes("-filter") ? [all, filter] : selector.includes("-status") ? [row] : [];
  const select = button => content.listeners.get("click")({currentTarget:content, target:{closest:()=>button}});
  select(filter);
  assert.equal(row.hidden, true);
  assert.equal(empty.hidden, false);
  select(all);
  assert.equal(row.hidden, false);
  assert.equal(empty.hidden, true);
});

test("checklist links inherit the dashboard theme and the toggle keeps the checklist open", () => {
  for (const theme of ["dark", "light"]) {
    const overview = new URL(theme === "light" ? "http://localhost/?theme=light" : "http://localhost/");
    const url = new URL("#research/lake/checklist", overview);
    const { elements, location } = openQuestions({href:url.href});
    assert.equal(elements.get("checklist-view").hidden, false);
    const toggle = elements.get("theme-toggle");
    assert.equal(toggle.textContent, theme === "light" ? "Dark" : "Light");
    toggle.listeners.get("click")();
    const next = new URL(location.href);
    assert.equal(next.hash, url.hash);
    assert.equal(next.searchParams.get("theme"), theme === "light" ? null : "light");
  }
});

test("item 2 links to an abstract entry and its recorded passage in the dashboard", async () => {
  const { elements, respond, location, route } = openQuestions();
  await respond(200, {runs});
  const endpoint = "/api/runs/lake/prisma/checklist";
  const report = {question:"Lake water", counts:[], citation:"Page et al.", items:[
    {id:"2", heading:"Abstract", title:"Structured abstract", status:"partial", elements:[], see_also:["abstract-2"]},
    {id:"abstract-2", heading:"Abstract", title:"Objectives", status:"complete", elements:[
      {requirement:"State the question.", basis:"saved_run", status:"complete", location:"#answer-abstract-2-1",
       passages:[{text:"What changes in lake water?", supplier:"run", evidence:["/answer/question"]}]}]}
  ]};
  const content = elements.get("checklist-content");
  location.hash = "#research/lake/checklist";
  route();
  await respond(200, report, endpoint);
  assert.equal((content.innerHTML.match(/<details\b/g) || []).length, report.items.length);
  assert.ok(content.innerHTML.includes("<p class=\"checklist-passage\">What changes in lake water?</p>"));
  assert.match(content.innerHTML, /Source · The question that started this review/);
  assert.doesNotMatch(content.innerHTML, /<blockquote>|>(?:Partly answered|Answered)\.<|Canary’s research record|Page et al/);
  assert.match(content.innerHTML, /Checklist adapted from/);
  const abstract = content.innerHTML.split('id="checklist-item-2"')[1].split("</details>")[0];
  assert.match(abstract, /What changes in lake water\?/);
  assert.equal(content.innerHTML.split('id="checklist-answer-abstract-2-1"').length, 2);
  for (const id of ["item-abstract-2", "answer-abstract-2-1"]) {
    const href = "#research/lake/checklist/" + id;
    assert.equal(content.innerHTML.includes('href="' + href + '"'), id === "item-abstract-2");
    assert.ok(content.innerHTML.includes('id="checklist-' + id + '"'));
    const section = {...element(), parentElement:{closest:()=>null}};
    const target = {...element(), closest:()=>section, matches:()=>id.startsWith("item"), querySelector:()=>section};
    content.contains = node => node === target;
    elements.set("checklist-" + id, target);
    location.hash = href;
    route();
    await respond(200, report, endpoint);
    assert.equal(section.open, true);
    assert.ok(section.focused || target.focused);
  }
});
