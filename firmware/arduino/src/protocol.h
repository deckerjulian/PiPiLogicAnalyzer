// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The wire protocol of the openSciLab Arduino firmware (docs/protocols.md, "Arduino firmware").
//
// frame:  COBS(<type> <sequence> <payload> <crc8>) 0x00
// crc8:   polynomial 0x07, initial value 0, not reflected, over type, sequence and payload
//
// Hardware-free: the native tests check it against tests/fixtures/arduino_frames.json.

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include "platform.h"

namespace proto {

const uint8_t PROTOCOL = 1;

// frame types
const uint8_t T_REQUEST = 0x01;
const uint8_t T_ANSWER = 0x02;
const uint8_t T_EVENT = 0x03;
const uint8_t T_ERROR = 0x04;

// payload bytes of one frame at most (command and data, without type, sequence and CRC)
const size_t MAX_PAYLOAD = 250;
// type, sequence, payload and CRC
const size_t MAX_BODY = MAX_PAYLOAD + 3;
// a body of MAX_BODY bytes takes at most this many bytes COBS-encoded (without the zero)
const size_t MAX_ENCODED = MAX_BODY + MAX_BODY / 254 + 1;

// commands
const uint8_t CMD_HELLO = 0x01;
const uint8_t CMD_PINS = 0x02;
const uint8_t CMD_CAPS = 0x03;
const uint8_t CMD_PIN_MODE = 0x10;
const uint8_t CMD_WRITE = 0x11;
const uint8_t CMD_READ = 0x12;
const uint8_t CMD_PWM = 0x13;
const uint8_t CMD_DAC = 0x14;
const uint8_t CMD_PULSE = 0x15;
const uint8_t CMD_ADC_READ = 0x16;
const uint8_t CMD_MONITOR = 0x17;
const uint8_t CMD_HEARTBEAT = 0x18;
const uint8_t CMD_SAFE = 0x19;
const uint8_t CMD_CAPTURE_SETUP = 0x20;
const uint8_t CMD_CAPTURE_START = 0x21;
const uint8_t CMD_CAPTURE_ABORT = 0x22;
const uint8_t CMD_GEN_LOAD = 0x30;
const uint8_t CMD_GEN_START = 0x31;
const uint8_t CMD_GEN_STOP = 0x32;
const uint8_t CMD_GEN_STATUS = 0x33;
const uint8_t CMD_SQUARE = 0x34;
const uint8_t CMD_ARB_LOAD = 0x35;
const uint8_t CMD_ARB_START = 0x36;
const uint8_t CMD_TX_UART = 0x40;
const uint8_t CMD_TX_SPI = 0x41;
const uint8_t CMD_TX_I2C = 0x42;

// events
const uint8_t EVENT_STATE = 0x01;
const uint8_t EVENT_DATA = 0x02;
const uint8_t EVENT_DONE = 0x03;
const uint8_t EVENT_WATCHDOG = 0x04;

// kinds of DATA events
const uint8_t DATA_DIGITAL = 0;
const uint8_t DATA_ANALOG = 1;
const uint8_t DATA_STATE = 2;

// capture modes
const uint8_t MODE_STREAM = 0;
const uint8_t MODE_BUFFER = 1;
const uint8_t MODE_STATE = 2;

// triggers
const uint8_t TRIGGER_NONE = 0;
const uint8_t TRIGGER_RISING = 1;
const uint8_t TRIGGER_FALLING = 2;
const uint8_t TRIGGER_PATTERN = 3;

// DONE status
const uint8_t DONE_COMPLETE = 0;
const uint8_t DONE_OVERFLOW = 1;
const uint8_t DONE_STOPPED = 2;

// error codes
const uint8_t ERR_UNKNOWN = 1;
const uint8_t ERR_ARGUMENTS = 2;
const uint8_t ERR_RESERVED = 3;
const uint8_t ERR_BUSY = 4;
const uint8_t ERR_UNSUPPORTED = 5;
const uint8_t ERR_NACK = 6;
const uint8_t ERR_OVERFLOW = 7;

// pin modes (as the Pico's command 12)
const uint8_t MODE_INPUT = 0;
const uint8_t MODE_INPUT_PULLUP = 1;
const uint8_t MODE_INPUT_PULLDOWN = 2;
const uint8_t MODE_OUTPUT = 3;
const uint8_t MODE_PWM = 4;
const uint8_t MODE_ANALOG = 5;

uint8_t crc8(const uint8_t *data, size_t n, uint8_t crc = 0);

// COBS-encodes n bytes into out (at least n + n / 254 + 1 bytes); returns the encoded length,
// without the frame's ending zero.
size_t cobs_encode(const uint8_t *in, size_t n, uint8_t *out);

// Decodes n COBS bytes (no zero among them) into out, which may be in; returns the decoded
// length or -1 when the bytes are no valid COBS block sequence.
long cobs_decode(const uint8_t *in, size_t n, uint8_t *out);

struct Frame {
    uint8_t type;
    uint8_t sequence;
    const uint8_t *payload;  // command (or event) byte and data
    uint8_t length;          // bytes of payload
};

// Collects frames from the bytes of the line as they arrive (one at a time).
class Decoder {
public:
    // true when byte completed a valid frame: it is in frame() until the next feed
    bool feed(uint8_t byte);
    const Frame &frame() const { return frame_; }
    // frames that could not be read (damaged on the line)
    uint16_t damaged() const { return damaged_; }
    void reset() { length_ = 0; overlong_ = false; }

private:
    uint8_t buffer_[MAX_ENCODED + 2];
    uint16_t length_ = 0;
    bool overlong_ = false;
    uint16_t damaged_ = 0;
    Frame frame_ = {0, 0, nullptr, 0};
};

typedef void (*Sink)(const uint8_t *data, size_t n);

// One outgoing frame: begin, add the payload, send.
class Writer {
public:
    void begin(uint8_t type, uint8_t sequence);
    void u8(uint8_t v);
    void u16(uint16_t v);
    void u32(uint32_t v);
    void f32(float v);
    void bytes(const uint8_t *data, size_t n);
    void text(const char *s);             // RAM string, without its zero
    void text_flash(const char *s);       // flash string (FW_STR), without its zero
    void decimal(uint32_t v);
    // payload bytes left
    size_t room() const { return MAX_PAYLOAD - (length_ - 2); }
    size_t payload_length() const { return length_ - 2; }
    // the payload so far (for tests)
    const uint8_t *payload() const { return body_ + 2; }
    bool overflowed() const { return overflowed_; }
    // finishes the frame (CRC, COBS, zero) and hands it to sink
    void send(Sink sink);

private:
    uint8_t body_[MAX_BODY];
    size_t length_ = 0;
    bool overflowed_ = false;
};

}  // namespace proto

#endif
