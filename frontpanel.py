#!/usr/bin/env python3
"""ASUSTOR AS6510T front-panel controller.

Owns the LCD serial port and the ITE IT8625 GPIO helper.  The process is
intentionally single-threaded so every GPIO read/modify/write is serialized.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import select
import signal
import socket
import ssl
import subprocess
import time
import termios
from pathlib import Path
from typing import Iterable

LOG = logging.getLogger("asustor-frontpanel")

STATUS_GPIO = 47
STATUS_OFF_VALUE = 1
RED_GPIOS = (4, 6, 8, 17, 19, 39, 36, 33, 29, 22)
IT87CTL = os.getenv("IT87CTL", "/usr/local/bin/it87ctl")
LCD_DEVICE = os.getenv("LCD_DEVICE", "/host-dev/ttyS1")
DISK_ROOT = Path(os.getenv("DISK_ROOT", "/host-dev"))
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "300")))
LCD_ON_SECONDS = max(1, int(os.getenv("LCD_ON_SECONDS", "30")))
LCD_STARTUP_DELAY_SECONDS = max(0.0, float(os.getenv("LCD_STARTUP_DELAY_SECONDS", "6")))
STATUS_LINE1 = os.getenv("STATUS_LINE1", "TrueNAS AS6510T")
TRUENAS_API_URL = os.getenv("TRUENAS_API_URL", "wss://192.168.1.5/api/current")
TRUENAS_API_USERNAME = os.getenv("TRUENAS_API_USERNAME", "")
TRUENAS_API_KEY = os.getenv("TRUENAS_API_KEY", "")
TRUENAS_TLS_VERIFY = os.getenv("TRUENAS_TLS_VERIFY", "false").lower() == "true"
def primary_ip() -> str:
    configured = os.getenv("PRIMARY_IP", "").strip()
    if configured:
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "IP UNKNOWN"


def checksum(body: bytes) -> int:
    return sum(body) & 0xFF


def frame(function: int, payload: bytes = b"", message_type: int = 0xF0) -> bytes:
    # The LCD length byte counts payload bytes, not the function byte.
    body = bytes((message_type, len(payload), function)) + payload
    return body + bytes((checksum(body),))


def parse_bay_map(value: str) -> dict[int, Path]:
    result: dict[int, Path] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            bay_text, path_text = item.split("=", 1)
            bay = int(bay_text, 10)
            if not 1 <= bay <= 10:
                raise ValueError("bay must be 1..10")
            result[bay] = Path(path_text)
        except ValueError as exc:
            raise ValueError(f"invalid BAY_DEVICE_MAP item {item!r}: {exc}") from exc
    return result
def discover_disks() -> dict[int, Path]:
    """Provide a default bay map; TrueNAS remains authoritative for health."""
    devices = sorted(DISK_ROOT.glob("sd[a-z]"))
    return {bay: path for bay, path in enumerate(devices[:10], 1)}








@dataclasses.dataclass(frozen=True)
class DiskState:
    bay: int
    path: Path
    fault: bool
    reason: str


class TrueNASClient:
    def __init__(self, url: str, username: str, api_key: str):
        self.url = url
        self.username = username
        self.api_key = api_key
        self.request_id = 0

    def _call(self, socket, method: str, params: list[object]) -> object:
        self.request_id += 1
        request_id = self.request_id
        socket.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        while True:
            message = json.loads(socket.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result")

    @staticmethod
    def _contains(value: object, needles: set[str]) -> bool:
        if isinstance(value, dict):
            return any(True for item in value.values() if TrueNASClient._contains(item, needles))
        if isinstance(value, list):
            return any(True for item in value if TrueNASClient._contains(item, needles))
        return isinstance(value, str) and any(needle and needle.lower() in value.lower() for needle in needles)

    def refresh(self, mapping: dict[int, Path]) -> list[DiskState]:
        from websockets.sync.client import connect

        if not self.username or not self.api_key:
            raise RuntimeError("TRUENAS_API_USERNAME and TRUENAS_API_KEY are required")
        connect_options = {"open_timeout": 10, "close_timeout": 5}
        if self.url.startswith("wss://") and not TRUENAS_TLS_VERIFY:
            connect_options["ssl"] = ssl._create_unverified_context()
        with connect(self.url, **connect_options) as socket:
            auth = self._call(
                socket,
                "auth.login_ex",
                [{"mechanism": "API_KEY_PLAIN", "username": self.username, "api_key": self.api_key}],
            )
            if not isinstance(auth, dict) or auth.get("response_type") != "SUCCESS":
                raise RuntimeError(f"TrueNAS authentication failed: {auth!r}")
            disks = self._call(socket, "disk.query", [])
            alerts = self._call(socket, "alert.list", [])
        disk_records = disks if isinstance(disks, list) else []
        active_alerts = [
            alert for alert in alerts if isinstance(alert, dict) and not alert.get("dismissed", False)
        ] if isinstance(alerts, list) else []
        states: list[DiskState] = []
        for bay, path in sorted(mapping.items()):
            devname = path.name
            record = next(
                (disk for disk in disk_records if isinstance(disk, dict)
                 and disk.get("devname") == devname),
                None,
            )
            if record is None:
                states.append(DiskState(bay, path, True, "not in TrueNAS inventory"))
                continue
            identifiers = {devname, str(record.get("name", "")), str(record.get("serial", ""))}
            matching_alert = next(
                (alert for alert in active_alerts if self._contains(alert, identifiers)),
                None,
            )
            if matching_alert is None:
                states.append(DiskState(bay, path, False, "ok"))
            else:
                states.append(DiskState(bay, path, True, str(matching_alert.get("formatted", "alert"))))
        return states

@dataclasses.dataclass(frozen=True)
class DiskState:
    bay: int
    path: Path
    fault: bool
    reason: str


class LCD:
    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.B115200 | termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.rx = bytearray()

    def close(self) -> None:
        os.close(self.fd)

    def _write_all(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _, writable, _ = select.select([], [self.fd], [], 2)
            if not writable:
                raise TimeoutError("LCD serial write timed out")
            count = os.write(self.fd, view)
            view = view[count:]

    def send(self, function: int, payload: bytes = b"") -> None:
        self._write_all(frame(function, payload))

    def set_power(self, on: bool) -> None:
        self.send(0x11, bytes((1 if on else 0,)))
        if on:
            self.send(0x22, b"\x00")

    def clear(self) -> None:
        self.send(0x12, b"\x01")

    def set_text(self, line: int, text: str) -> None:
        encoded = text.encode("ascii", "replace")[:16].ljust(16, b" ")
        self.send(0x27, bytes((line, 0)) + encoded)

    def poll_buttons(self, timeout: float) -> list[int]:
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return []
        try:
            self.rx.extend(os.read(self.fd, 256))
        except BlockingIOError:
            return []
        buttons: list[int] = []
        while len(self.rx) >= 4:
            if self.rx[0] not in (0xF0, 0xF1):
                del self.rx[0]
                continue
            length = self.rx[1]
            total = length + 4
            if length < 1 or total > 64:
                del self.rx[0]
                continue
            if len(self.rx) < total:
                break
            packet = bytes(self.rx[:total])
            del self.rx[:total]
            if checksum(packet[:-1]) != packet[-1]:
                continue
            if packet[0] == 0xF0 and packet[2] == 0x80 and len(packet) >= 5:
                buttons.append(packet[3])
        return buttons


class Hardware:
    def __init__(self):
        self.last_status: bool | None = None
        self.disk_values: dict[int, bool] = {}

    def gpio(self, gpio: int, value: int) -> None:
        subprocess.run([IT87CTL, "set", str(gpio), str(value)], check=True, timeout=5)

    def set_status_ready(self) -> None:
        # GP47 is the AS6510T status LED and is selected by IT8625E GPLED1.
        # Move it out of the hardware blink engine before setting it off.
        if self.last_status is not False:
            subprocess.run(
                [IT87CTL, "disable-blink", str(STATUS_GPIO), str(STATUS_OFF_VALUE)],
                check=True,
                timeout=5,
            )
            self.last_status = False

    def set_disk(self, bay: int, fault: bool) -> None:
        if not 1 <= bay <= len(RED_GPIOS):
            return
        if self.disk_values.get(bay) == fault:
            return
        # The AS6510T vendor driver uses logical value 1 for red on.
        self.gpio(RED_GPIOS[bay - 1], 1 if fault else 0)
        self.disk_values[bay] = fault




def status_line(states: Iterable[DiskState]) -> str:
    states = list(states)
    faults = [str(state.bay) for state in states if state.fault]
    if faults:
        return ("SMART ERR D" + ",D".join(faults))[:16]
    if not states:
        return "SMART NO DISKS"
    return f"SMART OK {len(states)}/{len(states)}"[:16]


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    mapping_text = os.getenv("BAY_DEVICE_MAP", "")
    mapping = parse_bay_map(mapping_text) if mapping_text.strip() else discover_disks()
    LOG.info("monitoring bays: %s", ", ".join(f"{bay}={path}" for bay, path in sorted(mapping.items())))
    hardware = Hardware()
    api = TrueNASClient(TRUENAS_API_URL, TRUENAS_API_USERNAME, TRUENAS_API_KEY)
    lcd = LCD(LCD_DEVICE)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    lcd_on = True
    next_scan = 0.0
    states: list[DiskState] = []
    try:
        lcd.set_power(True)
        hardware.set_status_ready()
        time.sleep(LCD_STARTUP_DELAY_SECONDS)
        lcd.clear()
        lcd.set_text(0, STATUS_LINE1)
        lcd.set_text(1, primary_ip())
        last_wake = time.monotonic()
        while not stopped:
            now = time.monotonic()
            if now >= next_scan:
                try:
                    states = api.refresh(mapping)
                except Exception as exc:
                    LOG.error("TrueNAS API refresh failed: %s", exc)
                    states = [
                        DiskState(bay, path, True, "TrueNAS API unavailable")
                        for bay, path in sorted(mapping.items())
                    ]
                for state in states:
                    hardware.set_disk(state.bay, state.fault)
                line2 = status_line(states)
                LOG.info("%s | %s", STATUS_LINE1, line2)
                if lcd_on:
                    lcd.set_text(0, STATUS_LINE1)
                    lcd.set_text(1, line2)
                next_scan = now + POLL_SECONDS

            if lcd_on and now - last_wake >= LCD_ON_SECONDS:
                LOG.info("LCD timeout: display off")
                lcd.set_power(False)
                lcd_on = False

            for button in lcd.poll_buttons(min(0.5, max(0.05, next_scan - now))):
                LOG.info("LCD button %d: display on", button)
                lcd.set_power(True)
                lcd_on = True
                last_wake = time.monotonic()
                lcd.set_text(0, STATUS_LINE1)
                lcd.set_text(1, status_line(states))
    finally:
        try:
            lcd.set_power(False)
        finally:
            lcd.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
