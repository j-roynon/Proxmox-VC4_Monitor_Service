#!/bin/bash
# Proxmox VC4 Monitor - Uninstallation Script
# Removes all installed files, configuration, and cleans up systemd service

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Proxmox VC4 Monitor - Uninstallation${NC}"
echo "======================================"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}"
   exit 1
fi

INSTALL_DIR="/opt/proxmox-monitor"
LOG_DIR="/var/log/proxmox-monitor"
CONFIG_DIR="/etc/proxmox-monitor"

# Stop the service
echo -e "${YELLOW}Stopping proxmox-monitor service...${NC}"
if systemctl is-active --quiet proxmox-monitor; then
    systemctl stop proxmox-monitor
    echo -e "${GREEN}âœ“ Service stopped${NC}"
else
    echo -e "${YELLOW}  Service not running${NC}"
fi

# Disable service from starting on boot
echo -e "${YELLOW}Disabling service from boot...${NC}"
systemctl disable proxmox-monitor 2>/dev/null || true
echo -e "${GREEN}âœ“ Service disabled${NC}"

# Remove systemd service file
echo -e "${YELLOW}Removing systemd service file...${NC}"
rm -f /etc/systemd/system/proxmox-monitor.service
systemctl daemon-reload
echo -e "${GREEN}âœ“ Service file removed${NC}"

# Remove installation directory and virtual environment
echo -e "${YELLOW}Removing installation directory (${INSTALL_DIR})...${NC}"
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo -e "${GREEN}âœ“ Installation directory removed${NC}"
else
    echo -e "${YELLOW}  Installation directory not found${NC}"
fi

# Remove log directory
echo -e "${YELLOW}Removing log directory (${LOG_DIR})...${NC}"
if [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
    echo -e "${GREEN}âœ“ Log directory removed${NC}"
else
    echo -e "${YELLOW}  Log directory not found${NC}"
fi

# Prompt for config removal
echo -e "${YELLOW}Remove configuration directory (${CONFIG_DIR})?${NC}"
read -p "This will delete all configuration files. Type 'yes' to confirm: " -r
if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    if [ -d "$CONFIG_DIR" ]; then
        rm -rf "$CONFIG_DIR"
        echo -e "${GREEN}âœ“ Configuration directory removed${NC}"
    fi
else
    echo -e "${YELLOW}  Configuration directory preserved at ${CONFIG_DIR}${NC}"
    echo -e "${YELLOW}  To remove manually: sudo rm -rf ${CONFIG_DIR}${NC}"
fi

echo ""
echo -e "${GREEN}Uninstallation complete!${NC}"
echo ""
echo "âœ“ Service removed"
echo "âœ“ Application files removed"
echo "âœ“ Virtual environment removed"
echo "âœ“ Logs removed"
if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    echo "âœ“ Configuration removed"
else
    echo "âœ“ Configuration preserved (can be reused or deleted manually)"
fi
echo ""
echo -e "${YELLOW}To reinstall: sudo bash install.sh${NC}"
