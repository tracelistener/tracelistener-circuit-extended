"""Wrap-around Shift + Macro 7/8 Filter LFO selection, no Filter-centre reset.

Hardware feedback (2026-10-09): the detented Distortion Type selector, which
wraps, "works great", while the Filter LFO selector stops at both ends and
sometimes will not move.  The LFO order is Off, then modes 12..19, which
only get faster (the DSP uses the mode as the rate shift).  This patch makes
the selector wrap: clockwise from the fastest sawtooth goes to Off,
counter-clockwise from Off goes to the fastest sawtooth.

Wrapping also removes most "will not move" cases.  Direction comes from the
stock Filter proposal, which the stock code clamps at 0 and 127, so with
Filter at an end stop only one direction registers; with the wrap that
direction still reaches every choice.

Changes, both inside slots that already hold this project's code:

* the Filter LFO wrapper 0x08035B4C..0x08035BF8 (it grows by 6 bytes into
  the 0xFF-filled tail of its own slot), and
* Filter record predictor B 0x08036B08..0x08036B28, so recorded automation
  still matches the wrapper.

Leaving Off still seeds the centre from the Filter amount through the
cubic map at 0x0803595C; entering Off still clears the packed word.

The patch also removes the Clear reset from the normal Filter path.  It
switched the LFO Off whenever the Filter value was exactly 64 and
STOCK_BUTTON_PRESSED(0x19) read zero.  0x19 belongs to the stock power-on
button-combination check at 0x0800A304, not to a verified Clear line, and
hardware (2026-10-09) showed the LFO switching Off and staying off after
turning Filter down through its centre.  Filter movement now never changes
the LFO mode; Shift + Macro 7/8 is the only way to select Off.  Removing
the check shrinks the wrapper, so the rest of the slot is erased to 0xFF.
"""

from __future__ import annotations

import hashlib

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80

DETENTS_SYSEX_SHA256 = "005514b86425391cf8944fb2878f35f8fd3ff37a84292de9bfb86274e0344aaf"

STOCK_DRUM_CONTROL_GETTER = 0x0800D8E4
STOCK_BUTTON_PRESSED = 0x0800C5A8
DSP_SETTER = 0x0801633C
DSP_GETTER = 0x08016E64
SHIFT_LOGICAL_ID = 0x1B
CLEAR_LOGICAL_ID = 0x19

LFO_ENTRIES = 0x0803594C
LFO_PARAMETER_IDS = (0x05, 0x0C, 0x13, 0x1A)
LFO_COMMON = 0x08035B4C
LFO_COMMON_END = 0x08035BF8
CENTER_MAP = 0x0803595C
PARAMETER_READ = 0x0802D494
NORMAL_CENTER = 0x0802D50C
FILTER_RESTORE_EXIT = 0x0802D654
FILTER_NORMAL_EXIT = 0x0802D660
STEP_DIVIDER = 0x08036A0C
STEP_DIVIDER_STATE = 0x1E69
STEP_DIVIDER_EVENTS = 3
FILTER_AMOUNT_BASE = 0x8105

LFO_MODE_MIN = 12
LFO_MODE_MAX = 19
LFO_CHOICES = 2 + LFO_MODE_MAX - LFO_MODE_MIN  # Off + eight rates = 9
LFO_MODE_REGISTER_BASE = 0x00A4
LFO_MODE_REGISTER_STRIDE = 10

PREDICT_B = 0x08036B08
PREDICT_B_END = 0x08036B28
RECORD_TAG = 0x080359E8


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


