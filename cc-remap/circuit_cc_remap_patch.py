"""Remap the original Circuit's MIDI CC numbers (firmware 1.8 build 3592).

Every CC the Circuit sends or answers comes from constant tables in flash:

* Forward (transmit) tables hold one 8-byte record per parameter:
  ``[param_lo, param_hi, type, offset, min, max, b6, b7]``.  ``b6 == 0xFF``
  marks a CC record whose number is ``b7``; any other ``b6`` is an NRPN.
  The transmit routine 0x08015618 sends ``0xB0|channel, b7, value``.
* Reverse (receive) tables hold 128 little-endian int16 record indexes, one
  per CC number, with 0x8000 meaning unmapped.  The receive lookup
  0x08014AEC indexes them after the dispatcher has consumed CC 6, 38, 98-101
  (NRPN/RPN), 121 and 123, and ignored CC 120 and above.

A remap rewrites ``b7`` of each moved record and the matching reverse-table
slots.  It adds no code, hooks, or free-space use, and the image size is
unchanged.  ``docs/cc-remap.js`` is the browser port of this module and must
produce byte-identical images.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80
RECORD_SIZE = 8
CC_MARKER = 0xFF
UNMAPPED = 0x8000

STOCK_IMAGE_SHA256 = "1a424d4f116c9b76c3e4e9c1cfa1abb3e262f7d120529c23992e7ee047e1f1ee"
STOCK_SYSEX_SHA256 = "260a72ebd10208aae44f7c01ad18a79cf1d7ad32658ecd1dee0d5215c0e6b7c0"
EXTENDED_V050_IMAGE_SHA256 = "1a3e6593e5cff6ec415b070fd1f93c618637f0520dae3af54b4d82a07c53d22e"
EXTENDED_V050_SYSEX_SHA256 = "7ea9affe4c5310a8c3d84abf6c05b1ee35d4ef9ee6d30bb040711a4eb047745f"

PERF_V2_SYSEX_SHA256 = "085f4decb43efb3c2738a1ef08851e42939c6e77c10bdb334ba70d70babf0d2e"
PERF_V2_IMAGE_SHA256 = "4355394aa331989dc61770f6b3e56ab215c2be9fbab5d0a241ea02c75ea7e225"

TX_ROUTINE = 0x08015618
TX_TABLE_POINTERS = 0x0802E730
RX_LOOKUP = 0x08014AEC
MIDI_MESSAGE_SEND = 0x08013464
CC_SEND_HELPER = 0x08013546

# CC numbers that must never be assigned.  The dispatcher consumes the
# NRPN/RPN numbers before the reverse table is consulted; 0 and 32 are Bank
# Select, which external gear acts on; 120-127 are channel mode messages.
RESERVED_CCS = {
    0: "Bank Select MSB",
    6: "Data Entry MSB (NRPN/RPN)",
    32: "Bank Select LSB",
    38: "Data Entry LSB (NRPN/RPN)",
    98: "NRPN LSB",
    99: "NRPN MSB",
    100: "RPN LSB",
    101: "RPN MSB",
}
ASSIGNABLE_CCS = frozenset(cc for cc in range(120) if cc not in RESERVED_CCS)


@dataclass(frozen=True)
class Part:
    name: str
    forward: int
    count: int
    reverse: int
    rx_literal: int
    part_ids: tuple[int, ...]
    channel_slot: int
    default_channel: int


PARTS = {
    "synth": Part("synth", 0x0802DBC0, 310, 0x08035340, 0x0802073C, (0x00, 0x01), 0, 1),
    "drums": Part("drums", 0x0802E810, 28, 0x08035240, 0x08020740, (0x10,), 2, 10),
    "session": Part("session", 0x0802D9CC, 38, 0x08035480, 0x08020744, (0x20,), 3, 16),
}

SYNTH_MACRO_FIRST_RECORD = 174
SYNTH_MACRO_STRIDE = 17
DRUM_PARAMS = ("patch", "level", "pitch", "decay", "distortion", "eq", "pan")
DRUM_PARAM_ALIASES = {"filter": "eq"}

# Stock synth CC names as written in the Circuit Programmer's Reference Guide
# (checked against each record's value range).  Used only for readable
# listings and manifests; docs/cc-remap.js carries the same names.
SYNTH_CC_NAMES = {
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
}

# Fingerprints of the stock layout; a base that fails these is refused.
STOCK_CC_FINGERPRINT = {
    "synth": {SYNTH_MACRO_FIRST_RECORD + SYNTH_MACRO_STRIDE * n: 80 + n for n in range(8)},
    "drums": {0: 8, 6: 77, 7: 18, 20: 79, 27: 80},
    "session": {0: 88, 22: 74, 23: 71, 37: 118},
}

PRESETS = {
    # Korg NTS-1: Macro 1/2 (oscillator by Circuit convention) -> OSC SHAPE/ALT,
    # Macro 5/6 (filter by Circuit convention) -> FILTER CUTOFF/RESONANCE.
    "nts1": {
        "synth.macro1": 54,
        "synth.macro2": 55,
        "synth.macro5": 43,
        "synth.macro6": 44,
    },
}

# Known-answer SysEx hashes for presets on the v0.5.0 base, shared with the
# browser uploader so both implementations are pinned to the same bytes.
REFERENCE_BUILDS = {
    "nts1": "d4e63fadfd10a532bb61c56673da4dfc7fb8b426745f3493efdde2853d764f0a",
}


PERF_V2_REFERENCE_BUILDS = {"nts1": "4fcf2c276cf49885d4e40c4d6a5727f872bd0b43a5da40822a3423fe785eadfd"}


@dataclass(frozen=True)
class Change:
    part: str
    record: int
    old_cc: int
    new_cc: int
    requested: bool


def offset(address: int) -> int:
    return address - BASE


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_bytes(image: bytes, part: Part, record: int) -> bytes:
    start = offset(part.forward + RECORD_SIZE * record)
    return image[start : start + RECORD_SIZE]


def forward_cc_map(image: bytes, part: Part) -> dict[int, int]:
    """Return {record index: CC number} for the part's CC records."""
    result: dict[int, int] = {}
    for record in range(part.count):
        raw = record_bytes(image, part, record)
        if raw[6] == CC_MARKER:
            result[record] = raw[7]
    return result


