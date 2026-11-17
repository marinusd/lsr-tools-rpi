#!/usr/bin/env python3
"""Tests for the collector against the mock ECU (spec.md section 7).

    python3 -m unittest -v tests/test_collector.py
"""
import csv
import glob
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import collector as col          # noqa: E402
import parse_ecu_log                   # noqa: E402
import mock_serial               # noqa: E402

LOGS = os.path.join(ROOT, 'C-ComWP-logs')
SAMPLE_1 = os.path.join(LOGS, 'world_finals_run_4.log')
SAMPLE_2 = os.path.join(LOGS, 'dyno_run_2.log')
FAST = os.path.join(LOGS, 'capture_run.log')
CAPTURED_TABLE = [0x46, 0x24, 0x28, 0x22, 0x26, 0x83, 0xB5, 0x4A, 0x92, 0x2A, 0x96, 0x00]


def load(path):
    buf = parse_ecu_log.read_bytes(path)
    hdr = parse_ecu_log.parse_header(buf)
    return hdr, parse_ecu_log.records(buf, hdr)


class StubGps:
    def __init__(self, synced=True):
        self.clock_synced = threading.Event()
        if synced:
            self.clock_synced.set()

    def row_fields(self):
        return ['2026-11-13T18:11:23.000Z', '39.739200', '-104.990300', '42.0']


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class ParserTest(unittest.TestCase):
    """spec.md 3.C.8"""

    def test_capture_run(self):
        hdr, recs = load(FAST)
        self.assertEqual((len(hdr.channels), hdr.record_size, len(recs)), (11, 26, 175))
        self.assertEqual(recs[-1][0], 17406)
        self.assertEqual([c.name for c in hdr.channels], [c.name for c in load(SAMPLE_1)[0].channels])

    def test_sample_1(self):
        hdr, recs = load(SAMPLE_1)
        self.assertEqual((len(hdr.channels), hdr.record_size, len(recs)), (11, 26, 3146))
        self.assertEqual((recs[0][0], recs[-1][0]), (0, 314500))
        self.assertEqual([c.name for c in hdr.channels][:3], ['TPS (%)', 'RPM', 'Battery (V)'])

    def test_sample_2(self):
        hdr, recs = load(SAMPLE_2)
        self.assertEqual((len(hdr.channels), hdr.record_size, len(recs)), (13, 30, 634))
        self.assertEqual(recs[-1][0], 63296)
        self.assertEqual([c.name for c in hdr.channels][:3], ['Scaled RPM', 'Coolant(*F)', 'Actual A/F'])

    def test_cut_off_file_gives_complete_records_only(self):
        buf = parse_ecu_log.read_bytes(FAST)[:-10]
        hdr = parse_ecu_log.parse_header(buf)
        self.assertEqual(len(parse_ecu_log.records(buf, hdr)), 174)


class TemplateTest(unittest.TestCase):
    def test_text_template_equals_the_log_it_came_from(self):
        for sample in (SAMPLE_1, SAMPLE_2, FAST):
            hdr, _ = load(sample)
            again = parse_ecu_log.parse_template_text(parse_ecu_log.template_text(hdr.channels))
            self.assertEqual(again, hdr.channels)

    def test_shipped_template_is_world_finals_run_4(self):
        channels, note = parse_ecu_log.load_template(os.path.join(ROOT, 'CHANNEL_TEMPLATE.txt'))
        self.assertIsNone(note)
        self.assertEqual(channels, load(SAMPLE_1)[0].channels)

    def test_bad_text_template(self):
        with self.assertRaises(parse_ecu_log.FormatError):
            parse_ecu_log.parse_template_text('name,unit\nRPM,\n')
        good = parse_ecu_log.template_text(load(SAMPLE_1)[0].channels)
        with self.assertRaises(parse_ecu_log.FormatError):
            parse_ecu_log.parse_template_text(good.replace('0x46', 'zz'))


