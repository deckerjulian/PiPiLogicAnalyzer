/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"
#include "analog.h"
#include "pins.h"
#include "timing.h"
#include "gpio_ctrl.h"
#include "hardware/adc.h"
#include "hardware/dma.h"
#include "hardware/irq.h"
#include "hardware/sync.h"

//Configuration of command 25
static bool armed;
static uint8_t configMask;
static uint8_t configCount;
static uint16_t configDivInt;
static uint8_t configDivFrac;
static uint32_t configMilliHz;

//Running conversions
static volatile bool converting;        //The ADC runs (cleared when the capture ends)
static bool active;                     //Started and not stopped yet
static uint32_t activeGpios;
static uint16_t* ring;
static uint32_t ringSamples;
static int dmaA = -1;
static int dmaB = -1;
static volatile uint32_t passes;        //Complete passes of the DMA channels over the ring
static bool halted;                     //Conversions and DMA stopped, the ring still valid
static uint64_t haltedWritten;          //Samples written until then

//Last single reading of every input (reported for inputs a running capture does not convert)
static uint16_t lastReading[PIN_ADC_COUNT];

static uint8_t bit_count(uint8_t mask)
{
    uint8_t count = 0;
    for(; mask; mask >>= 1)
        count += mask & 1;
    return count;
}

static uint32_t gpios_of(uint8_t mask)
{
    uint32_t gpios = 0;
    for(uint8_t adc = 0; adc < PIN_ADC_COUNT; adc++)
        if(mask & (1u << adc))
            gpios |= 1u << (PIN_ADC_BASE + adc);
    return gpios;
}

//Rewinds the channel that finished its pass (the other one continues)
static void __not_in_flash_func(analog_dma_handler)(void)
{
    if(dmaA >= 0 && dma_channel_get_irq1_status((uint)dmaA))
    {
        dma_channel_acknowledge_irq1((uint)dmaA);
        dma_channel_set_write_addr((uint)dmaA, ring, false);
        passes++;
    }

    if(dmaB >= 0 && dma_channel_get_irq1_status((uint)dmaB))
    {
        dma_channel_acknowledge_irq1((uint)dmaB);
        dma_channel_set_write_addr((uint)dmaB, ring, false);
        passes++;
    }
}

void analog_init(void)
{
    adc_init();
    irq_add_shared_handler(DMA_IRQ_1, analog_dma_handler, PICO_SHARED_IRQ_HANDLER_DEFAULT_ORDER_PRIORITY);
    irq_set_enabled(DMA_IRQ_1, true);
}

const char* analog_configure(uint8_t mask, uint32_t rate, uint32_t* actualMilliHz)
{
    armed = false;
    *actualMilliHz = 0;

    if(mask == 0 || rate == 0)
        return NULL;

    if(mask & ~((1u << PIN_ADC_COUNT) - 1))
        return ANSWER_ERR_PIN;

    for(uint8_t adc = 0; adc < PIN_ADC_COUNT; adc++)
    {
        if(!(mask & (1u << adc)))
            continue;

        uint8_t gpio = PIN_ADC_BASE + adc;
        const char* error = gpio_ctrl_check(gpio, PIN_CAN_ADC);
        if(error)
            return error;

        //An output would be switched off by the analog input
        if(gpio_ctrl_driven_mask() & (1u << gpio))
            return ANSWER_ERR_BUSY;
    }

    uint8_t count = bit_count(mask);
    uint32_t milliHz = timing_adc(rate, count, &configDivInt, &configDivFrac);
    if(milliHz == 0)
        return ANSWER_ERR_ARG;

    configMask = mask;
    configCount = count;
    configMilliHz = milliHz;
    armed = true;
    *actualMilliHz = milliHz;
    return NULL;
}

bool analog_take(void)
{
    bool was = armed;
    armed = false;
    return was;
}

uint8_t analog_mask(void)
{
    return configMask;
}

uint8_t analog_channels(void)
{
    return configCount;
}

uint32_t analog_rate_milli_hz(void)
{
    return configMilliHz;
}

