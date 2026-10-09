"""Emulated verification of the pitch bend / mod wheel / aftertouch receive patch.

    python performance/verify_circuit_perf_controls_v2.py build/circuit-perf-controls-v2/feature/manifest.json

Runs the real MIDI receive dispatcher (0x0802048C) of the base image and of
the patched image on every channel message (all statuses, all 16 channels,
edge data values), under several Rx-enable settings and channel layouts.
Downstream calls are stubbed and recorded.  The patched trace must equal the
base trace, except that each handled message (0xEn, 0xDn, CC 1 on the Synth 1
or Synth 2 channel, with Note Rx on) adds exactly one DSP setter call first,
with the expected address and value.  A placement-only build must match the
base exactly.
"""

from __future__ import annotations

import argparse
import itertools
import json
import struct
import sys
from pathlib import Path


ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(ANALYSIS_DIR))

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

import circuit_perf_controls_v2_patch as patch
from circuit_fw_tools import decode_firmware


FLASH_BASE = 0x08000000
FLASH_SIZE = 0x00040000
RAM_BASE = 0x20000000
RAM_SIZE = 0x00020000
STUBS = 0x08006000  # below the image: sentinel entry points
STUB_COUNT = 64
RETURN = 0x08007000
STACK = 0x2001EF00

CTX = 0x20010000
OBJ = 0x20010100
OBJC = 0x20010200
CHANNELS_Q = 0x20010300
VTABLE = 0x20010400
MESSAGE = 0x20010500


