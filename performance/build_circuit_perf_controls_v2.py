"""Build the pitch bend / mod wheel / aftertouch receive firmware for the original Circuit.

    python performance/build_circuit_perf_controls_v2.py

Writes two images on top of the hardware-validated Circuit Extended v0.5.0:

* ``isolation``: the handler bytes are placed in the dead newlib ``_start``
  region, but nothing calls them. This is an emulator comparison artifact.
* ``feature``: the same bytes plus the 4-byte receive-dispatcher hook.

Outputs go to build/circuit-perf-controls-v2/<variant>/ and are write-once.
See performance/README.md for the limited hardware results and known issues.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import circuit_perf_controls_v2_patch as patch
from circuit_fw_tools import decode_firmware, encode_firmware


BASE_SYSEX = (
    REPO_ROOT
    / "docs/firmware"
    / "circuit-3592-filter-lfo-shift-automation.syx"
)
BASE_COPY = "circuit-3592-extended-v0.5.0.syx"
OUTPUT_ROOT = REPO_ROOT / "build/circuit-perf-controls-v2"
VARIANTS = {"isolation": False, "feature": True}


def write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"{path} exists with different content; earlier builds may be the tested ones")
        return
    path.write_bytes(data)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-sysex", type=Path, help="optional exact stock 3592 recovery file to copy")
    args = parser.parse_args()
    base_sysex = BASE_SYSEX.read_bytes()
    if patch.sha256(base_sysex) != patch.EXTENDED_V050_SYSEX_SHA256:
        raise SystemExit(f"{BASE_SYSEX} is not the verified v0.5.0 SysEx")
    base_image, messages = decode_firmware(base_sysex)
    if encode_firmware(base_image, messages) != base_sysex:
        raise SystemExit("base SysEx does not round-trip")
    stock_recovery = None
    if args.stock_sysex:
        stock_recovery = args.stock_sysex.read_bytes()
        stock_image, _ = decode_firmware(stock_recovery)
        if patch.sha256(stock_recovery) != patch.STOCK_SYSEX_SHA256 or patch.sha256(stock_image) != patch.STOCK_IMAGE_SHA256:
            raise SystemExit("stock recovery does not match the pinned stock 3592 hashes")

    for variant, hook in VARIANTS.items():
        image, info = patch.apply(base_image, hook=hook)
        sysex = encode_firmware(image, messages)
        expected = {"feature": "085f4decb43efb3c2738a1ef08851e42939c6e77c10bdb334ba70d70babf0d2e",
                    "isolation": "bdec344a5d06fe3a03cde597129951a38ce42c1bb2ce281313dd5e5f9a843792"}[variant]
        if patch.sha256(sysex) != expected:
            raise SystemExit(f"{variant} does not reproduce its pinned reference hash")
        decoded, _ = decode_firmware(sysex)
        if decoded != image or len(sysex) != len(base_sysex):
            raise SystemExit("rebuilt SysEx failed the fixed-size round trip")
        out = OUTPUT_ROOT / variant
        out.mkdir(parents=True, exist_ok=True)
        stem = f"circuit-3592-extended-v0.5.0-perf-v2-{variant}"
        write_once(out / f"{stem}.syx", sysex)
        write_once(out / f"{stem}.bin", image)
        write_once(out / BASE_COPY, base_sysex)
        if stock_recovery is not None:
            write_once(out / "circuit-3592-stock-recovery.syx", stock_recovery)
        manifest = {
            "base": {
                "name": "extended-v0.5.0",
                "copy": BASE_COPY,
                "sysex_sha256": patch.sha256(base_sysex),
                "image_sha256": patch.sha256(base_image),
            },
            "output": {
                "variant": variant,
                "sysex": f"{stem}.syx",
                "sysex_sha256": patch.sha256(sysex),
                "image": f"{stem}.bin",
                "image_sha256": patch.sha256(image),
                "sysex_bytes": len(sysex),
            },
            "patch": info,
        }
        write_once(out / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        print(f"{variant:9s} {info['changed_byte_count']:3d} bytes changed  sysex sha256 {patch.sha256(sysex)}")
        print(f"          {out / (stem + '.syx')}")


if __name__ == "__main__":
    main()
