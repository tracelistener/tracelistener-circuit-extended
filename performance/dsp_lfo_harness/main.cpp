// Runs the drum Filter LFO routine at P:$0001 of a Circuit DSP program image
// in the dsp56300 project's emulator (https://github.com/dsp56300/dsp56300).
// Used by performance/verify_circuit_lfo_rates.py --dsp-emulator.
//
// stdin:  "P <count> <word> <word> ..."   the DSP program image (hex words)
//         then one line per case: "<drum 0..3> <X:$65 counter> <packed word>" (hex)
// stdout: "<y1> <a1> <instructions>" per case (hex, hex, decimal)
//
// A stub at P:$3000 sets r2 to the drum's parameter block, calls P:$0001,
// stores y1 and a1 to X memory and spins.
#include <cstdio>
#include <iostream>
#include "dsp56kEmu/dsp.h"
#include "dsp56kEmu/memory.h"
#include "dsp56kEmu/peripherals.h"

using namespace dsp56k;

static DefaultMemoryValidator g_validator;

int main()
{
	Peripherals56362 periphX;
	Peripherals56367 periphY;
	Memory mem(g_validator, 0x080000, 0x800000, 0x200000);
	DSP dsp(mem, &periphX, &periphY);

	std::string tag;
	size_t count;
	std::cin >> tag >> count;
	for (size_t i = 0; i < count; ++i)
	{
		unsigned word;
		std::cin >> std::hex >> word;
		mem.set(MemArea_P, static_cast<TWord>(i), word);
	}

	const TWord stub = 0x3000;
	const unsigned drumBase[4] = {0x9f, 0xa9, 0xb3, 0xbd};
	TWord code[] = {
		0x62F400, 0x00009F,  // move #>drum,r2
		0x0BF080, 0x000001,  // jsr $0001
		0x477000, 0x003F00,  // move y1,x:>$3f00
		0x547000, 0x003F01,  // move a1,x:>$3f01
		0x0AF080, stub + 8,  // jmp *
	};

	unsigned drum, counter, packed;
	while (std::cin >> std::hex >> drum >> counter >> packed)
	{
		code[1] = drumBase[drum & 3];
		for (int i = 0; i < 10; ++i)
			mem.set(MemArea_P, stub + i, code[i]);
		dsp.clearOpcodeCache();
		mem.set(MemArea_X, 0x65, counter & 0xffffff);
		mem.set(MemArea_X, drumBase[drum & 3] + 5, packed & 0xffffff);
		mem.set(MemArea_X, 0x3f00, 0xdead01);
		mem.set(MemArea_X, 0x3f01, 0xdead02);
		dsp.setPC(stub);
		int steps = 0;
		while (dsp.getPC().var != stub + 8 && steps < 1000)
		{
			dsp.exec();
			++steps;
		}
		std::printf("%06x %06x %d\n", mem.get(MemArea_X, 0x3f00), mem.get(MemArea_X, 0x3f01), steps);
	}
	return 0;
}
