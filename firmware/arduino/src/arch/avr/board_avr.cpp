// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Arduino Uno, Nano and Mega 2560: pin tables and the hal.
//
// Timers: Timer0 keeps millis(); Timer2 is the one periodic interrupt (stream sampling OR the
// pattern generator; PWM on its pins stops meanwhile); Timer1 is the square wave on its OC1x
// pins OR the counted clock of buffer captures and UART sending. Analog values come from
// analogRead() (10 bit, AVcc reference).

#if defined(__AVR__)

#include <Arduino.h>

#include "../../board.h"
#include "../../board_config.h"
#include "../../hal.h"
#include "../../protocol.h"

using namespace board;

namespace board {

#define IO (PC_DIN | PC_DOUT | PC_PULLUP)

#if defined(FW_BOARD_MEGA2560)

const PinDef PIN_TABLE[] FW_FLASH = {
    // PORTA
    {"D22", 22, IO, NO_CHANNEL, RES_FREE}, {"D23", 23, IO, NO_CHANNEL, RES_FREE},
    {"D24", 24, IO, NO_CHANNEL, RES_FREE}, {"D25", 25, IO, NO_CHANNEL, RES_FREE},
    {"D26", 26, IO, NO_CHANNEL, RES_FREE}, {"D27", 27, IO, NO_CHANNEL, RES_FREE},
    {"D28", 28, IO, NO_CHANNEL, RES_FREE}, {"D29", 29, IO, NO_CHANNEL, RES_FREE},
    // PORTB (pin change interrupts PCINT0-7)
    {"D53", 53, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE}, {"D52", 52, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D51", 51, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE}, {"D50", 50, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D10", 10, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D11", 11, IO | PC_PWM | PC_SQUARE | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D12", 12, IO | PC_PWM | PC_SQUARE | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D13", 13, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    // PORTL
    {"D49", 49, IO, NO_CHANNEL, RES_FREE}, {"D48", 48, IO, NO_CHANNEL, RES_FREE},
    {"D47", 47, IO, NO_CHANNEL, RES_FREE}, {"D46", 46, IO | PC_PWM, NO_CHANNEL, RES_FREE},
    {"D45", 45, IO | PC_PWM, NO_CHANNEL, RES_FREE}, {"D44", 44, IO | PC_PWM, NO_CHANNEL, RES_FREE},
    {"D43", 43, IO, NO_CHANNEL, RES_FREE}, {"D42", 42, IO, NO_CHANNEL, RES_FREE},
    // PORTK: A8-A15 (ADC8-15, pin change interrupts PCINT16-23)
    {"A8", A8, IO | PC_ADC | PC_CLOCK_SW, 0, RES_FREE}, {"A9", A9, IO | PC_ADC | PC_CLOCK_SW, 1, RES_FREE},
    {"A10", A10, IO | PC_ADC | PC_CLOCK_SW, 2, RES_FREE}, {"A11", A11, IO | PC_ADC | PC_CLOCK_SW, 3, RES_FREE},
    {"A12", A12, IO | PC_ADC | PC_CLOCK_SW, 4, RES_FREE}, {"A13", A13, IO | PC_ADC | PC_CLOCK_SW, 5, RES_FREE},
    {"A14", A14, IO | PC_ADC | PC_CLOCK_SW, 6, RES_FREE}, {"A15", A15, IO | PC_ADC | PC_CLOCK_SW, 7, RES_FREE},
};

const char *name() { return FW_STR("mega2560"); }

#else  // ATmega328P: Uno, Nano

const PinDef PIN_TABLE[] FW_FLASH = {
    {"D0", 0, PC_DIN, NO_CHANNEL, RES_SERIAL_RX},
    {"D1", 1, PC_DIN, NO_CHANNEL, RES_SERIAL_TX},
    {"D2", 2, IO | PC_CLOCK, NO_CHANNEL, RES_FREE},           // INT0
    {"D3", 3, IO | PC_PWM | PC_CLOCK, NO_CHANNEL, RES_FREE},  // INT1, OC2B
    {"D4", 4, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D5", 5, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D6", 6, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D7", 7, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D8", 8, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D9", 9, IO | PC_PWM | PC_SQUARE | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},   // OC1A
    {"D10", 10, IO | PC_PWM | PC_SQUARE | PC_CLOCK_SW, NO_CHANNEL, RES_FREE}, // OC1B
    {"D11", 11, IO | PC_PWM | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},             // OC2A
    {"D12", 12, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"D13", 13, IO | PC_CLOCK_SW, NO_CHANNEL, RES_FREE},
    {"A0", A0, IO | PC_ADC | PC_CLOCK_SW, 0, RES_FREE},
    {"A1", A1, IO | PC_ADC | PC_CLOCK_SW, 1, RES_FREE},
    {"A2", A2, IO | PC_ADC | PC_CLOCK_SW, 2, RES_FREE},
    {"A3", A3, IO | PC_ADC | PC_CLOCK_SW, 3, RES_FREE},
    {"A4", A4, IO | PC_ADC | PC_CLOCK_SW, 4, RES_FREE},
    {"A5", A5, IO | PC_ADC | PC_CLOCK_SW, 5, RES_FREE},
#if defined(FW_BOARD_NANO)
    {"A6", A6, PC_ADC, 6, RES_FREE},  // analog only
    {"A7", A7, PC_ADC, 7, RES_FREE},
#endif
};

#if defined(FW_BOARD_NANO)
const char *name() { return FW_STR("nano"); }
#else
const char *name() { return FW_STR("uno"); }
#endif

#endif

#undef IO

const uint8_t PIN_COUNT = sizeof(PIN_TABLE) / sizeof(PIN_TABLE[0]);

// Rates are estimates until the benchmark (step 2a) measured them.
const Info INFO = {
    F_CPU,    // clock_hz
    115200,   // serial_baud
    10,       // adc_bits
    0,        // dac_bits
    5000,     // vref_mv
    5000,     // logic_mv
    20000,    // stream_max_rate
    100000,   // buffer_max_rate
    1000000,  // monitor_max_mhz (1 kHz)
    50000,    // generator_max_rate
    0,        // arb_max_rate
    11520,    // stream_bytes (115200 baud)
    true,     // gpio_locked_in_buffer
};

}  // namespace board

// the millis timer of the core (wiring.c)
extern volatile unsigned long timer0_millis;
extern volatile unsigned long timer0_overflow_count;

namespace hal {

static inline uint8_t gpio(uint8_t pin) { return fw_flash_byte(&PIN_TABLE[pin].gpio); }

void init() {
    for (uint8_t i = 0; i < PIN_COUNT; i++)
        if (fw_flash_byte(&PIN_TABLE[i].reserved) == RES_FREE) pinMode(gpio(i), INPUT);
}

uint32_t micros() { return ::micros(); }
uint32_t millis() { return ::millis(); }

void delay_us(uint32_t us) {
    while (us > 10000) {
        delay(10);
        us -= 10000;
    }
    delayMicroseconds((unsigned int)us);
}

void serial_begin(uint32_t baud) { Serial.begin(baud); }
int serial_read() { return Serial.read(); }
void serial_write(const uint8_t *data, size_t n) { Serial.write(data, n); }

void pin_input(uint8_t pin, uint8_t pull) { pinMode(gpio(pin), pull == PULL_UP ? INPUT_PULLUP : INPUT); }

void pin_output(uint8_t pin, bool level) {
    uint8_t g = gpio(pin);
    digitalWrite(g, level ? HIGH : LOW);
    pinMode(g, OUTPUT);
}

bool pin_read(uint8_t pin) { return digitalRead(gpio(pin)) == HIGH; }

// --- timers in use
static const uint8_t NO_OWNER = 0xFF;
static uint8_t timer2_owner = NO_OWNER;
static bool timer1_square = false;
static bool timer1_clock = false;
static uint32_t pwm_active = 0;

static uint8_t timer_of(uint8_t pin) { return digitalPinToTimer(gpio(pin)); }

static bool on_timer2(uint8_t timer) { return timer == TIMER2A || timer == TIMER2B; }

static bool on_timer1(uint8_t timer) {
#if defined(TIMER1C)
    if (timer == TIMER1C) return true;
#endif
    return timer == TIMER1A || timer == TIMER1B;
}

static bool pwm_on(bool (*which)(uint8_t)) {
    for (uint8_t i = 0; i < PIN_COUNT; i++)
        if ((pwm_active & ((uint32_t)1 << i)) && which(timer_of(i))) return true;
    return false;
}

uint8_t pwm_start(uint8_t pin, float, uint16_t duty, float *actual) {
    uint8_t timer = timer_of(pin);
    if (timer == NOT_ON_TIMER) return proto::ERR_UNSUPPORTED;
    if (on_timer2(timer) && timer2_owner != NO_OWNER) return proto::ERR_BUSY;
    if (on_timer1(timer) && (timer1_square || timer1_clock)) return proto::ERR_BUSY;
    // the core's PWM: a fixed frequency per timer (Timer0 fast PWM, the others phase correct)
    analogWrite(gpio(pin), duty >> 8);
    pwm_active |= (uint32_t)1 << pin;
    bool timer0 = timer == TIMER0A || timer == TIMER0B;
    *actual = timer0 ? (float)F_CPU / 64.0f / 256.0f : (float)F_CPU / 64.0f / 510.0f;
    return 0;
}

void pwm_stop(uint8_t pin) {
    analogWrite(gpio(pin), 0);
    pwm_active &= ~((uint32_t)1 << pin);
}

uint8_t dac_write(uint8_t, uint16_t) { return proto::ERR_UNSUPPORTED; }
void dac_stop(uint8_t) {}

uint16_t adc_read(uint8_t channel) {
    uint8_t pin = board::analog_pin(channel);
    if (pin == NO_CHANNEL) return 0;
    return (uint16_t)analogRead(gpio(pin));
}

// --- Timer2: the periodic interrupt
static void (*volatile timer_tick)() = nullptr;
static volatile uint16_t timer_divider = 1;
static volatile uint16_t timer_countdown = 1;

struct Timer2Setting {
    uint8_t cs;
    uint8_t ocr;
    uint16_t divider;
    float actual;
};

static Timer2Setting timer2_setting(float rate) {
    static const uint16_t prescalers[] = {1, 8, 32, 64, 128, 256, 1024};
    Timer2Setting s = {7, 255, 1, 0.0f};
    float cycles = (float)F_CPU / rate;
    uint8_t i = 0;
    for (; i < 7; i++)
        if (cycles / prescalers[i] <= 256.0f) break;
    if (i == 7) {  // slower than Timer2 can count: a divider in the interrupt
        i = 6;
        float d = cycles / 1024.0f / 256.0f;
        s.divider = (uint16_t)(d > 65534.0f ? 65535 : (uint16_t)d + 1);
    }
    float ticks = cycles / prescalers[i] / s.divider;
    long ocr = (long)(ticks + 0.5f) - 1;
    if (ocr < 0) ocr = 0;
    if (ocr > 255) ocr = 255;
    s.cs = (uint8_t)(i + 1);
    s.ocr = (uint8_t)ocr;
    s.actual = (float)F_CPU / ((float)prescalers[i] * (float)(s.ocr + 1) * (float)s.divider);
    return s;
}

ISR(TIMER2_COMPA_vect) {
    if (timer_divider > 1) {
        if (--timer_countdown) return;
        timer_countdown = timer_divider;
    }
    void (*tick)() = timer_tick;
    if (tick) tick();
}

float timer_rate(float rate) { return timer2_setting(rate).actual; }

uint8_t timer_start(uint8_t slot, float rate, void (*tick)(), float *actual) {
    if (timer2_owner != NO_OWNER || pwm_on(on_timer2)) return proto::ERR_BUSY;
    Timer2Setting s = timer2_setting(rate);
    uint8_t sreg = SREG;
    cli();
    timer2_owner = slot;
    timer_tick = tick;
    timer_divider = timer_countdown = s.divider;
    TIMSK2 = 0;
    TCCR2A = _BV(WGM21);  // CTC
    TCCR2B = s.cs;
    OCR2A = s.ocr;
    TCNT2 = 0;
    TIFR2 = _BV(OCF2A);
    TIMSK2 = _BV(OCIE2A);
    SREG = sreg;
    *actual = s.actual;
    return 0;
}

void timer_stop(uint8_t slot) {
    if (timer2_owner != slot) return;
    TIMSK2 = 0;
    TCCR2A = _BV(WGM20);  // back to the core's PWM setting
    TCCR2B = _BV(CS22);
    timer_tick = nullptr;
    timer2_owner = NO_OWNER;
}

// --- clock edges: INT0/INT1 (INT4/5 on the Mega) by attachInterrupt, the rest by pin change
static void (*volatile edge_handler)() = nullptr;
static int8_t edge_interrupt = -1;
static uint8_t edge_pin_g = 0xFF;
static volatile uint8_t *edge_input = nullptr;
static uint8_t edge_bit = 0;
static bool edge_falling = false;
static volatile bool edge_last = false;

static inline void pin_change() {
    if (!edge_input) return;
    bool level = (*edge_input & edge_bit) != 0;
    if (level == edge_last) return;
    edge_last = level;
    if (level != edge_falling) {
        void (*handler)() = edge_handler;
        if (handler) handler();
    }
}

ISR(PCINT0_vect) { pin_change(); }
#if defined(PCINT1_vect)
ISR(PCINT1_vect) { pin_change(); }
#endif
#if defined(PCINT2_vect)
ISR(PCINT2_vect) { pin_change(); }
#endif

uint8_t edge_attach(uint8_t pin, bool falling, void (*edge)()) {
    edge_detach();
    uint8_t g = gpio(pin);
    edge_handler = edge;
    edge_falling = falling;
    edge_pin_g = g;
    int interrupt = digitalPinToInterrupt(g);
    if (interrupt != NOT_AN_INTERRUPT) {
        edge_interrupt = (int8_t)interrupt;
        attachInterrupt(interrupt, edge, falling ? FALLING : RISING);
        return 0;
    }
    if (digitalPinToPCICR(g) == nullptr) return proto::ERR_UNSUPPORTED;
    edge_input = portInputRegister(digitalPinToPort(g));
    edge_bit = digitalPinToBitMask(g);
    edge_last = (*edge_input & edge_bit) != 0;
    *digitalPinToPCMSK(g) |= _BV(digitalPinToPCMSKbit(g));
    *digitalPinToPCICR(g) |= _BV(digitalPinToPCICRbit(g));
    return 0;
}

void edge_detach() {
    if (edge_interrupt >= 0) detachInterrupt(edge_interrupt);
    edge_interrupt = -1;
    if (edge_input && edge_pin_g != 0xFF) {
        *digitalPinToPCMSK(edge_pin_g) &= (uint8_t)~_BV(digitalPinToPCMSKbit(edge_pin_g));
        if (*digitalPinToPCMSK(edge_pin_g) == 0) *digitalPinToPCICR(edge_pin_g) &= (uint8_t)~_BV(digitalPinToPCICRbit(edge_pin_g));
    }
    edge_input = nullptr;
    edge_handler = nullptr;
    edge_pin_g = 0xFF;
}

// --- Timer1: the square wave (CTC, the OC1x pin toggles)
static const uint16_t timer1_prescalers[] = {1, 8, 64, 256, 1024};

uint8_t square_start(uint8_t pin, float frequency, float *actual) {
    uint8_t timer = timer_of(pin);
    if (timer != TIMER1A && timer != TIMER1B) return proto::ERR_UNSUPPORTED;
    if (timer1_clock || pwm_on(on_timer1)) return proto::ERR_BUSY;
    float cycles = (float)F_CPU / (2.0f * frequency);
    uint8_t i = 0;
    for (; i < 4; i++)
        if (cycles / timer1_prescalers[i] <= 65536.0f) break;
    long top = (long)(cycles / timer1_prescalers[i] + 0.5f) - 1;
    if (top < 0) top = 0;
    if (top > 65535) top = 65535;
    pinMode(gpio(pin), OUTPUT);
    uint8_t sreg = SREG;
    cli();
    TCCR1B = 0;
    TCCR1A = timer == TIMER1A ? _BV(COM1A0) : _BV(COM1B0);
    OCR1A = (uint16_t)top;
    OCR1B = 0;
    TCNT1 = 0;
    TCCR1B = (uint8_t)(_BV(WGM12) | (i + 1));
    SREG = sreg;
    timer1_square = true;
    *actual = (float)F_CPU / (2.0f * timer1_prescalers[i] * (float)(top + 1));
    return 0;
}

static void timer1_default() {
    TCCR1B = 0;
    TCCR1A = _BV(WGM10);  // the core's PWM setting
    TCNT1 = 0;
    TCCR1B = _BV(CS11) | _BV(CS10);
}

void square_stop() {
    if (!timer1_square) return;
    timer1_default();
    timer1_square = false;
}

// --- Timer1: the counted clock (normal mode)
static uint8_t clock_prescaler(float slowest_rate) {
    uint8_t i = 0;
    for (; i < 4; i++)
        if ((float)F_CPU / timer1_prescalers[i] / slowest_rate < 32768.0f) break;
    return i;
}

uint32_t clock_ticks(float slowest_rate) { return F_CPU / timer1_prescalers[clock_prescaler(slowest_rate)]; }

uint8_t clock_begin(float slowest_rate, uint32_t *ticks_per_second) {
    if (timer1_square || timer1_clock || pwm_on(on_timer1)) return proto::ERR_BUSY;
    uint8_t i = clock_prescaler(slowest_rate);
    TCCR1B = 0;
    TCCR1A = 0;
    TCNT1 = 0;
    TCCR1B = (uint8_t)(i + 1);
    timer1_clock = true;
    *ticks_per_second = F_CPU / timer1_prescalers[i];
    return 0;
}

void clock_end() {
    if (!timer1_clock) return;
    timer1_default();
    timer1_clock = false;
}

void quiet_begin() { TIMSK0 &= (uint8_t)~_BV(TOIE0); }

void quiet_end(uint32_t elapsed_us) {
    uint8_t sreg = SREG;
    cli();
    timer0_millis += elapsed_us / 1000;
    timer0_overflow_count += elapsed_us / 1024;
    TIMSK0 |= _BV(TOIE0);
    SREG = sreg;
}

uint32_t irq_save() {
    uint8_t sreg = SREG;
    cli();
    return sreg;
}

void irq_restore(uint32_t state) { SREG = (uint8_t)state; }

}  // namespace hal

#endif
