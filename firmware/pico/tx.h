/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_TX__
#define __OPENSCILAB_TX__

//Transmitters (commands 26 TX_UART, 27 TX_SPI, 28 TX_I2C). They send at once and answer when
//done: UART and SPI by a PIO state machine on any pins, I2C by the I2C block of the pins (SDA on
//an even GPIO, SCL on an odd one of the same block). Afterwards the pins rest at their idle
//levels: UART TX high, SPI clock at its polarity, MOSI low and CS high as outputs, the I2C pins as
//inputs with pull-ups.

#include "pico/stdlib.h"

//Most bytes an I2C transaction reads
#define TX_I2C_MAX_READ 255

/// @brief Command 26
const char* tx_uart(uint8_t pin, uint32_t baud, const uint8_t* data, uint32_t length);

/// @brief Command 27
/// @param received Set to the bytes read on MISO (length bytes, nothing without MISO)
/// @param receivedLength Set to their number
const char* tx_spi(uint8_t sck, uint8_t mosi, uint8_t miso, uint8_t cs, uint32_t frequency, uint8_t mode, const uint8_t* data, uint32_t length, uint8_t* received, uint32_t* receivedLength);

/// @brief Command 28
/// @param received Set to the bytes read (readLength bytes)
const char* tx_i2c(uint8_t sda, uint8_t scl, uint8_t address, uint32_t frequency, uint16_t readLength, const uint8_t* data, uint32_t length, uint8_t* received);

#endif
