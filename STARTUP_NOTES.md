# 启动与自启（2026-10-27 改版）

## 现在怎么启动
双击桌面「大肥鱼」快捷方式（`C:\Users\ggcfn\Desktop\大肥鱼.lnk`，图标 `dafeiyu.ico`）。

链路：`wscript.exe start_dafeiyu.vbs` → 微信没开就先起 `E:\wx\Weixin\Weixin.exe` → 等 20 秒
→ `wscript.exe start_bridge_hidden.vbs` → `powershell -WindowStyle Hidden -File start_bridge_core.ps1`
→ 起 panel.py(8765) + run_v4.py，最后用默认浏览器打开 http://127.0.0.1:8765 。

## 现在开机什么都不启动
- 计划任务 `\DSH-WeChatBridge`（登录触发）已删除（需要管理员权限那次删除已完成）。
- 计划任务 `\DSH-WeChatBridge-Watchdog` 保留但**触发器已清空**，只剩手动运行：
  `schtasks /run /tn DSH-WeChatBridge-Watchdog`（动作已改成 wscript 调 VBS，不会再闪黑窗）。
- HKCU\...\Run 里的 `Weixin = "E:\wx\Weixin\Weixin.exe" -autorun` 已删除，
  备份在 `data\_backup_weixin_autorun.json`。
- 启动文件夹、其它 Run 键里没有本系统的条目。

## 文件说明
- `start_bridge.ps1` —— 现在只是个占位（`exit 0`），防止有残留引用一跑就起桥。
- `start_bridge_core.ps1` —— 真正的守护循环（互斥体单实例 + panel + run_v4 + 崩溃 60 秒重试）。
- `start_bridge_hidden.vbs` —— 用 wscript 隐藏启动 core，避免 Windows Terminal 闪窗。
- `start_dafeiyu.vbs` —— 一键启动（微信 → 桥 → 面板 → 开浏览器）。

## 为什么不能用 powershell 直接起
Win11 默认终端是 Windows Terminal，`-WindowStyle Hidden` 对它无效，每次调用都会闪一个黑窗。
必须经过 `wscript.exe` + `sh.Run(..., 0, False)` 才是真隐藏。

## 恢复微信自启（如果需要）
```
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name Weixin -Value '"E:\wx\Weixin\Weixin.exe" -autorun'
```
