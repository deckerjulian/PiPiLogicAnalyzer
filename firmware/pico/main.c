/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"

#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "hardware/flash.h"
#include "hardware/vreg.h"
#include "pico/multicore.h"
#include "capture.pio.h"
#include "structs.h"
#include "capture.h"
#include "self_test.h"
#include "sequence.h"
#include "capabilities.h"
#include "proto.h"
#include "pins.h"
#include "gpio_ctrl.h"
#include "analog.h"
#include "monitor.h"
#include "pattern.h"
#include "tx.h"
#include "hardware/structs/syscfg.h"
#include "hardware/structs/systick.h"
#include "tusb.h"
#include "pico/unique_id.h"
#include "pico/bootrom.h"

#ifdef WS2812_LED
    #include "ws2812.h"
#endif

#if defined (CYGW_LED) || defined(USE_CYGW_WIFI)

    #include "pico/cyw43_arch.h"

    #ifdef USE_CYGW_WIFI

        #include "event_machine.h"
        #include "shared_buffers.h"
        #include "wifi.h"
        #include "hardware/regs/usb.h"
        #include "hardware/structs/usb.h"

        bool usbDisabled = false;
        bool cywReady = false;
        bool skipWiFiData = false;
        bool dataFromWiFi = false;
        EVENT_FROM_WIFI wifiEventBuffer;

        #define MULTICORE_LOCKOUT_TIMEOUT (uint64_t)10 * 365 * 24 * 60 * 60 * 1000 * 1000

    #endif

#endif

#if defined (GPIO_LED)
    #define INIT_LED() {\
                            gpio_init(LED_IO); \
                            gpio_set_dir(LED_IO, GPIO_OUT); \
                        }
    #define LED_ON() gpio_put(LED_IO, 1)
    #define LED_OFF() gpio_put(LED_IO, 0)
#elif defined (CYGW_LED)

    #define INIT_LED() { }

    #ifdef USE_CYGW_WIFI
        #define LED_ON() {\
        EVENT_FROM_FRONTEND lonEvt;\
        lonEvt.event = LED_ON;\
        event_push(&frontendToWifi, &lonEvt);\
        }

        #define LED_OFF() {\
        EVENT_FROM_FRONTEND loffEvt;\
        loffEvt.event = LED_OFF;\
        event_push(&frontendToWifi, &loffEvt);\
        }
    #else
        #define LED_ON() cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, 1)
        #define LED_OFF() cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, 0)
    #endif

#elif defined (WS2812_LED)
    #define INIT_LED() init_rgb()
    #define LED_ON() send_rgb(0,32,0)
    #define LED_OFF() send_rgb(0,0,32)
#elif defined (NO_LED)
    #define INIT_LED() { }
    #define LED_ON() { }
    #define LED_OFF() { }
#endif

//Frame parser of the requests (proto.c). The longest frame is the trigger sequence (command 10)
//of 8 stages with every byte escaped: 2 + 2 * (1 + 4 + 8 * 28) + 2 = 462 bytes, the parser
//holds PROTO_FRAME_MAX = 512 (the WiFi settings request needs 238).
PROTO_PARSER parser;
//Capture status
bool capturing = false;
//The running capture converts analog channels (command 25 before the request)
bool analogCapture = false;
//Samples and rate of the running capture (the time the analog samples cover)
uint32_t captureRate;

//Stream capture (trigger type 6, USB only): the samples are sent while they are captured, in
//chunks of a 32 bit header and the data, until the host sends any byte. The header's top two
//bits say what follows (00: digital samples, 10: analog samples), the rest is the byte count.
//The end is a header of STREAM_END_STOPPED, or STREAM_END_OVERFLOW when USB was too slow (the
//chunk before may hold overwritten samples then).
#define STREAM_CHUNK_BYTES 4096
#define STREAM_FLUSH_US 20000
//Live state: the samples come with the clock edges, they are sent at once
#define STREAM_STATE_FLUSH_US 1000
#define STREAM_END_STOPPED 0u
#define STREAM_END_OVERFLOW 0xFFFFFFFFu
#define STREAM_CHUNK_ANALOG 0x80000000u
bool streaming = false;
uint64_t streamSent;        //Samples sent
uint64_t streamLastSend;    //time_us_64 of the last chunk
bool streamAnalog;          //The stream has analog channels
uint64_t streamAnalogSent;  //Analog samples sent (all channels)
uint64_t streamAnalogLastSend;

bool blink = false;
uint32_t blinkCount = 0;

//Trigger sequence capture: time of the last check for requests
#define SEQUENCE_CANCEL_CHECK_US 20000
uint32_t sequenceCancelCheck;

//LED while a capture waits or records: off and on, a second each
#define CAPTURE_BLINK_US 1000000
uint64_t captureBlinkTime;
bool captureBlinkOn;

//Watchdog: when outputs are driven and no frame came for WATCHDOG_US, SAFE and "WATCHDOG"
#define WATCHDOG_US 1000000
uint64_t lastFrameTime;
bool lastFrameWiFi;

//Capture request pointer
CAPTURE_REQUEST* req;
//Aligned copy of the last capture request
CAPTURE_REQUEST request;

#ifdef USE_CYGW_WIFI

/// @brief Stores a new WiFi configuration in the flash of the device
/// @param settings Settings to store
void storeSettings(WIFI_SETTINGS* settings)
{
    uint8_t buffer[FLASH_PAGE_SIZE];
    memcpy(buffer, settings, sizeof(WIFI_SETTINGS));
    //multicore_lockout_start_blocking ();
    multicore_lockout_start_timeout_us(MULTICORE_LOCKOUT_TIMEOUT);

    uint32_t intStatus = save_and_disable_interrupts();

    flash_range_erase(FLASH_SETTINGS_OFFSET, FLASH_SECTOR_SIZE);

    for(int buc = 0; buc < 1000; buc++)
    {
        asm("nop");
        asm("nop");
        asm("nop");
        asm("nop");
        asm("nop");
    }

    flash_range_program(FLASH_SETTINGS_OFFSET, buffer, FLASH_PAGE_SIZE);

    for(int buc = 0; buc < 1000; buc++)
    {
        asm("nop");
        asm("nop");
        asm("nop");
        asm("nop");
        asm("nop");
    }

    restore_interrupts(intStatus);

    bool unlocked = false;

    do {
        unlocked = multicore_lockout_end_timeout_us(MULTICORE_LOCKOUT_TIMEOUT);
    } while(!unlocked);

    sleep_ms(500);

}

