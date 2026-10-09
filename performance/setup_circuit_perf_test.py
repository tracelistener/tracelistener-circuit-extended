"""Route the mod wheel and aftertouch in the current Circuit synth patch, for testing.

    python performance/setup_circuit_perf_test.py --channel 1
    python performance/setup_circuit_perf_test.py --channel 1 --bend-range 2
    python performance/setup_circuit_perf_test.py --channel 1 --clear

Mod-matrix sources 1 (mod wheel) and 2 (aftertouch) exist in the Circuit's
synth engine but are undocumented, so no factory patch uses them.  This sends
NRPN edits to the patch currently loaded on that synth.  Nothing is saved:
reload the patch or session to restore the original assignments. --clear only
zeros slots 19/20; it cannot restore assignments overwritten by this test.
The test uses each patch's existing LFO1 settings, so vibrato varies by patch.

* slot 20: mod wheel x LFO 1 (+/-) -> osc 1 & 2 pitch   (mod-wheel vibrato)
* slot 19: aftertouch -> filter frequency, negative depth (press harder = darker)
* --bend-range N sets both oscillators' pitch-bend range to +N semitones
  (CC 28/40; the patch default is 12).
"""

from __future__ import annotations

import argparse
import time

import rtmidi

def select_port(names, wanted, direction):
    exact = [i for i, name in enumerate(names) if name == wanted]
    matches = exact or [i for i, name in enumerate(names) if wanted.lower() in name.lower()]
    if len(matches) != 1:
        raise SystemExit(f"Expected one {direction} port matching {wanted!r}; available: {names}")
    return matches[0]


# NRPN (MSB, LSB) for mod-matrix slots 19 and 20, from the synth parameter
# table at 0x0802DBC0 (records 166-173): source 1, source 2, depth, destination.
SLOT_NRPNS = {
    19: ((2, 45), (2, 46), (2, 48), (2, 49)),
    20: ((2, 50), (2, 51), (2, 53), (2, 54)),
}
SOURCE_DIRECT, SOURCE_MOD_WHEEL, SOURCE_AFTERTOUCH, SOURCE_LFO1_BIPOLAR = 0, 1, 2, 7
DEST_OSC_PITCH, DEST_FILTER_FREQUENCY = 0, 12
CC_OSC1_BEND_RANGE, CC_OSC2_BEND_RANGE = 28, 40


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="Circuit")
    parser.add_argument("--channel", type=int, required=True, help="the synth's MIDI channel, 1..16")
    parser.add_argument("--bend-range", type=int, help="semitones, -12..12 (patch default 12)")
    parser.add_argument("--vibrato-depth", type=int, default=12, help="-64..63 (default 12)")
    parser.add_argument("--pressure-depth", type=int, default=-48, help="-64..63 (default -48)")
    parser.add_argument("--clear", action="store_true", help="return slots 19/20 to unused")
    args = parser.parse_args()
    if not 1 <= args.channel <= 16:
        parser.error("channel must be 1..16")
    for name in ("vibrato_depth", "pressure_depth"):
        if not -64 <= getattr(args, name) <= 63:
            parser.error(f"--{name.replace('_', '-')} must be -64..63")
    if args.bend_range is not None and not -12 <= args.bend_range <= 12:
        parser.error("--bend-range must be -12..12")

    if args.clear:
        slots = {19: (SOURCE_DIRECT, SOURCE_DIRECT, 0, DEST_OSC_PITCH), 20: (SOURCE_DIRECT, SOURCE_DIRECT, 0, DEST_OSC_PITCH)}
    else:
        slots = {
            20: (SOURCE_MOD_WHEEL, SOURCE_LFO1_BIPOLAR, args.vibrato_depth, DEST_OSC_PITCH),
            19: (SOURCE_AFTERTOUCH, SOURCE_DIRECT, args.pressure_depth, DEST_FILTER_FREQUENCY),
        }

    midi = rtmidi.MidiOut()
    names = midi.get_ports()
    index = select_port(names, args.port, "output")
    midi.open_port(index, "Circuit perf-controls test setup")
    status = 0xB0 | (args.channel - 1)

    def nrpn(msb: int, lsb: int, value: int) -> None:
        midi.send_message([status, 99, msb])
        midi.send_message([status, 98, lsb])
        midi.send_message([status, 6, value])
        time.sleep(0.01)

    try:
        for slot, (source1, source2, depth, destination) in slots.items():
            (s1, s2, dp, ds) = SLOT_NRPNS[slot]
            nrpn(*dp, 64)  # depth 0 while rewiring
            nrpn(*s1, source1)
            nrpn(*s2, source2)
            nrpn(*ds, destination)
            nrpn(*dp, 64 + depth)
            print(f"slot {slot}: source {source1} x {source2} -> destination {destination}, depth {depth:+d}")
        if args.bend_range is not None:
            midi.send_message([status, CC_OSC1_BEND_RANGE, 64 + args.bend_range])
            midi.send_message([status, CC_OSC2_BEND_RANGE, 64 + args.bend_range])
            print(f"bend range: +{args.bend_range} semitones on both oscillators")
        time.sleep(0.05)
    finally:
        midi.close_port()
    print(f"sent on channel {args.channel} through {names[index]} (not saved; reload the patch to undo)")


if __name__ == "__main__":
    main()
