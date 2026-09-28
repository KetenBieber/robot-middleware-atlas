cmake_minimum_required(VERSION 3.16)
project(atlas_ecal_closed_loop LANGUAGES CXX)

find_package(eCAL REQUIRED)

add_library(atlas_wire_codec INTERFACE)
target_include_directories(atlas_wire_codec INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}>)
target_compile_features(atlas_wire_codec INTERFACE cxx_std_17)

add_executable(atlas_wire_codec_test wire_codec_test.cpp)
target_link_libraries(atlas_wire_codec_test PRIVATE atlas_wire_codec)

add_executable(atlas_ecal_source source.cpp)
add_executable(atlas_ecal_relay relay.cpp)
add_executable(atlas_ecal_observer observer.cpp)

foreach(target IN ITEMS atlas_ecal_source atlas_ecal_relay atlas_ecal_observer)
  target_compile_features(${target} PRIVATE cxx_std_17)
  target_link_libraries(${target} PRIVATE eCAL::core atlas_wire_codec)
endforeach()