#endif

#ifdef USE_CYGW_WIFI
void wifi_transfer(unsigned char* data, int len);
#endif

/// @brief Sends a response message to the host application in string mode
/// @param response The message to be sent (null terminated)
/// @param toWiFi If true the message is sent to a WiFi endpoint, else to the USB connection through STDIO
void sendResponse(const char* response, bool toWiFi)
{
    #ifdef USE_CYGW_WIFI
    if(toWiFi) //Split into 32 byte events, a longer response overflowed the event data
        wifi_transfer((unsigned char*)response, strlen(response));
    else
    #endif
        printf("%s", response);
}

/// @brief Transfer a buffer of data through USB using the TinyUSB CDC functions
/// @param data Buffer of data to transfer
/// @param len Length of the buffer
void cdc_transfer(unsigned char* data, int len)
{

    int left = len;
    int pos = 0;

    while(left > 0)
    {
        int avail = (int) tud_cdc_write_available();

        if(avail > left)
            avail = left;

        if(avail)
        {
            int transferred = (int) tud_cdc_write(data + pos, avail);
            tud_task();
            tud_cdc_write_flush();
            
            pos += transferred;
            left -= transferred;
        }
        else
        {
            tud_task();
            tud_cdc_write_flush();
            if (!tud_cdc_connected())
                break;
        }
    }
}

#ifdef USE_CYGW_WIFI
/// @brief Transfer a buffer of data through WiFi
/// @param data Buffer of data to transfer
/// @param len Length of the buffer
void wifi_transfer(unsigned char* data, int len)
{
    EVENT_FROM_FRONTEND evt;
    evt.event = SEND_DATA;

    int pos = 0;
    int filledData;
    while(pos < len)
    {
        filledData = 0;
        while(pos < len && filledData < 32)
        {
            evt.data[filledData] = data[pos];
            pos++;
            filledData++;
        }

        evt.dataLength = filledData;
        event_push(&frontendToWifi, &evt);
    }
}
#endif

/// @brief Sends one self-test result line to the host
/// @param line Result line (null terminated)
/// @param context Pointer to the bool telling whether the host is connected through WiFi
static void selfTestReport(const char* line, void* context)
{
    sendResponse(line, *(bool*)context);
}

/// @brief Sends detailed board and build information, one "INFO:<key>:<value>" line each,
/// terminated by "INFO_END" (command 9)
/// @param toWiFi If true the lines are sent to the WiFi endpoint
static void sendDeviceInfo(bool toWiFi)
{
    char line[96];
    char uniqueId[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];

    sendResponse("INFO:BOARD:"BOARD_NAME"\n", toWiFi);
    sendResponse("INFO:FIRMWARE:"FIRMWARE_VERSION"\n", toWiFi);
    sendResponse("INFO:BUILD_DATE:"__DATE__" "__TIME__"\n", toWiFi);
    sendResponse("INFO:SDK:"PICO_SDK_VERSION_STRING"\n", toWiFi);

    #if defined(CORE_TYPE_2)
        sendResponse("INFO:CHIP:RP2350\n", toWiFi);
    #else
        sendResponse("INFO:CHIP:RP2040\n", toWiFi);
        //rp2040_chip_version() is 1 for B0, 2 for B1 and 3 for B2
        snprintf(line, sizeof(line), "INFO:CHIP_REVISION:B%d\n", (int)rp2040_chip_version() - 1);
        sendResponse(line, toWiFi);
        snprintf(line, sizeof(line), "INFO:ROM_VERSION:%d\n", (int)rp2040_rom_version());
        sendResponse(line, toWiFi);
    #endif

    pico_get_unique_board_id_string(uniqueId, sizeof(uniqueId));
    snprintf(line, sizeof(line), "INFO:UNIQUE_ID:%s\n", uniqueId);
    sendResponse(line, toWiFi);

    snprintf(line, sizeof(line), "INFO:FLASH_SIZE:%d\n", (int)PICO_FLASH_SIZE_BYTES);
    sendResponse(line, toWiFi);

    snprintf(line, sizeof(line), "INFO:CLOCK:%lu\n", (unsigned long)clock_get_hz(clk_sys));
    sendResponse(line, toWiFi);

    #ifdef TURBO_MODE
        sendResponse("INFO:TURBO:1\n", toWiFi);
    #else
        sendResponse("INFO:TURBO:0\n", toWiFi);
    #endif

    #ifdef USE_CYGW_WIFI
        sendResponse("INFO:WIFI:1\n", toWiFi);
    #else
        sendResponse("INFO:WIFI:0\n", toWiFi);
    #endif

    #ifdef SUPPORTS_COMPLEX_TRIGGER
        sendResponse("INFO:PATTERN_TRIGGER:1\n", toWiFi);
    #else
        sendResponse("INFO:PATTERN_TRIGGER:0\n", toWiFi);
    #endif

    sendResponse("INFO_END\n", toWiFi);
}

//Pin map of the capture channels (capture.c)
extern const uint8_t pinMap[];

/// @brief SAFE (command 20, watchdog): every output an input, PWM, pulses, the pattern
/// generator and the monitor stopped
static void safe_all()
{
    monitor_stop();
    pattern_stop();
    gpio_ctrl_safe();
}

static uint8_t bytes_per_sample(uint8_t captureMode)
{
    return captureMode == MODE_8_CHANNEL ? 1 : (captureMode == MODE_16_CHANNEL ? 2 : 4);
}

