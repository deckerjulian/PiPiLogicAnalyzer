// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Arduino Uno R4 Minima (RA4M1, 48 MHz): pin table and hal.
//
// USB is native, so D0/D1 are free. Two FspTimers (allocated once) are the periodic interrupts,
// PwmOut drives PWM and the square wave (any PWM pin), the 12 bit DAC sits on A0, the ADC reads
// 14 bit. Clock edges use attachInterrupt on the pins with an IRQ line.

#if defined(ARDUINO_ARCH_RENESAS)

#include <Arduino.h>
#include <FspTimer.h>
#include <pwm.h>

#include "../../board.h"
#include "../../board_config.h"
#include "../../hal.h"
#include "../../protocol.h"

using namespace board;

namespace board {

#define IO (PC_DIN | PC_DOUT | PC_PULLUP)
#define PW (PC_PWM | PC_SQUARE)

const PinDef PIN_TABLE[] = {
    {"D0", 0, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D1", 1, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D2", 2, IO | PC_CLOCK, NO_CHANNEL, RES_FREE},
    {"D3", 3, IO | PW | PC_CLOCK, NO_CHANNEL, RES_FREE},
    {"D4", 4, IO, NO_CHANNEL, RES_FREE},
    {"D5", 5, IO | PW, NO_CHANNEL, RES_FREE},
    {"D6", 6, IO | PW, NO_CHANNEL, RES_FREE},
    {"D7", 7, IO, NO_CHANNEL, RES_FREE},
    {"D8", 8, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D9", 9, IO | PW, NO_CHANNEL, RES_FREE},
    {"D10", 10, IO | PW, NO_CHANNEL, RES_FREE},
    {"D11", 11, IO | PW, NO_CHANNEL, RES_FREE},
    {"D12", 12, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D13", 13, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"A0", A0, IO | PC_ADC | PC_DAC, 0, RES_FREE},
    {"A1", A1, IO | PC_ADC | PC_CLOCK_SW, 1, RES_FREE},
    {"A2", A2, IO | PC_ADC | PC_CLOCK_SW, 2, RES_FREE},
    {"A3", A3, IO | PC_ADC | PC_CLOCK_SW, 3, RES_FREE},
    {"A4", A4, IO | PC_ADC | PC_CLOCK_SW, 4, RES_FREE},
    {"A5", A5, IO | PC_ADC | PC_CLOCK_SW, 5, RES_FREE},
};

#undef IO
#undef PW

const uint8_t PIN_COUNT = sizeof(PIN_TABLE) / sizeof(PIN_TABLE[0]);

// Rates are estimates until the benchmark (step 2a) measured them.
const Info INFO = {
    48000000,  // clock_hz
    2000000,   // serial_baud (USB: any)
    14,        // adc_bits
    12,        // dac_bits
    5000,      // vref_mv
    5000,      // logic_mv
    100000,    // stream_max_rate
    1000000,   // buffer_max_rate
    2000000,   // monitor_max_mhz (2 kHz)
    200000,    // generator_max_rate
    100000,    // arb_max_rate
    500000,    // stream_bytes
    false,     // gpio_locked_in_buffer
};

const char *name() { return "uno_r4"; }

}  // namespace board

namespace hal {

namespace ra {
R_PORT0_Type *const port[PORTS] = {R_PORT0, R_PORT1, R_PORT2, R_PORT3, R_PORT4};
uint8_t pin_port[32];
uint16_t pin_mask[32];
uint8_t pin_count = 0;
}  // namespace ra

static inline uint8_t gpio(uint8_t pin) { return PIN_TABLE[pin].gpio; }

void init() {
    for (uint8_t i = 0; i < PIN_COUNT; i++) {
        bsp_io_port_pin_t p = g_pin_cfg[gpio(i)].pin;
        uint8_t port = (uint8_t)((uint32_t)p >> 8);
        ra::pin_port[i] = port < ra::PORTS ? port : 0;
        ra::pin_mask[i] = port < ra::PORTS ? (uint16_t)(1u << ((uint32_t)p & 0xFF)) : 0;
        pinMode(gpio(i), INPUT);
    }
    ra::pin_count = PIN_COUNT;
    analogReadResolution(14);
    analogWriteResolution(12);
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CYCCNT = 0;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

uint32_t micros() { return ::micros(); }
uint32_t millis() { return ::millis(); }

void delay_us(uint32_t us) {
    while (us > 10000) {
        delay(10);
        us -= 10000;
    }
    delayMicroseconds(us);
}

void serial_begin(uint32_t baud) { Serial.begin(baud); }
int serial_read() { return Serial.available() > 0 ? Serial.read() : -1; }
void serial_write(const uint8_t *data, size_t n) { Serial.write(data, n); }

void pin_input(uint8_t pin, uint8_t pull) { pinMode(gpio(pin), pull == PULL_UP ? INPUT_PULLUP : INPUT); }

void pin_output(uint8_t pin, bool level) {
    pinMode(gpio(pin), OUTPUT);
    digitalWrite(gpio(pin), level ? HIGH : LOW);
}

bool pin_read(uint8_t pin) { return digitalRead(gpio(pin)) == HIGH; }

// --- PWM and the square wave: one PwmOut per pin
static PwmOut *pwm_out[32];

uint8_t pwm_start(uint8_t pin, float frequency, uint16_t duty, float *actual) {
    if (!pwm_out[pin]) pwm_out[pin] = new PwmOut(gpio(pin));
    PwmOut *out = pwm_out[pin];
    out->end();
    if (!out->begin(frequency, (float)duty * 100.0f / 65535.0f)) return proto::ERR_UNSUPPORTED;
    *actual = frequency;
    return 0;
}

void pwm_stop(uint8_t pin) {
    if (pwm_out[pin]) pwm_out[pin]->end();
    pinMode(gpio(pin), INPUT);
}

static uint8_t square_pin = 0xFF;

uint8_t square_start(uint8_t pin, float frequency, float *actual) {
    uint8_t error = pwm_start(pin, frequency, 32768, actual);
    if (!error) square_pin = pin;
    return error;
}

void square_stop() {
    if (square_pin != 0xFF) pwm_stop(square_pin);
    square_pin = 0xFF;
}

// --- DAC (A0) and ADC
uint8_t dac_write(uint8_t pin, uint16_t raw) {
    analogWrite(gpio(pin), raw);
    return 0;
}

void dac_stop(uint8_t pin) {
    analogWrite(gpio(pin), 0);
    pinMode(gpio(pin), INPUT);
}

uint16_t adc_read(uint8_t channel) {
    uint8_t pin = board::analog_pin(channel);
    return pin == NO_CHANNEL ? 0 : (uint16_t)analogRead(gpio(pin));
}

// --- periodic interrupts: an FspTimer per slot, set up once
static FspTimer timers[2];
static bool timer_ready[2] = {false, false};
static void (*volatile ticks[2])() = {nullptr, nullptr};

static void tick0(timer_callback_args_t *) {
    void (*tick)() = ticks[0];
    if (tick) tick();
}

static void tick1(timer_callback_args_t *) {
    void (*tick)() = ticks[1];
    if (tick) tick();
}

float timer_rate(float rate) { return rate; }

uint8_t timer_start(uint8_t slot, float rate, void (*tick)(), float *actual) {
    if (ticks[slot]) return proto::ERR_BUSY;
    FspTimer &timer = timers[slot];
    if (!timer_ready[slot]) {
        uint8_t type = GPT_TIMER;
        int8_t channel = FspTimer::get_available_timer(type);
        if (channel < 0) {
            FspTimer::force_use_of_pwm_reserved_timer();
            channel = FspTimer::get_available_timer(type);
        }
        if (channel < 0) return proto::ERR_BUSY;
        if (!timer.begin(TIMER_MODE_PERIODIC, type, (uint8_t)channel, rate, 0.0f, slot ? tick1 : tick0)) return proto::ERR_UNSUPPORTED;
        if (!timer.setup_overflow_irq() || !timer.open()) return proto::ERR_UNSUPPORTED;
        timer_ready[slot] = true;
    } else {
        timer.stop();
        if (!timer.set_frequency(rate)) return proto::ERR_UNSUPPORTED;
    }
    ticks[slot] = tick;
    timer.reset();
    timer.start();
    *actual = rate;
    return 0;
}

void timer_stop(uint8_t slot) {
    if (timer_ready[slot]) timers[slot].stop();
    ticks[slot] = nullptr;
}

// --- clock edges
static int edge_gpio = -1;

uint8_t edge_attach(uint8_t pin, bool falling, void (*edge)()) {
    edge_detach();
    edge_gpio = gpio(pin);
    attachInterrupt(digitalPinToInterrupt(edge_gpio), edge, falling ? FALLING : RISING);
    return 0;
}

void edge_detach() {
    if (edge_gpio >= 0) detachInterrupt(digitalPinToInterrupt(edge_gpio));
    edge_gpio = -1;
}

// --- the counted clock: the CPU cycle counter, always there
uint32_t clock_ticks(float) { return SystemCoreClock; }

uint8_t clock_begin(float, uint32_t *ticks_per_second) {
    *ticks_per_second = SystemCoreClock;
    return 0;
}

void clock_end() {}
void quiet_begin() {}
void quiet_end(uint32_t) {}

uint32_t irq_save() {
    uint32_t state = __get_PRIMASK();
    __disable_irq();
    return state;
}

void irq_restore(uint32_t state) { __set_PRIMASK(state); }

}  // namespace hal

#endif
