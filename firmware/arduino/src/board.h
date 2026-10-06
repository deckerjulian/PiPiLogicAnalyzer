// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The board: its pin table and its limits. Every board (src/arch/<arch>/board_<arch>.cpp, the
// fake board of the native tests in src/arch/native/) defines PIN_TABLE, PIN_COUNT and INFO.
//
// Pins on the wire are indexes into the pin table; bit n of a level mask is pin n.

#ifndef BOARD_H
#define BOARD_H

#include "platform.h"

#define FIRMWARE_VERSION "0.1.0b1"

namespace board {

// pin capabilities (docs/protocols.md, "Pins")
const uint16_t PC_DIN = 1 << 0;
const uint16_t PC_DOUT = 1 << 1;
const uint16_t PC_PULLUP = 1 << 2;
const uint16_t PC_PULLDOWN = 1 << 3;
const uint16_t PC_PWM = 1 << 4;
const uint16_t PC_ADC = 1 << 5;
const uint16_t PC_DAC = 1 << 6;
const uint16_t PC_CLOCK = 1 << 7;
const uint16_t PC_CLOCK_SW = 1 << 8;
const uint16_t PC_SQUARE = 1 << 9;  // the square wave output can use the pin (not reported as such)

// why a pin is reserved
const uint8_t RES_FREE = 0;
const uint8_t RES_SERIAL_RX = 1;
const uint8_t RES_SERIAL_TX = 2;
const uint8_t RES_USB_DM = 3;
const uint8_t RES_USB_DP = 4;

const uint8_t NO_CHANNEL = 0xFF;  // no analog channel

struct PinDef {
    char name[8];
    uint8_t gpio;      // the Arduino pin number (digitalWrite, analogRead, ...)
    uint16_t caps;
    uint8_t analog;    // analog channel or NO_CHANNEL
    uint8_t reserved;  // RES_FREE or why the pin is reserved
};

struct Info {
    uint32_t clock_hz;          // CPU clock
    uint32_t serial_baud;       // of the USB serial line
    uint8_t adc_bits;
    uint8_t dac_bits;           // 0: no DAC
    uint16_t vref_mv;           // ADC reference
    uint16_t logic_mv;          // logic level of the pins
    uint32_t stream_max_rate;   // stream captures, samples per second (until measured: step 2a)
    uint32_t buffer_max_rate;   // buffer captures (counted loop)
    uint32_t monitor_max_mhz;   // monitor reports, mHz
    uint32_t generator_max_rate;
    uint32_t arb_max_rate;      // DAC points per second (0: no AFG)
    uint32_t stream_bytes;      // bytes per second the line carries (CAPS STREAM=)
    bool gpio_locked_in_buffer; // a buffer capture blocks the GPIO commands (AVR)
};

extern const PinDef PIN_TABLE[] FW_FLASH;
extern const uint8_t PIN_COUNT;
extern const Info INFO;
// "uno", "nano", ... (a flash string)
const char *name();
// "USB serial (RX)", ... (a flash string)
const char *reserved_text(uint8_t why);

static inline PinDef pin(uint8_t index) {
    PinDef def;
    fw_flash_copy(&def, &PIN_TABLE[index], sizeof(def));
    return def;
}

// analog channels (the highest channel + 1)
uint8_t analog_count();
// the pin of an analog channel, or NO_CHANNEL
uint8_t analog_pin(uint8_t channel);
// the first pin with the DAC capability, or NO_CHANNEL
uint8_t dac_pin();
// pins with all of caps (a mask of indexes)
uint32_t pins_with(uint16_t caps);
// pins that are reserved
uint32_t reserved_pins();

}  // namespace board

#endif
