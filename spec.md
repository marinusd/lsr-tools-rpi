# Program Specification: Raspberry Pi 3b ECU & GPS Data Hub

## 1. Overview & Goal
The objective is to build a low-overhead, memory-optimized telemetry application running on a headless **Raspberry Pi 3b (1GB RAM)**. The system acts as a real-time data ingestion bridge for a F.A.S.T. Engine Control Unit (ECU) and a USB-connected GPS receiver. It controls the ECU's log stream over the serial link, and merges both data streams into one CSV file per logging session, organized by day, with GPS telemetry positioned firstmost. It serves them over a lean web UI directory listing, generates on-demand server-side chart graphs, and offers systemd process mechanics.

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
Each logging session (Section 3.D) produces exactly **one CSV file**. Files are grouped into one directory per day:
```text
/var/log/ecu_hub/data/
└── YYYY-MM-DD/
    ├── log_HHMM.csv
    ├── log_HHMM_2.csv               (second session started in the same minute)
    └── plot_HHMM_[Metrics].png
```
* **Session start time:** `YYYY-MM-DD` and `HHMM` are the date and the hour/minute at which the session started, that is, when the first byte of the ECU header arrived after TRIGGER. They come from the system clock after it has been set from GPS (Section 4, Tier 1).
* **Time zone:** the directory date and `HHMM` use the time zone set by the parameter `FILE_TIMEZONE` (default: the Pi's local time zone). The `GPS_Timestamp` column inside the file is always UTC, as defined in 3.B.
* **One file per session, whatever its length:** a session that runs past the top of the hour, or past midnight, stays in its single file. That file is in the directory of the day the session **started**.
* **Name collisions:** if `log_HHMM.csv` already exists, the collector uses `log_HHMM_2.csv`, then `_3`, and so on. It never appends to or overwrites an existing file.
* **Clock not yet set:** if a session must start before a valid GPS time has been obtained, the file goes in `/var/log/ecu_hub/data/unsynced/` and is named `log_boot<N>_<seconds since boot>.csv`. That keeps it from being filed under a wrong date.
* **Plots:** `plot_HHMM_[Metrics].png` takes the `HHMM` (and any `_N` suffix) of the CSV it was drawn from, and is written next to it.

### B. Frame Synchronizer & Packet Specification
* **ECU Serial Link Configuration:** Baud rate fixed strictly to **9600 bps**, 8 Data Bits, 1 Stop Bit, No Parity. Record layout and value conversion: see Section 3.C. Starting and stopping the stream: see Section 3.D.
* **GPS Serial Link Configuration:** Typically **9600 bps** or **115200 bps** (configurable via spec parameter), reading standard NMEA-0183 sentences.
* **Unified Output CSV Schema (GPS Firstmost, then ECU channels from the session header):**
  The collector must stitch the asynchronous streams together by caching the most recent valid GPS lock and appending the incoming 10Hz ECU data frames. The ECU can be configured to deliver **different channels, in different orders**, so the ECU columns are **not fixed**. They come from the channel table in each session's header (3.C.3). The column order is:
  1. **GPS_Timestamp**: ISO-8601 formatted time string (`YYYY-MM-DDTHH:MM:SS.FFFZ`) parsed from GPS.
  2. **GPS_Latitude**: Decimal degrees representation.
  3. **GPS_Longitude**: Decimal degrees representation.
  4. **GPS_Speed_MPH**: Ground speed calculated directly from GPS knots ($1 \text{ knot} = 1.15078 \text{ mph}$).
  5. **…then one column per ECU channel, in the order of the session's channel table.**

  Rules for the ECU columns:
  * **Column name** = the channel name from the header, with trailing spaces and NULs removed, exactly as the ECU spells it (e.g. `TPS (%)`, `Air Temp(*F)`, `Fuel #s/hr`).
  * **Raw columns:** a channel whose conversion is not yet known (3.C.6) is written as its raw count, and ` [raw]` is appended to its name (e.g. `Coolant(*F) [raw]`).
  * **Duplicate names:** if two channels in one header have the same name, the second becomes `<name> #2`, the third `<name> #3`, and so on.
  * **Number format:** converted values use the decimal places of the channel's printf format (e.g. `%5.2f` → 2 decimals), with no padding. Raw counts are written as integers.
  * **Matching channels:** code that needs a particular channel (plots, conversions) must find it by **name** or by the header's channel index (3.C.2), never by column position or by channel ID. Channel IDs are not unique (3.C.3).
  * Examples: `ecu_sample.log` gives 11 ECU columns starting `TPS (%), RPM, Battery (V), …`; `ecu_sample_2.log` gives 13 starting `Scaled RPM, Coolant(*F) [raw], Actual A/F, …`.

### C. ECU Log File Format (derived from `ecu_sample.log` and `ecu_sample_2.log`)

This section documents the binary layout of `ecu_sample.log`, a log saved by the F.A.S.T. ECU software. It was reverse-engineered from two samples: `ecu_sample.log` (11 channels, 3,146 records, collected at a density altitude of about 6,000 ft) and `ecu_sample_2.log` (13 channels in a different order, 634 records, collected at a density altitude of about 150 ft and 75 °F). Every offset below was verified against both. Items marked **[TBD]** are not yet confirmed.

> **Note:** No ECU frame-marker or sync sequence exists in this format. The bytes `AA 45 E0` cited in earlier drafts are not a marker: they are the low bytes of four 16-bit header values (15, 170, 325, 480) at offset 132. Parsers must not search for them.

#### C.1 General conventions
* All multi-byte integers are **little-endian**. All real values are IEEE-754 **32-bit floats**, little-endian.
* Strings are fixed-width, NUL-padded ASCII.
* The file is self-describing: channel names, scaling and display ranges are read from the header, not hard-coded.

#### C.2 Top-level layout

| Offset (dec) | Size | Content |
|---|---|---|
| 0 | 2 | Format/version tag: ASCII `"22"` (`0x32 0x32`). The same in both samples; the parser should log a warning if it is different. |
| 2 | 2 | `u16` channel count *N* (sample: **11**) |
| 4 | 2 × *N* | **Channel index array:** one `u16` per channel, in channel-table order. It appears to identify the channel in the ECU software's list of loggable values. It is consistent across both samples: RPM = 0, Coolant = 2, Air Temp = 3, TPS = 4, Battery = 5, Spark = 10, Inj. DC = 12, Scaled RPM = 15, O2 Corr. = 24, Target A/F = 27, MAP = 30, Actual A/F = 45, Fuel #s/hr = 52. The space runs to offset 132, which would allow up to 64 channels **[TBD]**. |
| 4 + 2*N* to 516 | — | Display/layout settings for the ECU software, identical in both samples (e.g. `u16` 15, 170, 325, 480 at 132; `f32` 420.0 ×4 and 440.0 ×4 at 262). Ignored by the collector. |
| 516 | 58 × *N* | Channel definition table (C.3) |
| 516 + 58*N* | 18 | Data block header (C.4) — at offset 1154 in the sample |
| 534 + 58*N* | record size × count | Data records (C.5) — from offset 1172 in the sample |

`516` is a fixed offset: it is the same in both samples even though their channel counts differ (11 and 13).

#### C.3 Channel definition (58 bytes each, in record-column order)

| Rel. offset | Type | Field |
|---|---|---|
| 0 | `char[16]` | Channel name |
| 16 | `char[10]` | Unit label (often empty) |
| 26 | `u16` | Channel ID: probably the ECU memory/parameter address the value is read from. **Not unique:** `Inj. DC (%)` and `Fuel #s/hr` both use 0xB5 and carry identical raw values. Do not use it to identify a channel. |
| 28 | `u16` | Conversion code (C.6) |
| 30 | `f32` | Scale |
| 34 | `f32` | Offset |
| 38 | `u16` | Raw minimum (sample: always 0) |
| 40 | `u16` | Raw maximum (sample: always 255) |
| 42 | `f32` | Display maximum |
| 46 | `f32` | Display minimum |
| 50 | `char[8]` | printf-style display format (e.g. `%5.1f`) |

Python: `struct.unpack_from('<16s10sHHffHHff8s', buf, 516 + 58*i)`

Channel table in `ecu_sample.log` (the last column is the first record decoded as `raw × scale + offset`):

| # | Name | Unit | ID | Conv | Scale | Offset | Display range | Format | Record 0 |
|---|---|---|---|---|---|---|---|---|---|
| 0 | `TPS (%)` | `%` | 0x22 | 0 | 0.392157 | 0 | 0 to 100 | `%5.0f` | 55 → 21.6 % |
| 1 | `RPM` | | 0x46 | 5 | 50 | 0 | 400 to 12500 | `%5.0f` | 80 → 4000 |
| 2 | `Battery (V)` | `VDC` | 0x26 | 0 | 0.108 | 0 | 8 to 17 | `%5.1f` | 100 → 10.8 V |
| 3 | `Actual A/F` | | 0x96 | 0 | 0.0625 | 0 | 9 to 17 | `%5.2f` | 205 → 12.81 |
| 4 | `Target A/F` | | 0x92 | 0 | 0.0625 | 0 | 9 to 17 | `%5.1f` | 214 → 13.38 |
| 5 | `O2 Corr. (%)` | | 0x4A | 0 | 0.78125 | −100 | −50 to 50 | `%5.1f` | 158 → 23.4 % |
| 6 | `MAP (PSIA)` | | 0x2A | 8 | 1 | 0 | −13 to 50 | `%5.1f` | 156 → **[TBD]** |
| 7 | `Air Temp(*F)` | `(*F)` | 0x28 | 1 | 1 | 0 | −40 to 200 | `%5.1f` | 128 → **[TBD]** |
| 8 | `Coolant(*F)` | `Deg (*F)` | 0x24 | 1 | 1 | 0 | −40 to 250 | `%5.1f` | 39 → **[TBD]** |
| 9 | `Spark(*BTDC)` | | 0x83 | 0 | 0.25 | 0 | 0 to 100 | `%5.2f` | 143 → 35.75° |
| 10 | `Inj. DC (%)` | | 0xB5 | 4 | 0.128 | 0 | 0 to 100 | `%5.1f` | 41 → 5.2 % |

Channel table in `ecu_sample_2.log`: the same definitions in a different order, plus two new channels:

| # | Name | ID | Conv | Scale | Display range | Record 0 raw |
|---|---|---|---|---|---|---|
| 0 | `Scaled RPM` (new) | 0x45 | 6 | 1 | 0 to 10000 | 59 |
| 1 | `Coolant(*F)` | 0x24 | 1 | 1 | −40 to 250 | 74 |
| 2 | `Actual A/F` | 0x96 | 0 | 0.0625 | 9 to 17 | 227 |
| 3 | `Battery (V)` | 0x26 | 0 | 0.108 | 8 to 17 | 108 |
| 4 | `TPS (%)` | 0x22 | 0 | 0.392157 | 0 to 100 | 41 |
| 5 | `Spark(*BTDC)` | 0x83 | 0 | 0.25 | 0 to 100 | 140 |
| 6 | `O2 Corr. (%)` | 0x4A | 0 | 0.78125 | −50 to 50 | 159 |
| 7 | `Inj. DC (%)` | 0xB5 | 4 | 0.128 | 0 to 100 | 41 |
| 8 | `MAP (PSIA)` | 0x2A | 8 | 1 | −13 to 50 | 203 |
| 9 | `Air Temp(*F)` | 0x28 | 1 | 1 | −40 to 200 | 222 |
| 10 | `Target A/F` | 0x92 | 0 | 0.0625 | 9 to 17 | 217 |
| 11 | `Fuel #s/hr` (new) | 0xB5 | 10 | 0.128 | 0 to 1600 | 41 |
| 12 | `RPM` | 0x46 | 5 | 50 | 400 to 12500 | 44 |

#### C.4 Data block header (18 bytes)

| Rel. offset | Type | Field | Sample value |
|---|---|---|---|
| 0 | `u16` | Record size in bytes (= 4 + 2*N*) | 26 |
| 2 | `u16` | Reserved | 0 |
| 4 | `u32` | Record count | 3146 |
| 8 | `u16` | Unknown; 8 in both samples. Ignored. | 8 |
| 10 | `u16` | Reserved | 0 |
| 12 | `u16` | Unknown; 650 in both samples. Ignored. | 650 |
| 14 | `u16` | Unknown; 600 in both samples. Ignored. | 600 |
| 16 | `u16` | Unknown; 1 in both samples. Ignored. | 1 |

In the sample, `data_start + record_size × record_count` equals the file length exactly. The parser must check that and, if the file is shorter (a log cut off by power loss), process only the complete records.

#### C.5 Data record (`4 + 2N` bytes; 26 bytes in the sample)

| Rel. offset | Type | Field |
|---|---|---|
| 0 | `u32` | Elapsed time in milliseconds since the start of logging (first record = 0) |
| 4 + 2*i* | `u16` | Raw value of channel *i*, in channel-table order |

Python: `struct.unpack_from('<I' + 'H'*N, buf, data_start + k*record_size)`

Sample properties:
* 3,146 records over 314.5 s (sample 1) and 634 records over 63.3 s (sample 2). Both average **exactly 100 ms** per record (in sample 2, record 100 is at 10,000 ms and record 300 at 30,000 ms). Individual intervals jitter between 78 and 125 ms. Rows must be timestamped from the ms field, not by assuming even 100 ms steps.
* The timestamp strictly increases.
* Every raw value is 0–255 (the high byte is always 0). The ECU therefore sends 8-bit values that are stored as `u16`.

#### C.6 Converting raw values to engineering units

| Conv code | Channels | Rule |
|---|---|---|
| 0 | TPS, Battery, A/F, Target A/F, O2 Corr., Spark | `value = raw × scale + offset` (confirmed plausible) |
| 5 | RPM | Linear `raw × scale` gives sensible RPM (raw 80 → 4000). Treated as linear; meaning of code 5 **[TBD]** |
| 6 | Scaled RPM | Not linear with the file's scale (scale = 1). Its raw count tracks about 4/3 of the RPM count (≈ 37.5 rpm per count, against RPM's 50) but the exact rule is **[TBD]**. Written raw. |
| 10 | Fuel #s/hr | Computed by the ECU software from injector duty cycle: its raw values equal `Inj. DC (%)` in every record. The conversion presumably needs injector flow rate and possibly RPM, which are not in the file **[TBD]**. Written raw. |
| 4 | Inj. DC | Linear `raw × scale` gives sensible duty cycle. Treated as linear; meaning of code 4 **[TBD]** |
| 1 | Air Temp, Coolant | Non-linear thermistor lookup. Scale = 1 and offset = 0, so the conversion is **not** carried in the file. A raw-count → °F table is required **[TBD]**. Observed raw range: coolant 21–39, air 89–128. Coolant raw falls as the engine warms, which suggests a lower count means hotter. |
| 8 | MAP | Sensor-specific; the file gives scale = 1, offset = 0. **Provisional: `psia ≈ raw × 0.0597`**, i.e. about 0.41 kPa per count with no offset (255 counts ≈ 15.2 psia ≈ 105 kPa, the range of a typical 1-bar sensor). See the MAP notes below. **[TBD: confirm]** |

