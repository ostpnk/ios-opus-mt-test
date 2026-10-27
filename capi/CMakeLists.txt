# Builds libbergamot (the C interface in bergamot_c.h) on top of Mozilla's
# engine, included as a subdirectory so none of its files need changing.
#
#   cmake -S capi -B build -DENGINE_DIR=engine/inference [platform flags]
#
# Targets: bergamot (shared library), test_capi (C test program), plus the
# engine's own translator-cli.
cmake_minimum_required(VERSION 3.16)
project(bergamot_capi C CXX)

set(ENGINE_DIR "" CACHE PATH "mozilla/translations' inference/ directory")
if(NOT EXISTS "${ENGINE_DIR}/CMakeLists.txt")
  message(FATAL_ERROR "set ENGINE_DIR to the engine's inference/ directory")
endif()

add_subdirectory("${ENGINE_DIR}" engine)

# The engine commit, for bt_version().
execute_process(
  COMMAND git -C "${ENGINE_DIR}" rev-parse --short HEAD
  OUTPUT_VARIABLE BT_ENGINE_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE
  ERROR_QUIET)
if(NOT BT_ENGINE_COMMIT)
  set(BT_ENGINE_COMMIT unknown)
endif()

add_library(bergamot SHARED bergamot_c.cpp)
target_link_libraries(bergamot PRIVATE bergamot-translator-source)
target_compile_definitions(bergamot PRIVATE
  BERGAMOT_C_BUILD BT_ENGINE_VERSION="${BT_ENGINE_COMMIT}")
set_target_properties(bergamot PROPERTIES
  CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON
  # Export only the bt_* functions (BT_API), not the engine's thousands of
  # C++ symbols.
  CXX_VISIBILITY_PRESET hidden C_VISIBILITY_PRESET hidden
  VISIBILITY_INLINES_HIDDEN ON
  PUBLIC_HEADER bergamot_c.h)
# Export the bt_* functions and nothing else: the engine's static libraries
# weren't compiled with hidden visibility, so some of their C++ template
# instances would otherwise be exported too.
if(APPLE)
  # Loaded by path from wherever the app puts it.
  set_target_properties(bergamot PROPERTIES INSTALL_NAME_DIR "@rpath")
  target_link_options(bergamot PRIVATE
    "-Wl,-exported_symbols_list,${CMAKE_CURRENT_SOURCE_DIR}/exports.txt")
elseif(UNIX)
  target_link_options(bergamot PRIVATE
    "-Wl,--version-script=${CMAKE_CURRENT_SOURCE_DIR}/exports.map"
    "-Wl,--exclude-libs,ALL")
endif()

add_executable(test_capi test_capi.c)
target_link_libraries(test_capi PRIVATE bergamot)
# Finds libbergamot next to it (build dir) or in ../lib (build.sh's out/).
if(APPLE)
  set_target_properties(test_capi PROPERTIES
    BUILD_RPATH "@executable_path;@executable_path/../lib")
else()
  set_target_properties(test_capi PROPERTIES
    BUILD_RPATH "\$ORIGIN;\$ORIGIN/../lib" SKIP_BUILD_RPATH OFF)
endif()
