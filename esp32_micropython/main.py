from machine import Pin, UART, PWM
import time
import ujson
import sys
import uselect

# ============================================================
# ESP32 SMART ACCESS CONTROL SYSTEM
# MicroPython
#
# Arduino command format:
# $LCD|SEQ|LINE1|LINE2|PHONE|CHECKSUM
#
# Special LCD reset command:
# $LCD|SEQ|LCD RESET|NOW||CHECKSUM
# ============================================================


# ============================================================
# PIN CONFIG
# ============================================================

R307_UART_ID = 2
R307_BAUD = 57600
R307_RX_PIN = 16
R307_TX_PIN = 17

UNO_UART_ID = 1
UNO_BAUD = 4800
UNO_RX_PIN = 19
UNO_TX_PIN = 18

ROW_PINS = [13, 14, 27, 26]
COL_PINS = [25, 33, 23, 15]

KEYMAP = [
    ["1", "2", "3", "A"],
    ["4", "5", "6", "B"],
    ["7", "8", "9", "C"],
    ["*", "0", "#", "D"]
]

BUZZER_PIN = 4
GREEN_LED_PIN = 19
RED_LED_PIN = 22

MOSFET_PIN = 32
MOSFET_ACTIVE_HIGH = False
ACCESS_OPEN_MS = 5000
LCD_AFTER_SOLENOID_DELAY_MS = 1200

PIN_LENGTH = 4
PHONE_LENGTH = 11

USER_DB_FILE = "users.json"

DEFAULT_USERS = {
    "1": {
        "pin": "1234",
        "phone": ""
    }
}


# ============================================================
# R307 CONSTANTS
# ============================================================

FINGERPRINT_ADDR = 0xFFFFFFFF
FINGERPRINT_PASSWORD = 0x00000000
START_CODE = 0xEF01

PACKET_COMMAND = 0x01
PACKET_ACK = 0x07

CMD_GEN_IMAGE = 0x01
CMD_IMAGE_2_TZ = 0x02
CMD_SEARCH = 0x04
CMD_REG_MODEL = 0x05
CMD_STORE = 0x06
CMD_DELETE = 0x0C
CMD_EMPTY = 0x0D
CMD_VERIFY_PASSWORD = 0x13
CMD_TEMPLATE_COUNT = 0x1D

OK = 0x00
PACKET_ERROR = 0x01
NO_FINGER = 0x02
IMAGE_FAIL = 0x03
IMAGE_MESSY = 0x06
FEATURE_FAIL = 0x07
NO_MATCH = 0x08
NOT_FOUND = 0x09
ENROLL_MISMATCH = 0x0A
BAD_LOCATION = 0x0B
DB_ERROR = 0x0C
UPLOAD_ERROR = 0x0D
DELETE_FAIL = 0x10
CLEAR_FAIL = 0x11
PASSWORD_FAIL = 0x13


# ============================================================
# ARDUINO BRIDGE
# ============================================================