**MAP notes (from the two samples):**
* Higher count means higher pressure. Engine off, sample 2 reads a steady 245 for 10 s at about 150 ft (standard atmosphere ≈ 14.62 psia), so ≈ 0.0597 psia per count.
* Sample 1 (about 6,000 ft density altitude) reads 206 engine-off. With the factor above, that is 12.29 psia, a pressure altitude of about 4,900 ft. That is consistent with a 6,000 ft density altitude on a warm day. The reading is weak evidence, though: it comes from only 4 records, taken while the ECU's supply voltage was collapsing.
* A two-point straight-line fit through both engine-off readings (taking the standard pressures for 6,000 ft and 150 ft) is **rejected**: it gives negative pressures below about 45 counts, and both samples go as low as 11–14 counts on deceleration.
* Sanity check with the provisional factor: full throttle reads 196–203 counts at 6,000 ft and 236–241 at 150 ft, about 97 % of engine-off pressure in both cases, as expected for an engine without boost. Deceleration lows of 11–14 counts come out at 0.7–0.8 psia, which is lower than usual. A small offset (a sensor that bottoms out above 0 psia) cannot be ruled out.
* The display range of −13 to 50 is not explained by this conversion **[TBD]**.

**Configurable conversions:** each unknown conversion is a parameter in `/etc/ecu_hub/collector.conf`, unset by default, matched by channel **name**:

