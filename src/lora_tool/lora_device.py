# lora_tool/lora_device.py
import struct
import time
import random
import threading
from datetime import datetime
import packet_pb2 as packet_pb2
from lora_tool.constants import START_MARKER, END_MARKER
from lora_tool.data_handler import save_reception_data


class LoRaDevice:
    def __init__(self, ser):
        """
        Initialize the LoRaDevice with a serial connection.

        Args:
            ser: The serial connection to use for communication.
        """
        self.ser = ser
        self.transmit_count = 0
        self.receive_count = 0
        self.erroneous_count = 0
        self.received_total = 0
        self.count = 0
        self.lora_settings = {}
        self.gps_data = {}
        self.payload = 0
        self.lock = threading.Lock()

        # Buffer for processing packets
        self.buffer = b""
        # Callback functions for received packets
        self.callbacks = {}

    def send_transmission(self, payload, delay=0.5):
        """
        Build and send a transmission packet containing the payload.

        Args:
            payload: The data payload to send.
            delay: Delay after sending (in seconds).
        """
        if self.ser:
            transmission_packet = packet_pb2.Packet()
            transmission_packet.type = packet_pb2.PacketType.TRANSMISSION
            transmission_packet.transmission.payload = payload
            serialized = transmission_packet.SerializeToString()
            framed = START_MARKER + serialized + END_MARKER
            self.ser.write(framed)

            self.transmit_count += 1
            time.sleep(delay)
            return True
        return False

    def update_lora_settings(self, packet):
        """
        Update the stored settings from a received SETTINGS packet.

        Args:
            packet: The received SETTINGS packet.
        """
        settings = packet.settings
        self.lora_settings = {
            "Frequency": settings.frequency,
            "Power": settings.power,
            "Bandwidth": settings.bandwidth,
            "Spreading Factor": settings.spreading_factor,
            "Coding Rate": settings.coding_rate,
            "Preamble": settings.preamble,
            "CRC Enabled": settings.set_crc,
            "Sync Word": hex(settings.sync_word),
        }

    def update_status(self):
        """
        Send requests for both settings and GPS status and process incoming packets
        until both responses are received.
        """
        if not self.ser:
            return False

        self.ser.reset_input_buffer()
        status_received = {"settings": False, "gps": False}
        result = {"success": False}

        def callback(packet):
            if (
                packet.type == packet_pb2.PacketType.SETTINGS
                and not status_received["settings"]
            ):
                self.update_lora_settings(packet)
                status_received["settings"] = True
                result["settings"] = self.lora_settings
            elif (
                packet.type == packet_pb2.PacketType.GPS and not status_received["gps"]
            ):
                gps = packet.gps
                self.gps_data = {
                    "Latitude": gps.latitude,
                    "Longitude": gps.longitude,
                    "Satellites": gps.satellites,
                }
                status_received["gps"] = True
                result["gps"] = self.gps_data

            # Exit processing once both statuses are received
            if all(status_received.values()):
                result["success"] = True
                return True
            return False

        # Send combined requests for settings and GPS
        for req_type in [("settings", True), ("gps", True)]:
            request_pkt = packet_pb2.Packet()
            request_pkt.type = packet_pb2.PacketType.REQUEST
            if req_type[0] == "settings":
                request_pkt.request.settings = True
            else:
                request_pkt.request.gps = True
            serialized = request_pkt.SerializeToString()
            framed = START_MARKER + serialized + END_MARKER
            self.ser.write(framed)

        # Process packets with timeout
        start_time = time.time()
        timeout = 5.0  # 5 seconds timeout

        while time.time() - start_time < timeout:
            if self.ser.in_waiting > 0:
                if self.process_packet(callback):
                    break
            time.sleep(0.1)

        return result

    def process_packet(self, callback):
        """
        Process a single packet from the serial buffer.

        Args:
            callback: Function to call with the parsed packet.

        Returns:
            True if the callback indicates processing should stop,
            False otherwise.
        """
        if not self.ser or self.ser.in_waiting == 0:
            return False

        # Read data
        self.buffer += self.ser.read(self.ser.in_waiting)

        # Look for complete packets
        while START_MARKER in self.buffer and END_MARKER in self.buffer:
            start_idx = self.buffer.find(START_MARKER) + len(START_MARKER)
            end_idx = self.buffer.find(END_MARKER)

            if start_idx > end_idx:  # Corrupted buffer
                self.buffer = self.buffer[end_idx + len(END_MARKER) :]
                continue

            message = self.buffer[start_idx:end_idx]
            self.buffer = self.buffer[end_idx + len(END_MARKER) :]

            try:
                received_packet = packet_pb2.Packet()
                received_packet.ParseFromString(message)

                # Call the callback and check if it wants to stop processing
                if callback(received_packet):
                    return True
            except Exception as e:
                print(f"Failed to decode message: {e}")

        return False

    def process_serial_packets(
        self, callback, exit_on_condition=False, max_processing_time=1.0
    ):
        """
        Process incoming serial data for a limited time.

        Args:
            callback: Function to call with each parsed packet.
            exit_on_condition: If True, stop after one packet.
            max_processing_time: Maximum time to spend processing, in seconds.
        """
        start_time = time.time()

        try:
            while time.time() - start_time < max_processing_time:
                if self.process_packet(callback) and exit_on_condition:
                    break
                time.sleep(0.01)
        except Exception as e:
            print(f"Error in process_serial_packets: {e}")

    def register_callback(self, packet_type, callback_fn):
        """
        Register a callback function for a specific packet type.

        Args:
            packet_type: The PacketType enum value.
            callback_fn: Function to call with the packet.
        """
        self.callbacks[packet_type] = callback_fn

    def change_state(self, state):
        """
        Change the state of the LoRa device.

        Args:
            state: The new state to set for the device.
        """
        if self.ser:
            stateChange_request = packet_pb2.Packet()
            stateChange_request.type = packet_pb2.PacketType.REQUEST
            stateChange_request.request.stateChange = state
            serialized_request = stateChange_request.SerializeToString()
            framed_request = START_MARKER + serialized_request + END_MARKER
            self.ser.write(framed_request)
            return True
        return False
