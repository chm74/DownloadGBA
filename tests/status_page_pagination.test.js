const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("an older refresh cannot replace a newer completed-task page", async () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "scripts", "status_page.html"), "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace("%%REFRESH_MS%%", "15000");
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, {
          style: {},
          dataset: {},
          innerHTML: "",
          textContent: "",
          disabled: false,
          handlers: {},
          addEventListener(name, handler) { this.handlers[name] = handler; },
          querySelector() { return null; },
        });
      }
      return elements.get(id);
    },
    querySelectorAll() { return []; },
  };
  const requests = [];
  const context = vm.createContext({
    document,
    window: {},
    setInterval() { return 1; },
    clearInterval() {},
    fetch(url) {
      return new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
    },
  });
  vm.runInContext(script, context);
  vm.runInContext(
    "refreshDbUpdated = async () => {}; refreshControl = () => {}; refreshProcessControl = () => {}; " +
    "processingRecentState.total = 20;",
    context,
  );
  assert.equal(requests.length, 1);
  document.getElementById("p-recent-next").handlers.click();
  assert.equal(requests.length, 2);
  assert.match(requests[1].url, /process_recent_offset=10/);

  const snapshot = (offset, key) => ({
    batch_state: "done",
    total: 0,
    counts: {},
    queue_total: 0,
    running: [],
    queue: [],
    recent_done: [],
    failed: [],
    processing: {
      total: 20,
      counts: { OK: 20 },
      running: [],
      queue: [],
      recent_done: [{ dataset_key: key }],
      recent_total: 20,
      recent_limit: 10,
      recent_offset: offset,
      failed: [],
      blocked: [],
    },
  });
  requests[1].resolve({ json: async () => snapshot(10, "page-two") });
  await new Promise(setImmediate);
  requests[0].resolve({ json: async () => snapshot(0, "page-one") });
  await new Promise(setImmediate);

  assert.equal(document.getElementById("p-recent-page-label").textContent, "第 2/2 页");
  assert.match(document.getElementById("p-recent-table").innerHTML, /page-two/);
  assert.doesNotMatch(document.getElementById("p-recent-table").innerHTML, /page-one/);

  vm.runInContext("refresh(); refresh();", context);
  requests[3].resolve({ json: async () => snapshot(10, "new-page") });
  await new Promise(setImmediate);
  requests[2].reject(new Error("old request failed"));
  await new Promise(setImmediate);
  assert.equal(document.getElementById("error-banner").style.display, "none");
});