/// @brief Sets the region of the capture memory for a request: behind the pattern buffer, the
/// analog channels configured by command 25 (if any) behind the digital samples. Starts the
/// analog conversions.
/// @return False if the request cannot be served (CAPTURE_ERROR)
static bool prepare_capture(const CAPTURE_REQUEST* r, bool stream)
{
    uint32_t memorySize;
    uint8_t* memory = GetCaptureMemory(&memorySize);
    uint32_t offset = (pattern_reserved_bytes() + 3u) & ~3u;

    analogCapture = false;

    bool analog = analog_take();

    if(offset >= memorySize)
        return false;

    uint32_t available = memorySize - offset;

    if(!analog)
        return SetCaptureRegion(offset, available);

    //An analog input cannot be a digital channel of the same capture
    uint32_t analogGpios = analog_gpio_mask();
    for(uint8_t i = 0; i < r->channelCount && i < sizeof(r->channels); i++)
        if(r->channels[i] < MAX_CHANNELS && (analogGpios & (1u << pinMap[r->channels[i]])))
            return false;

    uint8_t count = analog_channels();
    uint32_t milliHz = analog_rate_milli_hz();
    uint8_t bps = bytes_per_sample(r->captureMode);
    uint64_t analogBytes;
    uint64_t samples = 0;

    if(stream)
    {
        //The rings in the proportion of the data rates (half each without a digital rate)
        uint64_t analogRate = (uint64_t)milliHz * count * 2;
        uint64_t digitalRate = (uint64_t)r->frequency * bps * 1000u;
        analogBytes = digitalRate ? available * analogRate / (analogRate + digitalRate) : available / 2;

        if(analogBytes < 4096)
            analogBytes = 4096;
        if(analogBytes > available - 4096)
            analogBytes = available - 4096;
    }
    else
    {
        //Bursts have gaps the analog samples could not follow
        if(r->loopCount > 0 || r->frequency == 0)
            return false;

        samples = r->triggerType == 3 ? r->postSamples : (uint64_t)r->preSamples + r->postSamples;

        //Rounds covering the time of the digital samples, two more for the ring
        uint64_t rounds = (samples * milliHz + (uint64_t)r->frequency * 1000u - 1) / ((uint64_t)r->frequency * 1000u) + 2;
        analogBytes = rounds * count * 2;
    }

    analogBytes = (analogBytes + 3u) & ~3ull;
    if(analogBytes >= available)
        return false;

    uint32_t digitalBytes = (available - (uint32_t)analogBytes) & ~3u;

    if(!stream && samples * bps > digitalBytes)
        return false;

    if(!SetCaptureRegion(offset, digitalBytes))
        return false;

    if(!analog_start((uint16_t*)(memory + offset + digitalBytes), (uint32_t)(analogBytes / 2)))
        return false;

    analogCapture = true;
    captureRate = r->frequency;
    return true;
}

/// @brief Stops a capture (0xFF from the host)
static void cancel_capture()
{
    StopCapture();
    analog_stop();
    analogCapture = false;
    capturing = false;
    LED_ON();
}

/// @brief Answers an error line, or OK
static void answer(const char* error, bool toWiFi)
{
    sendResponse(error ? error : ANSWER_OK, toWiFi);
}

/// @brief Formats bytes as "RX:<hex>"
static void answer_received(const uint8_t* data, uint32_t length, bool toWiFi)
{
    static const char digits[] = "0123456789ABCDEF";
    char line[4 + 2 * TX_I2C_MAX_READ + 2];
    uint32_t used = 0;

    memcpy(line, "RX:", 3);
    used = 3;

    for(uint32_t i = 0; i < length && used + 3 < sizeof(line); i++)
    {
        line[used++] = digits[data[i] >> 4];
        line[used++] = digits[data[i] & 0x0F];
    }

    line[used++] = '\n';
    line[used] = 0;
    sendResponse(line, toWiFi);
}

