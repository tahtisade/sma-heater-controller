/*
  SMA Heater Controller
  Arduino UNO

  Serial protocol @ 9600 baud:
    SET_POWER <watts>
    GET_STATUS

  Replies:
    HEATER_CONTROLLER_READY
    OK POWER=<watts>
    STATUS POWER=<watts>
    ERROR UNKNOWN_COMMAND

  Outputs:
    D8  -> SSR / heater element 1
    D9  -> SSR / heater element 2
    D10 -> SSR / heater element 3

  Control:
    - 3 x 2000 W elements
    - 6000 W maximum
    - 1 second time-proportional window
    - Suitable for LED/SSR bench testing
*/

const unsigned long WINDOW_LENGTH = 1000UL;  // 1 second
const unsigned long HEARTBEAT_INTERVAL = 5000UL;

const int MAX_TOTAL_POWER = 6000;
const int ELEMENT_POWER = 2000;

const int SSR_PINS[3] = {8, 9, 10};

int targetPower = 0;

unsigned long windowStartTime = 0;
unsigned long lastHeartbeat = 0;

void applyOutputs(unsigned long elapsedTime) {
  int remainingPower = targetPower;

  for (int i = 0; i < 3; i++) {
    int phasePower = 0;

    if (remainingPower >= ELEMENT_POWER) {
      phasePower = ELEMENT_POWER;
      remainingPower -= ELEMENT_POWER;
    }
    else if (remainingPower > 0) {
      phasePower = remainingPower;
      remainingPower = 0;
    }

    unsigned long dutyTime =
      map(phasePower, 0, ELEMENT_POWER, 0, WINDOW_LENGTH);

    if (elapsedTime < dutyTime) {
      digitalWrite(SSR_PINS[i], HIGH);
    }
    else {
      digitalWrite(SSR_PINS[i], LOW);
    }
  }
}

void handleSerial() {
  if (Serial.available() <= 0) {
    return;
  }

  String command = Serial.readStringUntil('\n');
  command.trim();

  if (command.startsWith("SET_POWER ")) {
    int power = command.substring(10).toInt();

    power = constrain(power, 0, MAX_TOTAL_POWER);
    targetPower = power;

    Serial.print("OK POWER=");
    Serial.println(targetPower);
  }
  else if (command == "GET_STATUS") {
    Serial.print("STATUS POWER=");
    Serial.println(targetPower);
  }
  else {
    Serial.println("ERROR UNKNOWN_COMMAND");
  }
}

void setup() {
  for (int i = 0; i < 3; i++) {
    pinMode(SSR_PINS[i], OUTPUT);
    digitalWrite(SSR_PINS[i], LOW);
  }

  Serial.begin(9600);

  // Allow USB serial connection to settle.
  delay(500);

  windowStartTime = millis();
  lastHeartbeat = millis();

  Serial.println("HEATER_CONTROLLER_READY");
}

void loop() {
  handleSerial();

  unsigned long now = millis();

  if (now - windowStartTime >= WINDOW_LENGTH) {
    windowStartTime = now;
  }

  unsigned long elapsedTime = now - windowStartTime;

  applyOutputs(elapsedTime);

  // Diagnostic heartbeat.
  if (now - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    lastHeartbeat = now;

    Serial.print("ALIVE POWER=");
    Serial.println(targetPower);
  }
}
