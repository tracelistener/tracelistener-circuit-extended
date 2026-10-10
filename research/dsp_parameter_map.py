"""List every ARM -> DSP parameter access in a Circuit firmware image.

    python research/dsp_parameter_map.py [SYSEX] [--csv OUT.csv]

Defaults to the published performance-v2 SysEx. For every call to the DSP
setter (0x0801633C, r0 = value, r1 = address) and getter (0x08016E64,
r0 = address) it reports:

* the DSP address, when the call site sets it from a constant ("const") or
  a constant plus an index register ("base+reg"); otherwise how it is
  computed ("expr", or "?" when the scan gives up);
* the memory space: address bit 15 set = Y memory, clear = X memory (the
  DSP host-command handler at P:$0167..$0191 tests bit 15);
* for writes, the stock parameter getter whose result feeds the value, as
  group/index (group 0x10 = drums, 0x20 = session; see research/
  dsp-parameter-map.md), or the call that produced it;
* whether the site lies in code added by this project.

The scan is a short backward walk inside straight-line code, so it is a
starting point for manual analysis, not a complete data-flow analysis.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

from circuit_fw_tools import load


BASE = 0x08008000
DSP_SETTER = 0x0801633C
DSP_GETTER = 0x08016E64
PARAM_GETTERS = {
    0x0800D8E4: "param",
    0x08025594: "param",
    0x0802D520: "drum-pitch",  # project wrapper around the drum pitch parameter (Scale Follow)
}
PROJECT_REGIONS = (
    (0x080081E4, 0x08008258),  # performance-v2 handler
    (0x08025B74, 0x0802D38B),  # ARM code stored inside the DSP program image
    (0x0802D38B, 0x0802D690),  # Scale Follow / Filter LFO helpers
    (0x08035900, 0x08035C00),  # Filter LFO wrapper and recorder
    (0x08036800, 0x08036B70),  # Sample Start, divider and Filter helpers
)
DEFAULT_SYSEX = ROOT / "docs/firmware/circuit-3592-extended-v0.5.0-perf-v2-feature.syx"


def disassemble(image: bytes) -> dict[int, tuple[str, str]]:
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.skipdata = True
    result: dict[int, tuple[str, str]] = {}
    for start in (0, 2):
        for insn in md.disasm(bytes(image[start:]), BASE + start):
            result.setdefault(insn.address, (insn.mnemonic, insn.op_str))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sysex", nargs="?", type=Path, default=DEFAULT_SYSEX)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()

    image, _, _ = load(args.sysex)
    ins = disassemble(image)
    addrs = sorted(ins)
    index = {a: i for i, a in enumerate(addrs)}
    u32 = lambda a: int.from_bytes(image[a - BASE : a - BASE + 4], "little")

    def literal(address: int, ops: str) -> int | None:
        m = re.match(r"r\d+, \[pc, #(0x[0-9a-f]+|\d+)\]", ops)
        return u32(((address + 4) & ~3) + int(m.group(1), 0)) if m else None

    def trace(i: int, reg: str, depth: int = 0) -> tuple[str, object]:
        j = i - 1
        while j >= 0 and i - j <= 14:
            address = addrs[j]
            mnem, ops = ins[address]
            if mnem in ("bl", "blx"):
                return ("?", "set before a call")
            if mnem in ("b", "b.w", "bx") or (mnem.startswith("pop") and "pc" in ops):
                return ("?", "control flow")
            if ops.split(",")[0].strip() == reg:
                if mnem in ("movs", "mov.w", "movw", "mov") and "#" in ops:
                    return ("const", int(ops.split("#")[1], 0) & 0xFFFFFFFF)
                if mnem == "ldr" and "[pc" in ops:
                    return ("const", literal(address, ops))
                if mnem in ("uxth", "uxtb") and ops.endswith(reg):
                    j -= 1
                    continue
                if mnem in ("addw", "add.w", "adds", "add") and "#" in ops:
                    return ("base+reg", (int(ops.split("#")[1], 0), ops.split(",")[1].strip()))
                if mnem in ("adds", "add") and ops.count(",") >= 2 and depth < 3:
                    parts = [p.strip() for p in ops.split(",")]
                    inner = trace(index[address], parts[1], depth + 1)
                    if inner[0] == "const":
                        return ("base+reg", (inner[1], parts[2]))
                    return ("expr", f"{mnem} {ops}")
                if mnem == "mov" and ops.count(",") == 1 and depth < 3:
                    return trace(index[address], ops.split(",")[1].strip(), depth + 1)
                return ("expr", f"{mnem} {ops}")
            j -= 1
        return ("?", "scan limit")

    def value_source(i: int) -> str:
        j = i - 1
        while j >= 0 and i - j <= 16:
            mnem, ops = ins[addrs[j]]
            if mnem == "bl":
                target = int(ops.lstrip("#"), 0)
                if target in PARAM_GETTERS:
                    regs: dict[str, int] = {}
                    k = j - 1
                    while k >= 0 and j - k <= 5 and ins[addrs[k]][0] != "bl":
                        m2, o2 = ins[addrs[k]]
                        if m2 in ("movs", "mov.w") and o2[:4] in ("r0, ", "r1, ") and "#" in o2:
                            regs.setdefault(o2[:2], int(o2.split("#")[1], 0))
                        k -= 1
                    group = regs.get("r0")
                    item = regs.get("r1")
                    return f"{PARAM_GETTERS[target]}(group={group:#x}, index={item:#x})" if group is not None and item is not None else PARAM_GETTERS[target]
                return f"after call {target:#010x}"
            j -= 1
        return ""

    rows = []
    for address in addrs:
        mnem, ops = ins[address]
        if mnem not in ("bl", "b.w") or not ops.startswith("#"):
            continue
        target = int(ops.lstrip("#"), 0)
        if target not in (DSP_SETTER, DSP_GETTER):
            continue
        write = target == DSP_SETTER
        kind, value = trace(index[address], "r1" if write else "r0")
        if kind == "const":
            text = f"{'Y' if value & 0x8000 else 'X'}:${value & 0x7FFF:04X}"
        elif kind == "base+reg":
            text = f"{'Y' if value[0] & 0x8000 else 'X'}:${value[0] & 0x7FFF:04X}+{value[1]}"
        else:
            text = str(value)
        rows.append({
            "site": f"{address:#010x}",
            "access": "write" if write else "read",
            "resolution": kind,
            "dsp_address": text,
            "value_source": value_source(index[address]) if write else "",
            "project_code": "yes" if any(lo <= address < hi for lo, hi in PROJECT_REGIONS) else "",
        })

    if args.csv:
        with args.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    else:
        for row in rows:
            print(f"{row['site']} {row['access']:5} {row['resolution']:8} {row['dsp_address']:28} {row['value_source']}{'  [project]' if row['project_code'] else ''}")
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        counts[(row["access"], row["resolution"])] = counts.get((row["access"], row["resolution"]), 0) + 1
    print(f"{len(rows)} accesses: " + ", ".join(f"{a} {r}: {n}" for (a, r), n in sorted(counts.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
