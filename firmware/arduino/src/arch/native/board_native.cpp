// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The fake board and hal of the native tests (no Arduino framework).

#if !defined(ARDUINO)

#include "fake_hal.h"

#include <string.h>

#include "../../board.h"
#include "../../hal.h"
#include "../../protocol.h"

namespace board {

#define IO (PC_DIN | PC_DOUT | PC_PULLUP)
const PinDef PIN_TABLE[] = {
    {"D0", 0, PC_DIN, NO_CHANNEL, RES_SERIAL_RX},
    {"D1", 1, PC_DIN, NO_CHANNEL, RES_SERIAL_TX},
    {"D2", 2, IO | PC_CLOCK, NO_CHANNEL, RES_FREE},
    {"D3", 3, IO | PC_PWM | PC_CLOCK, NO_CHANNEL, RES_FREE},
    {"D4", 4, IO | PC_PULLDOWN | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D5", 5, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D6", 6, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D7", 7, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D8", 8, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D9", 9, IO | PC_PWM | PC_SQUARE | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D10", 10, IO | PC_PWM | PC_SQUARE | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D11", 11, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D12", 12, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D13", 13, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"A0", 14, IO | PC_ADC | PC_DAC, 0, RES_FREE},
    {"A1", 15, IO | PC_ADC, 1, RES_FREE},
    {"A2", 16, IO | PC_ADC, 2, RES_FREE},
    {"A3", 17, IO | PC_ADC, 3, RES_FREE},
    {"A4", 18, IO | PC_ADC, 4, RES_FREE},
    {"A5", 19, IO | PC_ADC, 5, RES_FREE},
};
#undef IO
const uint8_t PIN_COUNT = sizeof(PIN_TABLE) / sizeof(PIN_TABLE[0]);

const Info INFO = {
    16000000,  // clock_hz
    115200,    // serial_baud
    10,        // adc_bits
    12,        // dac_bits
    5000,      // vref_mv
    5000,      // logic_mv
    50000,     // stream_max_rate
    1000000,   // buffer_max_rate
    1000000,   // monitor_max_mhz
    100000,    // generator_max_rate
    100000,    // arb_max_rate
    11520,     // stream_bytes
    true,      // gpio_locked_in_buffer
};

const char *name() { return "native"; }

}  // namespace board

namespace hal {
namespace fake {

uint32_t input_levels;
uint32_t (*input_source)();
uint32_t output_mask;
uint32_t output_levels;
uint32_t cycle_counter;
uint32_t cycle_step;
uint16_t dac_value;
uint32_t now_us;
std::vector<uint8_t> line;
std::vector<uint32_t> writes;
uint16_t adc_values[16];
uint32_t pullups;
uint32_t pulldowns;
uint32_t pwm_pins;
float pwm_frequency[32];
uint16_t pwm_duty[32];
uint32_t dac_pins;
int square_pin;
float square_frequency;
bool clock_busy;
uint8_t edge_pin;
bool edge_falling;
void (*edge_handler)();
void (*timer_handler[2])();
float timer_rates[2];

void record_write(uint32_t mask, uint32_t levels) {
    (void)mask;
    (void)levels;
    writes.push_back(output_levels);
}

void reset() {
    input_levels = 0;
    input_source = nullptr;
    output_mask = output_levels = 0;
    cycle_counter = 0;
    cycle_step = 1;
    dac_value = 0;
    now_us = 1000;
    line.clear();
    writes.clear();
    memset(adc_values, 0, sizeof(adc_values));
    pullups = pulldowns = pwm_pins = dac_pins = 0;
    memset(pwm_frequency, 0, sizeof(pwm_frequency));
    memset(pwm_duty, 0, sizeof(pwm_duty));
    square_pin = -1;
    square_frequency = 0;
    clock_busy = false;
    edge_pin = 0xFF;
    edge_falling = false;
    edge_handler = nullptr;
    timer_handler[0] = timer_handler[1] = nullptr;
    timer_rates[0] = timer_rates[1] = 0;
}

void tick(uint8_t slot, uint32_t n) {
    for (uint32_t i = 0; i < n && timer_handler[slot]; i++) {
        now_us += (uint32_t)(1000000.0f / timer_rates[slot]);
        timer_handler[slot]();
    }
}

void edge() {
    if (edge_handler) edge_handler();
}

}  // namespace fake

void init() {}
uint32_t micros() { return fake::now_us; }
uint32_t millis() { return fake::now_us / 1000; }
void delay_us(uint32_t us) { fake::now_us += us; }

void serial_begin(uint32_t) {}
int serial_read() { return -1; }
void serial_write(const uint8_t *data, size_t n) { fake::line.insert(fake::line.end(), data, data + n); }

static uint32_t bit(uint8_t pin) { return (uint32_t)1 << pin; }

void pin_input(uint8_t pin, uint8_t pull) {
    fake::output_mask &= ~bit(pin);
    fake::pullups &= ~bit(pin);
    fake::pulldowns &= ~bit(pin);
    if (pull == PULL_UP) fake::pullups |= bit(pin);
    if (pull == PULL_DOWN) fake::pulldowns |= bit(pin);
}

void pin_output(uint8_t pin, bool level) {
    fake::output_mask |= bit(pin);
    write_levels(bit(pin), level ? bit(pin) : 0);
}

bool pin_read(uint8_t pin) {
    // a released line with a pull-up reads high (I2C without a device)
    if (!(fake::output_mask & bit(pin)) && (fake::pullups & bit(pin))) return true;
    return (read_levels() & bit(pin)) != 0;
}

uint8_t pwm_start(uint8_t pin, float frequency, uint16_t duty, float *actual) {
    fake::output_mask |= bit(pin);
    fake::pwm_pins |= bit(pin);
    fake::pwm_frequency[pin] = frequency;
    fake::pwm_duty[pin] = duty;
    *actual = frequency;
    return 0;
}

void pwm_stop(uint8_t pin) { fake::pwm_pins &= ~bit(pin); }

uint8_t dac_write(uint8_t pin, uint16_t raw) {
    fake::dac_pins |= bit(pin);
    fake::dac_value = raw;
    return 0;
}

void dac_stop(uint8_t pin) { fake::dac_pins &= ~bit(pin); }

uint16_t adc_read(uint8_t channel) { return fake::adc_values[channel]; }

float timer_rate(float rate) { return rate; }

uint8_t timer_start(uint8_t slot, float rate, void (*tick)(), float *actual) {
    if (fake::timer_handler[slot]) return proto::ERR_BUSY;
    fake::timer_handler[slot] = tick;
    fake::timer_rates[slot] = rate;
    *actual = rate;
    return 0;
}

void timer_stop(uint8_t slot) { fake::timer_handler[slot] = nullptr; }

uint8_t edge_attach(uint8_t pin, bool falling, void (*edge)()) {
    fake::edge_pin = pin;
    fake::edge_falling = falling;
    fake::edge_handler = edge;
    return 0;
}

void edge_detach() { fake::edge_handler = nullptr; }

uint8_t square_start(uint8_t pin, float frequency, float *actual) {
    fake::square_pin = pin;
    fake::square_frequency = frequency;
    *actual = frequency;
    return 0;
}

void square_stop() { fake::square_pin = -1; }

uint32_t clock_ticks(float) { return 16000000; }

uint8_t clock_begin(float, uint32_t *ticks_per_second) {
    if (fake::clock_busy) return proto::ERR_BUSY;
    fake::clock_busy = true;
    *ticks_per_second = 16000000;
    return 0;
}

void clock_end() { fake::clock_busy = false; }
void quiet_begin() {}
void quiet_end(uint32_t elapsed_us) { fake::now_us += elapsed_us; }
uint32_t irq_save() { return 0; }
void irq_restore(uint32_t) {}

}  // namespace hal

#endif
