"""Build performance v2 with detented Shift + Macro 5/6 Distortion Type selection.

    python performance/build_circuit_selector_detents.py

Applies circuit_selector_detents_patch to the published performance-v2 SysEx
and writes build/circuit-selector-detents/. Outputs are write-once, and the
result is pinned: the builder refuses to write anything else.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import circuit_selector_detents_patch as patch
from circuit_fw_tools import decode_firmware, encode_firmware


BASE_SYSEX = REPO_ROOT / "docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-feature.syx"
OUTPUT_DIR = REPO_ROOT / "build/circuit-selector-detents"
OUTPUT_NAME = "circuit-3592-extended-v0.5.0-perf-v2-detents.syx"
PINNED_SYSEX_SHA256 = "005514b86425391cf8944fb2878f35f8fd3ff37a84292de9bfb86274e0344aaf"


def write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"{path} exists with different content")
        return
    path.write_bytes(data)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    base_sysex = BASE_SYSEX.read_bytes()
    if patch.sha256(base_sysex) != patch.PERF_V2_SYSEX_SHA256:
        raise SystemExit(f"{BASE_SYSEX} is not the published performance-v2 SysEx")
    base_image, messages = decode_firmware(base_sysex)
    if encode_firmware(base_image, messages) != base_sysex:
        raise SystemExit("base SysEx does not round-trip")

    image, manifest = patch.apply(base_image)
    sysex = encode_firmware(image, messages)
    if len(sysex) != len(base_sysex) or decode_firmware(sysex)[0] != image:
        raise SystemExit("patched SysEx does not round-trip")
    digest = patch.sha256(sysex)
    if digest != PINNED_SYSEX_SHA256:
        raise SystemExit(f"output {digest} does not match the pinned build {PINNED_SYSEX_SHA256}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_once(OUTPUT_DIR / OUTPUT_NAME, sysex)
    manifest.update(
        base_sysex_sha256=patch.PERF_V2_SYSEX_SHA256,
        image_sha256=patch.sha256(image),
        sysex_sha256=digest,
        sysex=OUTPUT_NAME,
        messages=len(messages),
        hardware_status="untested",
    )
    write_once(OUTPUT_DIR / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    print(f"{manifest['changed_image_bytes']} bytes changed  sysex sha256 {digest}")
    print(f"          {OUTPUT_DIR / OUTPUT_NAME}")


if __name__ == "__main__":
    main()
