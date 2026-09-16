# This file controls the build settings, set your board version
# Current versions: "BOARD_PICO", "BOARD_PICO_W", "BOARD_PICO_W_WIFI", "BOARD_PICO_2",
# "BOARD_PICO_2_W", "BOARD_PICO_2_W_WIFI", "BOARD_ZERO", "BOARD_INTERCEPTOR"
set(BOARD_TYPE "BOARD_PICO_2")

# Set to 1 to enable 200Mhz mode (warning! extreme overclock and overvoltage!)
# Set to 0 to disable turbo mode (default, the overclock has to be enabled deliberately)
# Not available for the Pico W and the Pico 2 W
set(TURBO_MODE 0)

# Uncomment to be able to debug the build
# set(DEBUG_BUILD 1)
