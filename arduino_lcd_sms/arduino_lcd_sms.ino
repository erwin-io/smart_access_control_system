#include <Arduino.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <AltSoftSerial.h>
#include <SoftwareSerial.h>

// ============================================================
// Arduino Uno + 12x2 I2C LCD + Air780E + ESP32 UART Bridge
//
// 12x2 I2C LCD:
// VCC -> 5V
// GND -> GND
// SDA -> A4
// SCL -> A5
//
// Air780E using AltSoftSerial:
// Air780E TX -> Arduino D8
// Air780E RX -> Arduino D9 through voltage divider
// Air780E G  -> Arduino GND
// Air780E 5V -> external stable 5V
// Air780E PK -> Arduino D7
//
// ESP32 command UART:
// ESP32 GPIO18 TX -> Arduino D2 RX
// ESP32 GND       -> Arduino GND
//
// Reliable ESP32 packet:
// $LCD|SEQ|LINE1|LINE2|PHONE|CHECKSUM
//
// Manual test commands:
// LCD:Hello|World
// LCD:Fingerprint|Not found|09615742398
// UNAUTH:09615742398
// SMS:09615742398|Message
// LCDRESET
// AT
// INIT
// INITPWR
// SEND
// HELP
// ============================================================


// ============================================================
// LCD CONFIG
// ============================================================

LiquidCrystal_I2C lcd(0x27, 12, 2);
// If LCD is blank, change to:
// LiquidCrystal_I2C lcd(0x3F, 12, 2);

const byte LCD_COLS = 12;


// ============================================================
// SERIALS
// ============================================================

AltSoftSerial modem;              // Air780E RX = D8, TX = D9
SoftwareSerial espSerial(2, 3);   // Arduino D2 RX from ESP32 GPIO18

#define DBG Serial


// ============================================================
// CONFIG
// ============================================================

const byte PWRKEY = 7;

const long AIR_BAUD = 38400;
const long ESP_BAUD = 4800;

const byte LINE_BUF_SIZE = 120;
const byte RESP_BUF_SIZE = 150;

const char TEST_PHONE[] = "09615742398";
const char UNAUTH_MSG[] =
  "Unauthorized use detected: Your PIN was used without valid verification.";

char usbLine[LINE_BUF_SIZE];
char espLine[LINE_BUF_SIZE];
char resp[RESP_BUF_SIZE];

byte usbIndex = 0;
byte espIndex = 0;

bool smsReady = false;
bool smsBusy = false;
bool pendingUnauthSMS = false;

char pendingPhone[12];

int lastSeqProcessed = -1;


// ============================================================
// STRING HELPERS
// ============================================================

void trimC(char *s) {
  int len = strlen(s);

  while (
    len > 0 &&
    (
      s[len - 1] == '\r' ||
      s[len - 1] == '\n' ||
      s[len - 1] == ' '  ||
      s[len - 1] == '\t'
    )
  ) {
    s[len - 1] = 0;
    len--;
  }

  int start = 0;

  while (s[start] == ' ' || s[start] == '\t') {
    start++;
  }

  if (start > 0) {
    memmove(s, s + start, strlen(s + start) + 1);
  }
}


void cleanPrintable(char *s) {
  byte j = 0;

  for (byte i = 0; s[i] != 0 && j < LINE_BUF_SIZE - 1; i++) {
    unsigned char c = (unsigned char)s[i];

    if (c >= 32 && c <= 126) {
      s[j++] = (char)c;
    }
  }

  s[j] = 0;
}


void recoverKnownCommandPrefix(char *s) {
  const char *prefixes[] = {
    "$LCD|",
    "LCD:",
    "SMS:",
    "UNAUTH:",
    "LCDRESET",
    "INITPWR",
    "INIT",
    "SEND",
    "HELP",
    "AT+",
    "AT",
    "A"
  };

  for (byte i = 0; i < sizeof(prefixes) / sizeof(prefixes[0]); i++) {
    char *p = strstr(s, prefixes[i]);

    if (p && p != s) {
      memmove(s, p, strlen(p) + 1);
      return;
    }
  }

  if (strncmp(s, "CD:", 3) == 0) {
    char fixed[LINE_BUF_SIZE];

    memset(fixed, 0, sizeof(fixed));
    strcpy(fixed, "L");
    strncat(fixed, s, LINE_BUF_SIZE - 2);
    strncpy(s, fixed, LINE_BUF_SIZE - 1);
  }
}


