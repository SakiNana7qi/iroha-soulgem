import asyncio
import time


class CommandRunner:
    def __init__(self, command_configs):
        self._configs = command_configs
        self._cache = {}

    async def run_command(self, cmd_cfg):
        name = cmd_cfg.get("name", "unnamed")
        cmd = cmd_cfg.get("cmd", "")
        interval = cmd_cfg.get("interval", 10)

        now = time.time()
        cached = self._cache.get(name)
        if cached and (now - cached["last_run"]) < interval:
            return cached

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode("utf-8", errors="replace").strip()
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            output = "[timeout after 15s]"
            exit_code = -1
        except Exception as e:
            output = f"[error: {e}]"
            exit_code = -1

        result = {
            "name": name,
            "cmd": cmd,
            "output": output,
            "exit_code": exit_code,
            "last_run": now,
        }
        self._cache[name] = result
        return result

    async def get_all(self):
        if not self._configs:
            return []
        tasks = [self.run_command(c) for c in self._configs]
        return await asyncio.gather(*tasks)