class UnoBridge:
    def __init__(self):
        self.uart = UART(
            UNO_UART_ID,
            baudrate=UNO_BAUD,
            tx=UNO_TX_PIN,
            rx=UNO_RX_PIN,
            bits=8,
            parity=None,
            stop=1,
            timeout=100
        )

        self.seq = 0
        self.last_packet = ""

    def checksum(self, text):
        cs = 0

        for ch in text:
            cs ^= ord(ch)

        return "{:02X}".format(cs)

    def make_packet(self, line1="", line2="", phone=""):
        self.seq += 1

        if self.seq > 9999:
            self.seq = 1

        line1 = str(line1)[:16]
        line2 = str(line2)[:16]
        phone = str(phone).strip()

        body = "$LCD|{}|{}|{}|{}".format(
            self.seq,
            line1,
            line2,
            phone
        )

        cs = self.checksum(body)
        packet = "{}|{}\n".format(body, cs)

        return packet

    def write_slow(self, text):
        self.uart.write("\n")
        time.sleep_ms(120)

        for ch in text:
            self.uart.write(ch)
            time.sleep_ms(8)

    def show(self, line1="", line2="", phone=""):
        packet = self.make_packet(line1, line2, phone)
        self.last_packet = packet

        self.write_slow(packet)
        print("[LCD SEND]", repr(packet))

        time.sleep_ms(300)

    def reset_lcd(self):
        self.show("LCD RESET", "NOW")

    def resend_last(self):
        if self.last_packet:
            self.write_slow(self.last_packet)
            print("[LCD RESEND]", repr(self.last_packet))
            time.sleep_ms(300)

    def show_raw(self, line1="", line2="", phone=""):
        line1 = str(line1)[:16]
        line2 = str(line2)[:16]
        phone = str(phone).strip()

        if phone:
            msg = "LCD:{}|{}|{}\n".format(line1, line2, phone)
        else:
            msg = "LCD:{}|{}\n".format(line1, line2)

        self.write_slow(msg)
        print("[LCD RAW SEND]", repr(msg))


# ============================================================
# KEYPAD
# ============================================================

class MatrixKeypad:
    def __init__(self, row_pins, col_pins, keymap):
        self.rows = [Pin(pin, Pin.OUT) for pin in row_pins]
        self.cols = [Pin(pin, Pin.IN, Pin.PULL_UP) for pin in col_pins]
        self.keymap = keymap

        for row in self.rows:
            row.value(1)

    def get_key(self):
        for r_index, row in enumerate(self.rows):
            row.value(0)

            for c_index, col in enumerate(self.cols):
                if col.value() == 0:
                    key = self.keymap[r_index][c_index]

                    while col.value() == 0:
                        time.sleep_ms(10)

                    row.value(1)
                    time.sleep_ms(100)
                    return key

            row.value(1)

        return None


# ============================================================
# OUTPUTS
# ============================================================

class AccessOutputs:
    def __init__(self):
        self.green = Pin(GREEN_LED_PIN, Pin.OUT)
        self.red = Pin(RED_LED_PIN, Pin.OUT)

        if MOSFET_ACTIVE_HIGH:
            self.mosfet = Pin(MOSFET_PIN, Pin.OUT, value=0)
        else:
            self.mosfet = Pin(MOSFET_PIN, Pin.OUT, value=1)

        self.buzzer = PWM(Pin(BUZZER_PIN))
        self.buzzer.duty(0)

        self.all_off()
        self.lock_solenoid()

    def all_off(self):
        self.green.value(0)
        self.red.value(0)

    def green_on(self):
        self.green.value(1)
        self.red.value(0)

    def red_on(self):
        self.red.value(1)
        self.green.value(0)

    def lock_solenoid(self):
        if MOSFET_ACTIVE_HIGH:
            self.mosfet.value(0)
        else:
            self.mosfet.value(1)

    def unlock_solenoid(self):
        if MOSFET_ACTIVE_HIGH:
            self.mosfet.value(1)
        else:
            self.mosfet.value(0)

    def beep(self, freq=1000, duration_ms=150):
        try:
            self.buzzer.freq(freq)
            self.buzzer.duty(512)
            time.sleep_ms(duration_ms)
            self.buzzer.duty(0)
            time.sleep_ms(40)
        except Exception as e:
            print("[BUZZER ERROR]", e)

    def key_tone(self):
        self.beep(1800, 40)

    def success_tone(self):
        self.beep(1200, 120)
        self.beep(1600, 120)
        self.beep(2000, 160)

    def failed_tone(self):
        self.beep(300, 250)
        self.beep(220, 300)


# ============================================================
# R307 FINGERPRINT
# ============================================================

