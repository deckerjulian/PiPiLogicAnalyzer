// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Capability strings of the firmware: the CAPS: answer of command 8.
//
// docs/protocols.md is their only source; tests/test_protocol_spec.py checks this file (and the
// Python constants) against its table. Strings ending in "=" are followed by their parameter.

#ifndef CAPABILITIES_H
#define CAPABILITIES_H

#define CAP_SELFTEST "SELFTEST"
#define CAP_SIMULATION "SIMULATION"
#define CAP_DEVICEINFO "DEVICEINFO"
#define CAP_EDGE_TRIGGER_OUT "EDGE_TRIGGER_OUT"
#define CAP_PATTERN_GROUPS "PATTERN_GROUPS="
#define CAP_STREAM "STREAM"
#define CAP_TRIGGER_SEQUENCE "TRIGGER_SEQUENCE"
#define CAP_TRIGGER_CONDITIONS "TRIGGER_CONDITIONS="
#define CAP_SEQUENCE_MAX_RATE "SEQUENCE_MAX_RATE="
#define CAP_STATE_MODE "STATE_MODE"
#define CAP_STATE_MAX_CLOCK "STATE_MAX_CLOCK="
// protocol 8
#define CAP_STREAM_STATE "STREAM_STATE"
#define CAP_GPIO "GPIO"
#define CAP_PWM "PWM"
#define CAP_MONITOR "MONITOR"
#define CAP_ANALOG "ANALOG="
#define CAP_PATTERN_GEN "PATTERN_GEN="
#define CAP_GEN_SQUARE "GEN_SQUARE"
#define CAP_TX_UART "TX_UART"
#define CAP_TX_SPI "TX_SPI"
#define CAP_TX_I2C "TX_I2C"

// Pin capabilities of the pin table (command 11, table "pin-capabilities" of docs/protocols.md)
#define PINCAP_DIN "DIN"
#define PINCAP_DOUT "DOUT"
#define PINCAP_PULLUP "PULLUP"
#define PINCAP_PULLDOWN "PULLDOWN"
#define PINCAP_PWM "PWM"
#define PINCAP_ADC "ADC"
#define PINCAP_CLOCK "CLOCK"

// Capabilities of protocol 8 (column "Pico" = 8 in docs/protocols.md until the column says yes)

#endif
