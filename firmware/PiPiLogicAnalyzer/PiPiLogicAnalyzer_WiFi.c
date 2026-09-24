/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of PiPiLogicAnalyzer, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "PiPiLogicAnalyzer_Board_Settings.h"

#ifdef USE_CYGW_WIFI

#include "Event_Machine.h"
#include "Shared_Buffers.h"
#include "PiPiLogicAnalyzer_WiFi.h"
#include "PiPiLogicAnalyzer_Structs.h"
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/cyw43_arch.h"
#include "pico/multicore.h"
#include "hardware/adc.h"
#include "hardware/gpio.h"
#include "hardware/flash.h"
#include "lwip/pbuf.h"
#include "lwip/tcp.h"

EVENT_FROM_FRONTEND frontendEventBuffer;
WIFI_STATE_MACHINE currentState = VALIDATE_SETTINGS;
ip_addr_t address;
struct tcp_pcb* serverPcb;
struct tcp_pcb* clientPcb;

//Data received from the client that did not fit into the event queue yet. It is acknowledged to lwIP
//(tcp_recved) as it is handed over, so the receive window closes while the frontend is busy instead of
//the WiFi core blocking on a full queue: core 0 may itself be blocked sending data to this core.
static struct pbuf* pendingData;
static uint16_t pendingPos;

//Time a client may keep the send buffer full before it is dropped
#define SEND_TIMEOUT_MS 5000

bool apConnected = false;
bool boot = false;

#define LED_ON() cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, 1)
#define LED_OFF() cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, 0)

void getPowerStatus()
{
    EVENT_FROM_WIFI evtPower;
    evtPower.event = POWER_STATUS_DATA;
    evtPower.dataLength = sizeof(POWER_STATUS);
    POWER_STATUS* status = (POWER_STATUS*)&evtPower.data;
    
    adc_init();

    uint32_t oldInt = save_and_disable_interrupts();
    uint32_t old_pad = pads_bank0_hw->io[29];
    uint32_t old_ctrl = io_bank0_hw->io[29].ctrl;

    adc_gpio_init(29);
    adc_select_input(3);

    //Busy wait: interrupts are disabled, sleep_ms relies on the timer interrupt to wake up
    busy_wait_ms(100);

    const float conversion_factor = 3.3f / (1 << 12);
    status->vsysVoltage = adc_read() * conversion_factor * 3;

    gpio_init(29);

    pads_bank0_hw->io[29] = old_pad;
    io_bank0_hw->io[29].ctrl = old_ctrl;
    restore_interrupts(oldInt);

    status->vbusConnected = cyw43_arch_gpio_get(2);
    
    event_push(&wifiToFrontend, &evtPower);

}

void readSettings()
{
    wifiSettings = *((volatile WIFI_SETTINGS*)(FLASH_SETTINGS_ADDRESS));
}

void stopServer()
{
    if(serverPcb == NULL)
        return;

    tcp_close(serverPcb);
    serverPcb = NULL;
}

static void dropPendingData()
{
    if(pendingData != NULL)
    {
        pbuf_free(pendingData);
        pendingData = NULL;
    }
    pendingPos = 0;
}

//Hands the received data over to the frontend as far as the event queue has room
static void flushPendingData()
{
    EVENT_FROM_WIFI evt;
    evt.event = DATA_RECEIVED;

    while(pendingData != NULL && pendingPos < pendingData->tot_len)
    {
        uint16_t left = pendingData->tot_len - pendingPos;
        uint8_t copy = left > 128 ? 128 : left;
        evt.dataLength = copy;
        pbuf_copy_partial(pendingData, evt.data, copy, pendingPos);

        if(!queue_try_add(&wifiToFrontend.queue, &evt))
            return;

        pendingPos += copy;

        if(clientPcb != NULL)
            tcp_recved(clientPcb, copy);
    }

    dropPendingData();
}

static void notifyDisconnected()
{
    EVENT_FROM_WIFI evt;
    evt.event = DISCONNECTED;
    event_push(&wifiToFrontend, &evt);
}

//Returns true if the connection had to be aborted (tcp_close can fail when lwIP is out of memory)
bool killClient()
{
    bool aborted = false;

    dropPendingData();

    if(clientPcb != NULL)
    {
        tcp_recv(clientPcb, NULL);
        tcp_err(clientPcb, NULL);
        if(tcp_close(clientPcb) != ERR_OK)
        {
            tcp_abort(clientPcb);
            aborted = true;
        }
        clientPcb = NULL;
    }
    currentState = WAITING_TCP_CLIENT;

    return aborted;
}

void sendData(uint8_t* data, uint8_t len)
{
    absolute_time_t timeout = make_timeout_time_ms(SEND_TIMEOUT_MS);

    while(clientPcb && tcp_sndbuf(clientPcb) < len)
    {
        //A client that stops reading would otherwise stall this core and, through the full event
        //queue, the capture core as well
        if(time_reached(timeout))
        {
            killClient();
            notifyDisconnected();
            return;
        }

        cyw43_arch_poll();
        flushPendingData();
        sleep_ms(1);
    }

    //The client may have disconnected while waiting for buffer space
    if(clientPcb == NULL)
        return;

    if(tcp_write(clientPcb, data, len, TCP_WRITE_FLAG_COPY))
    {
        killClient();
        notifyDisconnected();
        return;
    }

    //Send now instead of waiting for the next ACK or the TCP timer
    tcp_output(clientPcb);
}