/// @brief Executes one request
/// @param payload Command byte and data
/// @param length Bytes of the payload
/// @param fromWiFi If true the request came from a WiFi connection
static void processFrame(const uint8_t* payload, uint16_t length, bool fromWiFi)
{
    lastFrameTime = time_us_64();
    lastFrameWiFi = fromWiFi;

    if(length == 0)
    {
        sendResponse("ERR_UNKNOWN_MSG\n", fromWiFi);
        return;
    }

    uint8_t command = payload[0];
    const uint8_t* data = payload + 1;
    int payloadLength = length - 1;   //Data behind the command byte
    char line[96];

    //While a buffer capture runs only the pin commands are served
    if(capturing && !proto_allowed_while_capturing(command))
    {
        sendResponse(ANSWER_ERR_BUSY, fromWiFi);
        return;
    }

    switch(command)
    {
        case CMD_ID: //ID request

            if(payloadLength != 0) //Malformed message?
                sendResponse("ERR_UNKNOWN_MSG\n", fromWiFi);
            else
            {
                sendResponse("OPENSCILAB_PICO_"BOARD_NAME"_"FIRMWARE_VERSION"\n", fromWiFi);

                char msg[64];

                sprintf(msg, "FREQ:%d\n", MAX_FREQ);
                sendResponse(msg, fromWiFi);
                sprintf(msg, "BLASTFREQ:%d\n", MAX_BLAST_FREQ);
                sendResponse(msg, fromWiFi);
                sprintf(msg, "BUFFER:%d\n", CAPTURE_BUFFER_SIZE);
                sendResponse(msg, fromWiFi);
                sprintf(msg, "CHANNELS:%d\n", MAX_CHANNELS);
                sendResponse(msg, fromWiFi);
                sprintf(msg, "PROTOCOL:%d\n", FIRMWARE_PROTOCOL);
                sendResponse(msg, fromWiFi);
            }
            break;

        case CMD_CAPTURE: //Capture request
        {
            //Reject requests of a different size (e.g. the 48 byte request of V6_0 hosts)
            //instead of interpreting whatever is left in the buffer
            if(payloadLength != (int)sizeof(CAPTURE_REQUEST))
            {
                analog_take(); //The analog configuration applies to this request
                sendResponse("CAPTURE_ERROR\n", fromWiFi);
                break;
            }

            //Copy the request, the multi-byte fields are not aligned inside the buffer
            memcpy(&request, data, sizeof(CAPTURE_REQUEST));
            req = &request;

            bool started = false;

            //Simulated capture: test signals generated by the firmware, no pin is sampled.
            //The pattern is transmitted in triggerValue.
            bool simulated = req->triggerType == 4;

            if(req->triggerType == 6) //Stream capture, answered with the bit of every channel
            {
                //triggerValue 1: a test counter instead of the inputs (checks the transfer)
                if(fromWiFi || !prepare_capture(req, true))
                {
                    analog_stop();
                    sendResponse("CAPTURE_ERROR\n", fromWiFi);
                    break;
                }

                streamAnalog = analogCapture;

                if(!StartCaptureStream(req->frequency, (uint8_t*)&req->channels, req->channelCount, req->captureMode, req->triggerValue == 1))
                {
                    analog_stop();
                    analogCapture = streamAnalog = false;
                    sendResponse("CAPTURE_ERROR\n", fromWiFi);
                    break;
                }

                char bitsLine[128] = "STREAM_STARTED:";
                size_t bitsLength = strlen(bitsLine);
                GetStreamSampleBits(bitsLine + bitsLength, sizeof(bitsLine) - bitsLength - 1);
                strcat(bitsLine, "\n");
                sendResponse(bitsLine, fromWiFi);

                streamSent = 0;
                streamLastSend = time_us_64();
                streamAnalogSent = 0;
                streamAnalogLastSend = streamLastSend;
                streaming = true;
                break;
            }

            if(!prepare_capture(req, false))
            {
                analog_stop();
                analogCapture = false;
                sendResponse("CAPTURE_ERROR\n", fromWiFi);
                break;
            }

            if(req->triggerType == 7) //Trigger sequence or state mode, configured by command 10
            {
                if(req->loopCount == 0 && req->measure == 0)
                    started = StartCaptureSequence(req->frequency, req->preSamples, req->postSamples, (uint8_t*)&req->channels, req->channelCount, req->captureMode);

                if(started)
                {
                    sendResponse("CAPTURE_STARTED\n", fromWiFi);
                    capturing = true;
                    sequenceCancelCheck = time_us_32();
                    LED_OFF();
                }
                else
                {
                    analog_stop();
                    analogCapture = false;
                    sendResponse("CAPTURE_ERROR\n", fromWiFi);
                }

                break;
            }

            if(simulated && req->loopCount == 0)
                started = StartCaptureSimulation(req->frequency, req->preSamples, req->postSamples, (uint8_t*)&req->channels, req->channelCount, (uint8_t)req->triggerValue, req->captureMode);

            #ifdef SUPPORTS_COMPLEX_TRIGGER

                if(req->triggerType == 1) //Start complex trigger capture
                    started = StartCaptureComplex(req->frequency, req->preSamples, req->postSamples, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->count, req->triggerValue, req->captureMode);
                else if(req->triggerType == 2) //start fast trigger capture
                    started = StartCaptureFast(req->frequency, req->preSamples, req->postSamples, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->count, req->triggerValue, req->captureMode);
                else if(req->triggerType == 5) //Edge trigger that also drives the trigger output (multi device sets)
                    started = StartCaptureEdgeOut(req->frequency, req->preSamples, req->postSamples, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->inverted, req->captureMode);
                else if(req->triggerType == 3)
                    started = StartCaptureBlast(req->frequency, req->postSamples, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->inverted, req->captureMode);
                else if(req->triggerType == 0) //Start simple trigger capture (unknown types are an error)
                    started = StartCaptureSimple(req->frequency, req->preSamples, req->postSamples, req->loopCount, req->measure, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->inverted, req->captureMode);

            #else

                if(req->triggerType == 3)
                    started = StartCaptureBlast(req->frequency, req->postSamples, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->inverted, req->captureMode);
                else if(req->triggerType == 0) //Start simple trigger capture (unknown types are an error)
                    started = StartCaptureSimple(req->frequency, req->preSamples, req->postSamples, req->loopCount, req->measure, (uint8_t*)&req->channels, req->channelCount, req->trigger, req->inverted, req->captureMode);

            #endif

            if(started) //If started successfully inform to the host
            {
                sendResponse("CAPTURE_STARTED\n", fromWiFi);
                capturing = true;
                captureBlinkTime = time_us_64();
                captureBlinkOn = true;
            }
            else
            {
                analog_stop();
                analogCapture = false;
                sendResponse("CAPTURE_ERROR\n", fromWiFi); //Else notify the error
            }

            break;
        }

        #ifdef USE_CYGW_WIFI

        case CMD_WIFI_SETTINGS: //Update WiFi settings
        {
            if(payloadLength != (int)sizeof(WIFI_SETTINGS_REQUEST))
            {
                sendResponse("ERR_UNKNOWN_MSG\n", fromWiFi);
                break;
            }

            WIFI_SETTINGS_REQUEST wifiRequest;
            memcpy(&wifiRequest, data, sizeof(WIFI_SETTINGS_REQUEST));
            WIFI_SETTINGS_REQUEST* wReq = &wifiRequest;
            WIFI_SETTINGS settings;
            settings.checksum = 0;
            memcpy(settings.apName, wReq->apName, 33);
            memcpy(settings.passwd, wReq->passwd, 64);
            memcpy(settings.ipAddress, wReq->ipAddress, 16);
            //The WiFi core uses them as C strings, always terminate them
            settings.apName[32] = 0;
            settings.passwd[63] = 0;
            settings.ipAddress[15] = 0;
            settings.port = wReq->port;

            for(int buc = 0; buc < 33; buc++)
                settings.checksum += settings.apName[buc];

            for(int buc = 0; buc < 64; buc++)
                settings.checksum += settings.passwd[buc];

            for(int buc = 0; buc < 16; buc++)
                settings.checksum += settings.ipAddress[buc];

            settings.checksum += settings.port;

            settings.checksum += 0x0f0f;

            storeSettings(&settings);

            wifiSettings = settings;

            EVENT_FROM_FRONTEND evt;
            evt.event = CONFIG_RECEIVED;
            event_push(&frontendToWifi, &evt);

            sendResponse("SETTINGS_SAVED\n", fromWiFi);

            break;
        }

        case CMD_VOLTAGE: //Read power status

            if(!fromWiFi)
                sendResponse("ERR_UNSUPPORTED\n", fromWiFi);
            else
            {
                EVENT_FROM_FRONTEND powerEvent;
                powerEvent.event = GET_POWER_STATUS;
                event_push(&frontendToWifi, &powerEvent);
            }

            break;

        #else

        case CMD_WIFI_SETTINGS:
        case CMD_VOLTAGE:

            sendResponse("ERR_UNSUPPORTED\n", fromWiFi);
            break;

        #endif

        case CMD_BOOTLOADER:

            safe_all();
            sendResponse("RESTARTING_BOOTLOADER\n", fromWiFi);
            sleep_ms(1000);
            reset_usb_boot(0, 0);
            break;

        case CMD_BLINK_ON:

            blink = true;
            blinkCount = 0;
            sendResponse("BLINKON\n", fromWiFi);
            break;

        case CMD_BLINK_OFF:

            blink = false;
            blinkCount = 0;
            sendResponse("BLINKOFF\n", fromWiFi);
            LED_ON();
            break;

        case CMD_SELF_TEST: //Board self-test (no signal may be connected)
        {
            //The test drives the trigger pins and pulls the channels: no outputs, the whole
            //capture memory behind the pattern
            uint32_t memorySize;
            GetCaptureMemory(&memorySize);
            uint32_t offset = (pattern_reserved_bytes() + 3u) & ~3u;

            safe_all();
            SetCaptureRegion(offset, memorySize - offset);
            RunSelfTest(selfTestReport, &fromWiFi);
            break;
        }

        case 8: //Functions of this board (they depend on the board and the connection)
        {
            //The strings are those of capabilities.h (docs/protocols.md)
            char capsLine[512] = "CAPS:" CAP_SELFTEST "," CAP_SIMULATION "," CAP_DEVICEINFO "," CAP_STREAM "=800000";
            size_t capsLength;

            #ifdef SUPPORTS_COMPLEX_TRIGGER

                //EDGE_TRIGGER_OUT: trigger type 5, PATTERN_GROUPS: channels a pattern trigger can cover
                strcat(capsLine, "," CAP_EDGE_TRIGGER_OUT "," CAP_PATTERN_GROUPS);
                capsLength = strlen(capsLine);
                GetPatternTriggerGroups(capsLine + capsLength, 64);

            #endif

            //Trigger sequences and state mode (trigger type 7, command 10)
            capsLength = strlen(capsLine);
            snprintf(capsLine + capsLength, sizeof(capsLine) - capsLength,
                "," CAP_TRIGGER_SEQUENCE "=%d," CAP_TRIGGER_CONDITIONS "pattern/edge/pulse/gap," CAP_SEQUENCE_MAX_RATE "%lu,"
                CAP_STATE_MODE "," CAP_STATE_MAX_CLOCK "%lu",
                SEQ_MAX_STAGES, (unsigned long)GetSequenceMaxRate(NULL), (unsigned long)GetStateMaxClock());

            //Protocol 8: pins, monitor, analog channels, generator, transmitters, live state
            capsLength = strlen(capsLine);
            snprintf(capsLine + capsLength, sizeof(capsLine) - capsLength,
                "," CAP_STREAM_STATE "," CAP_GPIO "," CAP_PWM "," CAP_MONITOR "," CAP_ANALOG "%u,"
                CAP_PATTERN_GEN "%lu,%u," CAP_GEN_SQUARE "," CAP_TX_UART "," CAP_TX_SPI "," CAP_TX_I2C "\n",
                (unsigned)pins_adc_count(), (unsigned long)pattern_max_rate(), (unsigned)PATTERN_MAX_PINS);
            sendResponse(capsLine, fromWiFi);
            break;
        }

        case CMD_DEVICE_INFO: //Detailed board and build information

            sendDeviceInfo(fromWiFi);
            break;

        case CMD_SEQUENCE: //Trigger sequence and state mode of the next capture request with trigger type 7 or stream

            if(SetCaptureSequence(data, payloadLength > 0 ? (uint32_t)payloadLength : 0))
                sendResponse("SEQUENCE_OK\n", fromWiFi);
            else
                sendResponse("SEQUENCE_ERROR\n", fromWiFi);
            break;

        case CMD_PINS: //Pin table
        {
            char pinLine[160];
            snprintf(pinLine, sizeof(pinLine), "PINS:%u\n", (unsigned)pins_count());
            sendResponse(pinLine, fromWiFi);

            for(uint8_t index = 0; index < pins_count(); index++)
            {
                int used = pins_format(index, pinLine, sizeof(pinLine) - 1);
                if(used < 0 || used >= (int)sizeof(pinLine) - 1)
                    used = (int)strlen(pinLine);
                pinLine[used] = '\n';
                pinLine[used + 1] = 0;
                sendResponse(pinLine, fromWiFi);
            }
            break;
        }

        case CMD_PIN_MODE: //<BB pin, mode

            answer(payloadLength == 2 ? gpio_ctrl_set_mode(data[0], data[1]) : ANSWER_ERR_ARG, fromWiFi);
            break;

        case CMD_WRITE: //<II mask, levels

            answer(payloadLength == 8 ? gpio_ctrl_write(proto_u32(data), proto_u32(data + 4)) : ANSWER_ERR_ARG, fromWiFi);
            break;

        case CMD_READ: //<I mask

            if(payloadLength != 4)
                answer(ANSWER_ERR_ARG, fromWiFi);
            else
            {
                snprintf(line, sizeof(line), "LEVELS:%lX\n", (unsigned long)gpio_ctrl_read(proto_u32(data)));
                sendResponse(line, fromWiFi);
            }
            break;

        case CMD_PWM: //<BfH pin, frequency, duty
        {
            double actual = 0;
            const char* error = payloadLength == 7 ? gpio_ctrl_pwm(data[0], proto_f32(data + 1), proto_u16(data + 5), &actual) : ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
            {
                snprintf(line, sizeof(line), "PWM:%.3f\n", actual);
                sendResponse(line, fromWiFi);
            }
            break;
        }

        case CMD_PULSE: //<BBIHI pin, level, width ns, count, period ns

            answer(payloadLength == 12 ? gpio_ctrl_pulse(data[0], data[1], proto_u32(data + 2), proto_u16(data + 6), proto_u32(data + 8)) : ANSWER_ERR_ARG, fromWiFi);
            break;

        case CMD_MONITOR: //<IIB rate mHz, GPIO mask, ADC mask

            answer(payloadLength == 9 ? monitor_start(proto_u32(data), proto_u32(data + 4), data[8], fromWiFi) : ANSWER_ERR_ARG, fromWiFi);
            break;

        case CMD_HEARTBEAT: //Keeps the watchdog satisfied, no answer

            break;

        case CMD_ADC_READ: //<B ADC mask
        {
            uint16_t values[PIN_ADC_COUNT];
            uint8_t count = 0;
            const char* error = payloadLength == 1 ? analog_read(data[0], values, &count, true) : ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
            {
                int used = snprintf(line, sizeof(line), "ADC:");
                for(uint8_t i = 0; i < count; i++)
                    used += snprintf(line + used, sizeof(line) - used, i ? ",%u" : "%u", (unsigned)values[i]);
                snprintf(line + used, sizeof(line) - used, "\n");
                sendResponse(line, fromWiFi);
            }
            break;
        }

        case CMD_SAFE:

            safe_all();
            answer(NULL, fromWiFi);
            break;

        case CMD_GEN_LOAD: //<IB offset, flags, then the data
        {
            uint32_t samples = 0;
            const char* error = payloadLength >= 5 ? pattern_load(proto_u32(data), data[4], data + 5, (uint32_t)payloadLength - 5, &samples) : ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
            {
                snprintf(line, sizeof(line), "GEN_LOADED:%lu\n", (unsigned long)samples);
                sendResponse(line, fromWiFi);
            }
            break;
        }

        case CMD_GEN_START: //<fBBIIBB rate, first pin, pin count, length, passes, flags, sync pin
        {
            double actual = 0;
            const char* error = payloadLength == 16 ?
                pattern_start(proto_f32(data), data[4], data[5], proto_u32(data + 6), proto_u32(data + 10), data[14], data[15], &actual) :
                ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
            {
                snprintf(line, sizeof(line), "GEN_STARTED:%.3f\n", actual);
                sendResponse(line, fromWiFi);
            }
            break;
        }

        case CMD_GEN_STOP:

            pattern_stop();
            answer(NULL, fromWiFi);
            break;

        case CMD_GEN_STATUS:
        {
            bool running;
            uint32_t passes;
            pattern_status(&running, &passes);
            snprintf(line, sizeof(line), "GEN:%d,%lu\n", running ? 1 : 0, (unsigned long)passes);
            sendResponse(line, fromWiFi);
            break;
        }

        case CMD_CAPTURE_ANALOG: //<BI ADC mask, rate per channel
        {
            uint32_t milliHz = 0;
            const char* error = payloadLength == 5 ? analog_configure(data[0], proto_u32(data + 1), &milliHz) : ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
            {
                snprintf(line, sizeof(line), "ANALOG_OK:%lu.%03lu\n", (unsigned long)(milliHz / 1000), (unsigned long)(milliHz % 1000));
                sendResponse(line, fromWiFi);
            }
            break;
        }

        case CMD_TX_UART: //<BI pin, baud, then the bytes

            answer(payloadLength >= 5 ? tx_uart(data[0], proto_u32(data + 1), data + 5, (uint32_t)payloadLength - 5) : ANSWER_ERR_ARG, fromWiFi);
            break;

        case CMD_TX_SPI: //<BBBBIB SCK, MOSI, MISO, CS, frequency, mode, then the bytes
        {
            static uint8_t received[PROTO_FRAME_MAX];
            uint32_t receivedLength = 0;
            const char* error = payloadLength >= 9 ?
                tx_spi(data[0], data[1], data[2], data[3], proto_u32(data + 4), data[8], data + 9, (uint32_t)payloadLength - 9, received, &receivedLength) :
                ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
                answer_received(received, receivedLength, fromWiFi);
            break;
        }

        case CMD_TX_I2C: //<BBBIH SDA, SCL, address, frequency, bytes to read, then the bytes to write
        {
            static uint8_t received[TX_I2C_MAX_READ];
            uint16_t readLength = payloadLength >= 9 ? proto_u16(data + 7) : 0;
            const char* error = payloadLength >= 9 ?
                tx_i2c(data[0], data[1], data[2], proto_u32(data + 3), readLength, data + 9, (uint32_t)payloadLength - 9, received) :
                ANSWER_ERR_ARG;

            if(error)
                answer(error, fromWiFi);
            else
                answer_received(received, readLength, fromWiFi);
            break;
        }

        default:

            sendResponse("ERR_UNKNOWN_MSG\n", fromWiFi); //Unknown message
            break;
    }

    //A long command (a transfer) counts from its end for the watchdog
    lastFrameTime = time_us_64();
}

