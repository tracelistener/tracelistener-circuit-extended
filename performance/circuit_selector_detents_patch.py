"""Even, detented Shift + Macro 5/6 Distortion Type selection.

Hardware report (2026-10-09): the Shift-Macro selectors "do not change
intuitively".  The Circuit's macros are endless encoders, so a discrete
selector must turn a stream of encoder events into steps.  In v0.5.0 and
performance v2 the two selectors disagree:

* Shift + Macro 7/8 (Filter LFO) passes one step per three encoder events
  through the shared divider at 0x08036A0C and stops at Off and the fastest
  sawtooth.
* Shift + Macro 5/6 (Distortion Type) steps on *every* event, so a small
  turn skips several of the seven types and wraps around.

This patch routes Distortion Type through the same three-event divider, so
both selectors need the same amount of knob movement per choice.  The type
still wraps (6 -> 0 and 0 -> 6): the direction comes from comparing the stock
proposal with the stored Distortion amount, and the stock code clamps that
proposal at 0 and 127.  With the amount at 0 (a common setting while choosing
an algorithm) the knob can only report clockwise movement; without the wrap,
types below the current one would be unreachable.

The automation recorder runs before the wrapper on each encoder event
(hardware-proven for the Filter lane).  Its Distortion lane read the type
register directly, so a recorded change was one step behind what played.  The
recorder now predicts the wrapper's result with the same selector routine and
the same divider rule, as the Filter lane already did.

Placement uses only bytes that already hold this project's code:

* the Distortion wrapper slot 0x08025CD4..0x08025D58 (the literal at
  0x08025D58 is no longer referenced; 0x08025D5C onward is untouched), and
* the Shift automation recorder 0x08035976..0x080359F8, whose exit
  ``record_tag`` stays at 0x080359E8 for the Filter predictor.

The divider, its per-drum counters X:$1E69..$1E6C, the Filter LFO wrapper and
every hook site are unchanged.
"""

from __future__ import annotations

import hashlib

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80

PERF_V2_SYSEX_SHA256 = "085f4decb43efb3c2738a1ef08851e42939c6e77c10bdb334ba70d70babf0d2e"
EXTENDED_V050_SYSEX_SHA256 = "7ea9affe4c5310a8c3d84abf6c05b1ee35d4ef9ee6d30bb040711a4eb047745f"

STOCK_DRUM_CONTROL_GETTER = 0x0800D8E4
STOCK_BUTTON_PRESSED = 0x0800C5A8
DSP_SETTER = 0x0801633C
DSP_GETTER = 0x08016E64
SHIFT_LOGICAL_ID = 0x1B

STEP_DIVIDER = 0x08036A0C
STEP_DIVIDER_STATE = 0x1E69
STEP_DIVIDER_EVENTS = 3
PARAMETER_RESTORE = 0x0802D4EC  # r0 = byte, r1 = parameter index; r0-r3 only

DISTORTION_TYPE_BASE = 0x1E65
DISTORTION_AMOUNT_BASE = 0x8101
DISTORTION_TYPES = 7
DISTORTION_ENTRIES = 0x08025CC4  # four `movs r2,#drum; b common` stubs
DISTORTION_PARAMETER_IDS = (0x04, 0x0B, 0x12, 0x19)
DISTORTION_COMMON = 0x08025CD4
DISTORTION_COMMON_END = 0x08025D5C  # Filter record predictor A starts here

RECORD_HELPER = 0x08035976
RECORD_HELPER_END = 0x080359F8
RECORD_TAG = 0x080359E8  # fixed: Filter predictors A and B return here
FILTER_RECORD_PREDICT_A = 0x08025D5C
SAMPLE_RAW_BASE = 0x1E59
LFO_MODE_REGISTER_BASE = 0x00A4
LFO_MODE_REGISTER_STRIDE = 10
TAG_BIT = 0x80

def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def offset(address: int) -> int:
    result = address - BASE
    if not 0 <= result < IMAGE_SIZE:
        raise ValueError(f"address outside firmware image: {address:#010x}")
    return result


