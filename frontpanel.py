#!/usr/bin/env python3
"""ASUSTOR AS6510T front-panel controller.

Owns the LCD serial port and the ITE IT8625 GPIO helper.  The process is
intentionally single-threaded so every GPIO read/modify/write is serialized.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import select
import signal
import subprocess
import time
import termios
from pathlib import Path
from typing import Iterable

LOG = logging.getLogger("asustor-frontpanel")

STATUS_GPIO = 0
RED_GPIOS = (4, 6, 8, 17, 19, 39, 36, 33, 29, 22)
SMARTCTL = os.getenv("SMARTCTL", "/usr/sbin/smartctl")
SMART_DEVICE_TYPE = os.getenv("SMART_DEVICE_TYPE", "sat")
IT87CTL = os.getenv("IT87CTL", "/usr/local/bin/it87ctl")
LCD_DEVICE = os.getenv("LCD_DEVICE", "/host-dev/ttyS1")
DISK_ROOT = Path(os.getenv("DISK_ROOT", "/host-dev"))
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "300")))
LCD_ON_SECONDS = max(1, int(os.getenv("LCD_ON_SECONDS", "30")))
STATUS_LINE1 = os.getenv("STATUS_LINE1", "TrueNAS AS6510T")


def checksum(body: bytes) -> int:
    return sum(body) & 0xFF


def frame(function: int, payload: bytes = b"", message_type: int = 0xF0) -> bytes:
    body = bytes((message_type, len(payload) + 1, function)) + payload
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
    """Default to Linux's stable disk enumeration; explicit mapping is preferred."""
    devices = sorted(DISK_ROOT.glob("sd[a-z]"))
    return {bay: path for bay, path in enumerate(devices[:10], 1)}


def _health_failed(output: str) -> bool:
    health_lines = [line for line in output.splitlines() if "health" in line.lower() or "status" in line.lower()]
    return any(re.search(r"\b(FAILED|FAIL|CRITICAL)\b", line, re.I) for line in health_lines)


def _positive_error_count(output: str) -> bool:
    patterns = (
        r"ATA Error Count:\s*(\d+)",
        r"Error Information Log Entries:\s*(\d+)",
        r"Media and Data Integrity Errors:\s*(\d+)",
        r"Error Information Log Entries:\s*(\d+)",
    )
    return any(int(match.group(1)) > 0 for pattern in patterns for match in re.finditer(pattern, output, re.I))


def analyze_smart(output: str, returncode: int) -> tuple[bool, str]:
    """Return (fault, short reason) without treating smartctl warning bits as faults."""
    if "STANDBY mode" in output:
        return False, "standby"
    if _health_failed(output):
        return True, "health failed"
    if _positive_error_count(output):
        return True, "error log"
    if returncode != 0 and not re.search(r"No Errors Logged|No error information", output, re.I):
        return True, f"smartctl rc={returncode}"
    return False, "ok"


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
        # The requested post-boot behavior is no flashing status indicator.
        # ASUSTOR vendor mode 0 is off, which is IT87 GPIO value 1.
        if self.last_status is not False:
            self.gpio(STATUS_GPIO, 1)
            self.last_status = False

    def set_disk(self, bay: int, fault: bool) -> None:
        if not 1 <= bay <= len(RED_GPIOS):
            return
        if self.disk_values.get(bay) == fault:
            return
        # The AS6510T vendor driver uses logical value 1 for red on.
        self.gpio(RED_GPIOS[bay - 1], 1 if fault else 0)
        self.disk_values[bay] = fault


def scan_disks(mapping: dict[int, Path]) -> list[DiskState]:
    states: list[DiskState] = []
    for bay, path in sorted(mapping.items()):
        if not path.exists():
            states.append(DiskState(bay, path, True, "missing"))
            continue
        try:
            smart_command = [SMARTCTL, "-n", "standby,0"]
            if SMART_DEVICE_TYPE:
                smart_command.extend(["-d", SMART_DEVICE_TYPE])
            smart_command.extend(["-H", "-l", "error", str(path)])
            proc = subprocess.run(
                smart_command,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            fault, reason = analyze_smart(proc.stdout + proc.stderr, proc.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            fault, reason = True, str(exc)
        states.append(DiskState(bay, path, fault, reason))
    return states


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
    lcd = LCD(LCD_DEVICE)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    lcd_on = True
    last_wake = time.monotonic()
    next_scan = 0.0
    states: list[DiskState] = []
    try:
        lcd.set_power(True)
        hardware.set_status_ready()
        while not stopped:
            now = time.monotonic()
            if now >= next_scan:
                states = scan_disks(mapping)
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
