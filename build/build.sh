#!/usr/bin/env bash
# Builds Mozilla's translation engine (Bergamot + its Marian fork) for one
# platform: the translator-cli test binary plus the static libraries an app
# would link.
#
# Usage: build/build.sh <platform> [engine-dir]
#   platform: macos | ios | ios-sim | android | linux
#   engine-dir: what build/fetch-engine.sh produced (default ./engine)
# Output: out/<platform>/{bin,lib}
#
# Written for bash 3.2 too (macOS's /bin/bash): no associative arrays.
set -euo pipefail

PLATFORM="${1:?usage: build.sh <macos|ios|ios-sim|android|linux> [engine-dir]}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${2:-$ROOT/engine}/inference"
BUILD="$ROOT/build-$PLATFORM"
OUT="$ROOT/out/$PLATFORM"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN)}"

# Shared by every platform:
#   - GIT_SUBMODULE=OFF: CMake would otherwise `git submodule update` the
#     whole training repo's submodules (fetch-engine.sh already got ours).
#   - ruy for float GEMM everywhere (USE_RUY_SGEMM), not OpenBLAS/MKL or
#     Apple Accelerate: one code path on every platform, and no system
#     BLAS for an app to depend on. On x86 int8 still goes through intgemm.
COMMON=(
  -G Ninja
  -DCMAKE_BUILD_TYPE=Release
  -DGIT_SUBMODULE=OFF
  -DCOMPILE_CPU=ON -DCOMPILE_CUDA=OFF
  -DUSE_FBGEMM=OFF -DUSE_MKL=OFF -DUSE_NCCL=OFF -DUSE_DOXYGEN=OFF
  -DUSE_RUY=ON -DUSE_RUY_SGEMM=ON -DUSE_APPLE_ACCELERATE=OFF
)

case "$PLATFORM" in
  linux)
    # A portable x86-64 baseline, never `native` (the default), which
    # bakes in the build machine's CPU. intgemm picks AVX2/AVX512 kernels
    # at runtime regardless. PCRE2 comes from the system (libpcre2-dev).
    PLATFORM_FLAGS=(-DBUILD_ARCH=x86-64-v2)
    ;;
  android)
    NDK="${ANDROID_NDK:-${ANDROID_NDK_LATEST_HOME:?set ANDROID_NDK}}"
    # API 28: Marian's bundled pathie-cpp calls iconv/glob (bionic API 28)
    # and nl_langinfo (API 26).
    PLATFORM_FLAGS=(
      -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake"
      -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28
      -DANDROID_STL=c++_static
      -DBUILD_ARCH=armv8-a
      -DSSPLIT_USE_INTERNAL_PCRE2=ON
    )
    ;;
  macos)
    PLATFORM_FLAGS=(
      -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=12.0
      -DBUILD_ARCH=armv8-a
      -DSSPLIT_USE_INTERNAL_PCRE2=ON
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
      -DSSPLIT_USE_INTERNAL_PCRE2=ON
    )
    ;;
  *) echo "unknown platform: $PLATFORM" >&2; exit 2 ;;
esac

configure() {
  cmake -S "$SRC" -B "$BUILD" "${COMMON[@]}" "${PLATFORM_FLAGS[@]}" "$@"
}

# Pass 1, then hand Marian's own compile definitions to the whole build.
# Marian adds them with add_compile_definitions() inside marian-fork/ only,
# so Bergamot's sources would include Marian's headers with different
# definitions — on ARM that switches off its NEON/SSE-emulation code
# (undeclared sin_ps, _mm_*, FE_TONEAREST). Read from the generated
# build.ninja so each platform gets exactly what Marian chose for it.
configure
DEFS=$(awk '
  /^build marian-fork\/src\/CMakeFiles\/marian.dir\/common\/utils.cpp.o:/ { f = 1 }
  f && /^  DEFINES = / { sub(/^  DEFINES = /, ""); print; exit }
' "$BUILD/build.ninja")
if [[ -z "$DEFS" ]]; then
  echo "could not read Marian's DEFINES from build.ninja" >&2; exit 1
fi
echo "Marian definitions shared with the whole build: $DEFS"
configure -DCMAKE_CXX_FLAGS="$DEFS"

cmake --build "$BUILD" --target translator-cli -- -j "$JOBS"

mkdir -p "$OUT/bin" "$OUT/lib"
cp "$BUILD/src/app/translator-cli" "$OUT/bin/"
# Every static library translator-cli links, for an app to link the same.
find "$BUILD" -name '*.a' -not -path '*/CMakeFiles/*' -exec cp {} "$OUT/lib/" \;
ls -l "$OUT/bin" "$OUT/lib"
