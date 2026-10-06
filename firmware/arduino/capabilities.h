// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Capability strings of the Arduino firmware: the answer of command 0x03 CAPS.
//
// docs/protocols.md is their only source; tests/test_protocol_spec.py checks this file (and the
// Python constants) against its table. Strings ending in "=" are followed by their parameter;
// STREAM is reported as "STREAM=<bytes per second>".

#ifndef CAPABILITIES_H
#define CAPABILITIES_H

#define CAP_GPIO "GPIO"
#define CAP_PWM "PWM"
#define CAP_MONITOR "MONITOR"
#define CAP_ANALOG "ANALOG="
#define CAP_DAC "DAC"
#define CAP_AFG "AFG"
#define CAP_IMMEDIATE_TRIGGER "IMMEDIATE_TRIGGER"
#define CAP_CONTINUOUS_STREAM "CONTINUOUS_STREAM"
#define CAP_STREAM "STREAM"
#define CAP_STATE_MODE "STATE_MODE"
#define CAP_STREAM_STATE "STREAM_STATE"
#define CAP_PATTERN_GEN "PATTERN_GEN="
#define CAP_GEN_SQUARE "GEN_SQUARE"
#define CAP_TX_UART "TX_UART"
#define CAP_TX_SPI "TX_SPI"
#define CAP_TX_I2C "TX_I2C"

#endif
