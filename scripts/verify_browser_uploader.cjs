/* Offline uploader integration checks. MIDI and the DOM are test doubles;
 * no physical device is opened or sent data. Uses only Node built-ins. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const docs = path.join(__dirname, "../docs");
const html = fs.readFileSync(path.join(docs, "index.html"), "utf8");
const moduleSource = fs.readFileSync(path.join(docs, "cc-remap.js"), "utf8");
const pageSource = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];

class Element {
  constructor() {
    this.children = []; this.attributes = {}; this.style = {};
    this.classList = { toggle() {} }; this.value = "";
    this._text = ""; this.disabled = false;
  }
  set value(v) { this._value = String(v); }
  get value() { return this._value; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(" "); }
  set innerHTML(v) { this.textContent = v; }
  get innerHTML() { return this.textContent; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
}

async function until(test) {
  const deadline = Date.now() + 10000;
  while (!test()) {
    if (Date.now() > deadline) throw new Error("Timed out waiting for uploader state");
    await new Promise(resolve => setImmediate(resolve));
  }
}

async function page(key, corrupt = false) {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element()]));
  for (const id of ["s2", "sCC", "s4"]) nodes.get(id).setAttribute("aria-disabled", "true");
  const get = id => { assert(nodes.has(id), `Missing HTML element ${id}`); return nodes.get(id); };
  const sent = [];
  const output = { id: "fake", name: "Test Bootloader", send(message) {
    assert.equal(get("firmwareChoice").disabled, true, "build selection must lock during upload");
    sent.push(Buffer.from(message));
  }};
  const href = "https://example.test/" + (key ? "?firmware=" + key : "");
  const location = { href, search: new URL(href).search, assign(url) { this.next = url; } };
  const sandbox = {
    Uint8Array, URL, URLSearchParams, Blob, crypto: webcrypto, console,
    document: { getElementById: get, createElement: () => new Element() },
    navigator: { userAgent: "Chrome", requestMIDIAccess: async () => ({ outputs: new Map([["fake", output]]) }) },
    location, isSecureContext: true, confirm: () => true,
    setTimeout: callback => setImmediate(callback), clearTimeout: clearImmediate,
    fetch: async url => {
      const buffer = fs.readFileSync(path.join(docs, url));
      if (corrupt) buffer[100] ^= 1;
      return { ok: true, arrayBuffer: async () => Uint8Array.from(buffer).buffer };
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(moduleSource, sandbox);
  vm.runInContext(pageSource, sandbox);
  const cc = sandbox.CircuitCC;
  if (corrupt) {
    await until(() => get("fileHash").textContent.includes("hash mismatch"));
    await get("connect").onclick();
    assert.equal(get("s4").attributes["aria-disabled"], "true");
    await get("send").onclick();
    assert.equal(sent.length, 0);
    return;
  }
  await until(() => get("sCC").attributes["aria-disabled"] === "false");
  const perf = key !== "v050";
  const selected = key === "perf-v2-detents" ? "perf-v2-selectors" : key || "perf-v2";
  const expected = {
    "perf-v2": { file: "circuit-3592-extended-v0.5.0-perf-v2-feature.syx", hash: cc.PERF_V2_SYSEX_SHA256,
      references: cc.PERF_V2_REFERENCE_BUILDS, status: /unexplained crash/ },
    "perf-v2-selectors": { file: "circuit-3592-extended-v0.5.0-perf-v2-selectors.syx", hash: cc.SELECTORS_SYSEX_SHA256,
      references: cc.SELECTORS_REFERENCE_BUILDS, status: /emulation only/ },
    "perf-v2-lfo-rates": { file: "circuit-3592-extended-v0.5.0-perf-v2-lfo-rates.syx", hash: cc.LFO_RATES_SYSEX_SHA256,
      references: cc.LFO_RATES_REFERENCE_BUILDS, status: /DSP emulator only/ },
    v050: { file: "circuit-3592-filter-lfo-shift-automation.syx", hash: cc.EXTENDED_V050_SYSEX_SHA256,
      references: cc.REFERENCE_BUILDS, status: /Previous build/ },
  }[selected];
  assert.equal(get("firmwareChoice").value, selected);
  const file = expected.file;
  assert.equal(get("firmwareDownload").href, "firmware/" + file);
  const base = new Uint8Array(fs.readFileSync(path.join(docs, "firmware", file)));
  const baseHash = expected.hash;
  assert.equal(await cc.sha256Hex(base), baseHash);
  assert(get("fileHash").textContent.includes(baseHash));
  assert.match(get("firmwareStatus").textContent, expected.status);

  // A controller mapped to CC1 must not accidentally also drive the mod source.
  const image = cc.decodeFirmware(base).image;
  if (perf) {
    assert.throws(() => cc.planRemap(image, { "synth.macro5": 1 }), /reserved/);
    assert.throws(() => cc.applyRemap(image, [{ part: "synth", record: 242, oldCC: 84, newCC: 1, requested: true }]), /reserved/);
  } else {
    assert.equal(cc.planRemap(image, { "synth.macro5": 1 }).length, 1);
  }
  const input = get("ccSynth").children[4].children[0];
  input.value = 1; input.oninput();
  await until(() => get("ccResult").textContent.includes(perf ? "Fix the highlighted" : "Custom firmware ready"));
  if (perf) {
    await get("connect").onclick();
    assert.equal(get("s4").attributes["aria-disabled"], "true");
    await get("send").onclick();
    assert.equal(sent.length, 0);
  }

  get("ccPreset").value = "nts1";
  get("ccPreset").onchange();
  const expectedHash = expected.references.nts1;
  await until(() => get("ccResult").textContent.includes(expectedHash));
  const custom = cc.buildRemappedSysex(base, cc.PRESETS.nts1.map);
  assert.equal(await cc.sha256Hex(custom.sysex), expectedHash);
  assert.deepEqual(Buffer.from(custom.image.slice(0x1e4, 0x258)), Buffer.from(image.slice(0x1e4, 0x258)));
  assert.deepEqual(Buffer.from(custom.image.slice(0x18508, 0x1850c)), Buffer.from(image.slice(0x18508, 0x1850c)));
  await get("connect").onclick();
  assert.equal(get("s4").attributes["aria-disabled"], "false");
  await get("send").onclick();
  assert.equal(sent.length, 5981);
  assert.deepEqual(Buffer.concat(sent), Buffer.from(custom.sysex));
  assert.equal(get("firmwareChoice").disabled, false);

  get("ccReset").onclick();
  await until(() => get("ccResult").textContent.startsWith("Stock CC numbers:"));
  sent.length = 0;
  await get("send").onclick();
  assert.deepEqual(Buffer.concat(sent), Buffer.from(base), "Reset must restore the selected base exactly");
  get("firmwareChoice").value = perf ? "v050" : "perf-v2";
  get("firmwareChoice").onchange();
  assert.equal(new URL(location.next).searchParams.get("firmware"), perf ? "v050" : "perf-v2");
}

(async () => {
  await page(null);
  await page("v050");
  await page("perf-v2-selectors");
  await page("perf-v2-lfo-rates");
  await page("perf-v2-detents");
  await page(null, true);
  console.log("PASS: default, selectors (and old detents link), LFO-rates and rollback selection; exact base and NTS-1 bytes; CC1 guard; reset; upload locking; corrupt firmware refused (mock MIDI).");
})().catch(error => { console.error(error); process.exitCode = 1; });
