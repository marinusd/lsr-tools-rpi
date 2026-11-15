"""Mock ECU and mock GPS for `collector.py --mock` (spec.md section 4, Tier 4).

Both classes offer the small part of the pyserial interface the collector uses.
MockEcu speaks the protocol of spec.md 3.D: it echoes every byte, answers the
identity read, stores FE writes in a mock RAM, and answers FC with the values
for the addresses in its poll table.
"""
import math
import threading
import time
from datetime import datetime, timezone

import ecu_log

POLL_TABLE_BASE = 0x00B6
COMMAND_LENGTH = {0xFC: 1, 0xFD: 5, 0xFE: 4, 0xFF: 3}
IDENT_ADDRESS, IDENT_TEXT = 0xE000, b'SEFI8LO'


class MockEcu:
    """A serial-port look-alike with an ECU behind it.

    Switches (set them at any time):
      silent    no echo and no replies, as if the ECU were switched off
      noecho    commands are obeyed but sent bytes are not echoed
      badsum    number of following replies to send with a wrong checksum
    """

    def __init__(self, replay_path=None, silent=False):
        self.timeout = 1.0
        self.silent = silent
        self.noecho = False
        self.badsum = 0
        self.fe_writes = []            # every (address, value) received by FE
        self.poll_count = 0
        self._ram = {}
        self._rx = bytearray()
        self._cmd = bytearray()
        self._cond = threading.Condition()
        self._replay = None
        self._started = time.monotonic()
        if replay_path:
            buf = ecu_log.read_bytes(replay_path)
            hdr = ecu_log.parse_header(buf)
            recs = ecu_log.records(buf, hdr)
            if not recs:
                raise ValueError(f'{replay_path} has no records to replay')
            ids = [c.id for c in hdr.channels]
            self._replay = [dict(zip(ids, r[1:])) for r in recs]

    # ---- serial-port interface -------------------------------------------
    def write(self, data):
        with self._cond:
            for b in bytes(data):
                if self.silent:
                    self._cmd.clear()
                    continue
                if not self.noecho:
                    self._rx.append(b)
                self._byte(b)
            self._cond.notify_all()
        return len(data)

    def read(self, size=1):
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while len(self._rx) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            out = bytes(self._rx[:size])
            del self._rx[:size]
            return out

    def reset_input_buffer(self):
        with self._cond:
            self._rx.clear()

    def close(self):
        pass

    # ---- the ECU ------------------------------------------------------------
    def _byte(self, b):
        if not self._cmd and b not in COMMAND_LENGTH:
            return                                  # not a command: echo only
        self._cmd.append(b)
        if len(self._cmd) < COMMAND_LENGTH[self._cmd[0]]:
            return
        c, self._cmd = bytes(self._cmd), bytearray()
        if c[0] == 0xFD:
            start, end = c[1] << 8 | c[2], c[3] << 8 | c[4]
            self._reply(bytes(self._memory(a) for a in range(start, end)))
        elif c[0] == 0xFE:
            addr = c[1] << 8 | c[2]
            self.fe_writes.append((addr, c[3]))
            self._ram[addr] = c[3]
        elif c[0] == 0xFF:
            self._rx.append(self._memory(c[1] << 8 | c[2]))
        elif c[0] == 0xFC:
            self._reply(bytes(self._poll_values()))

    def _reply(self, data):
        checksum = sum(data) & 0xFF
        if self.badsum > 0:
            self.badsum -= 1
            checksum ^= 0x55
        self._rx.extend(data)
        self._rx.append(checksum)

    def _memory(self, addr):
        if IDENT_ADDRESS <= addr < IDENT_ADDRESS + len(IDENT_TEXT):
            return IDENT_TEXT[addr - IDENT_ADDRESS]
        return self._ram.get(addr, 0xFF)

    def _poll_values(self):
        table, addr = [], POLL_TABLE_BASE
        while self._ram.get(addr, 0) != 0 and len(table) < 64:
            table.append(self._ram[addr])
            addr += 1
        if self._replay:
            source = self._replay[self.poll_count % len(self._replay)]
        else:
            source = self._synthetic()
        self.poll_count += 1
        return [source.get(a, 0) & 0xFF for a in table]

    def _synthetic(self):
        """Raw counts for an engine stepping through its rev range."""
        t = time.monotonic() - self._started
        rpm = 3400 + 2600 * math.sin(t / 4)                 # 800 .. 6000
        load = 0.5 + 0.5 * math.sin(t / 4 + 0.6)
        duty = int(20 + 150 * load)
        return {
            0x46: int(rpm / 50), 0x45: int(rpm / 37.5),      # RPM, Scaled RPM
            0x22: int(255 * load), 0x26: 128,                # TPS, Battery
            0x96: int(208 + 8 * math.sin(t)), 0x92: 214,     # Actual, Target A/F
            0x4A: int(128 + 10 * math.sin(t * 1.3)),         # O2 Corr.
            0x2A: int(60 + 180 * load),                      # MAP
            0x28: 120, 0x24: 40,                             # Air Temp, Coolant
            0x83: int(60 + 80 * (1 - load)), 0xB5: duty,     # Spark, Inj. DC / Fuel
        }


class MockGps:
    """Sends one $GPRMC sentence a second along an imaginary route."""

    def __init__(self, lat=39.7392, lon=-104.9903):
        self._lat, self._lon = lat, lon
        self._heading = 0.0
        self._next = time.monotonic()
        self._closed = threading.Event()

    def readline(self):
        delay = self._next - time.monotonic()
        if delay > 0 and self._closed.wait(delay):
            return b''
        self._next += 1.0
        t = time.monotonic()
        knots = 35 + 25 * math.sin(t / 7)                    # 10 .. 60 knots
        self._heading = (self._heading + 3) % 360
        nm = knots / 3600.0                                  # distance in one second
        h = math.radians(self._heading)
        self._lat += nm * math.cos(h) / 60
        self._lon += nm * math.sin(h) / (60 * math.cos(math.radians(self._lat)))
        now = datetime.now(timezone.utc)
        body = 'GPRMC,{},A,{},{},{},{},{:.1f},{:.1f},{},,,A'.format(
            now.strftime('%H%M%S.') + f'{now.microsecond // 10000:02d}',
            self._dm(self._lat, 2), 'N' if self._lat >= 0 else 'S',
            self._dm(self._lon, 3), 'E' if self._lon >= 0 else 'W',
            knots, self._heading, now.strftime('%d%m%y'))
        checksum = 0
        for ch in body:
            checksum ^= ord(ch)
        return f'${body}*{checksum:02X}\r\n'.encode('ascii')

    @staticmethod
    def _dm(value, degree_digits):
        value = abs(value)
        deg = int(value)
        return f'{deg:0{degree_digits}d}{(value - deg) * 60:07.4f}'

    def close(self):
        self._closed.set()
