const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function page({ failPost = false, failRead = false } = {}) {
  const root = path.join(__dirname, "..", "pages", "apex-rank-watch");
  const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
  const element = () => ({
    value: "", hidden: false, disabled: false, textContent: "", innerHTML: "",
    className: "", addEventListener() {}, replaceChildren() {},
  });
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], element()]));
  const buttons = [element(), element()];
  const filters = [element(), element(), element()];
  const configInput = { dataset: { config: "timeout_ms" }, type: "number" };
  Object.defineProperty(configInput, "value", {
    get() { if (failRead) throw new Error("input unavailable"); return "30000"; },
    set() {},
  });
  const secretInput = { dataset: { secret: "api_key" }, value: "new-key" };
  const requests = [];
  const context = {
    document: {
      getElementById(id) { return elements.get(id) || null; },
      querySelector() { return element(); },
      querySelectorAll(selector) {
        if (selector === "[data-save]") return buttons;
        if (selector === "[data-config]") return [configInput];
        if (selector === "[data-secret]") return [secretInput];
        if (selector === "[data-filter]") return filters;
        return [];
      },
      addEventListener() {},
    },
    window: {
      AstrBotPluginView: {
        ready() { return new Promise(() => {}); },
        async apiPost(endpoint, body) {
          requests.push({ endpoint, body });
          if (failPost) throw new Error("server refused save");
          return { saved: true };
        },
        async apiGet(endpoint) {
          return endpoint === "/dashboard"
            ? { sessions: [], groups: [] }
            : { schema: {}, values: {}, sessions: [] };
        },
      },
      addEventListener() {},
    },
    structuredClone, setTimeout() { return 0; }, clearTimeout() {},
  };
  const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
  vm.runInNewContext(source.replace(/\}\)\(\);\s*$/, "globalThis.review = { state, saveAll };})();"), context);
  context.review.state.ready = true;
  return { ...context.review, elements, buttons, requests };
}

test("saving works with the real page IDs and the generated secret field", async () => {
  const app = page();
  assert.equal(app.elements.has("api-key"), false);
  assert.equal(await app.saveAll(), true);
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].endpoint, "/settings");
  assert.equal(app.requests[0].body.values.api_key, "new-key");
  assert.equal(app.requests[0].body.values.timeout_ms, 30000);
  assert.equal(app.state.busy, false);
  assert.ok(app.buttons.every((button) => !button.disabled));
});

test("a rejected save restores the controls and can be retried", async () => {
  const app = page({ failPost: true });
  assert.equal(await app.saveAll(), false);
  assert.equal(app.state.busy, false);
  assert.ok(app.buttons.every((button) => !button.disabled));
  assert.match(app.elements.get("notice").textContent, /server refused save/);
  assert.equal(await app.saveAll(), false);
  assert.equal(app.requests.length, 2);
});

test("a form read error also restores the controls", async () => {
  const app = page({ failRead: true });
  assert.equal(await app.saveAll(), false);
  assert.equal(app.requests.length, 0);
  assert.equal(app.state.busy, false);
  assert.ok(app.buttons.every((button) => !button.disabled));
  assert.match(app.elements.get("notice").textContent, /input unavailable/);
});