uint32_t analog_gpio_mask(void)
{
    return gpios_of(configMask);
}

uint32_t analog_busy_gpio_mask(void)
{
    return active ? activeGpios : 0;
}

static void configure_channel(uint channel, uint other)
{
    dma_channel_config config = dma_channel_get_default_config(channel);
    channel_config_set_transfer_data_size(&config, DMA_SIZE_16);
    channel_config_set_read_increment(&config, false);
    channel_config_set_write_increment(&config, true);
    channel_config_set_dreq(&config, DREQ_ADC);
    channel_config_set_chain_to(&config, other);
    dma_channel_set_irq1_enabled(channel, true);
    dma_channel_configure(channel, &config, ring, &adc_hw->fifo, ringSamples, false);
}

bool analog_start(uint16_t* buffer, uint32_t samples)
{
    if(active || configCount == 0 || samples < configCount)
        return false;

    dmaA = dma_claim_unused_channel(false);
    dmaB = dma_claim_unused_channel(false);

    if(dmaA < 0 || dmaB < 0)
    {
        if(dmaA >= 0)
            dma_channel_unclaim((uint)dmaA);
        if(dmaB >= 0)
            dma_channel_unclaim((uint)dmaB);
        dmaA = dmaB = -1;
        return false;
    }

    ring = buffer;
    ringSamples = samples - samples % configCount; //Every channel at a fixed place of a round
    passes = 0;
    activeGpios = gpios_of(configMask);
    gpio_ctrl_set_analog(activeGpios);

    adc_run(false);
    adc_fifo_setup(true, true, 1, false, false);
    adc_fifo_drain();

    configure_channel((uint)dmaA, (uint)dmaB);
    configure_channel((uint)dmaB, (uint)dmaA);
    dma_channel_start((uint)dmaA);

    //Round robin from the lowest input up: the samples are in the order of the mask bits
    uint8_t first = 0;
    while(!(configMask & (1u << first)))
        first++;

    adc_hw->div = ((uint32_t)configDivInt << ADC_DIV_INT_LSB) | ((uint32_t)configDivFrac << ADC_DIV_FRAC_LSB);
    adc_select_input(first);
    adc_set_round_robin(configCount > 1 ? configMask : 0);

    active = true;
    converting = true;
    adc_run(true);
    return true;
}

void __not_in_flash_func(analog_capture_ended)(void)
{
    if(converting)
    {
        hw_clear_bits(&adc_hw->cs, ADC_CS_START_MANY_BITS);
        converting = false;
    }
}

//The conversion in progress and the FIFO reach the ring
static void wait_drained(void)
{
    for(int i = 0; i < 100; i++)
    {
        busy_wait_us(2);
        if(!(adc_hw->cs & ADC_CS_START_MANY_BITS) && (adc_hw->cs & ADC_CS_READY_BITS) && adc_fifo_get_level() == 0)
            break;
    }
}

uint64_t analog_written(bool live)
{
    uint32_t passesSeen;
    uint32_t remaining;

    if(!active)
        return 0;

    if(halted)
        return haltedWritten;

    do
    {
        passesSeen = passes;

        if(dma_channel_is_busy((uint)dmaA))
            remaining = dma_channel_hw_addr((uint)dmaA)->transfer_count & 0x0FFFFFFF;
        else if(dma_channel_is_busy((uint)dmaB))
            remaining = dma_channel_hw_addr((uint)dmaB)->transfer_count & 0x0FFFFFFF;
        else
            remaining = ringSamples;

    } while(passesSeen != passes);

    uint32_t done = ringSamples - remaining;

    //The channel counts a transfer down when it starts it
    if(live && done)
        done--;

    return (uint64_t)passesSeen * ringSamples + done;
}

