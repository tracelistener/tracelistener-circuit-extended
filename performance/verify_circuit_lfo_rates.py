"""Verify the Filter LFO rate patch.

    python performance/verify_circuit_lfo_rates.py [PATCHED_SYSEX]
        [--dsp-emulator PATH] [--disassembler PATH]

Always: only the DSP LFO words P:$0001..$0023 differ from the selectors
build, the DSP program header is intact, the routine fits its slot and keeps
the shipped label positions, and the rate table is printed.

With --disassembler (dsp56kDisassemble from the dsp56300 project): every new
word disassembles to the intended instruction.

With --dsp-emulator (performance/dsp_lfo_harness): the shipped routine and
the new routine are each run in the DSP emulator over the same random cases
and compared with Python models.  The shipped routine must match its model
first; that checks the harness and the model's fixed-point arithmetic before
the new routine is judged.  Build both tools with
performance/dsp_lfo_harness/build.sh.
"""

from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import circuit_lfo_rates_patch as patch
from circuit_fw_tools import decode_firmware


SELECTORS_SYSEX = REPO_ROOT / "docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-selectors.syx"
BLOCKS_PER_SECOND = 48000 / 32

EXPECTED_TEXT = [
    "move x:(r2+$5),b", "move b,y0", "and #<$3f,b", "cmp #<$c,b", "jlt int_000021",
    "cmp #<$13,b", "jgt int_000021", "asr b", "add #<$7,b", "move x:>$65,a",
    "asl b1,a,a", "move a1,x1", "tfr x1,a", "btst #$17,b0", "jcs int_000019",
    "abs a", "sub #>$400000,a", "jmp int_dma1", "asr a", "move a1,x1",
    "mpy x1,y0,b", "asl b", "add y0,b", "move b,y1", "move #$80,a", "rts",
    "move #>$9999a,y1", "rts",
]


def sx24(value: int) -> int:
    value &= 0xFFFFFF
    return value - (1 << 24) if value & 0x800000 else value


def from_word(value: int) -> int:
    return sx24(value) << 24  # 24-bit word loaded into the high part, low part zero


def limit_to_word(acc: int) -> int:
    if acc > (1 << 47) - 1:
        return 0x7FFFFF
    if acc < -(1 << 47):
        return 0x800000
    return (acc >> 24) & 0xFFFFFF


def modulate(shape: int, packed: int) -> int:
    x1 = (shape >> 24) & 0xFFFFFF
    product = (sx24(x1) * sx24(packed)) << 2  # mpy (fractional, x2) then asl
    return limit_to_word(product + from_word(packed))


def phase(counter: int, shift: int) -> int:
    return from_word((counter << shift) & 0xFFFFFF)


def triangle(acc: int) -> int:
    return abs(acc) - (0x400000 << 24)


def sawtooth(acc: int) -> int:
    return acc >> 1


OFF = 0x09999A


def model_shipped(counter: int, packed: int) -> int:
    mode = packed & 0x3F
    if not 12 <= mode <= 19:
        return OFF
    p = phase(counter, mode)
    return modulate(sawtooth(p) if mode > 15 else triangle(p), packed)


def model_new(counter: int, packed: int) -> int:
    mode = packed & 0x3F
    if not 12 <= mode <= 19:
        return OFF
    p = phase(counter, (mode >> 1) + 7)
    return modulate(sawtooth(p) if mode & 1 else triangle(p), packed)


def rate_hz(shift: int) -> float:
    return BLOCKS_PER_SECOND * (1 << shift) / (1 << 24)


def static_checks(base: bytes, patched: bytes) -> str:
    lo, hi = patch.changed_region()
    stray = [i for i, (a, b) in enumerate(zip(base, patched)) if a != b and not patch.offset(lo) <= i < patch.offset(hi)]
    if stray or len(base) != len(patched):
        raise AssertionError(f"bytes changed outside the DSP LFO routine: {stray[:8]}")
    words, labels = patch.build_words()
    if patch.read_words(patched, patch.LFO_PC, len(words)) != words:
        raise AssertionError("patched DSP words differ from the builder")
    if len(words) != len(patch.SHIPPED_WORDS):
        raise AssertionError("routine size changed")
    if labels != {"saw": 0x19, "mod": 0x1A, "off": 0x21}:
        raise AssertionError(f"label positions moved: {labels}")
    changed = sum(1 for a, b in zip(base, patched) if a != b)
    return f"{changed} bytes differ, all inside DSP P:$0001..$0023; 35 words, labels unchanged"


