"""Pitch bend, mod wheel and channel aftertouch receive for the original Circuit.

Revision 2 writes the controller TARGETS in X memory. The DSP smoother at
P:$2122..$2133 / P:$21A8..$21B5 copies X:$2D7..$2DA to Y:$9B..$9E
and X:$449..$44C to Y:$C5..$C8. The synth target stride is $172.
Revision 1 incorrectly wrote the Y outputs, which decay back toward the
unchanged X targets: hardware produced zipper noise without sustained bend.

The smoothed outputs consumed by the synth (synth 2 = +0x2A) are:

* ``Y:$9B`` pitch wheel.  The pitch loop at P:$669 adds ``Y:$9B * X:$1C4``
  (osc 1) and ``* X:$1C8`` (osc 2), where X:$1C4/$1C8 hold the patch's
  "osc pitchbend" range (CC 28/40, -12..+12 semitones, default +12).
* ``Y:$9C`` mod wheel, ``Y:$9D`` aftertouch: copied into every voice before
  the mod matrix runs (P:$638), where they are mod-matrix sources 1 and 2.

The stock MIDI receive dispatcher (0x0802048C) drops 0xEn and 0xDn, and CC 1
is unmapped.  This patch redirects the dispatcher's first note-section compare
(0x08020508) to a handler that writes those DSP words through the stock DSP
setter, then continues where stock would have gone.  Every other message is
replayed through the original instructions.

The handler lives in newlib's ``_start`` (0x080081E4..0x08008258), which is
dead here: the vector table's reset handler is 0x080244A0, which calls main()
directly, and nothing in the image points or branches into the region.
"""

from __future__ import annotations

import hashlib

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80

STOCK_IMAGE_SHA256 = "1a424d4f116c9b76c3e4e9c1cfa1abb3e262f7d120529c23992e7ee047e1f1ee"
STOCK_SYSEX_SHA256 = "260a72ebd10208aae44f7c01ad18a79cf1d7ad32658ecd1dee0d5215c0e6b7c0"
EXTENDED_V050_IMAGE_SHA256 = "1a3e6593e5cff6ec415b070fd1f93c618637f0520dae3af54b4d82a07c53d22e"
EXTENDED_V050_SYSEX_SHA256 = "7ea9affe4c5310a8c3d84abf6c05b1ee35d4ef9ee6d30bb040711a4eb047745f"

RX_DISPATCHER = 0x0802048C
HOOK = 0x08020508
HOOK_STOCK = bytes.fromhex("902b04d0")  # cmp r3, #0x90 ; beq 0x08020516
NOTE_ON_PATH = 0x08020516
STOCK_RESUME = 0x0802050C  # cmp r3, #0xb0 ...
AFTER_NOTE_SECTION = 0x08020582  # CC section; stock reaches it for 0xEn/0xDn/CC 1
DSP_SETTER = 0x0801633C

CAVE = 0x080081E4
CAVE_END = 0x08008258  # main() starts here

SYNTH_STRIDE = 0x172
PITCH_WHEEL = 0x2D7
MOD_WHEEL = 0x2D8
AFTERTOUCH = 0x2D9


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


def handler_source() -> str:
    # Entry state (from the stock note section): r3 = status & 0xF0,
    # r6 = channel, r4 = Rx context, r5 = message [status, d1, d2].
    # r0-r2 are scratch; r3/r6 must survive for the stock replay.
    # Keystone mis-encodes conditional wide branches to absolute targets
    # (beq.w came out as 0x08028700), so only unconditional b.w/bl leave the cave.
    return f"""
        cmp     r3, #0x90
        bne     not_note_on
        b.w     {NOTE_ON_PATH:#x}
    not_note_on:
        ldrb    r2, [r4, #4]
        cmp     r2, r6
        beq     synth1
        ldrb    r2, [r4, #5]
        cmp     r2, r6
        bne     stock
        movw    r2, #{SYNTH_STRIDE:#x}
        b       check
    synth1:
        movs    r2, #0
    check:
        ldrb    r0, [r5, #1]
        ldrb    r1, [r5, #2]
        cmp     r3, #0xe0
        beq     bend
        cmp     r3, #0xd0
        beq     pressure
        cmp     r3, #0xb0
        bne     stock
        cmp     r0, #1
        bne     stock
        lsls    r0, r1, #24
        addw    r2, r2, #{MOD_WHEEL:#x}
        b       write
    pressure:
        lsls    r0, r0, #24
        addw    r2, r2, #{AFTERTOUCH:#x}
        b       write
    bend:
        orr.w   r0, r0, r1, lsl #7
        sub.w   r0, r0, #0x2000
        lsls    r0, r0, #18
        addw    r2, r2, #{PITCH_WHEEL:#x}
    write:
        mov     r1, r2
        bl      {DSP_SETTER:#x}
        b.w     {AFTER_NOTE_SECTION:#x}
    stock:
        b.w     {STOCK_RESUME:#x}
    """


