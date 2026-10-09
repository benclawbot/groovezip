const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("index.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function setup(fetchImpl) {
  const elements = new Map();
  for (const id of ["check-btn", "url", "toggle", "preview", "error", "pl-title", "pl-count", "pl-prevn", "pl-info", "progress", "stat-text", "stat-extra", "current", "bar-fill", "download-link", "zip-meta", "zip-href", "dl-btn", "quality"]) {
    elements.set(id, {
      id, value: id === "url" ? "" : id === "quality" ? "192" : "", textContent: "",
      disabled: false, style: {}, innerHTML: "", classList: {
        values: new Set(id === "pl-info" || id === "progress" || id === "preview" ? ["hidden"] : []),
        add(name) { this.values.add(name); }, remove(name) { this.values.delete(name); },
        toggle(name) { this.values.has(name) ? this.values.delete(name) : this.values.add(name); },
        contains(name) { return this.values.has(name); },
      }, addEventListener() {},
    });
  }
  const context = {
    document: { getElementById: id => elements.get(id) }, fetch: fetchImpl,
    AbortController, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    encodeURIComponent, JSON, Math, Error,
  };
  vm.runInNewContext(script, context);
  return { elements, context };
}

const response = (data, status = 200, jsonError = false) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => { if (jsonError) throw new SyntaxError("bad json"); return data; },
});

(async () => {
  {
    let requested;
    const { elements, context } = setup(async (url, options) => {
      requested = { url, body: JSON.parse(options.body) };
      return response({ id: "job-1" });
    });
    elements.get("url").value = "https://youtube.com/playlist?list=old";
    await context.check();
    elements.get("url").value = "https://youtube.com/playlist?list=new";
    await context.startDownload();
    assert.equal(requested.url, "/api/download");
    assert.equal(requested.body.url, "https://youtube.com/playlist?list=new");
    assert.equal(elements.get("dl-btn").disabled, true, "button stays disabled while job is running");
  }
  for (const [name, fetchImpl, expectedMessage] of [
    ["network failure", async () => { throw new TypeError("offline"); }, "offline"],
    ["non-JSON response", async () => response(null, 502, true), "unreadable response"],
    ["HTTP failure", async () => response({ error: "service unavailable" }, 503), "service unavailable"],
  ]) {
    const { elements, context } = setup(fetchImpl);
    elements.get("url").value = "https://youtube.com/watch?v=abc";
    await context.startDownload();
    assert.equal(elements.get("dl-btn").disabled, false, `${name}: button restored`);
    assert.match(elements.get("error").textContent, new RegExp(expectedMessage, "i"), name);
  }
  {
    const { elements, context } = setup(async () => response({
      is_playlist: true, title: "Visible Mix", count: 2, entries: [],
    }));
    elements.get("url").value = "https://youtube.com/playlist?list=abc";
    await context.check();
    assert.equal(elements.get("pl-info").classList.contains("hidden"), false);
    assert.equal(elements.get("pl-title").textContent, "Visible Mix");
    assert.equal(elements.get("pl-count").textContent, "2 tracks");
  }
  {
    let resolveStatus;
    let statusCalls = 0;
    const { elements, context } = setup(async url => {
      if (url === "/api/download") return response({ id: "job-poll" });
      statusCalls++;
      return new Promise(resolve => { resolveStatus = resolve; });
    });
    context.setInterval = callback => { context.tick = callback; return 7; };
    elements.get("url").value = "https://youtube.com/watch?v=abc";
    await context.startDownload();
    const firstPoll = context.tick();
    await context.tick();
    assert.equal(statusCalls, 1, "poll requests do not overlap");
    resolveStatus(response({ id: "job-poll", state: "running", completed: 0, total: 1 }));
    await firstPoll;
  }
  {
    const { elements, context } = setup(async url => url === "/api/download"
      ? response({ id: "job-http" }) : response({ error: "job not found" }, 404));
    context.setInterval = callback => { context.tick = callback; return 8; };
    elements.get("url").value = "https://youtube.com/watch?v=abc";
    await context.startDownload();
    await context.tick();
    assert.match(elements.get("error").textContent, /job not found/i);
    assert.equal(elements.get("dl-btn").disabled, false);
  }
  console.log("Frontend regression checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
