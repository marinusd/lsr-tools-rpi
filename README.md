# lsr-tools-rpi

F.A.S.T. Engine Control Unit Logging

Land speed racing tools for the Raspberry Pi: a data logger for a F.A.S.T.
engine control unit (ECU), built to run unattended on a Raspberry Pi 3b in the
car. It takes the place of a Windows laptop running C-Com WP for real-time
logging, and adds GPS position and speed to every row, so each run has engine
data and vehicle speed in one file.

## What it does

- **Polls the ECU ten times a second** over a USB serial adapter and records
  the engine values you choose (RPM, throttle, air/fuel ratio, battery voltage
  and so on).
- **Adds GPS data to each row**: UTC time, latitude, longitude and speed in
  mph, from a USB GPS receiver. The Pi has no clock battery, so GPS also sets
  the system clock.
- **Writes one CSV file per logging session**, grouped into a folder per day
  (`YYYY-MM-DD/log_HHMM.csv`). Rows are forced to disk every second, so a
  power cut loses very little.
- **Starts by itself at boot.** If the ECU is off, it retries every 60 seconds
  and begins logging when the ECU answers. If the ECU drops out mid-session,
  it closes the file and starts a new one when the ECU returns.
- **Takes its channel list from a template.** `CHANNEL_TEMPLATE.txt` holds the
  normal channels and their column order as editable text. A log saved by
  C-Com WP also works as a template: the logger reads the channel names,
  addresses and scaling from its header.

## How it talks to the ECU

The ECU does not stream data. The logger writes a list of memory addresses
into the ECU (the "poll table"), then asks for the values at those addresses
every 100 ms. The protocol was worked out from a USB capture of a C-Com WP
session; see `PROTOCOL.md` for the commands and `spec.md` for the full design.

## Components

| File | Purpose |
|---|---|
| `collector.py` | The logger: ECU polling, GPS, sessions, CSV output, control socket |
| `ecuctl.py` | Command-line control: `STATUS`, `NEW_SESSION`, `STOP` |
| `mock_serial.py` | Simulated ECU and GPS, so everything runs without the car |
| `ecu_log.py` | Reads C-Com WP `.log` files; writes a text channel template from one |
| `CHANNEL_TEMPLATE.txt` | The normal channel set and column order (from `ecu_sample.log`) |
| `tools/decode_capture.py` | Decodes a USB capture of a C-Com WP session |
| `tests/` | Automated tests, run against the simulated ECU |

Planned, not yet built: a web page for downloading logs and starting or
stopping sessions, server-side plots, and systemd service files.

## Try it without hardware

    python3 collector.py --mock --mock-replay ecu_sample.log
    python3 ecuctl.py STATUS

Log files appear in `mock_data/`.

Run the tests with:

    python3 -m unittest tests/test_collector.py

## Status

Tested against the simulated ECU only. It has not yet been run against a real
ECU. Some channels (temperatures, MAP, Scaled RPM, fuel flow) are written as
raw counts until their conversions are confirmed. `spec.md` section 3.D.7
lists the checks still to be done on the car.