class R307Fingerprint:
    def __init__(self, uart_id=2, baudrate=57600, tx=17, rx=16):
        self.uart_id = uart_id
        self.baudrate = baudrate
        self.tx_pin = tx
        self.rx_pin = rx

        self.uart = UART(
            uart_id,
            baudrate=baudrate,
            tx=tx,
            rx=rx,
            bits=8,
            parity=None,
            stop=1,
            timeout=1000
        )

    def flush(self):
        try:
            while self.uart.any():
                self.uart.read()
                time.sleep_ms(5)
        except Exception as e:
            print("[R307 FLUSH ERROR]", e)

    def restart_uart(self):
        print("[R307] Restart UART")

        try:
            self.uart.deinit()
        except Exception:
            pass

        time.sleep_ms(200)

        self.uart = UART(
            self.uart_id,
            baudrate=self.baudrate,
            tx=self.tx_pin,
            rx=self.rx_pin,
            bits=8,
            parity=None,
            stop=1,
            timeout=1000
        )

        time.sleep_ms(200)
        self.flush()

    def _checksum(self, packet_type, length, payload):
        total = packet_type
        total += (length >> 8) & 0xFF
        total += length & 0xFF

        for b in payload:
            total += b

        return total & 0xFFFF

    def _send_packet(self, packet_type, payload):
        length = len(payload) + 2
        checksum = self._checksum(packet_type, length, payload)

        packet = bytearray()

        packet.append((START_CODE >> 8) & 0xFF)
        packet.append(START_CODE & 0xFF)

        packet.append((FINGERPRINT_ADDR >> 24) & 0xFF)
        packet.append((FINGERPRINT_ADDR >> 16) & 0xFF)
        packet.append((FINGERPRINT_ADDR >> 8) & 0xFF)
        packet.append(FINGERPRINT_ADDR & 0xFF)

        packet.append(packet_type)

        packet.append((length >> 8) & 0xFF)
        packet.append(length & 0xFF)

        packet.extend(payload)

        packet.append((checksum >> 8) & 0xFF)
        packet.append(checksum & 0xFF)

        self.uart.write(packet)

    def _read_exact(self, size, timeout_ms=3000):
        start = time.ticks_ms()
        data = bytearray()

        while len(data) < size:
            if self.uart.any():
                chunk = self.uart.read(size - len(data))
                if chunk:
                    data.extend(chunk)

            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                return None

            time.sleep_ms(5)

        return bytes(data)

    def _read_packet(self, timeout_ms=3000):
        start_time = time.ticks_ms()
        sync = bytearray()

        while True:
            if self.uart.any():
                b = self.uart.read(1)

                if b:
                    sync.append(b[0])

                    if len(sync) > 2:
                        sync.pop(0)

                    if len(sync) == 2:
                        if sync[0] == 0xEF and sync[1] == 0x01:
                            break

            if time.ticks_diff(time.ticks_ms(), start_time) > timeout_ms:
                return None

            time.sleep_ms(2)

        header = self._read_exact(7, timeout_ms)

        if not header:
            return None

        packet_type = header[4]
        length = (header[5] << 8) | header[6]

        body = self._read_exact(length, timeout_ms)

        if not body:
            return None

        payload = body[:-2]
        received_checksum = (body[-2] << 8) | body[-1]
        calculated_checksum = self._checksum(packet_type, length, payload)

        if received_checksum != calculated_checksum:
            print("[R307 ERROR] Bad checksum")
            return None

        return {
            "packet_type": packet_type,
            "length": length,
            "payload": payload
        }

    def _command(self, payload, timeout_ms=3000):
        self.flush()
        self._send_packet(PACKET_COMMAND, payload)
        response = self._read_packet(timeout_ms)

        if response is None:
            return None, None

        if response["packet_type"] != PACKET_ACK:
            return None, None

        payload = response["payload"]

        if len(payload) == 0:
            return None, payload

        return payload[0], payload

    def verify_password(self):
        payload = [
            CMD_VERIFY_PASSWORD,
            (FINGERPRINT_PASSWORD >> 24) & 0xFF,
            (FINGERPRINT_PASSWORD >> 16) & 0xFF,
            (FINGERPRINT_PASSWORD >> 8) & 0xFF,
            FINGERPRINT_PASSWORD & 0xFF
        ]

        code, data = self._command(payload, timeout_ms=3000)

        if code == OK:
            print("[R307] Password verified")
            return True

        print("[R307] Password verification failed:", self.code_message(code))
        return False

    def get_template_count(self):
        code, data = self._command([CMD_TEMPLATE_COUNT], timeout_ms=3000)

        if code == OK and data and len(data) >= 3:
            return (data[1] << 8) | data[2]

        return -1

    def capture_image(self):
        code, data = self._command([CMD_GEN_IMAGE], timeout_ms=1200)
        return code

    def image_to_char(self, buffer_id):
        code, data = self._command([CMD_IMAGE_2_TZ, buffer_id], timeout_ms=3000)
        return code

    def create_model(self):
        code, data = self._command([CMD_REG_MODEL], timeout_ms=5000)
        return code

    def store_model(self, location_id, buffer_id=1):
        payload = [
            CMD_STORE,
            buffer_id,
            (location_id >> 8) & 0xFF,
            location_id & 0xFF
        ]

        code, data = self._command(payload, timeout_ms=5000)
        return code

    def search_finger(self, buffer_id=1, start_page=0, page_count=200):
        payload = [
            CMD_SEARCH,
            buffer_id,
            (start_page >> 8) & 0xFF,
            start_page & 0xFF,
            (page_count >> 8) & 0xFF,
            page_count & 0xFF
        ]

        code, data = self._command(payload, timeout_ms=5000)

        if code == OK and data and len(data) >= 5:
            finger_id = (data[1] << 8) | data[2]
            score = (data[3] << 8) | data[4]
            return code, finger_id, score

        return code, None, None

    def delete_model(self, location_id, count=1):
        payload = [
            CMD_DELETE,
            (location_id >> 8) & 0xFF,
            location_id & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF
        ]

        code, data = self._command(payload, timeout_ms=5000)
        return code

    def empty_database(self):
        code, data = self._command([CMD_EMPTY], timeout_ms=5000)
        return code

    def wait_remove_finger(self):
        print("[R307] Remove finger")

        start = time.ticks_ms()

        while True:
            check_usb_serial()

            code = self.capture_image()

            if code == NO_FINGER:
                print("[R307] Finger removed")
                return True

            if time.ticks_diff(time.ticks_ms(), start) > 10000:
                return False

            time.sleep_ms(200)

    def scan_login_fingerprint(self):
        lcd.show("Scanning", "Hold Finger")
        print("[LOGIN] Waiting for fingerprint after valid PIN")

        while True:
            check_usb_serial()

            code = self.capture_image()

            if code == OK:
                print("[LOGIN] Finger detected")
                time.sleep_ms(300)

                code = self.image_to_char(1)

                if code != OK:
                    print("[LOGIN] Bad fingerprint image:", self.code_message(code))
                    self.wait_remove_finger()
                    return False, None, None

                code, finger_id, score = self.search_finger()

                self.wait_remove_finger()

                if code == OK:
                    print("[LOGIN] Fingerprint matched ID:", finger_id)
                    print("[LOGIN] Score:", score)
                    return True, finger_id, score

                if code == NOT_FOUND:
                    print("[LOGIN] Fingerprint not found")
                    return False, None, None

                print("[LOGIN] Search error:", self.code_message(code))
                return False, None, None

            elif code == NO_FINGER:
                time.sleep_ms(150)

            else:
                print("[LOGIN] Capture result:", self.code_message(code))
                time.sleep_ms(300)

    def wait_for_good_finger(self, buffer_id, lcd=None):
        if lcd:
            lcd.show("FINGERPRINT", "Scan now")

        while True:
            check_usb_serial()

            code = self.capture_image()

            if code == OK:
                if lcd:
                    lcd.show("Scanning", "Hold Finger")

                time.sleep_ms(400)

                code = self.capture_image()

                if code != OK:
                    if lcd:
                        lcd.show("Finger moved", "Try again")
                    time.sleep_ms(1000)
                    if lcd:
                        lcd.show("FINGERPRINT", "Scan now")
                    continue

                code = self.image_to_char(buffer_id)

                if code == OK:
                    print("[R307] Finger image OK")
                    return True

                if lcd:
                    lcd.show("Scan failed", "Try again")

                self.wait_remove_finger()
                time.sleep_ms(800)

            elif code == NO_FINGER:
                time.sleep_ms(150)

            else:
                print("[R307] Capture error:", self.code_message(code))
                time.sleep_ms(500)

    def enroll(self, location_id, lcd=None):
        print("[ENROLL] ID:", location_id)

        if location_id < 1 or location_id > 200:
            return False

        if lcd:
            lcd.show("FINGERPRINT", "Scan now")

        self.wait_for_good_finger(1, lcd=lcd)

        if lcd:
            lcd.show("First OK", "Remove finger")

        self.wait_remove_finger()
        time.sleep(1)

        if lcd:
            lcd.show("FINGERPRINT", "Scan again")

        self.wait_for_good_finger(2, lcd=lcd)

        if lcd:
            lcd.show("Creating", "Please wait")

        code = self.create_model()

        if code != OK:
            if lcd:
                lcd.show("Register", "Failed")
            return False

        code = self.store_model(location_id, 1)

        if code != OK:
            if lcd:
                lcd.show("Register", "Failed")
            return False

        print("[ENROLL SUCCESS] ID:", location_id)

        return True

    def code_message(self, code):
        messages = {
            OK: "OK",
            PACKET_ERROR: "Packet receive error",
            NO_FINGER: "No finger detected",
            IMAGE_FAIL: "Image capture failed",
            IMAGE_MESSY: "Image too messy",
            FEATURE_FAIL: "Could not extract fingerprint features",
            NO_MATCH: "No match",
            NOT_FOUND: "Fingerprint not found",
            ENROLL_MISMATCH: "Two scans do not match",
            BAD_LOCATION: "Bad storage location",
            DB_ERROR: "Database error",
            UPLOAD_ERROR: "Upload error",
            DELETE_FAIL: "Delete failed",
            CLEAR_FAIL: "Clear database failed",
            PASSWORD_FAIL: "Wrong password",
            None: "No response"
        }

        return messages.get(code, "Unknown code: " + str(code))


