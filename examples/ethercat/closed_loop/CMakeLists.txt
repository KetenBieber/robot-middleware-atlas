cmake_minimum_required(VERSION 3.16)
project(atlas_ethercat_closed_loop LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

find_path(ECRT_INCLUDE_DIR
    NAMES ecrt.h
    HINTS
        ${ETHERCAT_ROOT}
        ENV ETHERCAT_ROOT
    PATH_SUFFIXES include)

find_library(ETHERCAT_LIBRARY
    NAMES ethercat
    HINTS
        ${ETHERCAT_ROOT}
        ENV ETHERCAT_ROOT
    PATH_SUFFIXES lib lib64)

if(NOT ECRT_INCLUDE_DIR)
    message(FATAL_ERROR
        "ecrt.h was not found. Set ETHERCAT_ROOT or -DECRT_INCLUDE_DIR=...")
endif()

if(NOT ETHERCAT_LIBRARY)
    message(FATAL_ERROR
        "libethercat was not found. Set ETHERCAT_ROOT or -DETHERCAT_LIBRARY=...")
endif()

add_library(atlas_ethercat_api INTERFACE)
target_include_directories(atlas_ethercat_api INTERFACE
    ${ECRT_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(atlas_ethercat_api INTERFACE
    ${ETHERCAT_LIBRARY})

add_executable(atlas_ethercat_controller src/controller.c)
target_link_libraries(atlas_ethercat_controller PRIVATE atlas_ethercat_api m)

add_executable(atlas_ethercat_plant src/plant_sim.c)
target_link_libraries(atlas_ethercat_plant PRIVATE atlas_ethercat_api m)

target_compile_options(atlas_ethercat_controller PRIVATE -Wall -Wextra -Wpedantic)
target_compile_options(atlas_ethercat_plant PRIVATE -Wall -Wextra -Wpedantic)
