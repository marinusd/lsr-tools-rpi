#!/usr/bin/env python3
"""Web interface for the ECU data hub (spec.md section 4, Tiers 2 and 3).

Lists the day directories and their session files, lets the operator download
a CSV, shows the collector's state with New session / Stop logging buttons,
and draws plots of chosen columns on the server.

    python server.py                 real paths, port 8080 (ecu-web.service uses --port 80)
    python server.py --mock          the ./mock_data of `collector.py --mock`

The server never touches the ECU serial port. It talks to the collector only
through the control socket.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime

from flask import Flask, abort, redirect, render_template_string, request, send_from_directory, url_for

import collector as collector_module

DAY_RE = re.compile(r'^(\d{4}-\d\d-\d\d|unsynced)$')
LOG_RE = re.compile(r'^log_\w+\.csv$')
NATIVE_RE = re.compile(r'^log_\w+\.log$')        # the same session in C-Com WP's format
PLOT_RE = re.compile(r'^plot_[\w.\-]+\.png$')
TIME_COLUMN = 'GPS_Timestamp'
DEFAULT_METRICS = ('RPM', 'GPS_Speed_MPH')
SERVICE = 'ecu-collector.service'
POWEROFF = ['/usr/bin/systemctl', 'poweroff']
SHUTDOWN_DELAY_S = 3            # time for the 'shutting down' page to reach the browser
MAX_PLOT_NAME = 120

app = Flask(__name__)
settings = {'DATA_DIR': None, 'CONTROL_SOCKET': None, 'MOCK': False}
_plot_lock = threading.Lock()          # matplotlib's pyplot is not thread-safe
_pyplot = None


# ---------------------------------------------------------------------------
# Collector control
# ---------------------------------------------------------------------------
def control(command):
    """Send one command to the collector. Returns its reply, or {'error': ...}."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(settings['CONTROL_SOCKET'])
            s.sendall((command + '\n').encode())
            return json.loads(s.makefile('r').readline())
    except (OSError, ValueError) as e:
        return {'error': f'collector not reachable: {e}'}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def day_dir(day):
    if not DAY_RE.match(day):
        abort(404)
    path = os.path.join(settings['DATA_DIR'], day)
    if not os.path.isdir(path):
        abort(404)
    return path


def log_path(day, name):
    if not LOG_RE.match(name):
        abort(404)
    path = os.path.join(day_dir(day), name)
    if not os.path.isfile(path):
        abort(404)
    return path


def list_days():
    try:
        names = [n for n in os.listdir(settings['DATA_DIR']) if DAY_RE.match(n)
                 and os.path.isdir(os.path.join(settings['DATA_DIR'], n))]
    except OSError:
        return []
    days = []
    for name in sorted(names, reverse=True):        # newest day first, unsynced on top
        folder = os.path.join(settings['DATA_DIR'], name)
        count = sum(1 for f in os.listdir(folder) if LOG_RE.match(f))
        days.append({'name': name, 'sessions': count})
    return days


def human_size(size):
    for unit in ('bytes', 'KB', 'MB'):
        if size < 1024 or unit == 'MB':
            return f'{size:.0f} {unit}' if unit == 'bytes' else f'{size:.1f} {unit}'
        size /= 1024


def list_sessions(day):
    folder = day_dir(day)
    names = os.listdir(folder)
    sessions = []
    for name in names:
        if not LOG_RE.match(name):
            continue
        info = os.stat(os.path.join(folder, name))
        stem = plot_stem(name)
        plots = sorted(p for p in names if PLOT_RE.match(p) and p.startswith(stem + '_'))
        native = name[:-len('.csv')] + '.log'
        sessions.append({'name': name, 'size': human_size(info.st_size), 'mtime': info.st_mtime,
                         'native': native if native in names else None,
                         'modified': datetime.fromtimestamp(info.st_mtime).strftime('%H:%M:%S'),
                         'plots': plots})
    sessions.sort(key=lambda s: s['mtime'], reverse=True)       # newest first
    return sessions


