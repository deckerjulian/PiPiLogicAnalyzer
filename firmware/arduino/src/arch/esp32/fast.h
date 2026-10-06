// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Fast pin access on the ESP32 and ESP32-S3: the GPIO input registers (GPIO 0-31, 32-48) are
// spread to the pin bits by a table made at start-up; outputs by the set/clear registers. The
// counted clock is the CPU cycle counter.

#ifndef ARCH_ESP32_FAST_H
#define ARCH_ESP32_FAST_H

#include <Arduino.h>
#include <hal/cpu_hal.h>
#include <soc/gpio_reg.h>
#include <stdint.h>
#if CONFIG_IDF_TARGET_ESP32
#include <hal/dac_ll.h>
#endif

namespace hal {

typedef uint32_t cycle_t;

namespace esp {
extern uint8_t pin_gpio[32];
extern uint8_t pin_count;
}  // namespace esp

static inline cycle_t IRAM_ATTR cycles() { return cpu_hal_get_cycle_count(); }

static inline uint32_t IRAM_ATTR read_levels() {
    uint32_t low = REG_READ(GPIO_IN_REG), high = REG_READ(GPIO_IN1_REG);
    uint32_t levels = 0;
    for (uint8_t i = 0; i < esp::pin_count; i++) {
        uint8_t g = esp::pin_gpio[i];
        uint32_t word = g < 32 ? low : high;
        if (word & ((uint32_t)1 << (g & 31))) levels |= (uint32_t)1 << i;
    }
    return levels;
}

static inline void IRAM_ATTR write_levels(uint32_t mask, uint32_t levels) {
    uint32_t set_low = 0, clear_low = 0, set_high = 0, clear_high = 0;
    for (uint8_t i = 0; i < esp::pin_count && mask; i++) {
        uint32_t bit = (uint32_t)1 << i;
        if (!(mask & bit)) continue;
        mask &= ~bit;
        uint8_t g = esp::pin_gpio[i];
        uint32_t gbit = (uint32_t)1 << (g & 31);
        if (g < 32) {
            if (levels & bit) set_low |= gbit;
            else clear_low |= gbit;
        } else {
            if (levels & bit) set_high |= gbit;
            else clear_high |= gbit;
        }
    }
    if (set_low) REG_WRITE(GPIO_OUT_W1TS_REG, set_low);
    if (clear_low) REG_WRITE(GPIO_OUT_W1TC_REG, clear_low);
    if (set_high) REG_WRITE(GPIO_OUT1_W1TS_REG, set_high);
    if (clear_high) REG_WRITE(GPIO_OUT1_W1TC_REG, clear_high);
}

static inline void IRAM_ATTR dac_write_fast(uint16_t raw) {
#if CONFIG_IDF_TARGET_ESP32
    dac_ll_update_output_value(DAC_CHANNEL_1, (uint8_t)raw);  // GPIO25
#else
    (void)raw;
#endif
}

}  // namespace hal

#endif
