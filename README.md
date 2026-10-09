# Circuit Extended Firmware

Custom firmware for the **original Novation Circuit**, firmware 1.8 build 3592. The uploader defaults to **Performance v2 (experimental)** and also offers the previous hardware-tested Extended v0.5.0 build.

## [Open the browser uploader](https://tracelistener.github.io/tracelistener-circuit-extended/)

Use Chrome or Edge. The page loads and verifies the firmware automatically—no Python, command line, or Novation Components import is required.

## Install

1. Back up your Circuit pack in Novation Components, then close Components and all other MIDI software.
2. Connect the Circuit directly by USB.
3. Turn it on while holding **Scales + Note + Velocity**.
4. Open the browser uploader, allow MIDI access, select the Bootloader port, and upload.
5. Leave power and USB connected until the Circuit finishes restarting.

## Features and controls

| Control | Function |
|---|---|
| MIDI pitch bend | Bends Synth 1/2 using the patch’s oscillator bend ranges (performance v2). |
| MIDI CC 1 / channel aftertouch | Drives the mod-wheel / aftertouch modulation sources when assigned in the synth patch (performance v2). |
| Drum Pitch | Follows the selected master root and scale across ±2 octaves; sample reference note is C. |
| Shift + Scales | Toggle Scale Follow. It starts enabled after boot. |
| Shift + Macro 3/4 | Move Sample Start for the first/second drum in the active pair. |
| Shift + Macro 5/6 | Select one of seven stock distortion algorithms for the first/second drum. |
| Shift + Macro 7/8 | Select Filter LFO Off or eight speeds: four triangle and four sawtooth. |
| Record + Shift Macro | Record and replay Sample Start, Distortion Type, and Filter LFO movements. |
| Clear + Macro 3–8 clockwise | Perform the stock blue-LED reset and reset the corresponding new Shift-Macro control: Sample Start, Distortion Type, or Filter LFO. |
| Clear + Macro counter-clockwise | Keep the stock red-LED automation-delete behavior. |

Normal Macro movement retains the Circuit's stock Decay, Distortion Amount, and bipolar Filter controls.

## Performance v2 — experimental

[Download the current SysEx](docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-feature.syx)

SHA-256:

```text
085f4decb43efb3c2738a1ef08851e42939c6e77c10bdb334ba70d70babf0d2e
```

This is the exact build tested on hardware on 2026-10-09. Pitch bend worked. One unexplained crash was reported, followed by stable operation; modulation was later reported inconsistent. The cause is unresolved and long-term stability is unconfirmed. Emulation checks do not establish hardware stability.

Mod wheel and aftertouch need modulation assignments in each patch; the firmware does not add them automatically. The vibrato test inherits the patch’s LFO1 settings. For a direct filter sweep, configure your controller’s mod strip to send **CC 74** on the synth channel instead (default channel 1 or 2). [Setup, build and verification notes](performance/README.md).

## Previous build and rollback

Select **Extended v0.5.0** in the uploader, or [open the uploader with v0.5.0 selected](https://tracelistener.github.io/tracelistener-circuit-extended/?firmware=v050). Reinstalling it removes the performance-v2 controls and keeps the earlier Extended features.

[Download Extended v0.5.0](docs/firmware/circuit-3592-filter-lfo-shift-automation.syx)

SHA-256:

```text
7ea9affe4c5310a8c3d84abf6c05b1ee35d4ef9ee6d30bb040711a4eb047745f
```

Validated on hardware on 2026-07-31: normal boot, all four drum paths, Scale Follow, Sample Start, independent Distortion Type, Filter LFO, Shift automation playback without a knob nudge, blue-LED Clear resets for every new Shift-Macro control, and red-LED automation deletion.

## MIDI CC remap (experimental)

The original Circuit's MIDI CC numbers are fixed. The uploader can change them: open **Optional — MIDI CC numbers**, pick a preset or type your own numbers for the synth macros and drum knobs, then upload as usual.

**Status:** the Korg NTS-1 preset on Extended v0.5.0 was tested on hardware on 2026-09-29: Macros 1, 2, 5 and 6 send CC 54, 55, 43 and 44. Other maps and the performance-v2 combination use the same table edit and are checked against the Circuit's own MIDI code in emulation; the combination still needs hardware testing.

The page takes your selected firmware build and rewrites only the Circuit's CC tables, in your browser. Its other features stay intact; the NTS-1 preset changes 19 bytes on either base. The page shows the new SHA-256 and offers the file as a download.

### Korg NTS-1 preset

| Circuit knob | Stock | Preset | NTS-1 control |
|---|---|---|---|
| Synth Macro 1 | CC 80 | CC 54 | Oscillator shape |
| Synth Macro 2 | CC 81 | CC 55 | Oscillator alt |
| Synth Macro 5 | CC 84 | CC 43 | Filter cutoff |
| Synth Macro 6 | CC 85 | CC 44 | Filter resonance |

This follows the Circuit's own layout, where Macros 1–2 are oscillator knobs and 5–6 are filter knobs. Set the NTS-1 to the Synth 1 or Synth 2 MIDI channel, and turn on its MIDI RX ShortMessage setting.

### Rules

- The new number is used for MIDI out and MIDI in.
- Both synths share one layout. Each still uses its own MIDI channel, which you set in Settings view (hold Shift while powering on).
- If another Circuit parameter on that channel already uses the number, the two swap. This covers every CC in the Circuit Programmer's Reference, not just the knobs on the page, and a note under the field names the parameter and its new number. The NTS-1 preset moves the synth's ring mod level from CC 54 to CC 80.
- Use 1–119, except 6, 32, 38 and 98–101. Those carry NRPN, RPN and bank select messages. **Performance v2 also reserves synth CC 1** for its mod-wheel source, preventing one message from driving two controls. CC 1 remains assignable on the previous v0.5.0 build.
- The knobs still control the Circuit's own sound as well.
- To undo, upload again with stock numbers.

### Command line

The same patch is available as a Python builder with a Thumb-emulated verifier. The CLI defaults to performance v2. Pass `--base-sysex docs/firmware/circuit-3592-filter-lfo-shift-automation.syx` to use v0.5.0 instead. The browser and Python builders share a pinned NTS-1 reference hash for each base.

```bash
python cc-remap/build_circuit_cc_remap.py --preset nts1
python cc-remap/verify_circuit_cc_remap.py build/cc-remap/nts1/manifest.json
```

`--list` prints every control and its current number. `--map synth.macro1=74` sets your own; drum controls are named like `drum1.pitch` or `drum2.filter`.

## Compatibility and recovery

- Original Novation Circuit only.
- Requires firmware 1.8 build 3592.
- Not compatible with Circuit Tracks, Circuit Rhythm, or Circuit Mono Station.
- If recovery is needed, enter the same bootloader mode and reinstall stock firmware through [Novation Components](https://components.novationmusic.com/).

Custom firmware always carries risk. Keep reliable power and do not disconnect the Circuit during transfer.

## Source and legal

Patch and verification sources are in [`tools/`](tools/), [`experimental/`](experimental/), [`performance/`](performance/), and [`cc-remap/`](cc-remap/). Builders retain exact SHA-256 guards for their supported bases and fixed-size image checks.

The MIT license applies to this project's original code and documentation. Novation and Circuit are trademarks of their respective owner. This independent project is not affiliated with or endorsed by Novation.

Created and hardware-tested by [tracelistener](https://github.com/tracelistener), with reverse-engineering and implementation assistance from OpenAI Codex.
