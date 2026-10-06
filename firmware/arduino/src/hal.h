// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The hardware layer: one implementation per architecture (src/arch/avr, renesas, esp32) and a
// fake one for the native tests (src/arch/native). Pins are indexes into the pin table.
// Functions that return uint8_t return 0 or an error code of the protocol (proto::ERR_*).

#ifndef HAL_H
#define HAL_H

#include "platform.h"

namespace hal {

void init();
uint32_t micros();
uint32_t millis();
void delay_us(uint32_t us);

// the line to the host
void serial_begin(uint32_t baud);
int serial_read();  // -1: no byte
void serial_write(const uint8_t *data, size_t n);

// pins
const uint8_t PULL_NONE = 0;
const uint8_t PULL_UP = 1;
const uint8_t PULL_DOWN = 2;
void pin_input(uint8_t pin, uint8_t pull);
void pin_output(uint8_t pin, bool level);
bool pin_read(uint8_t pin);
// fast, from interrupts as well (src/arch/<arch>/fast.h):
//   uint32_t read_levels();                          bit n: the level of pin n
//   void write_levels(uint32_t mask, uint32_t levels);  pins that are outputs already

// PWM, DAC, ADC
uint8_t pwm_start(uint8_t pin, float frequency, uint16_t duty, float *actual);
void pwm_stop(uint8_t pin);
uint8_t dac_write(uint8_t pin, uint16_t raw);
void dac_stop(uint8_t pin);
uint16_t adc_read(uint8_t channel);  // blocking
// fast (fast.h): void dac_write_fast(uint16_t raw);  the first DAC, from interrupts

// periodic interrupts: the sampling of stream captures and the outputs (pattern, DAC). Boards
// with one timer for both answer ERR_BUSY to the second.
const uint8_t TIMER_SAMPLE = 0;
const uint8_t TIMER_OUTPUT = 1;
float timer_rate(float rate);  // the rate timer_start would reach
uint8_t timer_start(uint8_t slot, float rate, void (*tick)(), float *actual);
void timer_stop(uint8_t slot);

// an interrupt on the edges of a clock pin (state captures)
uint8_t edge_attach(uint8_t pin, bool falling, void (*edge)());
void edge_detach();

// the square wave output
uint8_t square_start(uint8_t pin, float frequency, float *actual);
void square_stop();

// The counted clock of buffer captures and bit-banged transmissions: cycles() (fast.h) counts
// ticks_per_second; differences of cycle_t are valid up to half its range.
uint32_t clock_ticks(float slowest_rate);  // the ticks per second clock_begin would choose
uint8_t clock_begin(float slowest_rate, uint32_t *ticks_per_second);
void clock_end();
// Fewer interrupts for a counted loop (AVR: the millis timer stops; quiet_end adds the time)
void quiet_begin();
void quiet_end(uint32_t elapsed_us);

uint32_t irq_save();
void irq_restore(uint32_t state);

}  // namespace hal

#if defined(__AVR__)
#include "arch/avr/fast.h"
#elif defined(ARDUINO_ARCH_RENESAS)
#include "arch/renesas/fast.h"
#elif defined(ARDUINO_ARCH_ESP32)
#include "arch/esp32/fast.h"
#else
#include "arch/native/fast.h"
#endif

#endif
