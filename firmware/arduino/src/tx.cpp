// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// TX_UART, TX_SPI, TX_I2C bit-banged on any free pins. UART bits are timed by the counted clock
// with interrupts off for each byte; SPI and I2C (which carry their clock) by delays.

#include "tx.h"

#include "app.h"
#include "board.h"
#include "hal.h"

using namespace proto;

namespace tx {

static const hal::cycle_t HALF = (hal::cycle_t)(((hal::cycle_t)~(hal::cycle_t)0) / 2 + 1);

static uint8_t check_pin(uint8_t pin, uint16_t caps) {
    if (pin >= board::PIN_COUNT) return ERR_ARGUMENTS;
    return app::check_pins(app::pin_bit(pin), caps);
}

uint8_t uart(const uint8_t *d, uint8_t n) {
    if (n < 5) return ERR_ARGUMENTS;
    uint8_t pin = d[0];
    uint32_t baud = get_u32(d + 1);
    uint8_t error = check_pin(pin, board::PC_DOUT);
    if (error) return error;
    if (baud == 0 || baud > board::INFO.clock_hz / 64) return ERR_ARGUMENTS;
    uint32_t ticks;
    error = hal::clock_begin((float)baud, &ticks);
    if (error) return error;
    hal::cycle_t bit_time = (hal::cycle_t)((ticks + baud / 2) / baud);
    if (bit_time == 0 || bit_time >= HALF) {
        hal::clock_end();
        return ERR_ARGUMENTS;
    }
    uint32_t mask = app::pin_bit(pin);
    bool idle = hal::pin_read(pin);
    hal::pin_output(pin, true);
    app::driving(mask);
    if (!idle) hal::delay_us(10000000u / baud + 1);  // a frame of idle line first
    for (uint8_t i = 5; i < n; i++) {
        uint16_t frame = (uint16_t)((uint16_t)d[i] << 1 | 0x200);  // start bit, 8 data bits, stop bit
        uint32_t state = hal::irq_save();
        hal::cycle_t next = hal::cycles();
        for (uint8_t b = 0; b < 10; b++) {
            hal::write_levels(mask, (frame & 1) ? mask : 0);
            frame >>= 1;
            next += bit_time;
            while ((hal::cycle_t)(hal::cycles() - next) >= HALF) {
            }
        }
        hal::irq_restore(state);
    }
    hal::clock_end();
    return 0;
}

uint8_t spi(const uint8_t *d, uint8_t n, Writer &out) {
    if (n < 8) return ERR_ARGUMENTS;
    uint8_t sck = d[0], mosi = d[1], miso = d[2], cs = d[3];
    uint32_t frequency = get_u32(d + 4);
    uint8_t error = check_pin(sck, board::PC_DOUT);
    if (!error) error = check_pin(mosi, board::PC_DOUT);
    if (!error && miso != 0xFF) error = check_pin(miso, board::PC_DIN);
    if (!error && cs != 0xFF) error = check_pin(cs, board::PC_DOUT);
    if (error) return error;
    if (frequency == 0 || sck == mosi || (miso != 0xFF && (miso == sck || miso == mosi))) return ERR_ARGUMENTS;
    uint32_t half_us = 500000u / frequency;
    uint32_t outputs = app::pin_bit(sck) | app::pin_bit(mosi) | (cs != 0xFF ? app::pin_bit(cs) : 0);
    hal::pin_output(sck, false);
    hal::pin_output(mosi, false);
    if (miso != 0xFF) hal::pin_input(miso, hal::PULL_NONE);
    if (cs != 0xFF) hal::pin_output(cs, false);  // mode 0, MSB first
    app::driving(outputs);
    hal::delay_us(half_us);
    for (uint8_t i = 8; i < n; i++) {
        uint8_t sent = d[i], read = 0;
        for (uint8_t b = 0; b < 8; b++) {
            hal::pin_output(mosi, (sent & 0x80) != 0);
            sent <<= 1;
            hal::delay_us(half_us);
            hal::pin_output(sck, true);
            read = (uint8_t)(read << 1 | (miso != 0xFF && hal::pin_read(miso) ? 1 : 0));
            hal::delay_us(half_us);
            hal::pin_output(sck, false);
        }
        if (miso != 0xFF) out.u8(read);
    }
    if (cs != 0xFF) hal::pin_output(cs, true);
    return 0;
}

// --- I2C: open drain (a released line is pulled up)
static uint8_t sda_pin, scl_pin;
static uint32_t i2c_half_us;

static void line(uint8_t pin, bool high) {
    if (high) hal::pin_input(pin, hal::PULL_UP);
    else hal::pin_output(pin, false);
}

static void scl_high() {
    line(scl_pin, true);
    // a device may hold the clock low (clock stretching): wait for it a while
    for (uint16_t i = 0; i < 1000 && !hal::pin_read(scl_pin); i++) hal::delay_us(10);
}

static void wait() { hal::delay_us(i2c_half_us); }

static void i2c_start() {
    line(sda_pin, true);
    scl_high();
    wait();
    line(sda_pin, false);
    wait();
    line(scl_pin, false);
}

static void i2c_stop() {
    line(sda_pin, false);
    wait();
    scl_high();
    wait();
    line(sda_pin, true);
    wait();
}

// true when the byte was acknowledged
static bool i2c_write(uint8_t byte) {
    for (uint8_t b = 0; b < 8; b++) {
        line(sda_pin, (byte & 0x80) != 0);
        byte <<= 1;
        wait();
        scl_high();
        wait();
        line(scl_pin, false);
    }
    line(sda_pin, true);
    wait();
    scl_high();
    bool ack = !hal::pin_read(sda_pin);
    wait();
    line(scl_pin, false);
    return ack;
}

static uint8_t i2c_read(bool ack) {
    uint8_t byte = 0;
    line(sda_pin, true);
    for (uint8_t b = 0; b < 8; b++) {
        wait();
        scl_high();
        byte = (uint8_t)(byte << 1 | (hal::pin_read(sda_pin) ? 1 : 0));
        wait();
        line(scl_pin, false);
    }
    line(sda_pin, !ack);
    wait();
    scl_high();
    wait();
    line(scl_pin, false);
    line(sda_pin, true);
    return byte;
}

uint8_t i2c(const uint8_t *d, uint8_t n, Writer &out) {
    if (n < 8) return ERR_ARGUMENTS;
    uint8_t sda = d[0], scl = d[1], address = d[2];
    uint32_t frequency = get_u32(d + 3);
    uint8_t to_read = d[7];
    uint8_t error = check_pin(sda, board::PC_DIN | board::PC_DOUT);
    if (!error) error = check_pin(scl, board::PC_DIN | board::PC_DOUT);
    if (error) return error;
    if (sda == scl || address > 0x7F || frequency == 0 || to_read > out.room()) return ERR_ARGUMENTS;
    sda_pin = sda;
    scl_pin = scl;
    i2c_half_us = 500000u / frequency;
    if (!i2c_half_us) i2c_half_us = 1;
    app::released(app::pin_bit(sda) | app::pin_bit(scl));
    uint8_t to_write = (uint8_t)(n - 8);
    if (to_write || !to_read) {
        i2c_start();
        bool ack = i2c_write((uint8_t)(address << 1));
        for (uint8_t i = 0; ack && i < to_write; i++) ack = i2c_write(d[8 + i]);
        if (!ack) {
            i2c_stop();
            return ERR_NACK;
        }
    }
    if (to_read) {
        i2c_start();  // a repeated start after the bytes written
        if (!i2c_write((uint8_t)(address << 1 | 1))) {
            i2c_stop();
            return ERR_NACK;
        }
        for (uint8_t i = 0; i < to_read; i++) out.u8(i2c_read(i + 1 < to_read));
    }
    i2c_stop();
    return 0;
}

}  // namespace tx
