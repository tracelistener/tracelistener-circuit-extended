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

## Shift selector fixes

`build_circuit_selectors.py` applies two layers to performance v2 and publishes `docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-selectors.syx`. The intermediate image after the first layer is the published detents build (`…-perf-v2-detents.syx`); the uploader now offers only the combined build.

```sh
python performance/build_circuit_selectors.py
python performance/verify_circuit_lfo_wrap.py build/circuit-selectors/circuit-3592-extended-v0.5.0-perf-v2-selectors.syx
```

Feature SysEx SHA-256:

```text
54aa4e9ac25f87d16d796be6740fbbb35ee55bf496c31f2fb0db3be5c4551480
```

334 image bytes differ from performance v2.

### Filter LFO wrap and centre fix

Hardware feedback on 2026-10-09: the Filter LFO selector stopped at both ends and sometimes would not move. With the LFO on, turning Filter down switched the LFO Off, and it stayed off when Filter was turned back up.

- **Wrap.** Clockwise from mode 19 goes to Off; counter-clockwise from Off goes to mode 19 with its centre seeded from the Filter amount, as leaving Off clockwise already did. The rates only increase from mode 12 to 19 because the DSP uses the mode as the rate shift.
- **No reset on the Filter centre.** The normal Filter path set the mode to Off when the Filter value was exactly 64 and `STOCK_BUTTON_PRESSED(0x19)` read zero, on the assumption that 0x19 is an active-low Clear line. In stock code, 0x19 is queried only by the power-on button-combination check at `0x0800A304`, and the hardware result shows it can read zero with Clear released. The check is removed, so Filter movement never changes the LFO mode. This also removes the Clear + Macro 7/8 reset of the LFO.

`circuit_lfo_wrap_patch.py` first reassembles the shipped wrapper and refuses any base where it differs. It rewrites the Filter wrapper `0x08035B4C..0x08035BF8` (the unused tail is erased to 0xFF) and Filter record predictor B `0x08036B08..0x08036B28`.

`verify_circuit_lfo_wrap.py` runs the live wrapper from each drum's entry stub against a reference model for 24,000 Shift events, including 611 wraps. It checks 600 Shift-released events against the detents build and confirms the patch never queries 0x19. It replays the reported fault, a Filter sweep from 90 to 0 with 0x19 reading zero: the detents build turns the LFO Off, the patch keeps it. It also checks the recorder's Filter tag against the wrapper in 924 cases, and 580 other recorder inputs against the base.

Hardware status: the Distortion Type stepping below was tested on 2026-10-09. The Filter LFO changes and the recorder prediction are verified in emulation only.

### Detented Distortion Type

`build_circuit_selector_detents.py` builds the first layer for Shift + Macro 5/6 and publishes `docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-detents.syx`. Hardware test on 2026-10-09: the stepping "works great". Recording has not been tested yet.

- **Fewer skipped types.** v0.5.0 and performance v2 advance Distortion Type on every encoder step, so a small turn skips several of the seven types. The wrapper now uses the Filter LFO's three-step divider at `0x08036A0C`. The type still wraps: direction comes from the stock Distortion Amount proposal, which the stock code clamps at 0 and 127. With the amount at 0, only clockwise turns register, and without the wrap, lower types would be unreachable.
- **Recorded automation matches what plays.** Hardware testing showed that the recorder sees a Shift + Filter encoder event before the wrapper, so the Filter lane predicts the new mode. The Distortion lane goes through the same recorder hook but recorded the current, pre-step type. It now predicts the wrapper's result with the same selector routine. This defect is shown in emulation, not on hardware.

The patch rewrites only the Distortion wrapper slot `0x08025CD4..0x08025D5C` and the recorder `0x08035976..0x080359F8`. Both slots already held this project's code, and the recorder's `record_tag` stays at `0x080359E8`. 176 image bytes differ from performance v2.

```sh
python performance/build_circuit_selector_detents.py
python performance/verify_circuit_selector_detents.py build/circuit-selector-detents/circuit-3592-extended-v0.5.0-perf-v2-detents.syx
```

Feature SysEx SHA-256:

```text
005514b86425391cf8944fb2878f35f8fd3ff37a84292de9bfb86274e0344aaf
```

The verifier confirms that only the two slots changed, that nothing else loads the retired literal at `0x08025D58`, and that the Filter LFO code is unchanged. It runs the live wrapper from each drum's entry stub against a reference model for 20,421 Shift events, using the real parameter-restore helper. It checks the recorder's Distortion tag against the wrapper in 588 cases, and 696 other recorder inputs against performance v2. Against the shipped build, both the model and the recorder checks fail, which confirms the two defects. Stock services and the DSP are mocked.

