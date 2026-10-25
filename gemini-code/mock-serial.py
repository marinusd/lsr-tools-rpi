# This module provides a virtual simulation layer. It generates realistic moving telemetry attributes (RPM acceleration maps, fluctuating throttle points, stable battery voltage loops) wrapped inside precise, hex-encoded binary packet signatures.

#!/usr/bin/env python3
import time
import math
import struct

class MockSerialPort:
    """Simulates a physical USB-serial line connection streaming binary payload strings."""
    def __init__(self):
        self.step = 0
        self.header = b'\xaa\x45\xe0' # ª E à Match packet frame headers exactly

    def generate_synthetic_telemetry(self):
        """Generates dynamic moving values to cleanly stress-test parsing lookups."""
        self.step += 1
        
        # Simulates a smooth variable engine acceleration ramp sweep curve (700 to 5800 RPM)
        rpm = int(3250 + (2550 * math.sin(self.step * 0.05)))
        
        # TPS tracks directly with engine load demands
        tps = int(50 + (45 * math.sin(self.step * 0.05))) if rpm > 3000 else int(15 + (5 * math.cos(self.step * 0.1)))
        tps = max(0, min(100, tps))
        
        # Automotive runtime metrics mapping values as raw unscaled integers
        battery_raw = int(138 + (2 * math.sin(self.step * 0.01))) # ~13.8V scalar
        actual_af_raw = int(147 + (4 * math.sin(self.step * 0.08))) # ~14.7 AFR scaling
        target_af_raw = 147 # Steady state target state
        o2_corr = int(100 + (10 * math.cos(self.step * 0.05)))
        
        # Raw KPa maps to pass through conversion calculations smoothly
        map_raw = int(40 + (60 * (tps / 100.0)))
        
        # Analog ADC step count simulation (0-255 inputs)
        air_temp_counts = int(90 + (5 * math.sin(self.step * 0.002)))
        coolant_counts = int(160 + (3 * math.sin(self.step * 0.001)))
        
        spark = int(22 + (12 * (rpm / 6000.0)))
        inj_dc = int((tps * 0.8) + (rpm * 0.003))

        # Re-pack data back into exactly 31 trailing byte slots to match alignment requirements
        payload = struct.pack(
            ">B H B B B B B B B B B 11x",
            tps, rpm, battery_raw, actual_af_raw, target_af_raw,
            o2_corr, map_raw, air_temp_counts, coolant_counts, spark, inj_dc
        )
        
        return self.header + payload

    def read(self, size=64):
        """Simulates serial read updates by spacing packet injections accurately."""
        time.sleep(0.1)  # Simulates streaming telemetry frequencies at 10Hz cycles
        return self.generate_synthetic_telemetry()

    def close(self):
        """Clean closing termination interface tracking."""
        print("[INFO] Mock Serial Interface link connection disconnected securely.")

