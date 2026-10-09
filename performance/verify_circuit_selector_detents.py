"""Static and Thumb-emulated verification of the Distortion Type detent patch.

    python performance/verify_circuit_selector_detents.py [PATCHED_SYSEX]

Without an argument the patch is applied to the published performance-v2
SysEx in memory.  Checks:

* only the Distortion wrapper slot and the recorder slot differ from the base;
  nothing else in the image loads the retired literal at 0x08025D58;
* the live Distortion wrapper, run from each drum's entry stub, matches a
  reference model over long random event streams: three moving events per
  step, wrap 0..6, Shift-released and unchanged events leave type and
  divider untouched, and the stored amount is restored through the real
  restore helper;
* the recorder's Distortion tag equals the type the wrapper produces for the
  same event and state (recorder runs first on hardware);
* every other recorder input gives the same result as the base image.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_MODE_THUMB, Uc
from unicorn.arm_const import (
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
    UC_ARM_REG_R4,
    UC_ARM_REG_R5,
    UC_ARM_REG_R6,
    UC_ARM_REG_R7,
    UC_ARM_REG_SP,
)

import circuit_selector_detents_patch as patch
from circuit_fw_tools import decode_firmware


PERF_V2_SYSEX = REPO_ROOT / "docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-feature.syx"

FLASH_BASE = 0x08000000
FLASH_SIZE = 0x00040000
RAM_BASE = 0x20000000
RAM_SIZE = 0x00020000
STACK = 0x2001EF00
SENTINEL = 0x08007300

# Parameter-store layout walked by the real restore helper at 0x0802D4EC.
CONTEXT_POINTER = 0x20002DB0
CONTEXT = 0x20010000
OWNER = 0x20010400
INDEX_TABLE = 0x20010800
STORE_POINTER = 0x20010C00
STORE = 0x20011000

ENTRY_PRESERVED = (UC_ARM_REG_R1, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7)


class Machine:
    """One firmware image with mocked stock services and a modelled DSP."""

    def __init__(self, image: bytes):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE)
        self.uc.mem_write(patch.BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.dsp: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []
        self.shift = False
        self.proposal = 0
        self.uc.hook_add(UC_HOOK_CODE, self._hook)
        w = lambda a, v: self.uc.mem_write(a, v.to_bytes(4, "little"))
        w(CONTEXT_POINTER, CONTEXT)
        w(CONTEXT + 0x318, OWNER)
        w(OWNER + 4, INDEX_TABLE)
        w(OWNER + 0x0C, STORE_POINTER)
        w(STORE_POINTER, STORE)
        for index in range(0x20):
            self.uc.mem_write(INDEX_TABLE + index * 8, (0x40 + index).to_bytes(2, "little"))

    def stored_byte(self, parameter: int) -> int:
        return self.uc.mem_read(STORE + 0x40 + parameter, 1)[0]

    def set_stored_byte(self, parameter: int, value: int) -> None:
        self.uc.mem_write(STORE + 0x40 + parameter, bytes((value,)))

    @staticmethod
    def _poison(uc: Uc, tag: int) -> None:
        uc.reg_write(UC_ARM_REG_R1, tag | 1)
        uc.reg_write(UC_ARM_REG_R2, tag | 2)
        uc.reg_write(UC_ARM_REG_R3, tag | 3)

    def _return(self, uc: Uc, r0: int) -> None:
        uc.reg_write(UC_ARM_REG_R0, r0 & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _hook(self, uc: Uc, address: int, size: int, _: object) -> None:
        if address == SENTINEL:
            uc.emu_stop()
        elif address == patch.STOCK_DRUM_CONTROL_GETTER:
            if uc.reg_read(UC_ARM_REG_R0) != 0x10:
                raise AssertionError("drum getter called with an unexpected selector")
            self._poison(uc, 0xDEAD0A00)
            self._return(uc, self.proposal)
        elif address == patch.STOCK_BUTTON_PRESSED:
            if uc.reg_read(UC_ARM_REG_R0) & 0xFF != patch.SHIFT_LOGICAL_ID:
                raise AssertionError("unexpected button query")
            self._poison(uc, 0xDEAD0B00)
            self._return(uc, int(self.shift))
        elif address == patch.DSP_GETTER:
            register = uc.reg_read(UC_ARM_REG_R0) & 0xFFFF
            self._poison(uc, 0xDEAD0C00)
            self._return(uc, self.dsp.get(register, 0))
        elif address == patch.DSP_SETTER:
            register = uc.reg_read(UC_ARM_REG_R1) & 0xFFFF
            value = uc.reg_read(UC_ARM_REG_R0) & 0xFFFFFFFF
            self.dsp[register] = value
            self.writes.append((register, value))
            self._poison(uc, 0xDEAD0D00)
            self._return(uc, 0xDEAD0D00)

    def distortion_event(self, drum: int) -> int:
        uc = self.uc
        parameter = patch.DISTORTION_PARAMETER_IDS[drum]
        preserved = {
            UC_ARM_REG_R1: parameter,
            UC_ARM_REG_R3: 0x33333333,
            UC_ARM_REG_R4: 0x44444444,
            UC_ARM_REG_R5: 0x55555555,
            UC_ARM_REG_R6: 0x66666666,
            UC_ARM_REG_R7: 0x77777777,
        }
        for register, value in preserved.items():
            uc.reg_write(register, value)
        uc.reg_write(UC_ARM_REG_R0, 0x10)
        uc.reg_write(UC_ARM_REG_R2, 0x22222222)
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
        uc.emu_start((patch.DISTORTION_ENTRIES + drum * 4) | 1, SENTINEL, count=5000)
        for register, value in preserved.items():
            if uc.reg_read(register) != value:
                raise AssertionError(f"Distortion wrapper clobbered register {register}")
        if uc.reg_read(UC_ARM_REG_SP) != STACK:
            raise AssertionError("Distortion wrapper unbalanced the stack")
        return uc.reg_read(UC_ARM_REG_R0)

    def record(self, parameter_id: int, value: int) -> tuple[int, int]:
        uc = self.uc
        preserved = {
            UC_ARM_REG_R0: 0xA0A0A0A0,
            UC_ARM_REG_R1: parameter_id,
            UC_ARM_REG_R3: 0xA3A3A3A3,
            UC_ARM_REG_R4: 0xA4A4A4A4,
            UC_ARM_REG_R5: 5,
            UC_ARM_REG_R7: 0xA7A7A7A7,
        }
        for register, filler in preserved.items():
            uc.reg_write(register, filler)
        uc.reg_write(UC_ARM_REG_R2, 1)
        uc.reg_write(UC_ARM_REG_R6, value)
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
        before = len(self.writes)
        uc.emu_start(patch.RECORD_HELPER | 1, SENTINEL, count=5000)
        if len(self.writes) != before:
            raise AssertionError("recorder wrote DSP state; it must only read")
        for register, filler in preserved.items():
            if uc.reg_read(register) != filler:
                raise AssertionError(f"recorder clobbered register {register}")
        if uc.reg_read(UC_ARM_REG_SP) != STACK:
            raise AssertionError("recorder unbalanced the stack")
        return uc.reg_read(UC_ARM_REG_R2), uc.reg_read(UC_ARM_REG_R6)


def static_checks(base: bytes, patched: bytes) -> str:
    regions = patch.changed_regions()
    inside = lambda i: any(patch.offset(a) <= i < patch.offset(e) for a, e in regions)
    stray = [i for i, (a, b) in enumerate(zip(base, patched)) if a != b and not inside(i)]
    if stray or len(base) != len(patched):
        raise AssertionError(f"bytes changed outside the two slots: {stray[:8]}")
    changed = sum(1 for a, b in zip(base, patched) if a != b)

    # The old wrapper's literal pool word at 0x08025D58 is now code.  Make sure
    # nothing else loads it: scan every halfword for 16-bit `ldr rX, [pc, #n]`
    # and 32-bit `ldr.w rX, [pc, #±n]` that would resolve to that word.
    literal = 0x08025D58
    for start in range(0, len(patched) - 4, 2):
        pc = patch.BASE + start
        if inside(start):
            continue
        half = int.from_bytes(patched[start : start + 2], "little")
        if half & 0xF800 == 0x4800:
            target = ((pc + 4) & ~3) + (half & 0xFF) * 4
            if target == literal:
                raise AssertionError(f"{pc:#x} loads the retired literal")
        if half & 0xFF7F == 0xF85F:
            second = int.from_bytes(patched[start + 2 : start + 4], "little")
            imm = second & 0xFFF
            target = ((pc + 4) & ~3) + (imm if half & 0x80 else -imm)
            if target == literal:
                raise AssertionError(f"{pc:#x} loads the retired literal")

    # Every changed byte must decode as Thumb with no data holes.
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    for address, end in regions:
        chunk = patched[patch.offset(address) : patch.offset(end)]
        decoded = sum(i.size for i in md.disasm(chunk, address))
        if decoded != len(chunk):
            raise AssertionError(f"{address:#x}: only {decoded} of {len(chunk)} bytes decode")

    # The Filter LFO wrapper, divider and predictors must be byte-identical.
    for address, end in ((0x08035B4C, 0x08035BF8), (0x08036A0C, 0x08036A34), (0x08025D5C, 0x08025D90), (0x08036B08, 0x08036B68)):
        if base[patch.offset(address) : patch.offset(end)] != patched[patch.offset(address) : patch.offset(end)]:
            raise AssertionError(f"{address:#x} changed")
    return f"{changed} bytes differ, all inside the two slots; retired literal unreferenced; Filter LFO code unchanged"


def amount_word(amount: int) -> int:
    return (amount & 0xFF) << 24


def wrapper_model_checks(patched: bytes, seed: int = 3592) -> str:
    rng = random.Random(seed)
    events = 0
    steps = 0
    for drum in range(4):
        m = Machine(patched)
        parameter = patch.DISTORTION_PARAMETER_IDS[drum]
        type_reg = patch.DISTORTION_TYPE_BASE + drum
        counter_reg = patch.STEP_DIVIDER_STATE + drum
        amount_reg = patch.DISTORTION_AMOUNT_BASE + drum
        # Model state.  Start from invalid memory to cover cold boot.
        m.dsp[type_reg] = 0xABCDEF00
        m.dsp[counter_reg] = 0
        model_type, model_counter = 0, 0
        amount = 0
        for index in range(6000):
            amount = rng.choice((0, 0, 1, 64, 126, 127, rng.randrange(128)))
            m.dsp[amount_reg] = amount_word(amount)
            m.set_stored_byte(parameter, 0xEE)
            kind = rng.random()
            m.shift = kind > 0.15
            if kind < 0.25:
                proposal = amount  # refresh with no movement
            else:
                direction = rng.choice((-1, 1))
                proposal = max(0, min(127, amount + direction * rng.choice((1, 1, 1, 2, 5))))
            m.proposal = proposal
            writes = len(m.writes)
            result = m.distortion_event(drum)

            if not m.shift:
                if result != proposal or len(m.writes) != writes or m.stored_byte(parameter) != 0xEE:
                    raise AssertionError("Shift released: wrapper must return the proposal and touch nothing")
                continue
            events += 1
            if result != amount or m.stored_byte(parameter) != amount:
                raise AssertionError(f"drum {drum}: stored amount not restored ({result}, {m.stored_byte(parameter)})")
            if proposal != amount:
                model_counter += 1
                if model_counter >= patch.STEP_DIVIDER_EVENTS:
                    model_counter = 0
                    model_type = (model_type + (1 if proposal > amount else -1)) % patch.DISTORTION_TYPES
                    steps += 1
            actual_type = (m.dsp.get(type_reg, 0) >> 8) & 0xFFFFFF
            if actual_type > 6:
                actual_type = 0
            actual_counter = (m.dsp.get(counter_reg, 0) >> 8) & 0xFFFFFF
            if (actual_type, actual_counter) != (model_type, model_counter):
                raise AssertionError(
                    f"drum {drum} event {index}: type/counter {actual_type}/{actual_counter}, "
                    f"model {model_type}/{model_counter}"
                )
        # A full clockwise lap returns to the start; three events per step.
        start = model_type
        m.shift = True
        m.dsp[amount_reg] = amount_word(64)
        m.dsp[counter_reg] = 0
        for event in range(patch.DISTORTION_TYPES * patch.STEP_DIVIDER_EVENTS):
            m.proposal = 65
            m.distortion_event(drum)
            current = m.dsp[type_reg] >> 8
            expect = (start + (event + 1) // patch.STEP_DIVIDER_EVENTS) % patch.DISTORTION_TYPES
            if current != expect:
                raise AssertionError(f"drum {drum}: lap event {event} gave type {current}, expected {expect}")
    return f"{events} Shift events on 4 drums match the model ({steps} steps); 21-event lap per drum"


def recorder_checks(base: bytes, patched: bytes) -> str:
    cases = 0
    for drum in range(4):
        parameter = 4 + 7 * drum
        for current in (0, 1, 3, 5, 6, 7, 0xFFFF):
            for counter in (0, 1, 2):
                for amount, proposal in ((64, 65), (64, 63), (0, 1), (0, 0), (127, 126), (127, 127), (30, 30)):
                    state = {
                        patch.DISTORTION_TYPE_BASE + drum: current << 8,
                        patch.STEP_DIVIDER_STATE + drum: counter << 8,
                        patch.DISTORTION_AMOUNT_BASE + drum: amount_word(amount),
                    }
                    recorder = Machine(patched)
                    recorder.shift = True
                    recorder.dsp.update(state)
                    r2, tag = recorder.record(parameter, proposal)
                    if r2 != 1 + 0x16:
                        raise AssertionError("displaced add.w not reproduced")

                    wrapper = Machine(patched)
                    wrapper.shift = True
                    wrapper.dsp.update(state)
                    wrapper.proposal = proposal
                    wrapper.distortion_event(drum)
                    played = wrapper.dsp[patch.DISTORTION_TYPE_BASE + drum] >> 8
                    played = played if played <= 6 else 0
                    if tag != (patch.TAG_BIT | played):
                        raise AssertionError(
                            f"drum {drum} type {current} counter {counter} {amount}->{proposal}: "
                            f"recorded {tag:#04x}, wrapper plays {played}"
                        )
                    cases += 1

    # Everything except the Distortion lane is unchanged.
    rng = random.Random(1)
    compared = 0
    for parameter_id in list(range(0, 32)) + [0x20002DB0]:
        if parameter_id in (4, 11, 18, 25):
            continue
        for shift in (False, True):
            for _ in range(12):
                state = {
                    register: rng.getrandbits(32)
                    for register in (
                        *[patch.SAMPLE_RAW_BASE + d for d in range(4)],
                        *[patch.LFO_MODE_REGISTER_BASE + 10 * d for d in range(4)],
                        *[patch.STEP_DIVIDER_STATE + d for d in range(4)],
                        *[0x8105 + d for d in range(4)],
                    )
                }
                value = rng.randrange(128)
                results = []
                for image in (base, patched):
                    m = Machine(image)
                    m.shift = shift
                    m.dsp.update(state)
                    results.append(m.record(parameter_id, value))
                if results[0] != results[1]:
                    raise AssertionError(f"recorder differs from base for id {parameter_id}: {results}")
                compared += 1
    # Shift released on the Distortion lane is still stock.
    for drum in range(4):
        m = Machine(patched)
        if m.record(4 + 7 * drum, 0x5A)[1] != 0x5A:
            raise AssertionError("Shift released Distortion record changed the value")
    return f"{cases} Distortion record cases match the wrapper; {compared} other inputs identical to base"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patched_sysex", nargs="?", type=Path)
    args = parser.parse_args()

    base_sysex = PERF_V2_SYSEX.read_bytes()
    if patch.sha256(base_sysex) != patch.PERF_V2_SYSEX_SHA256:
        raise SystemExit("published performance-v2 SysEx does not match its pinned hash")
    base, _ = decode_firmware(base_sysex)
    expected, _ = patch.apply(base)
    if args.patched_sysex:
        patched, _ = decode_firmware(args.patched_sysex.read_bytes())
        if patched != expected:
            raise SystemExit("patched image is not reproducible from the published base")
    else:
        patched = expected

    print("static:", static_checks(base, patched))
    print("wrapper:", wrapper_model_checks(patched))
    print("recorder:", recorder_checks(base, patched))
    print("OK")


if __name__ == "__main__":
    main()