/// @brief Processes data received from the host application
/// @param data The received data
/// @param length Length of the data
/// @param fromWiFi If true the message comes from a WiFi connection
void processData(uint8_t* data, uint length, bool fromWiFi)
{
    for(uint pos = 0; pos < length; pos++)
    {
        const uint8_t* payload;
        uint16_t payloadLength;

        switch(proto_feed(&parser, data[pos], &payload, &payloadLength))
        {
            case PROTO_FRAME:
                processFrame(payload, payloadLength, fromWiFi);
                break;

            case PROTO_CANCEL: //0xFF outside a frame stops a capture
                if(capturing)
                    cancel_capture();
                break;

            case PROTO_OVERFLOW:
                sendResponse("ERR_MSG_OVERFLOW\n", fromWiFi);
                break;

            default:
                break;
        }
    }

    //PROTOCOL EXPLAINED:
    //
    //The protocol receives binary frames and sends strings terminated by a line end (captures
    //are binary). Each frame starts with 0x55 0xAA and ends with 0xAA 0x55; 0xAA, 0x55 and the
    //escape byte 0xF0 inside are sent as 0xF0 followed by the byte XOR 0xF0 (0xAA -> 0xF0 0x5A).
    //Inside each frame there is a command byte and its data (docs/protocols.md, proto.c).
}

