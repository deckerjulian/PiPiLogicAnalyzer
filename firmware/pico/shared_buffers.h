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
#ifdef USE_CYGW_WIFI
    #ifndef __SHARED_BUFFERS__
        #define __SHARED_BUFFERS__
        #include "structs.h"
        #include "event_machine.h"
        #include "hardware/flash.h"

        #if defined (CORE_TYPE_2)
            #define FLASH_SETTINGS_OFFSET ((4096 * 1024) - FLASH_SECTOR_SIZE)
        #else
            #define FLASH_SETTINGS_OFFSET ((2048 * 1024) - FLASH_SECTOR_SIZE)
        #endif

        #define FLASH_SETTINGS_ADDRESS (XIP_BASE + FLASH_SETTINGS_OFFSET)

        volatile extern WIFI_SETTINGS wifiSettings;
        extern EVENT_MACHINE wifiToFrontend;
        extern EVENT_MACHINE frontendToWifi;
    #endif
#endif