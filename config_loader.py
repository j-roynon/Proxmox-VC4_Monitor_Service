#!/usr/bin/env python3
"""
Configuration loader for Proxmox VM Monitor
Supports YAML config files, JSON config files, and environment variables
Supports per-VM CIP connections with unique host and IP_ID per VM
"""

import os
import json
import yaml
from typing import Dict, Any, Optional


class ConfigLoader:
    """Load and manage configuration from JSON, YAML, and environment variables."""
    
    def __init__(self, config_file: str = None):
        """
        Initialize config loader.
        
        Args:
            config_file: Path to config file (JSON or YAML)
                        If None, looks for config.json then config.yaml
        """
        self.config: Dict[str, Any] = {}
        self.config_file = config_file
        self.load()
    
    def load(self):
        """Load configuration from file (JSON/YAML) and merge with environment variables."""
        # Determine which config file to use
        if self.config_file:
            if os.path.exists(self.config_file):
                self._load_from_file(self.config_file)
            else:
                raise FileNotFoundError(f"Config file not found: {self.config_file}")
        else:
            # Try JSON first, then YAML
            if os.path.exists('config.json'):
                self._load_from_file('config.json')
            elif os.path.exists('config.yaml'):
                self._load_from_file('config.yaml')
        
        # Override with environment variables
        self._override_from_env()
    
    def _load_from_file(self, file_path: str):
        """Load configuration from JSON or YAML file."""
        try:
            if file_path.endswith('.json'):
                with open(file_path, 'r') as f:
                    self.config = json.load(f)
            elif file_path.endswith(('.yaml', '.yml')):
                with open(file_path, 'r') as f:
                    self.config = yaml.safe_load(f) or {}
            else:
                raise ValueError(f"Unsupported file format: {file_path}")
            
            print(f"✓ Loaded configuration from {file_path}")
        except Exception as e:
            print(f"Error loading config from {file_path}: {e}")
            raise
    
    def _override_from_env(self):
        """Override config values from environment variables."""
        env_overrides = {
            'PROXMOX_HOST': ('proxmox', 'host'),
            'PROXMOX_USER': ('proxmox', 'user'),
            'PROXMOX_PASSWORD': ('proxmox', 'password'),
            'PROXMOX_VERIFY_SSL': ('proxmox', 'verify_ssl'),
            'CIP_PULSE_SIGNAL': ('cip', 'pulse_signal'),
            'CIP_PULSE_TIMEOUT': ('cip', 'pulse_timeout'),
            'MONITOR_INTERVAL': ('monitor', 'interval'),
            'FAILURE_THRESHOLD': ('monitor', 'failure_threshold'),
            'MIGRATION_TIMEOUT': ('monitor', 'migration_timeout'),
            'LOG_LEVEL': ('logging', 'level'),
        }
        
        for env_var, (section, key) in env_overrides.items():
            value = os.getenv(env_var)
            if value:
                if section not in self.config:
                    self.config[section] = {}
                
                # Handle type conversions
                if env_var in ('PROXMOX_VERIFY_SSL',):
                    value = value.lower() in ('true', '1', 'yes')
                elif env_var in ('CIP_PULSE_SIGNAL', 'MONITOR_INTERVAL', 'FAILURE_THRESHOLD', 'MIGRATION_TIMEOUT'):
                    try:
                        value = int(value, 0)  # Support both decimal and hex (0x13)
                    except ValueError:
                        pass
                elif env_var in ('CIP_PULSE_TIMEOUT',):
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                
                self.config[section][key] = value
    
    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        return self.config.get(section, {}).get(key, default)
    
    def get_proxmox_config(self) -> Dict[str, Any]:
        """Get Proxmox configuration."""
        return {
            'host': self.get('proxmox', 'host', '10.10.10.3'),
            'user': self.get('proxmox', 'user', 'root@pam'),
            'password': self.get('proxmox', 'password', ''),
            'verify_ssl': self.get('proxmox', 'verify_ssl', False),
        }
    
    def get_cip_config(self) -> Dict[str, Any]:
        """Get base CIP configuration (shared settings)."""
        return {
            'pulse_signal': self.get('cip', 'pulse_signal', 1),
            'timeout': self.get('cip', 'pulse_timeout', 3.0),
        }
    
    def get_vm_cip_config(self, vm_id: str) -> Dict[str, Any]:
        """
        Get per-VM CIP configuration.
        
        Args:
            vm_id: Proxmox container ID
            
        Returns:
            Dict with cip_host, cip_ip_id for the VM
        """
        vms = self.get('monitor', 'vms', {})
        vm_config = vms.get(vm_id, {})
        
        # Parse IP_ID to handle both hex (0x13) and decimal formats
        ip_id = vm_config.get('cip_ip_id', 0x13)
        if isinstance(ip_id, str):
            ip_id = int(ip_id, 0)  # Supports both '0x13' and '19'
        
        return {
            'host': vm_config.get('cip_host', '10.10.10.10'),
            'ip_id': ip_id,
            'pulse_signal': self.get('cip', 'pulse_signal', 1),
            'timeout': self.get('cip', 'pulse_timeout', 3.0),
        }
    
    def get_monitor_config(self) -> Dict[str, Any]:
        """Get monitor configuration."""
        vms = self.get('monitor', 'vms', {})
        vm_ids = list(vms.keys()) if vms else ['100', '101']
        
        return {
            'interval': self.get('monitor', 'interval', 10),
            'failure_threshold': self.get('monitor', 'failure_threshold', 3),
            'migration_timeout': self.get('monitor', 'migration_timeout', 60),
            'vm_ids': vm_ids,
        }
    
    def get_logging_config(self) -> Dict[str, Any]:
        """Get logging configuration."""
        return {
            'level': self.get('logging', 'level', 'INFO'),
            'file': self.get('logging', 'file', 'proxmox_monitor.log'),
            'format': self.get('logging', 'format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s'),
        }
    
    def save_to_file(self, file_path: str):
        """Save current configuration to a file (JSON or YAML)."""
        try:
            if file_path.endswith('.json'):
                with open(file_path, 'w') as f:
                    json.dump(self.config, f, indent=2)
            elif file_path.endswith(('.yaml', '.yml')):
                with open(file_path, 'w') as f:
                    yaml.dump(self.config, f, default_flow_style=False)
            else:
                raise ValueError(f"Unsupported file format: {file_path}")
            
            print(f"✓ Saved configuration to {file_path}")
        except Exception as e:
            print(f"Error saving config to {file_path}: {e}")
            raise

