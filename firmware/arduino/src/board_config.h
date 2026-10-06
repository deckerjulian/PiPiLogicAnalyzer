// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Memory sizes per board (chosen by the -D FW_BOARD_<name> of platformio.ini).
//
// FW_CAPTURE_BYTES  the capture buffer (rings of stream and state captures, buffer captures)
// FW_GEN_RUNS       runs of the pattern generator (6 bytes each)
// FW_ARB_POINTS     DAC points of the arbitrary waveform (2 bytes each; 0 without a DAC)
// FW_ADC_CHANNELS   analog channels at most

#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#if defined(FW_BOARD_UNO) || defined(FW_BOARD_NANO)
// ATmega328P, 2 KiB RAM: the frame buffers take 0.5 KiB, the serial line 0.16 KiB
#define FW_CAPTURE_BYTES 448
#define FW_GEN_RUNS 32
#define FW_ARB_POINTS 0
#define FW_ADC_CHANNELS 8
#elif defined(FW_BOARD_MEGA2560)
// ATmega2560, 8 KiB RAM
#define FW_CAPTURE_BYTES 4096
#define FW_GEN_RUNS 256
#define FW_ARB_POINTS 0
#define FW_ADC_CHANNELS 8
#elif defined(FW_BOARD_UNO_R4)
// RA4M1, 32 KiB RAM (the USB stack takes a part)
#define FW_CAPTURE_BYTES 12288
#define FW_GEN_RUNS 512
#define FW_ARB_POINTS 2048
#define FW_ADC_CHANNELS 6
#elif defined(FW_BOARD_ESP32)
// static DRAM of the ESP32 holds about 100 KiB beside the core
#define FW_CAPTURE_BYTES 49152
#define FW_GEN_RUNS 2048
#define FW_ARB_POINTS 8192
#define FW_ADC_CHANNELS 6
#elif defined(FW_BOARD_ESP32S3)
#define FW_CAPTURE_BYTES 65536
#define FW_GEN_RUNS 2048
#define FW_ARB_POINTS 0
#define FW_ADC_CHANNELS 10
#elif defined(FW_BOARD_NATIVE)
// the fake board of the native tests
#define FW_CAPTURE_BYTES 600
#define FW_GEN_RUNS 16
#define FW_ARB_POINTS 64
#define FW_ADC_CHANNELS 6
#else
#error "No board: build with -D FW_BOARD_<name> (see platformio.ini)"
#endif

#endif
