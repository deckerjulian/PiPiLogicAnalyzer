// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// openSciLab Arduino benchmark (roadmap step 2a). Measures on the board:
//
//   port_read_hz     reads of a port register per second (the bound of buffer captures)
//   digital_read_hz  digitalRead() per second
//   port_write_hz    writes of a port register per second (the bound of the pattern output)
//   analog_read_hz   analogRead() of one channel per second
//   timer_isr_hz     the highest periodic interrupt rate that still leaves the loop half its time
//   pin_isr          edges counted by an interrupt on PIN_ISR_IN while PIN_ISR_OUT toggles as
//                    fast as it can (wire the two pins; 0 counted: no wire)
//   serial_bytes_s   bytes per second the board writes to the serial line (the host side is
//                    measured by read_benchmark.py, which is the number that counts)
//
// Every run prints one JSON line, then a block of serial data for the throughput test, framed
// by the lines "BLAST <bytes>" and "END". Send any byte to start a run.

#include <Arduino.h>

#if defined(__AVR__)
#define PIN_ISR_IN 2
#define PIN_ISR_OUT 3
#define READ_PORT() (PIND)
#define WRITE_PORT(v) (PORTB = (v))
#elif defined(ARDUINO_ARCH_RENESAS)
#include <FspTimer.h>
#define PIN_ISR_IN 2
#define PIN_ISR_OUT 3
#define READ_PORT() (R_PORT1->PIDR)
#define WRITE_PORT(v) (R_PORT4->PODR = (v))  // port 4 is not on the headers of the Minima
#elif defined(ARDUINO_ARCH_ESP32)
#include <soc/gpio_reg.h>
#define PIN_ISR_IN 5
#define PIN_ISR_OUT 4
#define READ_PORT() (REG_READ(GPIO_IN_REG))
#define WRITE_PORT(v) (REG_WRITE(GPIO_OUT_W1TC_REG, (v) & 0))  // a register write, no pin changes
#endif

#ifndef BENCH_BAUD
#define BENCH_BAUD 115200
#endif

static volatile uint32_t sink;
static volatile uint32_t isr_count;

static void count_edge() { isr_count++; }

static float per_second(uint32_t count, uint32_t us) { return us ? (float)count * 1e6f / (float)us : 0.0f; }

static float bench_port_read() {
    const uint32_t n = 100000;
    uint32_t start = micros();
    for (uint32_t i = 0; i < n; i++) sink = READ_PORT();
    return per_second(n, micros() - start);
}

static float bench_port_write() {
    const uint32_t n = 100000;
    uint32_t start = micros();
    for (uint32_t i = 0; i < n; i++) WRITE_PORT(0);
    return per_second(n, micros() - start);
}

static float bench_digital_read() {
    const uint32_t n = 20000;
    uint32_t start = micros();
    for (uint32_t i = 0; i < n; i++) sink = digitalRead(PIN_ISR_IN);
    return per_second(n, micros() - start);
}

static float bench_analog_read() {
    const uint32_t n = 2000;
    uint32_t start = micros();
    for (uint32_t i = 0; i < n; i++) sink = analogRead(A0);
    return per_second(n, micros() - start);
}

static uint32_t bench_pin_isr() {
    pinMode(PIN_ISR_OUT, OUTPUT);
    pinMode(PIN_ISR_IN, INPUT);
    isr_count = 0;
    attachInterrupt(digitalPinToInterrupt(PIN_ISR_IN), count_edge, RISING);
    const uint32_t toggles = 20000;
    for (uint32_t i = 0; i < toggles; i++) {
        digitalWrite(PIN_ISR_OUT, HIGH);
        digitalWrite(PIN_ISR_OUT, LOW);
    }
    detachInterrupt(digitalPinToInterrupt(PIN_ISR_IN));
    pinMode(PIN_ISR_OUT, INPUT);
    return isr_count;  // of 20000 rising edges
}

// --- a periodic interrupt at a rate: how many loop iterations are left
static volatile uint32_t ticks;
static void on_tick() { ticks++; }

