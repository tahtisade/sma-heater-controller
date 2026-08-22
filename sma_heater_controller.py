#!/usr/bin/env python3

import time
import signal
import sys

import requests
import serial


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = "http://localhost:8080/api/status"

SERIAL_PORT = "/dev/ttyACM1"
SERIAL_BAUD = 9600

TARGET_GRID_POWER = -100.0

DEADBAND_LOW = -130.0
DEADBAND_HIGH = -70.0

MAX_POWER = 6000

LOOP_INTERVAL = 1.0


# ============================================================
# POWER STEP SETTINGS
# ============================================================

EXPORT_STEP_300 = 500
EXPORT_STEP_200 = 250
EXPORT_STEP_100 = 100
EXPORT_STEP_50 = 50

IMPORT_STEP_300 = 500
IMPORT_STEP_200 = 250
IMPORT_STEP_100 = 100
IMPORT_STEP_50 = 50


# ============================================================
# GLOBAL STATE
# ============================================================

arduino = None
running = True


# ============================================================
# SIGNAL HANDLER
# ============================================================

def signal_handler(signum, frame):

    global running

    print()
    print("Controller pysäytetään...")

    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ============================================================
# ARDUINO CONNECTION
# ============================================================

def connect_arduino():

    global arduino

    print()
    print(f"Yhdistetään Arduinoon: {SERIAL_PORT}")
    print()

    try:

        arduino = serial.Serial(
            SERIAL_PORT,
            SERIAL_BAUD,
            timeout=1
        )

        # Arduino UNO voi resetointua kun
        # sarjaportti avataan.
        time.sleep(2)

        print("Arduino-yhteys valmis.")

        # ----------------------------------------------------
        # Luetaan mahdollinen READY-viesti.
        #
        # Tärkeää:
        # HEATER_CONTROLLER_READY ei ole vastaus
        # SET_POWER-komentoon.
        # ----------------------------------------------------

        arduino.reset_input_buffer()

        time.sleep(0.2)

        while arduino.in_waiting:

            line = arduino.readline()

            if not line:
                break

            text = line.decode(
                "utf-8",
                errors="replace"
            ).strip()

            if text:

                print(
                    f"Arduino: {text}"
                )

        return True

    except Exception as e:

        print(
            f"Arduino-yhteyden avaus epäonnistui: {e}"
        )

        arduino = None

        return False


# ============================================================
# ARDUINO COMMAND
# ============================================================

def send_command(command):

    if arduino is None:
        return None

    try:

        # ----------------------------------------------------
        # Tyhjennetään vanha data ennen uuden komennon
        # lähettämistä.
        #
        # Tämä estää READY- tai vanhan OK-viestin
        # sekoittumisen uuteen komentoon.
        # ----------------------------------------------------

        arduino.reset_input_buffer()

        # ----------------------------------------------------
        # Lähetetään komento
        # ----------------------------------------------------

        arduino.write(
            (command + "\n").encode("ascii")
        )

        arduino.flush()

        # ----------------------------------------------------
        # Odotetaan vastausta
        # ----------------------------------------------------

        response = arduino.readline()

        if not response:

            return None

        text = response.decode(
            "utf-8",
            errors="replace"
        ).strip()

        return text

    except Exception as e:

        print(
            f"Arduino-virhe: {e}"
        )

        return None


# ============================================================
# SET POWER
# ============================================================

def set_power(power):

    power = int(
        max(
            0,
            min(
                MAX_POWER,
                power
            )
        )
    )

    response = send_command(
        f"SET_POWER {power}"
    )

    if response is None:

        print(
            "VAROITUS: Arduino ei vastannut."
        )

        return False

    print(
        f"Arduino: {response}"
    )

    expected = f"OK POWER={power}"

    if response != expected:

        print(
            f"VAROITUS: odotettiin "
            f"'{expected}'"
        )

        return False

    return True


# ============================================================
# SMA API
# ============================================================

def get_grid_power():

    try:

        response = requests.get(
            API_URL,
            timeout=2
        )

        response.raise_for_status()

        data = response.json()

        summary = data.get(
            "summary",
            {}
        )

        if "grid_power" not in summary:

            raise RuntimeError(
                "API-vastauksesta puuttuu grid_power"
            )

        return float(
            summary["grid_power"]
        )

    except Exception as e:

        print(
            f"Controller virhe: {e}"
        )

        return None


# ============================================================
# POWER CONTROL
# ============================================================