/// @brief Receive and process USB data from the host application
/// @param skipProcessing If true the received data is not processed (used for cleanup)
/// @return True if anything is received, false if not
bool processUSBInput(bool skipProcessing)
{
    //Try to get char
    uint data = getchar_timeout_us(0);

    //Timeout? Then leave
    if(data == PICO_ERROR_TIMEOUT)
        return false;

    if(skipProcessing)
        return true;

    //Everything that is there (a frame at once), a limited amount per call
    for(int count = 0; count < 64 && data != (uint)PICO_ERROR_TIMEOUT; count++)
    {
        uint8_t filteredData = (uint8_t)data;
        processData(&filteredData, 1, false);

        //A request may have started a stream: the bytes behind it stop the stream
        if(streaming)
            break;

        data = getchar_timeout_us(0);
    }

    return true;

}

#ifdef USE_CYGW_WIFI

/// @brief Purges any pending data in the USB input
void purgeUSBData()
{
    while(getchar_timeout_us(0) != PICO_ERROR_TIMEOUT);
}

/// @brief Send a string response with the power status
/// @param status Status received from the WiFi core
void sendPowerStatus(POWER_STATUS* status)
{
    char buffer[32];
    memset(buffer, 0, 32);
    //Room for "_<vbus>\n" and the terminating zero, even for a nonsense voltage
    int len = snprintf(buffer, sizeof(buffer) - 3, "%.2f", status->vsysVoltage);
    if(len < 0)
        len = 0;
    else if(len > (int)sizeof(buffer) - 4)
        len = sizeof(buffer) - 4;
    buffer[len++] = '_';
    buffer[len++] = status->vbusConnected ? '1' : '0';
    buffer[len] = '\n';
    sendResponse(buffer, true);
}

