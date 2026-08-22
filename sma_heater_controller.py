#!/usr/bin/env python3

"""
SMA Heater Controller v2.0.1
============================

Korjaukset v2.0.0 -> v2.0.1:
- Arduino-portti /dev/ttyACM0
- ALIVE POWER=... -viestit ohitetaan
- HEATER_CONTROLLER_READY ohitetaan komentovastauksissa
- SET_POWER odottaa juuri oikeaa OK POWER=xxxx -vastausta
- vanhat sarjaviestit eivät sotke seuraavaa komentoa
- shutdown SET_POWER 0 odottaa oikeaa kuittausta
"""

import time
import signal
import sys

import requests
import serial


# ============================================================
# ASETUKSET
# ============================================================

SERIAL_PORT = "/dev/ttyACM0"
SERIAL_BAUD = 9600

SMA_API_URL = "http://localhost:8080/api/status"

TARGET_GRID_POWER = -100.0

DEADBAND_LOW = -130.0
DEADBAND_HIGH = -70.0

MAX_POWER = 6000

LOOP_INTERVAL = 1.0
CONTROL_DELAY = 1.0

SERIAL_TIMEOUT = 2.0


# ============================================================
# POWER STEP -LOGIIKKA
# ============================================================

def calculate_step(grid_power):

    # --------------------------------------------------------
    # EXPORT
    # --------------------------------------------------------

    if grid_power < DEADBAND_LOW:

        export_power = abs(
            grid_power - TARGET_GRID_POWER
        )

        if export_power > 500:
            return +300

        if export_power > 250:
            return +200

        if export_power > 100:
            return +100

        if export_power > 50:
            return +50

        return 0

    # --------------------------------------------------------
    # IMPORT
    # --------------------------------------------------------

    if grid_power > DEADBAND_HIGH:

        import_power = abs(
            grid_power - TARGET_GRID_POWER
        )

        if import_power > 500:
            return -300

        if import_power > 250:
            return -200

        if import_power > 100:
            return -100

        if import_power > 50:
            return -50

        return 0

    # --------------------------------------------------------
    # DEADBAND
    # --------------------------------------------------------

    return 0


# ============================================================
# SMA API
# ============================================================

def read_grid_power():

    response = requests.get(
        SMA_API_URL,
        timeout=3
    )

    response.raise_for_status()

    data = response.json()

    summary = data.get(
        "summary",
        {}
    )

    if "grid_power" in summary:

        return float(
            summary["grid_power"]
        )

    energy_meter = data.get(
        "energy_meter",
        {}
    )

    if "grid_power" in energy_meter:

        return float(
            energy_meter["grid_power"]
        )

    raise ValueError(
        "API-vastauksesta puuttuu grid_power"
    )


# ============================================================
# ARDUINO SERIAL
# ============================================================

def read_serial_line(ser, timeout=SERIAL_TIMEOUT):

    deadline = (
        time.monotonic()
        + timeout
    )

    while (
        time.monotonic()
        < deadline
    ):

        try:

            line = ser.readline()

        except Exception:

            return None

        if not line:
            continue

        text = line.decode(
            "utf-8",
            errors="replace"
        ).strip()

        if text:

            return text

    return None


def wait_for_ready(ser):

    deadline = (
        time.monotonic()
        + 5.0
    )

    while (
        time.monotonic()
        < deadline
    ):

        line = read_serial_line(
            ser,
            timeout=0.5
        )

        if line is None:
            continue

        print(
            f"Arduino: {line}"
        )

        if (
            line
            == "HEATER_CONTROLLER_READY"
        ):

            return True

    return False


# ============================================================
# ARDUINO COMMANDS
# ============================================================