def calculate_step(grid_power):

    # --------------------------------------------------------
    # DEAD BAND
    #
    # -130 ... -70 W
    #
    # Ei muuteta lämmitystehoa.
    # --------------------------------------------------------

    if (
        DEADBAND_LOW
        <= grid_power
        <= DEADBAND_HIGH
    ):

        return 0


    # --------------------------------------------------------
    # EXPORT
    #
    # grid_power < -130 W
    #
    # Mitä suurempi ylijäämä,
    # sitä suurempi askel.
    # --------------------------------------------------------

    if grid_power < DEADBAND_LOW:

        surplus = abs(
            grid_power - TARGET_GRID_POWER
        )

        if surplus > EXPORT_STEP_300:
            return 300

        if surplus > EXPORT_STEP_200:
            return 200

        if surplus > EXPORT_STEP_100:
            return 100

        if surplus > EXPORT_STEP_50:
            return 50

        return 0


    # --------------------------------------------------------
    # IMPORT
    #
    # grid_power > -70 W
    #
    # Pienennetään lämmitystehoa.
    # --------------------------------------------------------

    if grid_power > DEADBAND_HIGH:

        import_power = (
            grid_power - TARGET_GRID_POWER
        )

        if import_power > IMPORT_STEP_300:
            return -300

        if import_power > IMPORT_STEP_200:
            return -200

        if import_power > IMPORT_STEP_100:
            return -100

        if import_power > IMPORT_STEP_50:
            return -50

        return 0


    return 0


# ============================================================
# STATUS DISPLAY
# ============================================================

def print_status(
    grid_power,
    current_power,
    reason
):

    print(
        f"Grid: {grid_power:7.1f} W | "
        f"Power: {current_power:4d} W | "
        f"{reason}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global running

    print()
    print("SMA Heater Controller")
    print("=====================")


    # --------------------------------------------------------
    # Arduino connection
    # --------------------------------------------------------

    if not connect_arduino():

        print()
        print(
            "Arduino-yhteyttä ei voitu avata."
        )

        print(
            "Controller lopetetaan."
        )

        return 1


    # --------------------------------------------------------
    # Initial power = 0
    # --------------------------------------------------------

    print()
    print(
        "Arduino initial: SET_POWER 0"
    )

    if not set_power(0):

        print(
            "VAROITUS: Arduino ei vahvistanut "
            "käynnistystehoa."
        )


    current_power = 0


    # --------------------------------------------------------
    # Controller information
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
        f"  Export >{EXPORT_STEP_300} W  : +300 W"
    )

    print(
        f"  Export >{EXPORT_STEP_200} W  : +200 W"
    )

    print(
        f"  Export >{EXPORT_STEP_100} W  : +100 W"
    )

    print(
        f"  Export >{EXPORT_STEP_50} W   : +50 W"
    )

    print(
        "  Deadband           : ei muutosta"
    )

    print(
        f"  Import >{IMPORT_STEP_300} W  : -300 W"
    )

    print(
        f"  Import >{IMPORT_STEP_200} W  : -200 W"
    )

    print(
        f"  Import >{IMPORT_STEP_100} W  : -100 W"
    )

    print(
        f"  Import >{IMPORT_STEP_50} W   : -50 W"
    )

    print()

    print(
        f"Max power: {MAX_POWER} W"
    )

    print(
        f"Loop interval: "
        f"{LOOP_INTERVAL:.1f} s"
    )

    print()
    print(
        "Lopetus: CTRL-C"
    )

    print()


    # ========================================================
    # CONTROL LOOP
    # ========================================================

    while running:

        grid_power = get_grid_power()


        # ----------------------------------------------------
        # API error
        # ----------------------------------------------------

        if grid_power is None:

            # Turvallinen toimintatapa:
            #
            # API-virheen aikana emme muuta
            # nykyistä lämmitystehoa.

            time.sleep(
                LOOP_INTERVAL
            )

            continue


        # ----------------------------------------------------
        # Calculate required power step
        # ----------------------------------------------------

        step = calculate_step(
            grid_power
        )


        # ----------------------------------------------------
        # No change
        # ----------------------------------------------------

        if step == 0:

            if (
                DEADBAND_LOW
                <= grid_power
                <= DEADBAND_HIGH
            ):

                reason = "Deadband"

            else:

                reason = "Raja"


            print_status(
                grid_power,
                current_power,
                reason
            )


        # ----------------------------------------------------
        # Power change
        # ----------------------------------------------------

        else:

            new_power = (
                current_power
                + step
            )


            # ------------------------------------------------
            # Safety limits
            # ------------------------------------------------

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


            current_power = new_power


            # ------------------------------------------------
            # Display
            # ------------------------------------------------

            if actual_step > 0:

                reason = (
                    f"+{actual_step} W"
                )

            elif actual_step < 0:

                reason = (
                    f"{actual_step} W"
                )

            else:

                reason = "Raja"


            print_status(
                grid_power,
                current_power,
                reason
            )


            # ------------------------------------------------
            # Send to Arduino
            # ------------------------------------------------

            if actual_step != 0:

                if not set_power(
                    current_power
                ):

                    print(
                        "Arduino-ohjaus epäonnistui."
                    )


        # ----------------------------------------------------
        # Next control cycle
        # ----------------------------------------------------

        time.sleep(
            LOOP_INTERVAL
        )


    # ========================================================
    # SAFE SHUTDOWN
    # ========================================================

    print()

    print(
        "Controller pysäytetään..."
    )


    if arduino is not None:

        print(
            "Arduino: SET_POWER 0"
        )

        set_power(0)


        try:

            arduino.close()

            print(
                "Arduino-yhteys suljetaan..."
            )

        except Exception:

            pass


    print(
        "Controller pysäytetty."
    )

    return 0


# ============================================================
# PROGRAM START
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
