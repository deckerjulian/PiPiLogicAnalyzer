# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Host tests of the hardware-free parts of the Pico firmware: frame parser, pattern data (run
# lengths), dividers and the pin table of every board. Compiled with the host compiler:
#
#   cmake -S firmware/pico/tests -B build/pico-tests && cmake --build build/pico-tests
#   ctest --test-dir build/pico-tests --output-on-failure

cmake_minimum_required(VERSION 3.13)
project(openscilab_pico_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(FIRMWARE ${CMAKE_CURRENT_LIST_DIR}/..)

if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Wall -Wextra -Werror)
endif()

enable_testing()

find_library(MATH_LIBRARY m)

add_executable(test_protocol test_protocol.c ${FIRMWARE}/proto.c ${FIRMWARE}/pattern_data.c ${FIRMWARE}/timing.c)
target_include_directories(test_protocol PRIVATE ${FIRMWARE})
if(MATH_LIBRARY)
    target_link_libraries(test_protocol ${MATH_LIBRARY})
endif()
add_test(NAME protocol COMMAND test_protocol)

# The pin table, once per board (board_settings.h selects the board by its BUILD_ define)
set(BOARDS PICO PICO_2 PICO_W PICO_W_WIFI PICO_2_W PICO_2_W_WIFI ZERO INTERCEPTOR)
foreach(board ${BOARDS})
    add_executable(test_pins_${board} test_pins.c ${FIRMWARE}/pins.c)
    target_include_directories(test_pins_${board} PRIVATE ${CMAKE_CURRENT_LIST_DIR}/stub ${FIRMWARE})
    target_compile_definitions(test_pins_${board} PRIVATE BUILD_${board} TEST_BOARD="${board}")
    add_test(NAME pins_${board} COMMAND test_pins_${board})
endforeach()
