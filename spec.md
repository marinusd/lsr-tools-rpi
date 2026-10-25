# Program Specification: Raspberry Pi 3b ECU & GPS Data Hub

## 1. Overview & Goal
The objective is to build a low-overhead, memory-optimized telemetry application running on a headless **Raspberry Pi 3b (1GB RAM)**. The system acts as a real-time data ingestion bridge for a F.A.S.T. Engine Control Unit (ECU) and a USB-connected GPS receiver. It merges both data streams into unified daily CSV files with GPS telemetry positioned firstmost, serves them over a lean web UI directory listing, generates on-demand server-side chart graphs, and offers systemd process mechanics.

---

## 2. Tech Stack & Environment
* **Platform Target:** Raspberry Pi 3b (ARMv7, 1GB RAM) running Raspberry Pi OS Lite (Headless Debian 12).
* **Programming Runtime:** Python 3.11+
* **Dependencies (Production Core):**
  * `pyserial`: Interface communication handling for both the ECU and GPS USB serial connections.
  * `flask`: Lightweight, zero-asset-framework backend micro-server.
  * `matplotlib`: Server-side graph output generation.
* **Environment Constraints:** 
  * The system must strictly enforce zero runtime asset compilation and minimal layout overhead.
  * Matplotlib must run headless: `matplotlib.use('Agg')` must be called before importing pyplot to prevent GUI memory leaks.

---

## 3. Architecture & Binary Mapping Layout

### A. Directory & Folder Hierarchy
Logs and metrics are recorded using a date/hour file path convention driven by the authoritative GPS clock target:
```text
/var/log/ecu_hub/data/
└── YYYY-MM-DD/
    └── HH/
        ├── log_MMSS.csv
        └── plot_MMSS_[Metrics].png
```

### B. Frame Synchronizer & Packet Specification
* **ECU Serial Link Configuration:** Baud rate fixed strictly to **9600 bps**, 8 Data Bits, 1 Stop Bit, No Parity.
* **GPS Serial Link Configuration:** Typically **9600 bps** or **115200 bps** (configurable via spec parameter), reading standard NMEA-0183 sentences.
* **Unified Output CSV Schema (GPS Firstmost):**
  The collector must stitch the asynchronous streams together by caching the most recent valid GPS lock and appending the incoming 10Hz ECU data frames. The output schema order must strictly be:
  1. **GPS_Timestamp**: ISO-8601 formatted time string (`YYYY-MM-DDTHH:MM:SS.FFFZ`) parsed from GPS.
  2. **GPS_Latitude**: Decimal degrees representation.
  3. **GPS_Longitude**: Decimal degrees representation.
  4. **GPS_Speed_MPH**: Ground speed calculated directly from GPS knots ($1 \text{ knot} = 1.15078 \text{ mph}$).
  5. **TPS (%)**: Throttle Position Sensor from ECU.
  6. **RPM**: Engine Revolutions per Minute from ECU.
  7. **Battery (V)**: System supply voltage from ECU.
  8. **Actual A/F**: Wideband oxygen sensor tracking from ECU.
  9. **Target A/F**: Base tuning profile reference from ECU.
  10. **O2 Corr. (%)**: Closed-loop fueling offsets from ECU.
  11. **MAP (PSIA)**: Manifold Absolute Pressure from ECU (Converted from kPa).
  12. **Air Temp (*F)**: Intake temperature readings (Analog Count Conversion).
  13. **Coolant (*F)**: Engine coolant tracking (Analog Count Conversion).
  14. **Spark (*BTDC)**: Ignition timing degrees from ECU.
  15. **Inj. DC (%)**: Injector duty cycle tracking from ECU.

---

## 4. Functional Requirements