def assemble_thumb(source: str, address: int) -> bytes:
    encoding, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB | KS_MODE_LITTLE_ENDIAN).asm(source, address)
    if not encoding:
        raise ValueError(f"assembler produced no bytes at {address:#010x}")
    return bytes(encoding)


def distortion_source(*, with_record_tail: bool = True) -> str:
    # Entered from the stock per-drum refresh: r0 = 0x10, r1 = parameter
    # index, r2 = drum (set by the entry stub).  Returns the Distortion amount
    # the caller writes to X:$8101+drum.  [sp] keeps the parameter index.
    #
    # dsel: r5 = stored amount, r6 = proposal, r7 = drum.  Returns the type
    # after one passing event (wrapping 0..6); r5 == r6 means no movement.
    # Clobbers r0-r3 only, so the recorder can call it too.
    return f"""
        push {{r1, r2, r3, r4, r5, r6, r7, lr}}
        mov r7, r2
        bl {STOCK_DRUM_CONTROL_GETTER:#x}
        mov r6, r0
        movs r0, #{SHIFT_LOGICAL_ID:#x}
        bl {STOCK_BUTTON_PRESSED:#x}
        cbz r0, normal_amount

        movw r0, #{DISTORTION_AMOUNT_BASE:#x}
        adds r0, r0, r7
        bl {DSP_GETTER:#x}
        lsrs r5, r0, #24
        cmp r6, r5
        beq restore_amount
        mov r2, r7
        bl {STEP_DIVIDER:#x}
        cbz r0, restore_amount
        bl dsel
        lsls r0, r0, #8
        movw r1, #{DISTORTION_TYPE_BASE:#x}
        adds r1, r1, r7
        bl {DSP_SETTER:#x}
    restore_amount:
        mov r0, r5
        ldr r1, [sp]
        bl {PARAMETER_RESTORE:#x}
        mov r0, r5
        pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
    normal_amount:
        mov r0, r6
        pop {{r1, r2, r3, r4, r5, r6, r7, pc}}

    dsel:
        push {{r3, lr}}
        movw r0, #{DISTORTION_TYPE_BASE:#x}
        adds r0, r0, r7
        bl {DSP_GETTER:#x}
        lsrs r0, r0, #8
        cmp r0, #{DISTORTION_TYPES - 1}
        it hi
        movhi r0, #0
        cmp r6, r5
        beq dsel_done
        blo dsel_down
        adds r0, #1
        cmp r0, #{DISTORTION_TYPES}
        it eq
        moveq r0, #0
        pop {{r3, pc}}
    dsel_down:
        cbnz r0, dsel_decrement
        movs r0, #{DISTORTION_TYPES}
    dsel_decrement:
        subs r0, #1
    dsel_done:
        pop {{r3, pc}}
    """ + (RECORD_TAIL_SOURCE if with_record_tail else "")


# Second half of the recorder's Distortion prediction (the first half is in
# the recorder slot and ends with the divider counter read in r0).  A counter
# below 2 means the divider will not pass on this event, so the type is
# recorded unchanged.  Placed after dsel; returns to record_tag.
RECORD_TAIL_SOURCE = f"""
        lsrs r0, r0, #8
        cmp r0, #{STEP_DIVIDER_EVENTS - 1}
        it lo
        movlo r5, r6
        bl dsel
        mov r6, r0
        b.w {RECORD_TAG:#x}
"""