def reverse_table(image: bytes, part: Part) -> list[int]:
    return list(struct.unpack_from("<128H", image, offset(part.reverse)))


def check_layout(image: bytes) -> None:
    """Refuse any base whose table structure differs from build 3592."""
    if len(image) != IMAGE_SIZE:
        raise ValueError(f"image is {len(image)} bytes, expected {IMAGE_SIZE}")
    for part in PARTS.values():
        for part_id in part.part_ids:
            pointer = struct.unpack_from("<I", image, offset(TX_TABLE_POINTERS + 4 * part_id))[0]
            if pointer != part.forward:
                raise ValueError(f"{part.name}: transmit pointer {part_id:#x} is {pointer:#010x}")
        literal = struct.unpack_from("<I", image, offset(part.rx_literal))[0]
        if literal != part.reverse:
            raise ValueError(f"{part.name}: receive literal is {literal:#010x}")
        terminator = record_bytes(image, part, part.count)
        if terminator != bytes(RECORD_SIZE):
            raise ValueError(f"{part.name}: table is not terminated after {part.count} records")
        forward = forward_cc_map(image, part)
        reverse = reverse_table(image, part)
        for record, cc in forward.items():
            if cc > 119 or reverse[cc] != record:
                raise ValueError(f"{part.name}: record {record} CC {cc} has no matching reverse slot")
        mapped = {cc: index for cc, index in enumerate(reverse) if index != UNMAPPED}
        for cc, record in mapped.items():
            if record >= part.count or forward.get(record) != cc:
                raise ValueError(f"{part.name}: reverse slot CC {cc} -> {record} has no CC record")


