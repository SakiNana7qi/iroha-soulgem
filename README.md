# iroha-soulgem

Windows 服务器实时监控面板。通过 Web 界面展示硬件传感器、系统性能、服务状态和自定义命令输出，同时提供 LLM/Agent 友好的 JSON API。

## 功能

- **硬件传感器** — 通过 SuperDoctor 5 自动采集 CPU 温度、风扇转速、电压等（支持 SD5 / ipmitool / WMI 自动探测）
- **系统性能** — CPU 使用率（含每核心）、内存、磁盘、网络（基于 psutil）
- **GPU 监控** — 温度、利用率、显存、功耗（基于 pynvml，轻量无中断）
- **服务状态** — 指定 Windows 服务的运行状态
- **命令输出** — 定时执行任意命令并展示输出（支持 PowerShell）
- **实时推送** — SSE (Server-Sent Events) 每秒推送，网页自动刷新
- **LLM 友好** — `/api/status` 返回纯 JSON，可直接 `curl` 或供 Agent 读取
- **运行时调频** — 网页右上角滑条可实时调整刷新间隔（100ms ~ 2000ms）

## 快速开始

```bash
pip install -r requirements.txt
python main.py
```

打开浏览器访问 `http://localhost:8080`。

LLM/Agent 获取数据：

```bash
curl http://localhost:8080/api/status
```

## 项目结构

```
├── main.py              # FastAPI 入口，路由与 SSE 推送
├── config.yaml          # 配置文件（服务、命令、刷新间隔等）
├── requirements.txt     # Python 依赖
├── collectors/
│   ├── system.py        # CPU / 内存 / 磁盘 / 网络（psutil）
│   ├── gpu.py           # GPU 指标（pynvml）
│   ├── sensor.py        # 硬件传感器（SD5 sdc.bat / ipmitool / WMI）
│   ├── services.py      # Windows 服务状态
│   └── command.py       # 定时命令执行器
└── static/
    └── index.html       # 暗色主题仪表盘（单文件，嵌入 CSS/JS）
```

## 配置说明

编辑 `config.yaml` 后重启服务生效（刷新间隔可通过网页实时调整，无需重启）。

```yaml
server:
  host: "0.0.0.0"
  port: 8080
  refresh_interval: 1    # 秒

sensors:
  method: "auto"         # auto | ipmitool | sd5 | wmi | disabled
  ipmitool_path: "ipmitool"

gpu:
  enabled: true          # 无 GPU 或不需要时设为 false

services:
  - name: "sshd"         # Windows 服务名
    display: "OpenSSH SSH Server"

commands:
  - name: "boot-time"
    cmd: "systeminfo | findstr /B /C:\"System Boot Time\""
    interval: 60         # 执行间隔（秒）
  - name: "top-processes"
    cmd: "powershell -Command \"Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 | Out-String\""
    interval: 10
```

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 仪表盘页面 |
| `/api/status` | GET | 当前状态快照（JSON） |
| `/api/stream` | GET | SSE 实时推送流 |
| `/api/interval` | POST | 修改刷新间隔，body: `{"interval": 1000}`（ms） |

## 依赖

- Python 3.10+
- fastapi, uvicorn, psutil, pynvml, pyyaml
