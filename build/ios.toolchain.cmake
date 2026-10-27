# iOS toolchain for build/build.sh. The SDK comes from the environment
# (IOS_SDK = iphoneos | iphonesimulator) so that ExternalProjects (PCRE2),
# which receive only CMAKE_TOOLCHAIN_FILE, build for the same target.
set(CMAKE_SYSTEM_NAME iOS)
set(CMAKE_OSX_ARCHITECTURES arm64)
set(CMAKE_OSX_DEPLOYMENT_TARGET 13.0)
if(NOT DEFINED ENV{IOS_SDK})
  message(FATAL_ERROR "set IOS_SDK to iphoneos or iphonesimulator")
endif()
set(CMAKE_OSX_SYSROOT $ENV{IOS_SDK})
set(CMAKE_SYSTEM_PROCESSOR arm64)
# ruy's bundled cpuinfo predates CMake's own iOS support and recognises iOS
# only through the ios-cmake toolchain's variables: without IOS it leaves out
# src/arm/mach/init.c, and linking fails on cpuinfo_arm_mach_init.
set(IOS TRUE)
set(IOS_ARCH arm64)
# Executables (translator-cli, CMake's own checks) link without signing.
set(CMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED NO)
# Plain executables, not .app bundles (CMake's iOS default): SentencePiece's
# install() rules give no BUNDLE DESTINATION, which fails configure, and the
# simulator test runs translator-cli directly.
set(CMAKE_MACOSX_BUNDLE OFF)
