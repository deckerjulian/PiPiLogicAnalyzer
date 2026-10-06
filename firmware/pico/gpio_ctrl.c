/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"
#include "gpio_ctrl.h"
#include "pins.h"
#include "timing.h"
#include "aux_pio.h"
#include "capture.h"
#include "analog.h"
#include "hardware/gpio.h"
#include "hardware/pwm.h"
#include "hardware/adc.h"
#include "hardware/clocks.h"
#include "pulse.pio.h"

static uint8_t modes[PIN_GPIO_COUNT];

//PWM slices in use: divider and wrap, so pins sharing a slice keep one frequency
#define PWM_SLICE_MAX 16
static uint16_t sliceDiv16[PWM_SLICE_MAX];
static uint16_t sliceTop[PWM_SLICE_MAX];

//The pulse state machine and its pin
static AUX_PIO pulsePio;
static uint8_t pulsePin = 0xFF;
static uint8_t pulseLevel;

static bool is_driven(uint8_t mode)
{
    return mode == GPIO_MODE_OUTPUT || mode == GPIO_MODE_PWM || mode == GPIO_MODE_PULSE || mode == GPIO_MODE_PATTERN || mode == GPIO_MODE_TX;
}

uint32_t gpio_ctrl_busy_mask(void)
{
    return GetCaptureGpioMask() | analog_busy_gpio_mask();
}

const char* gpio_ctrl_check(uint8_t gpio, uint8_t caps)
{
    const PIN_INFO* info = pins_info(gpio);

    if(info == NULL)
        return ANSWER_ERR_PIN;

    if(info->reserved)
        return ANSWER_ERR_RESERVED;

    if((info->caps & caps) != caps)
        return ANSWER_ERR_PIN;

    if(gpio_ctrl_busy_mask() & (1u << gpio))
        return ANSWER_ERR_BUSY;

    if(modes[gpio] == GPIO_MODE_PATTERN || modes[gpio] == GPIO_MODE_TX)
        return ANSWER_ERR_BUSY;

    return NULL;
}

//------------------------------------------------------------------------- PWM

static bool slice_shared(uint slice, uint8_t except)
{
    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
        if(gpio != except && modes[gpio] == GPIO_MODE_PWM && pwm_gpio_to_slice_num(gpio) == slice)
            return true;

    return false;
}

//The pin stops its PWM output; the slice stops when no other pin uses it
static void pwm_leave(uint8_t gpio)
{
    uint slice = pwm_gpio_to_slice_num(gpio);
    pwm_set_chan_level(slice, pwm_gpio_to_channel(gpio), 0);

    if(!slice_shared(slice, gpio))
        pwm_set_enabled(slice, false);
}

//----------------------------------------------------------------------- pulse

static void pulse_stop(void)
{
    if(pulsePin == 0xFF)
        return;

    uint8_t pin = pulsePin;
    pulsePin = 0xFF;

    aux_pio_release(&pulsePio);

    //The pin keeps its idle level as an ordinary output
    gpio_put(pin, pulseLevel ? 0 : 1);
    gpio_set_dir(pin, GPIO_OUT);
    gpio_set_outover(pin, GPIO_OVERRIDE_NORMAL);
    gpio_set_function(pin, GPIO_FUNC_SIO);
    modes[pin] = GPIO_MODE_OUTPUT;
}

//------------------------------------------------------------------- the modes

//Ends what drove the pin, before it gets a new mode
static void leave_mode(uint8_t gpio)
{
    switch(modes[gpio])
    {
        case GPIO_MODE_PWM:
            pwm_leave(gpio);
            break;
        case GPIO_MODE_PULSE:
            if(pulsePin == gpio)
                pulse_stop();
            break;
        default:
            break;
    }

    gpio_set_outover(gpio, GPIO_OVERRIDE_NORMAL);
}

//The pin becomes an input without pulls
static void make_input(uint8_t gpio)
{
    leave_mode(gpio);
    gpio_init(gpio);
    gpio_disable_pulls(gpio);
    modes[gpio] = GPIO_MODE_INPUT;
}

