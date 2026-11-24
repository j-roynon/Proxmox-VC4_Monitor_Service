#!/bin/bash
# Proxmox VC4 Monitor - Installation Script for Linux

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Proxmox VC4 Monitor - Installation${NC}"
echo "======================================"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}"
   exit 1
fi

# Installation directory
INSTALL_DIR="/opt/proxmox-monitor"
LOG_DIR="/var/log/proxmox-monitor"
CONFIG_DIR="/etc/proxmox-monitor"

echo -e "${YELLOW}Creating directories...${NC}"
mkdir -p "$INSTALL_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$CONFIG_DIR"

echo -e "${YELLOW}Copying files...${NC}"
cp Proxmox_Manager.py "$INSTALL_DIR/"
cp config_loader.py "$INSTALL_DIR/"
cp config.yaml "$CONFIG_DIR/"
cp requirements.txt "$INSTALL_DIR/"
cp proxmox-monitor.service "$INSTALL_DIR/"

# Create environment file for credentials
if [ ! -f "$CONFIG_DIR/proxmox-monitor.env" ]; then
    echo -e "${YELLOW}Creating environment file for credentials...${NC}"
    cat > "$CONFIG_DIR/proxmox-monitor.env" << EOF
# Proxmox Monitor Environment Variables
# Edit this file to configure credentials and other settings

# Proxmox API credentials
PROXMOX_HOST=10.10.10.3
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=your_password_here

# CIP Device settings
CIP_HOST=10.10.10.10
CIP_IP_ID=0x13

# Monitor settings
MONITOR_INTERVAL=10
FAILURE_THRESHOLD=3
MIGRATION_TIMEOUT=60

# Logging
LOG_LEVEL=INFO
EOF
    chmod 600 "$CONFIG_DIR/proxmox-monitor.env"
    echo -e "${YELLOW}✓ Created $CONFIG_DIR/proxmox-monitor.env${NC}"
    echo -e "${RED}⚠  IMPORTANT: Edit this file and set PROXMOX_PASSWORD${NC}"
else
    echo -e "${YELLOW}Config file already exists, skipping creation${NC}"
fi

echo -e "${YELLOW}Installing Python dependencies in virtual environment...${NC}"
apt-get update
apt-get install -y python3-venv

cd "$INSTALL_DIR"

# Remove old venv if it exists (to ensure clean installation)
if [ -d venv ]; then
    echo -e "${YELLOW}Removing old virtual environment...${NC}"
    rm -rf venv
fi

# Create fresh virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

echo -e "${GREEN}✓ Python dependencies installed successfully${NC}"

echo -e "${YELLOW}Installing systemd service...${NC}"
cp "$INSTALL_DIR/proxmox-monitor.service" /etc/systemd/system/
systemctl daemon-reload

# Set permissions
chown -R root:root "$INSTALL_DIR"
chown -R root:root "$LOG_DIR"
chown -R root:root "$CONFIG_DIR"
chmod 755 "$INSTALL_DIR/Proxmox_Manager.py"
chmod 600 "$CONFIG_DIR/proxmox-monitor.env"

echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Edit credentials: sudo nano $CONFIG_DIR/proxmox-monitor.env"
echo "2. Start service: sudo systemctl start proxmox-monitor"
echo "3. Enable on boot: sudo systemctl enable proxmox-monitor"
echo "4. View logs: sudo journalctl -u proxmox-monitor -f"
echo ""
echo "Service status: sudo systemctl status proxmox-monitor"
echo "Service logs: sudo journalctl -u proxmox-monitor -n 100"
