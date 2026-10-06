# iroha-soulgem

Windows 服务器实时监控面板。通过 Web 界面展示硬件传感器、系统性能、服务状态和自定义命令输出，同时提供 LLM/Agent 友好的 JSON API。

## 功能

- **硬件传感器** — 通过 SuperDoctor 5 自动采集 CPU 温度、风扇转速、电压等（支持 SD5 `sdc.bat` / ipmitool / WMI 自动探测）
- **系统性能** — CPU 使用率（含每核心）、内存、磁盘、网络（基于 psutil）
- **GPU 监控** — 温度、利用率、显存、功耗（基于 nvidia-ml-py，轻量无中断）
- **风扇控制** — Supermicro BMC 风扇调速，支持温控曲线 / 手动 PWM / 全速 / BIOS 还原四种模式
- **服务状态** — 指定 Windows 服务的运行状态
- **命令输出** — 定时执行任意命令并展示输出（支持 cmd / PowerShell）
- **实时推送** — SSE (Server-Sent Events) 推送，网页自动刷新
- **LLM 友好** — `/api/status` 返回纯 JSON，可直接 `curl` 或供 Agent 读取
- **运行时调频** — 网页右上角滑条可实时调整刷新间隔（100ms ~ 2000ms）
- **Windows 服务** — 提供 NSSM 安装脚本，支持开机自启、日志轮转

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

## 注册为 Windows 服务

