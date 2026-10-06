/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"
#include "pattern.h"
#include "pattern_data.h"
#include "timing.h"
#include "pins.h"
#include "aux_pio.h"
#include "gpio_ctrl.h"
#include "hardware/dma.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "pattern.pio.h"
#include <string.h>

//IRQ flag set by the program when the output starts (flag 4 + state machine, "irq 4 rel")
#define STARTED_FLAG(sm) (4u + (sm))

static uint8_t* buffer;
static uint32_t capacity;
static uint32_t loaded;         //Samples in the buffer
static uint8_t loadedWidth = 1; //Bytes per sample of the loaded data

//The running output
static bool active;
static AUX_PIO pioSlot;
static uint16_t instructions[16];
static pio_program_t program;
static uint doneOffset;
static int dmaData = -1;
static int dmaControl = -1;
static const void* volatile readAddress;
static uint32_t pinMask;
static uint32_t length;
static uint32_t passes;
static double actualRate;
static bool started;
static uint64_t startTime;

void pattern_init(uint8_t* memory, uint32_t size)
{
    buffer = memory;
    capacity = size < PATTERN_BUFFER_MAX ? size : PATTERN_BUFFER_MAX;
    capacity &= ~3u;
}

uint32_t pattern_reserved_bytes(void)
{
    return ((uint32_t)loaded * loadedWidth + 3u) & ~3u;
}

uint32_t pattern_max_rate(void)
{
    return clock_get_hz(clk_sys) / TIMING_PATTERN_FAST_CYCLES;
}

const char* pattern_load(uint32_t offset, uint8_t flags, const uint8_t* data, uint32_t dataLength, uint32_t* samples)
{
    if(active)
        return ANSWER_ERR_BUSY;

    uint8_t width = gen_data_width(flags);
    if(width == 0)
        return ANSWER_ERR_ARG;

    //The samples of one pattern have one width
    if(offset > 0 && loaded > 0 && width != loadedWidth)
        return ANSWER_ERR_ARG;

    uint32_t end;
    GEN_DATA_RESULT result = gen_data_load(buffer, capacity, offset, flags, data, dataLength, &end);

    if(result == GEN_DATA_FULL)
        return "ERR:FULL\n";
    if(result != GEN_DATA_OK)
        return ANSWER_ERR_ARG;

    //Offset 0 starts a new pattern
    if(offset == 0 || end > loaded)
        loaded = end;
    loadedWidth = width;

    *samples = loaded;
    return NULL;
}

//Copies a program and patches it for the request
static void build_program(const pio_program_t* source, uint sampleOffset, uint lastOffset, bool waitTrigger, uint8_t bits, bool endless)
{
    memcpy(instructions, source->instructions, source->length * sizeof(uint16_t));
    program = *source;
    program.instructions = instructions;

    #ifdef SUPPORTS_COMPLEX_TRIGGER
    if(waitTrigger)
    {
        instructions[0] = pio_encode_wait_gpio(false, COMPLEX_TRIGGER_IN_PIN);
        instructions[1] = pio_encode_wait_gpio(true, COMPLEX_TRIGGER_IN_PIN);
    }
    else
    #endif
    {
        (void)waitTrigger;
        instructions[0] = pio_encode_nop();
        instructions[1] = pio_encode_nop();
    }

    instructions[sampleOffset] = pio_encode_out(pio_pins, bits);

    if(endless)
        instructions[lastOffset] = pio_encode_jmp(sampleOffset);
}

