#!/usr/bin/env python3
"""ECU & GPS data collector (spec.md sections 3 and 4, Tier 1).

Polls a F.A.S.T. ECU over its serial link, merges each reply with the latest
GPS fix, and writes one CSV file per logging session.

    python collector.py                 real hardware, /etc/ecu_hub/collector.conf
    python collector.py --mock          mock ECU and mock GPS, files in ./mock_data
    python collector.py --help          all parameters
"""
import argparse
import csv
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import ecu_log

log = logging.getLogger('collector')
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Configuration (spec.md 3.D.6 and 3.C.6)
# ---------------------------------------------------------------------------
PARAMS = [
    # name, type, default, help
    ('ECU_PORT', str, '/dev/ttyUSB0', 'ECU serial device'),
    # The default is the fixed channel list in CHANNEL_TEMPLATE.txt, next to this file.
    # For a dynamic list, name a log file saved by C-Com WP instead: the collector then
    # takes the channels and their order from that log's header at each session start,
    # so replacing the log changes what is logged without editing anything else.
    # Example: ('ECU_CHANNEL_TEMPLATE', str, '/etc/ecu_hub/channels.log', #
    ('ECU_CHANNEL_TEMPLATE', str, os.path.join(HERE, 'CHANNEL_TEMPLATE.txt'),
     'channel template: a text template, or a C-Com log file for a dynamic list'),
    ('ECU_POLL_INTERVAL_MS', float, 100, 'time between polls'),
    ('ECU_ECHO_TIMEOUT_MS', float, 50, 'longest wait for the echo of one byte'),
    ('ECU_REPLY_TIMEOUT_MS', float, 60, 'longest wait for a complete reply'),
    ('ECU_POLL_FAILURES_MAX', int, 30, 'failed polls in a row that end a session'),
    ('ECU_START_RETRIES', int, 3, 'start attempts before the ECU counts as not responding'),
    ('ECU_RETRY_INTERVAL_S', float, 60, 'time between attempts while the ECU is not responding'),
    ('ECU_AUTOSTART', bool, True, 'start a session at start-up'),
    ('ECU_IDENT_EXPECT', str, 'SEFI8LO', 'expected identity text (warning only)'),
    ('ECU_POLL_TABLE_MAX_CHANNELS', int, 11, 'largest channel count written to the ECU'),
    ('GPS_PORT', str, '/dev/ttyACM0', 'GPS serial device'),
    ('GPS_BAUD', int, 9600, 'GPS speed'),
    ('GPS_SYNC_WAIT_S', float, 60, 'longest wait for GPS time before the first attempt'),
    ('FILE_TIMEZONE', str, None, 'time zone of directory and file names (default: local)'),
    ('FLUSH_INTERVAL_S', float, 1, 'how often rows are forced to disk'),
    ('DATA_DIR', str, '/var/log/ecu_hub/data', 'where the day directories go'),
    ('CONTROL_SOCKET', str, '/run/ecu_hub/control.sock', 'control socket for the web interface'),
    ('MAP_PSIA_PER_COUNT', float, None, 'MAP conversion; unset = write raw counts'),
    ('MAP_PSIA_OFFSET', float, 0, 'MAP conversion offset'),
    ('TEMP_TABLE_COOLANT', str, None, 'CSV of raw count, deg F for Coolant'),
    ('TEMP_TABLE_AIR', str, None, 'CSV of raw count, deg F for Air Temp'),
    ('SCALED_RPM_PER_COUNT', float, None, 'Scaled RPM conversion'),
    ('FUEL_LBHR_PER_COUNT', float, None, 'Fuel #s/hr conversion'),
]
MOCK_DEFAULTS = {
    'DATA_DIR': os.path.join(HERE, 'mock_data'),
    'CONTROL_SOCKET': os.path.join(HERE, 'mock_data', 'control.sock'),
}


def _convert(name, kind, text):
    text = text.strip().strip('"\'')
    if text == '':
        return None
    if kind is bool:
        if text.lower() in ('true', 'yes', 'on', '1'):
            return True
        if text.lower() in ('false', 'no', 'off', '0'):
            return False
        raise SystemExit(f'{name}: "{text}" is not true or false')
    try:
        return kind(text)
    except ValueError:
        raise SystemExit(f'{name}: "{text}" is not a valid value')


