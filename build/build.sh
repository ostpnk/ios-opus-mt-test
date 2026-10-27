#!/usr/bin/env bash
# Builds libbergamot (capi/: a C interface over Mozilla's translation
# engine, Bergamot on its Marian fork) for one platform, plus two test
# programs: test_capi (the C interface) and the engine's translator-cli.
#
# Usage: build/build.sh <platform> [engine-dir]
#   platform: macos | ios | ios-sim | android | linux
#   engine-dir: what build/fetch-engine.sh produced (default ./engine)
# Output: out/<platform>/{lib/libbergamot.*,include/bergamot_c.h,bin/*}
#
# Written for bash 3.2 too (macOS's /bin/bash): no associative arrays.
set -euo pipefail

PLATFORM="${1:?usage: build.sh <macos|ios|ios-sim|android|linux> [engine-dir]}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${2:-$ROOT/engine}/inference"
BUILD="$ROOT/build-$PLATFORM"
OUT="$ROOT/out/$PLATFORM"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN)}"

# CMake 4 (Homebrew's, on the macOS runners) no longer configures projects
# declaring cmake_minimum_required < 3.5, as cpuinfo's bundled clog does.
# The environment variable (rather than -D) reaches ExternalProjects too.
# CMake 3.x ignores it.
export CMAKE_POLICY_VERSION_MINIMUM=3.5

# Shared by every platform:
#   - GIT_SUBMODULE=OFF: CMake would otherwise `git submodule update` the
#     whole training repo's submodules (fetch-engine.sh already got ours).
#   - PIC everywhere: the engine's static libraries end up inside a shared
#     library.
#   - ruy for float GEMM everywhere (USE_RUY_SGEMM), not OpenBLAS/MKL or
#     Apple Accelerate: one code path on every platform, and no system
#     BLAS for an app to depend on. On x86 int8 still goes through intgemm.
COMMON=(
  -G Ninja
  -DCMAKE_BUILD_TYPE=Release
  -DENGINE_DIR="$ENGINE"
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON
  # The sentence splitter's PCRE2, from the source fetch-engine.sh unpacked
  # (built PIC; a system libpcre2-8.a isn't).
  -DSSPLIT_USE_INTERNAL_PCRE2=ON
  -DGIT_SUBMODULE=OFF
  -DCOMPILE_CPU=ON -DCOMPILE_CUDA=OFF
  -DUSE_FBGEMM=OFF -DUSE_MKL=OFF -DUSE_NCCL=OFF -DUSE_DOXYGEN=OFF
  -DUSE_RUY=ON -DUSE_RUY_SGEMM=ON -DUSE_APPLE_ACCELERATE=OFF
)

case "$PLATFORM" in
  linux)
    # A portable x86-64 baseline, never `native` (the default), which
    # bakes in the build machine's CPU. intgemm picks AVX2/AVX512 kernels
    # at runtime regardless.
    PLATFORM_FLAGS=(-DBUILD_ARCH=x86-64-v2)
    ;;
  android)
    NDK="${ANDROID_NDK:-${ANDROID_NDK_LATEST_HOME:?set ANDROID_NDK}}"
    # API 24, Flutter's own minimum: patches/marian-pathie-android-api24
    # removes pathie-cpp's iconv/glob (bionic API 28) and nl_langinfo (26).
    PLATFORM_FLAGS=(
      -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake"
      -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-24
      -DANDROID_STL=c++_static
      -DBUILD_ARCH=armv8-a
    )
    ;;
  macos)
    # Also as an environment variable: the PCRE2 ExternalProject doesn't
    # receive -DCMAKE_OSX_DEPLOYMENT_TARGET and otherwise targets the
    # runner's own macOS version (ld: "built for newer macOS").
    export MACOSX_DEPLOYMENT_TARGET=12.0
    PLATFORM_FLAGS=(
      -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=12.0
      -DBUILD_ARCH=armv8-a
    )
    ;;
  ios|ios-sim)
    # A toolchain *file*, not -DCMAKE_SYSTEM_NAME=iOS on the command line:
    # ssplit builds PCRE2 as an ExternalProject and forwards only
    # CMAKE_TOOLCHAIN_FILE, so command-line settings would build PCRE2 for
    # macOS instead.
    if [[ "$PLATFORM" == ios ]]; then export IOS_SDK=iphoneos; else export IOS_SDK=iphonesimulator; fi
    PLATFORM_FLAGS=(
      -DCMAKE_TOOLCHAIN_FILE="$ROOT/build/ios.toolchain.cmake"
      -DBUILD_ARCH=armv8-a
    )
    ;;
  *) echo "unknown platform: $PLATFORM" >&2; exit 2 ;;
