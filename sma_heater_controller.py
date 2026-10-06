"""
SMA Heater Controller v2.5.2
============================

Host-side controller for surplus-energy based resistive heating.

Features:
- Reads grid power and heater configuration from SMA Local Portal.
- Controls up to 6000 W of resistive heating.
- Supports Arduino UNO and optional RS41-based heater controllers.
- Automatically detects Arduino UNO by USB VID/PID.
- RS41 USB-UART adapter can be selected with HEATER_RS41_SERIAL.
- Uses EMA filtering and asymmetric ramp-up/ramp-down control.
- Supports OFF, ON, PV, PV+PRICE and PRICE operating modes.
- PRICE mode supports a configurable local-time operating window.
- Uses a configurable DHW temperature target, 2 °C hysteresis and a hard 71 °C maximum.
- Applies fail-safe 0 W output on repeated API or sensor failures.
- Starts and stops safely at 0 W.
- Reconnects after serial errors or missing acknowledgements (5 s retry).
- Requires a confirmed 0 W command before resuming after reconnection.

The host communicates with the heater controller over a serial link.
The controller drives three independently time-proportioned outputs
intended for the low-voltage control side of suitable SSRs.

Mains-voltage wiring and protection are outside the scope of this
software and must be implemented using appropriate electrical safety
practices and equipment.
"""

import os
import math
import time
import signal
import sys

import requests
import serial
from serial.tools import list_ports


# ============================================================
# VERSION
# ============================================================

VERSION = "2.5.2"


# ============================================================
# ARDUINO
# ============================================================

SERIAL_BAUD = 9600
SERIAL_TIMEOUT = 2.0
SERIAL_RECONNECT_INTERVAL = 5.0

# Arduino UNO R3:
# Vendor  = 0x2341
# Product = 0x0043

ARDUINO_VID = 0x2341
ARDUINO_PID = 0x0043

# Optional RS41 heater controller USB-UART adapter (FT232R).
# Set HEATER_RS41_SERIAL to the serial number of the adapter.
RS41_SERIAL_NUMBER = os.environ.get(
    "HEATER_RS41_SERIAL"
)


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
    # Ensisijainen ohjain: RS41
    # Tunnistetaan yksilöllisellä USB-sarjanumerolla.
    # --------------------------------------------------------

    if RS41_SERIAL_NUMBER:

        for port in ports:

            if port.serial_number == RS41_SERIAL_NUMBER:

                return port.device

        # Explicit RS41 selection must not switch to an unrelated UNO.
        return None

    # --------------------------------------------------------
    # Varalaite: Arduino UNO tarkalla VID/PID-tunnisteella
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

def dhw_target_limits(target):
    if isinstance(target, bool):
        raise ValueError("Invalid DHW temperature target")
    target = float(target)
    if not math.isfinite(target) or not 40.0 <= target <= DHW_MAX_TEMP:
        raise ValueError("DHW temperature target must be 40–71 °C")
    return target, target - (DHW_MAX_TEMP - DHW_RESUME_TEMP)


