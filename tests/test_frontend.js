const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("index.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function setup(fetchImpl, search = "") {
  const elements = new Map();
  for (const id of ["check-btn", "url", "toggle", "preview", "error", "pl-title", "pl-count", "pl-duration", "pl-prevn", "pl-info", "progress", "job-title", "stat-text", "stat-extra", "current", "bar-fill", "download-link", "zip-meta", "zip-href", "dl-btn", "quality", "format", "resolution"]) {
    elements.set(id, {
      id, value: id === "url" ? "" : id === "quality" ? "192" : id === "format" ? "mp3" : id === "resolution" ? "720" : "", textContent: "",
      disabled: false, files: [], style: {}, innerHTML: "", classList: {
        values: new Set(id === "pl-info" || id === "progress" || id === "preview" ? ["hidden"] : []),
        add(name) { this.values.add(name); }, remove(name) { this.values.delete(name); },
        toggle(name) { this.values.has(name) ? this.values.delete(name) : this.values.add(name); },
        contains(name) { return this.values.has(name); },
      }, events: {}, addEventListener(name, callback) { this.events[name] = callback; },
    });
  }
  const context = {
    document: { getElementById: id => elements.get(id) }, fetch: fetchImpl,
    AbortController, setTimeout, clearTimeout, setInterval: callback => { context.tick = callback; return 1; }, clearInterval() {},
    encodeURIComponent, JSON, Math, Error, URL, URLSearchParams, location: { hash: search },
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
    let polled;
    const id = "a".repeat(32);
    const { context, elements } = setup(async url => {
      polled = url;
      return response({state: "running", title: "Recovery playlist", completed: 2, total: 359, phase: "converting"});
    }, "#job=" + id);
    assert.equal(elements.get("dl-btn").disabled, true);
    await context.tick();
    assert.equal(polled, "/api/status?id=" + id);
    assert.equal(elements.get("job-title").textContent, "Recovery playlist · Total duration unknown");
    assert.match(elements.get("stat-text").textContent, /2 \/ 359 MP3s ready · Converting/);
  }
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
    assert.equal(requested.body.format, "mp3");
    assert.equal(requested.body.quality, 192);
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
      ? response({ id: "job-progress" })
      : response({ id: "job-progress", state: "running", phase: "downloading",
        completed: 0, total: 2, current: "Live track", downloaded_bytes: 5242880,
        total_bytes: 10485760, speed: 2097152, failed: 0 }));
    context.setInterval = callback => { context.tick = callback; return 9; };
    elements.get("url").value = "https://youtube.com/playlist?list=abc";
    await context.startDownload();
    await context.tick();
    assert.match(elements.get("stat-text").textContent, /0 \/ 2 MP3s ready · Downloading/);
    assert.match(elements.get("current").textContent, /5\.0 \/ 10\.0 MB · 2\.0 MB\/s/);
    assert.equal(elements.get("bar-fill").style.width, "25%");
  }
  {
    const retryAt = Date.now()/1000 + 42;
    const { elements, context } = setup(async url => url === "/api/download"
      ? response({ id: "job-cooldown" })
      : response({ id: "job-cooldown", state: "running", phase: "cooldown", retry_at: retryAt,
        completed: 4, total: 10, elapsed: 12, estimated_remaining: 90,
        estimated_finish: Date.now()/1000+90,
        downloaded_bytes: 999, total_bytes: 1200, speed: 9 }));
    context.setInterval = callback => { context.tick = callback; return 9; };
    elements.get("url").value = "https://youtube.com/playlist?list=abc";
    await context.startDownload();
    await context.tick();
    assert.match(elements.get("stat-text").textContent, /Service cooldown/);
    assert.match(elements.get("current").textContent, /Elapsed 12s/);
    assert.match(elements.get("current").textContent, /About 1m remaining \(approx\.\)/);
    assert.match(elements.get("current").textContent, /Finish around /);
    assert.match(elements.get("current").textContent, /Service retry in 42s/);
    assert.doesNotMatch(elements.get("current").textContent, /MB\/s/);
  }
  {
    const { elements, context } = setup(async url => url === "/api/download"
      ? response({ id: "job-partial" })
      : response({ id: "job-partial", state: "done", result: "partial",
        result_reason: "YouTube required sign-in or a bot check.",
        completed: 45, total: 359, failed: 3, remaining: 311, zip_size: 1234 }));
    context.setInterval = callback => { context.tick = callback; return 10; };
    elements.get("url").value = "https://youtube.com/playlist?list=abc";
    await context.startDownload();
    await context.tick();
    assert.equal(elements.get("stat-text").textContent, "Partial ZIP — 45 MP3s ready");
    assert.match(elements.get("stat-extra").textContent, /3 failed · 311 not attempted/);
    assert.match(elements.get("error").textContent, /sign-in or a bot check/);
    assert.equal(elements.get("download-link").style.display, "flex");
    assert.equal(elements.get("bar-fill").style.width, "13%");
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
  {
    const { elements, context } = setup(async url => url === "/api/download"
      ? response({id:"job-mp4"})
      : response({id:"job-mp4",state:"done",result:"complete",mode:"single",format:"mp4",completed:1,total:1,file_size:900}));
    context.setInterval=callback=>{context.tick=callback;return 11;};
    elements.get("url").value="https://youtube.com/watch?v=clip&list=attached";
    elements.get("format").value="mp4";
    await context.startDownload(); await context.tick();
    assert.equal(elements.get("zip-href").href,"/api/file?id=job-mp4");
    assert.equal(elements.get("zip-href").textContent,"⬇ Save MP4");
    assert.equal(elements.get("stat-text").textContent,"Ready — MP4");
  }
  {
    const { elements } = setup(async () => response({ id: "unused" }));
    elements.get("url").value = "https://youtube.com/watch?v=abc";
    elements.get("format").value = "mp4";
    elements.get("format").events.change();
    assert.equal(elements.get("dl-btn").textContent, "⬇ Download MP4");
    elements.get("url").value = "https://youtube.com/playlist?list=abc";
    elements.get("url").events.input();
    assert.equal(elements.get("dl-btn").textContent, "⬇ Download ZIP");
    assert.match(html, /<select id="quality" aria-label="MP3 bitrate">/);
  }
  {
    const { elements, context } = setup(async url => url === "/api/download"
      ? response({ id: "job-queued" })
      : response({ id: "job-queued", state: "queued", phase: "queued", queue_position: 2,
        estimate_scope: "queue", estimated_remaining: 125, estimated_finish: Date.now()/1000+125 }));
    context.setInterval=callback=>{context.tick=callback;return 12;};
    elements.get("url").value="https://youtube.com/playlist?list=queued";
    await context.startDownload(); await context.tick();
    assert.match(elements.get("current").textContent,/Queue position 2/);
    assert.match(elements.get("current").textContent,/About 2m until this job starts/);
    assert.match(elements.get("current").textContent,/Start around /);
  }
  console.log("Frontend regression checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