def build_distortion() -> tuple[bytes, int]:
    """Return the wrapper slot bytes and the absolute address of the record tail."""

    code = assemble_thumb(distortion_source(), DISTORTION_COMMON)
    capacity = DISTORTION_COMMON_END - DISTORTION_COMMON
    if len(code) > capacity:
        raise ValueError(f"Distortion selector needs {len(code)} bytes; slot has {capacity}")
    tail = len(assemble_thumb(distortion_source(with_record_tail=False), DISTORTION_COMMON))
    # Pad with `nop` so the slot holds no stale bytes of the old wrapper.
    code += b"\x00\xbf" * ((capacity - len(code)) // 2)
    if len(code) != capacity:
        raise ValueError("Distortion slot padding is not halfword aligned")
    return code, DISTORTION_COMMON + tail


def recorder_source(record_tail: int) -> str:
    # Identical to the shipped recorder except for the Distortion lane, which
    # now predicts the wrapper's post-event type.  r7 = drum, r6 = the value
    # stock is about to store (the proposal), r4 holds the caller's r2.
    return f"""
        push {{r0, r1, r3, r4, r5, r7, lr}}
        mov r4, r2
        mov r5, r1
        movs r0, #{SHIFT_LOGICAL_ID:#x}
        bl {STOCK_BUTTON_PRESSED:#x}
        cbz r0, record_done
        mov r0, r5
        subs r0, #3
        blo record_done
        cmp r0, #24
        bhs record_done
        movs r7, #0
    record_scan:
        cmp r0, #7
        blo record_have
        subs r0, #7
        adds r7, #1
        b record_scan
    record_have:
        cmp r0, #1
        beq record_distortion
        cmp r0, #2
        beq record_filter
        cbnz r0, record_done

        movw r0, #{SAMPLE_RAW_BASE:#x}
        adds r0, r0, r7
        bl {DSP_GETTER:#x}
        lsrs r6, r0, #24
        cmp r6, #126
        bls record_tag
        movs r6, #126
        b record_tag

    record_filter:
        mov r5, r6
        movs r0, #{LFO_MODE_REGISTER_STRIDE}
        muls r0, r7, r0
        adds r0, #{LFO_MODE_REGISTER_BASE:#x}
        bl {DSP_GETTER:#x}
        lsrs r6, r0, #8
        and r6, r6, #0x3f
        b.w {FILTER_RECORD_PREDICT_A:#x}

    record_distortion:
        movw r0, #{DISTORTION_AMOUNT_BASE:#x}
        adds r0, r0, r7
        bl {DSP_GETTER:#x}
        lsrs r5, r0, #24
        movw r0, #{STEP_DIVIDER_STATE:#x}
        adds r0, r0, r7
        bl {DSP_GETTER:#x}
        b.w {record_tail:#x}

    record_tag:
        orr r6, r6, #{TAG_BIT:#x}
    record_done:
        mov r2, r4
        pop {{r0, r1, r3, r4, r5, r7, lr}}
        adds r2, #0x16
        cmp r2, #0x17
        bx lr
    """


def build_recorder(record_tail: int) -> bytes:
    code = assemble_thumb(recorder_source(record_tail), RECORD_HELPER)
    if len(code) != RECORD_HELPER_END - RECORD_HELPER:
        raise ValueError(f"recorder is {len(code)} bytes; slot is {RECORD_HELPER_END - RECORD_HELPER}")
    tag = assemble_thumb(f"orr r6, r6, #{TAG_BIT:#x}", RECORD_TAG)
    if code[RECORD_TAG - RECORD_HELPER : RECORD_TAG - RECORD_HELPER + len(tag)] != tag:
        raise ValueError("record_tag moved; the Filter predictors return to 0x080359E8")
    return code


def changed_regions() -> list[tuple[int, int]]:
    return [
        (DISTORTION_COMMON, DISTORTION_COMMON_END),
        (RECORD_HELPER, RECORD_HELPER_END),
    ]


def apply(base_image: bytes) -> tuple[bytes, dict]:
    if len(base_image) != IMAGE_SIZE:
        raise ValueError("unexpected firmware image size")
    distortion, record_tail = build_distortion()
    recorder = build_recorder(record_tail)
    image = bytearray(base_image)
    for address, code in ((DISTORTION_COMMON, distortion), (RECORD_HELPER, recorder)):
        image[offset(address) : offset(address) + len(code)] = code
    changed = sum(1 for a, b in zip(base_image, image) if a != b)
    manifest = {
        "patch": "Shift + Macro 5/6 Distortion Type: three-event detents and recorder prediction",
        "distortion_slot": [f"{DISTORTION_COMMON:#010x}", f"{DISTORTION_COMMON_END:#010x}"],
        "recorder_slot": [f"{RECORD_HELPER:#010x}", f"{RECORD_HELPER_END:#010x}"],
        "record_tail": f"{record_tail:#010x}",
        "events_per_step": STEP_DIVIDER_EVENTS,
        "changed_image_bytes": changed,
    }
    return bytes(image), manifest
