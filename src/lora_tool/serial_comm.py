import serial
from serial.tools import list_ports
import platform
import os
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lora_tool.serial")


def list_serial_ports():
    """
    Return a list of available serial port device names with enhanced debugging.

    Returns:
        List of available serial port device names.
    """
    try:
        ports = [port.device for port in list_ports.comports()]
        logger.info(f"Found {len(ports)} serial ports: {ports}")
        return ports
    except Exception as e:
        logger.error(f"Error listing serial ports: {e}")
        return []


def get_default_serial_port():
    """
    Try to find a likely LoRa device port.

    Returns:
        Most likely serial port or None if not found.
    """
    for port in list_ports.comports():
        # Look for common USB-serial converters often used with LoRa devices
        if any(
            identifier in port.description.lower()
            for identifier in [
                "cp210",
                "ch340",
                "ft232",
                "usb",
                "serial",
                "uart",
                "lora",
            ]
        ):
            return port.device
    return None


def open_serial_port(port_name, baudrate=115200, timeout=1, attempts=3):
    """
    Open and return a serial connection with multiple attempts.

    Args:
        port_name: The name of the serial port to open.
        baudrate: The baud rate for the serial connection (default is 115200).
        timeout: The timeout for the serial connection (default is 1 second).
        attempts: Number of connection attempts before giving up.

    Returns:
        An open serial connection or raises an exception.
    """
    logger.info(f"Attempting to open serial port {port_name}")

    for attempt in range(attempts):
        try:
            # Close port if it's already open (happens on some systems)
            try:
                temp_ser = serial.Serial(port_name)
                temp_ser.close()
                time.sleep(0.5)  # Wait for port to release
            except:
                pass

            # Now try to open it properly
            ser = serial.Serial(port_name, baudrate, timeout=timeout)
            logger.info(f"Successfully opened {port_name}")
            return ser
        except serial.SerialException as e:
            logger.warning(f"Attempt {attempt + 1}/{attempts} failed: {e}")
            if attempt == attempts - 1:  # Last attempt
                # Provide OS-specific guidance in the error
                os_type = platform.system()
                if os_type == "Linux":
                    msg = f"Error opening {port_name}: {e}. Try 'sudo chmod 666 {port_name}' or add user to dialout group."
                elif os_type == "Windows":
                    msg = f"Error opening {port_name}: {e}. Check if another application is using the port."
                else:
                    msg = f"Error opening {port_name}: {e}"

                logger.error(msg)
                raise serial.SerialException(msg)
            time.sleep(1)  # Wait before retry
