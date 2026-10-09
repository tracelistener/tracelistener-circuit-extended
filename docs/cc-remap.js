/* Circuit Extended: MIDI CC remap, browser port of cc-remap/circuit_cc_remap_patch.py.
 *
 * Rewrites the MIDI CC tables of the selected uploader image in the browser.
 * Forward (transmit) records are 8 bytes, [param_lo, param_hi, type, offset,
 * min, max, b6, b7]; b6 == 0xFF marks a CC whose number is b7.  Reverse
 * (receive) tables are 128 little-endian int16 record indexes, 0x8000 unmapped.
 * Only b7 of moved records and the matching reverse slots change.
 *
 * This file must produce byte-identical images to the Python builder;
 * cc-remap/verify_circuit_cc_remap.py checks that the constants below match.
 */
(function (root) {
  "use strict";

  const BASE = 0x08008000;
  const IMAGE_SIZE = 0x2EB80;
  const RECORD_SIZE = 8;
  const CC_MARKER = 0xFF;
  const UNMAPPED = 0x8000;
  const TX_TABLE_POINTERS = 0x0802E730;
  const BLOCK_SIZE = 32;
  const PACKED_BLOCK_SIZE = 37;
  const HEADER = [0xF0, 0x00, 0x20, 0x29, 0x00];
  const UPDATE_WRITE = 0x72;
  const UPDATE_FINISH = 0x73;

  const EXTENDED_V050_SYSEX_SHA256 = "7ea9affe4c5310a8c3d84abf6c05b1ee35d4ef9ee6d30bb040711a4eb047745f";
  const EXTENDED_V050_IMAGE_SHA256 = "1a3e6593e5cff6ec415b070fd1f93c618637f0520dae3af54b4d82a07c53d22e";

  const PERF_V2_SYSEX_SHA256 = "085f4decb43efb3c2738a1ef08851e42939c6e77c10bdb334ba70d70babf0d2e";
  const PERF_V2_IMAGE_SHA256 = "4355394aa331989dc61770f6b3e56ab215c2be9fbab5d0a241ea02c75ea7e225";

  // The dispatcher consumes the NRPN/RPN numbers before the reverse table is
  // consulted; 0 and 32 are Bank Select; 120-127 are channel mode messages.
  const RESERVED = {
    0: "Bank Select", 6: "NRPN data entry", 32: "Bank Select", 38: "NRPN data entry",
    98: "NRPN select", 99: "NRPN select", 100: "RPN select", 101: "RPN select",
  };

  const PARTS = {
    synth: { name: "synth", forward: 0x0802DBC0, count: 310, reverse: 0x08035340, rxLiteral: 0x0802073C, partIds: [0x00, 0x01] },
    drums: { name: "drums", forward: 0x0802E810, count: 28, reverse: 0x08035240, rxLiteral: 0x08020740, partIds: [0x10] },
    session: { name: "session", forward: 0x0802D9CC, count: 38, reverse: 0x08035480, rxLiteral: 0x08020744, partIds: [0x20] },
  };
  const PART_ORDER = ["synth", "drums", "session"];

  const SYNTH_MACRO_FIRST_RECORD = 174;
  const SYNTH_MACRO_STRIDE = 17;
  const DRUM_PARAMS = ["patch", "level", "pitch", "decay", "distortion", "eq", "pan"];
  const DRUM_PARAM_ALIASES = { filter: "eq" };
  const DRUM_KNOBS = ["pitch", "decay", "distortion", "eq"];

  // Stock synth CC names as written in the Circuit Programmer's Reference Guide.
  const SYNTH_CC_NAMES = {
    3: "polyphony mode", 5: "portamento rate", 9: "pre-glide", 13: "keyboard octave",
    19: "osc 1 wave", 20: "osc 1 wave interpolate", 21: "osc 1 pulse width index",
    22: "osc 1 virtual sync depth", 24: "osc 1 density", 25: "osc 1 density detune",
    26: "osc 1 semitones", 27: "osc 1 cents", 28: "osc 1 pitchbend",
    29: "osc 2 wave", 30: "osc 2 wave interpolate", 31: "osc 2 pulse width index",
    33: "osc 2 virtual sync depth", 35: "osc 2 density", 36: "osc 2 density detune",
    37: "osc 2 semitones", 39: "osc 2 cents", 40: "osc 2 pitchbend",
    51: "osc 1 level", 52: "osc 2 level", 54: "ring mod level", 56: "noise level",
    58: "pre FX level", 59: "post FX level", 60: "filter routing", 63: "filter drive",
    65: "filter drive type", 68: "filter type", 69: "filter tracking", 71: "filter resonance",
    74: "filter frequency", 78: "filter Q normalize", 79: "env 2 to filter frequency",
    108: "env 1 velocity", 73: "env 1 attack", 75: "env 1 decay", 70: "env 1 sustain",
    72: "env 1 release", 91: "distortion level", 93: "chorus level",
  };

  const STOCK_CC_FINGERPRINT = {
    synth: { 174: 80, 191: 81, 208: 82, 225: 83, 242: 84, 259: 85, 276: 86, 293: 87 },
    drums: { 0: 8, 6: 77, 7: 18, 20: 79, 27: 80 },
    session: { 0: 88, 22: 74, 23: 71, 37: 118 },
  };

  const PRESETS = {
    // Macro 1/2 are oscillator knobs and 5/6 filter knobs by Circuit convention.
    nts1: {
      label: "Korg NTS-1",
      map: { "synth.macro1": 54, "synth.macro2": 55, "synth.macro5": 43, "synth.macro6": 44 },
    },
  };

  // Known-answer SysEx hashes for presets on the v0.5.0 base (shared with Python).
  const REFERENCE_BUILDS = {
    nts1: "d4e63fadfd10a532bb61c56673da4dfc7fb8b426745f3493efdde2853d764f0a",
  };

  const PERF_V2_REFERENCE_BUILDS = { nts1: "4fcf2c276cf49885d4e40c4d6a5727f872bd0b43a5da40822a3423fe785eadfd" };

  const off = address => address - BASE;
  const u16 = (image, at) => image[at] | (image[at + 1] << 8);
  const u32 = (image, at) => (image[at] | (image[at + 1] << 8) | (image[at + 2] << 16) | (image[at + 3] << 24)) >>> 0;
  const sameBytes = (a, b) => a.length === b.length && a.every((value, index) => value === b[index]);

  function reservedReason(cc) {
    if (!Number.isInteger(cc) || cc < 0 || cc > 127) return "not a CC number";
    if (cc >= 120) return "channel mode message";
    return RESERVED[cc] || null;
  }

  const isAssignable = cc => reservedReason(cc) === null;

  /* ---------- SysEx container (tools/circuit_fw_tools.py) ---------- */

  function splitMessages(bytes) {
    const messages = [];
    let cursor = 0;
    while (cursor < bytes.length) {
      const start = bytes.indexOf(0xF0, cursor);
      if (start < 0) break;
      const end = bytes.indexOf(0xF7, start + 1);
      if (end < 0) throw new Error("unterminated SysEx message at " + start);
      if (HEADER.some((value, index) => bytes[start + index] !== value)) throw new Error("unexpected SysEx header at " + start);
      if (end + 1 - start < 7) throw new Error("short SysEx message at " + start);
      messages.push({ command: bytes[start + 5], start, end });
      cursor = end + 1;
    }
    return messages;
  }

  function decodeBlock(payload) {
    if (payload.length !== PACKED_BLOCK_SIZE) throw new Error("expected 37 packed bytes");
    const block = new Uint8Array(BLOCK_SIZE);
    let accumulator = 0, bits = 0, index = 0;
    for (const value of payload) {
      if (value & 0x80) throw new Error("packed firmware payload contains a non-MIDI-safe byte");
      accumulator = (accumulator << 7) | value;
      bits += 7;
      while (bits >= 8 && index < BLOCK_SIZE) {
        bits -= 8;
        block[index++] = (accumulator >> bits) & 0xFF;
      }
      accumulator &= (1 << bits) - 1;
    }
    return block;
  }

  function encodeBlock(block, padding) {
    const packed = new Uint8Array(PACKED_BLOCK_SIZE);
    let accumulator = 0, bits = 0, index = 0;
    const push = (value, width) => {
      accumulator = (accumulator << width) | value;
      bits += width;
      while (bits >= 7) {
        bits -= 7;
        packed[index++] = (accumulator >> bits) & 0x7F;
      }
      accumulator &= (1 << bits) - 1;
    };
    for (const value of block) push(value, 8);
    push(padding & 0x07, 3);
    if (index !== PACKED_BLOCK_SIZE || bits !== 0) throw new Error("block packing error");
    return packed;
  }

  const payloadOf = (bytes, message) => bytes.subarray(message.start + 6, message.end);

  function decodeFirmware(bytes) {
    const messages = splitMessages(bytes);
    const finish = messages.filter(message => message.command === UPDATE_FINISH);
    const writes = messages.filter(message => message.command === UPDATE_WRITE);
    if (finish.length !== 1) throw new Error("expected one UPDATE_FINISH message");
    if (!writes.length) throw new Error("firmware has no UPDATE_WRITE messages");
    const image = new Uint8Array(BLOCK_SIZE * (1 + writes.length));
    image.set(decodeBlock(payloadOf(bytes, finish[0])), 0);
    writes.forEach((message, index) => image.set(decodeBlock(payloadOf(bytes, message)), BLOCK_SIZE * (index + 1)));
    return { image, messages };
  }

  function encodeFirmware(image, messages, template) {
    const finish = messages.filter(message => message.command === UPDATE_FINISH);
    const writes = messages.filter(message => message.command === UPDATE_WRITE);
    if (image.length % BLOCK_SIZE) throw new Error("image length is not a multiple of 32 bytes");
    if (finish.length !== 1 || image.length / BLOCK_SIZE !== 1 + writes.length) throw new Error("image does not fit the template");
    const output = new Uint8Array(template);
    const put = (message, block) => output.set(encodeBlock(block, template[message.end - 1] & 0x07), message.start + 6);
    put(finish[0], image.subarray(0, BLOCK_SIZE));
    writes.forEach((message, index) => put(message, image.subarray(BLOCK_SIZE * (index + 1), BLOCK_SIZE * (index + 2))));
    return output;
  }

  /* ---------- CC tables (cc-remap/circuit_cc_remap_patch.py) ---------- */

  function forwardCCMap(image, part) {
    const map = new Map();
    for (let record = 0; record < part.count; record++) {
      const at = off(part.forward + RECORD_SIZE * record);
      if (image[at + 6] === CC_MARKER) map.set(record, image[at + 7]);
    }
    return map;
  }

  function reverseTable(image, part) {
    const at = off(part.reverse);
    return Array.from({ length: 128 }, (_, cc) => u16(image, at + 2 * cc));
  }

  function checkLayout(image) {
    if (image.length !== IMAGE_SIZE) throw new Error("image is " + image.length + " bytes, expected " + IMAGE_SIZE);
    for (const name of PART_ORDER) {
      const part = PARTS[name];
      for (const id of part.partIds) {
        if (u32(image, off(TX_TABLE_POINTERS + 4 * id)) !== part.forward) throw new Error(name + ": transmit pointer moved");
      }
      if (u32(image, off(part.rxLiteral)) !== part.reverse) throw new Error(name + ": receive literal moved");
      const terminator = off(part.forward + RECORD_SIZE * part.count);
      for (let i = 0; i < RECORD_SIZE; i++) {
        if (image[terminator + i] !== 0) throw new Error(name + ": table is not terminated");
      }
      const forward = forwardCCMap(image, part);
      const reverse = reverseTable(image, part);
      for (const [record, cc] of forward) {
        if (cc > 119 || reverse[cc] !== record) throw new Error(name + ": record " + record + " CC " + cc + " has no matching reverse slot");
      }
      reverse.forEach((record, cc) => {
        if (record !== UNMAPPED && (record >= part.count || forward.get(record) !== cc)) {
          throw new Error(name + ": reverse slot CC " + cc + " has no CC record");
        }
      });
    }
  }

  function checkStockCCs(image) {
    for (const [name, expected] of Object.entries(STOCK_CC_FINGERPRINT)) {
      const forward = forwardCCMap(image, PARTS[name]);
      for (const [record, cc] of Object.entries(expected)) {
        if (forward.get(Number(record)) !== cc) throw new Error(name + ": CC tables are not stock");
      }
    }
  }

  function resolveControl(image, name) {
    const text = String(name).trim().toLowerCase();
    let match = /^synth\.macro([1-8])$/.exec(text);
    if (match) return { part: PARTS.synth, record: SYNTH_MACRO_FIRST_RECORD + SYNTH_MACRO_STRIDE * (Number(match[1]) - 1) };
    match = /^drum([1-4])\.([a-z]+)$/.exec(text);
    if (match) {
      const param = DRUM_PARAM_ALIASES[match[2]] || match[2];
      const index = DRUM_PARAMS.indexOf(param);
      if (index < 0) throw new Error("unknown drum control " + name);
      return { part: PARTS.drums, record: (Number(match[1]) - 1) * DRUM_PARAMS.length + index };
    }
    match = /^(synth|drums|session)\.cc(\d{1,3})$/.exec(text);
    if (match) {
      const part = PARTS[match[1]];
      const stock = Number(match[2]);
      for (const [record, cc] of forwardCCMap(image, part)) if (cc === stock) return { part, record };
      throw new Error(part.name + " has no control on stock CC " + stock);
    }
    throw new Error("unknown control " + name);
  }

  // requests: [[control, cc], ...] or {control: cc}; order matters exactly as in Python.
  function hasPerformanceControls(image) {
    return sameBytes(image.slice(off(0x08020508), off(0x08020508) + 4), [0xe7, 0xf7, 0x6c, 0xbe]);
  }

  function planRemap(image, requests) {
    const entries = Array.isArray(requests) ? requests : Object.entries(requests);
    const byPart = new Map();
    for (const [control, cc] of entries) {
      const reason = reservedReason(cc);
      if (reason) throw new Error(control + ": CC " + cc + " cannot be assigned (" + reason + ")");
      const { part, record } = resolveControl(image, control);
      if (cc === 1 && part.name === "synth" && hasPerformanceControls(image)) {
        throw new Error("synth CC 1 is reserved for the performance build's mod-wheel source");
      }
      if (!forwardCCMap(image, part).has(record)) throw new Error(control + " is not a CC control");
      if (!byPart.has(part.name)) byPart.set(part.name, new Map());
      const wanted = byPart.get(part.name);
      if (wanted.has(record) && wanted.get(record) !== cc) throw new Error(control + ": assigned twice with different CC numbers");
      wanted.set(record, cc);
    }

    const changes = [];
    for (const [name, wanted] of byPart) {
      const current = forwardCCMap(image, PARTS[name]);
      const targets = [...wanted.values()];
      const duplicates = [...new Set(targets.filter((cc, index) => targets.indexOf(cc) !== index))].sort((a, b) => a - b);
      if (duplicates.length) throw new Error("CC " + duplicates.join(", ") + " is chosen for more than one control");
      const targetSet = new Set(targets);
      const ownerOfTarget = new Map([...wanted].map(([record, cc]) => [cc, record]));
      const freed = [...wanted.keys()].map(record => current.get(record)).filter(cc => !targetSet.has(cc));
      const final = new Map(current);
      for (const [record, cc] of wanted) final.set(record, cc);
      for (const record of [...current.keys()].sort((a, b) => a - b)) {
        if (wanted.has(record) || !targetSet.has(current.get(record))) continue;
        const preferred = current.get(ownerOfTarget.get(current.get(record)));
        const replacement = freed.includes(preferred) ? preferred : freed[0];
        freed.splice(freed.indexOf(replacement), 1);
        final.set(record, replacement);
      }
      if (new Set(final.values()).size !== final.size) throw new Error(name + ": planned map is not one-to-one");
      for (const record of [...final.keys()].sort((a, b) => a - b)) {
        if (final.get(record) !== current.get(record)) {
          changes.push({ part: name, record, oldCC: current.get(record), newCC: final.get(record), requested: wanted.has(record) });
        }
      }
    }
    return changes;
  }

  function finalMaps(image, changes) {
    const maps = {};
    for (const name of PART_ORDER) maps[name] = forwardCCMap(image, PARTS[name]);
    for (const change of changes) maps[change.part].set(change.record, change.newCC);
    return maps;
  }

  function newReverse(forward) {
    const reverse = new Array(128).fill(UNMAPPED);
    for (const [record, cc] of forward) reverse[cc] = record;
    return reverse;
  }

  function expectedOffsets(image, changes) {
    const offsets = new Set();
    for (const change of changes) offsets.add(off(PARTS[change.part].forward + RECORD_SIZE * change.record + 7));
    const maps = finalMaps(image, changes);
    for (const name of PART_ORDER) {
      const part = PARTS[name];
      const before = reverseTable(image, part);
      const after = newReverse(maps[name]);
      for (let cc = 0; cc < 128; cc++) {
        const slot = off(part.reverse + 2 * cc);
        if ((before[cc] & 0xFF) !== (after[cc] & 0xFF)) offsets.add(slot);
        if ((before[cc] >> 8) !== (after[cc] >> 8)) offsets.add(slot + 1);
      }
    }
    return [...offsets].sort((a, b) => a - b);
  }

  function applyRemap(image, changes) {
    checkLayout(image);
    const patched = new Uint8Array(image);
    const maps = finalMaps(image, changes);
    if (hasPerformanceControls(image) && [...maps.synth.values()].includes(1)) {
      throw new Error("synth CC 1 is reserved for the performance build's mod-wheel source");
    }
    for (const name of PART_ORDER) {
      const part = PARTS[name];
      for (const [record, cc] of maps[name]) patched[off(part.forward + RECORD_SIZE * record + 7)] = cc;
      const at = off(part.reverse);
      newReverse(maps[name]).forEach((record, cc) => {
        patched[at + 2 * cc] = record & 0xFF;
        patched[at + 2 * cc + 1] = record >> 8;
      });
    }
    checkLayout(patched);
    const expected = expectedOffsets(image, changes);
    const differing = [];
    for (let i = 0; i < image.length; i++) if (image[i] !== patched[i]) differing.push(i);
    if (!sameBytes(differing, expected)) throw new Error("patched image differs outside the planned table bytes");
    return patched;
  }

  /* ---------- Page helpers ---------- */

  // Build a remapped SysEx from the selected uploader SysEx. Synchronous;
  // hash the result with sha256Hex before trusting it.
  function buildRemappedSysex(baseSysex, requests) {
    const { image: base, messages } = decodeFirmware(baseSysex);
    if (!sameBytes(encodeFirmware(base, messages, baseSysex), baseSysex)) throw new Error("base SysEx does not round-trip");
    checkLayout(base);
    checkStockCCs(base);
    const changes = planRemap(base, requests);
    const image = applyRemap(base, changes);
    const sysex = encodeFirmware(image, messages, baseSysex);
    if (sysex.length !== baseSysex.length || !sameBytes(decodeFirmware(sysex).image, image)) {
      throw new Error("remapped SysEx failed the fixed-size round trip");
    }
    return { sysex, image, changes };
  }

  function uiControls(image) {
    const synth = forwardCCMap(image, PARTS.synth);
    const drums = forwardCCMap(image, PARTS.drums);
    const controls = [];
    for (let n = 1; n <= 8; n++) {
      const record = SYNTH_MACRO_FIRST_RECORD + SYNTH_MACRO_STRIDE * (n - 1);
      controls.push({ id: "synth.macro" + n, group: "synth", part: "synth", record, label: "Macro " + n, stock: synth.get(record) });
    }
    for (let drum = 1; drum <= 4; drum++) {
      for (const param of DRUM_KNOBS) {
        const record = (drum - 1) * DRUM_PARAMS.length + DRUM_PARAMS.indexOf(param);
        const name = param === "eq" ? "filter" : param;
        controls.push({ id: "drum" + drum + "." + param, group: "drums", part: "drums", record, label: "Drum " + drum + " " + name, stock: drums.get(record) });
      }
    }
    return controls;
  }

  // Name of the parameter a change moves, without the part prefix:
  // "macro 2", "filter frequency", "Drum 2 pitch".
  function parameterName(change) {
    if (change.part === "synth") {
      const macro = (change.record - SYNTH_MACRO_FIRST_RECORD) / SYNTH_MACRO_STRIDE;
      if (Number.isInteger(macro) && macro >= 0 && macro < 8) return "macro " + (macro + 1);
      return SYNTH_CC_NAMES[change.oldCC] || "parameter on CC " + change.oldCC;
    }
    if (change.part === "drums") {
      const param = DRUM_PARAMS[change.record % DRUM_PARAMS.length];
      return "Drum " + (Math.floor(change.record / DRUM_PARAMS.length) + 1) + " " + (param === "eq" ? "filter" : param);
    }
    return "session parameter on CC " + change.oldCC;
  }

  function describeChange(change) {
    const name = parameterName(change);
    return change.part === "synth" ? "Synth " + name : name.charAt(0).toUpperCase() + name.slice(1);
  }

  async function sha256Hex(bytes) {
    const digest = await root.crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
  }

  const api = {
    PARTS, PRESETS, REFERENCE_BUILDS, EXTENDED_V050_SYSEX_SHA256, EXTENDED_V050_IMAGE_SHA256,
    PERF_V2_REFERENCE_BUILDS, PERF_V2_SYSEX_SHA256, PERF_V2_IMAGE_SHA256, hasPerformanceControls,
    reservedReason, isAssignable, splitMessages, decodeFirmware, encodeFirmware,
    checkLayout, checkStockCCs, forwardCCMap, resolveControl, planRemap, applyRemap,
    expectedOffsets, buildRemappedSysex, uiControls, parameterName, describeChange, sha256Hex,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CircuitCC = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