def load_config(argv=None):
    ap = argparse.ArgumentParser(description='ECU & GPS data collector')
    ap.add_argument('--mock', action='store_true', help='use the mock ECU and mock GPS')
    ap.add_argument('--mock-replay', metavar='LOG',
                    help='mock ECU replays the records of this log (default: synthetic values)')
    ap.add_argument('--mock-silent', action='store_true', help='mock ECU starts switched off')
    ap.add_argument('--config', default='/etc/ecu_hub/collector.conf')
    ap.add_argument('-v', '--verbose', action='store_true')
    for name, _kind, default, text in PARAMS:
        ap.add_argument('--' + name.lower().replace('_', '-'), dest=name, metavar='VALUE',
                        help=f'{text} (default: {default if default is not None else "unset"})')
    args = ap.parse_args(argv)

    kinds = {name: kind for name, kind, _d, _h in PARAMS}
    cfg = {name: default for name, _k, default, _h in PARAMS}
    given = set()
    if os.path.exists(args.config):
        with open(args.config) as f:
            lines = f.read().splitlines()
        for number, raw in enumerate(lines, 1):
            text = raw.split('#', 1)[0].strip()
            if not text:
                continue
            key, sep, value = text.partition('=')
            key = key.strip().upper()
            if not sep or key not in kinds:
                raise SystemExit(f'{args.config} line {number}: unknown setting "{text}"')
            cfg[key] = _convert(key, kinds[key], value)
            given.add(key)
    for name in kinds:
        if getattr(args, name) is not None:
            cfg[name] = _convert(name, kinds[name], getattr(args, name))
            given.add(name)
    if args.mock:
        for name, value in MOCK_DEFAULTS.items():
            if name not in given:
                cfg[name] = value
    return args, cfg


# ---------------------------------------------------------------------------
# ECU link (spec.md 3.D.1 and 3.D.2)
# ---------------------------------------------------------------------------
CMD_POLL, CMD_READ_BLOCK, CMD_WRITE = 0xFC, 0xFD, 0xFE
POLL_TABLE_BASE = 0x00B6
IDENT_START, IDENT_END = 0xE000, 0xE007


class EcuError(Exception):
    """The ECU did not answer as expected."""


class EcuLink:
    def __init__(self, ser, echo_timeout_s, reply_timeout_s, max_channels):
        self.ser = ser
        self.echo_timeout = echo_timeout_s
        self.reply_timeout = reply_timeout_s
        self.max_channels = max_channels

    def _read_exact(self, count, timeout):
        deadline = time.monotonic() + timeout
        buf = b''
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.ser.timeout = remaining
            buf += self.ser.read(count - len(buf))
        return buf

    def _send(self, data):
        """Send one byte at a time, checking the echo of each."""
        self.ser.reset_input_buffer()
        for b in data:
            self.ser.write(bytes([b]))
            echo = self._read_exact(1, self.echo_timeout)
            if not echo:
                raise EcuError(f'no echo for byte {b:02X}')
            if echo[0] != b:
                raise EcuError(f'wrong echo: sent {b:02X}, got {echo[0]:02X}')

    def _reply(self, count):
        r = self._read_exact(count + 1, self.reply_timeout)
        if len(r) < count + 1:
            raise EcuError(f'short reply: {len(r)} of {count + 1} bytes')
        if sum(r[:-1]) & 0xFF != r[-1]:
            raise EcuError('reply has a wrong checksum')
        return r[:-1]

    def read_block(self, start, end):
        self._send(bytes([CMD_READ_BLOCK, start >> 8, start & 0xFF, end >> 8, end & 0xFF]))
        return self._reply(end - start)

    def identity(self):
        return self.read_block(IDENT_START, IDENT_END)

    def _write_poll_table_slot(self, slot, value):
        """The only place an FE (write to ECU memory) is ever sent."""
        if not 0 <= slot <= self.max_channels:
            raise EcuError(f'refused: poll-table slot {slot} is outside the allowed range')
        addr = POLL_TABLE_BASE + slot
        self._send(bytes([CMD_WRITE, addr >> 8, addr & 0xFF, value]))

    def write_poll_table(self, ids):
        if len(ids) > self.max_channels:
            raise EcuError(f'refused: {len(ids)} channels, but ECU_POLL_TABLE_MAX_CHANNELS '
                           f'is {self.max_channels}')
        for value in ids:
            if not 1 <= value <= 0xFF:
                raise EcuError(f'refused: channel ID {value:#x} cannot go in the poll table')
        for slot, value in enumerate(ids):
            self._write_poll_table_slot(slot, value)
        self._write_poll_table_slot(len(ids), 0)

    def poll(self, count):
        self._send(bytes([CMD_POLL]))
        return self._reply(count)