void serverError(void *arg, err_t err)
{
    //lwIP has already freed the PCB when this is called, it must not be touched any more
    clientPcb = NULL;
    dropPendingData();
    currentState = WAITING_TCP_CLIENT;
    notifyDisconnected();
}

err_t serverReceiveData(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err)
{
    //Client disconnected
    if(!p || p->tot_len == 0)
    {
        if(p)
            pbuf_free(p);

        bool aborted = killClient();
        notifyDisconnected();
        //ERR_ABRT tells lwIP that the PCB is gone, which is only true after tcp_abort
        return aborted ? ERR_ABRT : ERR_OK;
    }

    if(pendingData == NULL)
    {
        pendingData = p;
        pendingPos = 0;
    }
    else
        pbuf_cat(pendingData, p);

    flushPendingData();

    return ERR_OK;
}

err_t acceptConnection(void *arg, struct tcp_pcb *client_pcb, err_t err)
{
    if (err != ERR_OK || client_pcb == NULL || clientPcb != NULL || currentState != WAITING_TCP_CLIENT)
        return ERR_VAL;

    clientPcb = client_pcb;

    tcp_recv(clientPcb, serverReceiveData);
    tcp_err(clientPcb, serverError);

    currentState = TCP_CLIENT_CONNECTED;

    EVENT_FROM_WIFI evt;
    evt.event = CONNECTED;
    event_push(&wifiToFrontend, &evt);

    return ERR_OK;
}

bool tryStartServer()
{
    serverPcb = tcp_new_ip_type(IPADDR_TYPE_V4);

    if(serverPcb == NULL)
        return false;

    err_t err = tcp_bind(serverPcb, &address, wifiSettings.port);

    if (err)
    {
        //Free the PCB, the state machine retries and leaked one PCB per attempt
        tcp_close(serverPcb);
        serverPcb = NULL;
        return false;
    }

    //On success the listening PCB replaces (and frees) the bound one, on failure the bound one stays
    struct tcp_pcb* listenPcb = tcp_listen_with_backlog(serverPcb, 1);

    if(listenPcb == NULL)
    {
        tcp_close(serverPcb);
        serverPcb = NULL;
        return false;
    }

    serverPcb = listenPcb;
    tcp_accept(serverPcb, acceptConnection);

    //The original fell off the end of the function, so the return value was undefined
    return true;
}

bool tryConnectAP()
{
    if(cyw43_arch_wifi_connect_timeout_ms((const char*)wifiSettings.apName, (const char*)wifiSettings.passwd, CYW43_AUTH_WPA2_AES_PSK, 10000))
        return false;

    //An invalid stored address keeps the one assigned by DHCP
    if(ipaddr_aton((const char*)wifiSettings.ipAddress, &address))
        netif_set_ipaddr(netif_list, ip_2_ip4(&address));
    else
        ip_addr_copy_from_ip4(address, *netif_ip4_addr(netif_list));

    apConnected = true;

    return true;
}

void disconnectAP()
{
    if(!apConnected)
        return;
        
    cyw43_wifi_leave(&cyw43_state, 0);
    apConnected = false;

}

void processWifiMachine()
{
    switch (currentState)
    {
        case VALIDATE_SETTINGS:
            {
                if(!boot)
                    readSettings();

                boot = true;

                uint16_t checksum = 0;

                for(int buc = 0; buc < 33; buc++)
                    checksum += wifiSettings.apName[buc];

                for(int buc = 0; buc < 64; buc++)
                    checksum += wifiSettings.passwd[buc];

                for(int buc = 0; buc < 16; buc++)
                    checksum += wifiSettings.ipAddress[buc];

                checksum += wifiSettings.port;

                checksum += 0x0f0f;

                if(wifiSettings.checksum == checksum)
                    currentState = CONNECTING_AP;
                else
                    currentState = WAITING_SETTINGS;
            }
            break;

        case CONNECTING_AP:
            if(tryConnectAP())
                currentState = STARTING_TCP_SERVER;
            break;
        case STARTING_TCP_SERVER:
            if(tryStartServer())
                currentState = WAITING_TCP_CLIENT;
            break;
        default:
            break;
    }
}

void frontendEvent(void* event)
{
    EVENT_FROM_FRONTEND* evt = (EVENT_FROM_FRONTEND*)event;
    switch(evt->event)
    {
        case LED_ON:
            LED_ON();
            break;

        case LED_OFF:
            LED_OFF();
            break;
        case CONFIG_RECEIVED:

            killClient();
            stopServer();
            disconnectAP();
            currentState = VALIDATE_SETTINGS;
            break;

        case SEND_DATA:
            sendData(evt->data, evt->dataLength);
            break;
        
        case GET_POWER_STATUS:
            getPowerStatus();
            break;
    }
}

void runWiFiCore()
{
    event_machine_init(&frontendToWifi, frontendEvent, sizeof(EVENT_FROM_FRONTEND), 8);
    multicore_lockout_victim_init();
    cyw43_arch_init();
    cyw43_arch_enable_sta_mode();
    EVENT_FROM_WIFI evtRdy;
    evtRdy.event = CYW_READY;
    event_push(&wifiToFrontend, &evtRdy);

    while(true)
    {
        event_process_queue(&frontendToWifi, &frontendEventBuffer, 8);
        processWifiMachine();
        if(currentState > CONNECTING_AP)
        {
            cyw43_arch_poll();
            flushPendingData();
        }
    }
}

#endif