/// @brief Callback for the WiFi event queue
/// @param event Received event
void wifiEvent(void* event)
{
    EVENT_FROM_WIFI* wEvent = (EVENT_FROM_WIFI*)event;

    switch(wEvent->event)
    {
        case CYW_READY:
            cywReady = true;
            break;
        case CONNECTED:
            usbDisabled = true;
            //disableUSB();
            break;
        case DISCONNECTED:
            usbDisabled = false;
            purgeUSBData();
            //enableUSB();
            break;
        case DATA_RECEIVED:
            if(skipWiFiData)
                dataFromWiFi = true;
            else
                processData(wEvent->data, wEvent->dataLength, true);
            break;
        case POWER_STATUS_DATA:
            {
                POWER_STATUS status;
                memcpy(&status, wEvent->data, sizeof(POWER_STATUS));
                sendPowerStatus(&status);
            }
            break;
    }
}

/// @brief Receives and processes input from the host application (when connected through WiFi)
/// @param skipProcessing /// @param skipProcessing If true the received data is not processed (used for cleanup)
/// @return True if anything is received, false if not
bool processWiFiInput(bool skipProcessing)
{
    if(skipProcessing)
    {
        skipWiFiData = true;
        dataFromWiFi = false;
    }

    event_process_queue(&wifiToFrontend, &wifiEventBuffer, 8);

    skipWiFiData = false;    
    
    return dataFromWiFi;
}

#endif

/// @brief Process input data from the host application if it is available
void processInput()
{
    #ifdef USE_CYGW_WIFI
        if(!usbDisabled)
            processUSBInput(false);

        processWiFiInput(false);
    #else
        processUSBInput(false);
    #endif
}

/// @brief Transfer functions for the analog samples of a capture
static void send_usb(const uint8_t* data, uint32_t length)
{
    cdc_transfer((unsigned char*)data, (int)length);
}

#ifdef USE_CYGW_WIFI
static void send_wifi(const uint8_t* data, uint32_t length)
{
    wifi_transfer((unsigned char*)data, (int)length);
}
#endif

/// @brief Ends a stream capture with the end marker
static void end_stream(uint32_t marker)
{
    StopCapture();
    analog_stop();
    cdc_transfer((unsigned char*)&marker, 4);
    streaming = false;
    streamAnalog = false;
    analogCapture = false;
    LED_ON();
}

/// @brief Sends the analog samples of a stream converted since the last call
/// @return False if the stream ended (overflow)
static bool stream_analog_step(uint64_t now)
{
    uint32_t ringSamples;
    const uint16_t* ring = analog_ring(&ringSamples);
    uint8_t channels = analog_channels();

    //Whole rounds only: every chunk starts with the first channel of the mask
    uint64_t written = analog_written(true);
    written -= written % channels;

    uint64_t available = written - streamAnalogSent;
    if(available >= ringSamples)
    {
        end_stream(STREAM_END_OVERFLOW);
        return false;
    }

    uint32_t chunkSamples = STREAM_CHUNK_BYTES / 2;
    chunkSamples -= chunkSamples % channels;

    if(available == 0 || (available < chunkSamples && now - streamAnalogLastSend < STREAM_FLUSH_US))
        return true;

    uint32_t start = (uint32_t)(streamAnalogSent % ringSamples);
    uint32_t count = available < chunkSamples ? (uint32_t)available : chunkSamples;
    if(count > ringSamples - start) //The ring is a multiple of the channels: count stays one too
        count = ringSamples - start;

    uint32_t header = STREAM_CHUNK_ANALOG | (count * 2);
    cdc_transfer((unsigned char*)&header, 4);
    cdc_transfer((unsigned char*)(ring + start), (int)(count * 2));

    if(analog_written(true) - streamAnalogSent >= ringSamples)
    {
        end_stream(STREAM_END_OVERFLOW);
        return false;
    }

    streamAnalogSent += count;
    streamAnalogLastSend = now;
    return true;
}

/// @brief Sends the samples a stream captured since the last call, or ends it
static void stream_step()
{
    uint32_t bufferSamples;
    uint8_t bytesPerSample;
    uint8_t* buffer = GetStreamBuffer(&bufferSamples, &bytesPerSample);

    if(!tud_cdc_connected())
    {
        StopCapture(); //The host is gone
        analog_stop();
        streaming = false;
        streamAnalog = false;
        analogCapture = false;
        LED_ON();
        return;
    }

    if(processUSBInput(true)) //Any byte of the host stops the stream
    {
        end_stream(STREAM_END_STOPPED);
        return;
    }

    uint64_t now = time_us_64();

    if(streamAnalog && !stream_analog_step(now))
        return;

    uint64_t written = StreamWrittenSamples();
    if(written < streamSent) //Read during a DMA hand-over (see StreamWrittenSamples)
        return;

    uint64_t available = written - streamSent;
    if(available >= bufferSamples) //The DMA overtook the samples not sent yet
    {
        end_stream(STREAM_END_OVERFLOW);
        return;
    }

    uint32_t chunkSamples = STREAM_CHUNK_BYTES / bytesPerSample;
    uint32_t flush = IsStreamStateMode() ? STREAM_STATE_FLUSH_US : STREAM_FLUSH_US;

    if(available == 0 || (available < chunkSamples && now - streamLastSend < flush))
    {
        tud_task();
        return;
    }

    uint32_t start = (uint32_t)(streamSent % bufferSamples);
    uint32_t count = available < chunkSamples ? (uint32_t)available : chunkSamples;
    if(count > bufferSamples - start)
        count = bufferSamples - start;

    uint32_t bytes = count * bytesPerSample;
    cdc_transfer((unsigned char*)&bytes, 4);
    cdc_transfer(buffer + start * bytesPerSample, bytes);

    //The samples must still have been in the buffer when the transfer read them
    if(StreamWrittenSamples() - streamSent >= bufferSamples)
    {
        end_stream(STREAM_END_OVERFLOW);
        return;
    }

    streamSent += count;
    streamLastSend = now;
}

