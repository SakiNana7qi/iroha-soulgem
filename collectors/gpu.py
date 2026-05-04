try:
    import pynvml

    _nvml_available = True
except ImportError:
    _nvml_available = False

_initialized = False


def _init():
    global _initialized
    if _initialized:
        return True
    if not _nvml_available:
        return False
    try:
        pynvml.nvmlInit()
        _initialized = True
        return True
    except Exception:
        return False


def get_gpu():
    if not _init():
        return None

    try:
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name_raw = pynvml.nvmlDeviceGetName(handle)
            name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else name_raw

            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

            try:
                power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
            except Exception:
                power = None

            gpus.append({
                "index": i,
                "name": name,
                "temperature_c": temp,
                "gpu_utilization_percent": util.gpu,
                "memory_utilization_percent": util.memory,
                "memory_total_mb": round(mem.total / (1024**2), 0),
                "memory_used_mb": round(mem.used / (1024**2), 0),
                "memory_free_mb": round(mem.free / (1024**2), 0),
                "power_w": round(power, 1) if power else None,
            })
        return gpus if gpus else None
    except Exception:
        return None
