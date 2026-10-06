"""Fan control daemon for Supermicro BMC via pyghmi.

Modes:
  curve   — temperature-driven: 20% PWM normal, ramps to 100% at critical temp
  manual  — fixed PWM per zone, set via API
  full    — 100% PWM all zones (emergency)
  bios    — revert to BMC Optimal mode (auto)
"""

import threading
import time
import logging
import math
from copy import deepcopy

import pynvml

from pyghmi.ipmi import command

logger = logging.getLogger("fanctl")

# ── defaults (overridden by config.yaml) ──────────────────────────
DEFAULT_CONFIG = {
    "bmc_host": "192.168.1.123",
    "bmc_user": "ADMIN",
    "bmc_password": "ADMIN",
    "interval": 3,
    "curve": {
        "normal_pwm": 20,
        "cpu_ramp_start": 70,
        "cpu_full_speed": 85,
        "gpu_ramp_start": 80,
        "gpu_full_speed": 90,
    },
    "zones": [0, 1, 2, 3],
}

_state = {
    "mode": "curve",
    "running": False,
    "error": None,
    "last_update": None,
    "current_pwm": {},
    "current_fan_rpm": {},
    "current_temps": {},
    "target_pwm": 20,
    "failsafe": False,
    "config": deepcopy(DEFAULT_CONFIG),
}
_lock = threading.Lock()

# ── thread handle ──────────────────────────────────────────────────
_thread = None
_stop_event = threading.Event()
_ipmi = None
_nvml_ok = False
_expected_cpu_sensors = set()
_expected_gpu_count = 0


_ipmi_lock = threading.Lock()

def _get_ipmi():
    global _ipmi
    if _ipmi is not None:
        return _ipmi
    with _ipmi_lock:
        if _ipmi is not None:
            return _ipmi
        cfg = _state["config"]
        _ipmi = command.Command(
            bmc=cfg["bmc_host"],
            userid=cfg["bmc_user"],
            password=cfg["bmc_password"],
            keepalive=True,
        )
        return _ipmi


def _init_nvml():
    global _nvml_ok
    if _nvml_ok:
        return True
    try:
        pynvml.nvmlInit()
        _nvml_ok = True
        return True
    except (pynvml.NVMLError_LibraryNotFound, pynvml.NVMLError_DriverNotLoaded):
        # A machine without an NVIDIA driver is a supported CPU-only setup.
        return False


# ── temperature readers ────────────────────────────────────────────

def _read_cpu_temps():
    """Read CPU temperatures from BMC."""
    ipmi = _get_ipmi()
    temps = {}
    cpu_names = set()
    for s in ipmi.get_sensor_data():
        if s.type != "Temperature":
            continue
        is_cpu = "cpu" in s.name.lower()
        valid = (
            not getattr(s, "unavailable", False)
            and isinstance(s.value, (int, float))
            and math.isfinite(s.value)
        )
        if is_cpu and not valid:
            raise RuntimeError(f"CPU temperature unavailable: {s.name}")
        if valid:
            temps[s.name] = s.value
            if is_cpu:
                cpu_names.add(s.name)
    if not cpu_names:
        raise RuntimeError("No CPU temperature sensors available")
    missing = _expected_cpu_sensors - cpu_names
    if missing:
        raise RuntimeError(f"CPU temperature sensors missing: {', '.join(sorted(missing))}")
    _expected_cpu_sensors.update(cpu_names)
    return temps


def _read_gpu_temps():
    """Read GPU temperatures via NVML."""
    global _expected_gpu_count
    if not _init_nvml():
        return {}
    count = pynvml.nvmlDeviceGetCount()
    if count < _expected_gpu_count:
        raise RuntimeError("Previously detected GPUs are missing")
    _expected_gpu_count = count
    temps = {}
    for i in range(count):
        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        if not isinstance(temp, (int, float)) or not math.isfinite(temp):
            raise RuntimeError(f"GPU{i} temperature unavailable")
        temps[f"GPU{i}"] = temp
    return temps


def _read_fans():
    """Read fan RPMs from BMC."""
    try:
        ipmi = _get_ipmi()
        fans = {}
        for s in ipmi.get_sensor_data():
            if s.type == "Fan" and s.value is not None:
                fans[s.name] = s.value
        return fans
    except Exception:
        return {}


# ── PWM write ──────────────────────────────────────────────────────

def _raw_command(**kwargs):
    """pyghmi can return BMC errors without raising an exception."""
    response = _get_ipmi().raw_command(**kwargs)
    if not isinstance(response, dict):
        raise RuntimeError("Invalid BMC response")
    if response.get("error") or response.get("code", 0):
        raise RuntimeError(
            f"BMC rejected command: {response.get('error') or 'completion code'} "
            f"(code={response.get('code', 'unknown')})"
        )
    return response