const char* gpio_ctrl_set_mode(uint8_t gpio, uint8_t mode)
{
    static const uint8_t needs[] = { PIN_CAN_DIN, PIN_CAN_PULLUP, PIN_CAN_PULLDOWN, PIN_CAN_DOUT, PIN_CAN_PWM, PIN_CAN_ADC };

    if(mode > PIN_MODE_ANALOG)
        return ANSWER_ERR_ARG;

    const char* error = gpio_ctrl_check(gpio, needs[mode]);
    if(error)
        return error;

    switch(mode)
    {
        case PIN_MODE_INPUT:
            make_input(gpio);
            break;

        case PIN_MODE_PULLUP:
            make_input(gpio);
            gpio_pull_up(gpio);
            modes[gpio] = GPIO_MODE_PULLUP;
            break;

        case PIN_MODE_PULLDOWN:
            make_input(gpio);
            gpio_pull_down(gpio);
            modes[gpio] = GPIO_MODE_PULLDOWN;
            break;

        case PIN_MODE_OUTPUT:
            if(modes[gpio] == GPIO_MODE_PULSE)
                pulse_stop(); //The pin keeps its idle level
            else if(modes[gpio] != GPIO_MODE_OUTPUT)
            {
                //A new output starts low
                leave_mode(gpio);
                gpio_disable_pulls(gpio);
                gpio_put(gpio, 0);
                gpio_set_dir(gpio, GPIO_OUT);
                gpio_set_function(gpio, GPIO_FUNC_SIO);
                modes[gpio] = GPIO_MODE_OUTPUT;
            }
            break;

        case PIN_MODE_PWM:
            if(modes[gpio] != GPIO_MODE_PWM)
            {
                leave_mode(gpio);
                gpio_disable_pulls(gpio);
                pwm_set_chan_level(pwm_gpio_to_slice_num(gpio), pwm_gpio_to_channel(gpio), 0);
                gpio_set_function(gpio, GPIO_FUNC_PWM);
                modes[gpio] = GPIO_MODE_PWM;
            }
            break;

        case PIN_MODE_ANALOG:
            leave_mode(gpio);
            adc_gpio_init(gpio);
            modes[gpio] = GPIO_MODE_ANALOG;
            break;
    }

    return NULL;
}

const char* gpio_ctrl_write(uint32_t mask, uint32_t levels)
{
    for(uint8_t gpio = 0; gpio < 32; gpio++)
    {
        if(!(mask & (1u << gpio)))
            continue;

        const char* error = gpio_ctrl_check(gpio, PIN_CAN_DOUT);
        if(error)
            return error;
    }

    //The pins that are outputs already change together with one write
    gpio_put_masked(mask, levels);

    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
    {
        if(!(mask & (1u << gpio)) || modes[gpio] == GPIO_MODE_OUTPUT)
            continue;

        leave_mode(gpio);
        gpio_disable_pulls(gpio);
        gpio_put(gpio, (levels >> gpio) & 1);
        gpio_set_dir(gpio, GPIO_OUT);
        gpio_set_function(gpio, GPIO_FUNC_SIO);
        modes[gpio] = GPIO_MODE_OUTPUT;
    }

    return NULL;
}

uint32_t gpio_ctrl_read(uint32_t mask)
{
    return gpio_get_all() & mask;
}

const char* gpio_ctrl_pwm(uint8_t gpio, float frequency, uint16_t duty, double* actual)
{
    const char* error = gpio_ctrl_check(gpio, PIN_CAN_PWM);
    if(error)
        return error;

    PWM_TIMING timing;
    if(!timing_pwm(clock_get_hz(clk_sys), frequency, &timing))
        return ANSWER_ERR_ARG;

    uint slice = pwm_gpio_to_slice_num(gpio);
    if(slice >= PWM_SLICE_MAX)
        return ANSWER_ERR_PIN;

    //Two pins of a slice share its frequency
    if(slice_shared(slice, gpio) && (sliceDiv16[slice] != timing.div16 || sliceTop[slice] != timing.top))
        return ANSWER_ERR_BUSY;

    if(modes[gpio] != GPIO_MODE_PWM)
    {
        leave_mode(gpio);
        gpio_disable_pulls(gpio);
    }

    if(!slice_shared(slice, gpio))
    {
        pwm_set_enabled(slice, false);
        pwm_set_clkdiv_int_frac4(slice, (uint8_t)(timing.div16 >> 4), (uint8_t)(timing.div16 & 0x0F));
        pwm_set_wrap(slice, timing.top);
        pwm_set_phase_correct(slice, false);
        pwm_set_counter(slice, 0);
        sliceDiv16[slice] = timing.div16;
        sliceTop[slice] = timing.top;
    }

    //Duty 0 stops the output: the pin stays low
    pwm_set_chan_level(slice, pwm_gpio_to_channel(gpio), (uint16_t)timing_pwm_level(duty, timing.top));
    gpio_set_function(gpio, GPIO_FUNC_PWM);
    modes[gpio] = GPIO_MODE_PWM;
    pwm_set_enabled(slice, duty != 0 || slice_shared(slice, gpio));

    *actual = timing.actual;
    return NULL;
}

