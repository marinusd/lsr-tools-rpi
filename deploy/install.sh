#!/bin/sh
# Install the ECU data hub on a Raspberry Pi (Raspberry Pi OS Lite, Debian 12).
# Run from the repository:   sudo sh deploy/install.sh
# Safe to run again after updating the code: it keeps /etc/ecu_hub/collector.conf.
set -eu

[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/ecu_hub

# Python libraries (from Debian packages; needs a network connection the first time)
if ! python3 -c "import serial, flask, matplotlib" 2>/dev/null; then
    apt-get install -y python3-serial python3-flask python3-matplotlib
fi

# Service accounts. Both are in group ecu-hub, which owns the logs and the control socket.
getent group ecu-hub >/dev/null || groupadd --system ecu-hub
id ecu-collector >/dev/null 2>&1 || useradd --system --gid ecu-hub --groups dialout \
    --no-create-home --shell /usr/sbin/nologin ecu-collector
id ecu-web >/dev/null 2>&1 || useradd --system --gid ecu-hub \
    --no-create-home --shell /usr/sbin/nologin ecu-web

# Program files
install -d -m 755 "$DEST" /etc/ecu_hub
for f in collector.py server.py parse_ecu_log.py mock_serial.py ecuctl.py CHANNEL_TEMPLATE.txt spec.md; do
    install -m 644 "$SRC/$f" "$DEST/$f"
done
if [ ! -e /etc/ecu_hub/collector.conf ]; then
    install -m 644 "$SRC/deploy/collector.conf.example" /etc/ecu_hub/collector.conf
    echo "Created /etc/ecu_hub/collector.conf - set ECU_PORT and GPS_PORT in it."
fi

# Services and the sudo rule for the web page's restart button
install -m 644 "$SRC/deploy/ecu-collector.service" "$SRC/deploy/ecu-web.service" /etc/systemd/system/
visudo -cf "$SRC/deploy/ecu_control"
install -m 440 -o root -g root "$SRC/deploy/ecu_control" /etc/sudoers.d/ecu_control

systemctl daemon-reload
systemctl enable ecu-collector.service ecu-web.service
systemctl restart ecu-collector.service ecu-web.service

echo
echo "Installed. Web page: http://$(hostname):8080/"
echo "Status:   systemctl status ecu-collector ecu-web"
echo "Journal:  journalctl -u ecu-collector -f"
echo "Control:  sudo -u ecu-web python3 $DEST/ecuctl.py STATUS"
