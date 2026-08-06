# ============================================================================
# Vera Builder — a general compilation/build service for the Vera stack.
# ============================================================================
# Ships arduino-cli (+ the ESP32 Arduino core), PlatformIO, esptool, mpy-cross
# and a C/C++/CMake/Ninja toolchain, all behind builder_service.py (HTTP API on
# :8080). Vera calls it over vera-net so mesh.firmware.build and the build.*
# capabilities can compile firmware/software without bloating the vera image.
#
# NOTE: the ESP32 core install pulls the xtensa/riscv toolchains (~2 GB), so the
# FIRST `docker compose build vera-builder` is large and slow. It's cached after.
# ============================================================================
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    ARDUINO_DIRECTORIES_DATA=/opt/arduino/data \
    ARDUINO_DIRECTORIES_DOWNLOADS=/opt/arduino/downloads \
    ARDUINO_DIRECTORIES_USER=/opt/arduino/user \
    PLATFORMIO_CORE_DIR=/opt/platformio

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git xz-utils python3 python3-pip python3-venv \
      build-essential cmake ninja-build libffi-dev \
 && rm -rf /var/lib/apt/lists/*

# Build service + embedded toolchains (esptool for merge_bin, PlatformIO for the
# long tail of boards, mpy-cross for MicroPython bytecode).
# esptool is pinned to v4: v5 rewrote the CLI on a newer `click` than PlatformIO
# pins, so an unpinned install lands a broken binary (ImportError on click.
# shell_completion) AND renames merge_bin → merge-bin. Both break _merge_esp,
# which silently downgrades firmware to a non-merged 0x10000 image.
RUN pip3 install --no-cache-dir \
      "fastapi>=0.110" "uvicorn[standard]>=0.29" "pydantic>=2" \
      "esptool<5" platformio mpy-cross

# arduino-cli
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
      | BINDIR=/usr/local/bin sh

# ESP32 Arduino core (pre-installed so firmware builds are ready out of the box).
RUN arduino-cli config init \
      --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json \
 && arduino-cli core update-index \
 && arduino-cli core install esp32:esp32

# Libraries the Vera node sketch needs (everything else is bundled with the core).
# ArduinoJson is pinned to v6 — the sketch uses the v6 API (StaticJsonDocument,
# createNestedArray, containsKey) which v7 deprecates/removes.
RUN arduino-cli lib install "ArduinoJson@6.21.5"

COPY builder_service.py /app/builder_service.py
WORKDIR /app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "builder_service:app", "--host", "0.0.0.0", "--port", "8080"]
