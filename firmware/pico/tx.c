/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"
#include "tx.h"
#include "pins.h"
#include "aux_pio.h"
#include "gpio_ctrl.h"
#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "hardware/clocks.h"
#include "tx.pio.h"

#define PIO_CLOCK_DIVIDER_MAX 65536.0f

//I2C: longest wait for the bus per byte (clock stretching, missing pull-ups)
#define I2C_TIMEOUT_PER_BYTE_US 5000u

static const char* check_pins(const uint8_t* pins, const uint8_t* caps, uint8_t count)
{
    for(uint8_t i = 0; i < count; i++)
    {
        const char* error = gpio_ctrl_check(pins[i], caps[i]);
        if(error)
            return error;

        for(uint8_t j = 0; j < i; j++)
            if(pins[j] == pins[i])
                return ANSWER_ERR_PIN;
    }

    return NULL;
}

//The pin ends as an ordinary output at a level
static void rest_output(uint8_t pin, bool level)
{
    gpio_ctrl_release(1u << pin);
    gpio_ctrl_write(1u << pin, level ? 1u << pin : 0);
}

//------------------------------------------------------------------------ UART

const char* tx_uart(uint8_t pin, uint32_t baud, const uint8_t* data, uint32_t length)
{
    static const uint8_t caps[] = { PIN_CAN_DOUT };

    const char* error = check_pins(&pin, caps, 1);
    if(error)
        return error;

    float divider = baud ? (float)clock_get_hz(clk_sys) / (8.0f * (float)baud) : 0.0f;
    if(divider < 1.0f || divider >= PIO_CLOCK_DIVIDER_MAX)
        return ANSWER_ERR_ARG;

    AUX_PIO slot;
    if(!aux_pio_claim(&slot, &tx_uart_program))
        return ANSWER_ERR_BUSY;

    gpio_ctrl_take(1u << pin, GPIO_MODE_TX);

    PIO pio = slot.pio;
    uint sm = slot.sm;

    pio_sm_config config = tx_uart_program_get_default_config(slot.offset);
    sm_config_set_out_shift(&config, true, false, 32);
    sm_config_set_out_pins(&config, pin, 1);
    sm_config_set_sideset_pins(&config, pin);
    sm_config_set_fifo_join(&config, PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv(&config, divider);
    pio_sm_init(pio, sm, slot.offset, &config);

    pio_sm_set_pins_with_mask(pio, sm, 1u << pin, 1u << pin); //Idle high
    pio_sm_set_pindirs_with_mask(pio, sm, 1u << pin, 1u << pin);
    pio_gpio_init(pio, pin);
    pio_sm_set_enabled(pio, sm, true);

    for(uint32_t i = 0; i < length; i++)
        pio_sm_put_blocking(pio, sm, data[i]);

    //Sent once the state machine waits at its PULL again (holding the stop bit), then a stop bit
    uint32_t bitUs = 1000000u / baud + 1;
    uint32_t stall = 1u << (PIO_FDEBUG_TXSTALL_LSB + sm);

    while(!pio_sm_is_tx_fifo_empty(pio, sm))
        tight_loop_contents();

    pio->fdebug = stall;
    absolute_time_t timeout = make_timeout_time_us(12 * bitUs + 1000);
    while(!(pio->fdebug & stall) && !time_reached(timeout))
        tight_loop_contents();

    busy_wait_us(bitUs);

    aux_pio_release(&slot);
    rest_output(pin, true);
    return NULL;
}

//------------------------------------------------------------------------- SPI

const char* tx_spi(uint8_t sck, uint8_t mosi, uint8_t miso, uint8_t cs, uint32_t frequency, uint8_t mode, const uint8_t* data, uint32_t length, uint8_t* received, uint32_t* receivedLength)
{
    uint8_t pins[4] = { sck, mosi };
    uint8_t caps[4] = { PIN_CAN_DOUT, PIN_CAN_DOUT };
    uint8_t count = 2;

    *receivedLength = 0;

    if(mode > 3)
        return ANSWER_ERR_ARG;

    if(miso != 0xFF)
    {
        pins[count] = miso;
        caps[count++] = PIN_CAN_DIN;
    }

    if(cs != 0xFF)
    {
        pins[count] = cs;
        caps[count++] = PIN_CAN_DOUT;
    }

    const char* error = check_pins(pins, caps, count);
    if(error)
        return error;

    //4 cycles per bit
    float divider = frequency ? (float)clock_get_hz(clk_sys) / (4.0f * (float)frequency) : 0.0f;
    if(divider < 1.0f || divider >= PIO_CLOCK_DIVIDER_MAX)
        return ANSWER_ERR_ARG;

    bool cpha = mode & 1;
    bool cpol = (mode & 2) != 0;
    const pio_program_t* program = cpha ? &tx_spi_cpha1_program : &tx_spi_cpha0_program;

    AUX_PIO slot;
    if(!aux_pio_claim(&slot, program))
        return ANSWER_ERR_BUSY;

    uint32_t mask = 0;
    for(uint8_t i = 0; i < count; i++)
        mask |= 1u << pins[i];
    gpio_ctrl_take(mask, GPIO_MODE_TX);

    PIO pio = slot.pio;
    uint sm = slot.sm;

    pio_sm_config config = cpha ? tx_spi_cpha1_program_get_default_config(slot.offset)
                                : tx_spi_cpha0_program_get_default_config(slot.offset);
    sm_config_set_out_pins(&config, mosi, 1);
    sm_config_set_in_pins(&config, miso != 0xFF ? miso : mosi);
    sm_config_set_sideset_pins(&config, sck);
    sm_config_set_out_shift(&config, false, true, 8);   //MSB first
    sm_config_set_in_shift(&config, false, true, 8);
    sm_config_set_clkdiv(&config, divider);
    pio_sm_init(pio, sm, slot.offset, &config);

    uint32_t outputs = (1u << sck) | (1u << mosi);
    pio_sm_set_pins_with_mask(pio, sm, 0, outputs);
    pio_sm_set_pindirs_with_mask(pio, sm, outputs, outputs);

    //Clock polarity 1: the clock idles high
    gpio_set_outover(sck, cpol ? GPIO_OVERRIDE_INVERT : GPIO_OVERRIDE_NORMAL);
    pio_gpio_init(pio, sck);
    pio_gpio_init(pio, mosi);

    if(miso != 0xFF)
    {
        gpio_init(miso);
        gpio_set_dir(miso, GPIO_IN);
    }

    if(cs != 0xFF)
    {
        gpio_init(cs);
        gpio_put(cs, 1);
        gpio_set_dir(cs, GPIO_OUT);
    }

    pio_sm_set_enabled(pio, sm, true);

    if(cs != 0xFF)
    {
        gpio_put(cs, 0);
        busy_wait_us(1);
    }

    //One byte out, one byte in: the RX FIFO never overflows
    for(uint32_t i = 0; i < length; i++)
    {
        pio_sm_put_blocking(pio, sm, (uint32_t)data[i] << 24);
        uint8_t value = (uint8_t)pio_sm_get_blocking(pio, sm);

        if(miso != 0xFF)
            received[i] = value;
    }

    //The last clock edge is over when the byte came in; half a bit before CS goes high
    busy_wait_us(1 + 500000u / frequency);

    if(cs != 0xFF)
        gpio_put(cs, 1);

    aux_pio_release(&slot);
    gpio_set_outover(sck, GPIO_OVERRIDE_NORMAL);

    //Idle levels as ordinary outputs, MISO an input
    rest_output(sck, cpol);
    rest_output(mosi, false);
    if(cs != 0xFF)
        rest_output(cs, true);
    if(miso != 0xFF)
        gpio_ctrl_release(1u << miso);

    if(miso != 0xFF)
        *receivedLength = length;

    return NULL;
}

//------------------------------------------------------------------------- I2C

const char* tx_i2c(uint8_t sda, uint8_t scl, uint8_t address, uint32_t frequency, uint16_t readLength, const uint8_t* data, uint32_t length, uint8_t* received)
{
    uint8_t pins[2] = { sda, scl };
    uint8_t caps[2] = { PIN_CAN_DOUT | PIN_CAN_DIN | PIN_CAN_PULLUP, PIN_CAN_DOUT | PIN_CAN_PULLUP };

    const char* error = check_pins(pins, caps, 2);
    if(error)
        return error;

    //The I2C block of the pins: SDA on GPIO 4n (I2C0) or 4n + 2 (I2C1), SCL one above
    if((sda & 1) || !(scl & 1) || ((sda >> 1) & 1) != ((scl >> 1) & 1))
        return ANSWER_ERR_PIN;

    if(address > 0x7F || frequency == 0 || frequency > 1000000 || readLength > TX_I2C_MAX_READ)
        return ANSWER_ERR_ARG;

    i2c_inst_t* i2c = ((sda >> 1) & 1) ? i2c1 : i2c0;

    gpio_ctrl_take((1u << sda) | (1u << scl), GPIO_MODE_TX);

    i2c_init(i2c, frequency);
    gpio_set_function(sda, GPIO_FUNC_I2C);
    gpio_set_function(scl, GPIO_FUNC_I2C);
    gpio_pull_up(sda);
    gpio_pull_up(scl);

    int result = PICO_OK;
    uint32_t timeout = I2C_TIMEOUT_PER_BYTE_US * (length + readLength + 2);

    if(length > 0)
        result = i2c_write_timeout_us(i2c, address, data, length, readLength > 0, timeout);

    if(result >= 0 && readLength > 0)
        result = i2c_read_timeout_us(i2c, address, received, readLength, false, timeout);

    //Nothing to write or read: the address alone, by reading one byte
    if(length == 0 && readLength == 0)
    {
        uint8_t probe;
        result = i2c_read_timeout_us(i2c, address, &probe, 1, false, timeout);
    }

    i2c_deinit(i2c);

    //Idle bus: inputs with pull-ups
    gpio_ctrl_release((1u << sda) | (1u << scl));
    gpio_ctrl_set_mode(sda, PIN_MODE_PULLUP);
    gpio_ctrl_set_mode(scl, PIN_MODE_PULLUP);

    if(result == PICO_ERROR_TIMEOUT)
        return "ERR:TIMEOUT\n";
    if(result < 0)
        return "ERR:NACK\n";

    return NULL;
}
