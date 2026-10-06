# Development container of openSciLab: the application (Python, Qt), the Pico firmware (Pico SDK,
# ARM GNU toolchain, picotool), the Arduino firmware (PlatformIO) and the bridge app (Android
# command line tools, JDK 17). The versions are those of the CI (.github/workflows/build.yml).
FROM mcr.microsoft.com/devcontainers/base:bookworm

ARG PICO_SDK_VERSION=2.1.1
ARG PICOTOOL_VERSION=2.1.1
ARG ARM_TOOLCHAIN_VERSION=14.2.rel1
ARG ARM_TOOLCHAIN_DIR=14_2_Rel1
ARG ANDROID_CMDLINE_TOOLS=11076708
ARG ANDROID_PLATFORM=34
ARG ANDROID_BUILD_TOOLS=34.0.0

RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git curl xz-utils unzip pkg-config libusb-1.0-0-dev \
        python3 python3-venv python3-pip pipx \
        openjdk-17-jdk-headless \
        libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libdbus-1-3 \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
        libxcb-shape0 libxcb-xinerama0 libxcb-randr0 \
    && rm -rf /var/lib/apt/lists/*

USER vscode
ENV PICO_HOME=/home/vscode/.pico-sdk
ENV PICO_SDK_PATH=${PICO_HOME}/sdk/${PICO_SDK_VERSION} \
    PICO_TOOLCHAIN_PATH=${PICO_HOME}/toolchain/${ARM_TOOLCHAIN_DIR} \
    picotool_DIR=${PICO_HOME}/picotool/${PICOTOOL_VERSION}/picotool

# ARM GNU toolchain (the host architecture of the container: x86_64 or aarch64)
RUN mkdir -p "$PICO_TOOLCHAIN_PATH" \
    && curl -fsSL "https://developer.arm.com/-/media/Files/downloads/gnu/${ARM_TOOLCHAIN_VERSION}/binrel/arm-gnu-toolchain-${ARM_TOOLCHAIN_VERSION}-$(uname -m)-arm-none-eabi.tar.xz" \
        | tar -xJ --strip-components=1 -C "$PICO_TOOLCHAIN_PATH"

# Pico SDK with its submodules (cyw43, lwip for the W boards) and picotool
RUN git clone --depth 1 --branch "$PICO_SDK_VERSION" https://github.com/raspberrypi/pico-sdk "$PICO_SDK_PATH" \
    && git -C "$PICO_SDK_PATH" submodule update --init --depth 1 \
    && git clone --depth 1 --branch "$PICOTOOL_VERSION" https://github.com/raspberrypi/picotool /tmp/picotool \
    && cmake -S /tmp/picotool -B /tmp/picotool/build -G Ninja -DPICO_SDK_PATH="$PICO_SDK_PATH" \
        -DPICOTOOL_FLAT_INSTALL=1 -DCMAKE_INSTALL_PREFIX="${PICO_HOME}/picotool/${PICOTOOL_VERSION}" \
    && cmake --build /tmp/picotool/build && cmake --install /tmp/picotool/build && rm -rf /tmp/picotool

# PlatformIO for the Arduino firmware
RUN pipx install platformio
ENV PATH=/home/vscode/.local/bin:${PATH}

# Android command line tools for the bridge app (its build tools run on x86_64 hosts)
ENV ANDROID_HOME=/home/vscode/android-sdk
RUN mkdir -p "$ANDROID_HOME/cmdline-tools" \
    && curl -fsSL -o /tmp/tools.zip "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS}_latest.zip" \
    && unzip -q /tmp/tools.zip -d "$ANDROID_HOME/cmdline-tools" && mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest" \
    && rm /tmp/tools.zip \
    && yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null \
    && "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" "platform-tools" "platforms;android-${ANDROID_PLATFORM}" "build-tools;${ANDROID_BUILD_TOOLS}" >/dev/null
ENV PATH=${ANDROID_HOME}/platform-tools:${ANDROID_HOME}/cmdline-tools/latest/bin:${PATH}
