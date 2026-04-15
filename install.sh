#!/bin/bash
# install.sh — One-shot AMIS setup for Ubuntu
# Run as root: sudo bash install.sh

set -e

AMIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEST="/etc/amis/config.yaml"
RULES_DEST="/etc/udev/rules.d/99-amis.rules"
TRIGGER_DEST="/usr/local/bin/amis-trigger.sh"
INGEST_DEST="/usr/local/bin/amis-ingest"
WEB_DEST="/usr/local/bin/amis-web"
VENV="/opt/amis/venv"
PYTHON="$VENV/bin/python3"

# ---------------------------------------------------------------------------
echo "[1/7] Installing system dependencies..."
# ---------------------------------------------------------------------------
apt-get update -qq
apt-get install -y \
    ffmpeg \
    python3 \
    python3-venv \
    python3-full \
    cifs-utils \
    eject \
    util-linux

# ---------------------------------------------------------------------------
echo "[2/7] Creating Python virtual environment at $VENV..."
# ---------------------------------------------------------------------------
mkdir -p /opt/amis
python3 -m venv "$VENV"
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -r "$AMIS_DIR/requirements.txt"
echo "  venv ready at $VENV"

# ---------------------------------------------------------------------------
echo "[3/7] Installing AMIS files..."
# ---------------------------------------------------------------------------

# Config
mkdir -p /etc/amis
if [ ! -f "$CONFIG_DEST" ]; then
    cp "$AMIS_DIR/config.yaml" "$CONFIG_DEST"
    echo "  Config installed to $CONFIG_DEST"
    echo "  *** Edit $CONFIG_DEST with your SMB credentials before use ***"
else
    echo "  Config already exists at $CONFIG_DEST — skipping (won't overwrite)"
fi

# Ingest script
cp "$AMIS_DIR/ingest.py" "$INGEST_DEST"
chmod +x "$INGEST_DEST"
echo "  Ingest script installed to $INGEST_DEST"

# Trigger script — bake in the venv python path
sed "s|/usr/bin/python3|$PYTHON|g" "$AMIS_DIR/amis-trigger.sh" > "$TRIGGER_DEST"
chmod +x "$TRIGGER_DEST"
echo "  Trigger script installed to $TRIGGER_DEST"

# Web UI
cp "$AMIS_DIR/web.py" "$WEB_DEST"
chmod +x "$WEB_DEST"
echo "  Web UI installed to $WEB_DEST"

# ---------------------------------------------------------------------------
echo "[4/7] Installing udev rules..."
# ---------------------------------------------------------------------------
cp "$AMIS_DIR/99-amis.rules" "$RULES_DEST"
udevadm control --reload-rules
udevadm trigger
echo "  udev rules installed and reloaded"

# ---------------------------------------------------------------------------
echo "[5/7] Creating runtime directories..."
# ---------------------------------------------------------------------------
mkdir -p /mnt/sdcard
mkdir -p /mnt/smb
mkdir -p /var/log/amis
mkdir -p /var/lib/amis
echo "  Directories created"

# ---------------------------------------------------------------------------
echo "[6/7] Installing and enabling systemd web service..."
# ---------------------------------------------------------------------------

# Write service file with the correct venv python path
cat > /etc/systemd/system/amis-web.service <<EOF
[Unit]
Description=AMIS Web Dashboard
After=network.target

[Service]
Type=simple
ExecStart=$PYTHON $WEB_DEST --config $CONFIG_DEST
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable amis-web
systemctl restart amis-web
echo "  amis-web.service enabled and started"

# ---------------------------------------------------------------------------
echo "[7/7] Done!"
# ---------------------------------------------------------------------------
echo ""
echo "  AMIS installed. Next steps:"
echo "  1. Edit $CONFIG_DEST — set your SMB host, username, password"
echo "  2. Open the dashboard: http://$(hostname -I | awk '{print $1}'):8080"
echo "  3. Plug in an SD card to test"
echo "  4. Monitor logs: tail -f /var/log/amis/ingest.log"
echo ""
echo "  To test ingest manually:"
echo "    sudo $PYTHON $INGEST_DEST /dev/sdb1 --config $CONFIG_DEST"
echo ""
echo "  Web service status:"
echo "    systemctl status amis-web"