def _set_zone_pwm(zone, pwm_value):
    """pwm_value: 0–100"""
    pwm = max(0, min(100, int(pwm_value)))
    try:
        _raw_command(netfn=0x30, command=0x70, data=(0x66, 0x01, zone, pwm))
    except Exception as e:
        raise RuntimeError(f"Set zone {zone} PWM={pwm} failed: {e}") from e


def _set_fan_mode(mode_byte):
    """0x00=Standard 0x01=Full 0x02=Optimal 0x04=HeavyIO"""
    try:
        _raw_command(netfn=0x30, command=0x45, data=(0x01, mode_byte))
    except Exception as e:
        raise RuntimeError(f"Set fan mode 0x{mode_byte:02x} failed: {e}") from e


def _read_current_pwm():
    pwm = {}
    for z in _state["config"]["zones"]:
        try:
            rsp = _raw_command(netfn=0x30, command=0x70, data=(0x66, 0x00, z))
            value = rsp["data"][0]
            if not 0 <= value <= 100:
                raise ValueError("Invalid PWM readback")
            pwm[z] = value
        except Exception:
            pwm[z] = -1
    return pwm


# ── curve calculator ───────────────────────────────────────────────

def _calc_target_pwm(cpu_temps, gpu_temps):
    """Calculate target PWM based on temperatures and curve config."""
    curve = _state["config"]["curve"]
    normal = curve["normal_pwm"]

    # CPU-driven component
    cpu_pwm = normal
    for name, temp in cpu_temps.items():
        if temp <= curve["cpu_ramp_start"]:
            continue
        if temp >= curve["cpu_full_speed"]:
            return 100
        ramp_range = curve["cpu_full_speed"] - curve["cpu_ramp_start"]
        fraction = (temp - curve["cpu_ramp_start"]) / ramp_range
        needed = normal + fraction * (100 - normal)
        cpu_pwm = max(cpu_pwm, needed)

    # GPU-driven component
    gpu_pwm = normal
    for name, temp in gpu_temps.items():
        if temp <= curve["gpu_ramp_start"]:
            continue
        if temp >= curve["gpu_full_speed"]:
            return 100
        ramp_range = curve["gpu_full_speed"] - curve["gpu_ramp_start"]
        fraction = (temp - curve["gpu_ramp_start"]) / ramp_range
        needed = normal + fraction * (100 - normal)
        gpu_pwm = max(gpu_pwm, needed)

    return max(cpu_pwm, gpu_pwm)


def _apply_pwm(target):
    """Write target PWM to all configured zones."""
    errors = []
    for z in _state["config"]["zones"]:
        try:
            _set_zone_pwm(z, target)
        except Exception as e:
            errors.append(str(e))
    if errors:
        raise RuntimeError("; ".join(errors))


# ── main control loop ──────────────────────────────────────────────

def _control_cycle(active_mode):
    """Apply one control cycle and publish failures as well as readings."""
    with _lock:
        mode = _state["mode"]
        manual_pwm = dict(_state.get("_manual_pwm", {}))

    errors = []
    temperatures = []
    for label, reader in (("BMC", _read_cpu_temps), ("GPU", _read_gpu_temps)):
        try:
            values = reader()
            if label == "BMC" and not values:
                raise RuntimeError("No CPU temperature sensors available")
            temperatures.append(values)
        except Exception as e:
            errors.append(f"{label} temperature read failed: {e}")
            temperatures.append({})

    cpu_temps, gpu_temps = temperatures
    failsafe = mode == "curve" and bool(errors)
    target = None
    try:
        # PWM overrides require Full mode, including when leaving BIOS mode.
        desired_mode = 0x02 if mode == "bios" else 0x01
        if active_mode != desired_mode:
            _set_fan_mode(desired_mode)
            active_mode = desired_mode

        if mode == "curve":
            target = 100 if failsafe else _calc_target_pwm(cpu_temps, gpu_temps)
            _apply_pwm(target)
        elif mode == "manual":
            write_errors = []
            for z, p in manual_pwm.items():
                try:
                    _set_zone_pwm(z, p)
                except Exception as e:
                    write_errors.append(str(e))
            if write_errors:
                raise RuntimeError("; ".join(write_errors))
        elif mode == "full":
            target = 100
            _apply_pwm(target)
    except Exception as e:
        errors.append(str(e))
        # Retry the BMC mode command next cycle after a failed write.
        active_mode = None

    fans = _read_fans()
    pwm = _read_current_pwm()
    if not fans:
        errors.append("Fan RPM readings unavailable")
    if any(p < 0 for p in pwm.values()):
        errors.append("Fan PWM readback failed")
    error = "; ".join(errors) or None
    with _lock:
        _state.update(
            current_temps={**cpu_temps, **gpu_temps},
            current_fan_rpm=fans,
            current_pwm=pwm,
            target_pwm=target,
            failsafe=failsafe,
            last_update=time.time(),
            error=error,
        )
    if error:
        logger.warning("Fan control: %s", error)
    return active_mode