| Parameter | Default | When set |
|---|---|---|
| `MAP_PSIA_PER_COUNT`, `MAP_PSIA_OFFSET` | unset (provisional values: 0.0597, 0) | `MAP (PSIA)` is written as `raw × per_count + offset` |
| `TEMP_TABLE_COOLANT`, `TEMP_TABLE_AIR` | unset | Path to a two-column CSV (raw count, °F), linearly interpolated |
| `SCALED_RPM_PER_COUNT` | unset | `Scaled RPM` is written as `raw × value` |
| `FUEL_LBHR_PER_COUNT` | unset | `Fuel #s/hr` is written as `raw × value` |

While a parameter is unset, the collector writes the **raw count** for that channel and flags it in the CSV header (for example `Coolant(*F) [raw]`, see 3.B). It must never write an invented approximation.

**Quickest way to confirm these conversions:** open `ecu_sample_2.log` in the F.A.S.T. PC software and note the displayed MAP, Coolant, Air Temp, Scaled RPM and Fuel #s/hr at a few points, for example t = 0 s, 30 s and 60 s (raw MAP 203 / 238 / 245, Coolant 74 / 66 / 56). Each pair of displayed and raw values is one calibration point.

#### C.7 Relationship to the live serial stream
This format describes a **saved log file**. What is known about the live 9600-bps stream:
* **Known:** the stream is started and stopped by ARM and TRIGGER commands (Section 3.D). After a TRIGGER, the stream begins again with its initial header, so every session starts with a header.
* **To confirm [TBD]:** that the live header is byte-for-byte the layout in C.2–C.4 (including the channel table), and that the records match C.5 (u32 ms + N × u16), not a compact form such as N × u8 with no timestamp.
* **To confirm [TBD]:** how the live header fills in the record-count field (C.4), since the count is not known when the stream starts. The reader must not rely on it for live data.