def dhw_temperature_locked(temperature, locked, target):
    cutoff, resume = dhw_target_limits(target)
    if temperature >= cutoff:
        return True
    if temperature <= resume:
        return False
    return locked


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
        "temperature"
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

    heater_max_power = heater_control.get(
        "max_power"
    )

    if heater_max_power is None:
        heater_max_power = MAX_POWER
    else:
        heater_max_power = int(
            heater_max_power
        )

    heater_max_power = max(
        MIN_POWER,
        min(
            MAX_POWER,
            heater_max_power
        )
    )

    heater_price_start = heater_control.get(
        "price_start",
        "00:00"
    )

    heater_price_end = heater_control.get(
        "price_end",
        "06:00"
    )

    heater_temperature_target, _ = dhw_target_limits(
        heater_control.get("temperature_target", DHW_MAX_TEMP)
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
        heater_max_power,
        heater_price_start,
        heater_price_end,
        heater_temperature_target,
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
# WAIT FOR CONTROLLER READY
# ============================================================

def wait_for_ready(ser):

    deadline = (
        time.monotonic()
        + 5.0
    )

    next_status_query = 0.0

    while (
        time.monotonic()
        < deadline
    ):

        now = time.monotonic()

        if now >= next_status_query:
            ser.write(
                b"GET_STATUS\n"
            )
            ser.flush()

            next_status_query = (
                now + 1.0
            )

        line = read_serial_line(
            ser,
            timeout=0.5
        )

        if line is None:
            continue

        print(
            f"Controller: {line}"
        )

        if (
            line
            == "HEATER_CONTROLLER_READY"
        ):

            return True

        if line.startswith(
            "STATUS POWER="
        ):

            return True

    return False


# ============================================================
# SEND POWER
# ============================================================

def _send_power(
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
            f"Controller kirjoitusvirhe: "
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

            return True


        # ----------------------------------------------------
        # Heartbeat
        # ----------------------------------------------------

        if line.startswith(
            "ALIVE POWER="
        ):

            print(
                f"Controller heartbeat: "
                f"{line}"
            )

            continue


        # ----------------------------------------------------
        # Controller reset / READY
        # ----------------------------------------------------

        if (
            line
            == "HEATER_CONTROLLER_READY"
        ):

            print(
                f"Controller: {line}"
            )

            continue


        # ----------------------------------------------------
        # Vanha SET_POWER vastaus
        # ----------------------------------------------------

        if line.startswith(
            "OK POWER="
        ):

            print(
                f"Controller vanha vastaus: "
                f"{line}"
            )

            continue


        print(
            f"Controller muu viesti: "
            f"{line}"
        )


    print(
        f"VAROITUS: Controller ei "
        f"vahvistanut tehoa {power} W."
    )

    return False


def send_power(ser, power):
    """Any write/read error or missing ACK invalidates this connection."""
    try:
        if _send_power(ser, power):
            return True
    except Exception as exc:
        print(f"Controller sarjayhteysvirhe: {exc}")
    ser.close()
    return False


class ControllerConnection:
    """Re-enumerate the selected device; never reuse a failed USB handle."""

    def __init__(self):
        self.handle = None
        self.next_attempt = 0.0

    @property
    def is_open(self):
        return self.handle is not None and self.handle.is_open

    def __getattr__(self, name):
        if self.handle is None:
            raise serial.SerialException("Controller ei ole yhdistetty")
        return getattr(self.handle, name)

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        self.next_attempt = time.monotonic() + SERIAL_RECONNECT_INTERVAL

    def ensure_connected(self):
        if self.is_open:
            return True
        if time.monotonic() < self.next_attempt or not controller_running:
            return False
        try:
            port = find_arduino()
            if port is None:
                raise serial.SerialException("Heater controlleria ei löytynyt")
            print(f"Heater controller löytyi: {port}")
            self.handle = serial.Serial(
                port, SERIAL_BAUD, timeout=0.2, write_timeout=2
            )
            # UNO resets when opening its port. Stop signals remain effective.
            deadline = time.monotonic() + 2.0
            while controller_running and time.monotonic() < deadline:
                time.sleep(0.1)
            if not controller_running:
                self.close()
                return False
            if not wait_for_ready(self):
                raise serial.SerialException("Controller ei vastannut tilakyselyyn")
            # Discard startup/status messages before sending the safe command.
            self.reset_input_buffer()
            if not send_power(self, 0):
                raise serial.SerialException("Controller ei kuitannut nollatehoa")
            if not controller_running:
                send_power(self, 0)
                self.close()
                return False
            print("Heater controller -yhteys palautettu: 0 W vahvistettu.")
            return True
        except Exception as exc:
            print(f"Controller OFFLINE: {exc}; uusi yritys 5 s kuluttua.")
            self.close()
            return False


def report_controller_status(
    power,
    reason,
    controller_status,
):
    try:
        response = requests.post(
            HEATER_STATUS_URL,
            json={
                "power": int(power),
                "reason": str(reason),
                "controller_status": str(
                    controller_status
                ),
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

def is_time_in_window(
    start_time,
    end_time,
    current_time=None
):
    """
    Tarkistaa, onko paikallinen kellonaika
    annetussa aikaikkunassa.

    Tukee myös keskiyön ylittäviä jaksoja,
    esimerkiksi 22:00-06:00.

    Sama alku- ja loppuaika tarkoittaa
    koko vuorokauden aktiivista jaksoa.
    """

    if current_time is None:
        current_time = time.localtime()

    try:
        start_hour, start_minute = (
            int(value)
            for value in start_time.split(":")
        )

        end_hour, end_minute = (
            int(value)
            for value in end_time.split(":")
        )

    except (ValueError, AttributeError):
        return False

    if not (
        0 <= start_hour <= 23
        and 0 <= start_minute <= 59
        and 0 <= end_hour <= 23
        and 0 <= end_minute <= 59
    ):
        return False

    start_minutes = (
        start_hour * 60
        + start_minute
    )

    end_minutes = (
        end_hour * 60
        + end_minute
    )

    current_minutes = (
        current_time.tm_hour * 60
        + current_time.tm_min
    )

    if start_minutes == end_minutes:
        return True

    if start_minutes < end_minutes:
        return (
            start_minutes
            <= current_minutes
            < end_minutes
        )

    return (
        current_minutes >= start_minutes
        or current_minutes < end_minutes
    )

def main():

    global controller_running


    print()
    print(
        f"SMA Heater Controller v{VERSION}"
    )

    print(
        "============================"
    )


    # Connection attempts also run while the device is absent at startup.
    ser = ControllerConnection()

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
        f"DHW hard maximum temperature: "
        f"{DHW_MAX_TEMP:.1f} °C"
    )

    print(
        f"DHW default resume temperature: "
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
    next_status_print = time.monotonic()

    # Käynnistetään turvallisesti lukittuna.
    # Kelvollinen mittaus tavoite - 2 °C tai alempana vapauttaa lukon.
    last_temperature_target = None
    dhw_locked = True

    # ========================================================
    # MAIN LOOP
    # ========================================================

    try:

        while controller_running:

            loop_start = (
                time.monotonic()
            )


            # A failed send closes the handle, including fail-safe sends.
            # Resume only after re-enumeration and a confirmed SET_POWER 0.
            if not ser.is_open:
                if not ser.ensure_connected():
                    report_controller_status(
                        current_power, "SERIAL_CONNECTION_LOST", "OFFLINE"
                    )
                    # current_power is last confirmed power, not measured output.
                    if controller_running:
                        time.sleep(LOOP_INTERVAL)
                    continue
                current_power = 0
                filtered_grid = None
                api_failures = 0
                dhw_locked = True
                next_control_time = time.monotonic()
                report_controller_status(0, "SERIAL_RECONNECTED", "ONLINE")

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
                   heater_max_power,
                   heater_price_start,
                   heater_price_end,
                   heater_temperature_target,
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

               continue


            # =================================================
            # SPOT PRICE FAIL-SAFE
            #
            # Spot-hintaa tarvitaan PV + PRICE- ja PRICE-tiloissa.
            # =================================================

            if heater_mode in (
                "pv_price",
                "price",
            ):

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

            if heater_temperature_target != last_temperature_target:
                cutoff, resume = dhw_target_limits(heater_temperature_target)
                print(f"DHW tavoite: {cutoff:.1f} °C; jatkuu: {resume:.1f} °C; yläraja: {DHW_MAX_TEMP:.1f} °C")
                last_temperature_target = heater_temperature_target

            previous_dhw_locked = (
                dhw_locked
            )

            dhw_locked = dhw_temperature_locked(
                dhw_temperature, dhw_locked, heater_temperature_target
            )

            if (
               dhw_locked
               != previous_dhw_locked
            ):
               if dhw_locked:
                  print(
                      f"DHW {dhw_temperature:.1f} °C "
                      f"-> tavoite {heater_temperature_target:.1f} °C -> "
                      f"lämmitys estetty"
                  )

               else:
                  print(
                       f"DHW {dhw_temperature:.1f} °C "
                       f"-> lämpötilalukko vapautettu "
                       f"(tavoite {heater_temperature_target:.1f} °C)"
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

               new_power = heater_max_power

               step = (
                  new_power
                  - current_power
               )

               reason = "ON"


            # -------------------------------------------------
            # 4. PRICE
            #
            # Hinta-tila ei käytä PV-ylijäämää.
            #
            # Lämmitys sallitaan täydellä asetetulla
            # maksimiteholla, kun:
            #
            # - ollaan sallitussa aikaikkunassa
            # - spot-hinta on hintarajan alapuolella
            #   tai yhtä suuri kuin hintaraja
            #
            # DHW-raja ja spot fail-safe on käsitelty
            # jo ennen tätä kohtaa.
            # -------------------------------------------------

            elif heater_mode == "price":

                price_time_active = (
                    is_time_in_window(
                        heater_price_start,
                        heater_price_end
                    )
                )

                if not price_time_active:

                    new_power = 0

                    step = (
                        new_power
                        - current_power
                    )

                    reason = "PRICE_TIME"

                elif (
                    spot_price
                    > spot_price_limit
                ):

                    new_power = 0

                    step = (
                        new_power
                        - current_power
                    )

                    reason = "SPOT_HIGH"

                else:

                    new_power = (
                        heater_max_power
                    )

                    step = (
                        new_power
                        - current_power
                    )

                    reason = "PRICE_ON"

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
                        heater_max_power,
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
                "price",
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

            if time.monotonic() >= next_status_print:

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
                    f"Max: "
                    f"{heater_max_power:4d} W | "
                    f"Power: "
                    f"{new_power:4d} W | "
                    f"{step:+4d} W | "
                    f"{reason}"
                )

                next_status_print = (
                    time.monotonic()
                    + 30.0
                )

            # =================================================
            # HEATER CONTROLLER
            # =================================================

            controller_ok = send_power(
                ser,
                new_power
            )

            if controller_ok:

                current_power = (
                    new_power
                )

                controller_status = (
                    "ONLINE"
                )

            else:

                controller_status = (
                    "OFFLINE"
                )

                print(
                    "Heater controller -ohjaus "
                    "epäonnistui."
                )

            report_controller_status(
                current_power,
                reason,
                controller_status,
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
            "Heater controller: SET_POWER 0"
        )


        try:

            if ser.is_open:

                if not send_power(
                    ser,
                    0
                ):

                    print(
                        "VAROITUS: Controller ei "
                        "vahvistanut nollausta."
                    )

        except Exception as exc:

            print(
                f"Controller-nollauksen "
                f"virhe: {exc}"
            )


        try:

            if ser.is_open:

                ser.close()

        except Exception:

            pass


        print()
        print(
            "Heater controller -yhteys suljettu."
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
