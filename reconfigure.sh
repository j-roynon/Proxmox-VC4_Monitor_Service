#!/bin/bash
# Proxmox VC4 Monitor - Reconfiguration Helper
# Edit config and restart service

set -e

# Colors for output
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}Proxmox VC4 Monitor - Reconfiguration${NC}"
echo "========================================"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${YELLOW}This script must be run with sudo${NC}"
   exit 1
fi

CONFIG_FILE="/etc/proxmox-monitor/config.yaml"

# Check if config exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${YELLOW}ERROR: Configuration file not found at ${CONFIG_FILE}${NC}"
    echo "Run 'sudo bash install.sh' first to install the monitor"
    exit 1
fi

echo ""
echo "Opening configuration file for editing..."
echo "File: $CONFIG_FILE"
echo ""

# Open config in nano editor
nano "$CONFIG_FILE"

echo ""
echo -e "${YELLOW}Restarting proxmox-monitor service to apply changes...${NC}"

# Restart the service
systemctl restart proxmox-monitor

echo -e "${GREEN}âœ“ Service restarted${NC}"
echo ""
echo "To view logs and verify the changes:"
echo -e "  ${YELLOW}sudo journalctl -u proxmox-monitor -f${NC}"
echo ""