Not addressed by either layer: the end-stop limit (the wrap keeps every choice reachable, but turning toward the end stop still registers nothing) and the shared, unreset step counter described in the main README. The same callback runs when any of the drum's parameters are refreshed, so guessing a direction at an end stop could step the selector without a knob turn.

## Filter LFO speeds

`build_circuit_lfo_rates.py` adds a third layer to the selectors build and publishes `docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-lfo-rates.syx` (SHA-256 `258b413ca23fdea23129f0c0040d1eb206543299965d6610feaf1929b2bd996b`). Not yet tested on hardware.

The drum Filter LFO is a 35-word DSP routine at `P:$0001..$0023`. It used the mode (12..19) as the left shift applied to the audio-block counter `X:$65`. With 1500 blocks per second, the rates were 0.37..2.9 Hz triangle, then 5.9..47 Hz sawtooth. The new routine is the same size, keeps the entry, mode validation, Off path and depth maths, and the `saw`, `mod` and `off` labels stay at the same addresses. Only the rate and shape selection change:

- rate shift = (mode >> 1) + 7, giving 13, 13, 14, 14, 15, 15, 16, 16: about 0.73, 1.5, 2.9 and 5.9 Hz;
- even modes are triangle, odd modes sawtooth. The low mode bit is shifted into `b0` bit 23 and tested with `btst`.

The ARM side is unchanged. Modes are still Off and 12..19, so the selector, wrap, recorder and playback code is byte-identical to the selectors build. Recorded LFO automation from earlier builds replays with the new speed and shape for each mode.

```sh
python performance/build_circuit_lfo_rates.py
sh performance/dsp_lfo_harness/build.sh /tmp/dsp56300-work      # optional, needs git, cmake, g++
python performance/verify_circuit_lfo_rates.py build/circuit-lfo-rates/circuit-3592-extended-v0.5.0-perf-v2-lfo-rates.syx \
  --dsp-emulator /tmp/dsp56300-work/lfo_harness \
  --disassembler /tmp/dsp56300-work/build/source/disassemble/dsp56kDisassemble
```

Without the optional tools, the verifier checks that only the 35 DSP words changed and that the label positions are unchanged. With the [dsp56300](https://github.com/dsp56300/dsp56300) disassembler, every new word must disassemble to the intended instruction. With its emulator, the shipped routine and the new routine are each run on 4,000 random cases through `performance/dsp_lfo_harness` and compared with Python models. The shipped routine has to match first, which checks the harness and the fixed-point model before the new routine is judged. Both take at most 29 instructions per call.

### Tempo sync findings (not implemented)

Notes for a future tempo-synced build:

- On every tempo change, the ARM computes x = BPM × 2³² / 60000 at `0x0801DAFE` (BPM range 40–240) and calls `0x08016C9C`. That writes **`X:$15` = x × 96** (top 24 bits) and **`X:$16`**, proportional to 1/BPM. The boot default is 120 BPM. External MIDI clock reaches the same writer.
- The stock synth's tempo-synced LFO step is `X:$15 × X:($87B + i) >> X:$40` (DSP `P:$06F2`, `P:$0FD4`). The ARM fills `X:$87B..$89D` at boot (`0x08009106`) from the cycle lengths at `0x08025B00`, in 24-per-beat ticks: 6 = 1/16, 24 = one beat, 96 = one bar. Using the same formula makes the drum LFO match the synth's sync exactly, whatever the block length. If `X:$40` is 0, the formula implies 32-sample blocks at 48 kHz, matching the Hz values above.
- Blocker: tempo sync needs about 10 more DSP words than the 35-word slot holds. `P:$0024..$003D`, `P:$0070..$00B3` and `P:$00DC..$00FF` hold ARM code (sample playback helper, Distortion wrapper, automation decoder). The zero runs in the main program are data tables. Freeing room means moving or shrinking the 70-byte ARM sample playback helper at `0x08025BE0`.

## Implementation

The hook at ARM `0x08020508` branches to a 92-byte handler in the unused newlib startup area at `0x080081E4`. It calls the stock DSP setter and resumes the original dispatcher. Only 91 bytes differ from v0.5.0.

Revision 1 wrote the smoothed Y-memory outputs, which the DSP immediately pulled back toward unchanged X-memory targets. Revision 2 writes Synth 1 targets `X:$2D7..$2D9` and Synth 2 targets `X:$449..$44B`. The existing DSP smoother carries them into the synth’s Y-memory controller values. Full-scale bend and zero-centre encoding are verified independently against the embedded DSP instruction stream.
