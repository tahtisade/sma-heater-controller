import requests
import serial
import time


SMA_API = "http://localhost:8080/api/status"

ARDUINO_PORT = "/dev/ttyACM0"
BAUDRATE = 9600

MIN_POWER = 0
MAX_POWER = 6000


# --------------------------------------------------
# 1. Luetaan SMA Local Portal
# --------------------------------------------------

response = requests.get(SMA_API, timeout=3)
response.raise_for_status()

data = response.json()

grid_power = data["summary"]["grid_power"]
grid_export = data["summary"]["grid_export"]
grid_import = data["summary"]["grid_import"]

if grid_power < 0:
    surplus = round(-grid_power)
else:
    surplus = 0

surplus = max(MIN_POWER, min(MAX_POWER, surplus))


print(f"Grid power : {grid_power:.1f} W")
print(f"Grid export: {grid_export:.1f} W")
print(f"Grid import: {grid_import:.1f} W")
print(f"Surplus    : {surplus} W")


# --------------------------------------------------
# 2. Yhdistetään Arduinoon
# --------------------------------------------------

ser = serial.Serial(
    ARDUINO_PORT,
    BAUDRATE,
    timeout=2
)

# Arduino käynnistyy uudelleen portin avaamisen yhteydessä.
time.sleep(2)

# Tyhjennetään käynnistysviesti.
while ser.in_waiting:
    line = ser.readline().decode(
        "utf-8",
        errors="replace"
    ).strip()

    if line:
        print(f"Arduino: {line}")


# --------------------------------------------------
# 3. Lähetetään SMA:n perusteella laskettu teho
# --------------------------------------------------

command = f"SET_POWER {surplus}\n"

print(f"Lähetetään: {command.strip()}")

ser.write(command.encode("utf-8"))
ser.flush()


# --------------------------------------------------
# 4. Odotetaan vastausta
# --------------------------------------------------

response = ser.readline().decode(
    "utf-8",
    errors="replace"
).strip()

print(f"Arduino: {response}")


ser.close()
