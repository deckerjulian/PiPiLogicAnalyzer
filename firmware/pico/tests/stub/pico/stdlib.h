/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

// Host stand-in of the Pico SDK header that board_settings.h includes: the host tests compile
// the hardware-free parts of the firmware (pins.c) without the SDK.

#ifndef OPENSCILAB_TEST_STUB_PICO_STDLIB_H
#define OPENSCILAB_TEST_STUB_PICO_STDLIB_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#endif