#if defined(__AVR__)
ISR(TIMER2_COMPA_vect) { ticks++; }
static void timer_on(uint32_t rate) {
    uint32_t ocr = F_CPU / 8 / rate - 1;
    if (ocr > 255) ocr = 255;
    TCCR2A = _BV(WGM21);
    TCCR2B = _BV(CS21);
    OCR2A = (uint8_t)ocr;
    TIMSK2 = _BV(OCIE2A);
}
static void timer_off() { TIMSK2 = 0; }
#elif defined(ARDUINO_ARCH_RENESAS)
static FspTimer timer;
static bool timer_open = false;
static void renesas_tick(timer_callback_args_t *) { on_tick(); }
static void timer_on(uint32_t rate) {
    if (!timer_open) {
        uint8_t type = GPT_TIMER;
        int8_t channel = FspTimer::get_available_timer(type);
        timer.begin(TIMER_MODE_PERIODIC, type, (uint8_t)channel, (float)rate, 0.0f, renesas_tick);
        timer.setup_overflow_irq();
        timer.open();
        timer_open = true;
    } else {
        timer.stop();
        timer.set_frequency((float)rate);
    }
    timer.start();
}
static void timer_off() { timer.stop(); }
#elif defined(ARDUINO_ARCH_ESP32)
static hw_timer_t *timer = nullptr;
static void IRAM_ATTR esp_tick() { ticks++; }
static void timer_on(uint32_t rate) {
    if (!timer) {
        timer = timerBegin(0, 2, true);
        timerAttachInterrupt(timer, esp_tick, true);
    }
    timerAlarmWrite(timer, 40000000ull / rate, true);
    timerAlarmEnable(timer);
}
static void timer_off() { timerAlarmDisable(timer); }
#endif

static uint32_t loop_rate(uint32_t ms) {
    uint32_t count = 0, start = millis();
    while (millis() - start < ms) count++;
    return count;
}

static uint32_t bench_timer_isr() {
    (void)on_tick;
    uint32_t idle = loop_rate(100);
    uint32_t best = 0;
    for (uint32_t rate = 1000; rate <= 2000000; rate *= 2) {
#if defined(__AVR__)
        if (F_CPU / 8 / rate < 2) break;
#endif
        ticks = 0;
        timer_on(rate);
        uint32_t busy = loop_rate(100);
        timer_off();
        uint32_t got = ticks;
        // the interrupt keeps up and the loop keeps half its speed
        if (got >= rate / 10 * 9 / 10 && busy * 2 >= idle) best = rate;
        else break;
    }
    return best;
}

static float bench_serial(uint32_t bytes) {
    static uint8_t block[64];
    for (uint8_t i = 0; i < sizeof(block); i++) block[i] = (uint8_t)('A' + i % 26);
    Serial.print("BLAST ");
    Serial.println(bytes);
    Serial.flush();
    uint32_t start = micros();
    for (uint32_t sent = 0; sent < bytes; sent += sizeof(block)) Serial.write(block, sizeof(block));
    Serial.flush();
    uint32_t us = micros() - start;
    Serial.println();
    Serial.println("END");
    return per_second(bytes, us);
}

static void run() {
    float port_read = bench_port_read();
    float port_write = bench_port_write();
    float digital = bench_digital_read();
    float analog = bench_analog_read();
    uint32_t pin_isr = bench_pin_isr();
    uint32_t timer_isr = bench_timer_isr();
#if defined(__AVR__)
    uint32_t blast = 20000;
#else
    uint32_t blast = 500000;
#endif
    float serial = bench_serial(blast);
    Serial.print("{\"board\":\"");
#if defined(ARDUINO_AVR_MEGA2560)
    Serial.print("mega2560");
#elif defined(ARDUINO_AVR_NANO)
    Serial.print("nano");
#elif defined(__AVR__)
    Serial.print("uno");
#elif defined(ARDUINO_ARCH_RENESAS)
    Serial.print("uno_r4");
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
    Serial.print("esp32s3");
#else
    Serial.print("esp32");
#endif
    Serial.print("\",\"cpu_hz\":");
    Serial.print((uint32_t)F_CPU);
    Serial.print(",\"port_read_hz\":");
    Serial.print(port_read, 0);
    Serial.print(",\"port_write_hz\":");
    Serial.print(port_write, 0);
    Serial.print(",\"digital_read_hz\":");
    Serial.print(digital, 0);
    Serial.print(",\"analog_read_hz\":");
    Serial.print(analog, 0);
    Serial.print(",\"timer_isr_hz\":");
    Serial.print(timer_isr);
    Serial.print(",\"pin_isr\":{\"edges\":20000,\"counted\":");
    Serial.print(pin_isr);
    Serial.print("},\"serial_bytes_s\":");
    Serial.print(serial, 0);
    Serial.println("}");
}

void setup() {
    Serial.begin(BENCH_BAUD);
    while (!Serial) {
    }
    Serial.println("openSciLab benchmark: send any byte to start");
}

void loop() {
    if (Serial.available()) {
        while (Serial.available()) Serial.read();
        run();
    }
}
