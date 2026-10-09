"""Regression for the failure missed by the v1 dispatcher-only test.

Checks the actual embedded DSP instructions that route X targets into Y
outputs. A narrow fixed-point model of P:$21AF..$21B4 demonstrates decay of
v1 writes and retention of v2 targets. This is not a full DSP/audio emulator.
MAC semantics: NXP DSP56300 Family Manual, signed fractional multiplication,
https://www.nxp.com/docs/en/reference-manual/DSP56300FM.pdf
"""

from __future__ import annotations


def word(image: bytes, pc: int) -> int:
    # Two framing words precede P:$0 in the embedded boot stream.
    offset = 0x08025B6E - 0x08008000 + (pc + 2) * 3
    return int.from_bytes(image[offset:offset + 3], "big")


def require_words(image: bytes, pc: int, expected: tuple[int, ...]) -> None:
    actual = tuple(word(image, pc + n) for n in range(len(expected)))
    if actual != expected:
        raise AssertionError(f"DSP P:${pc:X} changed: {actual!r}")


def signed24(value: int) -> int:
    value &= 0xFFFFFF
    return value - 0x1000000 if value & 0x800000 else value


def smooth(current: int, target: int, coefficient: int) -> int:
    # move y0,a; mac -x1,y0,a; mac x1,x0,a; move a,y:(r4)+
    # Accumulator keeps the low product bits until the final 24-bit store.
    accumulator = (current << 24) + 2 * coefficient * (target - current)
    result = accumulator >> 24
    return max(-0x800000, min(0x7FFFFF, result))


def verify_dsp_targets(image: bytes) -> dict:
    from verify_circuit_perf_controls_v2 import Dispatcher

    # Independently pinned opcodes, not constants from the ARM patch module.
    require_words(image, 0x2122, (
        0x61F400, 0x0002D7, 0x349B00, 0x380400, 0x390100,
        0x57F400, 0x040000, 0x0BF080, 0x0021A8,
        0x61F400, 0x000449, 0x34C500, 0x380400, 0x390100,
        0x57F400, 0x040000, 0x0BF080, 0x0021A8,
    ))
    require_words(image, 0x21A8, (
        0x45F000, 0x000040, 0x0C1E7D, 0x000000, 0x21E500,
        0x06D800, 0x0021B4, 0xC08900, 0x20CE00, 0x2000E6,
        0x2000A2, 0x000000, 0x5E5C00, 0x00000C,
    ))
    # The pitch consumer loads the preceding Y word and accumulates it.
    require_words(image, 0x66E, (
        0x205600, 0x5E5C00, 0xCE9800, 0x5EDF18, 0xF0D83A, 0x2000D2,
    ))
    # SPI data receiver: bit 15 chooses Y, otherwise X.
    require_words(image, 0x18B, (
        0x609000, 0x0A118F, 0x000190, 0x4C5800,
        0x0C0191, 0x445800, 0x601000,
    ))

    # Extract both mappings directly from the pinned DSP call sites.
    routes = {}
    for start in (0x2122, 0x212B):
        target = word(image, start + 1)
        output = (word(image, start + 2) >> 8) & 0xFF
        count = (word(image, start + 3) >> 8) & 0xFF
        for n in range(count):
            routes[target + n] = output + n
    assert routes == {
        0x2D7: 0x9B, 0x2D8: 0x9C, 0x2D9: 0x9D, 0x2DA: 0x9E,
        0x449: 0xC5, 0x44A: 0xC6, 0x44B: 0xC7, 0x44C: 0xC8,
    }

    dispatcher = Dispatcher(image)
    cases = 0
    for channel, base in ((0, 0x2D7), (1, 0x449)):
        dispatcher.reset_ram(0x0F, (0, 1, 9), (0, 1, 9, 15))
        # Every 14-bit bend, including adjacent values and both signed edges.
        for bend in range(16384):
            trace = dispatcher.run(bytes((0xE0 | channel, bend & 127, bend >> 7)))
            expected = ((bend - 8192) * 0x40000) & 0xFFFFFFFF
            if trace != [("dsp", expected, base)]:
                raise AssertionError(f"bend target mismatch: {channel=} {bend=} {trace=}")
            assert signed24(expected >> 8) == (bend - 8192) * 1024
            cases += 1
        for value in range(128):
            for kind, d1, d2, address in (
                (0xB0, 1, value, base + 1),
                (0xD0, value, 0, base + 2),
            ):
                trace = dispatcher.run(bytes((kind | channel, d1, d2)))
                if trace != [("dsp", value << 24, address)]:
                    raise AssertionError(f"controller target mismatch: {trace=}")
                cases += 1

    # Exercise held controls, subsequent movement and release. X is persistent;
    # the DSP changes only the Y output, monotonically approaching the target.
    model_cases = 0
    for target_address, output_address in routes.items():
        if target_address in (0x2DA, 0x44C):
            continue  # expression is deliberately outside this patch
        values = (-0x800000, -1024, 0, 1024, 0x7FFC00) if target_address in (0x2D7, 0x449) else (0, 0x10000, 0x7F0000)
        for shift in range(5):
            alpha = 0x40000 >> shift
            tolerance = (1 << 23) // alpha
            y = {address: 123 for address in routes.values()}
            y[output_address] = 0
            for target in values:
                before = dict(y)
                current = y[output_address]
                for _ in range(10000):
                    new = smooth(current, target, alpha)
                    assert min(current, target) <= new <= max(current, target)
                    current = new
                y[output_address] = current
                assert abs(current - target) < tolerance
                assert all(y[a] == before[a] for a in y if a != output_address)
                model_cases += 1
            for _ in range(10000):
                current = smooth(current, 0, alpha)
            assert abs(current) < tolerance

    # Recreate v1's incorrect Y pitch target in an in-memory fault injection.
    # This exercises the real dispatcher without depending on an unpublished
    # historical binary. The unchanged X target makes the smoother erase it.
    import circuit_perf_controls_v2_patch as patch
    faulty_source = patch.handler_source().replace("addw    r2, r2, #0x2d7", "movw    r2, #0x809b")
    faulty_handler = patch.assemble_thumb(faulty_source, patch.CAVE)
    assert len(faulty_handler) <= patch.CAVE_END - patch.CAVE
    faulty = bytearray(image)
    at = patch.offset(patch.CAVE)
    faulty[at:at + len(faulty_handler)] = faulty_handler
    old = Dispatcher(bytes(faulty))
    old.reset_ram(0x0F, (0, 1, 9), (0, 1, 9, 15))
    old_trace = old.run(bytes((0xE0, 0x7F, 0x7F)))
    assert old_trace == [("dsp", 0x7FFC0000, 0x809B)]
    old_y = signed24(old_trace[0][1] >> 8)
    for _ in range(2048):
        old_y = smooth(old_y, 0, 0x40000)
    assert old_y == 0, "old output injection should decay back to zero"

    return {
        "exhaustive_controller_cases": cases,
        "smoother_model_cases": model_cases,
        "v1_fault_injection": "Y bend decays to zero",
        "scope": "pinned DSP opcodes plus fixed-point smoother model; hardware/audio pending",
    }
