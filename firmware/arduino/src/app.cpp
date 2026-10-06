// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

#include "app.h"

#include "../capabilities.h"
#include "board.h"
#include "capture.h"
#include "generator.h"
#include "hal.h"
#include "tx.h"

using namespace proto;

namespace app {

Writer out;
volatile uint16_t adc_latest[FW_ADC_CHANNELS];

static Decoder decoder;
static uint8_t event_sequence = 0;
static uint32_t last_frame_ms = 0;
static uint32_t driven = 0;    // outputs set by commands (WRITE, PIN_MODE, PULSE, TX_*)
static uint32_t pwm_pins = 0;  // pins with a running PWM
static uint32_t dac_pins = 0;  // pins with a DAC value

static struct {
    uint32_t period_us;  // 0: stopped
    uint32_t next_us;
    uint32_t pins;
    uint16_t adc;
} monitor = {0, 0, 0, 0};

static uint8_t adc_turn = 0;  // the channel refreshed next

const uint32_t WATCHDOG_MS = 1000;

void send() { out.send(hal::serial_write); }

void event(uint8_t code) {
    out.begin(T_EVENT, event_sequence++);
    out.u8(code);
}

static const char *error_text(uint8_t code) {
    switch (code) {
        case ERR_UNKNOWN: return FW_STR("unknown command");
        case ERR_ARGUMENTS: return FW_STR("bad arguments");
        case ERR_RESERVED: return FW_STR("the pin is reserved");
        case ERR_BUSY: return FW_STR("busy");
        case ERR_UNSUPPORTED: return FW_STR("not supported by this board");
        case ERR_NACK: return FW_STR("no acknowledge");
        case ERR_OVERFLOW: return FW_STR("overflow");
        default: return FW_STR("");
    }
}

static void send_error(uint8_t sequence, uint8_t command, uint8_t code) {
    out.begin(T_ERROR, sequence);
    out.u8(command);
    out.u8(code);
    out.text_flash(error_text(code));
    send();
}

uint8_t check_pins(uint32_t mask, uint16_t caps) {
    for (uint8_t i = 0; i < 32 && mask; i++) {
        if (!(mask & pin_bit(i))) continue;
        mask &= ~pin_bit(i);
        if (i >= board::PIN_COUNT) return ERR_ARGUMENTS;
        board::PinDef def = board::pin(i);
        if (def.reserved != board::RES_FREE) return ERR_RESERVED;
        if ((def.caps & caps) != caps) return ERR_UNSUPPORTED;
        if (capture::gpio_locked() || ((capture::pins() | gen::pins()) & pin_bit(i))) return ERR_BUSY;
    }
    return 0;
}

static uint8_t check_pin(uint8_t pin, uint16_t caps) {
    if (pin >= board::PIN_COUNT) return ERR_ARGUMENTS;
    return check_pins(pin_bit(pin), caps);
}

// the pin no longer runs PWM or holds a DAC value
static void quiet_pin(uint8_t pin) {
    if (pwm_pins & pin_bit(pin)) hal::pwm_stop(pin);
    if (dac_pins & pin_bit(pin)) hal::dac_stop(pin);
    pwm_pins &= ~pin_bit(pin);
    dac_pins &= ~pin_bit(pin);
}

void driving(uint32_t mask) { driven |= mask; }

void released(uint32_t mask) { driven &= ~mask; }

void safe() {
    gen::stop_all();
    monitor.period_us = 0;
    uint32_t pins = driven | pwm_pins | dac_pins;
    for (uint8_t i = 0; i < board::PIN_COUNT; i++) {
        if (!(pins & pin_bit(i))) continue;
        quiet_pin(i);
        hal::pin_input(i, hal::PULL_NONE);
    }
    driven = 0;
}

static uint16_t adc_in_use() { return monitor.adc | capture::adc_mask(); }

static void refresh_adc_all(uint16_t mask) {
    for (uint8_t channel = 0; channel < FW_ADC_CHANNELS; channel++)
        if (mask & (1u << channel)) adc_latest[channel] = hal::adc_read(channel);
}

static uint8_t check_adc_mask(uint16_t mask) {
    uint8_t count = board::analog_count();
    if (count < 16 && (mask >> count)) return ERR_ARGUMENTS;
    return 0;
}

// --------------------------------------------------------------------------------- commands

static void hello() {
    out.text_flash(FW_STR("board="));
    out.text_flash(board::name());
    out.text_flash(FW_STR(";version=" FIRMWARE_VERSION ";protocol=1;rate="));
    uint32_t rate = board::INFO.buffer_max_rate;
    if (board::INFO.stream_max_rate > rate) rate = board::INFO.stream_max_rate;
    out.decimal(rate);
    out.text_flash(FW_STR(";clock="));
    out.decimal(board::INFO.clock_hz);
    out.text_flash(FW_STR(";buffer="));
    out.decimal(FW_CAPTURE_BYTES);
    out.text_flash(FW_STR(";adc_bits="));
    out.decimal(board::INFO.adc_bits);
    out.text_flash(FW_STR(";dac_bits="));
    out.decimal(board::INFO.dac_bits);
    out.text_flash(FW_STR(";vref_mv="));
    out.decimal(board::INFO.vref_mv);
}

static void caps() {
    const board::Info &info = board::INFO;
    out.text_flash(FW_STR(CAP_GPIO "," CAP_PWM "," CAP_MONITOR));
    uint8_t analog = board::analog_count();
    if (analog) {
        out.text_flash(FW_STR("," CAP_ANALOG));
        out.decimal(analog);
    }
    if (board::dac_pin() != board::NO_CHANNEL) {
        out.text_flash(FW_STR("," CAP_DAC));
        if (FW_ARB_POINTS && info.arb_max_rate) out.text_flash(FW_STR("," CAP_AFG));
    }
    out.text_flash(FW_STR("," CAP_IMMEDIATE_TRIGGER "," CAP_CONTINUOUS_STREAM "," CAP_STREAM "="));
    out.decimal(info.stream_bytes);
    if (board::pins_with(board::PC_CLOCK) | board::pins_with(board::PC_CLOCK_SW))
        out.text_flash(FW_STR("," CAP_STATE_MODE "," CAP_STREAM_STATE));
    out.text_flash(FW_STR("," CAP_PATTERN_GEN));
    out.decimal(info.generator_max_rate);
    out.u8(',');
    uint32_t outputs = board::pins_with(board::PC_DOUT) & ~board::reserved_pins();
    uint8_t count = 0;
    for (; outputs; outputs &= outputs - 1) count++;
    out.decimal(count);
    if (board::pins_with(board::PC_SQUARE)) out.text_flash(FW_STR("," CAP_GEN_SQUARE));
    out.text_flash(FW_STR("," CAP_TX_UART "," CAP_TX_SPI "," CAP_TX_I2C));
}

static void pin_line(uint8_t index) {
    if (index >= board::PIN_COUNT) return;  // empty after the last pin
    board::PinDef def = board::pin(index);
    out.text_flash(FW_STR("PIN:"));
    out.text(def.name);
    out.u8(',');
    static const char names[] FW_FLASH = "DIN\0DOUT\0PULLUP\0PULLDOWN\0PWM\0ADC\0DAC\0CLOCK\0CLOCK_SW\0";
    const char *name = names;
    bool first = true;
    for (uint8_t i = 0; i < 9; i++) {
        if (def.caps & (1u << i)) {
            if (!first) out.u8('/');
            out.text_flash(name);
            first = false;
        }
        while (fw_flash_byte(name)) name++;
        name++;
    }
    out.u8(',');
    // capture channels are numbered without gaps over the free digital inputs (the host maps
    // them to pins by this field; masks stay pin indexes)
    if ((def.caps & board::PC_DIN) && def.reserved == board::RES_FREE) {
        uint8_t channel = 0;
        for (uint8_t i = 0; i < index; i++) {
            board::PinDef other = board::pin(i);
            if ((other.caps & board::PC_DIN) && other.reserved == board::RES_FREE) channel++;
        }
        out.decimal(channel);
    } else {
        out.u8('-');
    }
    out.u8(',');
    out.decimal(board::INFO.logic_mv);
    out.u8(',');
    if (def.analog != board::NO_CHANNEL) out.decimal(def.analog);
    else out.u8('-');
    if (def.reserved != board::RES_FREE) {
        out.text_flash(FW_STR(",reserved: "));
        out.text_flash(board::reserved_text(def.reserved));
    }
}

static uint8_t pin_mode(const uint8_t *d) {
    uint8_t pin = d[0], mode = d[1];
    static const uint16_t needs[] = {board::PC_DIN, board::PC_DIN | board::PC_PULLUP,
                                     board::PC_DIN | board::PC_PULLDOWN, board::PC_DOUT,
                                     board::PC_PWM, 0};
    if (mode > MODE_ANALOG) return ERR_ARGUMENTS;
    uint8_t error = check_pin(pin, needs[mode]);
    if (error) return error;
    if (mode == MODE_ANALOG) {
        board::PinDef def = board::pin(pin);
        if (!(def.caps & (board::PC_ADC | board::PC_DAC))) return ERR_UNSUPPORTED;
    }
    quiet_pin(pin);
    switch (mode) {
        case MODE_INPUT:
        case MODE_ANALOG:
            hal::pin_input(pin, hal::PULL_NONE);
            released(pin_bit(pin));
            break;
        case MODE_INPUT_PULLUP:
            hal::pin_input(pin, hal::PULL_UP);
            released(pin_bit(pin));
            break;
        case MODE_INPUT_PULLDOWN:
            hal::pin_input(pin, hal::PULL_DOWN);
            released(pin_bit(pin));
            break;
        default:  // output, PWM (low until the command PWM)
            hal::pin_output(pin, false);
            driving(pin_bit(pin));
    }
    return 0;
}

static uint8_t write_pins(uint32_t mask, uint32_t levels) {
    uint8_t error = check_pins(mask, board::PC_DOUT);
    if (error) return error;
    for (uint8_t i = 0; i < board::PIN_COUNT; i++) {
        if (!(mask & pin_bit(i))) continue;
        quiet_pin(i);
        hal::pin_output(i, (levels & pin_bit(i)) != 0);
    }
    driving(mask);
    return 0;
}

static uint8_t pwm(uint8_t pin, float frequency, uint16_t duty, float *actual) {
    uint8_t error = check_pin(pin, board::PC_PWM);
    if (error) return error;
    if (!(frequency > 0.0f)) return ERR_ARGUMENTS;
    *actual = 0.0f;
    if (duty == 0) {  // stops: output low
        quiet_pin(pin);
        hal::pin_output(pin, false);
        driving(pin_bit(pin));
        return 0;
    }
    if (dac_pins & pin_bit(pin)) quiet_pin(pin);
    error = hal::pwm_start(pin, frequency, duty, actual);
    if (error) return error;
    pwm_pins |= pin_bit(pin);
    driving(pin_bit(pin));
    return 0;
}

static uint8_t dac(uint8_t pin, uint16_t raw) {
    uint8_t error = check_pin(pin, board::PC_DAC);
    if (error) return error;
    if (board::INFO.dac_bits < 16 && raw >= (1u << board::INFO.dac_bits)) return ERR_ARGUMENTS;
    if (pwm_pins & pin_bit(pin)) quiet_pin(pin);
    error = hal::dac_write(pin, raw);
    if (error) return error;
    dac_pins |= pin_bit(pin);
    driving(pin_bit(pin));
    return 0;
}

static uint8_t pulse(uint8_t pin, uint8_t level, uint32_t width) {
    uint8_t error = check_pin(pin, board::PC_DOUT);
    if (error) return error;
    if (level > 1) return ERR_ARGUMENTS;
    quiet_pin(pin);
    driving(pin_bit(pin));
    hal::pin_output(pin, level);
    hal::delay_us(width);
    hal::pin_output(pin, !level);
    return 0;
}

static uint8_t adc_read(uint16_t mask) {
    uint8_t error = check_adc_mask(mask);
    if (error) return error;
    for (uint8_t channel = 0; channel < 16; channel++)
        if (mask & (1u << channel)) out.u16(hal::adc_read(channel));
    return 0;
}

static uint8_t start_monitor(uint32_t rate_mhz, uint32_t pins, uint16_t adc) {
    if (rate_mhz == 0) {
        monitor.period_us = 0;
        return 0;
    }
    if (rate_mhz > board::INFO.monitor_max_mhz) return ERR_ARGUMENTS;
    if (board::PIN_COUNT < 32 && (pins >> board::PIN_COUNT)) return ERR_ARGUMENTS;
    uint8_t error = check_adc_mask(adc);
    if (error) return error;
    refresh_adc_all(adc);
    monitor.pins = pins;
    monitor.adc = adc;
    monitor.period_us = (uint32_t)(1000000000UL / rate_mhz);
    monitor.next_us = hal::micros();
    return 0;
}

// size of the request data of fixed-size commands (0xFF: variable, checked by the handler)
static uint8_t data_size(uint8_t command) {
    switch (command) {
        case CMD_HELLO: case CMD_CAPS: case CMD_HEARTBEAT: case CMD_SAFE: case CMD_CAPTURE_START:
        case CMD_CAPTURE_ABORT: case CMD_GEN_STOP: case CMD_GEN_STATUS:
            return 0;
        case CMD_PINS: return 1;
        case CMD_PIN_MODE: return 2;
        case CMD_WRITE: return 8;
        case CMD_READ: return 4;
        case CMD_PWM: return 7;
        case CMD_DAC: return 3;
        case CMD_PULSE: return 6;
        case CMD_ADC_READ: return 2;
        case CMD_MONITOR: return 10;
        case CMD_CAPTURE_SETUP: return 30;
        case CMD_GEN_START: return 11;
        case CMD_SQUARE: return 5;
        case CMD_ARB_START: return 7;
        default: return 0xFF;
    }
}

static bool known(uint8_t command) {
    switch (command) {
        case CMD_HELLO: case CMD_PINS: case CMD_CAPS: case CMD_PIN_MODE: case CMD_WRITE: case CMD_READ:
        case CMD_PWM: case CMD_DAC: case CMD_PULSE: case CMD_ADC_READ: case CMD_MONITOR: case CMD_HEARTBEAT:
        case CMD_SAFE: case CMD_CAPTURE_SETUP: case CMD_CAPTURE_START: case CMD_CAPTURE_ABORT:
        case CMD_GEN_LOAD: case CMD_GEN_START: case CMD_GEN_STOP: case CMD_GEN_STATUS: case CMD_SQUARE:
        case CMD_ARB_LOAD: case CMD_ARB_START: case CMD_TX_UART: case CMD_TX_SPI: case CMD_TX_I2C:
            return true;
        default:
            return false;
    }
}

static uint8_t execute(uint8_t command, const uint8_t *d, uint8_t n) {
    float actual = 0.0f;
    uint32_t value = 0;
    uint16_t count = 0;
    uint8_t error = 0;
    switch (command) {
        case CMD_HELLO: hello(); return 0;
        case CMD_PINS: pin_line(d[0]); return 0;
        case CMD_CAPS: caps(); return 0;
        case CMD_PIN_MODE: return pin_mode(d);
        case CMD_WRITE: return write_pins(get_u32(d), get_u32(d + 4));
        case CMD_READ:
            value = get_u32(d);
            if (board::PIN_COUNT < 32 && (value >> board::PIN_COUNT)) return ERR_ARGUMENTS;
            out.u32(hal::read_levels() & value);
            return 0;
        case CMD_PWM:
            error = pwm(d[0], get_f32(d + 1), get_u16(d + 5), &actual);
            if (!error) out.f32(actual);
            return error;
        case CMD_DAC: return dac(d[0], get_u16(d + 1));
        case CMD_PULSE: return pulse(d[0], d[1], get_u32(d + 2));
        case CMD_ADC_READ: return adc_read(get_u16(d));
        case CMD_MONITOR: return start_monitor(get_u32(d), get_u32(d + 4), get_u16(d + 8));
        case CMD_HEARTBEAT: return 0;
        case CMD_SAFE: safe(); return 0;
        case CMD_CAPTURE_SETUP:
            error = capture::setup(d, n, &value);
            if (!error) out.u32(value);
            return error;
        case CMD_CAPTURE_START: return capture::start();
        case CMD_CAPTURE_ABORT: capture::abort(); return 0;
        case CMD_GEN_LOAD:
            error = gen::load(d, n, &count);
            if (!error) out.u16(count);
            return error;
        case CMD_GEN_START:
            error = gen::start(d, n, &actual);
            if (!error) out.f32(actual);
            return error;
        case CMD_GEN_STOP: gen::stop(); return 0;
        case CMD_GEN_STATUS: {
            uint8_t running;
            gen::status(&running, &count);
            out.u8(running);
            out.u16(count);
            return 0;
        }
        case CMD_SQUARE:
            error = gen::square(d[0], get_f32(d + 1), &actual);
            if (!error) out.f32(actual);
            return error;
        case CMD_ARB_LOAD:
            error = gen::arb_load(d, n, &count);
            if (!error) out.u16(count);
            return error;
        case CMD_ARB_START:
            error = gen::arb_start(d, n, &actual);
            if (!error) out.f32(actual);
            return error;
        case CMD_TX_UART: return tx::uart(d, n);
        case CMD_TX_SPI: return tx::spi(d, n, out);
        case CMD_TX_I2C: return tx::i2c(d, n, out);
        default: return ERR_UNKNOWN;
    }
}

static void handle(const Frame &frame) {
    if (frame.type != T_REQUEST) return;  // only requests come from the host
    last_frame_ms = hal::millis();
    uint8_t command = frame.length ? frame.payload[0] : 0;
    const uint8_t *data = frame.payload + 1;
    uint8_t n = frame.length ? (uint8_t)(frame.length - 1) : 0;
    if (!frame.length || !known(command)) {
        send_error(frame.sequence, command, ERR_UNKNOWN);
        return;
    }
    uint8_t size = data_size(command);
    if (size != 0xFF && n != size) {
        send_error(frame.sequence, command, ERR_ARGUMENTS);
        return;
    }
    out.begin(T_ANSWER, frame.sequence);
    out.u8(command);
    uint8_t error = execute(command, data, n);
    // commands that block (PULSE, TX_*) do not starve the watchdog
    last_frame_ms = hal::millis();
    if (error) send_error(frame.sequence, command, error);
    else send();
}

void receive(uint8_t byte) {
    if (decoder.feed(byte)) handle(decoder.frame());
}

// ---------------------------------------------------------------------------------- polling

static void poll_monitor() {
    if (!monitor.period_us) return;
    uint32_t now = hal::micros();
    if ((int32_t)(now - monitor.next_us) < 0) return;
    monitor.next_us += monitor.period_us;
    if ((int32_t)(now - monitor.next_us) >= 0) monitor.next_us = now + monitor.period_us;  // behind: skip
    event(EVENT_STATE);
    out.u32(now);
    out.u32(hal::read_levels() & monitor.pins);
    for (uint8_t channel = 0; channel < FW_ADC_CHANNELS; channel++)
        if (monitor.adc & (1u << channel)) out.u16(adc_latest[channel]);
    send();
}

static void poll_adc() {
    uint16_t mask = adc_in_use();
    if (!mask) return;
    for (uint8_t i = 0; i < FW_ADC_CHANNELS; i++) {
        adc_turn = (uint8_t)((adc_turn + 1) % FW_ADC_CHANNELS);
        if (mask & (1u << adc_turn)) {
            adc_latest[adc_turn] = hal::adc_read(adc_turn);
            return;
        }
    }
}

static void poll_watchdog() {
    if (!driven && !pwm_pins && !dac_pins && !gen::driving()) {
        last_frame_ms = hal::millis();
        return;
    }
    if (hal::millis() - last_frame_ms < WATCHDOG_MS) return;
    safe();
    event(EVENT_WATCHDOG);
    send();
    last_frame_ms = hal::millis();
}

void poll() {
    poll_watchdog();
    gen::poll();
    capture::poll();
    poll_adc();
    poll_monitor();
}

void init() {
    for (uint8_t i = 0; i < FW_ADC_CHANNELS; i++) adc_latest[i] = 0;
    driven = pwm_pins = dac_pins = 0;
    monitor.period_us = 0;
    decoder.reset();
    last_frame_ms = hal::millis();
}

}  // namespace app
