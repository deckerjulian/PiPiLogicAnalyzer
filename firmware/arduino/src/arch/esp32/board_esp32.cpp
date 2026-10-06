// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// ESP32 (DevKitC, WROOM-32) and ESP32-S3 (DevKitC-1): pin tables and hal.
//
// Two hardware timers are the periodic interrupts, LEDC drives PWM and the square wave (even
// channels only: two channels share a LEDC timer), the ESP32 has 8 bit DACs on GPIO25/26, the
// ADC reads 12 bit (ADC1 pins only, so WiFi would not disturb it). The ESP32 talks over its UART
// bridge (GPIO1/3 reserved), the S3 over its native USB (GPIO19/20 reserved).

#if defined(ARDUINO_ARCH_ESP32)

#include <Arduino.h>

#include "../../board.h"
#include "../../board_config.h"
#include "../../hal.h"
#include "../../protocol.h"

using namespace board;

namespace board {

#define IO (PC_DIN | PC_DOUT | PC_PULLUP | PC_PULLDOWN | PC_PWM | PC_SQUARE | PC_CLOCK_SW)
#define IN (PC_DIN | PC_CLOCK_SW)

#if defined(FW_BOARD_ESP32S3)

const PinDef PIN_TABLE[] = {
    {"GPIO1", 1, IO | PC_ADC, 0, RES_FREE},   {"GPIO2", 2, IO | PC_ADC, 1, RES_FREE},
    {"GPIO3", 3, IO | PC_ADC, 2, RES_FREE},   {"GPIO4", 4, IO | PC_ADC, 3, RES_FREE},
    {"GPIO5", 5, IO | PC_ADC, 4, RES_FREE},   {"GPIO6", 6, IO | PC_ADC, 5, RES_FREE},
    {"GPIO7", 7, IO | PC_ADC, 6, RES_FREE},   {"GPIO8", 8, IO | PC_ADC, 7, RES_FREE},
    {"GPIO9", 9, IO | PC_ADC, 8, RES_FREE},   {"GPIO10", 10, IO | PC_ADC, 9, RES_FREE},
    {"GPIO11", 11, IO, NO_CHANNEL, RES_FREE}, {"GPIO12", 12, IO, NO_CHANNEL, RES_FREE},
    {"GPIO13", 13, IO, NO_CHANNEL, RES_FREE}, {"GPIO14", 14, IO, NO_CHANNEL, RES_FREE},
    {"GPIO15", 15, IO, NO_CHANNEL, RES_FREE}, {"GPIO16", 16, IO, NO_CHANNEL, RES_FREE},
    {"GPIO17", 17, IO, NO_CHANNEL, RES_FREE}, {"GPIO18", 18, IO, NO_CHANNEL, RES_FREE},
    {"GPIO19", 19, PC_DIN, NO_CHANNEL, RES_USB_DM}, {"GPIO20", 20, PC_DIN, NO_CHANNEL, RES_USB_DP},
    {"GPIO21", 21, IO, NO_CHANNEL, RES_FREE}, {"GPIO38", 38, IO, NO_CHANNEL, RES_FREE},
    {"GPIO39", 39, IO, NO_CHANNEL, RES_FREE}, {"GPIO40", 40, IO, NO_CHANNEL, RES_FREE},
    {"GPIO41", 41, IO, NO_CHANNEL, RES_FREE}, {"GPIO42", 42, IO, NO_CHANNEL, RES_FREE},
    {"GPIO43", 43, IO, NO_CHANNEL, RES_FREE}, {"GPIO44", 44, IO, NO_CHANNEL, RES_FREE},
    {"GPIO47", 47, IO, NO_CHANNEL, RES_FREE}, {"GPIO48", 48, IO, NO_CHANNEL, RES_FREE},
    {"GPIO0", 0, IO, NO_CHANNEL, RES_FREE},
};

const char *name() { return "esp32s3"; }

// Rates are estimates until the benchmark (step 2a) measured them.
const Info INFO = {
    240000000,  // clock_hz
    2000000,    // serial_baud (USB: any)
    12,         // adc_bits
    0,          // dac_bits
    3300,       // vref_mv
    3300,       // logic_mv
    100000,     // stream_max_rate
    2000000,    // buffer_max_rate
    2000000,    // monitor_max_mhz (2 kHz)
    200000,     // generator_max_rate
    0,          // arb_max_rate
    500000,     // stream_bytes
    false,      // gpio_locked_in_buffer
};

static const uint8_t LEDC_CHANNELS = 8;

#else  // ESP32

const PinDef PIN_TABLE[] = {
    {"GPIO1", 1, PC_DIN, NO_CHANNEL, RES_SERIAL_TX}, {"GPIO3", 3, PC_DIN, NO_CHANNEL, RES_SERIAL_RX},
    {"GPIO2", 2, IO, NO_CHANNEL, RES_FREE},   {"GPIO4", 4, IO, NO_CHANNEL, RES_FREE},
    {"GPIO5", 5, IO, NO_CHANNEL, RES_FREE},   {"GPIO12", 12, IO, NO_CHANNEL, RES_FREE},
    {"GPIO13", 13, IO, NO_CHANNEL, RES_FREE}, {"GPIO14", 14, IO, NO_CHANNEL, RES_FREE},
    {"GPIO15", 15, IO, NO_CHANNEL, RES_FREE}, {"GPIO16", 16, IO, NO_CHANNEL, RES_FREE},
    {"GPIO17", 17, IO, NO_CHANNEL, RES_FREE}, {"GPIO18", 18, IO, NO_CHANNEL, RES_FREE},
    {"GPIO19", 19, IO, NO_CHANNEL, RES_FREE}, {"GPIO21", 21, IO, NO_CHANNEL, RES_FREE},
    {"GPIO22", 22, IO, NO_CHANNEL, RES_FREE}, {"GPIO23", 23, IO, NO_CHANNEL, RES_FREE},
    {"GPIO25", 25, IO | PC_DAC, NO_CHANNEL, RES_FREE}, {"GPIO26", 26, IO | PC_DAC, NO_CHANNEL, RES_FREE},
    {"GPIO27", 27, IO, NO_CHANNEL, RES_FREE},
    {"GPIO32", 32, IO | PC_ADC, 2, RES_FREE}, {"GPIO33", 33, IO | PC_ADC, 3, RES_FREE},
    {"GPIO34", 34, IN | PC_ADC, 4, RES_FREE}, {"GPIO35", 35, IN | PC_ADC, 5, RES_FREE},
    {"GPIO36", 36, IN | PC_ADC, 0, RES_FREE}, {"GPIO39", 39, IN | PC_ADC, 1, RES_FREE},
    {"GPIO0", 0, IO, NO_CHANNEL, RES_FREE},
};

const char *name() { return "esp32"; }

// Rates are estimates until the benchmark (step 2a) measured them.
const Info INFO = {
    240000000,  // clock_hz
    2000000,    // serial_baud
    12,         // adc_bits
    8,          // dac_bits
    3300,       // vref_mv
    3300,       // logic_mv
    100000,     // stream_max_rate
    2000000,    // buffer_max_rate
    2000000,    // monitor_max_mhz (2 kHz)
    200000,     // generator_max_rate
    100000,     // arb_max_rate
    200000,     // stream_bytes (2 Mbaud)
    false,      // gpio_locked_in_buffer
};

static const uint8_t LEDC_CHANNELS = 16;

#endif

#undef IO
#undef IN

const uint8_t PIN_COUNT = sizeof(PIN_TABLE) / sizeof(PIN_TABLE[0]);

}  // namespace board

