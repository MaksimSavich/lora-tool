# lora_tool/json_utils.py
import json
import logging
from flask import Flask

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lora_tool.json_utils")


class CustomJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that can handle special types from cantools and other libraries.
    This ensures Flask can properly serialize responses with CAN message data.
    """

    def default(self, obj):
        # Handle NamedSignalValue objects (from cantools)
        if hasattr(obj, "name") and hasattr(obj, "value"):
            # This is likely a named value (enum) from cantools
            return f"{obj.value} ({obj.name})"

        # Handle bytes objects
        if isinstance(obj, bytes):
            return obj.hex()

        # Handle sets
        if isinstance(obj, set):
            return list(obj)

        # Handle other iterables (but not strings/bytes/dicts)
        try:
            if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, dict)):
                return list(obj)
        except:
            pass

        # Let the base class handle everything else
        try:
            return super().default(obj)
        except TypeError as e:
            # Log the error and return a string representation as a fallback
            logger.warning(f"Unserializable object: {type(obj).__name__}: {str(e)}")
            return f"[Unserializable {type(obj).__name__}]"


def apply_custom_json_encoder(app):
    """
    Apply the custom JSON encoder to a Flask application.
    Works with both older and newer versions of Flask.

    Args:
        app: The Flask application instance
    """
    try:
        # First try the newer Flask approach
        app.json = {"cls": CustomJSONEncoder}
        logger.info("Applied custom JSON encoder using new Flask API")
    except (AttributeError, TypeError):
        try:
            # Fall back to the older approach
            app.json_encoder = CustomJSONEncoder
            logger.info("Applied custom JSON encoder using legacy Flask API")
        except Exception as e:
            logger.error(f"Failed to apply custom JSON encoder: {e}")

            # Last resort: monkey patch Flask's json module
            try:
                import flask.json

                original_dumps = flask.json.dumps

                def patched_dumps(*args, **kwargs):
                    kwargs["cls"] = kwargs.get("cls", CustomJSONEncoder)
                    return original_dumps(*args, **kwargs)

                flask.json.dumps = patched_dumps
                logger.info("Applied custom JSON encoder using monkey patch")
            except Exception as e:
                logger.error(f"All methods to apply custom JSON encoder failed: {e}")