EXTERNAL_TARGETS = {
    NOTE_ON_PATH: "b.w",
    DSP_SETTER: "bl",
    AFTER_NOTE_SECTION: "b.w",
    STOCK_RESUME: "b.w",
}


def check_branches(code: bytes, address: int) -> None:
    """Decode the assembled bytes independently; every branch must land where intended."""
    from capstone import CS_ARCH_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_THUMB, Cs

    decoded = list(Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN).disasm(code, address))
    if sum(i.size for i in decoded) != len(code):
        raise ValueError("handler does not decode cleanly")
    seen = set()
    for i in decoded:
        if not (i.mnemonic.startswith("b") or i.mnemonic.startswith("cb")) or not i.op_str.startswith("#"):
            continue
        target = int(i.op_str[1:], 16)
        if address <= target < address + len(code):
            starts = {x.address for x in decoded}
            if target not in starts:
                raise ValueError(f"{i.address:#x}: branch into the middle of an instruction")
            continue
        if EXTERNAL_TARGETS.get(target) != i.mnemonic:
            raise ValueError(f"{i.address:#x}: unexpected external branch {i.mnemonic} {target:#x}")
        seen.add(target)
    if seen != set(EXTERNAL_TARGETS):
        raise ValueError(f"missing external branches: {[hex(t) for t in set(EXTERNAL_TARGETS) - seen]}")


def build_handler() -> bytes:
    code = assemble_thumb(handler_source(), CAVE)
    if CAVE + len(code) > CAVE_END:
        raise ValueError(f"handler needs {len(code)} bytes; cave has {CAVE_END - CAVE}")
    check_branches(code, CAVE)
    return code


def build_hook() -> bytes:
    hook = assemble_thumb(f"b.w {CAVE:#x}", HOOK)
    if len(hook) != len(HOOK_STOCK):
        raise ValueError("hook is not a 4-byte b.w")
    return hook


def expected_value(status: int, d1: int, d2: int) -> int:
    """24-bit DSP word << 8, exactly as the handler passes it to the setter."""
    kind = status & 0xF0
    if kind == 0xE0:
        return ((((d2 << 7) | d1) - 8192) << 18) & 0xFFFFFFFF
    if kind == 0xD0:
        return (d1 << 24) & 0xFFFFFFFF
    if kind == 0xB0 and d1 == 1:
        return (d2 << 24) & 0xFFFFFFFF
    raise ValueError("not a handled message")


def expected_address(status: int, d1: int, synth: int) -> int:
    kind = status & 0xF0
    word = {0xE0: PITCH_WHEEL, 0xD0: AFTERTOUCH, 0xB0: MOD_WHEEL}[kind]
    return word + SYNTH_STRIDE * synth


def apply(base_image: bytes, *, hook: bool) -> tuple[bytes, dict]:
    """Place the handler (always) and the dispatcher hook (when ``hook``)."""
    if len(base_image) != IMAGE_SIZE:
        raise ValueError("image size changed")
    image = bytearray(base_image)
    if sha256(base_image) != EXTENDED_V050_IMAGE_SHA256:
        raise ValueError("base is not the pinned Extended v0.5.0 image")
    site = bytes(image[offset(HOOK) : offset(HOOK) + 4])
    if site != HOOK_STOCK:
        raise ValueError(f"hook site is not stock: {site.hex(' ')}")
    handler = build_handler()
    image[offset(CAVE) : offset(CAVE) + len(handler)] = handler
    hook_bytes = build_hook()
    if hook:
        image[offset(HOOK) : offset(HOOK) + 4] = hook_bytes
    changed = [BASE + i for i in range(IMAGE_SIZE) if image[i] != base_image[i]]
    allowed = set(range(CAVE, CAVE + len(handler))) | (set(range(HOOK, HOOK + 4)) if hook else set())
    stray = [a for a in changed if a not in allowed]
    if stray:
        raise ValueError(f"unexpected changed bytes: {[hex(a) for a in stray[:8]]}")
    info = {
        "revision": 2,
        "controller_targets": {"synth1": "X:$2D7..$2D9", "synth2": "X:$449..$44B"},
        "handler_address": f"{CAVE:#010x}",
        "handler_bytes": len(handler),
        "handler_hex": handler.hex(),
        "cave_free_after": CAVE_END - CAVE - len(handler),
        "hook_address": f"{HOOK:#010x}",
        "hook_applied": hook,
        "hook_hex": hook_bytes.hex(),
        "changed_byte_count": len(changed),
    }
    return bytes(image), info
