import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


proxmoxer = types.ModuleType("proxmoxer")
proxmoxer.ProxmoxAPI = object
cipclient = types.ModuleType("cipclient")
cipclient.CIPSocketClient = object
path = pathlib.Path(__file__).resolve().parents[1] / "Proxmox_Manager.py"
spec = importlib.util.spec_from_file_location("monitor_under_test", path)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"proxmoxer": proxmoxer, "cipclient": cipclient}):
    spec.loader.exec_module(module)


class ColdMigrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = module.ProxmoxManager("host", "user", "password")
        self.manager.proxmox = MagicMock()
        self.api = self.manager.proxmox
        self.api.nodes.get.return_value = [{"node": "source"}, {"node": "target"}]
        self.source = self.api.nodes("source")
        self.target = self.api.nodes("target")
        self.source_guest = self.source.lxc("101")
        self.target_guest = self.target.lxc("101")
        self.source_guest.status.current.get.side_effect = [
            {"status": "running"},
            {"status": "stopped"},
        ]
        self.source_guest.status.shutdown.post.return_value = "shutdown-task"
        self.source_guest.migrate.post.return_value = "migration-task"
        self.target_guest.status.start.post.return_value = "start-task"
        self.events = []
        self.source_guest.status.shutdown.post.side_effect = lambda: self.events.append("shutdown") or "shutdown-task"
        self.source_guest.migrate.post.side_effect = lambda **kwargs: self.events.append("migrate") or "migration-task"
        self.target_guest.status.start.post.side_effect = lambda: self.events.append("start") or "start-task"
        self.source.tasks.return_value.status.get.return_value = {"status": "stopped", "exitstatus": "OK"}
        self.target.tasks.return_value.status.get.return_value = {"status": "stopped", "exitstatus": "OK"}

    def test_shutdown_migrate_offline_and_start_target(self):
        self.assertEqual(self.manager.migrate_vm("101"), module.MigrationResult.SUCCESS)
        self.assertEqual(self.events, ["shutdown", "migrate", "start"])
        self.source_guest.migrate.post.assert_called_once_with(target="target")
        self.source.tasks.assert_any_call("shutdown-task")
        self.source.tasks.assert_any_call("migration-task")
        self.target.tasks.assert_called_with("start-task")

    def test_guest_status_includes_its_node(self):
        self.assertEqual(self.manager.get_vm_status_and_node("101"),
                         (module.VMStatus.RUNNING, "source"))

    def test_qemu_uses_offline_migration(self):
        self.source.lxc.return_value.status.current.get.side_effect = RuntimeError("not a container")
        self.source_guest = self.source.qemu("101")
        self.source_guest.status.current.get.side_effect = [{"status": "running"}, {"status": "stopped"}]
        self.source_guest.status.shutdown.post.return_value = "shutdown-task"
        self.source_guest.migrate.post.return_value = "migration-task"
        self.target.qemu("101").status.start.post.return_value = "start-task"
        self.assertEqual(self.manager.migrate_vm("101"), module.MigrationResult.SUCCESS)
        self.source_guest.migrate.post.assert_called_once_with(target="target")
        self.target.qemu("101").status.start.post.assert_called_once_with()

    def test_already_stopped_guest_skips_shutdown(self):
        self.source_guest.status.current.get.side_effect = [{"status": "stopped"}]
        self.assertEqual(self.manager.migrate_vm("101"), module.MigrationResult.SUCCESS)
        self.assertEqual(self.events, ["migrate", "start"])

    def test_shutdown_failure_does_not_migrate(self):
        self.source.tasks.return_value.status.get.return_value = {"status": "stopped", "exitstatus": "ERROR"}
        self.assertEqual(self.manager.migrate_vm("101"), module.MigrationResult.FAILED)
        self.assertEqual(self.events, ["shutdown"])

    def test_failed_migration_does_not_start_target(self):
        self.source.tasks.side_effect = lambda task_id: types.SimpleNamespace(
            status=types.SimpleNamespace(get=lambda: {
                "status": "stopped",
                "exitstatus": "OK" if task_id == "shutdown-task" else "ERROR",
            })
        )
        self.assertEqual(self.manager.migrate_vm("101"), module.MigrationResult.FAILED)
        self.assertEqual(self.events, ["shutdown", "migrate"])

    def test_migration_timeout_is_unknown_and_does_not_start_target(self):
        self.source.tasks.side_effect = lambda task_id: types.SimpleNamespace(
            status=types.SimpleNamespace(get=lambda: (
                {"status": "stopped", "exitstatus": "OK"} if task_id == "shutdown-task"
                else {"status": "running"}
            ))
        )
        self.manager.migration_timeout = 1
        with patch.object(module.time, "monotonic", side_effect=[0, 0, 0, 0, 2]), \
                patch.object(module.time, "sleep"):
            self.assertEqual(self.manager.migrate_vm("101"), module.MigrationResult.UNKNOWN)
        self.assertEqual(self.events, ["shutdown", "migrate"])

    def test_unknown_pauses_monitor_and_success_starts_grace_period(self):
        system = object.__new__(module.VMMonitorSystem)
        system.vm_monitors = {"101": module.VMHealthMonitor("101")}
        system.failure_threshold = 3
        system.migration_timeout = 60
        system.cip_monitors = {"101": module.CIPMonitor("101", "cip-host", 0x13)}
        system.proxmox = types.SimpleNamespace(migrate_vm=lambda vm_id: module.MigrationResult.UNKNOWN)
        asyncio.run(system._migrate_with_logging("101"))
        self.assertTrue(system.vm_monitors["101"].migration_uncertain)

        system.vm_monitors["101"] = module.VMHealthMonitor("101")
        system.proxmox.migrate_vm = lambda vm_id: module.MigrationResult.SUCCESS
        asyncio.run(system._migrate_with_logging("101"))
        self.assertGreater(system.vm_monitors["101"].recovery_countdown_remaining(), 0)
        self.assertEqual(system.vm_monitors["101"].consecutive_failures, 0)
        self.assertTrue(system.cip_monitors["101"].reinitialize_required)

    def test_failure_rearms_stopped_guest_recovery(self):
        system = object.__new__(module.VMMonitorSystem)
        monitor = module.VMHealthMonitor("101")
        monitor.recovery_attempted = True
        monitor.migration_triggered = True
        monitor.consecutive_failures = 3
        system.vm_monitors = {"101": monitor}
        system.failure_threshold = 3
        system.proxmox = types.SimpleNamespace(migrate_vm=lambda vm_id: module.MigrationResult.FAILED)
        asyncio.run(system._migrate_with_logging("101"))
        self.assertFalse(monitor.recovery_attempted)
        self.assertFalse(monitor.migration_triggered)
        self.assertEqual(monitor.consecutive_failures, 0)

    def test_unknown_outcome_suppresses_monitor_actions(self):
        system = object.__new__(module.VMMonitorSystem)
        monitor = module.VMHealthMonitor("101")
        monitor.migration_uncertain = True
        system.vm_monitors = {"101": monitor}
        system.failure_threshold = 3
        system.monitor_interval = 1
        system.proxmox = MagicMock()
        system._log_status_summary = lambda: None

        async def stop_after_cycle(delay):
            system.running = False

        with patch.object(module.asyncio, "sleep", side_effect=stop_after_cycle):
            asyncio.run(system.monitor_loop())
        system.proxmox.get_vm_status_and_node.assert_not_called()
        system.proxmox.start_vm.assert_not_called()
        self.assertEqual(monitor.consecutive_failures, 0)