# ---------------------------------------------------------------------------
# Value conversion and CSV columns (spec.md 3.B and 3.C.6)
# ---------------------------------------------------------------------------
LINEAR_CONV_CODES = (0, 4, 5)


def _load_table(path):
    points = []
    with open(path, newline='') as f:
        for row in csv.reader(f):
            try:
                points.append((float(row[0]), float(row[1])))
            except (ValueError, IndexError):
                continue                                # heading or blank line
    if len(points) < 2:
        raise ecu_log.FormatError(f'{path}: needs at least two rows of raw count, value')
    return sorted(points)


def _interpolate(points, x):
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _named_conversion(name, cfg):
    """Conversion for a channel whose rule is not in the template, or None if unset."""
    if name == 'MAP (PSIA)' and cfg['MAP_PSIA_PER_COUNT'] is not None:
        a, b = cfg['MAP_PSIA_PER_COUNT'], cfg['MAP_PSIA_OFFSET'] or 0
        return lambda r: r * a + b
    if name == 'Scaled RPM' and cfg['SCALED_RPM_PER_COUNT'] is not None:
        a = cfg['SCALED_RPM_PER_COUNT']
        return lambda r: r * a
    if name == 'Fuel #s/hr' and cfg['FUEL_LBHR_PER_COUNT'] is not None:
        a = cfg['FUEL_LBHR_PER_COUNT']
        return lambda r: r * a
    if name == 'Coolant(*F)' and cfg['TEMP_TABLE_COOLANT']:
        table = _load_table(cfg['TEMP_TABLE_COOLANT'])
        return lambda r: _interpolate(table, r)
    if name == 'Air Temp(*F)' and cfg['TEMP_TABLE_AIR']:
        table = _load_table(cfg['TEMP_TABLE_AIR'])
        return lambda r: _interpolate(table, r)
    return None


NAMED_CHANNELS = ('MAP (PSIA)', 'Scaled RPM', 'Fuel #s/hr', 'Coolant(*F)', 'Air Temp(*F)')


def make_converters(channels, cfg):
    """Return (column names, one function raw -> text per channel)."""
    names, funcs, seen = [], [], {}
    for ch in channels:
        if ch.name in NAMED_CHANNELS:
            value = _named_conversion(ch.name, cfg)
        elif ch.conv in LINEAR_CONV_CODES:
            value = lambda r, a=ch.scale, b=ch.offset: r * a + b
        else:
            value = None
        name = ch.name
        if value is None:
            name += ' [raw]'
            funcs.append(str)
        else:
            funcs.append(lambda r, v=value, d=ecu_log.decimals(ch.fmt): f'{v(r):.{d}f}')
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name += f' #{seen[name]}'
        names.append(name)
    return names, funcs


# ---------------------------------------------------------------------------
# GPS (spec.md section 4, Tier 1)
# ---------------------------------------------------------------------------
GPS_COLUMNS = ['GPS_Timestamp', 'GPS_Latitude', 'GPS_Longitude', 'GPS_Speed_MPH']
KNOTS_TO_MPH = 1.150779


def _nmea_degrees(text, hemisphere):
    value = float(text)
    degrees = int(value / 100)
    result = degrees + (value - degrees * 100) / 60
    return -result if hemisphere in ('S', 'W') else result