def check_stock_ccs(image: bytes) -> None:
    for name, expected in STOCK_CC_FINGERPRINT.items():
        forward = forward_cc_map(image, PARTS[name])
        for record, cc in expected.items():
            if forward.get(record) != cc:
                raise ValueError(
                    f"{name}: record {record} is CC {forward.get(record)}, stock is CC {cc}; "
                    "build from an unremapped base"
                )


def control_names(part: Part, record: int) -> list[str]:
    names: list[str] = []
    if part.name == "synth":
        macro, remainder = divmod(record - SYNTH_MACRO_FIRST_RECORD, SYNTH_MACRO_STRIDE)
        if remainder == 0 and 0 <= macro < 8:
            names.append(f"synth.macro{macro + 1}")
    elif part.name == "drums":
        drum, param = divmod(record, len(DRUM_PARAMS))
        names.append(f"drum{drum + 1}.{DRUM_PARAMS[param]}")
    return names


def describe(part: Part, record: int, stock_cc: int) -> str:
    names = control_names(part, record)
    if names:
        return names[0]
    label = f"{part.name}.cc{stock_cc}"
    if part.name == "synth" and stock_cc in SYNTH_CC_NAMES:
        label += f" ({SYNTH_CC_NAMES[stock_cc]})"
    return label


def resolve_control(image: bytes, name: str) -> tuple[Part, int]:
    """Map a control name to (part, record).

    Accepted forms: ``synth.macro1``..``synth.macro8``,
    ``drum1.pitch`` (patch, level, pitch, decay, distortion, eq/filter, pan),
    and ``<part>.cc<N>`` for whatever sends stock CC N on that part.
    """
    text = name.strip().lower()
    match = re.fullmatch(r"synth\.macro([1-8])", text)
    if match:
        macro = int(match.group(1)) - 1
        return PARTS["synth"], SYNTH_MACRO_FIRST_RECORD + SYNTH_MACRO_STRIDE * macro
    match = re.fullmatch(r"drum([1-4])\.([a-z]+)", text)
    if match:
        param = DRUM_PARAM_ALIASES.get(match.group(2), match.group(2))
        if param not in DRUM_PARAMS:
            raise ValueError(f"unknown drum control {name!r}; use one of {', '.join(DRUM_PARAMS)}")
        return PARTS["drums"], (int(match.group(1)) - 1) * len(DRUM_PARAMS) + DRUM_PARAMS.index(param)
    match = re.fullmatch(r"(synth|drums|session)\.cc(\d{1,3})", text)
    if match:
        part = PARTS[match.group(1)]
        stock_cc = int(match.group(2))
        for record, cc in forward_cc_map(image, part).items():
            if cc == stock_cc:
                return part, record
        raise ValueError(f"{part.name} has no control on stock CC {stock_cc}")
    raise ValueError(f"unknown control {name!r}")


def parse_assignment(text: str) -> tuple[str, int]:
    if "=" not in text:
        raise ValueError(f"expected CONTROL=CC, got {text!r}")
    control, value = text.split("=", maxsplit=1)
    return control.strip(), int(value, 0)


def has_performance_controls(image: bytes) -> bool:
    return image[offset(0x08020508):offset(0x08020508) + 4] == bytes.fromhex("e7f76cbe")