/// @brief Sends a completed buffer capture: the line CAPTURE_DATA, the samples, the burst
/// timestamps and the analog samples
static void send_capture()
{
    //Retrieve the capture buffer and get info about it.
    uint32_t length, first;
    CHANNEL_MODE mode;
    uint8_t* buffer = GetBuffer(&length, &first, &mode);
    uint32_t bufferSize = GetCaptureBufferSize();
    uint32_t samples = length;

    uint8_t stampsLength;
    volatile uint32_t* timestamps = GetTimestamps(&stampsLength);

    //Send the data to the host
    uint8_t* lengthPointer = (uint8_t*)&length;
    void (*send)(const uint8_t*, uint32_t) = send_usb;
    bool toWiFi = false;

    #ifdef USE_CYGW_WIFI

        if(usbDisabled)
        {
            send = send_wifi;
            toWiFi = true;
            sleep_ms(2000);
        }
        else
            sleep_ms(100);

    #else
        sleep_ms(100);
    #endif

    //Nothing else comes between this line and the binary data
    sendResponse("CAPTURE_DATA\n", toWiFi);

    //Send capture length
    send(lengthPointer, 4);

    sleep_ms(100);

    //Tanslate sample numbers to byte indexes, makes easier to send data
    switch(mode)
    {
        case MODE_16_CHANNEL:
            length *= 2;
            first *= 2;
            break;
        case MODE_24_CHANNEL:
            length *= 4;
            first *= 4;
            break;
        default:
            break;
    }

    //Send the samples
    if(first + length > bufferSize)
    {
        send(buffer + first, bufferSize - first);
        send(buffer, (first + length) - bufferSize);
    }
    else
        send(buffer + first, length);

    send(&stampsLength, 1);

    if(stampsLength > 1)
        send((const uint8_t*)timestamps, stampsLength * 4);

    //Analog channels of command 25: count per channel, rate in mHz and the samples
    if(analogCapture)
        analog_send_capture(samples, captureRate, send);

    analogCapture = false;
}

/// @brief SAFE when outputs are driven and no frame came for a second (not during captures)
static void watchdog_step()
{
    uint64_t now = time_us_64();

    if(capturing || streaming)
    {
        lastFrameTime = now;
        return;
    }

    if(now - lastFrameTime < WATCHDOG_US)
        return;

    if(gpio_ctrl_driven_mask() != 0 || pattern_active())
    {
        safe_all();
        sendResponse("WATCHDOG\n", lastFrameWiFi);
    }

    lastFrameTime = now;
}

/// @brief Main app loop
/// @return Exit code
int main()
{
    #if defined (TURBO_MODE)

        vreg_disable_voltage_limit();
        vreg_set_voltage(VREG_VOLTAGE_1_30);
        sleep_ms(100);
        
        //Overclock Powerrrr!
        set_sys_clock_khz(400000, true);
    
    #else

        set_sys_clock_khz(200000, true);

    #endif

    //Enable systick using CPU clock
    systick_hw->csr = 0x05;

    pico_unique_board_id_t id;
    pico_get_unique_board_id(&id);

    uint16_t delay = 0;

    for(int buc = 0; buc < PICO_UNIQUE_BOARD_ID_SIZE_BYTES; buc++)
        delay += id.id[buc];

    delay = (delay & 0x3ff) + ((delay & 0xFC00) >> 6);

    sleep_ms(delay);

    //Initialize USB stdio
    stdio_init_all();

    #if defined (BUILD_PICO_W) || defined (BUILD_PICO_2_W)
        cyw43_arch_init();
    #elif defined (BUILD_PICO_W_WIFI) || defined (BUILD_PICO_2_W_WIFI)
        event_machine_init(&wifiToFrontend, wifiEvent, sizeof(EVENT_FROM_WIFI), 8);
            multicore_launch_core1(runWiFiCore);
            while(!cywReady)
                event_process_queue(&wifiToFrontend, &wifiEventBuffer, 1);
    #endif

    //A bit of delay, if the program tries to send data before Windows has identified the device it may crash
    sleep_ms(1000);

    //Speed of the trigger sequence evaluation (SEQUENCE_MAX_RATE)
    InitSequenceCapture();

    //The pattern buffer is the start of the capture memory; the ADC; every pin an input
    {
        uint32_t memorySize;
        uint8_t* memory = GetCaptureMemory(&memorySize);
        pattern_init(memory, memorySize);
    }
    analog_init();

    proto_reset(&parser);
    lastFrameTime = time_us_64();

    //Configure led
    INIT_LED();
    LED_ON();

    while(1)
    {
        if(streaming)
        {
            //A stream takes no commands and sends no reports
            stream_step();
            watchdog_step();
            continue;
        }

        if(capturing)
        {
            #ifdef SUPPORTS_COMPLEX_TRIGGER
            check_fast_interrupt(); //The W boards miss the PIO interrupt of the fast trigger
            #endif

            //Is the PIO units still working?
            if(!IsCapturing())
            {
                send_capture();

                //Done!
                capturing = false;
                LED_ON();
            }
            else
            {
                if(IsSequenceCapture())
                {
                    //Evaluate the trigger sequence, look at the requests now and then
                    SequenceCaptureStep();

                    if(time_us_32() - sequenceCancelCheck >= SEQUENCE_CANCEL_CHECK_US)
                    {
                        sequenceCancelCheck = time_us_32();
                        processInput();
                    }
                }
                else
                {
                    //The LED blinks while the capture waits or records
                    uint64_t now = time_us_64();
                    if(now - captureBlinkTime >= CAPTURE_BLINK_US)
                    {
                        captureBlinkTime = now;
                        captureBlinkOn = !captureBlinkOn;
                        if(captureBlinkOn)
                        {
                            LED_ON();
                        }
                        else
                        {
                            LED_OFF();
                        }
                    }

                    processInput(); //Pin commands, the monitor and 0xFF (stop)
                }
            }
        }
        else
        {
            if(blink)
            {
                if(blinkCount++ == 200000)
                {
                    LED_OFF();
                }
                else if(blinkCount == 400000)
                {
                    LED_ON();
                    blinkCount = 0;
                }
            }

            processInput(); //Read incomming data
        }

        if(!streaming)
            monitor_step(sendResponse);

        pattern_step();
        watchdog_step();
    }

    return 0;
}
