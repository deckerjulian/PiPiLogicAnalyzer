/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_PINS__
#define __OPENSCILAB_PINS__

//Pin table of the board (command 11): every GPIO with what it can do, the capture channel it is
//wired to and why it is reserved. Built from board_settings.h (PIN_MAP, trigger pins, LED, WiFi).
//
//Plain C: compiled on the host by tests/ for every board (with a stub of pico/stdlib.h).

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

//GPIOs of the RP2040 and the RP2350A
#define PIN_GPIO_COUNT 30
//First GPIO with an ADC input (ADC n is GPIO 26 + n)
#define PIN_ADC_BASE 26
#define PIN_ADC_COUNT 4
//Logic level of every pin, mV
#define PIN_LOGIC_MV 3300

//Pin capabilities (bits)
#define PIN_CAN_DIN 0x01
#define PIN_CAN_DOUT 0x02
#define PIN_CAN_PULLUP 0x04
#define PIN_CAN_PULLDOWN 0x08
#define PIN_CAN_PWM 0x10
#define PIN_CAN_ADC 0x20
#define PIN_CAN_CLOCK 0x40

#define PIN_NO_CHANNEL 0xFF

typedef struct _PIN_INFO
{
    uint8_t caps;           //PIN_CAN_*
    uint8_t channel;        //Capture channel, PIN_NO_CHANNEL if none
    const char* reserved;   //Why the pin is reserved, NULL if it is free

} PIN_INFO;

/// @brief Information about a GPIO, NULL if the board has no such GPIO
const PIN_INFO* pins_info(uint8_t gpio);

/// @brief Number of pins in the table (lines after PINS:)
uint8_t pins_count(void);

/// @brief Formats the line "PIN:..." of the index-th pin (without line end)
/// @return Characters written (as snprintf), negative if there is no such pin
int pins_format(uint8_t index, char* buffer, size_t size);

/// @brief GPIOs that are reserved (bit n: GPIO n)
uint32_t pins_reserved_mask(void);

/// @brief GPIOs the board has (bit n: GPIO n)
uint32_t pins_valid_mask(void);

/// @brief ADC inputs usable as analog channels (bit n: ADC n on GPIO 26 + n, not reserved)
uint8_t pins_adc_mask(void);

/// @brief Number of usable ADC inputs (ANALOG=<n>)
uint8_t pins_adc_count(void);

#endif
