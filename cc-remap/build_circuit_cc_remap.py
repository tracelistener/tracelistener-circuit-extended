"""Build an original-Circuit firmware with remapped MIDI CC numbers.

Run from anywhere; paths resolve from the repository root:

    python cc-remap\\build_circuit_cc_remap.py --list
    python cc-remap\\build_circuit_cc_remap.py --preset nts1
    python cc-remap\\build_circuit_cc_remap.py --label mine --map synth.macro1=74 --map synth.macro2=71

The default base is the experimental performance-v2 browser-uploader image.
--base-sysex also accepts the published v0.5.0 image or a legitimate
stock 1.8 build 3592 update.  Pass --stock-sysex to write a stock recovery
copy beside the output.

Controls: synth.macro1..synth.macro8, drum1..drum4.{patch,level,pitch,decay,
distortion,filter,pan}, or <synth|drums|session>.cc<N> for whatever sends
stock CC N. Numbers 1-119 are allowed except 6, 32, 38 and 98-101.
Synth CC 1 is also reserved on performance-v2 for the mod-wheel source.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CC_REMAP_DIR = Path(__file__).resolve().parent
ROOT = CC_REMAP_DIR.parent
sys.path.insert(0, str(CC_REMAP_DIR))
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from circuit_cc_remap_patch import (
    EXTENDED_V050_IMAGE_SHA256,
    EXTENDED_V050_SYSEX_SHA256,
    PERF_V2_IMAGE_SHA256,
    PERF_V2_SYSEX_SHA256,
    DETENTS_IMAGE_SHA256,
    DETENTS_SYSEX_SHA256,
    DETENTS_REFERENCE_BUILDS,
    SELECTORS_IMAGE_SHA256,
    SELECTORS_SYSEX_SHA256,
    SELECTORS_REFERENCE_BUILDS,
    LFO_RATES_IMAGE_SHA256,
    LFO_RATES_SYSEX_SHA256,
    LFO_RATES_REFERENCE_BUILDS,
    PARTS,
    PRESETS,
    REFERENCE_BUILDS,
    PERF_V2_REFERENCE_BUILDS,
    RESERVED_CCS,
    STOCK_IMAGE_SHA256,
    STOCK_SYSEX_SHA256,
    apply_remap,
    check_layout,
    check_stock_ccs,
    describe,
    expected_offsets,
    final_maps,
    forward_cc_map,
    has_performance_controls,
    parse_assignment,
    plan_remap,
    sha256,
)
from circuit_fw_tools import decode_firmware, encode_firmware


DEFAULT_BASE = ROOT / "docs" / "firmware" / "circuit-3592-extended-v0.5.0-perf-v2-feature.syx"
OUTPUT_ROOT = ROOT / "build" / "cc-remap"
BASES = {
    PERF_V2_SYSEX_SHA256: ("extended-v0.5.0-perf-v2", PERF_V2_IMAGE_SHA256),
    DETENTS_SYSEX_SHA256: ("extended-v0.5.0-perf-v2-detents", DETENTS_IMAGE_SHA256),
    SELECTORS_SYSEX_SHA256: ("extended-v0.5.0-perf-v2-selectors", SELECTORS_IMAGE_SHA256),
    LFO_RATES_SYSEX_SHA256: ("extended-v0.5.0-perf-v2-lfo-rates", LFO_RATES_IMAGE_SHA256),
    EXTENDED_V050_SYSEX_SHA256: ("extended-v0.5.0", EXTENDED_V050_IMAGE_SHA256),
    STOCK_SYSEX_SHA256: ("stock", STOCK_IMAGE_SHA256),
}


def write_once(path: Path, data: bytes) -> None:
    """Never overwrite a different artifact: an earlier build may be the tested one."""
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"{path} already exists with different content; choose a new --label")
        return
    path.write_bytes(data)


def print_map(image: bytes) -> None:
    for name, part in PARTS.items():
        print(f"{name} (default MIDI channel {part.default_channel}):")
        for record, cc in sorted(forward_cc_map(image, part).items(), key=lambda item: item[1]):
            print(f"  CC {cc:3d}  {describe(part, record, cc)}")
    reserved = ", ".join(str(cc) for cc in sorted(RESERVED_CCS) if cc)
    print(f"assignable CC numbers: 1-119 except {reserved}")
    if has_performance_controls(image):
        print("synth CC 1 is additionally reserved for the performance mod-wheel source")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-sysex", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--stock-sysex", type=Path, help="stock 3592 SysEx to copy beside the output for recovery")
    parser.add_argument("--preset", choices=sorted(PRESETS))
    parser.add_argument("--map", action="append", default=[], metavar="CONTROL=CC")
    parser.add_argument("--label", help="output folder name (default: the preset name)")
    parser.add_argument("--list", action="store_true", help="print the base's CC map and exit")
    args = parser.parse_args()

    base_sysex = args.base_sysex.read_bytes()
    base_sysex_hash = sha256(base_sysex)
    if base_sysex_hash not in BASES:
        raise SystemExit(f"{args.base_sysex} is not a verified base (sha256 {base_sysex_hash})")
    base_name, base_image_hash = BASES[base_sysex_hash]
    base_image, messages = decode_firmware(base_sysex)
    if sha256(base_image) != base_image_hash or encode_firmware(base_image, messages) != base_sysex:
        raise SystemExit("base SysEx does not decode to the verified image")
    check_layout(base_image)
    check_stock_ccs(base_image)

    if args.list:
        print_map(base_image)
        return

    requests: dict[str, int] = dict(PRESETS.get(args.preset, {}))
    try:
        for item in args.map:
            control, cc = parse_assignment(item)
            requests[control] = cc
    except ValueError as error:
        parser.error(str(error))
    if not requests:
        parser.error("give --preset and/or at least one --map CONTROL=CC")
    label = args.label or args.preset
    if not label:
        parser.error("--label is required without --preset")

    try:
        changes = plan_remap(base_image, requests)
    except ValueError as error:
        raise SystemExit(f"cannot build this map: {error}") from None
    image = apply_remap(base_image, changes)
    sysex = encode_firmware(image, messages)
    decoded, _ = decode_firmware(sysex)
    if decoded != image or len(sysex) != len(base_sysex):
        raise SystemExit("rebuilt SysEx failed the fixed-size round trip")
    references = {"extended-v0.5.0": REFERENCE_BUILDS,
                  "extended-v0.5.0-perf-v2": PERF_V2_REFERENCE_BUILDS,
                  "extended-v0.5.0-perf-v2-detents": DETENTS_REFERENCE_BUILDS,
                  "extended-v0.5.0-perf-v2-selectors": SELECTORS_REFERENCE_BUILDS,
                  "extended-v0.5.0-perf-v2-lfo-rates": LFO_RATES_REFERENCE_BUILDS}.get(base_name, {})
    reference = references.get(args.preset) if not args.map else None
    if reference and sha256(sysex) != reference:
        raise SystemExit(f"preset {args.preset} does not match its reference build {reference}")

    output_dir = OUTPUT_ROOT / label
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"circuit-3592-{base_name}-cc-{label}"
    write_once(output_dir / f"{stem}.syx", sysex)
    write_once(output_dir / f"{stem}.bin", image)
    if base_name == "stock":
        write_once(output_dir / "circuit-3592-stock-recovery.syx", base_sysex)
    else:
        write_once(output_dir / f"circuit-3592-{base_name}.syx", base_sysex)
    if args.stock_sysex:
        stock = args.stock_sysex.read_bytes()
        if sha256(stock) != STOCK_SYSEX_SHA256:
            raise SystemExit(f"{args.stock_sysex} is not the verified stock 3592 SysEx")
        write_once(output_dir / "circuit-3592-stock-recovery.syx", stock)

    maps = final_maps(base_image, changes)
    entries = [
        {
            "part": change.part,
            "record": change.record,
            "control": describe(PARTS[change.part], change.record, change.old_cc),
            "old_cc": change.old_cc,
            "new_cc": change.new_cc,
            "requested": change.requested,
        }
        for change in changes
    ]
    manifest = {
        "base": {
            "name": base_name,
            "sysex": str(args.base_sysex.resolve()),
            "sysex_sha256": base_sysex_hash,
            "image_sha256": sha256(base_image),
        },
        "output": {
            "sysex": f"{stem}.syx",
            "sysex_sha256": sha256(sysex),
            "image": f"{stem}.bin",
            "image_sha256": sha256(image),
            "sysex_bytes": len(sysex),
        },
        "requested": requests,
        "changes": entries,
        "changed_offsets": expected_offsets(base_image, changes),
        "final_cc_maps": {name: {str(record): cc for record, cc in sorted(forward.items())} for name, forward in maps.items()},
    }
    write_once(output_dir / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))

    print(f"base: {base_name} ({args.base_sysex.name})")
    for entry in entries:
        why = "requested" if entry["requested"] else "displaced (swap)"
        print(f"  {entry['control']:32s} CC {entry['old_cc']:3d} -> {entry['new_cc']:3d}   {why}")
    print(f"changed bytes: {len(manifest['changed_offsets'])}")
    if reference:
        print(f"matches the {args.preset} reference build")
    print(f"sysex: {output_dir / (stem + '.syx')}")
    print(f"sha256: {sha256(sysex)}")


if __name__ == "__main__":
    main()
