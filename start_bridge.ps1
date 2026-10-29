# 计划任务/快捷方式指向的入口。
# 真正的守护逻辑在 start_bridge_core.ps1：单实例锁 + 缺进程才拉起 + 面板兜底。
# 想让它开机自启：把计划任务指向本文件即可（默认不要自动启动，见 README）。
& (Join-Path $PSScriptRoot 'start_bridge_core.ps1')
