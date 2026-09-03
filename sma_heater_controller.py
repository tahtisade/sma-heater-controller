#!/usr/bin/env python3

"""
SMA Heater Controller v2.2.0
============================

Muutokset v2.0.1 -> v2.1.0 -> v2.2.0:

- Resol säätimen tuottama lämpötila-arvo käyttöveden säiliölle, LKV 500l

- Arduino UNO tunnistetaan automaattisesti.
  /dev/ttyACM0 tai /dev/ttyACM1 ei tarvitse enää määrittää käsin.

- Kevyt EMA-suodatus SMA grid_power -mittaukselle.

- Ramp-down on nopeampi kuin ramp-up:
  verkostaoston alkaessa vastustehoa vähennetään nopeammin.

- ALIVE POWER=... -viestit käsitellään oikein.

- 3 peräkkäistä SMA API -virhettä -> SET_POWER 0.

- Käynnistys ja pysäytys aina turvallisesti 0 W.

Nykyinen käyttö:
    Arduino -> LEDit

Myöhemmin:
    Arduino -> välirele/SSR -> teho-SSR -> 3 x 2 kW vastukset
"""

import time
import signal
import sys

import requests
import serial
from serial.tools import list_ports


# ============================================================
# VERSION
# ============================================================

VERSION = "2.3.0"


# ============================================================
# ARDUINO
# ============================================================

SERIAL_BAUD = 9600
SERIAL_TIMEOUT = 2.0

# Arduino UNO R3:
# Vendor  = 0x2341
# Product = 0x0043

ARDUINO_VID = 0x2341
ARDUINO_PID = 0x0043


# ============================================================
# SMA LOCAL PORTAL
# ============================================================

SMA_API_URL = "http://localhost:8080/api/status"
HEATER_STATUS_URL = (
    "http://localhost:8080/api/heater/status"
)
API_TIMEOUT = 3

# Kuinka monta peräkkäistä SMA API -virhettä sallitaan
# ennen kuin lämmitysteho pakotetaan nollaan.

MAX_API_FAILURES = 3

# ============================================================
# KÄYTTÖVESISÄILIÖ / RESOL
# ============================================================

# Lämmitys estetään tämän lämpötilan saavuttamisen jälkeen.
DHW_MAX_TEMP = 71.0

# Lämmitys sallitaan uudelleen vasta tämän alapuolella.
# Näin saadaan 2 °C hystereesi.
DHW_RESUME_TEMP = 69.0

# RESOL-palvelu päivittää arvon 10 sekunnin välein.
# Jos arvo on tätä vanhempi, lämmitys estetään fail-safe-tilassa.
DHW_MAX_DATA_AGE = 30.0

# Järkevyystarkistus anturille.
DHW_MIN_VALID_TEMP = 0.0
DHW_MAX_VALID_TEMP = 100.0


# ============================================================
# SÄÄTÖ
# ============================================================

TARGET_GRID_POWER = -200.0

DEADBAND_LOW = -250.0
DEADBAND_HIGH = -150.0

MIN_POWER = 0
MAX_POWER = 6000

LOOP_INTERVAL = 1.0
CONTROL_DELAY = 2.0


# ============================================================
# SMA MITTAUKSEN SUODATUS
# ============================================================

# EMA = Exponential Moving Average
#
# 1.0 = ei suodatusta
# 0.1 = voimakas suodatus
#
# 0.35 on tarkoituksella kevyt:
# reagoi edelleen nopeasti mutta vaimentaa yksittäisiä piikkejä.

FILTER_ALPHA = 0.35


# ============================================================
# RAMP-UP
#
# Vienti verkkoon -> lisätään vastustehoa.
# ============================================================

UP_STEP_LARGE = 300
UP_STEP_MEDIUM = 200
UP_STEP_SMALL = 100
UP_STEP_FINE = 50


