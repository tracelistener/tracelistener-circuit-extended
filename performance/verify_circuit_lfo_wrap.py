"""Static and Thumb-emulated verification of the Filter LFO wrap patch.

    python performance/verify_circuit_lfo_wrap.py [PATCHED_SYSEX]

Without an argument the patch is applied in memory to the published detents
SysEx.  Checks:

* only the Filter LFO wrapper slot and predictor B differ from the base;
* the live wrapper, run from each drum's entry stub, matches a reference
  model over long random Shift event streams: Off and modes 12..19 in a
  cycle, three moving events per step, centre seeded on leaving Off and
  cleared on entering it, stored Filter amount restored;
* with Shift released (with and without Clear held) the wrapper behaves
  exactly as the base image;
* the recorder's Filter tag equals the mode the wrapper produces for the same
  event and state, and every other recorder input matches the base.
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

import circuit_lfo_wrap_patch as patch
import verify_circuit_selector_detents as harness
from circuit_fw_tools import decode_firmware


DETENTS_SYSEX = REPO_ROOT / "docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-detents.syx"


class Machine(harness.Machine):
    """Adds the Clear button (active-low) and the Filter callback."""

    def __init__(self, image: bytes):
        super().__init__(image)
        self.clear = False

    def _hook(self, uc, address, size, user):
        if address == patch.STOCK_BUTTON_PRESSED:
            button = uc.reg_read(UC_ARM_REG_R0) & 0xFF
            if button == patch.SHIFT_LOGICAL_ID:
                result = int(self.shift)
            elif button == patch.CLEAR_LOGICAL_ID:
                result = int(not self.clear)
            else:
                raise AssertionError(f"unexpected button query {button:#x}")
            self._poison(uc, 0xDEAD0B00)
            self._return(uc, result)
            return
        super()._hook(uc, address, size, user)

    def lfo_event(self, drum: int) -> int:
        uc = self.uc
        preserved = {
            UC_ARM_REG_R1: patch.LFO_PARAMETER_IDS[drum],
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
        uc.reg_write(UC_ARM_REG_SP, harness.STACK)
        uc.reg_write(UC_ARM_REG_LR, harness.SENTINEL | 1)
        uc.emu_start((patch.LFO_ENTRIES + drum * 4) | 1, harness.SENTINEL, count=5000)
        for register, value in preserved.items():
            if uc.reg_read(register) != value:
                raise AssertionError(f"Filter wrapper clobbered register {register}")
        if uc.reg_read(UC_ARM_REG_SP) != harness.STACK:
            raise AssertionError("Filter wrapper unbalanced the stack")
        return uc.reg_read(UC_ARM_REG_R0)


def amount_word(amount: int) -> int:
    return ((amount - 64) << 25) & 0xFFFFFFFF


def centre_map(amount: int, mode: int) -> int:
    value = ((amount * amount * amount * 2) + 0x8000) & 0xFFFFFFFF
    return (value & ~0x3F & 0xFFFFFFFF) | mode


def model_step(packed: int, amount: int, up: bool) -> int:
    mode = packed & 0x3F
    if not (mode == 0 or patch.LFO_MODE_MIN <= mode <= patch.LFO_MODE_MAX):
        mode = 0
    if mode == 0:
        return centre_map(amount, patch.LFO_MODE_MIN if up else patch.LFO_MODE_MAX)
    if (up and mode == patch.LFO_MODE_MAX) or (not up and mode == patch.LFO_MODE_MIN):
        return 0
    return (packed & ~0x3F & 0xFFFFFFFF) | (mode + (1 if up else -1))


def mode_of(word: int) -> int:
    mode = (word >> 8) & 0x3F
    return mode if mode == 0 or patch.LFO_MODE_MIN <= mode <= patch.LFO_MODE_MAX else 0


def static_checks(base: bytes, patched: bytes) -> str:
    regions = patch.changed_regions()
    inside = lambda i: any(patch.offset(a) <= i < patch.offset(e) for a, e in regions)
    stray = [i for i, (a, b) in enumerate(zip(base, patched)) if a != b and not inside(i)]
    if stray or len(base) != len(patched):
        raise AssertionError(f"bytes changed outside the two slots: {stray[:8]}")
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    for address, end in regions:
        chunk = patched[patch.offset(address) : patch.offset(end)]
        if sum(i.size for i in md.disasm(chunk, address)) != len(chunk):
            raise AssertionError(f"{address:#x}: not every byte decodes as Thumb")
    changed = sum(1 for a, b in zip(base, patched) if a != b)
    return f"{changed} bytes differ, all inside the Filter wrapper and predictor B"


def wrapper_model_checks(patched: bytes, seed: int = 7) -> str:
    rng = random.Random(seed)
    events = steps = wraps = 0
    for drum in range(4):
        m = Machine(patched)
        parameter = patch.LFO_PARAMETER_IDS[drum]
        mode_reg = patch.LFO_MODE_REGISTER_BASE + patch.LFO_MODE_REGISTER_STRIDE * drum
        counter_reg = patch.STEP_DIVIDER_STATE + drum
        amount_reg = patch.FILTER_AMOUNT_BASE + drum
        packed = rng.choice((0, 0x3F0005, 0x220000 | 14))
        m.dsp[mode_reg] = packed << 8
        counter = 0
        m.shift = True
        for index in range(6000):
            amount = rng.choice((0, 1, 64, 64, 126, 127, rng.randrange(128)))
            m.dsp[amount_reg] = amount_word(amount)
            m.set_stored_byte(parameter, 0xEE)
            if rng.random() < 0.2:
                proposal = amount
            else:
                proposal = max(0, min(127, amount + rng.choice((-1, 1)) * rng.choice((1, 1, 2, 6))))
            m.proposal = proposal
            result = m.lfo_event(drum)
            events += 1
            if result != amount or m.stored_byte(parameter) != amount:
                raise AssertionError(f"drum {drum}: Filter amount not restored")
            if proposal != amount:
                counter += 1
                if counter >= patch.STEP_DIVIDER_EVENTS:
                    counter = 0
                    before = mode_of(packed << 8)
                    packed = model_step(packed, amount, proposal > amount)
                    after = mode_of(packed << 8)
                    steps += 1
                    wraps += (before == patch.LFO_MODE_MAX and after == 0) or (before == 0 and after == patch.LFO_MODE_MAX)
            actual = (m.dsp[mode_reg] >> 8) & 0xFFFFFF
            if actual != (packed & 0xFFFFFF) or (m.dsp.get(counter_reg, 0) >> 8) != counter:
                raise AssertionError(
                    f"drum {drum} event {index}: packed {actual:#08x}/{m.dsp.get(counter_reg, 0) >> 8}, "
                    f"model {packed & 0xFFFFFF:#08x}/{counter}"
                )
        # A full lap in each direction visits all nine choices and returns.
        for up in (True, False):
            seen = []
            m.dsp[counter_reg] = 0
            m.dsp[amount_reg] = amount_word(64)
            m.proposal = 65 if up else 63
            for _ in range(patch.LFO_CHOICES * patch.STEP_DIVIDER_EVENTS):
                m.lfo_event(drum)
                seen.append(mode_of(m.dsp[mode_reg]))
            visited = seen[patch.STEP_DIVIDER_EVENTS - 1 :: patch.STEP_DIVIDER_EVENTS]
            if sorted(visited) != [0, *range(patch.LFO_MODE_MIN, patch.LFO_MODE_MAX + 1)]:
                raise AssertionError(f"drum {drum}: lap visited {visited}")
    if not wraps:
        raise AssertionError("random stream never wrapped")
    return f"{events} Shift events on 4 drums match the model ({steps} steps, {wraps} wraps); 27-event laps both ways"


def shift_released_checks(base: bytes, patched: bytes) -> str:
    rng = random.Random(11)
    cases = 0
    for drum in range(4):
        mode_reg = patch.LFO_MODE_REGISTER_BASE + patch.LFO_MODE_REGISTER_STRIDE * drum
        for _ in range(150):
            state = {
                mode_reg: rng.choice((0, 0x3F0005 << 8, (0x220000 | rng.randrange(64)) << 8, rng.getrandbits(32))),
                patch.FILTER_AMOUNT_BASE + drum: amount_word(rng.randrange(128)),
                patch.STEP_DIVIDER_STATE + drum: rng.randrange(3) << 8,
            }
            proposal = rng.choice((0x40, rng.randrange(128)))
            clear = rng.random() < 0.3
            outcomes = []
            for image in (base, patched):
                m = Machine(image)
                m.dsp.update(state)
                m.proposal = proposal
                m.clear = clear
                result = m.lfo_event(drum)
                outcomes.append((result, sorted(m.dsp.items()), m.writes))
            if outcomes[0] != outcomes[1]:
                raise AssertionError(f"Shift released differs from base: drum {drum}, {state}, clear {clear}")
            cases += 1
    return f"{cases} Shift-released cases (with and without Clear) identical to base"


def recorder_checks(base: bytes, patched: bytes) -> str:
    cases = 0
    for drum in range(4):
        parameter = 5 + 7 * drum
        mode_reg = patch.LFO_MODE_REGISTER_BASE + patch.LFO_MODE_REGISTER_STRIDE * drum
        for current in (0, 5, 11, 12, 13, 15, 16, 18, 19, 20, 63):
            for counter in (0, 1, 2):
                for amount, proposal in ((64, 65), (64, 63), (0, 1), (0, 0), (127, 126), (127, 127), (100, 101)):
                    state = {
                        mode_reg: (0x220000 | current) << 8,
                        patch.STEP_DIVIDER_STATE + drum: counter << 8,
                        patch.FILTER_AMOUNT_BASE + drum: amount_word(amount),
                    }
                    recorder = Machine(patched)
                    recorder.shift = True
                    recorder.dsp.update(state)
                    _, tag = recorder.record(parameter, proposal)
                    wrapper = Machine(patched)
                    wrapper.shift = True
                    wrapper.dsp.update(state)
                    wrapper.proposal = proposal
                    wrapper.lfo_event(drum)
                    plays = mode_of(wrapper.dsp[mode_reg])
                    if tag != (0x80 | plays):
                        raise AssertionError(
                            f"drum {drum} mode {current} counter {counter} {amount}->{proposal}: "
                            f"recorded {tag:#04x}, wrapper plays {plays}"
                        )
                    cases += 1
    rng = random.Random(5)
    compared = 0
    for parameter_id in list(range(0, 32)) + [0x20002DB0]:
        if parameter_id in (5, 12, 19, 26):
            continue
        for shift in (False, True):
            for _ in range(10):
                state = {
                    register: rng.getrandbits(32)
                    for register in (
                        *[0x1E59 + d for d in range(4)],
                        *[0x1E65 + d for d in range(4)],
                        *[0x8101 + d for d in range(4)],
                        *[patch.STEP_DIVIDER_STATE + d for d in range(4)],
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
                    raise AssertionError(f"recorder differs from base for id {parameter_id}")
                compared += 1
    return f"{cases} Filter record cases match the wrapper; {compared} other inputs identical to base"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patched_sysex", nargs="?", type=Path)
    args = parser.parse_args()

    base_sysex = DETENTS_SYSEX.read_bytes()
    if patch.sha256(base_sysex) != patch.DETENTS_SYSEX_SHA256:
        raise SystemExit("published detents SysEx does not match its pinned hash")
    base, _ = decode_firmware(base_sysex)
    expected, _ = patch.apply(base)
    if args.patched_sysex:
        patched, _ = decode_firmware(args.patched_sysex.read_bytes())
        if patched != expected:
            raise SystemExit("patched image is not reproducible from the published detents base")
    else:
        patched = expected

    print("static:", static_checks(base, patched))
    print("wrapper:", wrapper_model_checks(patched))
    print("shift released:", shift_released_checks(base, patched))
    print("recorder:", recorder_checks(base, patched))
    print("OK")


if __name__ == "__main__":
    main()
