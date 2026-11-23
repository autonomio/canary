import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../scripts/dashboard/index.html", import.meta.url), "utf8");
const [markup, script] = html.split("<script>");
const source = script.split("</script>")[0];

function element() {
  return {
    value: "",
    hidden: false,
    dataset: {},
    classList: { add() {}, remove() {} },
    addEventListener() {},
    setAttribute() {},
    focus() {},
    contains() { return false; },
    querySelectorAll() { return []; },
  };
}

function openQuestions() {
  // Seed the initial render from the page and let its real boot code handle fetch and clicks.
  const elements = new Map(
    [...markup.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, element()]),
  );
  const list = elements.get("research-list");
  const total = elements.get("stat-total");
  const banner = elements.get("connection-error");
  const label = elements.get("connection-label");
  list.innerHTML = markup.match(/\bid="research-list"[^>]*>\s*([\s\S]*?<\/div>)\s*<\/div>/)[1];
  total.textContent = markup.match(/\bid="stat-total"[^>]*>([^<]*)/)[1];
  banner.hidden = /\bid="connection-error"[^>]*\shidden\b/.test(markup);
  const handlers = new Map();
  const topbar = element();
  const requests = [];

  vm.runInNewContext(source, {
    URL,
    document: {
      documentElement: element(),
      getElementById: (id) => elements.get(id),
      querySelector: (selector) => selector === ".topbar" ? topbar : null,
      querySelectorAll: () => [],
      addEventListener: (name, handler) => handlers.set(name, handler),
    },
    window: {
      matchMedia: () => ({ addEventListener() {} }),
      addEventListener() {},
      scrollTo() {},
    },
    location: { hash: "", href: "http://localhost/" },
    sessionStorage: { getItem: () => null },
    setInterval() {},
    fetch(path) {
      assert.equal(path, "/api/runs");
      return new Promise((resolve) => requests.push(resolve));
    },
  }, { filename: "scripts/dashboard/index.html" });

  async function respond(status, body) {
    const resolve = requests.shift();
    assert.ok(resolve, "Expected a pending /api/runs request");
    resolve({ ok: status === 200, status, json: async () => body });
    await new Promise(setImmediate);
  }

  function retry() {
    const action = list.innerHTML.match(/<button\b[^>]*data-action="([^"]+)"[^>]*>Try again<\/button>/)?.[1];
    assert.equal(action, "refresh");
    const target = {
      dataset: { action },
      closest(selector) { return selector === "button" ? this : null; },
    };
    handlers.get("click")({ target });
  }

  return { list, total, banner, label, respond, retry };
}

test("Questions shows loading until the first list arrives", async () => {
  const { list, total, respond } = openQuestions();
  assert.match(list.innerHTML, /Loading your research/);
  assert.doesNotMatch(list.innerHTML, /Start a research question/);
  assert.equal(total.textContent, "—");

  await respond(200, { runs: [] });

  assert.match(list.innerHTML, /Start a research question/);
  assert.equal(total.textContent, "0");
});

test("Questions recovers after an initial request fails and the user retries", async () => {
  const { list, banner, label, respond, retry } = openQuestions();
  await respond(503, { error: "Fixture service unavailable" });
  assert.equal(banner.hidden, false);
  assert.equal(label.textContent, "Connection unavailable");
  assert.match(list.innerHTML, /Your workspace is unavailable/);

  retry();
  assert.match(list.innerHTML, /Your workspace is unavailable/);
  await respond(200, { runs: [] });

  assert.equal(banner.hidden, true);
  assert.equal(label.textContent, "Workspace connected");
  assert.match(list.innerHTML, /Start a research question/);
});