//Stops the conversions and the DMA channels; the ring and the count stay valid
static void halt(void)
{
    if(halted)
        return;

    analog_capture_ended();
    wait_drained();
    haltedWritten = analog_written(false);

    hw_clear_bits(&dma_hw->ch[dmaA].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    hw_clear_bits(&dma_hw->ch[dmaB].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_abort((uint)dmaA);
    dma_channel_abort((uint)dmaB);
    dma_channel_set_irq1_enabled((uint)dmaA, false);
    dma_channel_set_irq1_enabled((uint)dmaB, false);
    dma_channel_acknowledge_irq1((uint)dmaA);
    dma_channel_acknowledge_irq1((uint)dmaB);
    halted = true;
}

void analog_stop(void)
{
    if(!active)
        return;

    halt();

    dma_channel_unclaim((uint)dmaA);
    dma_channel_unclaim((uint)dmaB);
    dmaA = dmaB = -1;

    adc_set_round_robin(0);
    adc_fifo_setup(false, false, 0, false, false);
    adc_fifo_drain();
    adc_hw->div = 0;

    active = false;
    halted = false;
    activeGpios = 0;
}

bool analog_active(void)
{
    return active;
}

const uint16_t* analog_ring(uint32_t* samples)
{
    *samples = ringSamples;
    return ring;
}

//Newest sample of an input in the ring of the running capture
static bool newest(uint8_t adc, uint16_t* value)
{
    if(!(configMask & (1u << adc)))
        return false;

    //Place of the input in a round
    uint8_t place = bit_count(configMask & ((1u << adc) - 1));
    uint64_t written = analog_written(true);

    if(written <= place)
        return false;

    uint64_t last = written - 1;
    uint64_t index = last - (last % configCount + configCount - place) % configCount;
    *value = ring[index % ringSamples];
    return true;
}

const char* analog_read(uint8_t mask, uint16_t* values, uint8_t* count, bool strict)
{
    *count = 0;

    if(mask & ~pins_adc_mask())
    {
        for(uint8_t adc = 0; adc < 8; adc++)
        {
            const PIN_INFO* info = pins_info(PIN_ADC_BASE + adc);
            if((mask & (1u << adc)) && info && info->reserved && adc < PIN_ADC_COUNT)
                return ANSWER_ERR_RESERVED;
        }
        return ANSWER_ERR_PIN;
    }

    if(!active)
    {
        adc_run(false);
        adc_fifo_setup(false, false, 0, false, false);
        adc_set_round_robin(0);
        adc_hw->div = 0;
    }

    for(uint8_t adc = 0; adc < PIN_ADC_COUNT; adc++)
    {
        if(!(mask & (1u << adc)))
            continue;

        uint16_t value;

        if(active)
        {
            if(!newest(adc, &value))
            {
                if(strict)
                    return ANSWER_ERR_BUSY;
                value = lastReading[adc];
            }
        }
        else
        {
            adc_select_input(adc);
            value = adc_read();
            lastReading[adc] = value;
        }

        values[(*count)++] = value;
    }

    return NULL;
}

void analog_send_capture(uint32_t digitalSamples, uint32_t digitalRate, void (*send)(const uint8_t* data, uint32_t length))
{
    uint32_t header[2] = { 0, configMilliHz };

    if(!active)
    {
        send((const uint8_t*)header, sizeof(header));
        return;
    }

    halt();

    //Complete rounds up to the end of the capture; the oldest round of the ring may be cut
    uint64_t rounds = haltedWritten / configCount;
    uint64_t capacity = ringSamples / configCount;
    uint64_t count = 0;

    if(digitalRate)
        count = ((uint64_t)digitalSamples * configMilliHz + (uint64_t)digitalRate * 1000u - 1) / ((uint64_t)digitalRate * 1000u);

    if(count > rounds)
        count = rounds;
    if(capacity > 0 && count > capacity - 1)
        count = capacity - 1;

    header[0] = (uint32_t)count;
    send((const uint8_t*)header, sizeof(header));

    uint64_t end = rounds * configCount;
    uint32_t total = (uint32_t)(count * configCount);
    uint32_t start = (uint32_t)((end - total) % ringSamples);
    uint32_t first = total < ringSamples - start ? total : ringSamples - start;

    if(first)
        send((const uint8_t*)(ring + start), first * 2);
    if(total > first)
        send((const uint8_t*)ring, (total - first) * 2);

    analog_stop();
}