Record boundaries come from the header: once the reader has parsed the header at the start of a session, it reads fixed-size records of `record_size` bytes. A strictly increasing ms field is the sanity check. Byte-pattern searches are not acceptable.

#### C.8 Acceptance check for the parser
A reference parser, given `ecu_sample_2.log`, must report 13 channels in the order of the second table in C.3, a record size of 30, 634 records, and a last timestamp of 63,296 ms. Given `ecu_sample.log`, it must report: 11 channels with the names in C.3, a record size of 26, 3,146 records, first timestamp 0 ms, last timestamp 314,500 ms, and a first record of TPS 21.6 %, RPM 4000, Battery 10.8 V, Actual A/F 12.81, Target A/F 13.38, O2 Corr. 23.4 %, Spark 35.75°, Inj. DC 5.2 %.

### D. Controlling the Log Stream (ARM / TRIGGER)

The ECU accepts two commands on its serial link that control the log stream:

| Command | Effect on the ECU |
|---|---|
| **ARM** | The log stream **stops**. The ECU prepares to start again from the beginning: its next stream will open with the initial header (C.2–C.4). |
| **TRIGGER** | The log stream **begins**: the header first, then data records (C.5) at about 10 Hz. |

#### D.1 Command parameters (bytes to be filled in later)
The exact bytes of each command are **[TBD]**. They must not be hard-coded. They are configuration parameters, read at start-up from the collector's config file (`/etc/ecu_hub/collector.conf`), with a command-line override for testing:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `ECU_CMD_ARM` | hex byte string, e.g. `"AA 01"` | *unset* **[TBD]** | Bytes written to the ECU port to send ARM |
| `ECU_CMD_TRIGGER` | hex byte string | *unset* **[TBD]** | Bytes written to the ECU port to send TRIGGER |
| `ECU_CMD_SETTLE_MS` | integer | `250` **[TBD]** | Wait after ARM before sending TRIGGER, and before discarding leftover input |
| `ECU_HEADER_TIMEOUT_S` | integer | `5` | How long to wait for the header to arrive after TRIGGER before reporting a failure |
| `ECU_STREAM_TIMEOUT_S` | integer | `3` | Gap with no complete record, during a session, after which the session is treated as ended |
| `ECU_TRIGGER_RETRIES` | integer | `3` | How many ARM → TRIGGER attempts the start-up attempt (and a `NEW_SESSION` request) makes before the ECU is treated as not responding |
| `ECU_AUTOSTART` | boolean | `true` | Whether the collector tries ARM → TRIGGER by itself at start-up |
| `ECU_RETRY_INTERVAL_S` | integer | `60` | While the ECU is not responding, how often the collector tries again to connect |
| `GPS_SYNC_WAIT_S` | integer | `60` | At start-up, the longest the collector waits for a GPS time fix before its first ARM → TRIGGER attempt |

