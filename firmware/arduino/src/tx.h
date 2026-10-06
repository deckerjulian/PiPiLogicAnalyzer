// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Sending protocols: UART, SPI and I2C, bit-banged on any free pins (portable, timed by the
// counted clock of the hal).

#ifndef TX_H
#define TX_H

#include "protocol.h"

namespace tx {

// TX_UART <BI> pin, baud, then the bytes
uint8_t uart(const uint8_t *data, uint8_t n);
// TX_SPI <BBBBI> SCK, MOSI, MISO, CS, frequency, then the bytes; the bytes read go to out
uint8_t spi(const uint8_t *data, uint8_t n, proto::Writer &out);
// TX_I2C <BBBIB> SDA, SCL, address, frequency, bytes to read, then the bytes to write
uint8_t i2c(const uint8_t *data, uint8_t n, proto::Writer &out);

}  // namespace tx

#endif
