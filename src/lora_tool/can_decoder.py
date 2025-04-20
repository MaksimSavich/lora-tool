# lora_tool/can_decoder.py
import struct
import cantools


class CANDecoder:
    def __init__(self, dbc_path):
        """Initialize the CAN decoder with a DBC file."""
        try:
            self.db = cantools.database.load_file(dbc_path)
            self.message_by_id = {msg.frame_id: msg for msg in self.db.messages}
        except Exception as e:
            print(f"Error loading DBC file: {e}")
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
            return {"error": "Payload too short"}

        # Extract CAN ID (first 4 bytes)
        # Based on the format described where bytes 0-3 represent the CAN ID
        # with byte 0 being the most significant byte
        can_id = int.from_bytes(payload[:4], byteorder="big")

        # Extract data (remaining bytes)
        data = payload[4:]

        result = {"can_id": can_id, "data": data.hex(), "signals": {}}

        # Find the message in the DBC file by ID
        if self.db:
            for message in self.db.messages:
                if message.frame_id == can_id:
                    result["message_name"] = message.name

                    # Decode the message
                    try:
                        decoded = self.db.decode_message(can_id, data)

                        # Process each signal to format it nicely
                        for signal_name, signal_value in decoded.items():
                            # Round floating point values
                            if isinstance(signal_value, float):
                                signal_value = round(signal_value, 2)

                            # Get the signal definition to find the unit
                            for signal in message.signals:
                                if signal.name == signal_name:
                                    if signal.unit:
                                        signal_value = f"{signal_value} {signal.unit}"
                                    break

                            result["signals"][signal_name] = signal_value
                    except Exception as e:
                        result["decode_error"] = str(e)
                    break
            else:
                result["message_name"] = f"Unknown (0x{can_id:X})"

        return result