# ============================================================
# USER DATABASE
# ============================================================

def normalize_users(raw):
    cleaned = {}

    for fid in raw:
        record = raw[fid]

        if isinstance(record, dict):
            pin = str(record.get("pin", ""))
            phone = str(record.get("phone", ""))
        else:
            pin = str(record)
            phone = ""

        if len(pin) == PIN_LENGTH and pin.isdigit():
            cleaned[str(fid)] = {
                "pin": pin,
                "phone": phone
            }
        else:
            print("[DB WARNING] Ignored invalid user:", fid, record)

    return cleaned


def load_users():
    try:
        with open(USER_DB_FILE, "r") as f:
            raw = ujson.loads(f.read())
            print("[DB] Loaded raw users:", raw)

            cleaned = normalize_users(raw)

            if cleaned != raw:
                save_users(cleaned)

            print("[DB] Active users:", cleaned)
            return cleaned

    except Exception:
        print("[DB] No users.json found. Creating default.")
        save_users(DEFAULT_USERS)
        return dict(DEFAULT_USERS)


def save_users(data):
    try:
        with open(USER_DB_FILE, "w") as f:
            f.write(ujson.dumps(data))
        print("[DB] Saved:", data)
        return True
    except Exception as e:
        print("[DB ERROR]", e)
        return False