static void free_dma(void)
{
    if(dmaData >= 0)
        hw_clear_bits(&dma_hw->ch[dmaData].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    if(dmaControl >= 0)
        hw_clear_bits(&dma_hw->ch[dmaControl].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);

    if(dmaControl >= 0)
        dma_channel_abort((uint)dmaControl);
    if(dmaData >= 0)
        dma_channel_abort((uint)dmaData);
    if(dmaControl >= 0)
        dma_channel_abort((uint)dmaControl);

    if(dmaData >= 0)
        dma_channel_unclaim((uint)dmaData);
    if(dmaControl >= 0)
        dma_channel_unclaim((uint)dmaControl);

    dmaData = dmaControl = -1;
}

void pattern_stop(void)
{
    if(!active)
        return;

    pio_sm_set_enabled(pioSlot.pio, pioSlot.sm, false);
    free_dma();
    pio_interrupt_clear(pioSlot.pio, STARTED_FLAG(pioSlot.sm));
    aux_pio_release(&pioSlot);
    gpio_ctrl_release(pinMask);

    active = false;
    pinMask = 0;
}

const char* pattern_start(float rate, uint8_t firstPin, uint8_t pinCount, uint32_t sampleCount, uint32_t passCount, uint8_t flags, uint8_t syncPin, double* actual)
{
    if(pinCount < 1 || pinCount > PATTERN_MAX_PINS || (uint32_t)firstPin + pinCount > PIN_GPIO_COUNT)
        return ANSWER_ERR_PIN;

    if(flags & ~GEN_START_WAIT_TRIGGER)
        return ANSWER_ERR_ARG;

    uint8_t width = gen_data_width_for_pins(pinCount);
    if(loaded == 0 || width != loadedWidth || sampleCount < 1 || sampleCount > loaded)
        return ANSWER_ERR_ARG;

    //The state machine counts all samples in X
    uint64_t total = (uint64_t)sampleCount * passCount;
    if(total > 0x100000000ull)
        return ANSWER_ERR_ARG;

    PATTERN_TIMING timing;
    if(!timing_pattern(clock_get_hz(clk_sys), rate, &timing))
        return ANSWER_ERR_ARG;

    //A new start replaces the running output
    pattern_stop();

    uint32_t mask = ((1u << pinCount) - 1) << firstPin;

    for(uint8_t pin = firstPin; pin < firstPin + pinCount; pin++)
    {
        const char* error = gpio_ctrl_check(pin, PIN_CAN_DOUT);
        if(error)
            return error;
    }

    if(syncPin != 0xFF)
    {
        if(mask & (1u << syncPin))
            return ANSWER_ERR_PIN;

        const char* error = gpio_ctrl_check(syncPin, PIN_CAN_DOUT);
        if(error)
            return error;
    }

    bool waitTrigger = (flags & GEN_START_WAIT_TRIGGER) != 0;

    #ifndef SUPPORTS_COMPLEX_TRIGGER
    if(waitTrigger)
        return ANSWER_ERR_PIN;
    #endif

    uint8_t bits = (uint8_t)(width * 8);
    bool endless = passCount == 0;

    if(timing.fast)
    {
        build_program(&pattern_fast_program, pattern_fast_offset_sample, pattern_fast_offset_last, waitTrigger, bits, endless);
        doneOffset = pattern_fast_offset_done;
    }
    else
    {
        build_program(&pattern_slow_program, pattern_slow_offset_sample, pattern_slow_offset_last, waitTrigger, bits, endless);
        doneOffset = pattern_slow_offset_done;
    }

    if(!aux_pio_claim(&pioSlot, &program))
        return ANSWER_ERR_BUSY;

    dmaData = dma_claim_unused_channel(false);
    dmaControl = dma_claim_unused_channel(false);
    if(dmaData < 0 || dmaControl < 0)
    {
        free_dma();
        aux_pio_release(&pioSlot);
        return ANSWER_ERR_BUSY;
    }

    uint32_t syncMask = syncPin != 0xFF ? 1u << syncPin : 0;
    pinMask = mask | syncMask;
    gpio_ctrl_take(pinMask, GPIO_MODE_PATTERN);

    PIO pio = pioSlot.pio;
    uint sm = pioSlot.sm;

    pio_sm_config config = timing.fast ? pattern_fast_program_get_default_config(pioSlot.offset)
                                       : pattern_slow_program_get_default_config(pioSlot.offset);
    sm_config_set_out_pins(&config, firstPin, pinCount);
    sm_config_set_set_pins(&config, syncMask ? syncPin : 0, syncMask ? 1 : 0);
    sm_config_set_out_shift(&config, true, true, bits);
    sm_config_set_fifo_join(&config, PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv_int_frac(&config, timing.divInt, timing.divFrac);
    pio_sm_init(pio, sm, pioSlot.offset, &config);

    pio_sm_set_pins_with_mask(pio, sm, 0, pinMask);
    pio_sm_set_pindirs_with_mask(pio, sm, pinMask, pinMask);

    //X = samples - 1 (unused when endless), ISR = delay per sample; OSR emptied for the autopull
    pio_sm_put(pio, sm, (uint32_t)(total - 1));
    pio_sm_exec(pio, sm, pio_encode_pull(false, true));
    pio_sm_exec(pio, sm, pio_encode_mov(pio_x, pio_osr));
    if(!timing.fast)
    {
        pio_sm_put(pio, sm, timing.delay);
        pio_sm_exec(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec(pio, sm, pio_encode_mov(pio_isr, pio_osr));
    }
    pio_sm_exec(pio, sm, pio_encode_out(pio_null, 32));

    for(uint8_t pin = 0; pin < PIN_GPIO_COUNT; pin++)
        if(pinMask & (1u << pin))
            pio_gpio_init(pio, pin);

    #ifdef SUPPORTS_COMPLEX_TRIGGER
    if(waitTrigger && !(gpio_ctrl_busy_mask() & (1u << COMPLEX_TRIGGER_IN_PIN)))
    {
        gpio_init(COMPLEX_TRIGGER_IN_PIN);
        gpio_set_dir(COMPLEX_TRIGGER_IN_PIN, GPIO_IN);
    }
    #endif

    //The data channel sends one pass, then the control channel restarts it at the buffer
    readAddress = buffer;
    enum dma_channel_transfer_size size = width == 1 ? DMA_SIZE_8 : (width == 2 ? DMA_SIZE_16 : DMA_SIZE_32);

    dma_channel_config dataConfig = dma_channel_get_default_config((uint)dmaData);
    channel_config_set_transfer_data_size(&dataConfig, size);
    channel_config_set_read_increment(&dataConfig, true);
    channel_config_set_write_increment(&dataConfig, false);
    channel_config_set_dreq(&dataConfig, pio_get_dreq(pio, sm, true));
    channel_config_set_chain_to(&dataConfig, (uint)dmaControl);

    dma_channel_config controlConfig = dma_channel_get_default_config((uint)dmaControl);
    channel_config_set_transfer_data_size(&controlConfig, DMA_SIZE_32);
    channel_config_set_read_increment(&controlConfig, false);
    channel_config_set_write_increment(&controlConfig, false);

    dma_channel_configure((uint)dmaControl, &controlConfig, &dma_hw->ch[dmaData].al3_read_addr_trig, &readAddress, 1, false);
    dma_channel_configure((uint)dmaData, &dataConfig, &pio->txf[sm], buffer, sampleCount, true);

    pio_interrupt_clear(pio, STARTED_FLAG(sm));

    length = sampleCount;
    passes = passCount;
    actualRate = timing.actual;
    started = false;
    startTime = 0;
    active = true;

    pio_sm_set_enabled(pio, sm, true);

    *actual = timing.actual;
    return NULL;
}

void pattern_step(void)
{
    if(active && !started && pio_interrupt_get(pioSlot.pio, STARTED_FLAG(pioSlot.sm)))
    {
        started = true;
        startTime = time_us_64();
    }
}

static bool finished(void)
{
    return passes != 0 && pio_sm_get_pc(pioSlot.pio, pioSlot.sm) == pioSlot.offset + doneOffset;
}

void pattern_status(bool* running, uint32_t* passesDone)
{
    pattern_step();

    *running = false;
    *passesDone = 0;

    if(!active)
        return;

    if(finished())
    {
        *passesDone = passes;
        return;
    }

    *running = true;

    if(started && length > 0)
    {
        //The state machine paces the samples exactly: the passes follow from the time
        double samples = (double)(time_us_64() - startTime) * actualRate / 1e6;
        uint64_t done = (uint64_t)(samples / length);

        if(passes != 0 && done >= passes)
            done = passes - 1;
        if(done > 0xFFFFFFFFu)
            done = 0xFFFFFFFFu;

        *passesDone = (uint32_t)done;
    }
}

bool pattern_active(void)
{
    return active;
}