def disassembler_checks(tool: Path, words: list[int]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.txt"
        out = Path(tmp) / "out.asm"
        src.write_text(" ".join(f"{w:06x}" for w in words))
        subprocess.run([str(tool), "-in", str(src), "-out", str(out), "-pc", "1", "-nops"], check=True, capture_output=True)
        text = [re.sub(r"\s+", " ", m.group(1)).strip() for m in re.finditer(r"^[0-9a-f]{6}: (.*?)\s*;", out.read_text(), re.M)]
    if text != EXPECTED_TEXT:
        for i, (got, want) in enumerate(zip(text, EXPECTED_TEXT)):
            if got != want:
                raise AssertionError(f"instruction {i}: disassembles to {got!r}, expected {want!r}")
        raise AssertionError(f"{len(text)} instructions, expected {len(EXPECTED_TEXT)}")
    return f"{len(text)} instructions disassemble as intended"


def emulator_checks(tool: Path, shipped_image: bytes, patched_image: bytes) -> str:
    count = int.from_bytes(shipped_image[patch.offset(patch.DSP_PROGRAM_HEADER) :][:3], "big")
    rng = random.Random(3592)
    cases = []
    for _ in range(4000):
        counter = rng.choice([0, 1, 0x7FFFFF, 0x800000, 0xFFFFFF, rng.getrandbits(24)])
        centre = rng.choice([0x8000, 0x0A0000, 0x3F02C0, rng.getrandbits(22)]) & ~0x3F
        mode = rng.choice(list(range(12, 20)) * 4 + [0, 5, 11, 20, 63])
        cases.append((rng.randrange(4), counter, centre | mode))

    def run(image: bytes) -> list[tuple[int, int, int]]:
        program = patch.read_words(image, 0, count)
        stdin = f"P {count} " + " ".join(f"{w:x}" for w in program) + "\n"
        stdin += "".join(f"{d:x} {c:x} {p:x}\n" for d, c, p in cases)
        out = subprocess.run([str(tool)], input=stdin, capture_output=True, text=True, check=True).stdout
        rows = [line.split() for line in out.splitlines() if re.fullmatch(r"[0-9a-f]{6} [0-9a-f]{6} \d+", line.strip())]
        return [(int(y, 16), int(a, 16), int(n)) for y, a, n in rows]

    steps = {}
    for name, image, model in (("shipped", shipped_image, model_shipped), ("new", patched_image, model_new)):
        results = run(image)
        if len(results) != len(cases):
            raise AssertionError(f"{name}: emulator returned {len(results)} of {len(cases)} results")
        for (drum, counter, packed), (y1, a1, _) in zip(cases, results):
            if y1 != model(counter, packed):
                raise AssertionError(f"{name}: drum {drum} counter {counter:#x} packed {packed:#x}: y1 {y1:#x}, model {model(counter, packed):#x}")
            if 12 <= packed & 0x3F <= 19 and a1 != 0x800000:
                raise AssertionError(f"{name}: A not left at -1.0 (full-wet low-pass)")
        steps[name] = max(n for _, _, n in results)
    if steps["new"] > steps["shipped"]:
        raise AssertionError(f"new routine runs longer: {steps}")
    return f"{len(cases)} cases: shipped and new routines both match their models in the DSP emulator; max instructions {steps}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patched_sysex", nargs="?", type=Path)
    parser.add_argument("--dsp-emulator", type=Path)
    parser.add_argument("--disassembler", type=Path)
    args = parser.parse_args()

    base_sysex = SELECTORS_SYSEX.read_bytes()
    if patch.sha256(base_sysex) != patch.SELECTORS_SYSEX_SHA256:
        raise SystemExit("published selectors SysEx does not match its pinned hash")
    base, _ = decode_firmware(base_sysex)
    expected, _ = patch.apply(base)
    if args.patched_sysex:
        patched, _ = decode_firmware(args.patched_sysex.read_bytes())
        if patched != expected:
            raise SystemExit("patched image is not reproducible from the published selectors base")
    else:
        patched = expected

    print("static:", static_checks(base, patched))
    for mode in range(12, 20):
        shape = "sawtooth" if mode & 1 else "triangle"
        print(f"  mode {mode}: {shape:8} shift {(mode >> 1) + 7}  ~{rate_hz((mode >> 1) + 7):.2f} Hz")
    if args.disassembler:
        print("disassembler:", disassembler_checks(args.disassembler, patch.build_words()[0]))
    else:
        print("disassembler: skipped (pass --disassembler)")
    if args.dsp_emulator:
        print("emulator:", emulator_checks(args.dsp_emulator, base, patched))
    else:
        print("emulator: skipped (pass --dsp-emulator)")
    print("OK")


if __name__ == "__main__":
    main()
