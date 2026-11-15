#!/usr/bin/env python3
import os
import sys
import time
import struct
import argparse
from datetime import datetime

# Binary Frame Constants based on ECU log patterns
FRAME_HEADER = b'\xaa\x45\xe0'  # ª E à Looked up from physical log structure
PACKET_SIZE = 34               # 3-byte header + 22 bytes payload data + 9 bytes trailing alignment checks

# Analog Temperature Counts Conversion to Fahrenheit lookup table
# Maps raw ADC step counts (0-255) to real engineering values
def convert_analog_temp(raw_count):
    # Clamped linear approximation modeling typical automotive NTC thermistor metrics
    # Replace with precise engineering calibration curve if necessary
    clamped = max(0, min(255, raw_count))
    return int(40 + (clamped * 0.784))

def setup_directory_tree():
    """Generates the file paths ordered hierarchically by date/hour"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    hour_str = now.strftime("%H")
    
    dir_path = os.path.join("/var/log/ecu_hub/data", date_str, hour_str)
    os.makedirs(dir_path, exist_ok=True)
    
    filename = f"log_{now.strftime('%M%S')}.csv"
    return os.path.join(dir_path, filename)

def parse_ecu_packet(payload_bytes):
    """
    Unpacks fixed-width fields sequentially from the binary streaming sequence.
    Format definitions match variables observed in document mappings.
    """
    try:
        # Format string map breakdown:
        # B: TPS (1B), H: RPM (2B), B: Battery (1B), B: Actual A/F (1B), B: Target A/F (1B)
        # B: O2 Corr (1B), B: MAP (1B), B: Air Temp (1B), B: Coolant (1B), B: Spark (1B), B: Inj DC (1B)
        # Followed by trailing padding variables parsed cleanly to ensure alignment checks
        data = struct.unpack(">B H B B B B B B B B B 11x", payload_bytes)
        
        tps = data[0]
        rpm = data[1]
        battery = data[2] / 10.0  # Scalar normalization for voltage precision
        actual_af = data[3] / 10.0
        target_af = data[4] / 10.0
        o2_corr = data[5]
        
        # Unit Conversion: Translate KPa readings to standard PSIA ranges
        map_psia = round(data[6] * 0.145038, 1) 
        
        # Analog count lookups
        air_temp = convert_analog_temp(data[7])
        coolant_temp = convert_analog_temp(data[8])
        
        spark = data[9]
        inj_dc = data[10]

        return [tps, rpm, battery, actual_af, target_af, o2_corr, map_psia, air_temp, coolant_temp, spark, inj_dc]
    except struct.error:
        return None

def main():
    parser = argparse.ArgumentParser(description="ECU Telemetry Stream Ingestion Engine")
    parser.add_argument("--mock", action="store_true", help="Launch runtime in Mock Generation Mode")
    args = parser.parse_args()

    if args.mock:
        print("[INFO] Starting Collector in Mock Serial Loopback Mode...")
        import mock_serial
        serial_connection = mock_serial.MockSerialPort()
    else:
        import serial
        print("[INFO] Binding Physical Serial Communication Layer (9600 Bps)...")
        serial_connection = serial.Serial('/dev/ttyUSB0', 9600, timeout=1)

    csv_path = setup_directory_tree()
    print(f"[INFO] Ingestion initialized. Active logging output track: {csv_path}")
    
    headers = ["TPS(%)", "RPM", "Battery(V)", "Actual A/F", "Target A/F", "O2 Corr(%)", "MAP(PSIA)", "Air Temp(*F)", "Coolant(*F)", "Spark(*BTDC)", "Inj DC(%)"]
    
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write(",".join(headers) + "\n")

    byte_buffer = b""
    
    try:
        while True:
            byte_buffer += serial_connection.read(64)
            
            # Continuously scan for the 3-byte synchronization header block
            header_index = byte_buffer.find(FRAME_HEADER)
            if header_index != -1:
                # Ensure the full packet boundary is inside the reading buffer memory
                if len(byte_buffer) >= header_index + PACKET_SIZE:
                    packet = byte_buffer[header_index:header_index + PACKET_SIZE]
                    payload = packet[len(FRAME_HEADER):] # Strip header pattern
                    
                    parsed_metrics = parse_ecu_packet(payload)
                    if parsed_metrics:
                        csv_row = ",".join(map(str, parsed_metrics))
                        
                        # Dynamic validation check: Refresh tree tracking paths dynamically across hour updates
                        current_csv = setup_directory_tree()
                        with open(current_csv, 'a', encoding='utf-8') as f:
                            f.write(csv_row + "\n")
                            f.flush() # Force immediate IO serialization safety
                    
                    # Truncate buffer to slice out the fully handled transaction frame
                    byte_buffer = byte_buffer[header_index + PACKET_SIZE:]
                else:
                    # Incomplete packet, loop again to fetch remaining trailing bytes
                    time.sleep(0.01)
            else:
                # Retain only trailing bytes to guard against partial window fragmentation issues
                if len(byte_buffer) > len(FRAME_HEADER):
                    byte_buffer = byte_buffer[-len(FRAME_HEADER):]
                time.sleep(0.01)
                
    except KeyboardInterrupt:
        print("\n[INFO] Data logging terminated cleanly by administrator intervention.")
    finally:
        serial_connection.close()

if __name__ == "__main__":
    main()