def parse_nmea(line):
    """Return (utc datetime, lat, lon, mph) from an RMC or ZDA sentence, else None.

    lat, lon and mph are None for ZDA (it carries time only).
    """
    line = line.strip()
    if not line.startswith('$'):
        return None
    body, star, checksum = line[1:].partition('*')
    if star:
        calc = 0
        for ch in body:
            calc ^= ord(ch)
        try:
            if calc != int(checksum[:2], 16):
                return None
        except ValueError:
            return None
    f = body.split(',')
    try:
        if f[0][2:] == 'RMC' and f[2] == 'A':
            when = datetime.strptime(f[9] + f[1].split('.')[0], '%d%m%y%H%M%S')
            frac = float('0.' + f[1].split('.')[1]) if '.' in f[1] else 0.0
            when = when.replace(tzinfo=timezone.utc) + timedelta(seconds=frac)
            return (when, _nmea_degrees(f[3], f[4]), _nmea_degrees(f[5], f[6]),
                    float(f[7] or 0) * KNOTS_TO_MPH)
        if f[0][2:] == 'ZDA' and f[1] and int(f[4]) > 2000:
            when = datetime(int(f[4]), int(f[3]), int(f[2]), int(f[1][0:2]), int(f[1][2:4]),
                            int(f[1][4:6]), tzinfo=timezone.utc)
            return when, None, None, None
    except (ValueError, IndexError):
        pass
    return None


class GpsReader(threading.Thread):
    def __init__(self, open_port, set_system_clock=True):
        super().__init__(name='gps', daemon=True)
        self._open_port = open_port
        self._set_system_clock = set_system_clock
        self._lock = threading.Lock()
        self._fix = None                    # (utc, monotonic, lat, lon, mph)
        self._last_clock_try = None
        self.clock_synced = threading.Event()
        self.stop = threading.Event()

    def run(self):
        reported = False
        while not self.stop.is_set():
            port = None
            try:
                port = self._open_port()
                reported = False
                while not self.stop.is_set():
                    line = port.readline()
                    if line:
                        self.handle(line.decode('ascii', 'ignore'))
            except OSError as e:
                if not reported:
                    log.warning('GPS port problem: %s (trying again every 5 s)', e)
                    reported = True
            finally:
                if port is not None:
                    try:
                        port.close()
                    except OSError:
                        pass
            self.stop.wait(5)

    def handle(self, line):
        parsed = parse_nmea(line)
        if not parsed:
            return
        when, lat, lon, mph = parsed
        with self._lock:
            if lat is None and self._fix:
                lat, lon, mph = self._fix[2:]
            self._fix = (when, time.monotonic(), lat, lon, mph)
        if not self.clock_synced.is_set():
            self._sync_clock(when)

    def _sync_clock(self, when):
        if not self._set_system_clock:
            self.clock_synced.set()
            return
        now = time.monotonic()
        if self._last_clock_try is not None and now - self._last_clock_try < 30:
            return
        self._last_clock_try = now
        error = abs((when - datetime.now(timezone.utc)).total_seconds())
        try:
            done = subprocess.run(['date', '-u', '-s', when.strftime('%Y-%m-%d %H:%M:%S')],
                                  capture_output=True, text=True)
            ok, detail = done.returncode == 0, done.stderr.strip()
        except OSError as e:
            ok, detail = False, str(e)
        if ok:
            log.info('system clock set from GPS: %s', when.isoformat())
            self.clock_synced.set()
        elif error < 2:
            log.info('system clock already agrees with GPS (could not set it: %s)', detail)
            self.clock_synced.set()
        else:
            log.error('cannot set the system clock from GPS (%s); it is %.0f s off, so '
                      'sessions go to unsynced/', detail, error)

    def row_fields(self):
        """The four GPS columns for a row written now. Blank before the first fix."""
        with self._lock:
            fix = self._fix
        if fix is None:
            return ['', '', '', '']
        when, mono, lat, lon, mph = fix
        t = when + timedelta(seconds=time.monotonic() - mono)
        stamp = t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'
        if lat is None:
            return [stamp, '', '', '']
        return [stamp, f'{lat:.6f}', f'{lon:.6f}', f'{mph:.1f}']


# ---------------------------------------------------------------------------
# Collector state machine (spec.md 3.D.3 to 3.D.5)
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, path, handle, channels, order, funcs):
        self.path, self.handle = path, handle
        self.writer = csv.writer(handle)
        self.channels, self.order, self.funcs = channels, order, funcs
        self.last_flush = time.monotonic()