void recoverKnownLcdLine1(char *line1) {
  const char *knownLines[] = {
    "Fingerprint",
    "Success",
    "Invalid Pin",
    "WELCOME",
    "BOOTING",
    "R307 ERROR",
    "Scanning",
    "Register",
    "Registered",
    "System Ready",
    "System Error",
    "Access OK",
    "LCD RESET"
  };

  for (byte i = 0; i < sizeof(knownLines) / sizeof(knownLines[0]); i++) {
    char *p = strstr(line1, knownLines[i]);

    if (p && p != line1) {
      memmove(line1, p, strlen(p) + 1);
      return;
    }
  }
}


bool looksLikeImplicitLCD(char *line) {
  return strchr(line, '|') != NULL;
}


// ============================================================
// CHECKSUM
// ============================================================

byte checksumText(const char *text) {
  byte cs = 0;

  for (int i = 0; text[i] != '\0'; i++) {
    cs ^= (byte)text[i];
  }

  return cs;
}


byte hexToByte(const char *hex) {
  byte value = 0;

  for (byte i = 0; i < 2; i++) {
    char c = hex[i];

    value <<= 4;

    if (c >= '0' && c <= '9') {
      value |= c - '0';
    } else if (c >= 'A' && c <= 'F') {
      value |= c - 'A' + 10;
    } else if (c >= 'a' && c <= 'f') {
      value |= c - 'a' + 10;
    }
  }

  return value;
}


// ============================================================
// LCD FUNCTIONS
// ============================================================

void printFixedLine(byte row, const char *text) {
  char line[13];

  memset(line, 0, sizeof(line));

  if (text) {
    strncpy(line, text, LCD_COLS);
  }

  lcd.setCursor(0, row);
  lcd.print(line);

  byte len = strlen(line);

  for (byte i = len; i < LCD_COLS; i++) {
    lcd.print(' ');
  }
}


void resetLCDOnly() {
  DBG.println(F("[LCD] Soft reset only"));

  lcd.init();
  lcd.backlight();
  delay(120);
  lcd.clear();
}


void showLCD(const char *line1, const char *line2) {
  if ((!line1 || strlen(line1) == 0) && (!line2 || strlen(line2) == 0)) {
    return;
  }

  char l1[13];
  char l2[13];

  memset(l1, 0, sizeof(l1));
  memset(l2, 0, sizeof(l2));

  if (line1) {
    strncpy(l1, line1, LCD_COLS);
  }

  if (line2) {
    strncpy(l2, line2, LCD_COLS);
  }

  lcd.clear();
  printFixedLine(0, l1);
  printFixedLine(1, l2);

  DBG.print(F("[LCD] "));
  DBG.print(l1);
  DBG.print(F(" | "));
  DBG.println(l2);
}


void bootLCD() {
  showLCD("System Boot", "Please wait");
}


// ============================================================
// AIR780E BASIC
// ============================================================

void powerKeyPulse() {
  DBG.println(F("[PWRKEY] Pulse"));

  pinMode(PWRKEY, OUTPUT);
  digitalWrite(PWRKEY, LOW);
  delay(1200);

  pinMode(PWRKEY, INPUT);
  delay(3000);
}


char *readModem(unsigned long timeoutMs) {
  memset(resp, 0, sizeof(resp));

  unsigned long start = millis();
  byte index = 0;

  while (millis() - start < timeoutMs) {
    while (modem.available()) {
      char c = modem.read();

      if (index < RESP_BUF_SIZE - 1) {
        resp[index++] = c;
        resp[index] = 0;
      }

      start = millis();
    }
  }

  return resp;
}


char *at(const char *cmd, unsigned long timeoutMs = 1500, bool echo = true) {
  while (modem.available()) {
    modem.read();
  }

  modem.print(cmd);
  modem.print("\r");

  char *r = readModem(timeoutMs);

  if (echo) {
    DBG.print(F(">> "));
    DBG.println(cmd);
    DBG.print(F("<< "));
    DBG.println(r);
  }

  return r;
}


