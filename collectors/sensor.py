import shutil
import subprocess
import os
import re


def _detect_method(config_method):
    if config_method and config_method != "auto":
        return config_method

    if shutil.which("ipmitool"):
        return "ipmitool"

    sd5_bat = r"C:\Program Files\Supermicro\SuperDoctor5\sdc.bat"
    if os.path.isfile(sd5_bat):
        return "sd5"

    return "wmi"


def _classify(name):
    lower = name.lower()
    if "temp" in lower:
        return "temperature"
    if "fan" in lower:
        return "fan"
    if "volt" in lower or re.match(r"^\d+(\.\d+)?v\b", lower):
        return "voltage"
    if "power" in lower or "ps" in lower.split():
        return "power"
    if "intru" in lower:
        return "chassis"
    if "ecc" in lower:
        return "memory"
    if "raid" in lower or "slot" in lower or "vd " in lower or "physicaldrive" in lower:
        return "disk"
    return "other"


def _parse_sd5_table(output):
    sensors = []
    lines = output.strip().splitlines()

    header_idx = None
    for i, line in enumerate(lines):
        if "Monitored Item" in line and "Reading" in line:
            header_idx = i
            break
    if header_idx is None:
        return sensors

    sep_idx = header_idx + 1
    if sep_idx >= len(lines) or not lines[sep_idx].startswith("---"):
        return sensors

    for line in lines[sep_idx + 1:]:
        stripped = line.strip()
        if not stripped or line.startswith("---") or line.startswith("----"):
            continue
        if stripped.startswith("\\\\") or stripped.startswith("("):
            continue

        parts = line.split()
        if not parts:
            continue

        first_val_idx = None
        for j, p in enumerate(parts):
            if re.match(r"^[\d.]+$", p) or p in ("Good", "Triggered", "Unavailable", "CRITICAL"):
                first_val_idx = j
                break

        if first_val_idx is None:
            continue

        name = " ".join(parts[:first_val_idx])
        rest = parts[first_val_idx:]

        high = ""
        low = ""
        reading = ""
        status = ""

        is_fan = "fan" in name.lower()

        if len(rest) >= 6 and re.match(r"^[\d.]+$", rest[0]):
            high = f"{rest[0]} {rest[1]}"
            low = f"{rest[2]} {rest[3]}"
            reading = f"{rest[4]} {rest[5]}"
            if len(rest) > 6:
                status = " ".join(rest[6:])
        elif len(rest) >= 4 and re.match(r"^[\d.]+$", rest[0]):
            if is_fan:
                low = f"{rest[0]} {rest[1]}"
                reading = f"{rest[2]} {rest[3]}"
            else:
                high = f"{rest[0]} {rest[1]}"
                reading = f"{rest[2]} {rest[3]}"
            if len(rest) > 4:
                status = " ".join(rest[4:])
        elif len(rest) >= 2 and re.match(r"^[\d.]+$", rest[0]):
            reading = f"{rest[0]} {rest[1]}"
            if len(rest) > 2:
                status = " ".join(rest[2:])
        else:
            status = " ".join(rest)

        value = reading.split()[0] if reading else status
        unit = reading.split()[1] if reading and " " in reading else ""

        sensors.append({
            "name": name,
            "value": value,
            "unit": unit,
            "high": high,
            "low": low,
            "status": status,
            "type": _classify(name),
        })
    return sensors


def _read_sd5():
    sd5_bat = r"C:\Program Files\Supermicro\SuperDoctor5\sdc.bat"
    try:
        result = subprocess.run(
            ["cmd", "/c", sd5_bat],
            capture_output=True, text=True, timeout=15
        )
        if not result.stdout.strip():
            return []
        return _parse_sd5_table(result.stdout)
    except Exception:
        return []


def _read_wmi():
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance MSAcpi_ThermalZoneTemperature -Namespace 'root/wmi' | "
             "Select-Object InstanceName, CurrentTemperature | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        import json
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]

        sensors = []
        for item in data:
            temp_c = (item.get("CurrentTemperature", 0) / 10.0) - 273.15
            sensors.append({
                "name": item.get("InstanceName", "ThermalZone"),
                "value": f"{temp_c:.1f}",
                "unit": "C",
                "type": "temperature",
            })
        return sensors
    except Exception:
        return []


def get_sensors(config):
    method_cfg = config.get("sensors", {}).get("method", "auto")
    method = _detect_method(method_cfg)

    if method == "disabled":
        return {"method": "disabled", "sensors": []}

    ipmitool_path = config.get("sensors", {}).get("ipmitool_path", "ipmitool")

    if method == "ipmitool":
        sensors = _read_ipmitool(ipmitool_path)
    elif method == "sd5":
        sensors = _read_sd5()
    elif method == "wmi":
        sensors = _read_wmi()
    else:
        sensors = []

    return {"method": method, "sensors": sensors}