def plan_remap(image: bytes, requests: dict[str, int]) -> list[Change]:
    """Plan the moves for the requested {control: new CC} assignments.

    A control that already owns a requested CC on the same part is displaced
    to a number the requested controls gave up, preferring a direct swap, so
    each part keeps a one-to-one CC map and nothing is silently lost.
    """
    by_part: dict[str, dict[int, int]] = {}
    for control, cc in requests.items():
        if cc not in ASSIGNABLE_CCS:
            reason = RESERVED_CCS.get(cc, "channel mode message" if 120 <= cc <= 127 else "not a CC number")
            raise ValueError(f"{control}: CC {cc} cannot be assigned ({reason})")
        part, record = resolve_control(image, control)
        if cc == 1 and part.name == "synth" and has_performance_controls(image):
            raise ValueError("synth CC 1 is reserved for the performance build's mod-wheel source")
        if record not in forward_cc_map(image, part):
            raise ValueError(f"{control}: record {record} is not a CC control")
        wanted = by_part.setdefault(part.name, {})
        if wanted.get(record, cc) != cc:
            raise ValueError(f"{control}: assigned twice with different CC numbers")
        wanted[record] = cc

    changes: list[Change] = []
    for name, wanted in by_part.items():
        part = PARTS[name]
        current = forward_cc_map(image, part)
        targets = list(wanted.values())
        duplicates = sorted({cc for cc in targets if targets.count(cc) > 1})
        if duplicates:
            raise ValueError(f"{name}: CC {duplicates} requested for more than one control")
        target_set = set(targets)
        owner_of_target = {cc: record for record, cc in wanted.items()}
        freed = [current[record] for record in wanted if current[record] not in target_set]
        final = dict(current)
        final.update(wanted)
        for record in sorted(current):
            if record in wanted or current[record] not in target_set:
                continue
            preferred = current[owner_of_target[current[record]]]
            replacement = preferred if preferred in freed else freed[0]
            freed.remove(replacement)
            final[record] = replacement
        if len(set(final.values())) != len(final):
            raise AssertionError(f"{name}: planned map is not one-to-one")
        for record in sorted(final):
            if final[record] != current[record]:
                changes.append(Change(name, record, current[record], final[record], record in wanted))
    return changes


def final_maps(image: bytes, changes: list[Change]) -> dict[str, dict[int, int]]:
    maps = {name: forward_cc_map(image, part) for name, part in PARTS.items()}
    for change in changes:
        maps[change.part][change.record] = change.new_cc
    return maps


def expected_offsets(image: bytes, changes: list[Change]) -> list[int]:
    """File offsets a correct build may differ at, and must differ at."""
    offsets: set[int] = set()
    for change in changes:
        part = PARTS[change.part]
        offsets.add(offset(part.forward + RECORD_SIZE * change.record + 7))
    for name, forward in final_maps(image, changes).items():
        part = PARTS[name]
        old_reverse = reverse_table(image, part)
        new_reverse = [UNMAPPED] * 128
        for record, cc in forward.items():
            new_reverse[cc] = record
        for cc in range(128):
            if old_reverse[cc] != new_reverse[cc]:
                slot = offset(part.reverse + 2 * cc)
                old_bytes = struct.pack("<H", old_reverse[cc])
                new_bytes = struct.pack("<H", new_reverse[cc])
                offsets.update(slot + i for i in range(2) if old_bytes[i] != new_bytes[i])
    return sorted(offsets)


def apply_remap(image: bytes, changes: list[Change]) -> bytes:
    check_layout(image)
    if has_performance_controls(image) and 1 in final_maps(image, changes)["synth"].values():
        raise ValueError("synth CC 1 is reserved for the performance build's mod-wheel source")
    patched = bytearray(image)
    for name, forward in final_maps(image, changes).items():
        part = PARTS[name]
        for record, cc in forward.items():
            patched[offset(part.forward + RECORD_SIZE * record + 7)] = cc
        new_reverse = [UNMAPPED] * 128
        for record, cc in forward.items():
            new_reverse[cc] = record
        struct.pack_into("<128H", patched, offset(part.reverse), *new_reverse)
    result = bytes(patched)
    check_layout(result)
    differing = [index for index, (a, b) in enumerate(zip(image, result)) if a != b]
    if differing != expected_offsets(image, changes):
        raise AssertionError("patched image differs outside the planned table bytes")
    return result