class LinkTest(unittest.TestCase):
    """spec.md 3.D.1 and 3.D.2, against the bytes seen in the capture"""

    def setUp(self):
        self.ecu = mock_serial.MockEcu(FAST)
        self.link = col.EcuLink(self.ecu, 0.05, 0.06, 11)
        self.hdr, self.recs = load(FAST)
        self.order = parse_ecu_log.poll_order(self.hdr.channels)

    def test_identity(self):
        self.assertEqual(self.link.identity(), b'SEFI8LO')

    def test_poll_table_is_what_the_pc_wrote(self):
        self.link.write_poll_table([self.hdr.channels[k].id for k in self.order])
        self.assertEqual(self.ecu.fe_writes,
                         [(0x00B6 + i, v) for i, v in enumerate(CAPTURED_TABLE)])

    def test_poll_reply_is_in_table_order(self):
        self.link.write_poll_table([self.hdr.channels[k].id for k in self.order])
        reply = self.link.poll(11)
        # first record of capture_run.log, as the ECU sent it on the wire
        self.assertEqual(reply.hex(' '), '00 b2 fe 1c 6d 50 00 80 00 f0 ff')

    def test_write_guard(self):
        with self.assertRaises(col.EcuError):
            self.link.write_poll_table([0x46] * 12)
        with self.assertRaises(col.EcuError):
            self.link.write_poll_table([0x46, 0x00])
        with self.assertRaises(col.EcuError):
            self.link._write_poll_table_slot(12, 0)
        self.assertEqual(self.ecu.fe_writes, [])

    def test_bad_checksum_and_missing_echo(self):
        self.ecu.badsum = 1
        with self.assertRaises(col.EcuError):
            self.link.identity()
        self.assertEqual(self.link.identity(), b'SEFI8LO')
        self.ecu.noecho = True
        with self.assertRaises(col.EcuError):
            self.link.identity()
        self.ecu.noecho, self.ecu.silent = False, True
        with self.assertRaises(col.EcuError):
            self.link.identity()


