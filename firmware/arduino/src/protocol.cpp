// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

#include "protocol.h"

namespace proto {

uint8_t crc8(const uint8_t *data, size_t n, uint8_t crc) {
    for (size_t i = 0; i < n; i++) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; bit++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
    return crc;
}

// The blocks of COBS: up to 254 non-zero bytes behind a code byte (their count + 1). A block of
// 254 bytes is not followed by an implied zero. Calls emit(code, block, length) for each block.
template <typename Emit>
static void cobs_blocks(const uint8_t *in, size_t n, Emit emit) {
    size_t i = 0;
    for (;;) {
        size_t j = i;
        while (j < n && in[j] != 0 && j - i < 254) j++;
        uint8_t code = (uint8_t)(j - i + 1);
        emit(code, in + i, j - i);
        if (code == 0xFF) {
            i = j;  // a full block: the next one starts right behind it
            continue;
        }
        if (j >= n) break;
        i = j + 1;  // skip the zero the block stands for
    }
}

size_t cobs_encode(const uint8_t *in, size_t n, uint8_t *out) {
    size_t length = 0;
    cobs_blocks(in, n, [&](uint8_t code, const uint8_t *block, size_t count) {
        out[length++] = code;
        memcpy(out + length, block, count);
        length += count;
    });
    return length;
}

long cobs_decode(const uint8_t *in, size_t n, uint8_t *out) {
    size_t index = 0, length = 0;
    while (index < n) {
        uint8_t code = in[index];
        if (code == 0) return -1;
        index++;
        size_t count = (size_t)code - 1;
        if (index + count > n) return -1;
        memmove(out + length, in + index, count);
        length += count;
        index += count;
        if (code != 0xFF && index < n) out[length++] = 0;
    }
    return (long)length;
}

bool Decoder::feed(uint8_t byte) {
    if (byte != 0) {
        if (length_ < sizeof(buffer_)) buffer_[length_++] = byte;
        else overlong_ = true;
        return false;
    }
    if (length_ == 0 && !overlong_) return false;  // empty frame: ignored
    bool ok = !overlong_;
    long decoded = ok ? cobs_decode(buffer_, length_, buffer_) : -1;
    length_ = 0;
    overlong_ = false;
    if (decoded < 3 || decoded > (long)MAX_BODY || crc8(buffer_, (size_t)decoded - 1) != buffer_[decoded - 1]) {
        damaged_++;
        return false;
    }
    frame_.type = buffer_[0];
    frame_.sequence = buffer_[1];
    frame_.payload = buffer_ + 2;
    frame_.length = (uint8_t)(decoded - 3);
    return true;
}

void Writer::begin(uint8_t type, uint8_t sequence) {
    body_[0] = type;
    body_[1] = sequence;
    length_ = 2;
    overflowed_ = false;
}

void Writer::bytes(const uint8_t *data, size_t n) {
    if (n > room()) {
        n = room();
        overflowed_ = true;
    }
    memcpy(body_ + length_, data, n);
    length_ += n;
}

void Writer::u8(uint8_t v) { bytes(&v, 1); }

void Writer::u16(uint16_t v) {
    uint8_t raw[2];
    put_u16(raw, v);
    bytes(raw, 2);
}

void Writer::u32(uint32_t v) {
    uint8_t raw[4];
    put_u32(raw, v);
    bytes(raw, 4);
}

void Writer::f32(float v) {
    uint32_t raw;
    memcpy(&raw, &v, 4);
    u32(raw);
}

void Writer::text(const char *s) { bytes((const uint8_t *)s, strlen(s)); }

void Writer::text_flash(const char *s) {
    for (;; s++) {
        uint8_t c = fw_flash_byte(s);
        if (!c) break;
        u8(c);
    }
}

void Writer::decimal(uint32_t v) {
    char digits[11];
    uint8_t n = 0;
    do {
        digits[n++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (n) u8((uint8_t)digits[--n]);
}

void Writer::send(Sink sink) {
    body_[length_] = crc8(body_, length_);
    // COBS straight from the body to the line: no second buffer
    cobs_blocks(body_, length_ + 1, [&](uint8_t code, const uint8_t *block, size_t count) {
        sink(&code, 1);
        if (count) sink(block, count);
    });
    static const uint8_t end = 0;
    sink(&end, 1);
}

}  // namespace proto