def get_next_user_id():
    max_id = 0

    for key in users:
        try:
            current_id = int(key)
            if current_id > max_id:
                max_id = current_id
        except Exception:
            pass

    next_id = max_id + 1

    if next_id > 200:
        return None

    return next_id


# ============================================================
# APP INIT
# ============================================================

lcd = UnoBridge()
keypad = MatrixKeypad(ROW_PINS, COL_PINS, KEYMAP)
outputs = AccessOutputs()

finger = R307Fingerprint(
    uart_id=R307_UART_ID,
    baudrate=R307_BAUD,
    tx=R307_TX_PIN,
    rx=R307_RX_PIN
)

users = load_users()


# ============================================================
# BOOT ERROR LED HELPER
# ============================================================

def set_boot_error_led(has_error):
    if has_error:
        outputs.red_on()
    else:
        outputs.all_off()


# ============================================================
# USB SERIAL COMMAND TESTER
# ============================================================

usb_poll = uselect.poll()
usb_poll.register(sys.stdin, uselect.POLLIN)


def get_first_saved_phone():
    for uid in users:
        try:
            phone = str(users[uid].get("phone", "")).strip()
            if phone:
                return phone
        except Exception:
            pass

    return "09950431207"


def test_login_failed_fingerprint_lcd():
    test_phone = get_first_saved_phone()

    print("[TEST] Unauthorized fingerprint LCD event")
    print("[TEST] Phone:", test_phone)

    try:
        outputs.red_on()
        outputs.failed_tone()
    except Exception:
        pass

    lcd.show("Fingerprint", "Not found", test_phone)

    time.sleep(2)

    try:
        outputs.all_off()
    except Exception:
        pass


