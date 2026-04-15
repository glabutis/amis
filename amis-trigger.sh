#!/bin/bash
# amis-trigger.sh — called by udev when a block device is added.
# Usage: amis-trigger.sh <kernel_device_name>   e.g. sdb1
#
# udev runs this as root in a minimal environment, so we use absolute paths.

DEVICE="/dev/$1"
CONFIG="/etc/amis/config.yaml"
LOG="/var/log/amis/ingest.log"
PYTHON="/usr/bin/python3"
SCRIPT="/usr/local/bin/amis-ingest"

# Give the kernel a moment to finish device initialization
sleep 2

# Log the trigger
echo "$(date '+%Y-%m-%d %H:%M:%S') [TRIGGER] Device added: $DEVICE" >> "$LOG"

# Sanity check — device must exist and be a block device
if [ ! -b "$DEVICE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [TRIGGER] $DEVICE is not a block device, skipping" >> "$LOG"
    exit 0
fi

# Run ingest as root (already root from udev) in background so udev isn't blocked
"$PYTHON" "$SCRIPT" "$DEVICE" --config "$CONFIG" &
