# lora_tool/webapp.py
import os
import threading
import time
import logging
from flask import Flask, request, jsonify, render_template
from serial.tools import list_ports
from lora_tool.serial_comm import list_serial_ports, open_serial_port
from lora_tool.lora_device import LoRaDevice
from lora_tool.can_decoder import CANDecoder
from lora_tool.json_utils import CustomJSONEncoder
import packet_pb2 as packet_pb2

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lora_tool.webapp")

# Initialize the Flask application
app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
)

# Apply our custom JSON encoder
app.json_encoder = CustomJSONEncoder

# Global variables
lora_device = None
can_decoder = None
serial_connection = None
connected_port = None
message_queue = []
lock = threading.Lock()
is_receiving = False
stop_receive_event = threading.Event()

# Initialize the CAN decoder
dbc_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "telemetry.dbc")
try:
    can_decoder = CANDecoder(dbc_path)
    logger.info(f"CAN decoder initialized with DBC file: {dbc_path}")
except Exception as e:
    logger.error(f"Failed to initialize CAN decoder: {e}")
    can_decoder = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports", methods=["GET"])
def get_ports():
    try:
        ports = list_serial_ports()
        if ports:
            return jsonify({"success": True, "ports": ports})
        else:
            # No ports found, provide helpful message
            import sys
            import platform

            os_type = platform.system()
            help_message = ""

            if os_type == "Windows":
                help_message = (
                    "Make sure your device is connected and drivers are installed."
                )
            elif os_type == "Linux":
                help_message = (
                    "Make sure your user has access to dialout group or run with sudo."
                )
            elif os_type == "Darwin":  # macOS
                help_message = "Check System Information to verify devices. May need to grant permissions."

            return jsonify(
                {
                    "success": False,
                    "ports": [],
                    "error": "No serial ports found",
                    "help": help_message,
                    "python_version": sys.version,
                    "platform": platform.platform(),
                }
            )
    except Exception as e:
        # Handle any exceptions that might occur
        logger.error(f"Error listing ports: {str(e)}")
        return jsonify(
            {"success": False, "ports": [], "error": f"Error listing ports: {str(e)}"}
        )


@app.route("/api/connect", methods=["POST"])
def connect():
    global lora_device, serial_connection, connected_port

    data = request.get_json()
    port = data.get("port")

    if not port:
        return jsonify({"success": False, "error": "No port specified"})

    try:
        serial_connection = open_serial_port(port)
        lora_device = LoRaDevice(serial_connection)
        connected_port = port

        result = lora_device.update_status()
        if result.get("success", False):
            return jsonify(
                {
                    "success": True,
                    "settings": result.get("settings", {}),
                    "gps": result.get("gps", {}),
                }
            )
        else:
            return jsonify({"success": False, "error": "Failed to get device status"})

    except Exception as e:
        logger.error(f"Connection error: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/autodetect", methods=["POST"])