bool atOK(const char *cmd, unsigned long timeoutMs = 1500) {
  return strstr(at(cmd, timeoutMs), "OK") != NULL;
}


bool checkAT() {
  modem.begin(AIR_BAUD);
  delay(300);

  for (byte i = 0; i < 5; i++) {
    char *r = at("AT", 1000, false);

    DBG.print(F("[AT] "));
    DBG.println(r);

    if (strstr(r, "OK")) {
      DBG.println(F("[AIR780E] OK @38400"));
      return true;
    }

    delay(400);
  }

  return false;
}


bool ensureSIM() {
  unsigned long start = millis();

  while (millis() - start < 20000UL) {
    char *r = at("AT+CPIN?", 1000);

    if (strstr(r, "+CPIN: READY")) {
      DBG.println(F("[SIM] READY"));
      return true;
    }

    if (strstr(r, "SIM PIN")) {
      DBG.println(F("[SIM] Requires PIN"));
      return false;
    }

    delay(500);
  }

  DBG.println(F("[SIM] Not ready timeout"));
  return false;
}


bool parseReg(const char *r, int &stat) {
  char *p = strstr((char *)r, "+CEREG:");

  if (!p) {
    return false;
  }

  char *comma = strchr(p, ',');

  if (!comma) {
    return false;
  }

  stat = atoi(comma + 1);
  return true;
}


bool waitNetwork() {
  unsigned long start = millis();

  while (millis() - start < 60000UL) {
    int stat = -1;
    char *r = at("AT+CEREG?", 1200, false);

    if (parseReg(r, stat)) {
      DBG.print(F("[NET] CEREG="));
      DBG.println(stat);

      if (stat == 1 || stat == 5) {
        DBG.println(F("[NET] Registered"));
        return true;
      }
    }

    delay(1000);
  }

  DBG.println(F("[NET] Registration failed"));
  DBG.println(at("AT+CEER", 1500));

  return false;
}


// ============================================================
// AIR780E INIT
// ============================================================

bool initAir(bool usePowerPulse, bool showProgress) {
  smsReady = false;

  if (usePowerPulse) {
    DBG.println(F("[INIT] Power pulse requested"));

    if (showProgress) {
      showLCD("System Boot", "Please wait");
    }

    powerKeyPulse();
  }

  if (showProgress) {
    showLCD("System Boot", "Please wait");
  }

  if (!checkAT()) {
    DBG.println(F("[INIT] No AT Reply"));

    if (showProgress) {
      showLCD("System Error", "Check Module");
    }

    return false;
  }

  atOK("ATE0", 1000);
  atOK("AT+CMEE=1", 1000);
  atOK("AT+CMGF=1", 1000);
  atOK("AT+CSCS=\"GSM\"", 1000);
  atOK("AT+CSMS=1", 1000);
  atOK("AT+IFC=0,0", 1000);

  if (!ensureSIM()) {
    DBG.println(F("[INIT] SIM not ready"));

    if (showProgress) {
      showLCD("System Error", "Check SIM");
    }

    return false;
  }

  atOK("AT+COPS=0", 3000);

  if (!waitNetwork()) {
    DBG.println(F("[INIT] Network not ready"));

    if (showProgress) {
      showLCD("System Error", "Check Signal");
    }

    return false;
  }

  smsReady = true;

  if (showProgress) {
    showLCD("System Ready", "WAIT ESP32");
  }

  DBG.println(F("[AIR780E] SMS READY"));

  return true;
}


bool autoInitAir() {
  showLCD("System Boot", "Please wait");

  DBG.println(F("[AUTO INIT] Try without PK"));

  if (initAir(false, true)) {
    return true;
  }

  delay(1000);

  DBG.println(F("[AUTO INIT] No AT, try with PK"));

  return initAir(true, true);
}


bool ensureSmsReadySilent() {
  if (smsReady) {
    return true;
  }

  DBG.println(F("[SMS] Not ready. Silent init..."));

  if (initAir(false, false)) {
    return true;
  }

  delay(500);

  return initAir(true, false);
}


