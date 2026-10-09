"""Static and Thumb-emulated verification of a Circuit CC-remap build.

    python cc-remap\\verify_circuit_cc_remap.py build\\cc-remap\\nts1\\manifest.json

Runs the stock transmit routine (0x08015618) for every CC control of every
part and the stock receive lookup (0x08014AEC) for all 128 CC numbers, on both
the base and the remapped image, and checks each result against the manifest.
Also checks the planner's edge cases, the preset reference hashes, and that
docs/cc-remap.js declares the same tables and reference hashes.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

CC_REMAP_DIR = Path(__file__).resolve().parent
ROOT = CC_REMAP_DIR.parent
sys.path.insert(0, str(CC_REMAP_DIR))
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_MODE_THUMB, Uc
from unicorn.arm_const import (
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
    UC_ARM_REG_SP,
)

from circuit_cc_remap_patch import (
    ASSIGNABLE_CCS,
    BASE,
    CC_SEND_HELPER,
    EXTENDED_V050_IMAGE_SHA256,
    EXTENDED_V050_SYSEX_SHA256,
    PERF_V2_IMAGE_SHA256,
    PERF_V2_SYSEX_SHA256,
    PERF_V2_REFERENCE_BUILDS,
    DETENTS_IMAGE_SHA256,
    DETENTS_SYSEX_SHA256,
    DETENTS_REFERENCE_BUILDS,
    has_performance_controls,
    MIDI_MESSAGE_SEND,
    PARTS,
    PRESETS,
    REFERENCE_BUILDS,
    RX_LOOKUP,
    TX_ROUTINE,
    UNMAPPED,
    Change,
    apply_remap,
    check_layout,
    expected_offsets,
    forward_cc_map,
    plan_remap,
    reverse_table,
    sha256,
)
from circuit_fw_tools import decode_firmware, encode_firmware


BROWSER_MODULE = ROOT / "docs" / "cc-remap.js"
UPLOADER_BASE = ROOT / "docs" / "firmware" / "circuit-3592-filter-lfo-shift-automation.syx"

FLASH_BASE = 0x08000000
FLASH_SIZE = 0x00040000
RAM_BASE = 0x20000000
RAM_SIZE = 0x00020000
STACK = 0x2001EF00
RETURN = 0x08007000

# Fake objects in RAM that stand in for the firmware's MIDI context.
TX_THIS = 0x20001000
TX_MIDI = 0x20001100
TX_SETTINGS_POINTER = 0x20001200
TX_SETTINGS = 0x20001300
PORT_MASK = 0x03
CC_TX_ENABLED = 0x02
RX_THIS = 0x20002000
RX_OBJECT = 0x20002100
RX_VTABLE = 0x20002200
RX_SETTER = 0x20003000


class MidiEmulator:
    """Drive the stock transmit and receive routines against one image."""

    def __init__(self, image: bytes):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE)
        self.uc.mem_write(BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.uc.mem_write(TX_THIS + 0x08, struct.pack("<I", TX_MIDI))
        self.uc.mem_write(TX_THIS + 0x0C, bytes((PORT_MASK,)))
        self.uc.mem_write(TX_MIDI + 0x10, struct.pack("<I", TX_SETTINGS_POINTER))
        self.uc.mem_write(TX_MIDI + 0x18, bytes((CC_TX_ENABLED,)))
        self.uc.mem_write(TX_SETTINGS_POINTER, struct.pack("<I", TX_SETTINGS))
        # Channel slots: Synth 1, Synth 2, Drums, Session (zero based).
        self.channels = [0, 1, 9, 15]
        self.uc.mem_write(TX_SETTINGS + 3, bytes(self.channels))
        self.uc.mem_write(RX_THIS, struct.pack("<I", RX_OBJECT))
        self.uc.mem_write(RX_OBJECT + 4, struct.pack("<I", RX_VTABLE))
        self.uc.mem_write(RX_VTABLE, struct.pack("<I", RX_SETTER | 1))
        self.events: list[tuple] = []
        self.uc.hook_add(UC_HOOK_CODE, self._hook)

    def _return(self, uc: Uc) -> None:
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _hook(self, uc: Uc, address: int, size: int, _: object) -> None:
        if address == MIDI_MESSAGE_SEND:
            message = bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_R1), 3))
            self.events.append(("send", uc.reg_read(UC_ARM_REG_R0) & 0xFF, message))
            self._return(uc)
        elif address == CC_SEND_HELPER:
            registers = (UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
            self.events.append(("helper",) + tuple(uc.reg_read(reg) & 0xFF for reg in registers))
            self._return(uc)
        elif address == RX_SETTER:
            registers = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
            self.events.append(("set",) + tuple(uc.reg_read(reg) for reg in registers))
            self._return(uc)

    def _call(self, entry: int, registers: dict[int, int], stack_word: int = 0) -> list[tuple]:
        self.events = []
        self.uc.mem_write(STACK, struct.pack("<I", stack_word))
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        for register, value in registers.items():
            self.uc.reg_write(register, value)
        self.uc.reg_write(UC_ARM_REG_LR, RETURN | 1)
        self.uc.emu_start(entry | 1, RETURN, count=2000)
        pc = self.uc.reg_read(UC_ARM_REG_PC) & ~1
        if pc != RETURN:
            raise AssertionError(f"emulation of {entry:#010x} stopped at {pc:#010x}")
        return self.events

    def transmit(self, part_id: int, record: int, value: int) -> list[tuple]:
        return self._call(
            TX_ROUTINE,
            {UC_ARM_REG_R0: TX_THIS, UC_ARM_REG_R1: part_id, UC_ARM_REG_R2: record, UC_ARM_REG_R3: value},
        )

    def receive(self, part_id: int, cc: int, value: int, table: int) -> list[tuple]:
        return self._call(
            RX_LOOKUP,
            {UC_ARM_REG_R0: RX_THIS, UC_ARM_REG_R1: part_id, UC_ARM_REG_R2: cc, UC_ARM_REG_R3: table},
            stack_word=value,
        )


def emulate_maps(image: bytes, maps: dict[str, dict[int, int]]) -> tuple[int, int]:
    """Check transmit and receive behaviour of ``image`` against ``maps``."""
    emulator = MidiEmulator(image)
    transmitted = received = 0
    for part in PARTS.values():
        forward = maps[part.name]
        reverse = {cc: record for record, cc in forward.items()}
        for slot, part_id in enumerate(part.part_ids):
            channel = emulator.channels[part.channel_slot + slot]
            for record, cc in sorted(forward.items()):
                value = (record * 7 + part_id) & 0x7F
                events = emulator.transmit(part_id, record, value)
                expected = [("send", PORT_MASK, bytes((0xB0 | channel, cc, value)))]
                if events != expected:
                    raise AssertionError(f"{part.name} id {part_id:#x} record {record}: sent {events}, expected {expected}")
                transmitted += 1
            for cc in range(128):
                value = (cc * 5 + 3) & 0x7F
                events = emulator.receive(part_id, cc, value, part.reverse)
                record = reverse.get(cc)
                expected = [] if record is None else [("set", RX_OBJECT + 4, part_id, record, value)]
                if events != expected:
                    raise AssertionError(f"{part.name} id {part_id:#x} CC {cc}: set {events}, expected {expected}")
                received += 1
    return transmitted, received


def expect_error(base_image: bytes, requests: dict[str, int], fragment: str) -> None:
    try:
        plan_remap(base_image, requests)
    except ValueError as error:
        if fragment not in str(error):
            raise AssertionError(f"{requests}: wrong error {error!r}") from error
        return
    raise AssertionError(f"{requests}: accepted, expected an error containing {fragment!r}")


def planner_tests(base_image: bytes) -> int:
    stock = forward_cc_map(base_image, PARTS["synth"])
    ring_mod = next(record for record, cc in stock.items() if cc == 54)
    macro = {n: 174 + 17 * (n - 1) for n in range(1, 9)}

    def moves(requests: dict[str, int]) -> set[tuple[int, int, int, bool]]:
        return {(c.record, c.old_cc, c.new_cc, c.requested) for c in plan_remap(base_image, requests)}

    expected_moves = [
        (
            PRESETS["nts1"],
            {
                (macro[1], 80, 54, True),
                (ring_mod, 54, 80, False),
                (macro[2], 81, 55, True),
                (macro[5], 84, 43, True),
                (macro[6], 85, 44, True),
            },
        ),
        ({"synth.macro1": 81}, {(macro[1], 80, 81, True), (macro[2], 81, 80, False)}),
        ({"synth.macro1": 81, "synth.macro2": 80}, {(macro[1], 80, 81, True), (macro[2], 81, 80, True)}),
        (
            {"synth.macro1": 54, "synth.macro2": 80},
            {(macro[1], 80, 54, True), (macro[2], 81, 80, True), (ring_mod, 54, 81, False)},
        ),
        ({"synth.macro1": 80}, set()),
        ({"synth.macro1": 54, "synth.cc80": 54}, {(macro[1], 80, 54, True), (ring_mod, 54, 80, False)}),
    ]
    checks = 0
    for requests, expected in expected_moves:
        if moves(requests) != expected:
            raise AssertionError(f"{requests}: planned {moves(requests)}, expected {expected}")
        checks += 1
    if plan_remap(base_image, {"drum1.filter": 102}) != [Change("drums", 5, 17, 102, True)]:
        raise AssertionError("drum1.filter did not resolve to the Drum 1 EQ record")
    checks += 1
    for cc in (0, 6, 32, 38, 98, 99, 100, 101, 120, 121, 123, 127, 128, -1):
        expect_error(base_image, {"synth.macro1": cc}, "cannot be assigned")
        checks += 1
    for requests, fragment in (
        ({"synth.macro1": 54, "synth.macro2": 54}, "more than one control"),
        ({"synth.macro1": 54, "synth.cc80": 55}, "assigned twice"),
        ({"synth.macro9": 54}, "unknown control"),
        ({"drum1.snare": 54}, "unknown drum control"),
        ({"session.cc1": 54}, "no control on stock CC 1"),
    ):
        expect_error(base_image, requests, fragment)
        checks += 1
    for requests in ({"synth.macro1": 81}, {"synth.macro1": 54, "synth.macro2": 80}, {"drum4.pan": 119, "session.cc88": 1}):
        patched = apply_remap(base_image, plan_remap(base_image, requests))
        for part in PARTS.values():
            forward = forward_cc_map(patched, part)
            if len(set(forward.values())) != len(forward):
                raise AssertionError(f"{requests}: {part.name} map is not one-to-one")
        checks += 1
    return checks


def reference_tests() -> int:
    bases = (
        (UPLOADER_BASE, EXTENDED_V050_SYSEX_SHA256, EXTENDED_V050_IMAGE_SHA256, REFERENCE_BUILDS),
        (UPLOADER_BASE.with_name("circuit-3592-extended-v0.5.0-perf-v2-feature.syx"),
         PERF_V2_SYSEX_SHA256, PERF_V2_IMAGE_SHA256, PERF_V2_REFERENCE_BUILDS),
        (UPLOADER_BASE.with_name("circuit-3592-extended-v0.5.0-perf-v2-detents.syx"),
         DETENTS_SYSEX_SHA256, DETENTS_IMAGE_SHA256, DETENTS_REFERENCE_BUILDS),
    )
    checks = 0
    for path, sysex_hash, image_hash, references in bases:
        base_sysex = path.read_bytes()
        if sha256(base_sysex) != sysex_hash:
            raise AssertionError("uploader base SysEx changed")
        base_image, messages = decode_firmware(base_sysex)
        if sha256(base_image) != image_hash:
            raise AssertionError("uploader base image changed")
        if has_performance_controls(base_image):
            expect_error(base_image, {"synth.macro5": 1}, "reserved")
            expect_error(base_image, {"synth.cc74": 1}, "reserved")
        else:
            assert plan_remap(base_image, {"synth.macro5": 1})
        for preset, expected in references.items():
            sysex = encode_firmware(apply_remap(base_image, plan_remap(base_image, PRESETS[preset])), messages)
            if sha256(sysex) != expected:
                raise AssertionError(f"{path.name}: preset {preset} no longer matches its reference build")
            checks += 1
    return checks


def browser_module_tests() -> int:
    """Crude drift guard: the browser port must declare the same constants."""
    source = BROWSER_MODULE.read_text(encoding="utf-8").lower()
    needles = [EXTENDED_V050_SYSEX_SHA256, EXTENDED_V050_IMAGE_SHA256, *REFERENCE_BUILDS.values(),
               PERF_V2_SYSEX_SHA256, PERF_V2_IMAGE_SHA256, *PERF_V2_REFERENCE_BUILDS.values(),
               DETENTS_SYSEX_SHA256, DETENTS_IMAGE_SHA256, *DETENTS_REFERENCE_BUILDS.values()]
    for part in PARTS.values():
        needles += [f"{part.forward:#010x}", f"{part.reverse:#010x}", f"{part.rx_literal:#010x}"]
    missing = [needle for needle in needles if needle.lower() not in source]
    if missing:
        raise AssertionError(f"docs/cc-remap.js is missing {missing}")
    for preset, requests in PRESETS.items():
        for control, cc in requests.items():
            if not re.search(rf'"{re.escape(control)}"\s*:\s*{cc}\b', source):
                raise AssertionError(f"docs/cc-remap.js preset {preset} lacks {control}={cc}")
    return len(needles)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    manifest_path = args.manifest if args.manifest.exists() else ROOT / args.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_dir = manifest_path.parent
    base_sysex = Path(manifest["base"]["sysex"]).read_bytes()
    output_sysex = (output_dir / manifest["output"]["sysex"]).read_bytes()
    output_bin = (output_dir / manifest["output"]["image"]).read_bytes()
    if sha256(base_sysex) != manifest["base"]["sysex_sha256"]:
        raise AssertionError("base SysEx hash differs from the manifest")
    if sha256(output_sysex) != manifest["output"]["sysex_sha256"]:
        raise AssertionError("output SysEx hash differs from the manifest")
    if len(output_sysex) != len(base_sysex):
        raise AssertionError("output SysEx size differs from the base")

    base_image, base_messages = decode_firmware(base_sysex)
    image, messages = decode_firmware(output_sysex)
    if image != output_bin or sha256(image) != manifest["output"]["image_sha256"]:
        raise AssertionError("decoded output differs from the manifest image")
    if [m.command for m in messages] != [m.command for m in base_messages]:
        raise AssertionError("SysEx message structure changed")
    check_layout(base_image)
    check_layout(image)

    changes = [
        Change(entry["part"], entry["record"], entry["old_cc"], entry["new_cc"], entry["requested"])
        for entry in manifest["changes"]
    ]
    if plan_remap(base_image, manifest["requested"]) != changes:
        raise AssertionError("manifest changes do not match a fresh plan")
    if apply_remap(base_image, changes) != image:
        raise AssertionError("a fresh build does not reproduce the output image")
    differing = [index for index, (a, b) in enumerate(zip(base_image, image)) if a != b]
    if differing != expected_offsets(base_image, changes) or differing != manifest["changed_offsets"]:
        raise AssertionError("output differs outside the planned table bytes")

    base_maps = {name: forward_cc_map(base_image, part) for name, part in PARTS.items()}
    maps = {name: forward_cc_map(image, part) for name, part in PARTS.items()}
    for name, forward in maps.items():
        if {str(r): cc for r, cc in sorted(forward.items())} != manifest["final_cc_maps"][name]:
            raise AssertionError(f"{name}: final map differs from the manifest")
        moved = {r for r in forward if forward[r] != base_maps[name][r]}
        if any(forward[r] not in ASSIGNABLE_CCS for r in moved):
            raise AssertionError(f"{name}: a moved control landed on a reserved CC")
        reverse = reverse_table(image, PARTS[name])
        if sum(index != UNMAPPED for index in reverse) != len(forward):
            raise AssertionError(f"{name}: reverse table is not one-to-one with the CC records")

    base_tx, base_rx = emulate_maps(base_image, base_maps)
    out_tx, out_rx = emulate_maps(image, maps)
    tests = planner_tests(base_image)
    references = reference_tests()
    constants = browser_module_tests()

    print(f"base {manifest['base']['name']}: {base_tx} transmit + {base_rx} receive emulations match the stock map")
    print(f"output: {out_tx} transmit + {out_rx} receive emulations match the remapped map")
    print(f"changed bytes: {len(differing)} (all inside the CC tables)")
    print(f"planner tests: {tests} passed; preset reference builds: {references} matched")
    print(f"browser module: {constants} shared constants present")
    for entry in manifest["changes"]:
        print(f"  {entry['control']:32s} CC {entry['old_cc']:3d} -> {entry['new_cc']:3d}")
    print("PASS")


if __name__ == "__main__":
    main()