namespace hal {

namespace esp {
uint8_t pin_gpio[32];
uint8_t pin_count = 0;
}  // namespace esp

static inline uint8_t gpio(uint8_t pin) { return PIN_TABLE[pin].gpio; }

void init() {
    for (uint8_t i = 0; i < PIN_COUNT; i++) {
        esp::pin_gpio[i] = gpio(i);
        if (PIN_TABLE[i].reserved == RES_FREE) pinMode(gpio(i), INPUT);
    }
    esp::pin_count = PIN_COUNT;
    analogReadResolution(12);
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

void serial_begin(uint32_t baud) {
    Serial.setRxBufferSize(1024);
    Serial.begin(baud);
}

int serial_read() { return Serial.available() > 0 ? Serial.read() : -1; }
void serial_write(const uint8_t *data, size_t n) { Serial.write(data, n); }

void pin_input(uint8_t pin, uint8_t pull) {
    pinMode(gpio(pin), pull == PULL_UP ? INPUT_PULLUP : pull == PULL_DOWN ? INPUT_PULLDOWN : INPUT);
}

void pin_output(uint8_t pin, bool level) {
    digitalWrite(gpio(pin), level ? HIGH : LOW);
    pinMode(gpio(pin), OUTPUT);
}

bool pin_read(uint8_t pin) { return digitalRead(gpio(pin)) == HIGH; }

// --- LEDC: PWM and the square wave on even channels
static int8_t ledc_of[32];  // channel per pin, -1: none
static bool ledc_init = false;

static int8_t ledc_channel(uint8_t pin) {
    if (!ledc_init) {
        for (uint8_t i = 0; i < 32; i++) ledc_of[i] = -1;
        ledc_init = true;
    }
    if (ledc_of[pin] >= 0) return ledc_of[pin];
    for (uint8_t channel = 0; channel < board::LEDC_CHANNELS; channel += 2) {
        bool used = false;
        for (uint8_t i = 0; i < 32; i++) used |= ledc_of[i] == channel;
        if (!used) return ledc_of[pin] = (int8_t)channel;
    }
    return -1;
}

uint8_t pwm_start(uint8_t pin, float frequency, uint16_t duty, float *actual) {
    int8_t channel = ledc_channel(pin);
    if (channel < 0) return proto::ERR_BUSY;
    uint8_t bits = 1;
    while (bits < 14 && frequency * (float)(1u << (bits + 1)) <= 80000000.0f) bits++;
    uint32_t got = ledcSetup((uint8_t)channel, (uint32_t)(frequency + 0.5f), bits);
    if (!got) {
        ledc_of[pin] = -1;
        return proto::ERR_ARGUMENTS;
    }
    ledcAttachPin(gpio(pin), (uint8_t)channel);
    ledcWrite((uint8_t)channel, (uint32_t)(((uint64_t)duty * ((1u << bits) - 1) + 32767) / 65535));
    *actual = (float)got;
    return 0;
}

void pwm_stop(uint8_t pin) {
    if (ledc_init && ledc_of[pin] >= 0) {
        ledcWrite((uint8_t)ledc_of[pin], 0);
        ledcDetachPin(gpio(pin));
        ledc_of[pin] = -1;
    }
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

// --- DAC and ADC
uint8_t dac_write(uint8_t pin, uint16_t raw) {
#if CONFIG_IDF_TARGET_ESP32
    dacWrite(gpio(pin), (uint8_t)raw);
    return 0;
#else
    (void)pin;
    (void)raw;
    return proto::ERR_UNSUPPORTED;
#endif
}

void dac_stop(uint8_t pin) {
#if CONFIG_IDF_TARGET_ESP32
    dacDisable(gpio(pin));
#endif
    pinMode(gpio(pin), INPUT);
}

uint16_t adc_read(uint8_t channel) {
    uint8_t pin = board::analog_pin(channel);
    return pin == NO_CHANNEL ? 0 : (uint16_t)analogRead(gpio(pin));
}

// --- periodic interrupts: hardware timers 0 and 1 at 40 MHz
static hw_timer_t *timers[2] = {nullptr, nullptr};
static void (*volatile ticks[2])() = {nullptr, nullptr};
static const float TIMER_HZ = 40000000.0f;

static void IRAM_ATTR tick0() {
    void (*tick)() = ticks[0];
    if (tick) tick();
}

static void IRAM_ATTR tick1() {
    void (*tick)() = ticks[1];
    if (tick) tick();
}

static uint64_t alarm_for(float rate) {
    uint64_t alarm = (uint64_t)(TIMER_HZ / rate + 0.5f);
    return alarm < 1 ? 1 : alarm;
}

float timer_rate(float rate) { return TIMER_HZ / (float)alarm_for(rate); }

uint8_t timer_start(uint8_t slot, float rate, void (*tick)(), float *actual) {
    if (ticks[slot]) return proto::ERR_BUSY;
    if (!timers[slot]) {
        timers[slot] = timerBegin(slot, 2, true);  // 80 MHz APB / 2
        if (!timers[slot]) return proto::ERR_BUSY;
        timerAttachInterrupt(timers[slot], slot ? tick1 : tick0, true);
    }
    uint64_t alarm = alarm_for(rate);
    ticks[slot] = tick;
    timerAlarmDisable(timers[slot]);
    timerWrite(timers[slot], 0);
    timerAlarmWrite(timers[slot], alarm, true);
    timerAlarmEnable(timers[slot]);
    *actual = TIMER_HZ / (float)alarm;
    return 0;
}

void timer_stop(uint8_t slot) {
    if (timers[slot]) timerAlarmDisable(timers[slot]);
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

// --- the counted clock: the CPU cycle counter
uint32_t clock_ticks(float) { return getCpuFrequencyMhz() * 1000000u; }

uint8_t clock_begin(float rate, uint32_t *ticks_per_second) {
    *ticks_per_second = clock_ticks(rate);
    return 0;
}

void clock_end() {}
void quiet_begin() {}
void quiet_end(uint32_t) {}

uint32_t irq_save() { return portSET_INTERRUPT_MASK_FROM_ISR(); }
void irq_restore(uint32_t state) { portCLEAR_INTERRUPT_MASK_FROM_ISR(state); }

}  // namespace hal

#endif
