#!/usr/bin/env python3
"""Tests for the web interface and the plot generator (spec.md section 7, item 3).

    python3 -m unittest -v tests/test_server.py
"""
import glob
import os
import resource
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import collector as col          # noqa: E402
import mock_serial               # noqa: E402
import server                    # noqa: E402

SAMPLE_1 = os.path.join(ROOT, 'ecu_sample.log')


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class ServerTest(unittest.TestCase):
    """A real collector with the mock ECU and mock GPS, and the web app on top."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = os.path.join(cls.tmp.name, 'data')
        sock = os.path.join(cls.tmp.name, 'control.sock')
        cfg = {name: default for name, _k, default, _h in col.PARAMS}
        cfg.update(DATA_DIR=cls.data, ECU_POLL_INTERVAL_MS=10, ECU_ECHO_TIMEOUT_MS=20,
                   ECU_REPLY_TIMEOUT_MS=20, GPS_SYNC_WAIT_S=5, FLUSH_INTERVAL_S=0.05)
        cls.ecu = mock_serial.MockEcu(SAMPLE_1)
        cls.gps = col.GpsReader(mock_serial.MockGps, set_system_clock=False)
        cls.c = col.Collector(cfg, lambda: cls.ecu, cls.gps)
        cls.control = col.ControlServer(sock, cls.c, cls.ecu)
        cls.gps.start()
        cls.control.start()
        cls.thread = threading.Thread(target=cls.c.run, daemon=True)
        cls.thread.start()
        server.configure(cls.data, sock)
        cls.web = server.app.test_client()
        assert wait_for(lambda: cls.c.status()['rows'] >= 150, 10)

    @classmethod
    def tearDownClass(cls):
        cls.c.shutdown.set()
        cls.thread.join(3)
        cls.control.stop.set()
        cls.gps.stop.set()
        cls.control.join(2)
        cls.tmp.cleanup()

    def day_and_file(self):
        path = sorted(glob.glob(os.path.join(self.data, '*', 'log_*.csv')))[0]
        return os.path.basename(os.path.dirname(path)), os.path.basename(path)

    def test_1_index_shows_status_and_days(self):
        page = self.web.get('/').get_data(as_text=True)
        day, name = self.day_and_file()
        self.assertIn('LOGGING', page)
        self.assertIn(name, page)
        self.assertIn(f'/day/{day}/', page)
        self.assertIn('New session', page)
        self.assertIn('Stop logging', page)

    def test_2_day_page_and_download(self):
        day, name = self.day_and_file()
        page = self.web.get(f'/day/{day}/').get_data(as_text=True)
        self.assertIn(name, page)
        self.assertIn('logging now', page)
        got = self.web.get(f'/day/{day}/{name}')
        self.assertEqual(got.status_code, 200)
        self.assertIn('attachment', got.headers['Content-Disposition'])
        first = got.get_data(as_text=True).splitlines()[0]
        self.assertTrue(first.startswith('GPS_Timestamp,GPS_Latitude,GPS_Longitude,GPS_Speed_MPH,TPS (%)'))
        got.close()

    def test_3_plot_form_lists_this_files_columns(self):
        day, name = self.day_and_file()
        page = self.web.get(f'/day/{day}/{name}/plot').get_data(as_text=True)
        for column in ('GPS_Speed_MPH', 'RPM', 'MAP (PSIA) [raw]', 'Inj. DC (%)'):
            self.assertIn(f'value="{column}"', page)
        self.assertNotIn('value="GPS_Timestamp"', page)
        self.assertRegex(page, r'value="RPM" checked')

    def test_4_plot_is_written_next_to_the_csv(self):
        day, name = self.day_and_file()
        reply = self.web.post(f'/day/{day}/{name}/plot',
                              data={'metric': ['RPM', 'GPS_Speed_MPH']})
        self.assertEqual(reply.status_code, 302)
        expected = 'plot_' + name[4:-4] + '_GPS_Speed_MPH-RPM.png'
        self.assertTrue(reply.headers['Location'].endswith(f'/day/{day}/plots/{expected}'))
        path = os.path.join(self.data, day, expected)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')
        self.assertGreater(os.path.getsize(path), 5000)
        image = self.web.get(f'/day/{day}/plots/{expected}/image')
        self.assertEqual(image.mimetype, 'image/png')
        image.close()
        self.assertIn(expected, self.web.get(f'/day/{day}/').get_data(as_text=True))

    def test_5_plot_needs_a_column_and_rejects_unknown_ones(self):
        day, name = self.day_and_file()
        reply = self.web.post(f'/day/{day}/{name}/plot', data={'metric': ['No such column']})
        self.assertIn('Choose+at+least+one+column', reply.headers['Location'])

    def test_6_memory_stays_flat_over_repeated_plots(self):
        day, name = self.day_and_file()
        fixed = os.path.join(self.tmp.name, 'fixed.csv')     # a file that is not growing
        shutil.copyfile(os.path.join(self.data, day, name), fixed)
        out = os.path.join(self.data, day, 'plot_memtest_RPM.png')

        def resident_mb():
            with open('/proc/self/statm') as f:
                return int(f.read().split()[1]) * resource.getpagesize() / 1e6

        for _ in range(25):                                      # let the allocator settle
            server.render_plot(fixed, ['RPM', 'GPS_Speed_MPH'], out)
        self.assertEqual(server.pyplot().get_fignums(), [], 'every figure was closed')
        if not os.path.exists('/proc/self/statm'):
            self.skipTest('memory reading needs Linux')
        before = resident_mb()
        for _ in range(40):
            server.render_plot(fixed, ['RPM', 'GPS_Speed_MPH'], out)
        self.assertEqual(server.pyplot().get_fignums(), [])
        self.assertLess(resident_mb() - before, 15, 'memory grew by under 15 MB in 40 plots')

    def test_7_bad_paths_are_refused(self):
        day, name = self.day_and_file()
        for url in ('/day/..%2F..%2Fetc/', f'/day/{day}/passwd', f'/day/{day}/..%2Fx.csv',
                    f'/day/{day}/plots/log_x.csv/image', '/day/2020-01-01/',
                    f'/day/{day}/plots/plot_none.png'):
            self.assertEqual(self.web.get(url).status_code, 404, url)
        self.assertEqual(self.web.post('/command', data={'command': 'MOCK silent on'}).status_code, 400)

    def test_8_buttons(self):
        first = self.c.status()['file']
        reply = self.web.post('/command', data={'command': 'NEW_SESSION'})
        self.assertIn('New+session+requested', reply.headers['Location'])
        self.assertTrue(wait_for(lambda: self.c.status()['file'] != first))
        reply = self.web.post('/command', data={'command': 'STOP'})
        self.assertIn('Logging+stopped', reply.headers['Location'])
        self.assertTrue(wait_for(lambda: self.c.status()['state'] == 'IDLE'))
        self.assertIn('IDLE', self.web.get('/').get_data(as_text=True))

    def test_9_collector_down(self):
        server.configure(self.data, os.path.join(self.tmp.name, 'nothing.sock'))
        try:
            page = self.web.get('/').get_data(as_text=True)
        finally:
            server.configure(self.data, self.control.path)
        self.assertIn('collector not reachable', page)
        self.assertIn('Restart collector service', page)


class PlotNameTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(server.plot_name('log_1432.csv', ['RPM', 'GPS_Speed_MPH']),
                         'plot_1432_RPM-GPS_Speed_MPH.png')
        self.assertEqual(server.plot_name('log_1432_2.csv', ['TPS (%)', 'MAP (PSIA) [raw]']),
                         'plot_1432_2_TPS-MAP_PSIA_raw.png')
        self.assertEqual(server.plot_name('log_boot3_812.csv', ['RPM']), 'plot_boot3_812_RPM.png')
        long_name = server.plot_name('log_1432.csv', [f'Channel number {i}' for i in range(30)])
        self.assertLess(len(long_name), 160)
        self.assertRegex(long_name, server.PLOT_RE)


if __name__ == '__main__':
    unittest.main()
