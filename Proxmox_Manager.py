#!/usr/bin/env python3
"""
Proxmox VM Monitor - Monitors Crestron VC4 VMs on Proxmox HA cluster.
- Checks container health status
- Sends CIP pulses and awaits responses
- Migrates VMs on cluster if health checks fail 3 times
- Runs monitoring loop at 10-second intervals
"""

import asyncio
import json
import logging
import time
import socket
from datetime import datetime
from typing import Dict, Optional, List, Tuple
from enum import Enum
from proxmoxer import ProxmoxAPI
import cipclient


# ============================================================================
# Configuration - Loaded from config.yaml or environment variables
# (See ConfigLoader class below for defaults if not specified)
# ============================================================================

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes & Enums
# ============================================================================

class VMStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class HealthCheckResult(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class MigrationResult(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VMHealthMonitor:
    """Tracks health status of a single VM."""
    
    def __init__(self, vm_id: str, failure_threshold: int = 3):
        self.vm_id = vm_id
        self.failure_threshold = failure_threshold
        self.consecutive_failures = 0
        self.last_check_time: Optional[datetime] = None
        self.last_healthy: Optional[datetime] = None
        self.migration_triggered = False
        self.migration_uncertain = False
        self.recovery_deadline: Optional[float] = None
        self.recovery_attempted = False
    
    def record_success(self):
        """Record successful health check."""
        self.consecutive_failures = 0
        self.last_healthy = datetime.now()
        self.migration_triggered = False
        self.recovery_deadline = None
        self.recovery_attempted = False
    
    def record_failure(self):
        """Record failed health check."""
        self.consecutive_failures += 1
    
    def should_migrate(self) -> bool:
        """Check if migration threshold has been reached."""
        return self.consecutive_failures >= self.failure_threshold and not self.migration_triggered
    
    def mark_migration_triggered(self):
        """Mark that migration has been initiated."""
        self.migration_triggered = True

    def record_migration_failure(self):
        """Reset the failure window so a failed migration can be retried."""
        self.consecutive_failures = 0
        self.migration_triggered = False
        self.recovery_attempted = False

    def start_recovery_countdown(self, seconds: int):
        """Allow time for a stopped VM to boot before counting health failures."""
        self.consecutive_failures = 0
        self.migration_triggered = False
        self.recovery_deadline = time.monotonic() + seconds
        self.recovery_attempted = True

    def recovery_countdown_remaining(self) -> Optional[float]:
        """Return the remaining boot grace period, clearing it once it expires."""
        if self.recovery_deadline is None:
            return None

        remaining = self.recovery_deadline - time.monotonic()
        if remaining <= 0:
            self.recovery_deadline = None
            return None
        return remaining
    
    def get_status(self) -> Dict:
        """Get current monitor status."""
        return {
            'vm_id': self.vm_id,
            'consecutive_failures': self.consecutive_failures,
            'last_check': self.last_check_time.isoformat() if self.last_check_time else None,
            'last_healthy': self.last_healthy.isoformat() if self.last_healthy else None,
            'migration_triggered': self.migration_triggered
        }


# ============================================================================
# Proxmox Functions
# ============================================================================

class ProxmoxManager:
    """Handles Proxmox API interactions."""
    
    def __init__(self, host: str, user: str, password: str, verify_ssl: bool = False, migration_timeout: int = 60):
        self.host = host
        self.user = user
        self.password = password
        self.verify_ssl = verify_ssl
        self.migration_timeout = migration_timeout
        self.proxmox: Optional[ProxmoxAPI] = None
    
    def connect(self) -> bool:
        """Establish connection to Proxmox."""
        try:
            self.proxmox = ProxmoxAPI(
                self.host,
                user=self.user,
                password=self.password,
                verify_ssl=self.verify_ssl
            )
            logger.info(f"Successfully connected to Proxmox at {self.host}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Proxmox: {e}")
            return False
    
    def get_vm_status(self, vm_id: str) -> VMStatus:
        """
        Get the current status of a VM/container (supports both LXC and QEMU).
        
        Args:
            vm_id: Container/VM ID in Proxmox
            
        Returns:
            VMStatus enum value
        """
        try:
            if not self.proxmox:
                logger.error("Proxmox not connected")
                return VMStatus.UNKNOWN
            
            # Try to get container/VM status from all nodes in cluster
            for node in self.proxmox.nodes.get():
                node_name = node['node']
                
                # Try LXC container first
                try:
                    status_response = self.proxmox.nodes(node_name).lxc(vm_id).status.current.get()
                    status_str = status_response.get('status', 'unknown').lower()
                    
                    if status_str == 'running':
                        logger.debug(f"VM {vm_id} on node {node_name}: RUNNING (LXC)")
                        return VMStatus.RUNNING
                    elif status_str == 'stopped':
                        logger.debug(f"VM {vm_id} on node {node_name}: STOPPED (LXC)")
                        return VMStatus.STOPPED
                    elif status_str == 'paused':
                        logger.debug(f"VM {vm_id} on node {node_name}: PAUSED (LXC)")
                        return VMStatus.PAUSED
                except Exception:
                    # Not an LXC container on this node, try QEMU VM
                    pass
                
                # Try QEMU VM
                try:
                    status_response = self.proxmox.nodes(node_name).qemu(vm_id).status.current.get()
                    status_str = status_response.get('status', 'unknown').lower()
                    
                    if status_str == 'running':
                        logger.debug(f"VM {vm_id} on node {node_name}: RUNNING (QEMU)")
                        return VMStatus.RUNNING
                    elif status_str == 'stopped':
                        logger.debug(f"VM {vm_id} on node {node_name}: STOPPED (QEMU)")
                        return VMStatus.STOPPED
                    elif status_str == 'paused':
                        logger.debug(f"VM {vm_id} on node {node_name}: PAUSED (QEMU)")
                        return VMStatus.PAUSED
                except Exception:
                    # VM not on this node, try next
                    continue
            
            logger.warning(f"VM {vm_id} not found on any node")
            return VMStatus.UNKNOWN
            
        except Exception as e:
            logger.error(f"Error checking VM {vm_id} status: {e}")
            return VMStatus.UNKNOWN

    def start_vm(self, vm_id: str) -> bool:
        """Start a stopped LXC container or QEMU VM."""
        if not self.proxmox:
            logger.error("Proxmox not connected; cannot start VM %s", vm_id)
            return False

        try:
            nodes = self.proxmox.nodes.get()
        except Exception as e:
            logger.error(f"Could not list nodes to start VM {vm_id}: {e}")
            return False

        for node in nodes:
            node_name = node['node']
            for vm_type in ('lxc', 'qemu'):
                try:
                    guest = (
                        self.proxmox.nodes(node_name).lxc(vm_id)
                        if vm_type == 'lxc'
                        else self.proxmox.nodes(node_name).qemu(vm_id)
                    )
                    status = guest.status.current.get().get('status', 'unknown').lower()
                except Exception:
                    continue

                if status == 'running':
                    logger.info(f"VM {vm_id} is already running on {node_name}")
                    return True
                if status != 'stopped':
                    logger.error(f"Cannot start VM {vm_id} on {node_name}: unexpected status {status}")
                    return False

                try:
                    task_id = guest.status.start.post()
                except Exception as e:
                    logger.error(f"Failed to start VM {vm_id} on {node_name}: {e}")
                    return False

                logger.info(f"Start command sent for VM {vm_id} on {node_name}, task ID: {task_id}")
                return True

        logger.error(f"Could not find VM {vm_id} on any node to start")
        return False
    
    def migrate_vm(self, vm_id: str, target_node: Optional[str] = None) -> MigrationResult:
        """
        Shut down, migrate offline, and start an LXC container or QEMU VM.
        
        Args:
            vm_id: Container/VM ID to migrate
            target_node: Target node name (if None, picks another available node)
            
        Returns:
            The outcome, including an unknown result if an operation may still be running.
        """
        operation_in_progress = False
        try:
            if not self.proxmox:
                logger.error("Proxmox not connected")
                return MigrationResult.FAILED
            
            # Find which node has the VM and determine its type (LXC or QEMU)
            source_node = None
            vm_type = None  # 'lxc' or 'qemu'
            
            for node in self.proxmox.nodes.get():
                node_name = node['node']
                
                # Try LXC first
                try:
                    guest = self.proxmox.nodes(node_name).lxc(vm_id)
                    status = guest.status.current.get().get('status', 'unknown').lower()
                    source_node = node_name
                    vm_type = 'lxc'
                    break
                except Exception:
                    pass
                
                # Try QEMU
                try:
                    guest = self.proxmox.nodes(node_name).qemu(vm_id)
                    status = guest.status.current.get().get('status', 'unknown').lower()
                    source_node = node_name
                    vm_type = 'qemu'
                    break
                except Exception:
                    pass
            
            if not source_node:
                logger.error(f"Could not find VM {vm_id} on any node")
                return MigrationResult.FAILED
            
            # If no target node specified, pick the first available node that isn't the source
            if not target_node:
                for node in self.proxmox.nodes.get():
                    if node['node'] != source_node:
                        target_node = node['node']
                        break
            
            if not target_node:
                logger.error(f"No available target nodes for migration (only have {source_node})")
                return MigrationResult.FAILED

            if target_node == source_node:
                logger.error(f"Target node for VM {vm_id} is its current node {source_node}")
                return MigrationResult.FAILED
            
            logger.info(f"Initiating cold migration of VM {vm_id} ({vm_type.upper()}) from {source_node} to {target_node}")

            if status != 'stopped':
                if status not in ('running', 'paused'):
                    logger.error(f"Cannot shut down VM {vm_id}: unexpected status {status}")
                    return MigrationResult.FAILED
                operation_in_progress = True
                task_id = guest.status.shutdown.post()
                result = self._wait_for_task(source_node, vm_id, task_id, "Shutdown")
                if result != MigrationResult.SUCCESS:
                    return result
                operation_in_progress = False
                if guest.status.current.get().get('status', 'unknown').lower() != 'stopped':
                    logger.error(f"VM {vm_id} is not stopped after shutdown; migration cancelled")
                    return MigrationResult.FAILED

            operation_in_progress = True
            task_id = guest.migrate.post(target=target_node)
            result = self._wait_for_task(source_node, vm_id, task_id, "Migration")
            if result != MigrationResult.SUCCESS:
                return result
            operation_in_progress = False

            target = self.proxmox.nodes(target_node)
            target_guest = target.lxc(vm_id) if vm_type == 'lxc' else target.qemu(vm_id)
            operation_in_progress = True
            task_id = target_guest.status.start.post()
            return self._wait_for_task(target_node, vm_id, task_id, "Start")
            
        except Exception as e:
            logger.error(f"Error migrating VM {vm_id}: {e}")
            return MigrationResult.UNKNOWN if operation_in_progress else MigrationResult.FAILED
    
    def _wait_for_task(self, node: str, vm_id: str, task_id: str, operation: str) -> MigrationResult:
        """Wait for a Proxmox task to finish, without treating a timeout as failure."""
        start_time = time.monotonic()
        
        while time.monotonic() - start_time < self.migration_timeout:
            try:
                task_status = self.proxmox.nodes(node).tasks(task_id).status.get()
                if task_status.get('status') == 'stopped':
                    exit_status = task_status.get('exitstatus')
                    if exit_status == 'OK':
                        logger.info(f"{operation} completed for VM {vm_id}, task ID: {task_id}")
                        return MigrationResult.SUCCESS
                    if not exit_status:
                        logger.error(f"{operation} task {task_id} for VM {vm_id} has no exit status")
                        return MigrationResult.UNKNOWN
                    logger.error(f"{operation} failed for VM {vm_id}: {exit_status}")
                    return MigrationResult.FAILED
            except Exception as e:
                logger.warning(f"Error checking {operation.lower()} task {task_id}: {e}")
            
            time.sleep(2)
        
        logger.error(f"{operation} timeout for VM {vm_id}, task ID: {task_id}; outcome unknown")
        return MigrationResult.UNKNOWN


# ============================================================================
# CIP Client Functions
# ============================================================================

class CIPMonitor:
    """Handles CIP health checks via pulse mechanism with subscription callbacks."""
    
    def __init__(self, vm_id: str, host: str, ip_id: int, pulse_signal: int = 1, timeout: float = 3.0):
        self.vm_id = vm_id  # Which VM this monitor is for
        self.host = host
        self.ip_id = ip_id
        self.pulse_signal = pulse_signal  # CIP digital or analog signal index for pulse
        self.timeout = timeout
        self.cip_client: Optional[cipclient.CIPSocketClient] = None
        self.cip_sub: Optional[cipclient.CIPSocketClient] = None
        self.connected = False
        self.pulse_received = False
        self.last_pulse_time: Optional[datetime] = None
    
    def connect(self) -> bool:
        """Initialize and start CIP client with subscription for this VM."""
        try:
            # Main CIP client for sending commands
            self.cip_client = cipclient.CIPSocketClient(self.host, self.ip_id)
            self.cip_client.start()
            logger.info(f"[VM {self.vm_id}] CIP client initialized for {self.host}:{self.ip_id}")
            
            # Subscription client for receiving CIP feedback
            self.cip_sub = cipclient.CIPSocketClient(self.host, self.ip_id)
            self.cip_sub.start()
            
            # Subscribe to pulse signal with callback
            self.cip_sub.subscribe('d', self.pulse_signal, self._cip_callback)
            logger.info(f"[VM {self.vm_id}] Subscribed to CIP signal {self.pulse_signal} for health pulses")
            
            # Give it time to connect
            time.sleep(2)
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"[VM {self.vm_id}] Failed to initialize CIP client: {e}")
            self.connected = False
            return False
    
    def _cip_callback(self, dataType: str, index: int, data: any) -> None:
        """
        Callback handler for CIP subscription responses.
        
        Args:
            dataType: Data type ('d' for digital, 'a' for analog, 's' for serial)
            index: Signal index
            data: The received data value
        """
        try:
            self.last_pulse_time = datetime.now()
            self.pulse_received = True
            logger.debug(f"[VM {self.vm_id}] CIP Callback - Type: {dataType}, Index: {index}, Data: {data}")
        except Exception as e:
            logger.error(f"[VM {self.vm_id}] Error in CIP callback: {e}")
    
    def send_pulse(self) -> bool:
        """
        Send a health pulse to the CIP device and wait for response.
        
        Pulse mechanism: Set digital signal HIGH, then LOW to trigger the
        remote Crestron program. The program mirrors the signal, which
        triggers our subscription callback.
        
        Returns:
            True if pulse received response, False otherwise
        """
        try:
            if not self.cip_sub or not self.connected:
                logger.warning(f"[VM {self.vm_id}] CIP client not connected")
                return False
            
            logger.debug(f"[VM {self.vm_id}] Sending CIP pulse on signal {self.pulse_signal}")
            
            # Reset pulse flag before sending
            self.pulse_received = False
            
            # Pulse pattern: HIGH then LOW to trigger remote device
            # The Crestron program will mirror this signal back
            try:
                # Set signal HIGH (true)
                self.cip_sub.set('d', self.pulse_signal, True)
                logger.debug(f"[VM {self.vm_id}] CIP pulse HIGH sent")
                time.sleep(0.1)  # Small delay between HIGH and LOW
                
                # Set signal LOW (false)
                self.cip_sub.set('d', self.pulse_signal, False)
                logger.debug(f"[VM {self.vm_id}] CIP pulse LOW sent")
            except Exception as e:
                logger.error(f"[VM {self.vm_id}] Error sending CIP pulse: {e}")
                return False
            
            # Wait for callback response with timeout
            # When Crestron program mirrors the signal, callback triggers
            start_time = time.time()
            while time.time() - start_time < self.timeout:
                if self.pulse_received:
                    logger.debug(f"[VM {self.vm_id}] Received CIP response at {self.last_pulse_time}")
                    return True
                time.sleep(0.1)  # Small sleep to avoid busy waiting
            
            logger.warning(f"[VM {self.vm_id}] No CIP response received (timeout after {self.timeout}s)")
            return False
                
        except socket.timeout:
            logger.warning(f"[VM {self.vm_id}] CIP pulse timeout")
            return False
        except Exception as e:
            logger.error(f"[VM {self.vm_id}] Error sending CIP pulse: {e}")
            return False
    
    def disconnect(self):
        """Disconnect CIP client."""
        try:
            if self.cip_sub:
                self.cip_sub.stop()
                logger.info("CIP subscription client disconnected")
            if self.cip_client:
                self.cip_client.stop()
                logger.info("CIP client disconnected")
            self.connected = False
        except Exception as e:
            logger.error(f"Error disconnecting CIP client: {e}")


# ============================================================================
# Main Monitor Logic
# ============================================================================

class VMMonitorSystem:
    """Main monitoring system coordinator."""
    
    def __init__(self, vm_ids: List[str], proxmox_config: Dict, config_loader):
        self.vm_ids = vm_ids
        self.config_loader = config_loader
        
        # Get monitor config for failure_threshold
        monitor_config = config_loader.get_monitor_config()
        self.failure_threshold = monitor_config.get('failure_threshold', 3)
        self.monitor_interval = monitor_config.get('interval', 10)
        self.migration_timeout = monitor_config.get('migration_timeout', 60)
        
        self.vm_monitors: Dict[str, VMHealthMonitor] = {
            vm_id: VMHealthMonitor(vm_id, self.failure_threshold) for vm_id in vm_ids
        }
        # Create a CIP monitor for EACH VM with per-VM CIP config
        self.cip_monitors: Dict[str, CIPMonitor] = {}
        for vm_id in vm_ids:
            vm_cip_config = config_loader.get_vm_cip_config(vm_id)
            self.cip_monitors[vm_id] = CIPMonitor(vm_id, **vm_cip_config)
        
        # Pass migration_timeout to ProxmoxManager
        proxmox_config_with_timeout = dict(proxmox_config)
        proxmox_config_with_timeout['migration_timeout'] = self.migration_timeout
        self.proxmox = ProxmoxManager(**proxmox_config_with_timeout)
        self.running = False
        self.start_time = datetime.now()
    
    async def initialize(self) -> bool:
        """Initialize all connections."""
        logger.info("Initializing monitor system...")
        
        if not self.proxmox.connect():
            logger.error("Failed to connect to Proxmox")
            return False
        
        # Initialize CIP connection for each VM
        failed_vms = []
        for vm_id, cip_monitor in self.cip_monitors.items():
            if not cip_monitor.connect():
                logger.warning(f"[VM {vm_id}] Failed to connect to CIP - continuing with Proxmox checks only")
                failed_vms.append(vm_id)
        
        if failed_vms:
            logger.warning(f"CIP connection failed for VMs: {', '.join(failed_vms)}")
        
        logger.info("Monitor system initialized successfully")
        return True
    
    async def check_vm_health(self, vm_id: str, vm_status: Optional[VMStatus] = None) -> HealthCheckResult:
        """
        Perform comprehensive health check on a VM.
        
        Returns:
            HealthCheckResult enum
        """
        logger.debug(f"Checking health of VM {vm_id}")
        
        # Check 1: Container status
        status = vm_status if vm_status is not None else self.proxmox.get_vm_status(vm_id)
        if status != VMStatus.RUNNING:
            logger.warning(f"[VM {vm_id}] Container is not running (status: {status})")
            return HealthCheckResult.UNHEALTHY
        
        # Check 2: CIP pulse (using VM-specific CIP monitor)
        cip_monitor = self.cip_monitors.get(vm_id)
        if cip_monitor and cip_monitor.connected:
            cip_alive = cip_monitor.send_pulse()
            if not cip_alive:
                logger.warning(f"VM {vm_id} failed CIP pulse check")
                return HealthCheckResult.UNHEALTHY
            logger.debug(f"VM {vm_id} CIP pulse OK")
        else:
            logger.debug(f"CIP not available for VM {vm_id}, relying on container status")
        
        return HealthCheckResult.HEALTHY
    
    async def monitor_loop(self):
        """Main monitoring loop - runs every monitor_interval seconds."""
        self.running = True
        logger.info(f"Starting monitor loop with {self.monitor_interval}s interval")
        
        try:
            while self.running:
                loop_start = time.time()
                logger.info("=" * 60)
                logger.info(f"Health check cycle started")
                
                for vm_id in self.vm_monitors.keys():
                    monitor = self.vm_monitors[vm_id]
                    monitor.last_check_time = datetime.now()

                    if monitor.migration_uncertain:
                        logger.error(f"[VM {vm_id}] Migration outcome is unknown; manual investigation required")
                        continue

                    remaining = monitor.recovery_countdown_remaining()
                    if remaining is not None:
                        logger.info(f"[VM {vm_id}] Waiting for boot grace period ({remaining:.1f}s remaining)")
                        continue

                    status = self.proxmox.get_vm_status(vm_id)
                    if status == VMStatus.STOPPED:
                        if monitor.recovery_attempted:
                            logger.warning(f"[VM {vm_id}] Container is still stopped after the boot grace period")
                            health = HealthCheckResult.UNHEALTHY
                        else:
                            logger.warning(f"[VM {vm_id}] Container is stopped; attempting to start it")
                            if self.proxmox.start_vm(vm_id):
                                monitor.start_recovery_countdown(self.migration_timeout)
                                logger.info(
                                    f"[VM {vm_id}] Boot grace period started for {self.migration_timeout}s "
                                    "before health failures are counted"
                                )
                                continue
                            health = HealthCheckResult.UNHEALTHY
                    else:
                        health = await self.check_vm_health(vm_id, status)
                    
                    if health == HealthCheckResult.HEALTHY:
                        monitor.record_success()
                        logger.info(f"✓ VM {vm_id}: HEALTHY")
                    else:
                        monitor.record_failure()
                        logger.warning(f"✗ VM {vm_id}: UNHEALTHY (Failures: {monitor.consecutive_failures}/{self.failure_threshold})")
                        
                        # Check if migration threshold reached
                        if monitor.should_migrate():
                            logger.critical(f"⚠ VM {vm_id}: Failure threshold reached! Initiating migration...")
                            monitor.mark_migration_triggered()
                            
                            # Perform migration asynchronously
                            asyncio.create_task(self._migrate_with_logging(vm_id))
                
                # Log current status
                self._log_status_summary()
                
                # Calculate sleep time to maintain interval
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.monitor_interval - elapsed)
                
                if sleep_time > 0:
                    logger.debug(f"Cycle completed in {elapsed:.2f}s, sleeping {sleep_time:.2f}s")
                    await asyncio.sleep(sleep_time)
                else:
                    logger.warning(f"Health check took longer than interval ({elapsed:.2f}s > {self.monitor_interval}s)")
                    await asyncio.sleep(0.5)  # Minimum sleep
                    
        except asyncio.CancelledError:
            logger.info("Monitor loop cancelled")
            self.running = False
        except Exception as e:
            logger.error(f"Fatal error in monitor loop: {e}", exc_info=True)
            self.running = False
    
    async def _migrate_with_logging(self, vm_id: str):
        """Perform migration with logging."""
        try:
            result = self.proxmox.migrate_vm(vm_id)
            if result == MigrationResult.SUCCESS:
                self.vm_monitors[vm_id].start_recovery_countdown(self.migration_timeout)
                logger.info(f"✓ VM {vm_id} cold migration completed; boot grace period started")
            elif result == MigrationResult.FAILED:
                logger.error(f"✗ VM {vm_id} migration failed")
                self.vm_monitors[vm_id].record_migration_failure()
                logger.warning(
                    f"[VM {vm_id}] Failure counter reset; migration will be retried after "
                    f"{self.failure_threshold} consecutive failures"
                )
            else:
                self.vm_monitors[vm_id].migration_uncertain = True
                logger.error(f"[VM {vm_id}] Migration outcome is unknown; manual investigation required")
        except Exception as e:
            logger.error(f"Error during VM {vm_id} migration: {e}", exc_info=True)
            self.vm_monitors[vm_id].migration_uncertain = True
            logger.error(f"[VM {vm_id}] Migration outcome is unknown; manual investigation required")
    
    def _log_status_summary(self):
        """Log summary of all VM statuses."""
        summary_lines = ["Status Summary:"]
        for vm_id, monitor in self.vm_monitors.items():
            status = monitor.get_status()
            summary_lines.append(
                f"  VM {vm_id}: {status['consecutive_failures']}/{self.failure_threshold} failures, "
                f"Last healthy: {status['last_healthy']}"
            )
        logger.info("\n".join(summary_lines))
    
    async def shutdown(self):
        """Gracefully shutdown the monitor."""
        logger.info("Shutting down monitor system...")
        self.running = False
        
        # Disconnect all CIP monitors
        for vm_id, cip_monitor in self.cip_monitors.items():
            try:
                cip_monitor.disconnect()
            except Exception as e:
                logger.error(f"[VM {vm_id}] Error disconnecting CIP: {e}")
        
        uptime = datetime.now() - self.start_time
        logger.info(f"Monitor system shutdown. Uptime: {uptime}")


