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
# Executables (translator-cli, CMake's own checks) link without signing.
set(CMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED NO)
