import unittest
from nose.tools import *
import serial
from test.util.mock_bled112 import MockBLED112
from test.util.ble_device import MockIOTileDevice
from iogateway.adapters.bled112.bled112 import BLED112Adapter
import test.util.dummy_serial
import threading

class TestBLED112AdapterActive(unittest.TestCase):
    """
    Test to make sure that the BLED112Manager is working correctly
    """

    def setUp(self):
        self.old_serial = serial.Serial
        serial.Serial = test.util.dummy_serial.Serial
        self.adapter = MockBLED112(3)
        self.adapter.add_device(MockIOTileDevice(100, "00:11:22:33:44:55", 3.3, 0))

        test.util.dummy_serial.RESPONSE_GENERATOR = self.adapter.generate_response

        self._scanned_devices_seen = threading.Event()
        self.num_scanned_devices = 0
        self.scanned_devices = []
        self.bled = BLED112Adapter('test', self._on_scan_callback, 
                                   self._on_disconnect_callback, passive=False)

    def tearDown(self):
        self.bled.stop()
        serial.Serial = self.old_serial

    def test_basic_init(self):
        """Test that we initialize correctly and the bled112 comes up scanning
        """

        assert self.bled.scanning

    def _on_scan_callback(self, ad_id, info, expiry):
        self.num_scanned_devices += 1
        self.scanned_devices.append(info) 
        self._scanned_devices_seen.set()

    def _on_disconnect_callback(self, *args, **kwargs):
        pass

    def test_scanning(self):
        self._scanned_devices_seen.wait(timeout=1.0)
        assert self.num_scanned_devices == 1
        assert 'voltage' in self.scanned_devices[0]