# ============================================================================
# Entry Point
# ============================================================================

async def main():
    """Main entry point."""
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='Proxmox VC4 Monitor')
    parser.add_argument('--config', type=str, default=None,
                       help='Path to config file (JSON or YAML). Default: config.json or config.yaml')
    args = parser.parse_args()
    
    # Load configuration from file (JSON/YAML) and environment variables
    try:
        from config_loader import ConfigLoader
        config_loader = ConfigLoader(args.config)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return
    
    # Setup logging from config
    logging_config = config_loader.get_logging_config()
    log_level = logging.getLevelName(logging_config.get('level', 'INFO'))
    log_file = logging_config.get('file', 'proxmox_monitor.log')
    log_format = logging_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Reconfigure logging with config values
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    
    logger.info("Starting Proxmox VM Monitor System")
    
    # Get configuration sections
    proxmox_config = config_loader.get_proxmox_config()
    monitor_config = config_loader.get_monitor_config()
    
    # Initialize monitor system with config_loader for per-VM CIP config
    monitor = VMMonitorSystem(
        monitor_config['vm_ids'],
        proxmox_config,
        config_loader
    )
    
    if not await monitor.initialize():
        logger.error("Failed to initialize monitor system")
        return
    
    try:
        # Run monitor loop
        await monitor.monitor_loop()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await monitor.shutdown()



if __name__ == "__main__":
    asyncio.run(main())
