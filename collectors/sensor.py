import shutil
import subprocess
import re


def _detect_method(config_method):
    if config_method and config_method != "auto":
        return config_method

    if shutil.which("ipmitool"):
        return "ipmitool"

    sd5_path = r"C:\Program Files\Supermicro\SuperDoctor5\SD5CLI.exe"
    import os
    if os.path.isfile(sd5_path):
        return "sd5"

    return "wmi"


def _read_ipmitool(ipmitool_path):
    try:
        result = subprocess.run(
            [ipmitool_path, "sdr", "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []

        sensors = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                name = parts[0]
                value = parts[1]
                unit = parts[2] if len(parts) > 2 else ""

                sensor_type = "other"
                lower = name.lower()
                if "temp" in lower:
                    sensor_type = "temperature"
                elif "fan" in lower:
                    sensor_type = "fan"
                elif "volt" in lower:
                    sensor_type = "voltage"
                elif "power" in lower:
                    sensor_type = "power"

                sensors.append({
                    "name": name,
                    "value": value,
                    "unit": unit,
                    "type": sensor_type,
                })
        return sensors
    except Exception:
        return []


def _read_sd5():
    sd5_path = r"C:\Program Files\Supermicro\SuperDoctor5\SD5CLI.exe"
    try:
        result = subprocess.run(
            [sd5_path, "-s", "all"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []

        sensors = []
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                sensors.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "unit": "",
                    "type": "other",
                })
        return sensors
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