def autodetect_device():
    """Attempt to automatically detect and connect to a LoRa device"""
    global lora_device, serial_connection, connected_port

    # Common USB-Serial devices used with LoRa
    KNOWN_VID_PID = [
        # FTDI
        (0x0403, 0x6001),  # FT232
        (0x0403, 0x6010),  # FT2232
        (0x0403, 0x6011),  # FT4232
        (0x0403, 0x6014),  # FT232H
        # Silicon Labs
        (0x10C4, 0xEA60),  # CP2102/CP2109
        (0x10C4, 0xEA63),  # CP2103
        (0x10C4, 0xEA70),  # CP2105
        # WCH
        (0x1A86, 0x7523),  # CH340
        (0x1A86, 0x5523),  # CH341
    ]

    try:
        # First get all ports
        all_ports = list(list_ports.comports())
        if not all_ports:
            return jsonify({"success": False, "error": "No serial ports found"})

        # Try to find ports that match known LoRa adapter VID/PIDs
        matched_ports = []
        for port in all_ports:
            if port.vid is not None and port.pid is not None:
                if (port.vid, port.pid) in KNOWN_VID_PID:
                    matched_ports.append(port.device)

        # If no known devices found, try ports with common keywords
        if not matched_ports:
            for port in all_ports:
                if any(
                    keyword in port.description.lower()
                    for keyword in ["cp210", "ch340", "ft232", "usb", "uart", "lora"]
                ):
                    matched_ports.append(port.device)

        # If still no matches, take the first available port
        if not matched_ports and all_ports:
            matched_ports = [all_ports[0].device]

        if not matched_ports:
            return jsonify(
                {"success": False, "error": "Could not identify a suitable port"}
            )

        # Try to connect to the first matched port
        try:
            port_to_try = matched_ports[0]
            logger.info(f"Attempting to autodetect on port: {port_to_try}")
            serial_connection = open_serial_port(port_to_try)
            lora_device = LoRaDevice(serial_connection)
            connected_port = port_to_try

            result = lora_device.update_status()
            if result.get("success", False):
                return jsonify(
                    {
                        "success": True,
                        "port": port_to_try,
                        "settings": result.get("settings", {}),
                        "gps": result.get("gps", {}),
                    }
                )
            else:
                # Close connection on failure
                if serial_connection:
                    serial_connection.close()
                return jsonify(
                    {"success": False, "error": "Failed to get device status"}
                )

        except Exception as e:
            logger.error(f"Failed to connect during autodetect: {str(e)}")
            return jsonify({"success": False, "error": f"Failed to connect: {str(e)}"})

    except Exception as e:
        logger.error(f"Autodetect error: {str(e)}")
        return jsonify({"success": False, "error": f"Autodetect error: {str(e)}"})


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    global lora_device

    if not lora_device or not lora_device.ser:
        return jsonify({"success": False, "error": "Not connected"})

    if request.method == "GET":
        return jsonify({"success": True, "settings": lora_device.lora_settings})

    elif request.method == "POST":
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

            # Get updated settings
            lora_device.update_status()

            return jsonify({"success": True, "settings": lora_device.lora_settings})
        except Exception as e:
            logger.error(f"Error updating settings: {str(e)}")
            return jsonify({"success": False, "error": str(e)})


@app.route("/api/receive", methods=["POST"])
def receive():
    global lora_device, is_receiving, stop_receive_event, message_queue

    if not lora_device or not lora_device.ser:
        return jsonify({"success": False, "error": "Not connected"})

    if is_receiving:
        return jsonify({"success": False, "error": "Already receiving"})

    try:
        # Clear message queue
        with lock:
            message_queue = []

        # Reset stop event
        stop_receive_event.clear()

        # Set to receiver mode
        success = lora_device.change_state(packet_pb2.State.RECEIVER)
        if not success:
            return jsonify({"success": False, "error": "Failed to set receiver mode"})

        # Start receiving in a background thread
        receive_thread = threading.Thread(
            target=receive_data_thread, args=(stop_receive_event,)
        )
        receive_thread.daemon = True
        receive_thread.start()

        is_receiving = True
        logger.info("Started receiving mode")

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error starting receiver: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/stop_receive", methods=["POST"])
def stop_receive():
    global lora_device, is_receiving, stop_receive_event

    if not lora_device or not lora_device.ser:
        return jsonify({"success": False, "error": "Not connected"})

    if not is_receiving:
        return jsonify({"success": False, "error": "Not currently receiving"})

    try:
        # Signal the receive thread to stop
        stop_receive_event.set()

        # Set to standby mode
        lora_device.change_state(packet_pb2.State.STANDBY)

        is_receiving = False
        logger.info("Stopped receiving mode")

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