def send_power(ser, power):

    power = int(
        max(
            0,
            min(
                MAX_POWER,
                power
            )
        )
    )

    command = (
        f"SET_POWER {power}\n"
    )

    expected = (
        f"OK POWER={power}"
    )

    try:

        # ----------------------------------------------------
        # Lähetetään komento
        # ----------------------------------------------------

        ser.write(
            command.encode("ascii")
        )

        ser.flush()

    except Exception as exc:

        print(
            f"Arduino kirjoitusvirhe: "
            f"{exc}"
        )

        return False


    # --------------------------------------------------------
    # Odotetaan juuri oikeaa vastausta
    #
    # ALIVE POWER=...
    # HEATER_CONTROLLER_READY
    #
    # voidaan saada tässä välissä ja ne ohitetaan.
    # --------------------------------------------------------

    deadline = (
        time.monotonic()
        + SERIAL_TIMEOUT
    )

    while (
        time.monotonic()
        < deadline
    ):

        line = read_serial_line(
            ser,
            timeout=0.3
        )

        if line is None:

            continue


        # ----------------------------------------------------
        # Oikea vastaus
        # ----------------------------------------------------

        if line == expected:

            print(
                f"Arduino: {line}"
            )

            return True


        # ----------------------------------------------------
        # Heartbeat
        # ----------------------------------------------------

        if line.startswith(
            "ALIVE POWER="
        ):

            print(
                f"Arduino heartbeat: {line}"
            )

            continue


        # ----------------------------------------------------
        # READY
        # ----------------------------------------------------

        if (
            line
            == "HEATER_CONTROLLER_READY"
        ):

            print(
                f"Arduino: {line}"
            )

            continue


        # ----------------------------------------------------
        # Vanha OK POWER -vastaus
        # ----------------------------------------------------

        if line.startswith(
            "OK POWER="
        ):

            print(
                f"Arduino vanha vastaus: "
                f"{line}"
            )

            continue


        # ----------------------------------------------------
        # Muu viesti
        # ----------------------------------------------------

        print(
            f"Arduino muu viesti: "
            f"{line}"
        )


    print(
        f"VAROITUS: Arduino ei vahvistanut "
        f"tehoa {power} W."
    )

    return False


# ============================================================
# SIGNAALIT
# ============================================================

controller_running = True


def stop_controller(
    signum=None,
    frame=None
):

    global controller_running

    controller_running = False


signal.signal(
    signal.SIGINT,
    stop_controller
)

signal.signal(
    signal.SIGTERM,
    stop_controller
)


# ============================================================
# MAIN
# ============================================================

