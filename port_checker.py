# save as port_checker.py in the root directory
import serial.tools.list_ports


def main():
    print("Checking for available serial ports...")
    ports = list(serial.tools.list_ports.comports())

    if not ports:
        print("No serial ports detected!")
        print("\nPossible reasons:")
        print("1. No serial devices are connected")
        print("2. Permissions issue (especially on Linux/Mac)")
        print("3. PySerial installation problem")
        print("\nTry running with administrator/sudo privileges")
    else:
        print(f"Found {len(ports)} ports:")
        for port in ports:
            print(f"- {port.device}: {port.description}")


if __name__ == "__main__":
    main()