class Dispatcher:
    def __init__(self, image: bytes):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE)
        self.uc.mem_write(patch.BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.trace: list[tuple] = []
        self.uc.hook_add(UC_HOOK_CODE, self._stub, begin=STUBS, end=STUBS + 8 * STUB_COUNT)
        self.uc.hook_add(UC_HOOK_CODE, self._setter, begin=patch.DSP_SETTER, end=patch.DSP_SETTER)

    def _return(self) -> None:
        self.uc.reg_write(UC_ARM_REG_PC, self.uc.reg_read(UC_ARM_REG_LR))

    def _stub(self, uc: Uc, address: int, size: int, _: object) -> None:
        sp = uc.reg_read(UC_ARM_REG_SP)
        stack_word = struct.unpack("<I", bytes(uc.mem_read(sp, 4)))[0]
        regs = tuple(uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3))
        self.trace.append(("call", (address - STUBS) // 8) + regs + (stack_word,))
        self._return()

    def _setter(self, uc: Uc, address: int, size: int, _: object) -> None:
        self.trace.append(("dsp", uc.reg_read(UC_ARM_REG_R0) & 0xFFFFFFFF, uc.reg_read(UC_ARM_REG_R1) & 0xFFFF))
        self._return()

    def reset_ram(self, flags: int, note_channels: tuple[int, int, int], cc_channels: tuple[int, int, int, int]) -> None:
        self.uc.mem_write(RAM_BASE, bytes(RAM_SIZE))
        vtable = b"".join(struct.pack("<I", (STUBS + 8 * k) | 1) for k in range(STUB_COUNT))
        self.uc.mem_write(VTABLE, vtable)
        self.uc.mem_write(OBJ, struct.pack("<I", VTABLE) * 64)  # every member is a vtable pointer
        self.uc.mem_write(OBJC, struct.pack("<II", OBJ, CHANNELS_Q))
        self.uc.mem_write(CHANNELS_Q + 3, bytes(cc_channels[:3]))
        ctx = bytearray(0x14)
        ctx[0:4] = struct.pack("<I", OBJ)
        ctx[4:7] = bytes(note_channels)
        ctx[7:11] = bytes(cc_channels)
        ctx[0x0C:0x10] = struct.pack("<I", OBJC)
        ctx[0x10] = flags
        self.uc.mem_write(CTX, bytes(ctx))

    def run(self, message: bytes) -> list[tuple]:
        self.trace = []
        self.uc.mem_write(MESSAGE, message)
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_R0, 0)
        self.uc.reg_write(UC_ARM_REG_R1, MESSAGE)
        self.uc.reg_write(UC_ARM_REG_R2, CTX)
        self.uc.reg_write(UC_ARM_REG_LR, RETURN | 1)
        self.uc.emu_start(patch.RX_DISPATCHER | 1, RETURN, count=5000)
        pc = self.uc.reg_read(UC_ARM_REG_PC) & ~1
        if pc != RETURN:
            raise AssertionError(f"dispatcher did not return (stopped at {pc:#010x}) for {message.hex()}")
        return self.trace


FLAGS = (0x0F, 0x0E, 0x0D, 0x0B, 0x07, 0x01, 0x03, 0x00)
LAYOUTS = (
    ((0, 1, 9), (0, 1, 9, 15)),  # factory defaults
    ((3, 7, 9), (3, 7, 9, 15)),
    ((9, 0, 1), (9, 0, 1, 15)),
    ((0, 0, 9), (0, 0, 9, 15)),  # both synths on one channel: Synth 1 wins
    ((0, 1, 9), (5, 6, 9, 15)),  # CC channels differ from note channels
)
DATA1 = (0, 1, 2, 11, 63, 64, 99, 123, 127)
DATA2 = (0, 1, 63, 64, 127)


def expected_extra(status: int, d1: int, d2: int, flags: int, notes: tuple[int, int, int]) -> list[tuple]:
    kind, channel = status & 0xF0, status & 0x0F
    if not flags & 1 or not 0x80 <= status <= 0xEF:
        return []
    if not (kind in (0xE0, 0xD0) or (kind == 0xB0 and d1 == 1)):
        return []
    if channel == notes[0]:
        synth = 0
    elif channel == notes[1]:
        synth = 1
    else:
        return []
    return [("dsp", patch.expected_value(status, d1, d2), patch.expected_address(status, d1, synth))]


def compare(base: bytes, built: bytes, hooked: bool) -> dict:
    stock_run, new_run = Dispatcher(base), Dispatcher(built)
    cases = handled = 0
    statuses = list(range(0x80, 0xF0)) + [0xF1, 0xF2, 0xF3, 0xF6, 0xF8, 0xFA, 0xFB, 0xFC, 0xFE, 0xFF]
    for flags, (notes, ccs) in itertools.product(FLAGS, LAYOUTS):
        for status in statuses:
            pairs = [(0, 0)] if status >= 0xF0 else itertools.product(DATA1, DATA2)
            for d1, d2 in pairs:
                message = bytes((status, d1, d2))
                stock_run.reset_ram(flags, notes, ccs)
                new_run.reset_ram(flags, notes, ccs)
                want = stock_run.run(message)
                got = new_run.run(message)
                extra = expected_extra(status, d1, d2, flags, notes) if hooked else []
                if got != extra + want:
                    raise AssertionError(
                        f"mismatch flags={flags:#04x} notes={notes} msg={message.hex(' ')}\n"
                        f"  base:    {want}\n  patched: {got}\n  expected extra: {extra}"
                    )
                cases += 1
                handled += bool(extra)
    return {"cases": cases, "handled": handled}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    folder = args.manifest.parent
    base_sysex = (folder / manifest["base"]["copy"]).read_bytes()
    built_sysex = (folder / manifest["output"]["sysex"]).read_bytes()
    if patch.sha256(base_sysex) != manifest["base"]["sysex_sha256"]:
        raise SystemExit("base SysEx hash does not match the manifest")
    if patch.sha256(built_sysex) != manifest["output"]["sysex_sha256"]:
        raise SystemExit("built SysEx hash does not match the manifest")
    base, _ = decode_firmware(base_sysex)
    built, _ = decode_firmware(built_sysex)
    hooked = manifest["patch"]["hook_applied"]

    # Static: rebuild from the base and require byte equality.
    rebuilt, _ = patch.apply(base, hook=hooked)
    if rebuilt != built:
        raise SystemExit("built image is not reproducible from the base")
    changed = [i for i in range(len(base)) if base[i] != built[i]]
    print(f"static: {len(changed)} bytes differ from base; image reproducible")

    result = compare(base, built, hooked)
    print(f"emulated dispatcher: {result['cases']} messages identical to base"
          + (f", {result['handled']} with the expected extra DSP write" if hooked else ""))

    # Negative control: a wrong expectation must be caught.
    if hooked:
        bad = Dispatcher(built)
        bad.reset_ram(0x0F, (0, 1, 9), (0, 1, 9, 15))
        trace = bad.run(bytes((0xE0, 0x00, 0x40)))
        if trace != [("dsp", 0, 0x02D7)]:
            raise SystemExit(f"centre bend did not write 0 to X:$2D7: {trace}")
        trace = bad.run(bytes((0xE1, 0x7F, 0x7F)))
        if trace != [("dsp", (8191 << 18) & 0xFFFFFFFF, 0x0449)]:
            raise SystemExit(f"Synth 2 full bend wrong: {trace}")
        print("spot checks: centre bend -> X:$2D7 = 0; Synth 2 full up -> X:$449 = 0x7FFC00")
        from verify_circuit_perf_dsp_targets import verify_dsp_targets
        print(f"DSP target regression: {verify_dsp_targets(built)}")
    print("OK")


if __name__ == "__main__":
    main()
