# lora_tool/web_app.py
import sys
import os
from flask import Flask
from flask.cli import main

# Add the directory containing the package to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import the application module
from lora_tool.webapp import run_app

if __name__ == "__main__":
    run_app()
