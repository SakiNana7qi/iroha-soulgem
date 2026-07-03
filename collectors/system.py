import threading
import psutil

# ── background CPU sampler ─────────────────────────────────────────
# Continuously samples CPU in a daemon thread so get_cpu() always
# returns fresh data without blocking the main event loop.
# This also fixes the dual-socket Windows bug where non-blocking
# psutil.cpu_percent(interval=0) returns stale zeros for some cores.

_cpu_lock = threading.Lock()
_cpu_snapshot = None
_sampler_started = False


def _cpu_sampler():
    global _cpu_snapshot
    n_logical = psutil.cpu_count(logical=True)
    n_physical = psutil.cpu_count(logical=False)

    while True:
        try:
            per_core = psutil.cpu_percent(interval=0.5, percpu=True)
            usage = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
        except Exception:
            per_core = []
            usage = 0.0

        try:
            freq = psutil.cpu_freq()
            f_cur = round(freq.current, 0)
            f_max = round(freq.max, 0)
        except Exception:
            f_cur = None
            f_max = None

        with _cpu_lock:
            _cpu_snapshot = {
                "usage_percent": usage,
                "cores_logical": n_logical,
                "cores_physical": n_physical,
                "per_core": per_core,
                "freq_current_mhz": f_cur,
                "freq_max_mhz": f_max,
            }


_sampler_thread = None


def _ensure_sampler():
    global _sampler_thread, _sampler_started
    if _sampler_started:
        return
    _sampler_started = True
    _sampler_thread = threading.Thread(
        target=_cpu_sampler, daemon=True, name="cpu-sampler"
    )
    _sampler_thread.start()


# ── public collectors ──────────────────────────────────────────────

def get_cpu():
    _ensure_sampler()
    with _cpu_lock:
        if _cpu_snapshot is not None:
            return dict(_cpu_snapshot)

    # fallback: first call before sampler has any data
    try:
        per_core = psutil.cpu_percent(interval=0, percpu=True)
        usage = psutil.cpu_percent(interval=0)
    except Exception:
        per_core = []
        usage = 0.0
    try:
        freq = psutil.cpu_freq()
        f_cur = round(freq.current, 0) if freq else None
        f_max = round(freq.max, 0) if freq else None
    except Exception:
        f_cur = f_max = None
    return {
        "usage_percent": usage,
        "cores_logical": psutil.cpu_count(logical=True),
        "cores_physical": psutil.cpu_count(logical=False),
        "per_core": per_core,
        "freq_current_mhz": f_cur,
        "freq_max_mhz": f_max,
    }


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