### Tier 1: Data Ingestion & GPS Synchronization (`collector.py`)
* **Authoritative Clock Synchronization:** Since the Raspberry Pi 3b lacks an internal battery-backed Real-Time Clock (RTC), the collector must parse the first valid `$GPRMC` or `$ZDA` sentence from the GPS stream, validate the time lock, and issue a privileged system call (e.g., `timedatectl` or `date -s`) to set the system clock before writing logs.
* **Asynchronous Multi-Threaded Read Loop:** 
  * Thread A continuously monitors the GPS USB serial interface port, extracting lat/long coordinates and speed. It handles NMEA conversions, notably multiplying knots data fields by `1.150779` to derive instantaneous integer or float **MPH values**.
  * Thread B manages the high-frequency ECU input buffer, matching incoming frames with the latest thread-safe memory snapshot of current GPS spatial tracking data.
* **Analog Temperature Processing:** Maps raw analog count integers (0-255) to precise Fahrenheit parameters using standard lookup parameters or NTC thermistor calibration tracking.
* **Dynamic Flushing:** Unified records are buffered dynamically and flushed to disk targets every 1–2 seconds to prevent file corruption during engine power-downs.

### Tier 2: File Delivery Web Interface (`server.py`)
* Exposes a fast directory index mapping `/var/log/ecu_hub/data/`.
* Renders a native anchor tree list layout allowing operators to seamlessly navigate dates, browse specific hourly directory blocks, and download target merged `.csv` files.

### Tier 3: Visualization Generator
* Provides a selection interface allowing users to select a log, choose multiple variable fields (e.g., graphing engine `RPM` alongside vehicle `GPS_Speed_MPH`), and trigger plot graphics rendering.
* **Server-Side Render Boundary:** The daemon invokes matplotlib to output static plot graphics directly inside the active log subdirectory next to the target `.csv` stream.
* **OOM Prevention Loop:** Explicitly handles garbage cleanup inside render loops using `plt.close(fig)` to protect the 1GB RAM system.

### Tier 4: Mock Generation & Simulation Layer (`mock_serial.py`)
* Activated globally using a programmatic execution flag (`python collector.py --mock`).
* Emulates both hardware serial interfaces simultaneously using internal background simulator loops.
* Feeds realistic, moving metrics strings:
  * Telemetry cycles stepping RPM through normal rev bands.
  * Spatial route generation loop modifying latitude and longitude over an imaginary driving path, calculating step velocities to cleanly mock logical non-zero `GPS_Speed_MPH` data fields.

---

## 5. System Control & Linux Systemd Automation

The process management deployment leverages two systemd configuration engines:
* `ecu-collector.service`: Starts at system boot, initializes the GPS serial link first to synchronize time parameters, and begins logging data rows.
* `ecu-web.service`: Serves the application directory interface on port `8080`.
The web interface accesses service toggles securely by using explicit sudo authorization rule mappings written inside `/etc/sudoers.d/ecu_control`.

---

## 6. Development Boundaries & Guardrails

* **ALWAYS:** Position the 4 core GPS elements firstmost inside the CSV file outputs to ensure data formatting uniformity across scripts.
* **ALWAYS:** Implement thread-safe structures (such as Python's `threading.Lock` or atomic object assignments) when copying telemetry between the GPS reading loops and the primary ECU row compilation loops.
* **NEVER:** Block the main ingestion routine if the GPS receiver temporarily drops satellite tracking signals. The script must fallback gracefully to appending the last known coordinate coordinates or blank fields, maintaining the high-speed 10Hz ECU data collection frequency.

---

## 7. Acceptance Validation Criteria

An execution workflow is valid only when matching these specific checks:
1. Executing `python collector.py --mock` accurately instantiates simultaneous generation tasks, producing merged CSV logs with columns matching the structural order layout definitions.
2. The data output file's path dynamically updates if the authoritative GPS timestamp calculation changes hour markers during long telemetry collection windows.
3. Graph layout requests processing `GPS_Speed_MPH` vs engine factors render smoothly, verifying that active memory usage traces stay flat over sequential rendering checks.

