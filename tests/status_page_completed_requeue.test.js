const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("completed processing row confirms irreversible deletion before requeueing", async () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "scripts", "status_page.html"), "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace("%%REFRESH_MS%%", "15000");
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        style: {}, dataset: {}, innerHTML: "", textContent: "", disabled: false, handlers: {},
        addEventListener(name, handler) { this.handlers[name] = handler; },
        querySelector() { return null; },
      });
      return elements.get(id);
    },
    querySelectorAll() { return []; },
  };
  const posts = [];
  let confirmed = false;
  let confirmText = "";
  const context = vm.createContext({
    document,
    window: { confirm(text) { confirmText = text; return confirmed; } },
    setInterval() { return 1; }, clearInterval() {},
    fetch(url, options) {
      if (options?.method === "POST") {
        posts.push({ url, options });
        return Promise.resolve({ ok: true, json: async () => ({ ok: true, pending_total: 1 }) });
      }
      return new Promise(() => {});
    },
  });
  vm.runInContext(script, context);
  vm.runInContext(`processingRecentState.total = 1; renderProcessingRecent({recent_done:[{
    dataset_key:"Asia|China|Shanghai", output_dir:"Tools/Oneshp_pipline_qgis/out_data/shanghai_pipeline"
  }]});`, context);
  assert.match(document.getElementById("p-recent-table").innerHTML, /置顶/);
  const button = { disabled: false, dataset: { prequeueCompleted: "Asia|China|Shanghai",
    outputDir: "Tools/Oneshp_pipline_qgis/out_data/shanghai_pipeline",
    inputDir: "Tools/Oneshp_pipline_qgis/out_data/shanghai_pipeline_input" } };
  const click = document.getElementById("p-recent-table").handlers.click;
  await click({ target: { closest() { return button; } } });
  assert.equal(posts.length, 0);
  confirmed = true;
  await click({ target: { closest() { return button; } } });
  await new Promise(setImmediate);
  assert.match(confirmText, /无法恢复/);
  assert.match(confirmText, /shanghai_pipeline/);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, "/api/process/requeue-completed");
  assert.deepEqual(JSON.parse(posts[0].options.body), {
    dataset_key: "Asia|China|Shanghai", confirm_delete: true,
    expected_output_dir: "Tools/Oneshp_pipline_qgis/out_data/shanghai_pipeline",
    expected_input_dir: "Tools/Oneshp_pipline_qgis/out_data/shanghai_pipeline_input",
  });
});