def lfo_common_source(*, wrap: bool) -> str:
    # wrap=False reproduces the shipped wrapper byte for byte (checked by
    # apply()); wrap=True changes the two end transitions and drops the
    # Filter-centre Clear reset.
    top = "bhs step_off" if wrap else "bhs restore_amount"
    if wrap:
        start = f"""
    step_start:
        movs r1, #{LFO_MODE_MIN}
    step_centre:
        mov r0, r4
        bl {CENTER_MAP:#x}
        b store_state"""
        down_off = "cbz r0, wrap_top\n        "
        wrap_top = f"""
    wrap_top:
        movs r1, #{LFO_MODE_MAX}
        b step_centre"""
    else:
        start = f"""
    step_start:
        mov r0, r4
        movs r1, #{LFO_MODE_MIN}
        bl {CENTER_MAP:#x}
        b store_state"""
        down_off = ""
        wrap_top = ""
    clear_reset = "" if wrap else f"""cmp r7, #0x40
        bne normal_clear_done
        movs r0, #{CLEAR_LOGICAL_ID:#x}
        bl {STOCK_BUTTON_PRESSED:#x}
        cbnz r0, normal_clear_done
        movs r5, #0
    normal_clear_done:"""
    return f"""
        push {{r1, r2, r3, r4, r5, r6, r7, lr}}
        movs r6, #{LFO_MODE_REGISTER_STRIDE}
        muls r6, r2, r6
        adds r6, #{LFO_MODE_REGISTER_BASE:#x}
        mov r0, r6
        bl {DSP_GETTER:#x}
        lsrs r5, r0, #8
        movs r0, #{SHIFT_LOGICAL_ID:#x}
        bl {STOCK_BUTTON_PRESSED:#x}
        cbnz r0, shift_event

        ldr r1, [sp]
        movs r0, #0x10
        bl {STOCK_DRUM_CONTROL_GETTER:#x}
        mov r7, r0
        {clear_reset}
        and r1, r5, #0x3f
        cbz r1, normal_mode_valid
        cmp r1, #{LFO_MODE_MIN}
        blo normal_amount
        cmp r1, #{LFO_MODE_MAX}
        bhi normal_amount
    normal_mode_valid:
        mov r0, r7
        mov r2, r6
        bl {NORMAL_CENTER:#x}
        b wrapper_return

    shift_event:
        ldr r1, [sp]
        ldr r2, [sp, #4]
        bl {PARAMETER_READ:#x}
        cmp r7, r4
        beq restore_amount
        ldr r2, [sp, #4]
        bl {STEP_DIVIDER:#x}
        cbz r0, restore_amount
        and r0, r5, #0x3f
        cbz r0, shift_mode_valid
        cmp r0, #{LFO_MODE_MIN}
        blo shift_mode_off
        cmp r0, #{LFO_MODE_MAX}
        bls shift_mode_valid
    shift_mode_off:
        movs r0, #0
    shift_mode_valid:
        cmp r7, r4
        blo step_down
        cbz r0, step_start
        cmp r0, #{LFO_MODE_MAX}
        {top}
        adds r0, #1
        b pack_state
    {start}
    step_down:
        {down_off}cmp r0, #{LFO_MODE_MIN}
        bls step_off
        subs r0, #1
    pack_state:
        bic r1, r5, #0x3f
        orrs r0, r1
        b store_state
    {wrap_top}
    step_off:
        movs r0, #0
    store_state:
        lsls r0, r0, #8
        mov r1, r6
        bl {DSP_SETTER:#x}
    restore_amount:
        b.w {FILTER_RESTORE_EXIT:#x}
    normal_amount:
        b.w {FILTER_NORMAL_EXIT:#x}
    wrapper_return:
        pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
    """


def predict_b_source() -> str:
    # Entered from predictor A after a passing divider event: r5 = proposal,
    # r0 = stored Filter amount (0..127), r6 = validated mode (0 or 12..19).
    # Steps through the cyclic index Off=0, 12..19 -> 1..8; returns the
    # predicted mode in r6.  r1 is saved and restored by the recorder.
    return f"""
        cmp r5, r0
        beq predict_done
        ite hi
        movhi r1, #1
        movls r1, #{LFO_CHOICES - 1}
        cbz r6, predict_index
        subs r6, #{LFO_MODE_MIN - 1}
    predict_index:
        adds r6, r6, r1
        cmp r6, #{LFO_CHOICES}
        it hs
        subhs r6, #{LFO_CHOICES}
        cbz r6, predict_done
        adds r6, #{LFO_MODE_MIN - 1}
    predict_done:
        b.w {RECORD_TAG:#x}
    """


def changed_regions() -> list[tuple[int, int]]:
    return [(LFO_COMMON, LFO_COMMON_END), (PREDICT_B, PREDICT_B_END)]


def apply(base_image: bytes) -> tuple[bytes, dict]:
    if len(base_image) != IMAGE_SIZE:
        raise ValueError("unexpected firmware image size")
    shipped = assemble_thumb(lfo_common_source(wrap=False), LFO_COMMON)
    if base_image[offset(LFO_COMMON) : offset(LFO_COMMON) + len(shipped)] != shipped:
        raise ValueError("base Filter LFO wrapper differs from the shipped source")
    common = assemble_thumb(lfo_common_source(wrap=True), LFO_COMMON)
    if len(common) > LFO_COMMON_END - LFO_COMMON:
        raise ValueError(f"Filter LFO wrapper needs {len(common)} bytes; slot has {LFO_COMMON_END - LFO_COMMON}")
    tail = base_image[offset(LFO_COMMON) + len(shipped) : offset(LFO_COMMON_END)]
    if any(byte != 0xFF for byte in tail):
        raise ValueError("Filter LFO slot tail is not erased")
    predict = assemble_thumb(predict_b_source(), PREDICT_B)
    if len(predict) > PREDICT_B_END - PREDICT_B:
        raise ValueError(f"predictor B needs {len(predict)} bytes; slot has {PREDICT_B_END - PREDICT_B}")
    predict += b"\x00\xbf" * ((PREDICT_B_END - PREDICT_B - len(predict)) // 2)

    common += b"\xff" * (LFO_COMMON_END - LFO_COMMON - len(common))
    image = bytearray(base_image)
    image[offset(LFO_COMMON) : offset(LFO_COMMON_END)] = common
    image[offset(PREDICT_B) : offset(PREDICT_B_END)] = predict
    manifest = {
        "patch": "Filter LFO wraps between Off and the fastest sawtooth; Filter movement never turns it Off",
        "lfo_wrapper": [f"{LFO_COMMON:#010x}", f"{LFO_COMMON_END:#010x}"],
        "filter_centre_clear_reset": "removed",
        "predictor_b": [f"{PREDICT_B:#010x}", f"{PREDICT_B_END:#010x}"],
        "changed_image_bytes": sum(1 for a, b in zip(base_image, image) if a != b),
    }
    return bytes(image), manifest