# ============================================================
# RAMP-DOWN
#
# Verkostaosto -> vähennetään vastustehoa nopeammin.
#
# Tarkoituksella noin 2 x ramp-up.
# ============================================================

DOWN_STEP_LARGE = 400
DOWN_STEP_MEDIUM = 300
DOWN_STEP_SMALL = 200
DOWN_STEP_FINE = 100


# ============================================================
# GLOBAL STATE
# ============================================================

controller_running = True


# ============================================================
# SIGNAL HANDLER
# ============================================================

def stop_controller(signum=None, frame=None):

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
# ARDUINO AUTODETECTION
# ============================================================

def find_arduino():

    """
    Etsii Arduino UNO:n USB VID/PID-tunnisteen perusteella.

    Palauttaa esimerkiksi:
        /dev/ttyACM0
        /dev/ttyACM1

    tai None jos Arduinoa ei löydy.
    """

    ports = list(
        list_ports.comports()
    )

    # --------------------------------------------------------
    # Ensisijaisesti tarkka VID/PID
    # --------------------------------------------------------

    for port in ports:

        if (
            port.vid == ARDUINO_VID
            and port.pid == ARDUINO_PID
        ):

            return port.device


    # --------------------------------------------------------
    # Varavaihtoehto:
    # Arduino-nimi USB-kuvauksessa.
    # --------------------------------------------------------

    for port in ports:

        description = (
            port.description or ""
        ).lower()

        manufacturer = (
            port.manufacturer or ""
        ).lower()

        if (
            "arduino" in description
            or "arduino" in manufacturer
        ):

            return port.device


    return None


# ============================================================
# SMA API
# ============================================================

