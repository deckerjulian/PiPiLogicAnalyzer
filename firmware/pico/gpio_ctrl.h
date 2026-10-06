/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_GPIO_CTRL__
#define __OPENSCILAB_GPIO_CTRL__

//Pins as inputs and outputs (commands 12 PIN_MODE, 13 WRITE, 14 READ, 15 PWM, 16 PULSE, 20 SAFE).
//
//Every pin starts as an input. The module keeps the mode of every pin; the outputs of the other
//modules (pattern generator, transmitters) take their pins through it, so SAFE and the watchdog
//find every driven pin here. Pins of a running capture and reserved pins are refused.

#include "pico/stdlib.h"

//Answers
#define ANSWER_OK "OK\n"
#define ANSWER_ERR_PIN "ERR:PIN\n"
#define ANSWER_ERR_RESERVED "ERR:RESERVED\n"
#define ANSWER_ERR_BUSY "ERR:BUSY\n"
#define ANSWER_ERR_ARG "ERR:ARG\n"

typedef enum
{
    GPIO_MODE_IDLE = 0,     //Not touched since the start or the last SAFE
    GPIO_MODE_INPUT,
    GPIO_MODE_PULLUP,
    GPIO_MODE_PULLDOWN,
    GPIO_MODE_OUTPUT,
    GPIO_MODE_PWM,
    GPIO_MODE_ANALOG,
    GPIO_MODE_PULSE,        //Driven by the pulse state machine
    GPIO_MODE_PATTERN,      //Driven by the pattern generator (its pins and its sync pin)
    GPIO_MODE_TX            //Used by a transmitter while it sends

} GPIO_MODE;

//Pin modes of command 12
#define PIN_MODE_INPUT 0
#define PIN_MODE_PULLUP 1
#define PIN_MODE_PULLDOWN 2
#define PIN_MODE_OUTPUT 3
#define PIN_MODE_PWM 4
#define PIN_MODE_ANALOG 5

/// @brief Checks whether a pin may be used
/// @param gpio The pin
/// @param caps PIN_CAN_* the pin must have
/// @return NULL if it may, else the error answer
const char* gpio_ctrl_check(uint8_t gpio, uint8_t caps);

/// @brief GPIOs of the running capture, stream or analog capture (bit n: GPIO n)
uint32_t gpio_ctrl_busy_mask(void);

/// @brief Command 12
const char* gpio_ctrl_set_mode(uint8_t gpio, uint8_t mode);

/// @brief Command 13: the pins of the mask become outputs with the levels
const char* gpio_ctrl_write(uint32_t mask, uint32_t levels);

/// @brief Command 14: levels of all GPIOs, masked
uint32_t gpio_ctrl_read(uint32_t mask);

/// @brief Command 15
/// @param actual Set to the frequency the slice makes
const char* gpio_ctrl_pwm(uint8_t gpio, float frequency, uint16_t duty, double* actual);

/// @brief Command 16 (returns at once, the state machine makes the pulses)
const char* gpio_ctrl_pulse(uint8_t gpio, uint8_t level, uint32_t widthNs, uint16_t count, uint32_t periodNs);

/// @brief Every driven pin back to an input (PWM and pulses stopped), part of command 20
void gpio_ctrl_safe(void);

/// @brief Pins driven by the board (outputs, PWM, pulses, pattern generator)
uint32_t gpio_ctrl_driven_mask(void);

/// @brief Pins a capture must not take over: the driven pins and the pins of an analog capture
uint32_t gpio_ctrl_claimed_mask(void);

/// @brief Mode of a pin
GPIO_MODE gpio_ctrl_mode(uint8_t gpio);

/// @brief Hands pins to another output (pattern generator, transmitter); stops what drove them before
void gpio_ctrl_take(uint32_t mask, GPIO_MODE mode);

/// @brief Pins of another output become inputs again
void gpio_ctrl_release(uint32_t mask);

/// @brief Marks the pins of an analog capture (analog inputs while it runs)
void gpio_ctrl_set_analog(uint32_t mask);

#endif
