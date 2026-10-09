# Performance controls v2 (experimental)

This adds incoming pitch bend, CC 1 mod wheel and channel aftertouch to Synth 1/2 of the original Circuit. It builds on Extended v0.5.0 and keeps its existing features. The published SysEx is byte-identical to the build tested on 2026-10-09.

## Hardware status

Pitch bend worked on hardware. One unexplained crash was reported, followed by stable operation; modulation was later reported inconsistent. The crash was not reproduced in emulation and its cause is unresolved. Long-term stability and the performance-v2 plus CC-remap combination still need hardware testing. This is an experimental build, not a claim of hardware stability.

The browser uploader offers the previous v0.5.0 build for rollback. Stock recovery is available through Novation Components.

## Using the controls

- Send on the synth’s note channel (defaults: Synth 1 = channel 1, Synth 2 = channel 2), with MIDI Note Receive enabled. The hook follows the Note Receive setting, including for mod wheel and aftertouch.
- Pitch bend follows each oscillator’s patch bend range. Stock CC 28 and 40 set the ranges; value 66 gives +2 semitones and value 76 gives +12.
- CC 1 and channel aftertouch become modulation-matrix sources 1 and 2. A patch needs assignments before these make an audible change. The firmware does not install patch assignments.
- For a direct filter sweep without editing the modulation matrix, make the controller send stock CC 74 on the synth channel. Arturia KeyStep owners can set **ModWheel CC = 74** and **ModWheel MIDI Ch = User** in MIDI Control Center. Lower the strip to zero before changing its CC, then reload any patch changed by the test helper.
- Performance v2 reserves synth CC 1 in both CC-remap builders. Mapping another synth control to CC 1 would make it respond alongside the mod-wheel source. Other allowed CC numbers remain available; ordinary CC table changes preserve pitch bend.

References: [KeyStep manual](https://downloads.arturia.net/products/keystep/manual/KeyStep_Manual_1_1_1_EN.pdf), [Circuit MIDI reference](https://fael-downloads-prod.focusrite.com/customer/prod/s3fs-public/downloads/Circuit%20Programmers%20Reference%20Guide%20v1-1_0.pdf).

### Optional patch-routing test

The helper **overwrites modulation slots 19 and 20 in the currently loaded patch**, and optionally changes oscillator bend ranges. It does not save the patch. Reload the original patch to restore its assignments; `--clear` only zeros the two slots and cannot restore their previous contents.

```sh
python performance/setup_circuit_perf_test.py --channel 1 --bend-range 2
```

Slot 20 routes mod wheel × bipolar LFO1 to oscillator pitch; slot 19 routes aftertouch to filter frequency. Vibrato inherits the patch’s LFO1 shape, rate and sync settings, so results vary by patch. This helper is a diagnostic setup, not a universal modulation preset. Use the controller’s normal CC 1 assignment for this test, not CC 74. The helper’s CC numbers assume stock mappings.

## Reproduce and verify

Install Python 3.10+ and the pinned requirements, then run from the repository root:

```sh
python -m pip install -r requirements.txt
python performance/build_circuit_perf_controls_v2.py
python performance/verify_circuit_perf_controls_v2.py build/circuit-perf-controls-v2/feature/manifest.json
python performance/verify_circuit_perf_controls_v2.py build/circuit-perf-controls-v2/isolation/manifest.json
python performance/verify_circuit_perf_setter_stream.py
python cc-remap/build_circuit_cc_remap.py --preset nts1
python cc-remap/verify_circuit_cc_remap.py build/cc-remap/nts1/manifest.json
node scripts/verify_browser_uploader.cjs
```

The builder accepts only the pinned v0.5.0 image. It writes feature and placement-only isolation variants, pins both output hashes, and copies v0.5.0 beside each as a fallback. An optional `--stock-sysex PATH` copies an exact hash-verified stock recovery file. Existing different outputs are never overwritten. Only the feature variant is offered by the uploader.

Feature SysEx SHA-256:

```text
085f4decb43efb3c2738a1ef08851e42939c6e77c10bdb334ba70d70babf0d2e
```

Verification covers 202,000 dispatcher cases per variant, 33,280 exhaustive controller-target cases, the pinned DSP smoothing instructions, and a 24,000-message stream through the real ARM setter with modeled SPI completion. These tests do not emulate full audio, IRQ/DMA timing or long-term hardware behaviour.

## Implementation

The hook at ARM `0x08020508` branches to a 92-byte handler in the unused newlib startup area at `0x080081E4`. It calls the stock DSP setter and resumes the original dispatcher. Only 91 bytes differ from v0.5.0.

Revision 1 wrote the smoothed Y-memory outputs, which the DSP immediately pulled back toward unchanged X-memory targets. Revision 2 writes Synth 1 targets `X:$2D7..$2D9` and Synth 2 targets `X:$449..$44B`. The existing DSP smoother carries them into the synth’s Y-memory controller values. Full-scale bend and zero-centre encoding are verified independently against the embedded DSP instruction stream.