class Collector:
    def __init__(self, cfg, open_ecu_port, gps):
        self.cfg = cfg
        self._open_ecu_port = open_ecu_port
        self.gps = gps
        self.commands = queue.Queue()
        self.shutdown = threading.Event()
        self._ser = None
        self._link = None
        self._session = None
        self._lock = threading.Lock()
        self._status = {'state': 'IDLE', 'file': None, 'rows': 0, 'failed_polls': 0,
                        'last_row_time': None, 'channels': 0, 'failed_attempts': 0,
                        'last_error': None}
        self._next_attempt = None
        self._tz = None
        if cfg['FILE_TIMEZONE']:
            from zoneinfo import ZoneInfo
            self._tz = ZoneInfo(cfg['FILE_TIMEZONE'])

    # ---- status ---------------------------------------------------------------
    def _set(self, **changes):
        with self._lock:
            self._status.update(changes)

    def status(self):
        with self._lock:
            out = dict(self._status)
        if out['state'] == 'WAITING' and self._next_attempt is not None:
            out['next_attempt_in_s'] = round(max(0.0, self._next_attempt - time.monotonic()), 1)
        else:
            out.pop('failed_attempts')
        return out

    # ---- main loop ------------------------------------------------------------
    def run(self):
        try:
            self._wait_for_clock()
            if self.cfg['ECU_AUTOSTART'] and not self.shutdown.is_set():
                self._start_with_retries()
            while not self.shutdown.is_set():
                state = self._status['state']
                if state == 'IDLE':
                    if self._command(0.2) == 'NEW_SESSION':
                        self._start_with_retries()
                elif state == 'WAITING':
                    wait = max(0.0, self._next_attempt - time.monotonic())
                    cmd = self._command(min(wait, 0.2))
                    if cmd == 'STOP':
                        self._set(state='IDLE', failed_attempts=0)
                    elif cmd == 'NEW_SESSION' or time.monotonic() >= self._next_attempt:
                        started = time.monotonic()
                        if not self._attempt():
                            self._next_attempt = started + self.cfg['ECU_RETRY_INTERVAL_S']
                            self._set(state='WAITING')
                elif state == 'LOGGING':
                    reason = self._log_session()
                    self._close_session()
                    if reason == 'STOP':
                        self._set(state='IDLE')
                    elif reason == 'NEW_SESSION':
                        self._start_with_retries()
                    elif reason == 'LOST':
                        self._next_attempt = time.monotonic()       # first retry at once
                        self._set(state='WAITING')
        finally:
            self._close_session()
            self._close_port()

    def _command(self, timeout):
        try:
            return self.commands.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def _wait_for_clock(self):
        deadline = time.monotonic() + self.cfg['GPS_SYNC_WAIT_S']
        while not self.shutdown.is_set() and time.monotonic() < deadline:
            if self.gps.clock_synced.wait(0.2):
                return
        if not self.gps.clock_synced.is_set() and not self.shutdown.is_set():
            log.warning('no GPS time after %.0f s; carrying on without it',
                        self.cfg['GPS_SYNC_WAIT_S'])

    def _start_with_retries(self):
        started = time.monotonic()
        for _ in range(max(1, self.cfg['ECU_START_RETRIES'])):
            if self.shutdown.is_set():
                return
            started = time.monotonic()
            if self._attempt():
                return
        self._next_attempt = started + self.cfg['ECU_RETRY_INTERVAL_S']
        self._set(state='WAITING')

    # ---- one start attempt (STARTING) --------------------------------------------
    def _attempt(self):
        self._set(state='STARTING')
        try:
            if self._ser is None:
                self._ser = self._open_ecu_port()
                self._link = EcuLink(self._ser, self.cfg['ECU_ECHO_TIMEOUT_MS'] / 1000,
                                     self.cfg['ECU_REPLY_TIMEOUT_MS'] / 1000,
                                     self.cfg['ECU_POLL_TABLE_MAX_CHANNELS'])
            template = self.cfg['ECU_CHANNEL_TEMPLATE']
            channels, note = ecu_log.load_template(template)
            if note:
                log.warning('%s: %s', template, note)
            order = ecu_log.poll_order(channels)
            names, funcs = make_converters(channels, self.cfg)

            ident = self._link.identity().decode('ascii', 'replace')
            if ident != self.cfg['ECU_IDENT_EXPECT']:
                log.warning('ECU identity is "%s", expected "%s"', ident,
                            self.cfg['ECU_IDENT_EXPECT'])
            self._link.write_poll_table([channels[k].id for k in order])
            reply = self._link.poll(len(channels))

            path, handle = self._new_log_file()
            self._session = Session(path, handle, channels, order, funcs)
            self._session.writer.writerow(GPS_COLUMNS + names)
        except (EcuError, OSError, ecu_log.FormatError) as e:
            if isinstance(e, OSError):
                self._close_port()
            with self._lock:
                self._status['failed_attempts'] += 1
                self._status['last_error'] = str(e)
                count = self._status['failed_attempts']
            if count == 1:
                log.error('ECU not responding: %s', e)
            else:
                log.warning('ECU attempt %d failed: %s', count, e)
            return False
        with self._lock:
            again = self._status['failed_attempts'] > 0
        log.info('%s; logging %d channels to %s',
                 'ECU connection made again' if again else 'ECU connected', len(channels), path)
        self._set(state='LOGGING', file=path, rows=0, failed_polls=0, last_row_time=None,
                  channels=len(channels), failed_attempts=0, last_error=None)
        self._write_row(reply)
        return True

    # ---- LOGGING -------------------------------------------------------------------
    def _log_session(self):
        """Poll until the session ends. Returns STOP, NEW_SESSION, LOST or SHUTDOWN."""
        interval = self.cfg['ECU_POLL_INTERVAL_MS'] / 1000
        count = len(self._session.channels)
        start, slot, failures = time.monotonic(), 1, 0
        while True:
            due = start + slot * interval
            while True:
                if self.shutdown.is_set():
                    return 'SHUTDOWN'
                remaining = due - time.monotonic()
                cmd = self._command(min(max(remaining, 0.0), 0.2))
                if cmd in ('STOP', 'NEW_SESSION'):
                    return cmd
                if time.monotonic() >= due:
                    break
            try:
                reply = self._link.poll(count)
                failures = 0
                self._write_row(reply)
            except EcuError as e:
                failures += 1
                with self._lock:
                    self._status['failed_polls'] += 1
                if failures >= self.cfg['ECU_POLL_FAILURES_MAX']:
                    log.error('ECU lost after %d failed polls: %s', failures, e)
                    self._set(last_error=str(e))
                    return 'LOST'
            except OSError as e:
                log.error('ECU port or file error: %s', e)
                self._set(last_error=str(e))
                self._close_port()
                return 'LOST'
            # the next slot that is still in the future: a late poll is skipped
            slot = max(slot + 1, int((time.monotonic() - start) / interval) + 1)

    def _write_row(self, reply):
        s = self._session
        values = [''] * len(s.channels)
        for position, k in enumerate(s.order):
            values[k] = s.funcs[k](reply[position])
        s.writer.writerow(self.gps.row_fields() + values)
        with self._lock:
            self._status['rows'] += 1
            self._status['last_row_time'] = datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ')
        now = time.monotonic()
        if now - s.last_flush >= self.cfg['FLUSH_INTERVAL_S']:
            s.handle.flush()
            os.fsync(s.handle.fileno())
            s.last_flush = now

    def _close_session(self):
        if self._session is None:
            return
        try:
            self._session.handle.flush()
            os.fsync(self._session.handle.fileno())
            self._session.handle.close()
        except OSError as e:
            log.error('closing %s: %s', self._session.path, e)
        log.info('session closed: %s (%d rows)', self._session.path, self._status['rows'])
        self._session = None

    def _close_port(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except OSError:
                pass
        self._ser = self._link = None

    # ---- file naming (spec.md 3.A) -------------------------------------------------
    def _new_log_file(self):
        data = self.cfg['DATA_DIR']
        if self.gps.clock_synced.is_set():
            now = datetime.now(self._tz)
            folder = os.path.join(data, now.strftime('%Y-%m-%d'))
            base = 'log_' + now.strftime('%H%M')
            os.makedirs(folder, exist_ok=True)
        else:
            folder = os.path.join(data, 'unsynced')
            os.makedirs(folder, exist_ok=True)
            base = f'log_boot{self._boot_number(folder)}_{int(time.monotonic())}'
        n = 1
        while True:
            path = os.path.join(folder, base + ('' if n == 1 else f'_{n}') + '.csv')
            try:
                return path, open(path, 'x', newline='')
            except FileExistsError:
                n += 1

    @staticmethod
    def _boot_number(folder):
        """A counter that goes up by one each time the Pi boots."""
        try:
            with open('/proc/sys/kernel/random/boot_id') as f:
                boot_id = f.read().strip()
        except OSError:
            boot_id = 'unknown'
        record = os.path.join(folder, '.boot')
        number, last_id = 0, None
        try:
            with open(record) as f:
                last_id, text = f.read().split()
            number = int(text)
        except (OSError, ValueError):
            pass
        if last_id != boot_id:
            number += 1
            with open(record, 'w') as f:
                f.write(f'{boot_id} {number}\n')
        return number


# ---------------------------------------------------------------------------
# Control socket (spec.md 3.D.4)
# ---------------------------------------------------------------------------
class ControlServer(threading.Thread):
    """One request per connection: a command line in, one JSON line out."""

    def __init__(self, path, collector, mock_ecu=None):
        super().__init__(name='control', daemon=True)
        self.path, self.collector, self.mock_ecu = path, collector, mock_ecu
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o660)
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)
                    line = conn.makefile('r').readline().strip()
                    conn.sendall((json.dumps(self.handle(line)) + '\n').encode())
                except OSError:
                    pass
        self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def handle(self, line):
        words = line.split()
        cmd = words[0].upper() if words else ''
        if cmd == 'STATUS':
            return self.collector.status()
        if cmd in ('NEW_SESSION', 'STOP'):
            self.collector.commands.put(cmd)
            return {'ok': True}
        if cmd == 'MOCK' and self.mock_ecu is not None and len(words) == 3:
            switch, value = words[1].lower(), words[2].lower()
            if switch in ('silent', 'noecho') and value in ('on', 'off'):
                setattr(self.mock_ecu, switch, value == 'on')
                return {'ok': True}
            if switch == 'badsum' and value.isdigit():
                self.mock_ecu.badsum = int(value)
                return {'ok': True}
        return {'ok': False, 'error': f'unknown command: {line}'}


