import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from frontpanel import checksum, frame, parse_bay_map, status_line, DiskState, TrueNASClient


class FrontPanelTests(unittest.TestCase):
    def test_lcd_frame_checksum(self):
        packet = frame(0x13, b"\x01")
        self.assertEqual(packet, bytes.fromhex("f0 02 13 01 06"))
        self.assertEqual(checksum(packet[:-1]), packet[-1])

    def test_bay_map(self):
        result = parse_bay_map("1=/host-dev/sda, 5=/host-dev/sde")
        self.assertEqual(str(result[1]), "/host-dev/sda")
        self.assertEqual(str(result[5]), "/host-dev/sde")

    def test_alert_matching_is_recursive(self):
        alert = {"formatted": "Disk sda SMART failure", "args": {"dev": "sda"}}
        self.assertTrue(TrueNASClient._contains(alert, {"sda"}))
        self.assertFalse(TrueNASClient._contains(alert, {"sdb"}))

    def test_status_line_reports_faulty_bay(self):
        states = [
            DiskState(1, Path("/dev/sda"), False, "ok"),
            DiskState(2, Path("/dev/sdb"), True, "error log"),
        ]
        self.assertEqual(status_line(states), "SMART ERR D2")