def handle_usb_command(cmd):
    cmd = cmd.strip().upper()

    if cmd == "":
        return

    print("[USB CMD]", cmd)

    if cmd == "A":
        test_login_failed_fingerprint_lcd()

    elif cmd == "H":
        lcd.show("WELCOME", "1LOGIN 2REG")

    elif cmd == "S":
        lcd.show("Success", "Access OK")

    elif cmd == "I":
        lcd.show("Invalid Pin", "Try again")

    elif cmd == "R":
        lcd.show("R307 ERROR", "Check wiring")

    elif cmd == "L":
        lcd.reset_lcd()

    elif cmd == "RESEND":
        lcd.resend_last()

    elif cmd == "RAW":
        lcd.show_raw("Fingerprint", "Not found", get_first_saved_phone())

    else:
        print("Commands:")
        print("  A = test unauthorized fingerprint LCD + SMS trigger")
        print("  H = show home")
        print("  S = show success")
        print("  I = show invalid pin")
        print("  R = show R307 error")
        print("  L = reset Arduino LCD only")
        print("  RESEND = resend last LCD packet")
        print("  RAW = send old plain LCD format")


def check_usb_serial():
    try:
        events = usb_poll.poll(0)

        if events:
            cmd = sys.stdin.readline()
            handle_usb_command(cmd)

    except Exception as e:
        print("[USB READ ERROR]", e)


# ============================================================
# UI HELPERS
# ============================================================

def show_home():
    outputs.all_off()
    outputs.lock_solenoid()
    lcd.show("WELCOME", "1LOGIN 2REG")


def show_success():
    outputs.green_on()
    outputs.success_tone()
    lcd.show("Success", "Access OK")

    try:
        outputs.unlock_solenoid()
        print("[MOSFET] Solenoid ON")
        time.sleep_ms(ACCESS_OPEN_MS)
    finally:
        outputs.lock_solenoid()
        print("[MOSFET] Solenoid OFF")

    time.sleep_ms(LCD_AFTER_SOLENOID_DELAY_MS)

    lcd.reset_lcd()
    time.sleep_ms(500)

    outputs.all_off()
    show_home()


def show_invalid_pin():
    outputs.red_on()
    outputs.failed_tone()
    lcd.show("Invalid Pin", "Try again")
    time.sleep(2)
    outputs.all_off()