esac

configure() {
  cmake -S "$ROOT/capi" -B "$BUILD" "${COMMON[@]}" "${PLATFORM_FLAGS[@]}" "$@"
}

# Pass 1, then hand Marian's own compile definitions to the whole build.
# Marian adds them with add_compile_definitions() inside marian-fork/ only,
# so Bergamot's sources would include Marian's headers with different
# definitions — on ARM that switches off its NEON/SSE-emulation code
# (undeclared sin_ps, _mm_*, FE_TONEAREST). Read from the generated
# build.ninja so each platform gets exactly what Marian chose for it.
configure
DEFS=$(awk '
  /^build [^:]*marian-fork\/src\/CMakeFiles\/marian\.dir\/common\/utils\.cpp\.o:/ { f = 1 }
  f && /^  DEFINES = / { sub(/^  DEFINES = /, ""); print; exit }
' "$BUILD/build.ninja")
if [[ -z "$DEFS" ]]; then
  echo "could not read Marian's DEFINES from build.ninja" >&2; exit 1
fi
echo "Marian definitions shared with the whole build: $DEFS"
configure -DCMAKE_CXX_FLAGS="$DEFS"

# -k 0: keep building past a failure, so one run reports every error
# instead of the first (each CI round trip rebuilds Marian from scratch).
# marian-conv only for Linux: catalog/opus_bundles.py converts OPUS-MT models
# to int8 with it.
targets=(bergamot test_capi translator-cli)
[[ "$PLATFORM" == linux ]] && targets+=(marian-conv)
cmake --build "$BUILD" --target "${targets[@]}" -- -j "$JOBS" -k 0

rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/lib" "$OUT/include"
cp "$BUILD"/libbergamot.* "$OUT/lib/"
cp "$ROOT/capi/bergamot_c.h" "$OUT/include/"
cp "$BUILD/test_capi" "$BUILD/engine/src/app/translator-cli" "$OUT/bin/"
[[ "$PLATFORM" == linux ]] && cp "$BUILD/marian-conv" "$OUT/bin/"

# The release copy: stripped (a few MB instead of 12-40 MB of debug info),
# named for its target so all of them can sit side by side in one release.
# Only local symbols go on Apple (strip -x); the bt_* exports must stay.
mkdir -p "$OUT/release"
case "$PLATFORM" in
  android)
    STRIP=$(ls "$NDK"/toolchains/llvm/prebuilt/*/bin/llvm-strip | head -1)
    "$STRIP" --strip-unneeded -o "$OUT/release/libbergamot-android-arm64.so" "$OUT/lib/libbergamot.so"
    ;;
  linux)
    strip --strip-unneeded -o "$OUT/release/libbergamot-linux-x64.so" "$OUT/lib/libbergamot.so"
    ;;
  macos|ios|ios-sim)
    name="libbergamot-$PLATFORM-arm64.dylib"
    [[ "$PLATFORM" == macos ]] && name="libbergamot-macos-arm64.dylib"
    cp "$OUT/lib/libbergamot.dylib" "$OUT/release/$name"
    strip -x "$OUT/release/$name"
    ;;
esac
ls -l "$OUT"/*