def main():

    global controller_running

    print()
    print(
        "SMA Heater Controller v2.0.1"
    )
    print(
        "============================"
    )

    print()
    print(
        f"Yhdistetään Arduinoon: "
        f"{SERIAL_PORT}"
    )


    # --------------------------------------------------------
    # Arduino connection
    # --------------------------------------------------------

    try:

        ser = serial.Serial(
            SERIAL_PORT,
            SERIAL_BAUD,
            timeout=0.2,
            write_timeout=2
        )

    except Exception as exc:

        print()
        print(
            "Arduino-yhteyden avaus "
            f"epäonnistui: {exc}"
        )

        print()
        print(
            "Controller lopetetaan."
        )

        return 1


    print()
    print(
        "Arduino-yhteys valmis."
    )


    # --------------------------------------------------------
    # Arduino reset
    # --------------------------------------------------------

    time.sleep(2.0)


    # --------------------------------------------------------
    # READY
    # --------------------------------------------------------

    ready = wait_for_ready(
        ser
    )

    if not ready:

        print()
        print(
            "VAROITUS: Arduino READY-"
            "viestiä ei vastaanotettu."
        )


    # --------------------------------------------------------
    # Safe initial state
    # --------------------------------------------------------

    print()
    print(
        "Arduino initial: SET_POWER 0"
    )

    if not send_power(
        ser,
        0
    ):

        print(
            "VAROITUS: Arduino ei "
            "vahvistanut käynnistystehoa."
        )


    current_power = 0


    # --------------------------------------------------------
    # Controller info
    # --------------------------------------------------------

    print()
    print(
        "SMA Heater Controller käynnissä."
    )

    print()

    print(
        f"Tavoite grid_power: "
        f"{TARGET_GRID_POWER:.0f} W"
    )

    print(
        f"Deadband: "
        f"{DEADBAND_LOW:.0f} ... "
        f"{DEADBAND_HIGH:.0f} W"
    )

    print()
    print("Power steps:")
    print()

    print(
        "  Export >500 W  : +300 W"
    )

    print(
        "  Export >250 W  : +200 W"
    )

    print(
        "  Export >100 W  : +100 W"
    )

    print(
        "  Export >50 W   :  +50 W"
    )

    print(
        "  Deadband        : ei muutosta"
    )

    print(
        "  Import >500 W  : -300 W"
    )

    print(
        "  Import >250 W  : -200 W"
    )

    print(
        "  Import >100 W  : -100 W"
    )

    print(
        "  Import >50 W   :  -50 W"
    )

    print()

    print(
        f"Max power: "
        f"{MAX_POWER} W"
    )

    print(
        f"Loop interval: "
        f"{LOOP_INTERVAL:.1f} s"
    )

    print(
        f"Control delay: "
        f"{CONTROL_DELAY:.1f} s"
    )

    print()
    print(
        "Lopetus: CTRL-C"
    )

    print()


    # --------------------------------------------------------
    # Control timing
    # --------------------------------------------------------

    next_control_time = (
        time.monotonic()
    )


    # ========================================================
    # MAIN LOOP
    # ========================================================

    try:

        while controller_running:

            loop_start = (
                time.monotonic()
            )


            # ------------------------------------------------
            # SMA
            # ------------------------------------------------

            try:

                grid_power = (
                    read_grid_power()
                )

            except Exception as exc:

                print(
                    f"Controller virhe: "
                    f"{exc}"
                )

                time.sleep(
                    LOOP_INTERVAL
                )

                continue


            new_power = (
                current_power
            )

            step = 0

            reason = "HOLD"

            now = (
                time.monotonic()
            )


            # ------------------------------------------------
            # Control decision
            # ------------------------------------------------

            if (
                now
                >= next_control_time
            ):

                step = (
                    calculate_step(
                        grid_power
                    )
                )


                if step != 0:

                    new_power = (
                        current_power
                        + step
                    )

                    new_power = max(
                        0,
                        min(
                            MAX_POWER,
                            new_power
                        )
                    )

                    actual_step = (
                        new_power
                        - current_power
                    )

                    step = (
                        actual_step
                    )


                    if step > 0:

                        reason = (
                            "EXPORT"
                        )

                    elif step < 0:

                        reason = (
                            "IMPORT"
                        )

                    else:

                        reason = (
                            "LIMIT"
                        )

                else:

                    if (
                        DEADBAND_LOW
                        <= grid_power
                        <= DEADBAND_HIGH
                    ):

                        reason = (
                            "DEADBAND"
                        )

                    else:

                        reason = (
                            "HOLD"
                        )


                next_control_time = (
                    now
                    + CONTROL_DELAY
                )


            # ------------------------------------------------
            # Status
            # ------------------------------------------------

            if step != 0:

                print(
                    f"Grid: "
                    f"{grid_power:7.1f} W | "
                    f"Power: "
                    f"{new_power:4d} W | "
                    f"{step:+4d} W | "
                    f"{reason}"
                )

            else:

                print(
                    f"Grid: "
                    f"{grid_power:7.1f} W | "
                    f"Power: "
                    f"{current_power:4d} W | "
                    f"{reason}"
                )


            # ------------------------------------------------
            # Arduino update
            # ------------------------------------------------

            if (
                new_power
                != current_power
            ):

                if send_power(
                    ser,
                    new_power
                ):

                    current_power = (
                        new_power
                    )

                else:

                    print(
                        "Arduino-ohjaus "
                        "epäonnistui."
                    )


            # ------------------------------------------------
            # Loop timing
            # ------------------------------------------------

            elapsed = (
                time.monotonic()
                - loop_start
            )

            sleep_time = (
                LOOP_INTERVAL
                - elapsed
            )

            if sleep_time > 0:

                time.sleep(
                    sleep_time
                )


    finally:

        # ====================================================
        # SAFE SHUTDOWN
        # ====================================================

        print()
        print(
            "Controller pysäytetään..."
        )

        print()
        print(
            "Arduino: SET_POWER 0"
        )


        try:

            if ser.is_open:

                if not send_power(
                    ser,
                    0
                ):

                    print(
                        "VAROITUS: Arduino ei "
                        "vahvistanut nollausta."
                    )

        except Exception as exc:

            print(
                f"Arduino-nollauksen "
                f"virhe: {exc}"
            )


        try:

            if ser.is_open:

                ser.close()

        except Exception:

            pass


        print()
        print(
            "Arduino-yhteys suljetaan..."
        )

        print()
        print(
            "Controller pysäytetty."
        )


    return 0


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