const char* gpio_ctrl_pulse(uint8_t gpio, uint8_t level, uint32_t widthNs, uint16_t count, uint32_t periodNs)
{
    const char* error = gpio_ctrl_check(gpio, PIN_CAN_DOUT);
    if(error)
        return error;

    if(level > 1)
        return ANSWER_ERR_ARG;

    uint32_t systemClock = clock_get_hz(clk_sys);
    uint32_t highDelay, lowDelay;

    if(!timing_pulse(timing_ns_to_cycles(systemClock, widthNs), timing_ns_to_cycles(systemClock, periodNs), count, &highDelay, &lowDelay))
        return ANSWER_ERR_ARG;

    //One pulse output at a time: a new pulse ends the one before (its pin stays at its idle level)
    pulse_stop();

    if(!aux_pio_claim(&pulsePio, &pulse_program))
        return ANSWER_ERR_BUSY;

    leave_mode(gpio);
    gpio_disable_pulls(gpio);

    PIO pio = pulsePio.pio;
    uint sm = pulsePio.sm;

    pio_sm_config config = pulse_program_get_default_config(pulsePio.offset);
    sm_config_set_set_pins(&config, gpio, 1);
    sm_config_set_clkdiv_int_frac(&config, 1, 0); //Exact to a cycle of the system clock
    pio_sm_init(pio, sm, pulsePio.offset, &config);

    pio_sm_exec(pio, sm, pio_encode_set(pio_pins, 0));
    pio_sm_set_consecutive_pindirs(pio, sm, gpio, 1, true);

    //X = pulses - 1, ISR = delay of the pulse, OSR = delay of the gap
    pio_sm_put(pio, sm, (uint32_t)count - 1);
    pio_sm_exec(pio, sm, pio_encode_pull(false, true));
    pio_sm_exec(pio, sm, pio_encode_mov(pio_x, pio_osr));
    pio_sm_put(pio, sm, highDelay);
    pio_sm_exec(pio, sm, pio_encode_pull(false, true));
    pio_sm_exec(pio, sm, pio_encode_mov(pio_isr, pio_osr));
    pio_sm_put(pio, sm, lowDelay);
    pio_sm_exec(pio, sm, pio_encode_pull(false, true));

    //A low pulse: the output is inverted, so the idle level is high
    gpio_set_outover(gpio, level ? GPIO_OVERRIDE_NORMAL : GPIO_OVERRIDE_INVERT);
    pio_gpio_init(pio, gpio);

    pulsePin = gpio;
    pulseLevel = level;
    modes[gpio] = GPIO_MODE_PULSE;

    pio_sm_set_enabled(pio, sm, true);
    return NULL;
}

//------------------------------------------------------------------ the others

void gpio_ctrl_safe(void)
{
    pulse_stop();

    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
        if(is_driven(modes[gpio]))
            make_input(gpio);
}

uint32_t gpio_ctrl_driven_mask(void)
{
    uint32_t mask = 0;

    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
        if(is_driven(modes[gpio]))
            mask |= 1u << gpio;

    return mask;
}

uint32_t gpio_ctrl_claimed_mask(void)
{
    return gpio_ctrl_driven_mask() | analog_busy_gpio_mask();
}

GPIO_MODE gpio_ctrl_mode(uint8_t gpio)
{
    return gpio < PIN_GPIO_COUNT ? (GPIO_MODE)modes[gpio] : GPIO_MODE_IDLE;
}

void gpio_ctrl_take(uint32_t mask, GPIO_MODE mode)
{
    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
    {
        if(!(mask & (1u << gpio)))
            continue;

        leave_mode(gpio);
        gpio_disable_pulls(gpio);
        modes[gpio] = mode;
    }
}

void gpio_ctrl_release(uint32_t mask)
{
    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
        if(mask & (1u << gpio))
            make_input(gpio);
}

void gpio_ctrl_set_analog(uint32_t mask)
{
    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
    {
        if(!(mask & (1u << gpio)))
            continue;

        leave_mode(gpio);
        adc_gpio_init(gpio);
        modes[gpio] = GPIO_MODE_ANALOG;
    }
}
