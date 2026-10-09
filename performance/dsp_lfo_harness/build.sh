#!/bin/sh
# Build the DSP LFO harness against the dsp56300 emulator (Linux/macOS).
#   sh performance/dsp_lfo_harness/build.sh /path/to/workdir
# Clones dsp56300 into the work directory, builds its emulator library in
# interpreter mode, and links main.cpp. Prints the harness path.
set -eu
WORK=${1:?usage: build.sh WORKDIR}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$WORK"
cd "$WORK"
[ -d dsp56300 ] || git clone --depth 1 https://github.com/dsp56300/dsp56300
git -C dsp56300 submodule update --init --depth 1 source/asmjit
cmake -S dsp56300 -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DDSP56K_FORCE_INTERPRETER=ON >/dev/null
cmake --build build --target dsp56kDisassemble -j4 >/dev/null
FLAGS=build/source/disassemble/CMakeFiles/dsp56kDisassemble.dir/flags.make
DEFS=$(grep CXX_DEFINES "$FLAGS" | cut -d= -f2-)
INCS=$(grep CXX_INCLUDES "$FLAGS" | cut -d= -f2-)
CXXF=$(grep CXX_FLAGS "$FLAGS" | cut -d= -f2-)
# shellcheck disable=SC2086
g++ $CXXF $DEFS $INCS -Idsp56300/source "$HERE/main.cpp" -o lfo_harness \
  build/source/dsp56kEmu/libdsp56kEmu.a build/source/dsp56kBase/libdsp56kBase.a \
  build/source/asmjit/libasmjit.a build/source/vtuneSdk/libvtuneSdk.a -lpthread -ldl
echo "$WORK/lfo_harness"
echo "disassembler: $WORK/build/source/disassemble/dsp56kDisassemble"
