"""Musical Filter LFO speeds: four rates, each in triangle and sawtooth.

The drum Filter LFO is a 35-word DSP routine at P:$0001..$0023, inside
unused DSP interrupt vectors.  It used the mode (12..19) as the shift
applied to the audio-block counter X:$65, so the eight choices ran
0.37..2.9 Hz triangle, then 5.9..47 Hz sawtooth.  The top sawtooth speeds
are audio-rate buzz, and the speed dropped back at the triangle-to-sawtooth
change.

This routine keeps the same entry, validation, Off path, depth maths and
size.  Only the rate and shape selection change:

* rate shift = (mode >> 1) + 7, i.e. 13, 13, 14, 14, 15, 15, 16, 16;
* even modes are triangle, odd modes are sawtooth (the low mode bit is
  shifted into b0 bit 23 and tested with btst).

So Shift + Macro 7/8 steps Off, triangle 0.73 Hz, sawtooth 0.73 Hz,
triangle 1.5, sawtooth 1.5, triangle 2.9, sawtooth 2.9, triangle 5.9,
sawtooth 5.9 Hz.  Hz values assume 32-sample blocks at 48 kHz (1500 blocks
per second), which the stock synth's tempo-sync arithmetic also implies.
The rates are free-running, not tempo-synced.

The ARM side is unchanged: modes are still Off and 12..19, so the selector,
wrap, recorder and playback stay byte-identical.

Every opcode below was checked with the dsp56300 project's disassembler, and
the routine was run against a Python model in that project's DSP emulator
(see performance/README.md).
"""

from __future__ import annotations

import hashlib


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80

SELECTORS_SYSEX_SHA256 = "54aa4e9ac25f87d16d796be6740fbbb35ee55bf496c31f2fb0db3be5c4551480"

DSP_PROGRAM_HEADER = 0x08025B6E  # 3-byte word count, 3-byte load address
DSP_PROGRAM_DATA = DSP_PROGRAM_HEADER + 6  # P:$0000, 3 bytes big-endian per word
LFO_PC = 0x0001
LFO_END_PC = 0x0024  # P:$0024 onward holds ARM code (sample playback helper)

# The routine shipped in v0.5.0 .. selectors (from circuit_filter_lfo_patch).
SHIPPED_WORDS = (
    0x0212DF, 0x21E600, 0x017F8E, 0x014C8D, 0x0AF0A9, 0x000021, 0x01538D,
    0x0AF0A7, 0x000021, 0x21E400, 0x57F000, 0x000065, 0x0C1E59, 0x21A500,
    0x200069, 0x208E00, 0x014F85, 0x0AF0A7, 0x000019, 0x20002E, 0x0140CC,
    0x400000, 0x0AF080, 0x00001A, 0x20002A, 0x21A500, 0x2000E8, 0x20003A,
    0x200058, 0x21E700, 0x2E8000, 0x00000C, 0x47F400, 0x09999A, 0x00000C,
)


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def offset(address: int) -> int:
    result = address - BASE
    if not 0 <= result < IMAGE_SIZE:
        raise ValueError(f"address outside firmware image: {address:#010x}")
    return result


def dsp_address(pc: int) -> int:
    return DSP_PROGRAM_DATA + 3 * pc


def build_words() -> tuple[list[int], dict[str, int]]:
    words: list[int] = []
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(name: str) -> None:
        labels[name] = LFO_PC + len(words)

    def emit(*values: int) -> None:
        words.extend(values)

    def branch(opcode: int, target: str) -> None:
        emit(opcode, 0)
        fixups.append((len(words) - 1, target))

    emit(0x0212DF)              # move x:(r2+$5),b    packed centre|mode
    emit(0x21E600)              # move b,y0           packed saved for depth
    emit(0x017F8E)              # and #<$3f,b         extract mode
    emit(0x014C8D)              # cmp #<$c,b
    branch(0x0AF0A9, "off")     # jlt off
    emit(0x01538D)              # cmp #<$13,b
    branch(0x0AF0A7, "off")     # jgt off
    emit(0x20002A)              # asr b               b1 = mode >> 1, b0 bit 23 = mode & 1
    emit(0x014788)              # add #<$7,b          b1 = rate shift 13..16
    emit(0x56F000, 0x000065)    # move x:>$65,a       audio-block counter
    emit(0x0C1E46)              # asl b1,a,a
    emit(0x218500)              # move a1,x1
    emit(0x200061)              # tfr x1,a            wrap, not saturate
    emit(0x0BC977)              # btst #$17,b0        odd mode -> sawtooth
    branch(0x0AF0A8, "saw")     # jcs saw
    emit(0x200026)              # abs a               triangle
    emit(0x0140C4, 0x400000)    # sub #>$400000,a     -> -0.5 .. +0.5
    branch(0x0AF080, "mod")     # jmp mod
    mark("saw")
    emit(0x200022)              # asr a               ramp -> -0.5 .. +0.5
    mark("mod")
    emit(0x218500)              # move a1,x1
    emit(0x2000E8)              # mpy x1,y0,b         centre-proportional depth
    emit(0x20003A)              # asl b               doubled sweep 0..2x
    emit(0x200058)              # add y0,b
    emit(0x21E700)              # move b,y1           coefficient out
    emit(0x2E8000)              # move #$80,a         force full-wet low-pass
    emit(0x00000C)              # rts
    mark("off")
    emit(0x47F400, 0x09999A)    # move #>$9999a,y1    stock coefficient
    emit(0x00000C)              # rts

    for index, target in fixups:
        words[index] = labels[target]
    if LFO_PC + len(words) > LFO_END_PC:
        raise ValueError(f"LFO routine needs {len(words)} words; slot has {LFO_END_PC - LFO_PC}")
    return words, labels


def read_words(image: bytes, pc: int, count: int) -> list[int]:
    start = offset(dsp_address(pc))
    return [int.from_bytes(image[start + 3 * i : start + 3 * i + 3], "big") for i in range(count)]


def changed_region() -> tuple[int, int]:
    return dsp_address(LFO_PC), dsp_address(LFO_END_PC)


def apply(base_image: bytes) -> tuple[bytes, dict]:
    if len(base_image) != IMAGE_SIZE:
        raise ValueError("unexpected firmware image size")
    header = base_image[offset(DSP_PROGRAM_HEADER) : offset(DSP_PROGRAM_HEADER) + 6]
    if header != bytes.fromhex("00280d000000"):
        raise ValueError("DSP program header changed")
    if read_words(base_image, LFO_PC, len(SHIPPED_WORDS)) != list(SHIPPED_WORDS):
        raise ValueError("base DSP LFO routine differs from the shipped routine")
    words, labels = build_words()
    image = bytearray(base_image)
    start = offset(dsp_address(LFO_PC))
    image[start : start + 3 * len(words)] = b"".join(w.to_bytes(3, "big") for w in words)
    return bytes(image), {
        "patch": "Filter LFO: four free-running rates (~0.73, 1.5, 2.9, 5.9 Hz), each triangle then sawtooth",
        "dsp_words": len(words),
        "dsp_labels": labels,
        "changed_image_bytes": sum(1 for a, b in zip(base_image, image) if a != b),
    }