@app.route("/api/debug", methods=["GET"])
def debug_info():
    """Endpoint to provide debugging information"""
    import sys
    import platform
    import serial
    import os

    # Get all serial ports with extra information
    try:
        ports_info = []
        for port in list_ports.comports():
            ports_info.append(
                {
                    "device": port.device,
                    "name": port.name,
                    "description": port.description,
                    "hwid": port.hwid,
                    "vid": port.vid,
                    "pid": port.pid,
                    "serial_number": port.serial_number,
                    "location": port.location,
                    "manufacturer": port.manufacturer,
                    "product": port.product,
                    "interface": getattr(port, "interface", None),
                }
            )
    except Exception as e:
        logger.error(f"Error getting port info: {str(e)}")
        ports_info = [{"error": str(e)}]

    # Check permissions on Linux
    permissions_info = {}
    if platform.system() == "Linux":
        try:
            import pwd, grp

            user = os.getenv("USER", "unknown")
            permissions_info["user"] = user
            permissions_info["groups"] = [
                g.gr_name for g in grp.getgrall() if user in g.gr_mem
            ]

            # Check dialout group
            in_dialout = "dialout" in permissions_info["groups"]
            permissions_info["in_dialout"] = in_dialout

            # Check port permissions
            port_perms = {}
            for port in list_ports.comports():
                try:
                    stat_info = os.stat(port.device)
                    perms = oct(stat_info.st_mode)[-3:]
                    owner = pwd.getpwuid(stat_info.st_uid).pw_name
                    group = grp.getgrgid(stat_info.st_gid).gr_name
                    port_perms[port.device] = {
                        "permissions": perms,
                        "owner": owner,
                        "group": group,
                        "can_read": os.access(port.device, os.R_OK),
                        "can_write": os.access(port.device, os.W_OK),
                    }
                except Exception as e:
                    port_perms[port.device] = {"error": str(e)}

            permissions_info["port_permissions"] = port_perms
        except Exception as e:
            logger.error(f"Error checking permissions: {str(e)}")
            permissions_info["error"] = str(e)

    # Get CAN decoder info
    can_decoder_info = {}
    if can_decoder and can_decoder.db:
        can_decoder_info["dbc_path"] = dbc_path
        can_decoder_info["message_count"] = len(can_decoder.db.messages)
        can_decoder_info["messages"] = [
            {"name": msg.name, "frame_id": f"0x{msg.frame_id:X}", "length": msg.length}
            for msg in can_decoder.db.messages[:5]  # Limit to first 5 messages
        ]

    return jsonify(
        {
            "system": {
                "platform": platform.platform(),
                "python_version": sys.version,
                "cwd": os.getcwd(),
            },
            "serial": {
                "pyserial_version": serial.__version__,
                "ports": ports_info,
            },
            "permissions": permissions_info,
            "can_decoder": can_decoder_info,
            "lora_status": {
                "connected": lora_device is not None
                and hasattr(lora_device, "ser")
                and lora_device.ser is not None,
                "port": connected_port,
                "is_receiving": is_receiving,
            },
        }
    )


def receive_data_thread(stop_event):
    """Background thread for receiving data."""
    global lora_device, message_queue, can_decoder, is_receiving

    try:

        def packet_callback(packet):
            if packet.type == packet_pb2.PacketType.LOG:
                log = packet.log

                # Process the CAN message from the payload
                if can_decoder:
                    can_data = can_decoder.decode_payload(log.payload)
                else:
                    can_data = {"error": "CAN decoder not initialized"}

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
            return False  # Continue processing

        # Process packets until stopped
        while not stop_event.is_set():
            if lora_device and lora_device.ser and lora_device.ser.in_waiting > 0:
                lora_device.process_serial_packets(
                    packet_callback, exit_on_condition=False
                )
            time.sleep(0.1)
    except Exception as e:
        logger.error(f"Error in receive thread: {e}")
    finally:
        if lora_device and lora_device.ser:
            lora_device.change_state(packet_pb2.State.STANDBY)
        is_receiving = False
        logger.info("Receive thread stopped")


