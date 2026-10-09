"""Build the selectors firmware with musical Filter LFO speeds.

    python performance/build_circuit_lfo_rates.py

Layers, in order, on the published performance-v2 SysEx:

1. circuit_selector_detents_patch: Distortion Type moves one type per three
   encoder steps (the published detents build, hardware-tested 2026-10-09);
2. circuit_lfo_wrap_patch: the Filter LFO wraps between Off and the fastest
   sawtooth, and Filter movement never switches it Off;
3. circuit_lfo_rates_patch: Filter LFO speeds ~0.73, 1.5, 2.9, 5.9 Hz, each
   as a triangle and then a sawtooth.

The image after layer 2 must be the published selectors build.
Writes build/circuit-lfo-rates/. Outputs are write-once and pinned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import circuit_lfo_rates_patch as lfo_rates
import circuit_lfo_wrap_patch as lfo_wrap
import circuit_selector_detents_patch as detents
from circuit_fw_tools import decode_firmware, encode_firmware


BASE_SYSEX = REPO_ROOT / "docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-feature.syx"
OUTPUT_DIR = REPO_ROOT / "build/circuit-lfo-rates"
OUTPUT_NAME = "circuit-3592-extended-v0.5.0-perf-v2-lfo-rates.syx"
PINNED_SYSEX_SHA256 = "258b413ca23fdea23129f0c0040d1eb206543299965d6610feaf1929b2bd996b"


def write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"{path} exists with different content")
        return
    path.write_bytes(data)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    base_sysex = BASE_SYSEX.read_bytes()
    if detents.sha256(base_sysex) != detents.PERF_V2_SYSEX_SHA256:
        raise SystemExit(f"{BASE_SYSEX} is not the published performance-v2 SysEx")
    base_image, messages = decode_firmware(base_sysex)
    if encode_firmware(base_image, messages) != base_sysex:
        raise SystemExit("base SysEx does not round-trip")

    detents_image, detents_manifest = detents.apply(base_image)
    if detents.sha256(encode_firmware(detents_image, messages)) != lfo_wrap.DETENTS_SYSEX_SHA256:
        raise SystemExit("intermediate image is not the published detents build")
    selectors_image, lfo_manifest = lfo_wrap.apply(detents_image)
    if detents.sha256(encode_firmware(selectors_image, messages)) != lfo_rates.SELECTORS_SYSEX_SHA256:
        raise SystemExit("intermediate image is not the published selectors build")
    image, rates_manifest = lfo_rates.apply(selectors_image)

    sysex = encode_firmware(image, messages)
    if len(sysex) != len(base_sysex) or decode_firmware(sysex)[0] != image:
        raise SystemExit("patched SysEx does not round-trip")
    digest = detents.sha256(sysex)
    if digest != PINNED_SYSEX_SHA256:
        raise SystemExit(f"output {digest} does not match the pinned build {PINNED_SYSEX_SHA256}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_once(OUTPUT_DIR / OUTPUT_NAME, sysex)
    manifest = {
        "base_sysex_sha256": detents.PERF_V2_SYSEX_SHA256,
        "layers": [detents_manifest, lfo_manifest, rates_manifest],
        "changed_image_bytes": sum(1 for a, b in zip(base_image, image) if a != b),
        "image_sha256": detents.sha256(image),
        "sysex_sha256": digest,
        "sysex": OUTPUT_NAME,
        "messages": len(messages),
        "hardware_status": "Distortion Type stepping and Filter LFO wrap/centre fix as in the selectors build; new LFO speeds untested",
    }
    write_once(OUTPUT_DIR / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    print(f"{manifest['changed_image_bytes']} bytes changed from performance v2  sysex sha256 {digest}")
    print(f"          {OUTPUT_DIR / OUTPUT_NAME}")


if __name__ == "__main__":
    main()