def _control_loop():
    """Runs in a background thread."""
    cfg = _state["config"]
    interval = cfg["interval"]

    logger.info("Fan control thread starting...")

    # Connection and retries run in this thread, outside the server event loop.
    connected = False
    for attempt in range(5):
        if _stop_event.is_set():
            break
        try:
            _set_fan_mode(0x01)  # Full Speed mode
            connected = True
            logger.info("BMC connected, fan control active")
            break
        except Exception as e:
            logger.warning(f"BMC connect attempt {attempt + 1}/5: {e}")
            with _lock:
                _state["error"] = str(e)
            _stop_event.wait(timeout=interval)

    if not connected:
        with _lock:
            _state["error"] = f"Failed to connect to BMC: {_state['error']}"
            _state["running"] = False
        return

    active_mode = 0x01
    while not _stop_event.is_set():
        try:
            active_mode = _control_cycle(active_mode)
        except Exception as e:
            logger.error(f"Fan control error: {e}")
            with _lock:
                _state["error"] = str(e)

        _stop_event.wait(timeout=interval)

    # Cleanup: revert to Optimal mode on stop
    try:
        _set_fan_mode(0x02)
    except Exception as e:
        logger.error("Failed to restore BMC Optimal mode: %s", e)
        with _lock:
            _state["error"] = str(e)
    with _lock:
        _state["running"] = False


# ── public API ─────────────────────────────────────────────────────

def configure(config_dict):
    """Apply configuration and restart if needed."""
    with _lock:
        cfg = deepcopy(DEFAULT_CONFIG)
        _deep_update(cfg, config_dict)
        _state["config"] = cfg


def _deep_update(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def get_state():
    """Thread-safe snapshot of fan controller state."""
    with _lock:
        return {
            "mode": _state["mode"],
            "running": _state["running"],
            "error": _state["error"],
            "last_update": _state["last_update"],
            "current_pwm": dict(_state["current_pwm"]),
            "current_fan_rpm": dict(_state["current_fan_rpm"]),
            "current_temps": dict(_state["current_temps"]),
            "target_pwm": _state["target_pwm"],
            "failsafe": _state["failsafe"],
            "config": {
                "interval": _state["config"]["interval"],
                "curve": dict(_state["config"]["curve"]),
                "zones": list(_state["config"]["zones"]),
            },
        }


def set_mode(mode):
    """Switch fan control mode: curve | manual | full | bios."""
    if mode not in ("curve", "manual", "full", "bios"):
        raise ValueError(f"Unknown mode: {mode}")

    with _lock:
        if mode == "manual" and _state["mode"] != "manual":
            # Missing/failed readbacks must not initialize a zone to 0%.
            current = _state["current_pwm"]
            _state["_manual_pwm"] = {
                z: current[z] if 0 <= current.get(z, -1) <= 100 else 100
                for z in _state["config"]["zones"]
            }
        _state["mode"] = mode
    logger.info(f"Fan mode -> {mode}")


def set_manual_pwm(zone_values):
    """Set per-zone PWM (0-100). Only takes effect in 'manual' mode.
    zone_values: dict {zone_number: pwm_value}
    """
    with _lock:
        values = {
            int(k): max(0, min(100, int(v))) for k, v in zone_values.items()
        }
        unknown = values.keys() - set(_state["config"]["zones"])
        if unknown:
            raise ValueError(f"Unknown fan zones: {sorted(unknown)}")
        _state.setdefault("_manual_pwm", {}).update(values)


def start():
    """Start fan control in a background thread."""
    global _thread, _stop_event, _state
    if _thread and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_control_loop, name="fanctl", daemon=True)
    with _lock:
        _state["running"] = True
        _state["error"] = None
    _thread.start()
    logger.info("Fan control started")


def stop():
    """Stop fan control and revert to BMC Optimal mode."""
    global _thread
    _stop_event.set()
    if _thread:
        _thread.join(timeout=10)
    with _lock:
        _state["running"] = False
    logger.info("Fan control stopped")