// ============================================================
// SMS
// ============================================================

bool validPhone(const char *phone) {
  if (strlen(phone) != 11) {
    return false;
  }

  if (phone[0] != '0' || phone[1] != '9') {
    return false;
  }

  for (byte i = 0; i < 11; i++) {
    if (!isDigit(phone[i])) {
      return false;
    }
  }

  return true;
}


bool sendSMSRaw(const char *to, const char *msg) {
  if (!ensureSmsReadySilent()) {
    DBG.println(F("[SMS] Silent init failed"));
    return false;
  }

  DBG.print(F("[SMS] To: "));
  DBG.println(to);

  while (modem.available()) {
    modem.read();
  }

  modem.print("AT+CMGF=1\r");
  readModem(800);

  while (modem.available()) {
    modem.read();
  }

  modem.print("AT+CMGS=\"");
  modem.print(to);
  modem.print("\"\r");

  unsigned long start = millis();
  bool prompt = false;

  while (millis() - start < 5000UL) {
    if (modem.available()) {
      if (modem.read() == '>') {
        prompt = true;
        break;
      }
    }
  }

  if (!prompt) {
    DBG.println(F("[SMS] No prompt"));
    return false;
  }

  modem.print(msg);
  modem.write(26);

  char *r = readModem(20000);

  DBG.println(F("[SMS RESP]"));
  DBG.println(r);

  return (strstr(r, "OK") != NULL);
}


// ============================================================
// SMS QUEUE SYSTEM
// ============================================================

void queueUnauthSMS(const char *phone) {
  if (!validPhone(phone)) {
    DBG.print(F("[UNAUTH] Bad phone: "));
    DBG.println(phone);
    return;
  }

  if (smsBusy || pendingUnauthSMS) {
    DBG.println(F("[UNAUTH] SMS busy/pending, ignored"));
    return;
  }

  memset(pendingPhone, 0, sizeof(pendingPhone));
  strncpy(pendingPhone, phone, sizeof(pendingPhone) - 1);

  pendingUnauthSMS = true;

  DBG.print(F("[UNAUTH] Queued SMS to "));
  DBG.println(pendingPhone);
}


void processPendingSMS() {
  if (!pendingUnauthSMS) {
    return;
  }

  pendingUnauthSMS = false;
  smsBusy = true;

  DBG.print(F("[UNAUTH] Background SMS to "));
  DBG.println(pendingPhone);

  bool ok = sendSMSRaw(pendingPhone, UNAUTH_MSG);

  if (ok) {
    DBG.println(F("[UNAUTH] SMS sent"));
  } else {
    DBG.println(F("[UNAUTH] SMS failed"));
  }

  while (espSerial.available()) {
    espSerial.read();
  }

  espIndex = 0;
  memset(espLine, 0, sizeof(espLine));

  smsBusy = false;
}


void sendManualSMS(const char *phone, const char *msg) {
  if (!validPhone(phone)) {
    showLCD("SMS Error", "Bad Number");
    delay(1200);
    return;
  }

  showLCD("Sending SMS", phone);

  smsBusy = true;

  bool ok = sendSMSRaw(phone, msg);

  smsBusy = false;

  if (ok) {
    showLCD("SMS Sent", phone);
  } else {
    showLCD("SMS Failed", phone);
  }

  delay(1500);

  if (smsReady) {
    showLCD("System Ready", "WAIT ESP32");
  }
}


// ============================================================
// RELIABLE PACKET HANDLER
// ============================================================

bool validOptionalPhone(const char *phone) {
  if (phone == NULL) return true;
  if (strlen(phone) == 0) return true;
  return validPhone(phone);
}