Parsing rules: hex pairs separated by optional spaces; case does not matter; an empty or missing value means *unset*. Whether the ECU sends an acknowledgement in reply to either command is **[TBD]**; if it does, an `ECU_ACK_*` parameter will be added.

**While the command bytes are unset**, the collector runs in **passive mode**. It logs a warning once, never writes to the ECU port, and starts a session whenever a header shows up in the incoming stream (for example, if logging is started from the ECU's own software). It must still run and log in this mode.

#### D.2 Collector states

```text
   start-up (AUTOSTART) or NEW_SESSION
  ┌──────┐   send ARM, wait SETTLE,   ┌───────────┐  header parsed   ┌───────────┐
  │ IDLE │──discard input, TRIGGER──▶│ STARTING  │─────────────────▶│ STREAMING │
  └──────┘                            └───────────┘                  └───────────┘
     ▲                                  │      ▲                       │      │
     │                     no response  │      │ every RETRY_INTERVAL  │      │
     │                 (retries used up │      │ (60 s), or at once    │      │
     │                 or port missing) ▼      │ on NEW_SESSION        │      │
     │                                ┌─────────────┐  stream timeout  │      │
     │            STOP                │   WAITING   │◀─ or port error ─┘      │
     ├────────────────────────────────│  (polling)  │                         │
     │                                └─────────────┘                         │
     └────────────────── STOP (send ARM) ─────────────────────────────────────┘
```

A header arriving on its own (for example, logging started from the ECU's software) moves the collector straight to STREAMING from IDLE, STARTING or WAITING.

* **IDLE:** no session. Incoming ECU bytes are discarded (in passive mode, the reader keeps watching for a header).
* **STARTING:** the collector sends **ARM**, waits `ECU_CMD_SETTLE_MS`, discards everything in the input buffer, then sends **TRIGGER** and waits up to `ECU_HEADER_TIMEOUT_S` for the header. ARM always goes first, even if the stream appears to be stopped, so the ECU is guaranteed to start from its initial header.
* **STREAMING:** the header has been parsed. The collector opens the session's new CSV file (3.A), writes the column header row, and appends one row per record.
* **WAITING:** the ECU is not responding. The collector keeps listening on the port (if it is open) and, every `ECU_RETRY_INTERVAL_S`, tries again to connect (D.5).

#### D.3 Session rules
* **A session is one header followed by its records.** It starts when a header is parsed and ends at the first of: ARM sent by the collector, `ECU_STREAM_TIMEOUT_S` with no complete record, a serial-port error or disconnect, or the collector shutting down.
* **New header means new session.** A header arriving while STREAMING (for example, the ECU was reset or triggered from elsewhere) closes the current file and opens a new one.
* **Closing a session** flushes and closes the CSV file. A partial record at the end is dropped. The file is never reopened later.
* **Channel set per session:** the CSV columns after the four GPS columns come from the header of that session. If the ECU's channel configuration changes, the next session's file has the new columns.
* **ECU time base:** the ms field restarts at 0 in each session (C.5), so it is relative to the session start.

#### D.4 Who sends the commands
* The **collector** is the only process that opens the ECU serial port and the only one that sends ARM and TRIGGER.
* **By default the collector starts logging by itself at start-up:** with `ECU_AUTOSTART = true` (the default), it makes an ARM → TRIGGER attempt as soon as the GPS clock has been set, or after `GPS_SYNC_WAIT_S` if no GPS fix has arrived (the session's file then goes in `unsynced/`; see 3.A). No action on the web page is needed. If that attempt gets no response, the collector polls as described in D.5.
* With `ECU_AUTOSTART = false`, the collector stays IDLE at start-up and waits for `NEW_SESSION` from the web page (or a header the ECU sends by itself).
* The **web interface** asks the collector to act through a local control socket, `/run/ecu_hub/control.sock` (a Unix domain socket that only the `ecu-web` service user can access). Commands:
  * `NEW_SESSION`: run the STARTING sequence (ends any current session first). In WAITING, it makes an attempt at once instead of waiting for the next poll.
  * `STOP`: send ARM, end the current session, and stop any polling. The collector then stays IDLE until `NEW_SESSION` or a collector restart.
  * `STATUS`: return the state, the current file path, records written, the time of the last record and, when WAITING, the number of failed attempts, the last error, and when the next attempt is due.

  The web interface never writes to the serial port itself.
* On a normal shutdown (`systemctl stop ecu-collector`), the collector sends ARM, so the ECU is left in its ready-to-restart state, and closes the current file.

#### D.5 When the ECU does not respond: polling every 60 seconds
* **What "not responding" means:** a connection attempt fails when the ECU serial port cannot be opened (for example, the USB adapter is missing or the ECU is unpowered), or when no header arrives within `ECU_HEADER_TIMEOUT_S` after TRIGGER on any of the `ECU_TRIGGER_RETRIES` tries. (If the ECU turns out to send an acknowledgement, a missing acknowledgement will count as a failure too; see D.1.)
* **Polling:** after a failed attempt, the collector enters WAITING and tries again every `ECU_RETRY_INTERVAL_S` (default **60 s**), measured from the start of one attempt to the start of the next. Each poll re-opens the port if needed, then runs one ARM → TRIGGER sequence. Polling continues indefinitely until a session starts, `STOP` is received, or the collector is stopped.
* **Lost connection during a session:** if a session ends because of `ECU_STREAM_TIMEOUT_S` or a serial-port error (and not because of `STOP` or a new header), the collector closes the file and goes to WAITING, so logging resumes by itself when the ECU comes back. Each resumed session gets its own new file.
* **Passive mode** (command bytes unset): no commands are ever sent. If the port is missing, the collector tries to re-open it every `ECU_RETRY_INTERVAL_S`. While it is open, the collector keeps listening for a header.
* **Must not block other work:** while WAITING, the GPS thread, the control socket and `STATUS` keep working normally.
* **Logging to the system journal:** the collector logs the first failure in full, then one short line per failed poll, and one line when the connection is made again.

---

## 4. Functional Requirements

### Tier 1: Data Ingestion & GPS Synchronization (`collector.py`)
* **Authoritative Clock Synchronization:** Since the Raspberry Pi 3b lacks an internal battery-backed Real-Time Clock (RTC), the collector must parse the first valid `$GPRMC` or `$ZDA` sentence from the GPS stream, validate the time lock, and issue a privileged system call (e.g., `timedatectl` or `date -s`) to set the system clock before writing logs.
* **Asynchronous Multi-Threaded Read Loop:** 
  * Thread A continuously monitors the GPS USB serial interface port, extracting lat/long coordinates and speed. It handles NMEA conversions, notably multiplying knots data fields by `1.150779` to derive instantaneous integer or float **MPH values**.
  * Thread B manages the high-frequency ECU input buffer, matching incoming frames with the latest thread-safe memory snapshot of current GPS spatial tracking data.
* **Value Conversion:** Converts raw counts to engineering units using each channel's scale and offset from the header. Channels whose conversion is not carried in the file (temperatures, MAP, Scaled RPM, Fuel #s/hr) use the configurable conversions in Section 3.C.6, and are written raw until those are set.
* **Stream Control & Sessions:** The collector runs the ARM/TRIGGER sequence and the session rules in Section 3.D, and writes one CSV file per session in the layout of Section 3.A.
* **Dynamic Flushing:** Unified records are buffered dynamically and flushed to disk targets every 1–2 seconds to prevent file corruption during engine power-downs.

### Tier 2: File Delivery Web Interface (`server.py`)
* Exposes a fast directory index mapping `/var/log/ecu_hub/data/`.
* Renders a native anchor tree list layout allowing operators to seamlessly navigate the day directories, see that day's session files (one per session, newest first), and download target merged `.csv` files.
* Shows the collector's state from `STATUS`, with **New session** and **Stop logging** buttons that send `NEW_SESSION` and `STOP` over the control socket (Section 3.D.4).

### Tier 3: Visualization Generator
* Provides a selection interface allowing users to select a log, choose multiple variable fields from that CSV's own header row (columns differ between sessions; see 3.B), for example graphing engine `RPM` alongside vehicle `GPS_Speed_MPH`, and trigger plot graphics rendering.
* **Server-Side Render Boundary:** The daemon invokes matplotlib to output static plot graphics directly inside the active log subdirectory next to the target `.csv` stream.
* **OOM Prevention Loop:** Explicitly handles garbage cleanup inside render loops using `plt.close(fig)` to protect the 1GB RAM system.

### Tier 4: Mock Generation & Simulation Layer (`mock_serial.py`)
* Activated globally using a programmatic execution flag (`python collector.py --mock`).
* Emulates both hardware serial interfaces simultaneously using internal background simulator loops.
* Feeds realistic, moving metrics strings:
  * Telemetry cycles stepping RPM through normal rev bands.
  * A replay option that streams the header and records of `ecu_sample.log` or `ecu_sample_2.log` after TRIGGER, so both channel layouts (11 and 13 channels, different orders) can be tested.
  * A mock ECU that obeys ARM and TRIGGER, with a switch that makes it ignore commands (silent) or drop out mid-session, to test the polling in 3.D.5. It stays silent after ARM, and after TRIGGER sends a header in the format of 3.C followed by records. It recognizes the command bytes from the same `ECU_CMD_*` parameters; when those are unset, it uses mock-only placeholder values so the full sequence can still be tested.
  * Spatial route generation loop modifying latitude and longitude over an imaginary driving path, calculating step velocities to cleanly mock logical non-zero `GPS_Speed_MPH` data fields.

---

## 5. System Control & Linux Systemd Automation

The process management deployment leverages two systemd configuration engines:
* `ecu-collector.service`: Starts at system boot, initializes the GPS serial link first to synchronize time parameters, then by default starts a session with ARM → TRIGGER (Section 3.D). If the ECU does not respond, it polls every 60 s until it does (3.D.5); the service keeps running and does not exit or restart because of this. On stop it sends ARM and closes the current file.
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
1. Executing `python collector.py --mock` accurately instantiates simultaneous generation tasks, producing merged CSV logs whose columns are the four GPS columns followed by the ECU channels in the order of the session header (3.B). Replaying `ecu_sample.log` and then `ecu_sample_2.log` produces two files with 11 and 13 ECU columns respectively, each in its own header's order.
2. Each session produces exactly one file, `data/YYYY-MM-DD/log_HHMM.csv`, named from the session start time. A session that runs past the top of the hour or past midnight stays in that one file. Two sessions started in the same minute produce `log_HHMM.csv` and `log_HHMM_2.csv`.
3. Graph layout requests processing `GPS_Speed_MPH` vs engine factors render smoothly, verifying that active memory usage traces stay flat over sequential rendering checks.
4. In mock mode, `NEW_SESSION` makes the collector send ARM then TRIGGER, and a new file is created. `STOP` sends ARM and closes the file. A header that arrives mid-session starts a new file. With the `ECU_CMD_*` parameters unset on real hardware, the collector sends nothing to the ECU and still logs any stream that the ECU starts by itself.
5. With the mock ECU set to stay silent (or the ECU port absent), the collector starts, makes its start-up attempt, enters WAITING, and makes another attempt every 60 s (as shown by `STATUS` and the journal). When the mock ECU is made to respond, the next attempt starts a session and creates a file with no manual action. `NEW_SESSION` while WAITING makes an attempt at once, and `STOP` stops the polling.