# ---------------------------------------------------------------------------
def main(argv=None):
    args, cfg = load_config(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    mock_ecu = None
    if args.mock:
        import mock_serial
        mock_ecu = mock_serial.MockEcu(args.mock_replay, silent=args.mock_silent)

        def open_ecu():
            return mock_ecu
        open_gps = mock_serial.MockGps
        log.info('mock mode: files in %s, control socket %s', cfg['DATA_DIR'],
                 cfg['CONTROL_SOCKET'])
    else:
        import serial

        def open_ecu():
            port = serial.Serial()
            port.port = cfg['ECU_PORT']
            port.baudrate = 9600
            port.bytesize, port.parity, port.stopbits = 8, 'N', 1
            port.xonxoff = port.rtscts = port.dsrdtr = False
            port.dtr, port.rts = True, False
            port.timeout = 0.1
            port.open()
            return port

        def open_gps():
            return serial.Serial(cfg['GPS_PORT'], cfg['GPS_BAUD'], timeout=1)

    try:
        os.makedirs(cfg['DATA_DIR'], exist_ok=True)
    except OSError as e:
        raise SystemExit(f'cannot use DATA_DIR {cfg["DATA_DIR"]}: {e}')
    gps = GpsReader(open_gps, set_system_clock=not args.mock)
    collector = Collector(cfg, open_ecu, gps)
    try:
        control = ControlServer(cfg['CONTROL_SOCKET'], collector, mock_ecu)
    except OSError as e:
        raise SystemExit(f'cannot create control socket {cfg["CONTROL_SOCKET"]}: {e}')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: collector.shutdown.set())
    gps.start()
    control.start()
    try:
        collector.run()
    finally:
        control.stop.set()
        gps.stop.set()
        control.join(2)
    log.info('collector stopped')


if __name__ == '__main__':
    main()
