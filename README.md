# translate-engine

Build scripts for running offline machine translation (Mozilla's Firefox
Translations models and OPUS-MT models) on phones and desktops. A spike for
`core_translate` in flutter-core-kit: it builds Mozilla's translation engine
([Bergamot](https://github.com/mozilla/translations/tree/main/inference),
on its Marian fork) for each platform and checks that it translates.

Nothing from the engine is vendored here: `build/fetch-engine.sh` checks
out `mozilla/translations` at a pinned commit (plus the PCRE2 source,
checksum-verified) and applies `patches/`.

## libbergamot

`capi/` is a plain C interface to the engine (`bergamot_c.h`), built as a
shared library, `libbergamot`, for callers that can't use C++ — `dart:ffi`
in particular:

- `bt_service_create` / `bt_model_load` / `bt_translate` / `*_free`, on
  opaque handles. `bt_translate` takes a batch of texts, optionally a pivot
  model (it→en then en→de, for it→de) and `BT_FLAG_HTML`.
- Relative paths in a model's config resolve against the directory passed
  to `bt_model_load`, so a model directory can move without rewriting it.
- **Errors never take the process down.** Every call returns a
  `bt_status` and a message. The engine's fatal errors (Marian's `ABORT`,
  normally `std::abort()`) are switched to exceptions and caught at the
  boundary: a missing or corrupt model file is `BT_ERROR_MODEL_LOAD`.
- Blocking, on the calling thread; one thread at a time per service and per
  model.
- Exports the `bt_*` functions only (`exports.map` / `exports.txt`).

`capi/test_capi.c`, in C, tests it: error paths, a batch, HTML, pivoting.

## Platforms

`build/build.sh <platform>` produces `out/<platform>/` with
`lib/libbergamot.{so,dylib}`, `include/bergamot_c.h` and two test programs,
`bin/test_capi` and `bin/translator-cli` (the engine's own CLI).
`build/smoke-test.sh out/<platform>` runs both against Mozilla's it↔en
models.

| Platform | Script | Tested by CI |
|---|---|---|
| macOS (arm64) | `build/build.sh macos` | yes, native |
| iOS device (arm64) | `build/build.sh ios` | build only |
| iOS simulator (arm64) | `build/build.sh ios-sim` | in a simulator (best effort) |
| Linux (x86-64) | `build/build.sh linux` | yes, native |
| Android (arm64-v8a, API 24+) | `build/build.sh android` | build only (tested on a phone over adb) |

CI (`.github/workflows/build.yml`) runs on every push to `main` and on manual dispatch.

## What the build changes, and why

- **Position-independent everywhere, PCRE2 built from source everywhere**:
  the engine's static libraries go into a shared library, and a system
  `libpcre2-8.a` (Linux) isn't PIC.
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
- `patches/marian-pathie-android-api24.patch`: Android API 24 (Flutter's
  minimum) instead of 28. Marian's bundled pathie-cpp converted filenames
  with `nl_langinfo`/`iconv` (bionic API 26/28) and offered `glob` (API 28).
  Android filenames are always UTF-8, so it now treats Android like Apple
  (no conversion), leaves `iconv` out below API 28, and `glob` — which
  Marian never calls — fails with `ENOSYS` there.
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
- `patches/marian-zlib-TARGET_OS_MAC.patch`: Marian bundles zlib 1.2.11,
  whose `zutil.h` takes `TARGET_OS_MAC` to mean classic Mac OS and
  `#define`s `fdopen` to `NULL` — breaking the macOS/iOS SDK's own
  `fdopen` declaration. Checks `MACOS` only, as zlib 1.2.12 does.
- `patches/ssplit-pcre2-nojit-ios.patch`: the sentence splitter builds
  PCRE2 with its JIT compiler, which iOS forbids (an app may not generate
  executable code) and which doesn't even link there (`___clear_cache`).
  Off on Apple's mobile platforms, as it already was for WebAssembly;
  ssplit checks `PCRE2_CONFIG_JIT` at runtime and runs the regexes
  interpreted.
- `CMAKE_POLICY_VERSION_MINIMUM=3.5` (set by `build.sh`): CMake 4 no longer
  configures cpuinfo's bundled clog, which declares an older minimum.

## Licences

The scripts here are MIT. The engine they build is MPL-2.0
(mozilla/translations) with an MIT core (Marian); Mozilla's models are
MPL-2.0 and OPUS-MT's CC BY 4.0.