def read_header(path):
    with open(path, newline='') as f:
        return next(csv.reader(f), [])


# ---------------------------------------------------------------------------
# Plots (Tier 3)
# ---------------------------------------------------------------------------
def plot_stem(log_name):
    """log_1432_2.csv -> plot_1432_2"""
    return 'plot_' + log_name[len('log_'):-len('.csv')]


def plot_name(log_name, metrics):
    """plot_HHMM_[Metrics].png, next to the CSV it was drawn from."""
    parts = [re.sub(r'[^A-Za-z0-9]+', '_', m).strip('_') or 'x' for m in metrics]
    label = '-'.join(parts)
    if len(label) > MAX_PLOT_NAME:
        digest = hashlib.sha1('|'.join(metrics).encode()).hexdigest()[:8]
        label = f'{label[:MAX_PLOT_NAME]}-{digest}'
    return f'{plot_stem(log_name)}_{label}.png'


def read_columns(path, metrics):
    """Return (x values, x label, {metric: values}). Unreadable cells become None."""
    with open(path, newline='') as f:
        reader = csv.reader(f)
        header = next(reader, [])
        index = {m: header.index(m) for m in metrics}
        time_index = header.index(TIME_COLUMN) if TIME_COLUMN in header else None
        stamps, series = [], {m: [] for m in metrics}
        for row in reader:
            if len(row) != len(header):
                continue                        # a line still being written
            stamps.append(row[time_index] if time_index is not None else '')
            for m in metrics:
                try:
                    series[m].append(float(row[index[m]]))
                except ValueError:
                    series[m].append(None)
    try:
        times = [datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%fZ') for s in stamps]
        x = [(t - times[0]).total_seconds() for t in times]
        label = 'Seconds from start of session (GPS time)'
    except ValueError:                          # no GPS time on some rows
        x = list(range(len(stamps)))
        label = 'Row number (no GPS time in this file)'
    return x, label, series


def pyplot():
    """Import matplotlib on first use, headless (spec.md section 2)."""
    global _pyplot
    if _pyplot is None:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        _pyplot = plt
    return _pyplot


def render_plot(csv_path, metrics, out_path):
    x, x_label, series = read_columns(csv_path, metrics)
    with _plot_lock:
        plt = pyplot()
        fig = plt.figure(figsize=(10, 2.4 * len(metrics) + 0.8), dpi=100)
        try:
            axes = fig.subplots(len(metrics), 1, sharex=True, squeeze=False)
            for ax, metric in zip(axes[:, 0], metrics):
                values = [float('nan') if v is None else v for v in series[metric]]
                ax.plot(x, values, linewidth=1.0, color='#1f5fa8')
                ax.set_ylabel(metric)
                ax.grid(True, linewidth=0.4, color='#cccccc')
                ax.margins(x=0)
            axes[-1, 0].set_xlabel(x_label)
            fig.suptitle(f'{os.path.basename(os.path.dirname(csv_path))}  '
                         f'{os.path.basename(csv_path)}', fontsize=11)
            fig.tight_layout()
            temp = out_path + '.tmp'
            fig.savefig(temp, format='png')
            os.replace(temp, out_path)
        finally:
            plt.close(fig)                      # keep memory flat (OOM prevention)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{% endif %}
<title>{{ title }} - ECU data hub</title>
<style>
body{font-family:sans-serif;margin:1.2em;max-width:60em;color:#222}
h1{font-size:1.3em} h2{font-size:1.1em;margin-top:1.4em}
table{border-collapse:collapse} td,th{padding:.25em .9em .25em 0;text-align:left;vertical-align:top}
th{font-weight:normal;color:#666}
.box{border:1px solid #bbb;padding:.7em 1em;margin:.8em 0;background:#f7f7f7}
.state{font-weight:bold} .bad{color:#a40000} .note{color:#666;font-size:.9em}
button{font-size:1em;padding:.35em .9em;margin:.3em .5em .3em 0}
form.inline{display:inline} img{max-width:100%;border:1px solid #bbb}
label{display:block;padding:.15em 0}
</style></head><body>
<p><a href="{{ url_for('index') }}">ECU data hub</a>{% for text, link in crumbs %} / {% if link %}<a href="{{ link }}">{{ text }}</a>{% else %}{{ text }}{% endif %}{% endfor %}</p>
{% if message %}<p class="box {{ 'bad' if message_bad else '' }}">{{ message }}</p>{% endif %}
{{ body|safe }}
</body></html>"""

INDEX = """
<h1>Collector</h1>
<div class="box">
{% if st.error %}
  <p class="bad">{{ st.error }}</p>
{% else %}
  <table>
  <tr><th>State</th><td class="state">{{ st.state }}</td></tr>
  {% if st.file %}<tr><th>{{ 'Current file' if st.state == 'LOGGING' else 'Last file' }}</th><td>{{ file_label }}</td></tr>
  <tr><th>Rows written</th><td>{{ st.rows }}</td></tr>
  <tr><th>Failed polls</th><td>{{ st.failed_polls }}</td></tr>
  <tr><th>Last row</th><td>{{ st.last_row_time or '-' }}</td></tr>{% endif %}
  {% if st.state == 'WAITING' %}<tr><th>Failed attempts</th><td>{{ st.failed_attempts }}</td></tr>
  <tr><th>Next attempt in</th><td>{{ st.next_attempt_in_s }} s</td></tr>{% endif %}
  {% if st.last_error %}<tr><th>Last error</th><td class="bad">{{ st.last_error }}</td></tr>{% endif %}
  </table>
{% endif %}
<form class="inline" method="post" action="{{ url_for('command') }}">
  <button name="command" value="NEW_SESSION">New session</button>
  <button name="command" value="STOP">Stop logging</button>
</form>
<form class="inline" method="post" action="{{ url_for('restart') }}">
  <button>Restart collector service</button>
</form>
<form class="inline" method="get" action="{{ url_for('shutdown') }}">
  <button>Shut down Pi</button>
</form>
<p class="note">This page refreshes every 5 seconds.</p>
</div>
<h2>Logs by day</h2>
{% if days %}<table><tr><th>Day</th><th>Sessions</th></tr>
{% for d in days %}<tr><td><a href="{{ url_for('day', day=d.name) }}">{{ d.name }}</a></td><td>{{ d.sessions }}</td></tr>
{% endfor %}</table>{% else %}<p>No logs yet.</p>{% endif %}
"""

DAY = """
<h1>{{ day }}</h1>
{% if sessions %}<table><tr><th>Session file</th><th>Size</th><th>Last written</th><th>C-Com WP</th><th></th><th>Plots</th></tr>
{% for s in sessions %}<tr>
<td><a href="{{ url_for('download', day=day, name=s.name) }}">{{ s.name }}</a>{% if s.name == active %} <span class="note">(logging now)</span>{% endif %}</td>
<td>{{ s.size }}</td><td>{{ s.modified }}</td>
<td>{% if s.native %}<a href="{{ url_for('download', day=day, name=s.native) }}">{{ s.native }}</a>{% else %}-{% endif %}</td>
<td><a href="{{ url_for('plot_form', day=day, name=s.name) }}">New plot</a></td>
<td>{% for p in s.plots %}<a href="{{ url_for('plot_view', day=day, plot=p) }}">{{ p }}</a><br>{% endfor %}</td>
</tr>{% endfor %}</table>{% else %}<p>No session files in this directory.</p>{% endif %}
"""

PLOT_FORM = """
<h1>New plot from {{ name }}</h1>
<form method="post">
<p>Choose the columns to plot against time:</p>
{% for c in columns %}<label><input type="checkbox" name="metric" value="{{ c }}"{% if c in chosen %} checked{% endif %}> {{ c }}</label>
{% endfor %}
<p><button>Draw plot</button></p>
</form>
"""

SHUTDOWN_CONFIRM = """
<h1>Shut down the Raspberry Pi?</h1>
<div class="box">
<p>Logging stops and the current session's files are closed. Then the Pi shuts down.
This web page will stop answering.</p>
<p>{% if st.error %}Collector: <span class="bad">{{ st.error }}</span>
{% else %}Collector state now: <span class="state">{{ st.state }}</span>
{% if st.state == 'LOGGING' %} ({{ st.rows }} rows in the current session){% endif %}{% endif %}</p>
<p>To start the Pi again afterwards, disconnect its power and connect it again.</p>
<form class="inline" method="post"><button>Shut down now</button></form>
<form class="inline" method="get" action="{{ url_for('index') }}"><button>Cancel</button></form>
</div>
"""

SHUTDOWN_STARTED = """
<h1>Shutting down</h1>
<div class="box">
<p>Logging has been stopped and the Pi is shutting down.</p>
<p><b>Wait until the Pi's green light has stopped flashing</b> (about 20 seconds) before
you disconnect its power.</p>
<p class="note">If this page still opens after a minute, the shutdown did not happen:
see <code>journalctl -u ecu-web</code>.</p>
</div>
"""

PLOT_VIEW = """
<h1>{{ plot }}</h1>
<p><img src="{{ url_for('plot_image', day=day, plot=plot) }}" alt="{{ plot }}"></p>
<p><a href="{{ url_for('plot_image', day=day, plot=plot, download=1) }}">Download this plot</a></p>
"""


def page(title, body_template, crumbs=(), refresh=None, **values):
    body = render_template_string(body_template, **values)
    return render_template_string(PAGE, title=title, body=body, crumbs=crumbs, refresh=refresh,
                                  message=request.args.get('message'),
                                  message_bad=request.args.get('bad'))


@app.route('/')
def index():
    st = control('STATUS')
    file_label = ''
    if st.get('file'):
        file_label = os.path.join(os.path.basename(os.path.dirname(st['file'])),
                                  os.path.basename(st['file']))
    return page('Status', INDEX, refresh=None if request.args.get('message') else 5,
                st=st, file_label=file_label, days=list_days())


@app.route('/command', methods=['POST'])
def command():
    cmd = request.form.get('command')
    if cmd not in ('NEW_SESSION', 'STOP'):
        abort(400)
    reply = control(cmd)
    if reply.get('ok'):
        time.sleep(0.5)                         # let the collector act before the page reloads
        text = 'New session requested.' if cmd == 'NEW_SESSION' else 'Logging stopped.'
        return redirect(url_for('index', message=text))
    return redirect(url_for('index', message=reply.get('error', 'The collector refused.'), bad=1))


@app.route('/restart', methods=['POST'])
def restart():
    """Restart the collector service through the sudo rule in /etc/sudoers.d/ecu_control."""
    try:
        done = subprocess.run(['sudo', '-n', '/usr/bin/systemctl', 'restart', SERVICE],
                              capture_output=True, text=True, timeout=30)
        ok, detail = done.returncode == 0, done.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, detail = False, str(e)
    if ok:
        time.sleep(1)
        return redirect(url_for('index', message='Collector service restarted.'))
    return redirect(url_for('index', message=f'Could not restart the service: {detail}', bad=1))


def _poweroff_later():
    """Runs in a thread, so that the 'shutting down' page is sent first."""
    time.sleep(SHUTDOWN_DELAY_S)
    try:
        done = subprocess.run(['sudo', '-n', *POWEROFF], capture_output=True, text=True,
                              timeout=30)
        if done.returncode != 0:
            app.logger.error('shutdown failed: %s', done.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        app.logger.error('shutdown failed: %s', e)


@app.route('/shutdown', methods=['GET', 'POST'])
def shutdown():
    """Shut the Pi down through the sudo rule in /etc/sudoers.d/ecu_control."""
    if request.method == 'GET':
        return page('Shut down', SHUTDOWN_CONFIRM, crumbs=[('Shut down', None)],
                    st=control('STATUS'))
    if settings['MOCK']:
        return redirect(url_for('index', message='Mock mode: this computer is not shut down.'))
    # check the sudo rule now, so that a refusal can be shown on the page
    try:
        allowed = subprocess.run(['sudo', '-n', '-l', *POWEROFF], capture_output=True,
                                 text=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        allowed = False
    if not allowed:
        return redirect(url_for('index', bad=1, message='Shutdown is not permitted: the sudo '
                                'rule in /etc/sudoers.d/ecu_control is missing.'))
    control('STOP')         # close the session's files now; the collector also does it on stop
    threading.Thread(target=_poweroff_later, name='poweroff', daemon=True).start()
    return page('Shutting down', SHUTDOWN_STARTED)


@app.route('/day/<day>/')
def day(day):
    st = control('STATUS')
    active = None
    if st.get('state') == 'LOGGING' and st.get('file') and \
            os.path.basename(os.path.dirname(st['file'])) == day:
        active = os.path.basename(st['file'])
    return page(day, DAY, crumbs=[(day, None)], day=day, sessions=list_sessions(day),
                active=active)


@app.route('/day/<day>/<name>')
def download(day, name):
    if NATIVE_RE.match(name):
        if not os.path.isfile(os.path.join(day_dir(day), name)):
            abort(404)
        mimetype = 'application/octet-stream'
    else:
        log_path(day, name)
        mimetype = 'text/csv'
    return send_from_directory(day_dir(day), name, as_attachment=True,
                               mimetype=mimetype, max_age=0)


@app.route('/day/<day>/<name>/plot', methods=['GET', 'POST'])
def plot_form(day, name):
    path = log_path(day, name)
    columns = [c for c in read_header(path) if c != TIME_COLUMN]
    if request.method == 'POST':
        metrics = [m for m in request.form.getlist('metric') if m in columns]
        metrics = [m for m in columns if m in metrics]          # file order, no repeats
        if not metrics:
            return redirect(url_for('plot_form', day=day, name=name,
                                    message='Choose at least one column.', bad=1))
        out_name = plot_name(name, metrics)
        try:
            render_plot(path, metrics, os.path.join(day_dir(day), out_name))
        except OSError as e:
            return redirect(url_for('plot_form', day=day, name=name,
                                    message=f'Could not write the plot: {e}', bad=1))
        return redirect(url_for('plot_view', day=day, plot=out_name))
    chosen = [c for c in columns if c in DEFAULT_METRICS]
    return page('New plot', PLOT_FORM, crumbs=[(day, url_for('day', day=day)), (name, None)],
                name=name, columns=columns, chosen=chosen)


@app.route('/day/<day>/plots/<plot>')
def plot_view(day, plot):
    if not PLOT_RE.match(plot) or not os.path.isfile(os.path.join(day_dir(day), plot)):
        abort(404)
    return page(plot, PLOT_VIEW, crumbs=[(day, url_for('day', day=day)), (plot, None)],
                day=day, plot=plot)


@app.route('/day/<day>/plots/<plot>/image')
def plot_image(day, plot):
    if not PLOT_RE.match(plot):
        abort(404)
    return send_from_directory(day_dir(day), plot, mimetype='image/png', max_age=0,
                               as_attachment=bool(request.args.get('download')))


# ---------------------------------------------------------------------------
def configure(data_dir, control_socket, mock=False):
    settings['DATA_DIR'] = data_dir
    settings['CONTROL_SOCKET'] = control_socket
    settings['MOCK'] = mock


def main(argv=None):
    ap = argparse.ArgumentParser(description='ECU data hub web interface')
    ap.add_argument('--mock', action='store_true', help='use the paths of collector.py --mock')
    ap.add_argument('--config', default='/etc/ecu_hub/collector.conf')
    ap.add_argument('--data-dir', help='override DATA_DIR')
    ap.add_argument('--control-socket', help='override CONTROL_SOCKET')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8080)
    args = ap.parse_args(argv)
    # the same settings file and defaults as the collector
    _, cfg = collector_module.load_config(['--config', args.config] +
                                          (['--mock'] if args.mock else []))
    configure(args.data_dir or cfg['DATA_DIR'], args.control_socket or cfg['CONTROL_SOCKET'],
              mock=args.mock)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