def show_fingerprint_not_found(phone_list=None):
    outputs.red_on()
    outputs.failed_tone()

    if phone_list and len(phone_list) > 0:
        lcd.show("Fingerprint", "Not found", phone_list[0])
    else:
        lcd.show("Fingerprint", "Not found")

    time.sleep(2)
    outputs.all_off()


def find_user_ids_by_pin(pin):
    matched = []

    for fid in users:
        if str(users[fid].get("pin", "")) == str(pin):
            try:
                matched.append(int(fid))
            except Exception:
                pass

    return matched


def get_phones_by_ids(id_list):
    phones = []

    for uid in id_list:
        key = str(uid)
        if key in users:
            phone = str(users[key].get("phone", "")).strip()
            if phone != "":
                phones.append(phone)

    return phones


def pin_exists(pin):
    pin = str(pin)

    for fid in users:
        try:
            if str(users[fid].get("pin", "")) == pin:
                return True
        except Exception:
            pass

    return False


def check_fingerprint_already_registered():
    lcd.show("Scanning", "Hold Finger")

    while True:
        check_usb_serial()

        code = finger.capture_image()

        if code == OK:
            break
        elif code == NO_FINGER:
            time.sleep_ms(150)
        else:
            time.sleep_ms(300)

    time.sleep_ms(300)

    code = finger.image_to_char(1)

    if code != OK:
        finger.wait_remove_finger()
        return False, None

    code, existing_id, score = finger.search_finger()

    finger.wait_remove_finger()

    if code == OK:
        return True, existing_id

    return False, None


# ============================================================
# INPUT HELPERS
# ============================================================

def read_fixed_digits(title, length, show_plain=False):
    value = ""

    lcd.show(title, "")

    while True:
        check_usb_serial()

        key = keypad.get_key()

        if key:
            outputs.key_tone()

            if key >= "0" and key <= "9":
                if len(value) < length:
                    value += key

                if show_plain:
                    lcd.show(title, value[-16:])
                else:
                    lcd.show(title, "*" * len(value))

                if len(value) == length:
                    time.sleep_ms(200)
                    return value

            elif key == "*":
                value = ""
                lcd.show(title, "")

            elif key == "D":
                return None

        time.sleep_ms(20)


def is_valid_phone(phone):
    return (
        len(phone) == PHONE_LENGTH and
        phone.startswith("09") and
        phone.isdigit()
    )


# ============================================================
# LOGIN
# ============================================================

def handle_login():
    while True:
        outputs.all_off()
        outputs.lock_solenoid()

        entered_pin = read_fixed_digits("PIN & FPRINT", PIN_LENGTH)

        if entered_pin is None:
            show_home()
            return

        valid_pin_user_ids = find_user_ids_by_pin(entered_pin)

        if len(valid_pin_user_ids) == 0:
            show_invalid_pin()
            continue

        matched, scanned_id, score = finger.scan_login_fingerprint()

        if not matched:
            phones = get_phones_by_ids(valid_pin_user_ids)
            show_fingerprint_not_found(phones)
            continue

        if scanned_id in valid_pin_user_ids:
            show_success()
            return

        phones = get_phones_by_ids(valid_pin_user_ids)
        show_fingerprint_not_found(phones)


# ============================================================
# REGISTER
# ============================================================

