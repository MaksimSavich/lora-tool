# lora_tool/webapp.py
import os
import threading
import time
from flask import Flask, request, jsonify, render_template
from lora_tool.serial_comm import list_serial_ports, open_serial_port
from lora_tool.lora_device import LoRaDevice
from lora_tool.can_decoder import CANDecoder
import packet_pb2 as packet_pb2

# Initialize the Flask application
app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
)

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
can_decoder = CANDecoder(dbc_path)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports", methods=["GET"])
def get_ports():
    ports = list_serial_ports()
    return jsonify({"ports": ports})


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
        return jsonify({"success": False, "error": str(e)})


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

        return jsonify({"success": True})
    except Exception as e:
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

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/messages", methods=["GET"])
def get_messages():
    global message_queue

    with lock:
        messages = message_queue.copy()
        message_queue = []

    return jsonify({"messages": messages})


def receive_data_thread(stop_event):
    """Background thread for receiving data."""
    global lora_device, message_queue, can_decoder, is_receiving

    try:

        def packet_callback(packet):
            if packet.type == packet_pb2.PacketType.LOG:
                log = packet.log

                # Process the CAN message from the payload
                can_data = can_decoder.decode_payload(log.payload)

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
            return False  # Continue processing

        # Process packets until stopped
        while not stop_event.is_set():
            if lora_device and lora_device.ser and lora_device.ser.in_waiting > 0:
                lora_device.process_serial_packets(
                    packet_callback, exit_on_condition=False
                )
            time.sleep(0.1)
    except Exception as e:
        print(f"Error in receive thread: {e}")
    finally:
        if lora_device and lora_device.ser:
            lora_device.change_state(packet_pb2.State.STANDBY)
        is_receiving = False


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

    # Write the template file
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if not os.path.exists(template_path):
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
                        <button id="connect-button" class="btn btn-primary mb-2">Connect</button>
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
        const connectButton = document.getElementById('connect-button');
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
            connectButton.addEventListener('click', toggleConnection);
            updateSettingsButton.addEventListener('click', updateSettings);
            startReceiveButton.addEventListener('click', startReceiving);
            stopReceiveButton.addEventListener('click', stopReceiving);
            clearMessagesButton.addEventListener('click', clearMessages);
        });
        
        // Functions
        async function loadPorts() {
            try {
                const response = await fetch('/api/ports');
                const data = await response.json();
                
                portSelect.innerHTML = '<option value="">Select Port</option>';
                data.ports.forEach(port => {
                    const option = document.createElement('option');
                    option.value = port;
                    option.textContent = port;
                    portSelect.appendChild(option);
                });
                
                if (data.ports.length === 0) {
                    connectionInfo.textContent = 'No serial ports available';
                }
            } catch (error) {
                console.error('Error loading ports:', error);
                connectionInfo.textContent = 'Error loading ports';
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
                    method:
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

    # Start the Flask app
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)


if __name__ == "__main__":
    run_app()
