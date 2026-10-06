// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Small portability helpers: constant tables in flash on AVR, plain memory elsewhere.

#ifndef PLATFORM_H
#define PLATFORM_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#if defined(__AVR__)
#include <avr/pgmspace.h>
#define FW_FLASH PROGMEM
#define FW_STR(text) (__extension__({ static const char _s[] PROGMEM = (text); &_s[0]; }))
static inline uint8_t fw_flash_byte(const void *address) { return pgm_read_byte(address); }
static inline void fw_flash_copy(void *to, const void *from, size_t n) { memcpy_P(to, from, n); }
#else
#define FW_FLASH
#define FW_STR(text) (text)
static inline uint8_t fw_flash_byte(const void *address) { return *(const uint8_t *)address; }
static inline void fw_flash_copy(void *to, const void *from, size_t n) { memcpy(to, from, n); }
#endif

// Code that runs in interrupts on the ESP32 lives in IRAM.
#if defined(ARDUINO_ARCH_ESP32)
#include <esp_attr.h>
#define FW_ISR IRAM_ATTR
#else
#define FW_ISR
#endif

// Little-endian access to unaligned bytes.
static inline uint16_t get_u16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static inline uint32_t get_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline float get_f32(const uint8_t *p) {
    uint32_t raw = get_u32(p);
    float value;
    memcpy(&value, &raw, 4);
    return value;
}
static inline void put_u16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void put_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

#endif
