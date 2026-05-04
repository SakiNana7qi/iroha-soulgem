import psutil


def get_services(service_list):
    result = []
    for svc in service_list:
        name = svc.get("name", "")
        display = svc.get("display", name)
        try:
            s = psutil.win_service_get(name)
            info = s.as_dict()
            result.append({
                "name": name,
                "display": display,
                "status": info.get("status", "unknown"),
            })
        except psutil.NoSuchProcess:
            result.append({
                "name": name,
                "display": display,
                "status": "not_found",
            })
        except Exception as e:
            result.append({
                "name": name,
                "display": display,
                "status": "error",
                "error": str(e),
            })
    return result