void handleReliableLCD(char *line) {
  if (strncmp(line, "$LCD|", 5) != 0) {
    return;
  }

  char original[LINE_BUF_SIZE];
  memset(original, 0, sizeof(original));
  strncpy(original, line, LINE_BUF_SIZE - 1);

  char *lastSep = strrchr(original, '|');

  if (!lastSep) {
    DBG.println(F("[PKT] Missing checksum"));
    return;
  }

  *lastSep = 0;
  char *csText = lastSep + 1;

  if (strlen(csText) < 2) {
    DBG.println(F("[PKT] Bad checksum text"));
    return;
  }

  byte received = hexToByte(csText);
  byte calculated = checksumText(original);

  if (received != calculated) {
    DBG.println(F("[PKT] Checksum failed, ignored"));
    return;
  }

  char *seqText = original + 5;
  char *sepSeq = strchr(seqText, '|');

  if (!sepSeq) {
    return;
  }

  *sepSeq = 0;

  int seq = atoi(seqText);

  if (seq == lastSeqProcessed) {
    DBG.print(F("[PKT] Duplicate ignored seq "));
    DBG.println(seq);
    return;
  }

  char *line1 = sepSeq + 1;
  char *sepL1 = strchr(line1, '|');

  if (!sepL1) {
    return;
  }

  *sepL1 = 0;

  char *line2 = sepL1 + 1;
  char *sepL2 = strchr(line2, '|');

  if (!sepL2) {
    return;
  }

  *sepL2 = 0;

  char *phone = sepL2 + 1;

  trimC(line1);
  trimC(line2);
  trimC(phone);

  if (!validOptionalPhone(phone)) {
    DBG.print(F("[PKT] Invalid phone ignored: "));
    DBG.println(phone);
    return;
  }

  lastSeqProcessed = seq;

  if (strcmp(line1, "LCD RESET") == 0) {
    resetLCDOnly();
    showLCD("System Ready", "WAIT ESP32");
    return;
  }

  showLCD(line1, line2);

  bool unauthorized =
    strcmp(line1, "Fingerprint") == 0 &&
    (
      strcmp(line2, "Not found") == 0 ||
      strcmp(line2, "Not match") == 0
    );

  if (unauthorized && strlen(phone) > 0) {
    DBG.println(F("[LCD EVENT] Unauthorized fingerprint result"));
    queueUnauthSMS(phone);
  }
}


// ============================================================
// LEGACY MANUAL COMMANDS
// ============================================================

void cmdLCDPayload(char *payload) {
  char *sep1 = strchr(payload, '|');

  char *line1 = payload;
  char *line2 = (char *)"";
  char *phone = (char *)"";

  if (sep1) {
    *sep1 = 0;
    line2 = sep1 + 1;

    char *sep2 = strchr(line2, '|');

    if (sep2) {
      *sep2 = 0;
      phone = sep2 + 1;
    }
  }

  trimC(line1);
  trimC(line2);
  trimC(phone);

  recoverKnownLcdLine1(line1);

  if (strlen(line1) == 0 && strlen(line2) == 0) {
    return;
  }

  if (strcmp(line1, "LCD RESET") == 0) {
    resetLCDOnly();
    showLCD("System Ready", "WAIT ESP32");
    return;
  }

  showLCD(line1, line2);

  bool unauthorized =
    strcmp(line1, "Fingerprint") == 0 &&
    (
      strcmp(line2, "Not found") == 0 ||
      strcmp(line2, "Not match") == 0
    );

  if (unauthorized) {
    DBG.println(F("[LCD EVENT] Unauthorized fingerprint result"));

    if (strlen(phone) > 0 && validPhone(phone)) {
      queueUnauthSMS(phone);
    } else {
      DBG.print(F("[LCD EVENT] No valid phone attached: "));
      DBG.println(phone);
    }
  }
}


void cmdLCD(char *line) {
  cmdLCDPayload(line + 4);
}


void cmdSMS(char *line) {
  char *payload = line + 4;
  char *sep = strchr(payload, '|');

  if (!sep) {
    showLCD("SMS Error", "Bad Format");
    return;
  }

  *sep = 0;

  char *phone = payload;
  char *msg = sep + 1;

  trimC(phone);
  trimC(msg);

  if (strlen(msg) == 0) {
    showLCD("SMS Error", "No Message");
    return;
  }

  sendManualSMS(phone, msg);
}


void help() {
  DBG.println(F("Commands: LCD, UNAUTH, SMS, AT, INIT, INITPWR, SEND, LCDRESET"));
}


