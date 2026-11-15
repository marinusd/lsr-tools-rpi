# F.A.S.T. ECU serial protocol, as seen in C-Com WP real-time logging

Source: `prolific_usb_capture_20261113_101123.pcap` (USB capture of the PL2303 adapter)
and `fast-data.log` (the log C-Com saved in the same session).
Reproduce with: `python3 tools/decode_capture.py prolific_usb_capture_20261113_101123.pcap fast-data.log`

Status words: **Verified** = checked against the capture/log. **Inferred** = fits the
evidence but untested. **Unknown** = needs a test on the car.

## 1. Main finding

The ECU never streams and never sends a header. It is a polled memory-access
protocol: the PC writes a list of 8-bit RAM addresses into the ECU, then asks for the
values at those addresses about every 100 ms. The `.log` header, the channel table and
the millisecond timestamps are all produced by C-Com on the PC. (Verified: all 175 log
records equal the replies to the last 175 polls, and the log's ms values track the
capture clock within -20/+3 ms.)

## 2. Link

| Item | Value | Status |
|---|---|---|
| Adapter | Prolific PL2303 (067b:2303), Linux driver `pl2303`, `/dev/ttyUSB*` | Verified |
| Speed / framing | 9600 baud, 10-bit frames (8N1) | Inferred from timing: 206 bytes in 213 ms. The SET_LINE_CODING payload is not in the capture. |
| Control lines while talking | DTR asserted, RTS de-asserted | Verified. Whether the ECU/cable needs it: Unknown |
| Echo | Every byte sent comes back, and C-Com waits for it before sending the next byte (about 3 ms per byte) | Verified. Whether the ECU or the cable produces it: Unknown |

## 3. Commands

All addresses are 16-bit, high byte first. Checksum = sum of the data bytes, mod 256.

| Sent | Reply (after the echo) | Meaning | Status |
|---|---|---|---|
| `FD sh sl eh el` | bytes at `start` .. `end-1`, then checksum | Read block | Verified (13 reads, all checksums good) |
| `FF ah al` | 1 byte, no checksum | Read one byte | Verified (2 uses) |
| `FE ah al vv` | nothing (echo only) | Write one byte | Verified (12 uses, all to 0x00B6-0x00C1) |
| `FC` | one byte per poll-table entry, then checksum | Read the poll table's values | Verified (228 polls, all checksums good) |

### Poll table
RAM 0x00B6 upward holds the list of addresses that `FC` returns, ended by `00`.
The address of a channel is the "Channel ID" field of the log header (spec C.3).
C-Com writes the entries in ascending order of the header's channel index (spec C.2),
not in log-column order:

```
FE 00 B6 46   RPM            FE 00 BC B5   Inj. DC
FE 00 B7 24   Coolant        FE 00 BD 4A   O2 Corr.
FE 00 B8 28   Air Temp       FE 00 BE 92   Target A/F
FE 00 B9 22   TPS            FE 00 BF 2A   MAP
FE 00 BA 26   Battery        FE 00 C0 96   Actual A/F
FE 00 BB 83   Spark          FE 00 C1 00   end of list
```
`FC` then returns 12 bytes: the 11 values in that order plus the checksum,
about 13 ms after the echo.

Unknown: the largest table the ECU accepts, whether the order matters, whether the
table survives an ECU power cycle (assume not).

## 4. What C-Com did, in order

| Time (s) | Traffic | Needed for logging on the Pi? |
|---|---|---|
| 0-8.5 | DTR/RTS toggling, no data (Windows/C-Com port probing) | No |
| 8.52 | `FD E0 00 E0 07` -> `"SEFI8LO"` | Optional: a good "is the ECU there" check |
| 8.55-10.9 | 11 block reads covering 0x7800-0x7FFF (the 2 KB calibration) | No (Inferred) |
| 10.93-10.96 | `FF 00 54` -> 84, `FF 7F FF` -> 62, `FD 7F FD 7F FF` -> 12 B3 | No (Inferred) |
| 36.66 | 12 x `FE` (poll table), then the first `FC` | **Yes** |
| 36.66-59.63 | `FC` every ~100 ms (84-125 ms) | **Yes** |
| 42.23 | no change on the wire: C-Com starts keeping the replies (log record 0) | PC-side only |
| 59.63 | polling stops; nothing is sent to end it | - |
| 80.28 | port closed (DTR dropped) | - |

So "ARM" and "TRIGGER" are not ECU commands. On the wire there is only "write the
poll table and start polling" (36.66 s) and, 5.6 s later, a purely PC-side decision to
start saving records. Which C-Com button caused each is not visible in the capture.

## 5. Minimum sequence for the Pi

1. Open the port: 9600 8N1, no flow control, DTR on, RTS off.
2. (Optional) send `FD E0 00 E0 07`, expect `53 45 46 49 38 4C 4F FA`.
3. For each channel i (0..N-1): send `FE 00 (B6+i) id`. Then `FE 00 (B6+N) 00`.
4. Every 100 ms: send `FC`, read N+1 bytes, check the checksum, timestamp the row
   with the Pi's own clock.
5. To stop: stop sending.

For every byte sent: read the echo back and compare before sending the next byte.

Safety: `FE` writes ECU memory. The Pi code should refuse any write outside
0x00B6-0x00C1 (the only addresses C-Com wrote) and should never write an address it
has not seen C-Com write.

## 6. Tests still needed on the car

1. Skip steps 0-10.96 s of section 4 and confirm polling still works from a cold ECU.
2. ECU powered off: does a sent byte still echo? (Tells cable echo from ECU echo.)
3. Send without waiting for each echo: does the ECU still accept the command?
4. A capture with the engine running, so RPM, MAP and temperatures change.
5. A capture with the 13-channel dashboard, to confirm the table grows to 0x00C3.
