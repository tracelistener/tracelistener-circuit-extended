"""Run v2 dispatcher and real ARM DSP setter with modeled SPI completion.

Keeps RAM/cache across messages. This checks transport bytes, target addressing,
and stack/register preservation; it does NOT reproduce IRQ/DMA timing or audio.
"""
import json
import random
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "performance"))
import verify_circuit_perf_controls_v2 as verify
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_SP


class RealSetter(verify.Dispatcher):
    def __init__(self, image):
        super().__init__(image)
        self.uc.mem_map(0x40010000, 0x1000)
        self.uc.hook_add(UC_HOOK_CODE, self.wait, begin=0x0800D8A8, end=0x0800D8A8)
        self.uc.hook_add(UC_HOOK_CODE, self.address, begin=0x08015930, end=0x08015930)
        self.uc.hook_add(UC_HOOK_CODE, self.transfer, begin=0x080158A4, end=0x080158A4)
        self.address_phase = False
        self.cursor = None
        self.writes = []
        self.address_commands = 0
        self.reset_ram(0x0F, (0, 1, 9), (0, 1, 9, 15))
        self.uc.mem_write(0x200020FC, b"\x01")  # stock DSP SPI mode already active
        self.uc.mem_write(0x20000820, struct.pack("<I", 0x40010000))
        self.uc.mem_write(0x20000830, struct.pack("<I", 0x40010100))
        self.uc.mem_write(0x20000824, struct.pack("<H", 1))
        self.uc.mem_write(0x20000834, struct.pack("<H", 2))

    def _setter(self, uc, address, size, context):
        # Observe but execute the actual stock setter and its address helper.
        self.trace.append(("dsp", uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)))

    def wait(self, uc, address, size, context):
        self._return()  # model completion, not its physical timing

    def address(self, uc, address, size, context):
        self.address_phase = True

    def transfer(self, uc, address, size, context):
        packet = tuple(uc.reg_read(reg) & 255 for reg in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
        if self.address_phase:
            assert packet[0] == 0
            self.cursor = (packet[1] << 8) | packet[2]
            self.address_phase = False
            self.address_commands += 1
        else:
            assert self.cursor is not None
            self.writes.append((self.cursor, (packet[0] << 16) | (packet[1] << 8) | packet[2]))
            self.cursor = (self.cursor + 1) & 0xFFFF
        # Actual transfer returns received data in r0. Exercise a nonzero value.
        uc.reg_write(UC_ARM_REG_R0, 0xA1B2C300)
        self._return()


image = (ROOT / "build/circuit-perf-controls-v2/feature/circuit-3592-extended-v0.5.0-perf-v2-feature.bin").read_bytes()
machine = RealSetter(image)
rng = random.Random(3592)
callee_saved = (UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)
for i, reg in enumerate(callee_saved):
    machine.uc.reg_write(reg, 0xAABB0000 + i)

total = 24000
write_count = 0
for index in range(total):
    channel = (index // 6) % 2
    value = rng.randrange(16384)
    kind = index % 6
    if kind == 0:
        message = bytes((0xE0 | channel, value & 127, value >> 7))
        expected = (0x2D7 + channel * 0x172, ((value - 8192) << 10) & 0xFFFFFF)
    elif kind == 1:
        message = bytes((0xB0 | channel, 1, value & 127))
        expected = (0x2D8 + channel * 0x172, (value & 127) << 16)
    elif kind == 2:
        message = bytes((0xD0 | channel, value & 127, 0))
        expected = (0x2D9 + channel * 0x172, (value & 127) << 16)
    else:
        status = (0x90, 0x80, 0xB0)[kind - 3] | channel
        message = bytes((status, 60 if kind != 5 else 28, value & 127))
        expected = None
    machine.writes = []
    machine.run(message)
    assert machine.writes == ([] if expected is None else [expected]), (index, machine.writes, expected)
    for i, reg in enumerate(callee_saved):
        assert machine.uc.reg_read(reg) == 0xAABB0000 + i, (index, reg)
    assert machine.uc.reg_read(UC_ARM_REG_SP) == verify.STACK
    write_count += expected is not None

result = {"messages": total, "real_setter_writes": write_count, "address_commands": machine.address_commands, "cache_hits": write_count - machine.address_commands, "result": "PASS", "limitation": "SPI completion modeled; no hardware timing, full synth DSP, or audio simulation"}
print(json.dumps(result))