def read_sma_status():
    """
    Lukee yhdellä HTTP-pyynnöllä:
      - sähköverkon tehon
      - käyttövesisäiliön lämpötilan
      - RESOL-tiedot
      - spot-hinnan
      - heater control -asetukset
    """

    response = requests.get(
        SMA_API_URL,
        timeout=API_TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    # --------------------------------------------------------
    # GRID POWER
    # --------------------------------------------------------

    summary = data.get(
        "summary",
        {}
    )

    if "grid_power" in summary:
        grid_power = float(
            summary["grid_power"]
        )

    else:
        energy_meter = data.get(
            "energy_meter",
            {}
        )

        if "grid_power" not in energy_meter:
            raise ValueError(
                "API-vastauksesta puuttuu grid_power"
            )

        grid_power = float(
            energy_meter["grid_power"]
        )

    # --------------------------------------------------------
    # RESOL / KÄYTTÖVESI
    # --------------------------------------------------------

    resol = data.get(
        "resol",
        {}
    )

    dhw_temperature = resol.get(
        "LKV 500l"
    )

    dhw_timestamp = resol.get(
        "timestamp"
    )

    dhw_error = resol.get(
        "error"
    )

    if dhw_temperature is not None:
        dhw_temperature = float(
            dhw_temperature
        )

    if dhw_timestamp is not None:
        dhw_timestamp = float(
            dhw_timestamp
        )

    # --------------------------------------------------------
    # SPOT PRICE
    # --------------------------------------------------------

    spot_price_data = data.get(
        "spot_price",
        {}
    )

    spot_price = spot_price_data.get(
        "current"
    )

    spot_error = spot_price_data.get(
        "error"
    )

    spot_start = spot_price_data.get(
        "start"
    )

    spot_end = spot_price_data.get(
        "end"
    )

    if spot_price is not None:
        spot_price = float(
            spot_price
        )

    # --------------------------------------------------------
    # HEATER CONTROL
    # --------------------------------------------------------

    heater_control = data.get(
        "heater_control",
        {}
    )

    heater_mode = heater_control.get(
        "mode",
        "off"
    )

    spot_price_limit = heater_control.get(
        "spot_price_limit"
    )

    if spot_price_limit is not None:
        spot_price_limit = float(
            spot_price_limit
        )

    return (
        grid_power,
        dhw_temperature,
        dhw_timestamp,
        dhw_error,
        spot_price,
        spot_error,
        spot_start,
        spot_end,
        heater_mode,
        spot_price_limit,
    )

def validate_dhw_temperature(
    temperature,
    timestamp,
    error
):
    """
    Tarkistaa, että käyttövesianturin tieto on turvallisesti
    käytettävissä.

    Palauttaa:
        (True, age)
    tai
        (False, selitys)
    """

    if error:
        return (
            False,
            f"RESOL error: {error}"
        )

    if temperature is None:
        return (
            False,
            "käyttövesilämpötila puuttuu"
        )

    if timestamp is None:
        return (
            False,
            "RESOL timestamp puuttuu"
        )

    if not (
        DHW_MIN_VALID_TEMP
        <= temperature
        <= DHW_MAX_VALID_TEMP
    ):
        return (
            False,
            f"epäkelpo lämpötila {temperature:.1f} °C"
        )

    age = (
        time.time()
        - timestamp
    )

    if age < -5.0:
        return (
            False,
            f"RESOL timestamp tulevaisuudessa ({age:.1f} s)"
        )

    if age > DHW_MAX_DATA_AGE:
        return (
            False,
            f"RESOL-data vanha ({age:.1f} s)"
        )

    return (
       True,
       age
    )

# ============================================================
# SPOT PRICE VALIDATION
# ============================================================

def validate_spot_price(
    price,
    error
):
    """
    Tarkistaa, että spot-hinta on käytettävissä.

    Palauttaa:
        (True, None)
    tai
        (False, selitys)
    """

    if error:
        return (
            False,
            f"spot price error: {error}"
        )

    if price is None:
        return (
            False,
            "spot-hinta puuttuu"
        )

    # Järkevyystarkistus.
    # Negatiiviset hinnat ovat sallittuja.
    if not (
        -100.0
        <= price
        <= 500.0
    ):
        return (
            False,
            f"epäkelpo spot-hinta {price:.3f} snt/kWh"
        )

    return (
        True,
        None
    )


# ============================================================
# EMA FILTER
# ============================================================

def filter_grid_power(
    raw_value,
    previous_filtered
):

    """
    Kevyt eksponentiaalinen suodatus.

    Ensimmäinen mittaus otetaan sellaisenaan.
    """

    if previous_filtered is None:

        return raw_value


    filtered = (
        FILTER_ALPHA
        * raw_value
        +
        (1.0 - FILTER_ALPHA)
        * previous_filtered
    )

    return filtered


# ============================================================
# SÄÄTÖASKEL
# ============================================================

def calculate_step(grid_power):

    """
    Palauttaa tehomuutoksen watteina.

    Positiivinen:
        lisää vastustehoa

    Negatiivinen:
        vähennä vastustehoa
    """


    # --------------------------------------------------------
    # DEADBAND
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
    # Liikaa sähköä verkkoon.
    # Vastustehoa lisätään rauhallisesti.
    # --------------------------------------------------------

    if grid_power < DEADBAND_LOW:

        error = abs(
            grid_power
            - TARGET_GRID_POWER
        )

        if error > 500:
            return +UP_STEP_LARGE

        if error > 250:
            return +UP_STEP_MEDIUM

        if error > 100:
            return +UP_STEP_SMALL

        if error > 50:
            return +UP_STEP_FINE

        return 0


    # --------------------------------------------------------
    # IMPORT
    #
    # Verkosta ostetaan sähköä.
    #
    # Vastustehoa pudotetaan nopeammin kuin nostetaan.
    # --------------------------------------------------------

    if grid_power > DEADBAND_HIGH:

        error = (
            grid_power
            - TARGET_GRID_POWER
        )

        if error > 500:
            return -DOWN_STEP_LARGE

        if error > 250:
            return -DOWN_STEP_MEDIUM

        if error > 100:
            return -DOWN_STEP_SMALL

        if error > 50:
            return -DOWN_STEP_FINE

        return 0


    return 0


# ============================================================
# SERIAL READ
# ============================================================

def read_serial_line(
    ser,
    timeout=SERIAL_TIMEOUT
):

    deadline = (
        time.monotonic()
        + timeout
    )

    while (
        time.monotonic()
        < deadline
    ):

        line = ser.readline()

        if not line:
            continue


        text = line.decode(
            "utf-8",
            errors="replace"
        ).strip()


        if text:

            return text


    return None


# ============================================================
# WAIT FOR ARDUINO READY
# ============================================================

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
# SEND POWER
# ============================================================

def send_power(
    ser,
    power
):

    power = int(
        max(
            MIN_POWER,
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
    # Odotetaan juuri oikeaa OK POWER -vastausta.
    #
    # ALIVE- ja READY-viestit ohitetaan.
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
            timeout=0.25
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
                f"Arduino heartbeat: "
                f"{line}"
            )

            continue


        # ----------------------------------------------------
        # Arduino reset / READY
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
        # Vanha SET_POWER vastaus
        # ----------------------------------------------------

        if line.startswith(
            "OK POWER="
        ):

            print(
                f"Arduino vanha vastaus: "
                f"{line}"
            )

            continue


        print(
            f"Arduino muu viesti: "
            f"{line}"
        )


    print(
        f"VAROITUS: Arduino ei "
        f"vahvistanut tehoa {power} W."
    )

    return False


def report_controller_status(
    power,
    reason
):
    try:
        response = requests.post(
            HEATER_STATUS_URL,
            json={
                "power": int(power),
                "reason": str(reason),
            },
            timeout=1.0
        )

        response.raise_for_status()

        return True

    except Exception as exc:
        print(
            f"Controller status report "
            f"failed: {exc}"
        )

        return False

# ============================================================
# MAIN
# ============================================================

def main():

    global controller_running


    print()
    print(
        f"SMA Heater Controller v{VERSION}"
    )

    print(
        "============================"
    )


    # ========================================================
    # ARDUINO AUTODETECTION
    # ========================================================

    print()
    print(
        "Etsitään Arduino UNO..."
    )


    serial_port = find_arduino()


    if serial_port is None:

        print()
        print(
            "Arduino UNOa ei löytynyt."
        )

        print(
            "Controller lopetetaan."
        )

        return 1


    print(
        f"Arduino löytyi: "
        f"{serial_port}"
    )


    # ========================================================
    # SERIAL CONNECTION
    # ========================================================

    try:

        ser = serial.Serial(
            serial_port,
            SERIAL_BAUD,
            timeout=0.2,
            write_timeout=2
        )


    except Exception as exc:

        print()
        print(
            f"Arduino-yhteyden avaus "
            f"epäonnistui: {exc}"
        )

        return 1


    print(
        "Arduino-yhteys valmis."
    )


    # Arduino UNO voi resetointua
    # kun sarjaportti avataan.

    time.sleep(2.0)


    # ========================================================
    # READY
    # ========================================================

    if not wait_for_ready(
        ser
    ):

        print()
        print(
            "VAROITUS: Arduino READY-"
            "viestiä ei vastaanotettu."
        )


    # ========================================================
    # SAFE START
    # ========================================================

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


    # ========================================================
    # INFO
    # ========================================================

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

    print(
        f"EMA filter alpha: "
        f"{FILTER_ALPHA}"
    )

    print()

    print(
        "Ramp UP:"
    )

    print(
        "  >500 W : +300 W"
    )

    print(
        "  >250 W : +200 W"
    )

    print(
        "  >100 W : +100 W"
    )

    print(
        "  >50 W  :  +50 W"
    )

    print()

    print(
        "Ramp DOWN:"
    )

    print(
        "  >500 W : -600 W"
    )

    print(
        "  >250 W : -400 W"
    )

    print(
        "  >100 W : -200 W"
    )

    print(
        "  >50 W  : -100 W"
    )

    print()

    print(
        f"Max power: {MAX_POWER} W"
    )
    print(
        f"DHW max temperature: "
        f"{DHW_MAX_TEMP:.1f} °C"
    )

    print(
        f"DHW resume temperature: "
        f"{DHW_RESUME_TEMP:.1f} °C"
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
    # CONTROLLER STATE
    # ========================================================

    filtered_grid = None

    next_control_time = (
        time.monotonic()
    )

    api_failures = 0

    # Käynnistetään turvallisesti lukittuna.
    # Ensimmäinen kelvollinen alle 69 °C mittaus vapauttaa lukon.
    dhw_locked = True

    # ========================================================
    # MAIN LOOP
    # ========================================================

    try:

        while controller_running:

            loop_start = (
                time.monotonic()
            )


            # =================================================
            # SMA
            # =================================================

            try:
               (
                   raw_grid,
                   dhw_temperature,
                   dhw_timestamp,
                   dhw_error,
                   spot_price,
                   spot_error,
                   spot_start,
                   spot_end,
                   heater_mode,
                   spot_price_limit,
               ) = read_sma_status()

               api_failures = 0

            except Exception as exc:

                api_failures += 1

                print(
                    f"SMA API virhe "
                    f"({api_failures}/"
                    f"{MAX_API_FAILURES}): "
                    f"{exc}"
                )


                # --------------------------------------------
                # Fail-safe
                # --------------------------------------------

                if (
                    api_failures
                    >= MAX_API_FAILURES
                    and current_power != 0
                ):

                    print(
                        "SMA-yhteys menetetty -> "
                        "SET_POWER 0"
                    )

                    if send_power(
                        ser,
                        0
                    ):

                        current_power = 0


                time.sleep(
                    LOOP_INTERVAL
                )

                continue

            # =================================================
            # KÄYTTÖVESI / RESOL FAIL-SAFE
            # =================================================

            dhw_valid, dhw_info = (
               validate_dhw_temperature(
                  dhw_temperature,
                  dhw_timestamp,
                  dhw_error
               )
            )

            if not dhw_valid:
               print(
                   f"DHW FAIL-SAFE: "
                   f"{dhw_info} -> "
                   f"SET_POWER 0"
               )

               if current_power != 0:
                   if send_power(
                      ser,
                      0
                   ):
                      current_power = 0

               time.sleep(
                   LOOP_INTERVAL
               )


            # =================================================
            # SPOT PRICE FAIL-SAFE
            #
            # Spot-hintaa tarvitaan vain PV + PRICE -tilassa.
            # =================================================

            if heater_mode == "pv_price":

                spot_valid, spot_info = (
                   validate_spot_price(
                       spot_price,
                       spot_error
                    )
                )

                if not spot_valid:
                   print(
                       f"SPOT FAIL-SAFE: "
                       f"{spot_info} -> "
                       f"SET_POWER 0"
                   )

                   if current_power != 0:
                       if send_power(
                           ser,
                           0
                       ):
                           current_power = 0

                   time.sleep(
                       LOOP_INTERVAL
                   )

                   continue

            # =================================================
            # KÄYTTÖVEDEN LÄMPÖTILAHYSTEREESI
            # =================================================

            previous_dhw_locked = (
                dhw_locked
            )

            if (
               dhw_temperature
               >= DHW_MAX_TEMP
            ):
               dhw_locked = True

            elif (
               dhw_temperature
               <= DHW_RESUME_TEMP
            ):
               dhw_locked = False


            if (
               dhw_locked
               != previous_dhw_locked
            ):
               if dhw_locked:
                  print(
                      f"DHW {dhw_temperature:.1f} °C "
                      f"-> MAX TEMP -> "
                      f"lämmitys estetty"
                  )

               else:
                  print(
                       f"DHW {dhw_temperature:.1f} °C "
                       f"-> lämpötilalukko vapautettu"
                  )

            # =================================================
            # FILTER
            # =================================================

            filtered_grid = (
                filter_grid_power(
                    raw_grid,
                    filtered_grid
                )
            )


            # =================================================
            # CONTROL
            # =================================================

            step = 0
            reason = "HOLD"

            new_power = (
                current_power
            )


            now = (
                time.monotonic()
            )

            # -------------------------------------------------
            # Käyttövesisäiliön lämpötilaraja ohittaa
            # kaiken normaalin PV-säädön.
            # -------------------------------------------------

            # -------------------------------------------------
            # 1. Käyttöveden lämpötilaraja ohittaa kaiken.
            # -------------------------------------------------

            if dhw_locked:

               new_power = 0

               step = (
                   new_power
                   - current_power
               )

               reason = "DHW_MAX"

            # -------------------------------------------------
            # 2. OFF
            # -------------------------------------------------

            elif heater_mode == "off":

               new_power = 0

               step = (
                   new_power
                   - current_power
               )

               reason = "OFF"


            # -------------------------------------------------
            # 3. ON
            #
            # Pakotettu täysi teho.
            # DHW-raja on silti voimassa.
            # -------------------------------------------------

            elif heater_mode == "on":

               new_power = MAX_POWER

               step = (
                  new_power
                  - current_power
               )

               reason = "ON"


            # -------------------------------------------------
            # 4. PV + PRICE
            # -------------------------------------------------

            elif (
               heater_mode == "pv_price"
               and spot_price
               > spot_price_limit
            ):

               new_power = 0

               step = (
                   new_power
                   - current_power
               )

               reason = "SPOT_HIGH"


            # -------------------------------------------------
            # 5. PV tai PV + PRICE, kun hinta sallii
            # -------------------------------------------------

            elif (
               heater_mode
               in (
                   "pv",
                   "pv_price",
               )
               and now
               >= next_control_time
            ):

               step = calculate_step(
                   filtered_grid
               )

               new_power = (
                   current_power
                   + step
               )

               new_power = max(
                   MIN_POWER,
                   min(
                        MAX_POWER,
                        new_power
                   )
               )

               actual_step = (
                   new_power
                   - current_power
               )

               step = actual_step

               if step > 0:
                   reason = "EXPORT"

               elif step < 0:
                reason = "IMPORT"

               elif (
                   DEADBAND_LOW
                   <= filtered_grid
                   <= DEADBAND_HIGH
               ):
                   reason = "DEADBAND"

               else:
                   reason = "LIMIT"

               next_control_time = (
                   now
                   + CONTROL_DELAY
               )


            # -------------------------------------------------
            # Tuntematon tila = fail-safe OFF
            # -------------------------------------------------

            elif heater_mode not in (
                "off",
                "on",
                "pv",
                "pv_price",
            ):

                new_power = 0

                step = (
                    new_power
                    - current_power
                )

                reason = "MODE_ERROR"

            # =================================================
            # STATUS PRINT
            # =================================================

            print(
                f"Grid raw: "
                f"{raw_grid:7.1f} W | "
                f"Filtered: "
                f"{filtered_grid:7.1f} W | "
                f"DHW: "
                f"{dhw_temperature:4.1f} °C | "
                f"Spot: "
                f"{spot_price:6.3f} c/kWh | "
                f"Limit: "
                f"{spot_price_limit:5.1f} c/kWh | "
                f"Mode: "
                f"{heater_mode:8s} | "
                f"Power: "
                f"{new_power:4d} W | "
                f"{step:+4d} W | "
                f"{reason}"
            )

            report_controller_status(
                new_power,
                reason
            )

            # =================================================
            # ARDUINO
            # =================================================

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


            # =================================================
            # LOOP TIMING
            # =================================================

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
            "Arduino-yhteys suljettu."
        )

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
