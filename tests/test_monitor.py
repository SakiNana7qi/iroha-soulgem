"""Regression checks using fake sensors and BMC; never start the service."""

import asyncio
from copy import deepcopy
from contextlib import ExitStack
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import main
from collectors import fanctl


def sensor(name, value, unavailable=False):
    return SimpleNamespace(
        name=name, type="Temperature", value=value, unavailable=unavailable
    )


class FanControlTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fanctl, "_state", deepcopy(fanctl._state)))
        self.stack.enter_context(patch.object(fanctl, "_expected_cpu_sensors", set()))
        self.stack.enter_context(patch.object(fanctl, "_expected_gpu_count", 0))
        self.stack.enter_context(patch.object(fanctl, "_nvml_ok", False))
        # Every hardware entry point is mocked, even in tests of the readers.
        self.ipmi = self.stack.enter_context(patch.object(fanctl, "_get_ipmi")).return_value
        self.stack.enter_context(patch.object(fanctl.pynvml, "nvmlInit"))
        self.stack.enter_context(patch.object(fanctl.pynvml, "nvmlDeviceGetCount", return_value=0))
        self.stack.enter_context(patch.object(fanctl.pynvml, "nvmlDeviceGetHandleByIndex", return_value="fake"))
        self.stack.enter_context(patch.object(fanctl.pynvml, "nvmlDeviceGetTemperature", return_value=45))
        self.stack.enter_context(patch.object(fanctl.logger, "warning"))
        fanctl.configure({"zones": [0, 1, 2, 3]})
        fanctl._state.update(mode="curve", error=None, failsafe=False, running=True)

    def cycle_mocks(self):
        cpu = self.stack.enter_context(patch.object(fanctl, "_read_cpu_temps", return_value={"CPU1 Temp": 40}))
        gpu = self.stack.enter_context(patch.object(fanctl, "_read_gpu_temps", return_value={}))
        self.stack.enter_context(patch.object(fanctl, "_read_fans", return_value={"FAN1": 3000}))
        self.stack.enter_context(patch.object(fanctl, "_read_current_pwm", return_value={z: 20 for z in range(4)}))
        write = self.stack.enter_context(patch.object(fanctl, "_set_zone_pwm"))
        mode = self.stack.enter_context(patch.object(fanctl, "_set_fan_mode"))
        return cpu, gpu, write, mode

    def test_cpu_requires_cpu_sensor_not_just_other_temperatures(self):
        self.ipmi.get_sensor_data.return_value = [sensor("System Temp", 30)]
        with self.assertRaisesRegex(RuntimeError, "No CPU"):
            fanctl._read_cpu_temps()

    def test_unavailable_and_invalid_cpu_temperatures_raise(self):
        for value, unavailable in ((None, False), (float("nan"), False), (50, True)):
            with self.subTest(value=value, unavailable=unavailable):
                self.ipmi.get_sensor_data.return_value = [sensor("CPU1 Temp", value, unavailable)]
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    fanctl._read_cpu_temps()

    def test_missing_second_cpu_is_detected(self):
        self.ipmi.get_sensor_data.return_value = [sensor("CPU1 Temp", 40), sensor("CPU2 Temp", 45)]
        fanctl._read_cpu_temps()
        self.ipmi.get_sensor_data.return_value = [sensor("CPU1 Temp", 40)]
        with self.assertRaisesRegex(RuntimeError, "CPU2 Temp"):
            fanctl._read_cpu_temps()

    def test_gpu_read_failure_is_not_empty_success(self):
        with patch.object(fanctl.pynvml, "nvmlDeviceGetCount", side_effect=RuntimeError("NVML failed")):
            with self.assertRaisesRegex(RuntimeError, "NVML failed"):
                fanctl._read_gpu_temps()

    def test_cpu_only_machine_without_nvidia_driver(self):
        with patch.object(fanctl.pynvml, "nvmlInit", side_effect=fanctl.pynvml.NVMLError_DriverNotLoaded):
            self.assertEqual(fanctl._read_gpu_temps(), {})

    def test_gpu_disappearance_is_detected(self):
        with patch.object(fanctl.pynvml, "nvmlDeviceGetCount", side_effect=[1, 0]):
            self.assertEqual(fanctl._read_gpu_temps(), {"GPU0": 45})
            with self.assertRaisesRegex(RuntimeError, "missing"):
                fanctl._read_gpu_temps()

    def test_cpu_only_healthy_curve(self):
        _, _, write, _ = self.cycle_mocks()
        fanctl._control_cycle(0x01)
        self.assertFalse(fanctl.get_state()["failsafe"])
        self.assertIsNone(fanctl.get_state()["error"])
        self.assertEqual(write.call_count, 4)
        self.assertTrue(all(c.args[1] == 20 for c in write.call_args_list))

    def test_temperature_loss_requests_full_speed_and_recovers(self):
        cpu, _, write, _ = self.cycle_mocks()
        cpu.return_value = {}
        fanctl._control_cycle(0x01)
        state = fanctl.get_state()
        self.assertTrue(state["failsafe"])
        self.assertEqual(state["target_pwm"], 100)
        self.assertIn("temperature read failed", state["error"])
        self.assertTrue(all(c.args[1] == 100 for c in write.call_args_list))
        cpu.return_value = {"CPU1 Temp": 40}
        fanctl._control_cycle(0x01)
        self.assertFalse(fanctl.get_state()["failsafe"])
        self.assertIsNone(fanctl.get_state()["error"])

    def test_gpu_failure_also_requests_full_speed(self):
        _, gpu, write, _ = self.cycle_mocks()
        gpu.side_effect = RuntimeError("lost GPU")
        fanctl._control_cycle(0x01)
        self.assertTrue(fanctl.get_state()["failsafe"])
        self.assertIn("lost GPU", fanctl.get_state()["error"])
        self.assertTrue(all(c.args[1] == 100 for c in write.call_args_list))

    def test_failed_write_does_not_skip_other_zones_or_report_success(self):
        _, _, write, _ = self.cycle_mocks()
        write.side_effect = [RuntimeError("zone 0 failed"), None, None, None]
        self.assertIsNone(fanctl._control_cycle(0x01))
        self.assertEqual(write.call_count, 4)
        self.assertIn("zone 0 failed", fanctl.get_state()["error"])
        self.assertIsNotNone(fanctl.get_state()["last_update"])

    def test_failed_mode_prevents_pwm_writes(self):
        _, _, write, mode = self.cycle_mocks()
        mode.side_effect = RuntimeError("mode failed")
        self.assertIsNone(fanctl._control_cycle(0x02))
        write.assert_not_called()
        self.assertIn("mode failed", fanctl.get_state()["error"])

    def test_bios_to_curve_restores_bmc_full_mode(self):
        _, _, write, mode = self.cycle_mocks()
        fanctl.set_mode("bios")
        active = fanctl._control_cycle(0x01)
        mode.assert_called_once_with(0x02)
        write.assert_not_called()
        fanctl.set_mode("curve")
        self.assertEqual(fanctl._control_cycle(active), 0x01)
        self.assertEqual(mode.call_args.args, (0x01,))
        self.assertEqual(write.call_count, 4)

    def test_bmc_write_helpers_propagate_failures(self):
        self.ipmi.raw_command.side_effect = RuntimeError("BMC offline")
        with self.assertRaisesRegex(RuntimeError, "zone 0"):
            fanctl._set_zone_pwm(0, 20)
        with self.assertRaisesRegex(RuntimeError, "fan mode"):
            fanctl._set_fan_mode(0x01)

    def test_empty_pwm_readback_is_failure_not_zero(self):
        self.ipmi.raw_command.return_value = {"data": []}
        self.assertEqual(fanctl._read_current_pwm(), {z: -1 for z in range(4)})

    def test_bmc_completion_codes_and_error_responses_are_failures(self):
        for response in ({"code": 0xC1, "data": []}, {"error": "timeout"}, None):
            with self.subTest(response=response):
                self.ipmi.raw_command.return_value = response
                with self.assertRaises(RuntimeError):
                    fanctl._set_zone_pwm(0, 20)
                with self.assertRaises(RuntimeError):
                    fanctl._set_fan_mode(0x01)
                self.assertEqual(fanctl._read_current_pwm(), {z: -1 for z in range(4)})

    def test_successful_bmc_response_and_pwm_readback(self):
        self.ipmi.raw_command.return_value = {"code": 0, "data": [25]}
        fanctl._set_zone_pwm(0, 25)
        fanctl._set_fan_mode(0x01)
        self.assertEqual(fanctl._read_current_pwm(), {z: 25 for z in range(4)})

    def test_startup_retries_and_retains_failure(self):
        with patch.object(fanctl, "_set_fan_mode", side_effect=RuntimeError("offline")) as mode, patch.object(fanctl, "_stop_event") as stop:
            stop.is_set.return_value = False
            fanctl._control_loop()
            self.assertEqual(mode.call_count, 5)
        self.assertFalse(fanctl.get_state()["running"])
        self.assertIn("offline", fanctl.get_state()["error"])

    def test_partial_manual_updates_keep_all_zones(self):
        fanctl._state["current_pwm"] = {z: 20 for z in range(4)}
        fanctl.set_mode("manual")
        fanctl.set_manual_pwm({"0": 35})
        fanctl.set_manual_pwm({"1": 45})
        fanctl.set_mode("manual")  # Repeated mode request must not reset values.
        self.assertEqual(fanctl._state["_manual_pwm"], {0: 35, 1: 45, 2: 20, 3: 20})
        _, _, write, _ = self.cycle_mocks()
        fanctl._control_cycle(0x01)
        self.assertIsNone(fanctl.get_state()["error"])
        self.assertEqual({c.args[0]: c.args[1] for c in write.call_args_list}, {0: 35, 1: 45, 2: 20, 3: 20})

    def test_unknown_zone_rejection_is_atomic(self):
        fanctl._state["_manual_pwm"] = {0: 20}
        with self.assertRaisesRegex(ValueError, "Unknown fan zones"):
            fanctl.set_manual_pwm({0: 30, 99: 40})
        self.assertEqual(fanctl._state["_manual_pwm"], {0: 20})

    def test_manual_initialization_uses_full_speed_for_missing_readback(self):
        fanctl._state["current_pwm"] = {0: -1, 1: 25}
        fanctl.set_mode("manual")
        self.assertEqual(fanctl._state["_manual_pwm"], {0: 100, 1: 25, 2: 100, 3: 100})

    def test_manual_mode_is_not_overridden_by_temperature_failure(self):
        fanctl._state["current_pwm"] = {z: 30 for z in range(4)}
        fanctl.set_mode("manual")
        cpu, _, write, _ = self.cycle_mocks()
        cpu.side_effect = RuntimeError("no temperatures")
        fanctl._control_cycle(0x01)
        self.assertFalse(fanctl.get_state()["failsafe"])
        self.assertIn("no temperatures", fanctl.get_state()["error"])
        self.assertTrue(all(c.args[1] == 30 for c in write.call_args_list))

    def test_configure_does_not_change_defaults(self):
        defaults = deepcopy(fanctl.DEFAULT_CONFIG)
        fanctl.configure({"curve": {"normal_pwm": 18}})
        self.assertEqual(fanctl.DEFAULT_CONFIG, defaults)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_hardware_does_not_block_api_or_command_collection(self):
        started = threading.Event()
        release = threading.Event()
        command_started = asyncio.Event()

        def slow_metrics():
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test did not release fake hardware collector")
            return {"cpu": {"usage_percent": 10}}

        async def commands():
            command_started.set()
            return []

        with patch.object(main, "_collection_lock", asyncio.Lock()), patch.object(main, "collect_metrics", side_effect=slow_metrics), patch.object(main.cmd_runner, "get_all", side_effect=commands):
            task = asyncio.create_task(main.collect_all())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                await asyncio.wait_for(command_started.wait(), timeout=1)
                # A request must complete while the sensor thread is still blocked.
                with patch.object(main, "refresh_interval", 1):
                    response = await asyncio.wait_for(main.set_interval(main.IntervalUpdate(interval=500)), timeout=0.5)
                    self.assertEqual(response, {"interval_ms": 500})
                self.assertFalse(task.done())
            finally:
                release.set()
                snapshot = await task
            self.assertEqual(snapshot["cpu"]["usage_percent"], 10)
            self.assertEqual(snapshot["commands"], [])
            self.assertIn("timestamp", snapshot)

    async def test_pwm_api_returns_client_error_for_unknown_zone(self):
        with patch.object(main, "fanctl_enabled", True), patch.object(main, "require_fan_password", new_callable=AsyncMock), patch.object(fanctl, "set_manual_pwm", side_effect=ValueError("Unknown fan zones")):
            response = await main.api_fanctl_pwm(main.FanPwmUpdate(zones={99: 20}))
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