void handle(char *line) {
  cleanPrintable(line);
  trimC(line);

  if (strlen(line) == 0) {
    return;
  }

  DBG.print(F("[CMD] "));
  DBG.println(line);

  if (strncmp(line, "$LCD|", 5) == 0) {
    handleReliableLCD(line);
    return;
  }

  if (strncmp(line, "LCD:", 4) == 0) {
    cmdLCD(line);
    return;
  }

  if (strncmp(line, "SMS:", 4) == 0) {
    cmdSMS(line);
    return;
  }

  if (strncmp(line, "UNAUTH:", 7) == 0) {
    char *phone = line + 7;
    trimC(phone);

    showLCD("Fingerprint", "Not found");
    queueUnauthSMS(phone);
    return;
  }

  if (strcmp(line, "LCDRESET") == 0) {
    resetLCDOnly();
    showLCD("System Ready", "WAIT ESP32");
    return;
  }

  if (strcmp(line, "AT") == 0 || strcmp(line, "A") == 0) {
    DBG.println(at("AT", 1500));
    return;
  }

  if (strcmp(line, "INIT") == 0) {
    bool ok = initAir(false, true);

    if (ok) {
      DBG.println(F("[INIT] OK"));
    } else {
      DBG.println(F("[INIT] FAILED"));
      showLCD("System Error", "Use INITPWR");
    }

    return;
  }

  if (strcmp(line, "INITPWR") == 0) {
    bool ok = initAir(true, true);

    if (ok) {
      DBG.println(F("[INITPWR] OK"));
    } else {
      DBG.println(F("[INITPWR] FAILED"));
      showLCD("System Error", "Check Module");
    }

    return;
  }

  if (strcmp(line, "SEND") == 0) {
    showLCD("Fingerprint", "Not found");
    queueUnauthSMS(TEST_PHONE);
    return;
  }

  if (strncmp(line, "AT+", 3) == 0) {
    DBG.println(at(line, 2000));
    return;
  }

  if (looksLikeImplicitLCD(line)) {
    DBG.println(F("[RECOVER] Treating as LCD payload"));
    cmdLCDPayload(line);
    return;
  }

  help();
}


// ============================================================
// SERIAL READERS
// ============================================================

void readLine(Stream &port, char *buffer, byte &index) {
  while (port.available()) {
    char c = port.read();

    if (c == '\n') {
      buffer[index] = 0;
      handle(buffer);
      index = 0;
      memset(buffer, 0, LINE_BUF_SIZE);
    } else if (c != '\r') {
      if (index < LINE_BUF_SIZE - 1) {
        buffer[index++] = c;
      }
    }
  }
}


// ============================================================
// SETUP / LOOP
// ============================================================

void setup() {
  pinMode(PWRKEY, INPUT);

  DBG.begin(115200);
  espSerial.begin(ESP_BAUD);
  modem.begin(AIR_BAUD);

  lcd.init();
  lcd.backlight();

  bootLCD();

  delay(1000);

  DBG.println(F(""));
  DBG.println(F("================================"));
  DBG.println(F("Air780E + 12x2 I2C LCD + ESP32"));
  DBG.println(F("CHECKSUM QUEUE LCD RESET VERSION"));
  DBG.println(F("================================"));
  DBG.println(F("LCD SDA -> A4"));
  DBG.println(F("LCD SCL -> A5"));
  DBG.println(F("Air780E TX D8 RX D9 PK D7"));
  DBG.println(F("ESP32 GPIO18 TX -> D2"));
  DBG.println(F("ESP32 UART baud: 4800"));
  DBG.println(F("Auto init starts now"));
  DBG.println(F("================================"));
  help();

  showLCD("System Boot", "Please wait");

  bool ready = autoInitAir();

  if (!ready) {
    showLCD("System Error", "Check Module");
    DBG.println(F("[BOOT] Auto init failed. Type INITPWR to retry."));
  }
}


void loop() {
  if (!smsBusy) {
    readLine(DBG, usbLine, usbIndex);
    readLine(espSerial, espLine, espIndex);
  }

  processPendingSMS();
}