class GpsTest(unittest.TestCase):
    def test_rmc(self):
        when, lat, lon, mph = col.parse_nmea(
            '$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A')
        self.assertEqual(when.strftime('%Y-%m-%dT%H:%M:%S'), '1994-03-23T12:35:19')
        self.assertAlmostEqual(lat, 48.1173, places=4)
        self.assertAlmostEqual(lon, 11.5167, places=4)
        self.assertAlmostEqual(mph, 22.4 * 1.150779, places=3)

    def test_rejects_bad_checksum_and_void_fix(self):
        self.assertIsNone(col.parse_nmea(
            '$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6B'))
        self.assertIsNone(col.parse_nmea('$GPRMC,123519,V,,,,,,,230394,,'))

    def test_mock_gps_feeds_reader(self):
        gps = col.GpsReader(mock_serial.MockGps, set_system_clock=False)
        self.assertEqual(gps.row_fields(), ['', '', '', ''])
        gps.handle(mock_serial.MockGps().readline().decode())
        fields = gps.row_fields()
        self.assertTrue(gps.clock_synced.is_set())
        self.assertRegex(fields[0], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
        self.assertGreater(float(fields[3]), 0)


class CollectorTest(unittest.TestCase):
    """spec.md section 7, items 1, 2, 4 and 5 (with short intervals)"""

    def start(self, template=SAMPLE_1, replay=SAMPLE_1, silent=False, synced=True, **overrides):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = {name: default for name, _k, default, _h in col.PARAMS}
        cfg.update(DATA_DIR=self.tmp.name, ECU_CHANNEL_TEMPLATE=template,
                   ECU_POLL_INTERVAL_MS=10, ECU_ECHO_TIMEOUT_MS=20, ECU_REPLY_TIMEOUT_MS=20,
                   ECU_POLL_FAILURES_MAX=5, ECU_RETRY_INTERVAL_S=0.3, GPS_SYNC_WAIT_S=0,
                   FLUSH_INTERVAL_S=0.05)
        cfg.update(overrides)
        self.ecu = mock_serial.MockEcu(replay, silent=silent)
        self.c = col.Collector(cfg, lambda: self.ecu, StubGps(synced))
        self.thread = threading.Thread(target=self.c.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.c.shutdown.set()
        self.thread.join(3)

    def state(self):
        return self.c.status()['state']

    def files(self):
        return sorted(glob.glob(os.path.join(self.tmp.name, '*', '*.csv')))

    def rows(self, path):
        with open(path, newline='') as f:
            return list(csv.reader(f))

    def test_session_file_and_columns(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 20))
        self.c.commands.put('STOP')
        self.assertTrue(wait_for(lambda: self.state() == 'IDLE'))
        polls = self.ecu.poll_count
        time.sleep(0.1)
        self.assertEqual(self.ecu.poll_count, polls, 'nothing is sent after STOP')
        (path,) = self.files()
        self.assertRegex(path, r'/\d{4}-\d\d-\d\d/log_\d{4}\.csv$')
        rows = self.rows(path)
        self.assertEqual(rows[0], col.GPS_COLUMNS + [
            'TPS (%)', 'RPM', 'Battery (V)', 'Actual A/F', 'Target A/F', 'O2 Corr. (%)',
            'MAP (PSIA) [raw]', 'Air Temp(*F) [raw]', 'Coolant(*F) [raw]', 'Spark(*BTDC)',
            'Inj. DC (%)'])
        # first record of world_finals_run_4.log (spec 3.C.3), in each channel's own format
        self.assertEqual(rows[1][4:], ['22', '4000', '10.8', '12.81', '13.4', '23.4',
                                       '156', '128', '39', '35.75', '5.2'])
        self.assertEqual(rows[1][:4], StubGps().row_fields())
        self.assertEqual(len(rows) - 1, self.c.status()['rows'])
        self.assertEqual(self.ecu.fe_writes,
                         [(0x00B6 + i, v) for i, v in enumerate(CAPTURED_TABLE)])

    def test_new_session_makes_a_second_file(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 5))
        first = self.c.status()['file']
        self.c.commands.put('NEW_SESSION')
        self.assertTrue(wait_for(lambda: self.c.status()['file'] != first
                                 and self.c.status()['rows'] >= 5))
        self.assertEqual(len(self.files()), 2)
        self.assertEqual(len(self.ecu.fe_writes), 24, 'poll table written for each session')
        if os.path.dirname(first) == os.path.dirname(self.c.status()['file']) and \
                first[-8:-4] == self.c.status()['file'][-10:-6]:
            self.assertTrue(self.c.status()['file'].endswith('_2.csv'))

    def test_13_channels_need_the_limit_raised(self):
        self.start(template=SAMPLE_2, replay=SAMPLE_2)
        self.assertTrue(wait_for(lambda: self.state() == 'WAITING'))
        self.assertIn('ECU_POLL_TABLE_MAX_CHANNELS', self.c.status()['last_error'])
        self.assertEqual(self.ecu.fe_writes, [])
        self.assertEqual(self.files(), [])

    def test_13_channels(self):
        self.start(template=SAMPLE_2, replay=SAMPLE_2, ECU_POLL_TABLE_MAX_CHANNELS=13)
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 5))
        self.c.commands.put('STOP')
        self.assertTrue(wait_for(lambda: self.state() == 'IDLE'))
        rows = self.rows(self.files()[0])
        self.assertEqual(rows[0][4:], [
            'Scaled RPM [raw]', 'Coolant(*F) [raw]', 'Actual A/F', 'Battery (V)', 'TPS (%)',
            'Spark(*BTDC)', 'O2 Corr. (%)', 'Inj. DC (%)', 'MAP (PSIA) [raw]',
            'Air Temp(*F) [raw]', 'Target A/F', 'Fuel #s/hr [raw]', 'RPM'])
        self.assertEqual(rows[1][4:6] + rows[1][-2:], ['59', '74', '41', '2200'])

    def test_configured_conversions(self):
        table = os.path.join(tempfile.gettempdir(), 'coolant_test_table.csv')
        with open(table, 'w') as f:
            f.write('raw,degF\n20,220\n40,180\n')
        self.addCleanup(os.remove, table)
        self.start(MAP_PSIA_PER_COUNT=0.0597, TEMP_TABLE_COOLANT=table)
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 2))
        self.c.commands.put('STOP')
        self.assertTrue(wait_for(lambda: self.state() == 'IDLE'))
        rows = self.rows(self.files()[0])
        self.assertIn('MAP (PSIA)', rows[0])
        self.assertIn('Coolant(*F)', rows[0])
        self.assertEqual(rows[1][rows[0].index('MAP (PSIA)')], '9.3')       # 156 x 0.0597
        self.assertEqual(rows[1][rows[0].index('Coolant(*F)')], '182.0')    # raw 39

    def test_bad_checksum_skips_one_row_only(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 5))
        self.ecu.badsum = 2
        self.assertTrue(wait_for(lambda: self.c.status()['failed_polls'] == 2))
        before = self.c.status()['rows']
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= before + 5))
        self.assertEqual(self.state(), 'LOGGING')
        self.assertEqual(len(self.files()), 1)

    def test_silent_ecu_then_response(self):
        self.start(silent=True)
        self.assertTrue(wait_for(lambda: self.state() == 'WAITING'))
        self.assertTrue(wait_for(lambda: self.c.status().get('failed_attempts', 0) >= 5))
        self.assertIn('next_attempt_in_s', self.c.status())
        self.assertEqual(self.files(), [])
        self.ecu.silent = False
        self.assertTrue(wait_for(lambda: self.state() == 'LOGGING'))
        self.assertEqual(len(self.files()), 1)

    def test_dropout_mid_session_starts_a_new_file(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 5))
        self.ecu.silent = True
        self.assertTrue(wait_for(lambda: self.state() == 'WAITING'))
        self.ecu.silent = False
        self.assertTrue(wait_for(lambda: self.state() == 'LOGGING'))
        self.assertEqual(len(self.files()), 2)
        self.assertEqual(len(self.ecu.fe_writes), 24, 'poll table written again')

    def test_stop_while_waiting_ends_the_retries(self):
        self.start(silent=True)
        self.assertTrue(wait_for(lambda: self.state() == 'WAITING'))
        self.c.commands.put('STOP')
        self.assertTrue(wait_for(lambda: self.state() == 'IDLE'))
        self.ecu.silent = False
        time.sleep(0.6)
        self.assertEqual(self.state(), 'IDLE')
        self.c.commands.put('NEW_SESSION')
        self.assertTrue(wait_for(lambda: self.state() == 'LOGGING'))

    def test_text_template_gives_the_same_file(self):
        self.start(template=os.path.join(ROOT, 'CHANNEL_TEMPLATE.txt'))
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 2))
        self.c.commands.put('STOP')
        self.assertTrue(wait_for(lambda: self.state() == 'IDLE'))
        rows = self.rows(self.files()[0])
        self.assertEqual(rows[0][4:7], ['TPS (%)', 'RPM', 'Battery (V)'])
        self.assertEqual(rows[1][4:], ['22', '4000', '10.8', '12.81', '13.4', '23.4',
                                       '156', '128', '39', '35.75', '5.2'])

    def test_no_autostart(self):
        self.start(ECU_AUTOSTART=False)
        time.sleep(0.3)
        self.assertEqual((self.state(), self.ecu.poll_count), ('IDLE', 0))

    def test_unsynced_clock_goes_to_unsynced_folder(self):
        self.start(synced=False)
        self.assertTrue(wait_for(lambda: self.c.status()['rows'] >= 2))
        self.assertRegex(self.c.status()['file'], r'/unsynced/log_boot\d+_\d+\.csv$')


if __name__ == '__main__':
    unittest.main()
