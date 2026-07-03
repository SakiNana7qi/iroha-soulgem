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
    "config": dict(DEFAULT_CONFIG),
}
_lock = threading.Lock()

# ── thread handle ──────────────────────────────────────────────────
_thread = None
_stop_event = threading.Event()
_ipmi = None
_nvml_ok = False


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
    except Exception:
        return False


# ── temperature readers ────────────────────────────────────────────

def _read_cpu_temps():
    """Read CPU temperatures from BMC."""
    try:
        ipmi = _get_ipmi()
        temps = {}
        for s in ipmi.get_sensor_data():
            if s.type == "Temperature" and s.value is not None:
                temps[s.name] = s.value
        return temps
    except Exception as e:
        logger.warning(f"BMC temp read failed: {e}")
        return {}


def _read_gpu_temps():
    """Read GPU temperatures via NVML."""
    if not _init_nvml():
        return {}
    try:
        count = pynvml.nvmlDeviceGetCount()
        temps = {}
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            temps[f"GPU{i}"] = temp
        return temps
    except Exception as e:
        logger.warning(f"NVML temp read failed: {e}")
        return {}


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

def _set_zone_pwm(zone, pwm_value):
    """pwm_value: 0–100"""
    pwm = max(0, min(100, int(pwm_value)))
    try:
        ipmi = _get_ipmi()
        ipmi.raw_command(netfn=0x30, command=0x70, data=(0x66, 0x01, zone, pwm))
    except Exception as e:
        logger.warning(f"Set zone {zone} PWM={pwm} failed: {e}")


def _set_fan_mode(mode_byte):
    """0x00=Standard 0x01=Full 0x02=Optimal 0x04=HeavyIO"""
    try:
        ipmi = _get_ipmi()
        ipmi.raw_command(netfn=0x30, command=0x45, data=(0x01, mode_byte))
    except Exception as e:
        logger.warning(f"Set fan mode 0x{mode_byte:02x} failed: {e}")


def _read_current_pwm():
    pwm = {}
    for z in _state["config"]["zones"]:
        try:
            ipmi = _get_ipmi()
            rsp = ipmi.raw_command(netfn=0x30, command=0x70, data=(0x66, 0x00, z))
            pwm[z] = rsp["data"][0] if rsp["data"] else 0
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
    for z in _state["config"]["zones"]:
        _set_zone_pwm(z, target)


# ── main control loop ──────────────────────────────────────────────

def _control_loop():
    """Runs in a background thread."""
    cfg = _state["config"]
    interval = cfg["interval"]

    logger.info("Fan control thread starting...")

    # Defer initial BMC connection so server startup isn't blocked
    # Wait one interval before first connection attempt
    connected = False
    for attempt in range(5):
        try:
            _set_fan_mode(0x01)  # Full Speed mode
            connected = True
            logger.info("BMC connected, fan control active")
            break
        except Exception as e:
            logger.warning(f"BMC connect attempt {attempt + 1}/5: {e}")
            _stop_event.wait(timeout=interval)

    if not connected:
        with _lock:
            _state["error"] = "Failed to connect to BMC after 5 attempts"
            _state["running"] = False
        return

    while not _stop_event.is_set():
        try:
            mode = _state["mode"]
            cpu_temps = _read_cpu_temps()
            gpu_temps = _read_gpu_temps()
            fans = _read_fans()
            pwm = _read_current_pwm()
            all_temps = {**cpu_temps, **gpu_temps}

            if mode == "curve":
                target = _calc_target_pwm(cpu_temps, gpu_temps)
                _apply_pwm(target)
            elif mode == "manual":
                target = None
                for z, p in _state.get("_manual_pwm", {}).items():
                    _set_zone_pwm(z, p)
            elif mode == "full":
                _apply_pwm(100)
                target = 100
            elif mode == "bios":
                _set_fan_mode(0x02)  # Optimal
                target = None

            with _lock:
                _state["current_temps"] = all_temps
                _state["current_fan_rpm"] = fans
                _state["current_pwm"] = pwm
                _state["target_pwm"] = target if mode != "manual" else 0
                _state["last_update"] = time.time()
                _state["error"] = None

        except Exception as e:
            logger.error(f"Fan control error: {e}")
            with _lock:
                _state["error"] = str(e)

        _stop_event.wait(timeout=interval)

    # Cleanup: revert to Optimal mode on stop
    try:
        _set_fan_mode(0x02)
    except Exception:
        pass


# ── public API ─────────────────────────────────────────────────────

def configure(config_dict):
    """Apply configuration and restart if needed."""
    with _lock:
        cfg = dict(DEFAULT_CONFIG)
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

    if mode == "manual":
        # Initialize manual PWM with current values
        with _lock:
            current = _state["current_pwm"]
            _state["_manual_pwm"] = {
                z: current.get(z, 20) for z in _state["config"]["zones"]
            }

    with _lock:
        _state["mode"] = mode
    logger.info(f"Fan mode -> {mode}")


def set_manual_pwm(zone_values):
    """Set per-zone PWM (0-100). Only takes effect in 'manual' mode.
    zone_values: dict {zone_number: pwm_value}
    """
    with _lock:
        _state["_manual_pwm"] = {
            int(k): max(0, min(100, int(v))) for k, v in zone_values.items()
        }


def start():
    """Start fan control in a background thread."""
    global _thread, _stop_event, _state
    if _thread and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_control_loop, name="fanctl", daemon=True)
    _thread.start()
    with _lock:
        _state["running"] = True
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
