# ios-opus-mt-test

Build scripts for running offline machine translation (Mozilla's Firefox
Translations models and OPUS-MT models) on phones and desktops. A spike for
`core_translate` in flutter-core-kit: it builds Mozilla's translation engine
([Bergamot](https://github.com/mozilla/translations/tree/main/inference),
on its Marian fork) for each platform and checks that it translates.

Nothing from the engine is vendored here: `build/fetch-engine.sh` checks
out `mozilla/translations` at a pinned commit and applies `patches/`.

## Platforms

| Platform | Script | Tested by CI |
|---|---|---|
| macOS (arm64) | `build/build.sh macos` | yes, native |
| iOS device (arm64) | `build/build.sh ios` | build only |
| iOS simulator (arm64) | `build/build.sh ios-sim` | in a simulator (best effort) |
| Linux (x86-64) | `build/build.sh linux` | yes, native |
| Android (arm64-v8a, API 28+) | `build/build.sh android` | build only (tested on a phone over adb) |

CI (`.github/workflows/build.yml`) runs on every push to `main` and on manual dispatch.

## What the build changes, and why

- **One float-GEMM path everywhere** (`USE_RUY_SGEMM`): Google's ruy, not
  OpenBLAS/MKL/Accelerate, so no system BLAS is needed. On x86 int8 still
  uses intgemm.
- **`BUILD_ARCH` is never `native`**: that bakes in the build machine's CPU
  (and on a cross build, passes the host's x86 flags to the ARM compiler).
- **Marian's compile definitions are shared with Bergamot's sources.**
  Marian sets them only inside its own directory, so on ARM Bergamot's
  sources would include Marian's headers with the NEON/SSE-emulation code
  switched off. `build.sh` reads them from the first configure and passes
  them to the whole build.
- **Android needs API 28**: Marian's bundled pathie-cpp uses `iconv`/`glob`
  (bionic API 28) and `nl_langinfo` (API 26).
- `patches/sentencepiece-clang20-kAnyType.patch`: clang 20 (NDK r29) rejects
  a `constexpr` enum constant outside the enum's range in SentencePiece's
  trainer (never used for translation, but linked).
- `patches/marian-TargetArch-arm64.patch`: Marian's architecture detection
  predates Apple Silicon and rejects `CMAKE_OSX_ARCHITECTURES=arm64`
  ("Invalid OS X arch name"); it now reports it as `armv8`, like aarch64.
- `patches/marian-ExceptionWithCallStack-vla.patch`: two buffers in
  Marian's backtrace helper were sized by non-`const` variables, i.e.
  variable-length arrays, which Apple clang reports
  (`-Wvla-cxx-extension`) and Marian's `-Werror` turns into errors. The
  sizes are now `const`.
- `CMAKE_POLICY_VERSION_MINIMUM=3.5` (set by `build.sh`): CMake 4 no longer
  configures cpuinfo's bundled clog, which declares an older minimum.

## Licences

The scripts here are MIT. The engine they build is MPL-2.0
(mozilla/translations) with an MIT core (Marian); Mozilla's models are
MPL-2.0 and OPUS-MT's CC BY 4.0.
