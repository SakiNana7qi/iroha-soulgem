import psutil


def get_cpu():
    try:
        freq = psutil.cpu_freq()
        return {
            "usage_percent": psutil.cpu_percent(interval=0),
            "cores_logical": psutil.cpu_count(logical=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "per_core": psutil.cpu_percent(interval=0, percpu=True),
            "freq_current_mhz": round(freq.current, 0) if freq else None,
            "freq_max_mhz": round(freq.max, 0) if freq else None,
        }
    except Exception as e:
        return {"error": str(e)}


def get_memory():
    try:
        v = psutil.virtual_memory()
        return {
            "total_gb": round(v.total / (1024**3), 2),
            "used_gb": round(v.used / (1024**3), 2),
            "available_gb": round(v.available / (1024**3), 2),
            "percent": v.percent,
        }
    except Exception as e:
        return {"error": str(e)}


def get_network():
    try:
        io = psutil.net_io_counters(pernic=True)
        stats = {}
        for name, counters in io.items():
            stats[name] = {
                "bytes_sent": counters.bytes_sent,
                "bytes_recv": counters.bytes_recv,
                "packets_sent": counters.packets_sent,
                "packets_recv": counters.packets_recv,
                "errin": counters.errin,
                "errout": counters.errout,
            }
        return stats
    except Exception as e:
        return {"error": str(e)}


def get_disk():
    try:
        partitions = psutil.disk_partitions()
        result = []
        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
                result.append({
                    "device": p.device,
                    "mountpoint": p.mountpoint,
                    "fstype": p.fstype,
                    "total_gb": round(usage.total / (1024**3), 2),
                    "used_gb": round(usage.used / (1024**3), 2),
                    "free_gb": round(usage.free / (1024**3), 2),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue
        return result
    except Exception as e:
        return {"error": str(e)}
