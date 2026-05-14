from machine import UART
import time
import ujson

UART_ID = 2
UART_BAUD = 57600
UART_RX_PIN = 16
UART_TX_PIN = 17

USER_DB_FILE = "users.json"

FINGERPRINT_ADDR = 0xFFFFFFFF
FINGERPRINT_PASSWORD = 0x00000000
START_CODE = 0xEF01

PACKET_COMMAND = 0x01
PACKET_ACK = 0x07

CMD_VERIFY_PASSWORD = 0x13
CMD_EMPTY = 0x0D

OK = 0x00
PASSWORD_FAIL = 0x13


class R307Reset:
    def __init__(self):
        self.uart = UART(
            UART_ID,
            baudrate=UART_BAUD,
            tx=UART_TX_PIN,
            rx=UART_RX_PIN,
            bits=8,
            parity=None,
            stop=1,
            timeout=1000
        )

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

                    if len(sync) == 2 and sync[0] == 0xEF and sync[1] == 0x01:
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

        return {
            "packet_type": packet_type,
            "payload": payload
        }

    def command(self, payload, timeout_ms=3000):
        self._send_packet(PACKET_COMMAND, payload)
        response = self._read_packet(timeout_ms)

        if response is None:
            return None

        if response["packet_type"] != PACKET_ACK:
            return None

        payload = response["payload"]

        if len(payload) == 0:
            return None

        return payload[0]

    def verify_password(self):
        payload = [
            CMD_VERIFY_PASSWORD,
            (FINGERPRINT_PASSWORD >> 24) & 0xFF,
            (FINGERPRINT_PASSWORD >> 16) & 0xFF,
            (FINGERPRINT_PASSWORD >> 8) & 0xFF,
            FINGERPRINT_PASSWORD & 0xFF
        ]

        code = self.command(payload, 3000)
        return code == OK

    def empty_database(self):
        return self.command([CMD_EMPTY], 5000)


def clear_users_json():
    try:
        with open(USER_DB_FILE, "w") as f:
            f.write(ujson.dumps({}))

        print("users.json cleared.")
        return True

    except Exception as e:
        print("Failed to clear users.json:", e)
        return False


print("Starting R307 reset...")

finger = R307Reset()

if not finger.verify_password():
    print("ERROR: Cannot communicate with R307.")
    print("Check wiring:")
    print("R307 TX -> ESP32 GPIO16")
    print("R307 RX -> ESP32 GPIO17")
    print("R307 VCC -> 5V")
    print("R307 GND -> GND")
else:
    print("R307 detected.")

    code = finger.empty_database()

    if code == OK:
        print("SUCCESS: All R307 fingerprints deleted.")
        clear_users_json()
        print("FULL RESET DONE.")
    else:
        print("FAILED: Could not clear R307 database. Code:", code)
