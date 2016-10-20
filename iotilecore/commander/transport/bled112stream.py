# This file is adapted from python code released by WellDone International
# under the terms of the LGPLv3.  WellDone International's contact information is
# info@welldone.org
# http://welldone.org
#
# Modifications to this file from the original created at WellDone International 
# are copyright Arch Systems Inc.

import iotilecore.commander.transport.bled112dongle
#from iotilecore.commander.transport.bled112dongle import BLED112Dongle
from iotilecore.commander.transport.cmdstream import CMDStream
from iotilecore.commander.commands import RPCCommand
from iotilecore.exceptions import *
from iotilecore.commander.exceptions import *
from iotilecore.utilities.packed import unpack
import uuid
import serial.tools.list_ports

class BLED112Stream (CMDStream):
	def __init__(self, port, connection_string, record=None):
		if port == '<auto>' or port == None:
			auto_port = None
			ports = serial.tools.list_ports.comports()
			for p in ports:
				#Check if the device matches the BLED112's PID/VID combination
				if (p.pid == 1 and p.vid == 9304):
					auto_port = p.device
					break

			if auto_port == None:
				raise HardwareError("No valid bled 112 device found and automatic port selection specified")

			port = auto_port

		self.dongle = iotilecore.commander.transport.bled112dongle.BLED112Dongle(port)
		self.dongle_open = True

		super(BLED112Stream, self).__init__(port, connection_string, record=record)

	def _close(self):
		try:
			if self.connected:
				self.disconnect()
		finally:
			if self.dongle_open:
				self.dongle.stream.stop()
				self.dongle_open = False

	def _connect(self, connection_string):
		self.conn = self.dongle.connect(connection_string, timeout=6.0)
		self.services = self.dongle.probe_device(self.conn)

		#Check to make sure we support the right ble services like an IOTile device should
		if self.dongle.TileBusService in self.services:
			self.protocol = "tilebus"
			self.dongle.set_notification(self.conn, self.services[self.dongle.TileBusService]['characteristics'][self.dongle.TileBusReceiveHeaderCharacteristic], True)
			self.dongle.set_notification(self.conn, self.services[self.dongle.TileBusService]['characteristics'][self.dongle.TileBusReceivePayloadCharacteristic], True)
		else:
			raise HardwareError("Attempted to connect to device that does not have the appropriate bluetooth service", services=self.services.keys())

	def _send_rpc(self, address, feature, command, *args, **kwargs):
		if not self.connected:
			raise HardwareError("Cannot send an RPC until we are connected to a device")

		if self.dongle.check_disconnected():
			try:
				self._connect(self.connection_string)
			except HardwareError:
				self.connected = False
				raise HardwareError("Connection was disconnected before RPC could be sent and the attempt to reconnect failed")

		rpc = RPCCommand(address, feature, command, *args)

		payload = rpc._format_args()
		payload = payload[:rpc.spec]

		try:
			response = self.dongle.send_tilebus_packet(self.conn, self.services, address, feature, command, payload, **kwargs)
			
			status = ord(response[0])

			#Only read the length of the packet if the has data bit is set
			#If no one responds, the length is 0 as well
			if status == 0xFF and ord(response[1]) == 0xFF:
				length = 0
			elif (status & 1 << 7):
				length = ord(response[3])
			else:
				length = 0
		
			mib_buffer = response[4:4+length]
			assert len(mib_buffer) == length

			return status, mib_buffer
		except TimeoutError:
			if address == 8:
				raise ModuleNotFoundError(address)
			else:
				raise HardwareError("Timeout waiting for a response from the remote BTLE module", address=address, feature=feature, command=command)

	def _send_highspeed(self, data):
		"""
		Send highspeed data using unacknowledged writes to the device
		"""

		hs_char = self.services[self.dongle.TileBusService]['characteristics'][self.dongle.TileBusHighSpeedCharacteristic]['handle']

		for i in xrange(0, len(data), 20):
			chunk_size = len(data) - i
			if chunk_size > 20:
				chunk_size = 20

			chunk = data[i:i+chunk_size]
			self.dongle.write_handle(self.conn, hs_char, str(chunk), wait_ack=False)

	def _enable_streaming(self):
		if self.protocol == 'tilebus':
			self.dongle.set_notification(self.conn, self.services[self.dongle.TileBusService]['characteristics'][self.dongle.TileBusStreamingCharacteristic], True)

			return self.dongle._get_streaming_queue()
		else:
			raise HardwareError("Transport protocol does not support streaming")

	def _disconnect(self):
		try:
			#If we were interrupted in the process of connecting, we may not have a valid conn object, so 
			#try to disconnect the first connection of the dongle.
			if not hasattr(self, 'conn'):
				self.dongle.disconnect(0)
			else:
				self.dongle.disconnect(self.conn)
		except HardwareError as e:
			pass

	def _scan(self):
		found_devs = self.dongle.scan()

		iotile_devs = {}

		#filter devices based on which ones have the iotile service characteristic
		for dev in found_devs:
			#If this is an advertisement response, see if its an IOTile device
			if dev['type'] == 0 or dev['type'] == 6:
				scan_data = dev['scan_data']

				if len(scan_data) < 29:
					continue

				#Skip BLE flags
				scan_data = scan_data[3:]

				#Make sure the scan data comes back with an incomplete UUID list
				if scan_data[0] != 17 or scan_data[1] != 6:
					continue

				uuid_buf = scan_data[2:18]
				assert(len(uuid_buf) == 16)
				service = uuid.UUID(bytes_le=str(uuid_buf))

				if service == self.dongle.TileBusService:
					#Now parse out the manufacturer specific data
					manu_data = scan_data[18:]
					assert (len(manu_data) == 10)

					
					#FIXME: Move flag parsing code flag definitions somewhere else
					length, datatype, manu_id, device_uuid, flags = unpack("<BBHLH", manu_data)
					
					pending = False
					low_voltage = False
					user_connected = False
					if flags & (1 << 0):
						pending = True
					if flags & (1 << 1):
						low_voltage = True
					if flags & (1 << 2):
						user_connected = True

					iotile_devs[dev['address']] = {'user_connected': user_connected, 'connection_string': dev['address'], 'uuid': device_uuid, 'pending_data': pending, 'low_voltage': low_voltage}
			elif dev['type'] == 4 and dev['address'] in iotile_devs:
				#Check if this is a scan response packet from an iotile based device
				scan_data = dev['scan_data']

				if len(scan_data) != 31:
					continue

				length, datatype, manu_id, voltage, stream, reading, reading_time, curr_time = unpack("<BBHHHLLL11x", scan_data)
				
				info = iotile_devs[dev['address']]
				info['voltage'] = voltage / 256.0
				info['current_time'] = curr_time

				if stream != 0xFFFF:
					info['visible_readings'] = [(stream, reading_time, reading),]

		return iotile_devs.values()