class CIPReinitializationTests(unittest.TestCase):
    def setUp(self):
        self.system = object.__new__(module.VMMonitorSystem)
        self.system.vm_monitors = {"101": module.VMHealthMonitor("101")}
        self.cip = module.CIPMonitor("101", "cip-host", 0x13)
        self.system.cip_monitors = {"101": self.cip}
        self.system.proxmox = MagicMock()
        self.clients = [MagicMock() for _ in range(6)]
        self.client_factory = patch.object(module.cipclient, "CIPSocketClient", side_effect=self.clients)
        self.sleep = patch.object(module.time, "sleep")
        self.client_factory.start()
        self.sleep.start()
        self.addCleanup(self.client_factory.stop)
        self.addCleanup(self.sleep.stop)
        self.assertTrue(self.cip.connect())

    def test_failed_pulse_reinitializes_before_next_pulse(self):
        with patch.object(self.cip, "send_pulse", side_effect=[False, True]) as pulse:
            self.assertEqual(asyncio.run(self.system.check_vm_health("101", module.VMStatus.RUNNING)),
                             module.HealthCheckResult.UNHEALTHY)
            self.assertEqual(asyncio.run(self.system.check_vm_health("101", module.VMStatus.RUNNING)),
                             module.HealthCheckResult.HEALTHY)
        self.assertEqual(pulse.call_count, 2)
        self.clients[0].stop.assert_called_once()
        self.clients[1].stop.assert_called_once()
        self.clients[3].subscribe.assert_called_once_with("d", 1, self.cip._cip_callback)
        self.assertFalse(self.cip.reinitialize_required)

    def test_failed_reinitialization_is_retried_without_skipping_cip(self):
        self.cip.reinitialize_required = True
        self.clients[2].start.side_effect = RuntimeError("unavailable")
        with patch.object(self.cip, "send_pulse", return_value=True) as pulse:
            self.assertEqual(asyncio.run(self.system.check_vm_health("101", module.VMStatus.RUNNING)),
                             module.HealthCheckResult.UNHEALTHY)
            pulse.assert_not_called()
            self.assertTrue(self.cip.reinitialize_required)
            self.assertEqual(asyncio.run(self.system.check_vm_health("101", module.VMStatus.RUNNING)),
                             module.HealthCheckResult.HEALTHY)
            pulse.assert_called_once()

    def test_external_host_change_reinitializes_before_pulse(self):
        self.system.monitor_interval = 1
        self.system.failure_threshold = 3
        self.system.proxmox.get_vm_status_and_node.side_effect = [
            (module.VMStatus.RUNNING, "source"),
            (module.VMStatus.UNKNOWN, None),
            (module.VMStatus.RUNNING, "target"),
        ]
        self.system._log_status_summary = lambda: None
        cycles = []

        async def stop_after_three_cycles(delay):
            cycles.append(delay)
            if len(cycles) == 3:
                self.system.running = False

        def verify_pulse():
            if len(cycles) == 2:
                self.clients[3].subscribe.assert_called_once_with("d", 1, self.cip._cip_callback)
            return True

        with patch.object(self.cip, "send_pulse", side_effect=verify_pulse) as pulse, \
                patch.object(module.asyncio, "sleep", side_effect=stop_after_three_cycles):
            asyncio.run(self.system.monitor_loop())
        self.assertEqual(pulse.call_count, 2)
        self.clients[0].stop.assert_called_once()
        self.clients[1].stop.assert_called_once()
        self.clients[3].subscribe.assert_called_once_with("d", 1, self.cip._cip_callback)


if __name__ == "__main__":
    unittest.main()