def handle_register():
    global users

    while True:
        outputs.all_off()
        outputs.lock_solenoid()

        new_id = get_next_user_id()

        if new_id is None:
            outputs.red_on()
            outputs.failed_tone()
            lcd.show("Register", "Full")
            time.sleep(2)
            outputs.all_off()
            show_home()
            return

        phone = read_fixed_digits("Enter Phone#", PHONE_LENGTH, show_plain=True)

        if phone is None:
            show_home()
            return

        if not is_valid_phone(phone):
            outputs.red_on()
            outputs.failed_tone()
            lcd.show("Invalid Phone", "Try again")
            time.sleep(2)
            outputs.all_off()
            show_home()
            return

        lcd.show("Pin then", "Fingerprint")
        time.sleep(2)

        first_pin = read_fixed_digits("Register Pin", PIN_LENGTH)

        if first_pin is None:
            show_home()
            return

        if pin_exists(first_pin):
            outputs.red_on()
            outputs.failed_tone()
            lcd.show("PIN Taken", "Try again")
            time.sleep(2)
            outputs.all_off()
            continue

        second_pin = read_fixed_digits("Retype Pin", PIN_LENGTH)

        if second_pin is None:
            show_home()
            return

        if first_pin != second_pin:
            outputs.red_on()
            outputs.failed_tone()
            lcd.show("PIN Mismatch", "Try again")
            time.sleep(2)
            outputs.all_off()
            show_home()
            return

        lcd.show("FINGERPRINT", "Scan now")
        time.sleep_ms(500)

        already_exists, existing_id = check_fingerprint_already_registered()

        if already_exists:
            outputs.red_on()
            outputs.failed_tone()
            lcd.show("Already Reg", "ID {}".format(existing_id))
            time.sleep(2)
            outputs.all_off()
            show_home()
            return

        ok = finger.enroll(new_id, lcd=lcd)

        if ok:
            users[str(new_id)] = {
                "pin": first_pin,
                "phone": phone
            }

            save_users(users)

            outputs.green_on()
            outputs.success_tone()
            lcd.show("Registered", "1L 2R")
            time.sleep(1)
            outputs.all_off()
            show_home()
            return

        else:
            outputs.red_on()
            outputs.failed_tone()
            lcd.show("Register", "Failed")
            time.sleep(2)
            outputs.all_off()
            show_home()
            return


# ============================================================
# BOOT
# ============================================================

def boot_check():
    print()
    print("====================================")
    print("ESP32 SMART ACCESS CONTROL SYSTEM")
    print("====================================")
    print("R307 RX GPIO:", R307_RX_PIN)
    print("R307 TX GPIO:", R307_TX_PIN)
    print("UNO LCD TX GPIO:", UNO_TX_PIN)
    print("UNO LCD RX GPIO:", UNO_RX_PIN)
    print("Keypad ROW pins:", ROW_PINS)
    print("Keypad COL pins:", COL_PINS)
    print("MOSFET GPIO:", MOSFET_PIN)
    print("PIN length:", PIN_LENGTH)
    print("Phone length:", PHONE_LENGTH)
    print("====================================")

    outputs.all_off()
    outputs.lock_solenoid()

    time.sleep(25)

    lcd.show("BOOTING", "Please wait")
    time.sleep(1)

    if not finger.verify_password():
        set_boot_error_led(True)

        lcd.show("R307 ERROR", "Check wiring")
        outputs.failed_tone()

        print("[ERROR] Cannot communicate with R307.")
        print("Check wiring:")
        print("R307 VCC -> ESP32 5V / VIN")
        print("R307 GND -> ESP32 GND")
        print("R307 TX  -> ESP32 GPIO16 RX")
        print("R307 RX  -> ESP32 GPIO17 TX")
        print("If still not working, swap TX and RX.")

        while True:
            check_usb_serial()
            time.sleep_ms(50)

    set_boot_error_led(False)

    count = finger.get_template_count()
    print("[R307] Stored fingerprints:", count)

    lcd.show("R307 READY", "Count {}".format(count))
    time.sleep(1)


# ============================================================
# MAIN
# ============================================================

def main():
    boot_check()
    show_home()

    while True:
        check_usb_serial()

        key = keypad.get_key()

        if key:
            outputs.key_tone()

            if key == "1":
                handle_login()

            elif key == "2":
                handle_register()

            elif key == "A":
                test_login_failed_fingerprint_lcd()
                show_home()

            else:
                show_home()

        time.sleep_ms(20)


main()
