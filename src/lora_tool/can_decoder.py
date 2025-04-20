# lora_tool/can_decoder.py
import struct
import logging
import traceback
import cantools

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lora_tool.can_decoder")


class CANDecoder:
    def __init__(self, dbc_path):
        """Initialize the CAN decoder with a DBC file."""
        try:
            self.db = cantools.database.load_file(dbc_path)
            self.message_by_id = {msg.frame_id: msg for msg in self.db.messages}
            logger.info(f"Successfully loaded DBC file: {dbc_path}")
            logger.info(f"Found {len(self.db.messages)} messages in DBC file")
        except Exception as e:
            logger.error(f"Error loading DBC file: {e}")
            self.db = None
            self.message_by_id = {}

    def decode_payload(self, payload):
        """
        Decode a CAN message from a payload.

        The payload format is:
        - First 4 bytes: CAN ID
        - Remaining bytes: CAN data (up to 8 bytes)

        Returns a dictionary with the decoded information.
        """
        if len(payload) < 4:
            logger.warning("Payload too short to decode CAN message")
            return {"error": "Payload too short"}

        # Extract CAN ID (first 4 bytes)
        can_id = int.from_bytes(payload[:4], byteorder="big")

        # Extract data (remaining bytes)
        data = payload[4:]

        result = {"can_id": can_id, "data": data.hex(), "signals": {}}

        # Find the message in the DBC file by ID
        if self.db:
            message = None
            for msg in self.db.messages:
                if msg.frame_id == can_id:
                    message = msg
                    break

            if message:
                result["message_name"] = message.name
                logger.info(f"Decoding message: {message.name} (ID: 0x{can_id:X})")

                # Decode the message
                try:
                    # Try to decode the message
                    decoded = self.db.decode_message(can_id, data)

                    # Process each signal to format it nicely
                    for signal_name, signal_value in decoded.items():
                        # Convert NamedSignalValue to primitive type
                        if hasattr(signal_value, "name") and hasattr(
                            signal_value, "value"
                        ):
                            # This is a NamedSignalValue, extract the name and value
                            value_name = signal_value.name
                            value = signal_value.value
                            signal_value = f"{value} ({value_name})"
                            logger.debug(
                                f"Converted NamedSignalValue: {signal_name}={value}({value_name})"
                            )
                        # Round floating point values
                        elif isinstance(signal_value, float):
                            signal_value = round(signal_value, 2)

                        # Get the signal definition to find the unit
                        for signal in message.signals:
                            if signal.name == signal_name:
                                if signal.unit:
                                    # Only add unit if not already in the signal value
                                    if (
                                        not isinstance(signal_value, str)
                                        or signal.unit not in signal_value
                                    ):
                                        signal_value = f"{signal_value} {signal.unit}"
                                break

                        result["signals"][signal_name] = signal_value
                except Exception as e:
                    error_msg = f"Error decoding message: {str(e)}"
                    logger.error(error_msg)
                    logger.error(traceback.format_exc())
                    result["decode_error"] = error_msg

                    # Even though decoding failed, let's try to generate a human-readable
                    # representation of the raw data for debugging
                    try:
                        hex_data = " ".join(f"{b:02X}" for b in data)
                        result["raw_hex"] = hex_data
                        logger.info(
                            f"Raw data for failed message (ID: 0x{can_id:X}): {hex_data}"
                        )
                    except:
                        pass
            else:
                result["message_name"] = f"Unknown (0x{can_id:X})"
                logger.warning(f"Unknown message ID: 0x{can_id:X}")

        return result