需要 [NSSM](https://nssm.cc)（`choco install nssm`）：

```bash
tools\install_service.bat    # 安装并启动服务
tools\uninstall_service.bat  # 停止并卸载服务
```

服务管理：

```bash
nssm start SoulGemMonitor     # 启动
nssm stop SoulGemMonitor      # 停止
nssm restart SoulGemMonitor   # 重启
nssm edit SoulGemMonitor      # GUI 编辑配置
```

日志位于 `logs\service.log`（自动轮转，最大 10MB）。

## 项目结构

```
├── main.py                  # FastAPI 入口，路由与 SSE 推送
├── config.yaml              # 配置文件（服务、命令、刷新间隔等）
├── requirements.txt         # Python 依赖
├── collectors/
│   ├── system.py            # CPU / 内存 / 磁盘 / 网络（psutil）
│   ├── gpu.py               # GPU 指标（nvidia-ml-py）
│   ├── sensor.py            # 硬件传感器（SD5 sdc.bat / ipmitool / WMI）
│   ├── fanctl.py            # BMC 风扇温控守护
│   ├── services.py          # Windows 服务状态
│   └── command.py           # 定时命令执行器
├── static/
│   └── index.html           # 暗色主题仪表盘（单文件，嵌入 CSS/JS）
└── tools/
    ├── install_service.bat  # NSSM 服务安装脚本
    └── uninstall_service.bat
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

fanctl:
  enabled: true          # 设为 false 可禁用风扇控制
  bmc_host: "192.168.1.123"
  bmc_user: "ADMIN"
  bmc_password: "ADMIN"
  interval: 3            # 温控循环间隔（秒）
  zones: [0, 1, 2, 3]    # 风扇区域
  curve:
    normal_pwm: 18       # 日常 PWM%，约 3000 RPM
    cpu_ramp_start: 75   # CPU 超过此温度开始提高转速（°C）
    cpu_full_speed: 88   # CPU 超过此温度风扇满速（°C）
    gpu_ramp_start: 81   # GPU 超过此温度开始介入（°C）
    gpu_full_speed: 87   # GPU 超过此温度风扇满速（°C）

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
| `/api/status` | GET | 当前状态快照（JSON），含 `fanctl` 字段 |
| `/api/stream` | GET | SSE 实时推送流 |
| `/api/interval` | POST | 修改刷新间隔，body: `{"interval": 1000}`（ms） |
| `/api/fanctl` | GET | 风扇控制状态（当前模式、PWM、转速、温度） |
| `/api/fanctl/mode` | POST | 切换模式，body: `{"mode": "curve|manual|full|bios", "password": "控制密码"}` |
| `/api/fanctl/pwm` | POST | 手动 PWM（仅 manual 模式），body: `{"zones": {"0": 18, "1": 18}, "password": "控制密码"}` |

## 风扇控制

Dashboard 内置 Supermicro BMC 风扇温控。BMC 通过 pyghmi（纯 Python IPMI 库）直接操控，无需安装 ipmitool CLI。

### 四种模式

| 模式 | 行为 | 过热保护 |
|------|------|----------|
| **Curve** | 温度驱动，日常 ~18% PWM（约 3000 RPM）。CPU > 75°C 线性爬升，> 88°C 满速 | 有 |
| **Manual** | 固定各 Zone PWM（0-100%），网页滑块实时调节 | 无 |
| **Full** | 全速 100% PWM | — |
| **BIOS** | 还原 BMC Optimal 模式，由 BMC 自行管理 | BMC 接管 |

### 设置风扇控制密码

修改模式和 PWM 必须通过服务端密码验证；监控页面、JSON 状态和 SSE 仍可直接读取。后台自动温控不受这项验证影响。

在仓库目录执行以下命令，输入两次至少 12 字符的独立控制密码，然后重启服务：

```powershell
python tools/set_fan_password.py
nssm restart SoulGemMonitor
```

密码输入不回显，支持中文；设置工具只保存随机盐和 PBKDF2-SHA256 哈希（600,000 次迭代），文件 `fanctl-password.json` 已加入 `.gitignore`，不会存入 `config.yaml` 或复用 BMC 密码。未设置、文件损坏或无法读取时，写接口返回 HTTP 503 并保持锁定；缺少或错误密码返回 HTTP 401。

网页首次修改时弹出密码框，只在当前页面内存中暂存密码，不写入浏览器存储；刷新页面或密码错误后需重新输入。取消输入不会修改设置。重置密码时再次运行同一命令并重启服务。

Agent 调用写接口时，将 `password` 放在 JSON body 中，不要放在 URL 参数中。当前 HTTP 入口不会加密传输密码，公网使用应配合 HTTPS。

### 工作原理

fanctl 模块在后台线程中运行，每 3 秒执行一次循环：

1. 通过 pyghmi 读取 BMC 温度传感器（CPU）
2. 通过 pynvml 读取 GPU 温度
3. 计算目标 PWM：正常情况下维持 `normal_pwm`，超过 `ramp_start` 后线性提升，达到 `full_speed` 时满速
4. 通过 IPMI raw command（`0x30 0x70 0x66`）写入各 Zone PWM

GPU 温控阈值由 `config.yaml` 决定，当前为 81°C 开始介入、87°C 满速；应按实际显卡和机箱散热条件调整。

Curve 模式下，CPU 温度缺失、已识别的 CPU/GPU 消失或温度读取失败时，会请求所有 Zone 使用 100% PWM，并通过 API 的 `fanctl.failsafe`、`fanctl.error` 和面板显示故障。有效温度恢复后自动恢复曲线控制。没有 NVIDIA 驱动的纯 CPU 服务器不因缺少 GPU 触发此保护；Manual 和 BIOS 模式仍遵循所选模式。

BMC 写入失败会显示错误并在后续控制循环重试；`target_pwm` 表示目标值，实际读回值见 `current_pwm`，不能把目标值当作已经写入成功。手动 PWM 接口按 Zone 合并更新，未提交的 Zone 保留原设定，未配置的 Zone 返回 HTTP 400。

阻塞的硬件采集在工作线程运行，命令采集与其并行，避免阻塞网页和 API。刷新滑条控制循环等待和 SSE 推送间隔；完整快照的更新速度仍取决于采集耗时。

### 禁用风扇控制

在 `config.yaml` 中将 `fanctl.enabled` 设为 `false`，dashboard 启动时不会初始化 pyghmi 连接，也不会写入 PWM。

## 依赖

- Python 3.10+
- fastapi, uvicorn, psutil, nvidia-ml-py, pyyaml, pyghmi

## 回归检查

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

测试模拟硬件和 BMC，不启动服务或操作实际风扇。

安装 Node.js 后可检查网页密码交互和渲染：

```bash
node tests/test_dashboard.js
```