def create_folders():
    """Create necessary folders for the application."""
    # Create templates folder if it doesn't exist
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)

    # Create static folder if it doesn't exist
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)


def run_app():
    """Run the Flask application."""
    create_folders()

    # Write the template file if it doesn't exist
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if not os.path.exists(template_path):
        try:
            with open(template_path, "w") as f:
                f.write("""<!DOCTYPE html>
<html>
<head>
    <title>LoRa Tool Web Interface</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .settings-form {
            margin-bottom: 20px;
            padding: 15px;
            border: 1px solid #ddd;
            border-radius: 5px;
        }
        .message-container {
            height: 500px;
            overflow-y: auto;
            padding: 15px;
            border: 1px solid #ddd;
            border-radius: 5px;
            margin-bottom: 20px;
        }
        .message-item {
            margin-bottom: 10px;
            padding: 10px;
            border: 1px solid #eee;
            border-radius: 5px;
        }
        .signal-table {
            width: 100%;
            margin-top: 10px;
        }
        .status-indicator {
            display: inline-block;
            width: 15px;
            height: 15px;
            border-radius: 50%;
            margin-right: 5px;
        }
        .status-connected {
            background-color: green;
        }
        .status-disconnected {
            background-color: red;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="my-4">LoRa Tool Web Interface</h1>
        
        <div class="row">
            <div class="col-md-4">
                <div class="card mb-3">
                    <div class="card-header">
                        Connection
                        <span id="connection-status" class="status-indicator status-disconnected float-end"></span>
                    </div>
                    <div class="card-body">
                        <select id="port-select" class="form-select mb-2">
                            <option value="">Select Port</option>
                        </select>
                        <button id="refresh-ports" class="btn btn-secondary btn-sm mb-2">Refresh Ports</button>
                        <button id="debug-button" class="btn btn-info btn-sm mb-2">Debug</button>
                        <button id="connect-button" class="btn btn-primary mb-2">Connect</button>
                        <button id="autodetect-button" class="btn btn-outline-primary btn-sm mb-2">Auto-Detect</button>
                        <div id="connection-info" class="small mt-2"></div>
                    </div>
                </div>
                
                <div class="card mb-3">
                    <div class="card-header">LoRa Settings</div>
                    <div class="card-body">
                        <form id="settings-form">
                            <div class="mb-2">
                                <label class="form-label">Frequency (Hz)</label>
                                <input type="number" id="frequency" name="frequency" class="form-control" value="915.0" step="0.1">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Power (dBm)</label>
                                <input type="number" id="power" name="power" class="form-control" value="22">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Bandwidth (kHz)</label>
                                <input type="number" id="bandwidth" name="bandwidth" class="form-control" value="500.0" step="0.1">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Spreading Factor</label>
                                <input type="number" id="spreading_factor" name="spreading_factor" class="form-control" value="7" min="6" max="12">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Coding Rate</label>
                                <input type="number" id="coding_rate" name="coding_rate" class="form-control" value="5" min="5" max="8">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Preamble Length</label>
                                <input type="number" id="preamble" name="preamble" class="form-control" value="8">
                            </div>
                            <div class="mb-2 form-check">
                                <input type="checkbox" id="set_crc" name="set_crc" class="form-check-input" checked>
                                <label class="form-check-label">Enable CRC</label>
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Sync Word (hex)</label>
                                <input type="text" id="sync_word" name="sync_word" class="form-control" value="0xAB">
                            </div>
                            <button type="button" id="update-settings" class="btn btn-primary">Update Settings</button>
                        </form>
                    </div>
                </div>
            </div>
            
            <div class="col-md-8">
                <div class="card mb-3">
                    <div class="card-header">
                        CAN Message Reception
                        <div class="float-end">
                            <button id="start-receive" class="btn btn-success btn-sm">Start Receiving</button>
                            <button id="stop-receive" class="btn btn-danger btn-sm">Stop</button>
                            <button id="clear-messages" class="btn btn-secondary btn-sm">Clear</button>
                        </div>
                    </div>
                    <div class="card-body">
                        <div id="messages" class="message-container"></div>
                    </div>
                </div>
                
                <div class="card mb-3">
                    <div class="card-header">Statistics</div>
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-6">
                                <p>Messages Received: <span id="messages-count">0</span></p>
                                <p>CRC Errors: <span id="crc-errors">0</span></p>
                            </div>
                            <div class="col-md-6">
                                <p>Last RSSI: <span id="last-rssi">N/A</span> dBm</p>
                                <p>Last SNR: <span id="last-snr">N/A</span> dB</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        // Global variables
        let isConnected = false;
        let isReceiving = false;
        let messagePollingInterval = null;
        let messagesCount = 0;
        let crcErrorsCount = 0;
        
        // DOM Elements
        const portSelect = document.getElementById('port-select');
        const refreshPortsButton = document.getElementById('refresh-ports');
        const debugButton = document.getElementById('debug-button');
        const connectButton = document.getElementById('connect-button');
        const autodetectButton = document.getElementById('autodetect-button');
        const connectionStatus = document.getElementById('connection-status');
        const connectionInfo = document.getElementById('connection-info');
        const updateSettingsButton = document.getElementById('update-settings');
        const startReceiveButton = document.getElementById('start-receive');
        const stopReceiveButton = document.getElementById('stop-receive');
        const clearMessagesButton = document.getElementById('clear-messages');
        const messagesContainer = document.getElementById('messages');
        const messagesCountElement = document.getElementById('messages-count');
        const crcErrorsElement = document.getElementById('crc-errors');
        const lastRssiElement = document.getElementById('last-rssi');
        const lastSnrElement = document.getElementById('last-snr');
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            loadPorts();
            updateButtons();
            
            refreshPortsButton.addEventListener('click', loadPorts);
            debugButton.addEventListener('click', showDebugInfo);
            connectButton.addEventListener('click', toggleConnection);
            autodetectButton.addEventListener('click', autoDetectDevice);
            updateSettingsButton.addEventListener('click', updateSettings);
            startReceiveButton.addEventListener('click', startReceiving);
            stopReceiveButton.addEventListener('click', stopReceiving);
            clearMessagesButton.addEventListener('click', clearMessages);
        });
        
        // Functions
        async function loadPorts() {
            try {
                // Update UI to show loading state
                connectionInfo.textContent = 'Loading ports...';
                
                const response = await fetch('/api/ports');
                const data = await response.json();
                
                portSelect.innerHTML = '<option value="">Select Port</option>';
                
                if (data.success && data.ports && data.ports.length > 0) {
                    data.ports.forEach(port => {
                        const option = document.createElement('option');
                        option.value = port;
                        option.textContent = port;
                        portSelect.appendChild(option);
                    });
                    connectionInfo.textContent = `Found ${data.ports.length} port(s)`;
                } else {
                    // Handle error case with helpful message
                    connectionInfo.innerHTML = `<span class="text-danger">No serial ports found</span><br>`;
                    
                    if (data.error) {
                        connectionInfo.innerHTML += `Error: ${data.error}<br>`;
                    }
                    
                    if (data.help) {
                        connectionInfo.innerHTML += `${data.help}<br>`;
                    }
                    
                    // Add troubleshooting tips
                    connectionInfo.innerHTML += `
                        <small class="mt-2">
                            <strong>Troubleshooting:</strong><br>
                            - Make sure your device is connected<br>
                            - Try a different USB port<br>
                            - Restart the application<br>
                            - Check device drivers
                        </small>`;
                }
            } catch (error) {
                console.error('Error loading ports:', error);
                connectionInfo.innerHTML = `<span class="text-danger">Error loading ports: ${error.message}</span>`;
            }
        }
        
        async function showDebugInfo() {
            try {
                connectionInfo.textContent = 'Loading debug information...';
                
                const response = await fetch('/api/debug');
                const data = await response.json();
                
                // Create a modal dialog to show the debug info
                const modalDiv = document.createElement('div');
                modalDiv.className = 'modal fade';
                modalDiv.id = 'debugModal';
                modalDiv.setAttribute('tabindex', '-1');
                
                modalDiv.innerHTML = `
                    <div class="modal-dialog modal-lg">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title">Debug Information</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                            </div>
                            <div class="modal-body">
                                <pre class="border p-3 bg-light" style="max-height: 400px; overflow: auto;">${JSON.stringify(data, null, 2)}</pre>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
                            </div>
                        </div>
                    </div>
                `;
                
                // Add the modal to the page
                document.body.appendChild(modalDiv);
                
                // Show the modal
                const modal = new bootstrap.Modal(document.getElementById('debugModal'));
                modal.show();
                
                // Add event listener to remove the modal from the DOM when hidden
                document.getElementById('debugModal').addEventListener('hidden.bs.modal', function () {
                    document.body.removeChild(modalDiv);
                });
                
                connectionInfo.textContent = 'Debug information loaded';
            } catch (error) {
                console.error('Error getting debug info:', error);
                connectionInfo.textContent = `Error loading debug info: ${error.message}`;
            }
        }
        
        async function autoDetectDevice() {
            try {
                connectionInfo.textContent = 'Auto-detecting device...';
                
                const response = await fetch('/api/autodetect', {
                    method: 'POST',
                });
                
                const data = await response.json();
                
                if (data.success) {
                    // Mark as connected
                    isConnected = true;
                    connectionStatus.classList.remove('status-disconnected');
                    connectionStatus.classList.add('status-connected');
                    connectionInfo.innerHTML = `Auto-connected to ${data.port}<br>`;
                    connectButton.textContent = 'Disconnect';
                    
                    // Update port select dropdown to show the selected port
                    Array.from(portSelect.options).forEach(option => {
                        if (option.value === data.port) {
                            option.selected = true;
                        }
                    });
                    
                    // Update form with current settings
                    if (data.settings) {
                        document.getElementById('frequency').value = data.settings['Frequency'];
                        document.getElementById('power').value = data.settings['Power'];
                        document.getElementById('bandwidth').value = data.settings['Bandwidth'];
                        document.getElementById('spreading_factor').value = data.settings['Spreading Factor'];
                        document.getElementById('coding_rate').value = data.settings['Coding Rate'];
                        document.getElementById('preamble').value = data.settings['Preamble'];
                        document.getElementById('set_crc').checked = data.settings['CRC Enabled'];
                        document.getElementById('sync_word').value = data.settings['Sync Word'];
                    }
                    
                    // Show GPS info if available
                    if (data.gps) {
                        Object.entries(data.gps).forEach(([key, value]) => {
                            connectionInfo.innerHTML += `${key}: ${value}<br>`;
                        });
                    }
                } else {
                    connectionInfo.textContent = `Auto-detection failed: ${data.error}`;
                }
                
                updateButtons();
            } catch (error) {
                console.error('Error auto-detecting:', error);
                connectionInfo.textContent = 'Auto-detection failed';
            }
        }
        
        async function toggleConnection() {
            if (isConnected) {
                // Disconnect logic (in a real app you'd add an API endpoint for this)
                isConnected = false;
                connectionStatus.classList.remove('status-connected');
                connectionStatus.classList.add('status-disconnected');
                connectionInfo.textContent = 'Disconnected';
                connectButton.textContent = 'Connect';
                stopReceiving();
            } else {
                // Connect logic
                const selectedPort = portSelect.value;
                if (!selectedPort) {
                    connectionInfo.textContent = 'Please select a port';
                    return;
                }
                
                try {
                    const response = await fetch('/api/connect', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({ port: selectedPort }),
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        isConnected = true;
                        connectionStatus.classList.remove('status-disconnected');
                        connectionStatus.classList.add('status-connected');
                        connectionInfo.innerHTML = `Connected to ${selectedPort}<br>`;
                        connectButton.textContent = 'Disconnect';
                        
                        // Update form with current settings
                        if (data.settings) {
                            document.getElementById('frequency').value = data.settings['Frequency'];
                            document.getElementById('power').value = data.settings['Power'];
                            document.getElementById('bandwidth').value = data.settings['Bandwidth'];
                            document.getElementById('spreading_factor').value = data.settings['Spreading Factor'];
                            document.getElementById('coding_rate').value = data.settings['Coding Rate'];
                            document.getElementById('preamble').value = data.settings['Preamble'];
                            document.getElementById('set_crc').checked = data.settings['CRC Enabled'];
                            document.getElementById('sync_word').value = data.settings['Sync Word'];
                        }
                        
                        // Show GPS info if available
                        if (data.gps) {
                            Object.entries(data.gps).forEach(([key, value]) => {
                                connectionInfo.innerHTML += `${key}: ${value}<br>`;
                            });
                        }
                    } else {
                        connectionInfo.textContent = `Connection error: ${data.error}`;
                    }
                } catch (error) {
                    console.error('Error connecting:', error);
                    connectionInfo.textContent = 'Connection failed';
                }
            }
            
            updateButtons();
        }
        
        async function updateSettings() {
            if (!isConnected) {
                alert('Please connect to a device first');
                return;
            }
            
            const formData = {
                frequency: parseFloat(document.getElementById('frequency').value),
                power: parseInt(document.getElementById('power').value),
                bandwidth: parseFloat(document.getElementById('bandwidth').value),
                spreading_factor: parseInt(document.getElementById('spreading_factor').value),
                coding_rate: parseInt(document.getElementById('coding_rate').value),
                preamble: parseInt(document.getElementById('preamble').value),
                set_crc: document.getElementById('set_crc').checked,
                sync_word: document.getElementById('sync_word').value,
            };
            
            try {
                const response = await fetch('/api/settings', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(formData),
                });
                
                const data = await response.json();
                
                if (data.success) {
                    alert('Settings updated successfully');
                } else {
                    alert(`Error updating settings: ${data.error}`);
                }
            } catch (error) {
                console.error('Error updating settings:', error);
                alert('Error updating settings');
            }
        }
        
        async function startReceiving() {
            if (!isConnected) {
                alert('Please connect to a device first');
                return;
            }
            
            try {
                const response = await fetch('/api/receive', {
                    method: 'POST',
                });
                
                const data = await response.json();
                
                if (data.success) {
                    isReceiving = true;
                    
                    // Start polling for messages
                    messagePollingInterval = setInterval(fetchMessages, 500);
                    
                    updateButtons();
                } else {
                    alert(`Error starting receiver: ${data.error}`);
                }
            } catch (error) {
                console.error('Error starting receiver:', error);
                alert('Error starting receiver');
            }
        }
        
        async function stopReceiving() {
            if (!isReceiving) return;
            
            try {
                // Clear polling interval
                if (messagePollingInterval) {
                    clearInterval(messagePollingInterval);
                    messagePollingInterval = null;
                }
                
                const response = await fetch('/api/stop_receive', {
                    method: 'POST',
                });
                
                const data = await response.json();
                
                if (data.success) {
                    isReceiving = false;
                    updateButtons();
                } else {
                    alert(`Error stopping receiver: ${data.error}`);
                }
            } catch (error) {
                console.error('Error stopping receiver:', error);
                alert('Error stopping receiver');
            }
        }
        
        async function fetchMessages() {
            try {
                const response = await fetch('/api/messages');
                const data = await response.json();
                
                if (data.messages && data.messages.length > 0) {
                    data.messages.forEach(message => {
                        displayMessage(message);
                        
                        messagesCount++;
                        if (message.crc_error) {
                            crcErrorsCount++;
                        }
                        
                        // Update stats
                        messagesCountElement.textContent = messagesCount;
                        crcErrorsElement.textContent = crcErrorsCount;
                        lastRssiElement.textContent = message.rssi.toFixed(2);
                        lastSnrElement.textContent = message.snr.toFixed(2);
                    });
                }
            } catch (error) {
                console.error('Error fetching messages:', error);
            }
        }
        
        function displayMessage(message) {
            const messageElement = document.createElement('div');
            messageElement.className = 'message-item';
            
            // Message header
            const header = document.createElement('div');
            header.innerHTML = `
                <strong>Message: ${message.message_name}</strong> (ID: 0x${message.can_id.toString(16)})
                <span class="float-end">
                    RSSI: ${message.rssi.toFixed(2)} dBm | 
                    SNR: ${message.snr.toFixed(2)} dB |
                    ${message.crc_error ? '<span class="text-danger">CRC Error</span>' : '<span class="text-success">CRC OK</span>'}
                </span>
            `;
            messageElement.appendChild(header);
            
            // Raw data
            const rawData = document.createElement('div');
            rawData.className = 'small text-muted';
            rawData.textContent = `Raw Data: ${message.raw_data}`;
            messageElement.appendChild(rawData);
            
            // Signal table
            if (Object.keys(message.signals).length > 0) {
                const table = document.createElement('table');
                table.className = 'table table-sm signal-table';
                
                // Table header
                const thead = document.createElement('thead');
                thead.innerHTML = `
                    <tr>
                        <th>Signal</th>
                        <th>Value</th>
                        <th>Unit</th>
                    </tr>
                `;
                table.appendChild(thead);
                
                // Table body
                const tbody = document.createElement('tbody');
                
                Object.entries(message.signals).forEach(([name, value]) => {
                    // Check if the value has a unit (indicated by a string with format "123 unit")
                    let displayValue = value;
                    let unit = '';
                    
                    if (typeof value === 'string' && value.includes(' ')) {
                        const parts = value.split(' ');
                        displayValue = parts[0];
                        unit = parts.slice(1).join(' ');
                    }
                    
                    const row = document.createElement('tr');
                    row.innerHTML = `
                        <td>${name}</td>
                        <td>${displayValue}</td>
                        <td>${unit}</td>
                    `;
                    tbody.appendChild(row);
                });
                
                table.appendChild(tbody);
                messageElement.appendChild(table);
            } else {
                const noSignals = document.createElement('div');
                noSignals.className = 'small text-muted';
                noSignals.textContent = 'No signals decoded';
                messageElement.appendChild(noSignals);
            }
            
            // Add to container (at the top)
            messagesContainer.insertBefore(messageElement, messagesContainer.firstChild);
        }
        
        function clearMessages() {
            messagesContainer.innerHTML = '';
            messagesCount = 0;
            crcErrorsCount = 0;
            messagesCountElement.textContent = '0';
            crcErrorsElement.textContent = '0';
            lastRssiElement.textContent = 'N/A';
            lastSnrElement.textContent = 'N/A';
        }
        
        function updateButtons() {
            // Connection related buttons
            refreshPortsButton.disabled = isConnected;
            portSelect.disabled = isConnected;
            autodetectButton.disabled = isConnected;
            
            // Settings buttons
            updateSettingsButton.disabled = !isConnected;
            
            // Receive buttons
            startReceiveButton.disabled = !isConnected || isReceiving;
            stopReceiveButton.disabled = !isReceiving;
            clearMessagesButton.disabled = !isConnected;
        }
    </script>
</body>
</html>""")
        except Exception as e:
            logger.error(f"Error creating template file: {e}")

    # Start the Flask app
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)


if __name__ == "__main__":
    run_app()
