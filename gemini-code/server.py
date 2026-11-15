#!/usr/bin/env python3
import os
import sys
import mimetypes
from datetime import datetime

# Prevent Matplotlib from attempting to bind to an X11 GUI server framework
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from flask import Flask, render_template_string, request, send_from_directory, abort, redirect, url_for

app = Flask(__name__)
DATA_DIR = "/var/log/ecu_hub/data"

# Ensure data base directory path tracking structure exists safely
os.makedirs(DATA_DIR, exist_ok=True)

# Minimal, lightning-fast native HTML template styled cleanly for mobile screens
BASE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ECU Data Hub</title>
    <style>
        body { font-family: -apple-system, sans-serif; margin: 20px; background: #f5f5f7; color: #1d1d1f; }
        h1, h2 { color: #333; }
        a { color: #0066cc; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .container { max-width: 900px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; }
        .btn { background: #0071e3; color: white; padding: 6px 12px; border-radius: 4px; border: none; cursor: pointer; font-size: 14px; }
        .btn:hover { background: #0077ed; }
        .btn-danger { background: #df2c2c; }
        .btn-danger:hover { background: #e33e3e; }
        .checkbox-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin: 15px 0; }
        .control-panel { display: flex; gap: 10px; margin-top: 20px; background: #fafafa; padding: 15px; border-radius: 6px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚗 ECU Data Hub Portal</h1>
        <hr>
        {% block content %}{% endblock %}
    </div>
</body>
</html>
"""

INDEX_TEMPLATE = """
{% extends "base" %}
{% block content %}
<h2>📁 Data Storage Directories</h2>
<p><a href="{{ url_for('system_controls') }}">⚙️ System Service Controls</a></p>
<ul>
    {% if current_rel_path %}
    <li><strong><a href="{{ url_for('browse_directory', rel_path=parent_path) }}">⬅️ Up One Level</a></strong></li>
    {% endif %}
    
    {% for item in sorted_items %}
    <li>
        {% if item.is_dir %}
        <a href="{{ url_for('browse_directory', rel_path=item.rel_path) }}">📁 {{ item.name }}/</a>
        {% else %}
        <div>
            📄 <a href="{{ url_for('download_file', rel_path=item.rel_path) }}">{{ item.name }}</a>
            {% if item.name.endswith('.csv') %}
            | <a href="{{ url_for('visualize_file', rel_path=item.rel_path) }}" style="color: #2c974b; font-weight: bold;">📊 Plot Metrics</a>
            {% endif %}
        </div>
        <span style="font-size: 12px; color: #888;">{{ item.size }}</span>
        {% endif %}
    </li>
    {% endfor %}
</ul>
{% endblock %}
"""

PLOT_TEMPLATE = """
{% extends "base" %}
{% block content %}
<h2>📊 Visualization Panel: {{ filename }}</h2>
<p><a href="{{ url_for('browse_directory', rel_path=parent_dir_path) }}">📁 Back to Directory Index</a></p>

<form method="POST">
    <h3>Select Telemetry Traces to Graph:</h3>
    <div class="checkbox-grid">
        {% for header in headers %}
        <label>
            <input type="checkbox" name="metrics" value="{{ header }}" {% if header in selected_metrics %}checked{% endif %}>
            {{ header }}
        </label>
        {% endfor %}
    </div>
    <button type="submit" class="btn">⚡ Generate Static Plot Render</button>
</form>

{% if generated_plot_url %}
<hr>
<h3>Rendered Graph Visualization Target:</h3>
<img src="{{ generated_plot_url }}" alt="ECU Metrics Plot" style="max-width: 100%; height: auto; border: 1px solid #ccc; border-radius: 4px; margin-top: 15px;">
{% endif %}
{% endblock %}
"""

CONTROLS_TEMPLATE = """
{% extends "base" %}
{% block content %}
<h2>⚙️ System Process Control Panel</h2>
<p><a href="{{ url_for('browse_directory') }}">📁 Return to Directory Index</a></p>

<div style="margin-top: 20px;">
    <h3>Ingestion Collector Engine (collector.py)</h3>
    <p>Status Tracking: Active / Enabled at Startup</p>
    <form action="{{ url_for('trigger_service_action', service='ecu-collector', action='restart') }}" method="POST" style="display:inline;">
        <button type="submit" class="btn">🔄 Restart Data Collector</button>
    </form>
    <form action="{{ url_for('trigger_service_action', service='ecu-collector', action='stop') }}" method="POST" style="display:inline;">
        <button type="submit" class="btn btn-danger">🛑 Stop Collector</button>
    </form>
</div>

<div style="margin-top: 40px;">
    <h3>Web Application Portal Interface (server.py)</h3>
    <form action="{{ url_for('trigger_service_action', service='ecu-web', action='restart') }}" method="POST" style="display:inline;">
        <button type="submit" class="btn">🔄 Restart Web Server</button>
    </form>
</div>
{% endblock %}
"""

@app.route('/base')
def base_layout():
    return BASE_TEMPLATE

def render_custom_template(template_str, **context):
    """Combines baseline templates dynamically without needing file IO checks"""
    full_template = BASE_TEMPLATE.replace("{% block content %}{% endblock %}", template_str)
    return render_template_string(full_template, **context)

@app.route('/')
@app.route('/browse/')
@app.route('/browse/<path:rel_path>')
def browse_directory(rel_path=""):
    target_abs_path = os.path.abspath(os.path.join(DATA_DIR, rel_path))
    
    if not target_abs_path.startswith(os.path.abspath(DATA_DIR)):
        abort(403) # Prevent directory traversal vulnerability hacks
    if not os.path.exists(target_abs_path):
        abort(404)
        
    items = []
    for f in os.listdir(target_abs_path):
        abs_item_path = os.path.join(target_abs_path, f)
        item_rel_path = os.path.relpath(abs_item_path, DATA_DIR)
        is_dir = os.path.isdir(abs_item_path)
        
        size_str = ""
        if not is_dir:
            size_bytes = os.path.getsize(abs_item_path)
            size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes > 1024 else f"{size_bytes} Bytes"
            
        items.append({
            'name': f,
            'rel_path': item_rel_path,
            'is_dir': is_dir,
            'size': size_str
        })
        
    # Sort order: directories first, then filenames sequentially
    sorted_items = sorted(items, key=lambda x: (not x['is_dir'], x['name']))
    parent_path = os.path.dirname(rel_path)
    
    return render_custom_template(
        INDEX_TEMPLATE, 
        sorted_items=sorted_items, 
        current_rel_path=rel_path, 
        parent_path=parent_path
    )

@app.route('/download/<path:rel_path>')
def download_file(rel_path):
    target_abs_path = os.path.abspath(os.path.join(DATA_DIR, rel_path))
    if not target_abs_path.startswith(os.path.abspath(DATA_DIR)):
        abort(403)
        
    return send_from_directory(os.path.dirname(target_abs_path), os.path.basename(target_abs_path), as_attachment=True)

@app.route('/visualize/<path:rel_path>', methods=['GET', 'POST'])
def visualize_file(rel_path):
    csv_abs_path = os.path.abspath(os.path.join(DATA_DIR, rel_path))
    if not csv_abs_path.startswith(os.path.abspath(DATA_DIR)) or not os.path.exists(csv_abs_path):
        abort(403)
        
    # Extract headers straight out of the active telemetry file target
    with open(csv_abs_path, 'r', encoding='utf-8') as f:
        headers = f.readline().strip().split(',')
        
    selected_metrics = []
    generated_plot_url = None
    parent_dir_path = os.path.dirname(rel_path)
    filename_base = os.path.splitext(os.path.basename(csv_abs_path))[0]
    
    if request.method == 'POST':
        selected_metrics = request.form.getlist('metrics')
        if selected_metrics:
            # Parse lines and values dynamically to ensure flat memory scale array structures
            data_columns = {m: [] for m in selected_metrics}
            
            with open(csv_abs_path, 'r', encoding='utf-8') as f:
                file_headers = f.readline().strip().split(',')
                indices = {m: file_headers.index(m) for m in selected_metrics if m in file_headers}
                
                for line in f:
                    parts = line.strip().split(',')
                    if len(parts) == len(file_headers):
                        for m, idx in indices.items():
                            try:
                                data_columns[m].append(float(parts[idx]))
                            except ValueError:
                                pass # Discard fragmented or broken strings cleanly
                                
            if any(data_columns.values()):
                # Strict Memory Management Boundaries for the 1GB Raspberry Pi 3b
                fig, ax = plt.subplots(figsize=(10, 5))
                try:
                    for metric, values in data_columns.items():
                        if values:
                            ax.plot(values, label=metric, linewidth=1.5)
                            
                    ax.set_title(f"ECU Trace Metrics Visualization Summary ({filename_base})")
                    ax.set_xlabel("Data Loop Chronological Index Records (10Hz)")
                    ax.grid(True, linestyle='--', alpha=0.6)
                    ax.legend(loc='upper right')
                    
                    # Store graphic rendering alongside raw log files inside target directories
                    metrics_slug = "_".join([m.replace('/', '_').replace('%', '') for m in selected_metrics])[:30]
                    plot_filename = f"plot_{filename_base}_{metrics_slug}.png"
                    plot_abs_path = os.path.join(os.path.dirname(csv_abs_path), plot_filename)
                    
                    fig.savefig(plot_abs_path, format='png', dpi=110, bbox_inches='tight')
                    generated_plot_url = url_for('browse_directory', rel_path=os.path.relpath(plot_abs_path, DATA_DIR))
                finally:
                    # Enforce critical cleanup safety procedures to bypass OOM errors
                    plt.close(fig)

    return render_custom_template(
        PLOT_TEMPLATE,
        filename=os.path.basename(csv_abs_path),
        headers=headers,
        selected_metrics=selected_metrics,
        generated_plot_url=generated_plot_url,
        parent_dir_path=parent_dir_path
    )

@app.route('/controls')
def system_controls():
    return render_custom_template(CONTROLS_TEMPLATE)

@app.route('/controls/action/<service>/<action>', methods=['POST'])
def trigger_service_action(service, action):
    if service not in ['ecu-collector', 'ecu-web'] or action not in ['start', 'stop', 'restart']:
        abort(400)
        
    # Execute structural systemctl daemon adjustments securely using restricted sudo elevation rules
    import subprocess
    cmd = ["sudo", "/bin/systemctl", action, service]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Administrative systemctl execution failed: {e.stderr.decode()}", file=sys.stderr)
        
    return redirect(url_for('system_controls'))

if __name__ == '__main__':
    # Listen broad interfaces on port 8080 by default target options
    app.run(host='0.0.0.0', port=8080, debug=False)
