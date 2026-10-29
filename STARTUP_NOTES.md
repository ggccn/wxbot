# 启动方式备忘

## 手动启动（默认）

双击一个 `.cmd` / 快捷方式即可，例如：

```bat
@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe run_v4.py
```

启动后面板在 http://127.0.0.1:8765 ，微信里给机器人发消息就有反应。

## 守护（可选）

`start_bridge.ps1` → `start_bridge_core.ps1`：

- 用命名互斥体保证只有一份 wrapper 在跑；
- 桥或面板掉了才重新拉起；
- 适合挂到「任务计划程序」里定时跑（例如每 5 分钟一次）。

```powershell
schtasks /create /tn wxbot-watchdog /sc minute /mo 5 ^
  /tr "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File E:\path\to\start_bridge.ps1"
```

## 停止

- 微信里发 `/停`：只静音，进程还在（消息照收、不回复）；
- 直接结束 `python.exe` 进程：彻底停。

## 开机不自动启动的原因

微信小号 + UI 自动化有封号风险，建议手动开、用完关，不要挂成后台常驻服务。
