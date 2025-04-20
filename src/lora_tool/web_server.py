# lora_tool/web_server.py
import threading
import time
import json
import logging
from flask import Flask, request, jsonify, render_template, send_from_directory
import serial
from serial.tools import list_ports
from lora_tool.serial_comm import open_serial_port
from lora_tool.lora_device import LoRaDevice
from lora_tool.can_decoder import CANDecoder
from lora_tool.json_utils import CustomJSONEncoder, apply_custom_json_encoder
import proto.packet_pb2 as packet_pb2
import can
from can.database import load_file as load_dbc_file

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lora_tool.web_server")

app = Flask(__name__, static_folder="static", template_folder="templates")

# Apply our custom JSON encoder using the compatible method
apply_custom_json_encoder(app)

# Global variables
lora_device = None
serial_connection = None
can_db = None
can_decoder = None
connected_port = None
message_queue = []
lock = threading.Lock()

# Load DBC file
try:
    dbc_path = "telemetry.dbc"
    can_db = load_dbc_file(dbc_path)
    can_decoder = CANDecoder(dbc_path)
    logger.info(f"CAN database loaded from {dbc_path}")
except Exception as e:
    logger.error(f"Error loading DBC file: {e}")
    can_db = None
    can_decoder = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports", methods=["GET"])
def get_ports():
    ports = [port.device for port in list_ports.comports()]
    return jsonify({"ports": ports})


@app.route("/api/connect", methods=["POST"])
def connect():
    global lora_device, serial_connection, connected_port

    data = request.get_json()
    port = data.get("port")

    try:
        serial_connection = open_serial_port(port)
        lora_device = LoRaDevice(serial_connection)
        connected_port = port
        lora_device.update_status()
        return jsonify(
            {
                "success": True,
                "settings": lora_device.lora_settings,
                "gps": lora_device.gps_data,
            }
        )
    except Exception as e:
        logger.error(f"Connection error: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    global lora_device

    if request.method == "GET":
        if lora_device:
            return jsonify({"success": True, "settings": lora_device.lora_settings})
        return jsonify({"success": False, "error": "Not connected"})

    elif request.method == "POST":
        if not lora_device:
            return jsonify({"success": False, "error": "Not connected"})

        try:
            data = request.get_json()
            frequency = float(data.get("frequency", 915.0))
            power = int(data.get("power", 22))
            bandwidth = float(data.get("bandwidth", 500.0))
            spreading_factor = int(data.get("spreading_factor", 7))
            coding_rate = int(data.get("coding_rate", 5))
            preamble = int(data.get("preamble", 8))
            set_crc = bool(data.get("set_crc", True))

            # Handle hex input for sync_word
            sync_word_str = data.get("sync_word", "0xAB")
            if isinstance(sync_word_str, str) and sync_word_str.startswith("0x"):
                sync_word = int(sync_word_str, 16)
            else:
                sync_word = int(sync_word_str)

            from lora_tool.settings import update_settings

            update_settings(
                lora_device,
                frequency,
                power,
                bandwidth,
                spreading_factor,
                coding_rate,
                preamble,
                set_crc,
                sync_word,
            )

            lora_device.update_status()
            return jsonify({"success": True, "settings": lora_device.lora_settings})
        except Exception as e:
            logger.error(f"Error updating settings: {str(e)}")
            return jsonify({"success": False, "error": str(e)})


@app.route("/api/receive", methods=["POST"])
def receive():
    global lora_device, message_queue

    if not lora_device:
        return jsonify({"success": False, "error": "Not connected"})

    try:
        # Clear message queue
        with lock:
            message_queue = []

        # Set to receiver mode
        lora_device.change_state(packet_pb2.State.RECEIVER)

        # Start receiving in a background thread
        stop_event = threading.Event()
        receive_thread = threading.Thread(
            target=receive_data_thread, args=(stop_event,)
        )
        receive_thread.daemon = True
        receive_thread.start()

        # Return immediately to allow frontend to start polling for messages
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error starting receiver: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/stop_receive", methods=["POST"])
def stop_receive():
    global lora_device

    if not lora_device:
        return jsonify({"success": False, "error": "Not connected"})

    try:
        lora_device.change_state(packet_pb2.State.STANDBY)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error stopping receiver: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/messages", methods=["GET"])
def get_messages():
    global message_queue

    with lock:
        messages = message_queue.copy()
        message_queue = []

    return jsonify({"messages": messages})


def decode_can_message(payload):
    """Decode CAN message from payload using the CAN decoder."""
    if can_decoder:
        return can_decoder.decode_payload(payload)
    else:
        # Fallback if CAN decoder is not initialized
        if len(payload) < 4:
            return {"error": "Payload too short"}

        # Extract CAN ID (first 4 bytes)
        can_id = int.from_bytes(payload[:4], byteorder="big")

        # Extract data (remaining bytes)
        data = payload[4:]

        result = {"can_id": can_id, "data": data.hex(), "signals": {}}

        # Find the message in the DBC file by ID
        if can_db:
            for message in can_db.messages:
                if message.frame_id == can_id:
                    result["message_name"] = message.name

                    # Create a CAN message object
                    msg = can.Message(
                        arbitration_id=can_id,
                        data=data,
                        is_extended_id=(can_id > 0x7FF),
                    )

                    try:
                        # Decode the message
                        decoded = can_db.decode_message(can_id, data)

                        # Process each signal to handle NamedSignalValue objects
                        for signal_name, signal_value in decoded.items():
                            # Convert NamedSignalValue to string
                            if hasattr(signal_value, "name") and hasattr(
                                signal_value, "value"
                            ):
                                result["signals"][signal_name] = (
                                    f"{signal_value.value} ({signal_value.name})"
                                )
                            else:
                                result["signals"][signal_name] = signal_value
                    except Exception as e:
                        logger.error(f"Error decoding CAN message: {str(e)}")
                        result["decode_error"] = str(e)
                    break
            else:
                result["message_name"] = f"Unknown (0x{can_id:X})"

        return result


def receive_data_thread(stop_event):
    """Background thread for receiving data."""
    global lora_device, message_queue

    def packet_callback(packet):
        if packet.type == packet_pb2.PacketType.LOG:
            log = packet.log

            # Process the CAN message from the payload
            can_data = decode_can_message(log.payload)

            message_info = {
                "timestamp": time.time(),
                "rssi": log.rssi_avg,
                "snr": log.snr,
                "crc_error": log.crc_error,
                "general_error": log.general_error,
                "can_id": can_data.get("can_id"),
                "message_name": can_data.get("message_name", "Unknown"),
                "signals": can_data.get("signals", {}),
                "raw_data": can_data.get("data"),
            }

            with lock:
                message_queue.append(message_info)

            logger.debug(f"Received message: {message_info['message_name']}")

    try:
        # Process packets until stopped
        while not stop_event.is_set():
            if lora_device.ser.in_waiting > 0:
                lora_device.process_serial_packets(
                    packet_callback, exit_on_condition=False
                )
            time.sleep(0.1)
    except Exception as e:
        logger.error(f"Error in receive thread: {e}")
    finally:
        lora_device.change_state(packet_pb2.State.STANDBY)


def start_server():
    """Start the web server on port 5000."""
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)


if __name__ == "__main__":
    start_server()
