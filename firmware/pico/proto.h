/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_PROTO__
#define __OPENSCILAB_PROTO__

//Framing of the requests (docs/protocols.md, "Pico firmware"):
//
//  request:  0x55 0xAA <payload> 0xAA 0x55
//  payload:  <command byte> [<data>]
//  escaping: 0xAA, 0x55 and 0xF0 inside the payload are sent as 0xF0 (byte ^ 0xF0)
//
//The single byte 0xFF outside a frame stops a capture.
//
//Plain C without Pico SDK dependencies, compiled on the host by tests/ as well.

#include <stdint.h>
#include <stdbool.h>

//Longest frame on the wire (escaped, with start and stop condition)
#define PROTO_FRAME_MAX 512
//Longest variable data of a request (pattern blocks, bytes to send)
#define PROTO_DATA_MAX 200
//Byte that stops a capture when it arrives outside a frame
#define PROTO_CANCEL_BYTE 0xFF

//Commands
#define CMD_ID 0
#define CMD_CAPTURE 1
#define CMD_WIFI_SETTINGS 2
#define CMD_VOLTAGE 3
#define CMD_BOOTLOADER 4
#define CMD_BLINK_ON 5
#define CMD_BLINK_OFF 6
#define CMD_SELF_TEST 7
#define CMD_CAPABILITIES 8
#define CMD_DEVICE_INFO 9
#define CMD_SEQUENCE 10
#define CMD_PINS 11
#define CMD_PIN_MODE 12
#define CMD_WRITE 13
#define CMD_READ 14
#define CMD_PWM 15
#define CMD_PULSE 16
#define CMD_MONITOR 17
#define CMD_HEARTBEAT 18
#define CMD_ADC_READ 19
#define CMD_SAFE 20
#define CMD_GEN_LOAD 21
#define CMD_GEN_START 22
#define CMD_GEN_STOP 23
#define CMD_GEN_STATUS 24
#define CMD_CAPTURE_ANALOG 25
#define CMD_TX_UART 26
#define CMD_TX_SPI 27
#define CMD_TX_I2C 28

typedef enum
{
    PROTO_NONE,         //Nothing complete yet
    PROTO_FRAME,        //A frame is complete: payload and length are set
    PROTO_CANCEL,       //0xFF outside a frame
    PROTO_OVERFLOW      //The frame was longer than PROTO_FRAME_MAX bytes and was dropped

} PROTO_EVENT;

typedef struct _PROTO_PARSER
{
    uint8_t buffer[PROTO_FRAME_MAX];
    uint16_t position;

} PROTO_PARSER;

/// @brief Clears the parser (a frame in progress is dropped)
void proto_reset(PROTO_PARSER* parser);

/// @brief Feeds one received byte
/// @param parser Parser state
/// @param byte Received byte
/// @param payload Set to the unescaped payload (command byte first) when PROTO_FRAME is returned;
/// valid until the next call
/// @param length Set to the length of the payload (0: an empty frame)
/// @return What the byte completed
PROTO_EVENT proto_feed(PROTO_PARSER* parser, uint8_t byte, const uint8_t** payload, uint16_t* length);

/// @brief Escapes a payload into a frame (the inverse of proto_feed, used by the tests)
/// @return Bytes written, 0 if the frame does not fit
uint16_t proto_build(const uint8_t* payload, uint16_t length, uint8_t* frame, uint16_t size);

/// @brief Whether a command is accepted while a buffer capture runs
bool proto_allowed_while_capturing(uint8_t command);

//Little endian fields of the requests (the payload is not aligned)
static inline uint16_t proto_u16(const uint8_t* data)
{
    return (uint16_t)(data[0] | (data[1] << 8));
}

static inline uint32_t proto_u32(const uint8_t* data)
{
    return (uint32_t)data[0] | ((uint32_t)data[1] << 8) | ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);
}

static inline float proto_f32(const uint8_t* data)
{
    union { uint32_t u; float f; } value;
    value.u = proto_u32(data);
    return value.f;
}

#endif
