import serial
import time

PORT = "/dev/ttyACM0"
BAUDRATE = 9600

ser = serial.Serial(PORT, BAUDRATE, timeout=2)

# USB-sarjaportin avaaminen käynnistää UNO:n uudelleen.
time.sleep(2)

# Tyhjennetään Arduinon käynnistysviesti ja muut mahdolliset vanhat rivit.
while ser.in_waiting:
    line = ser.readline().decode("utf-8", errors="replace").strip()
    print(f"Arduino: {line}")

print("Lähetetään: SET_POWER 2500")

ser.write(b"SET_POWER 2500\n")
ser.flush()

# Odotetaan vastausta.
while True:
    response = ser.readline().decode("utf-8", errors="replace").strip()

    if not response:
        print("Ei vastausta.")
        break

    print(f"Arduino: {response}")

    if response.startswith("OK POWER="):
        break

ser.close()
