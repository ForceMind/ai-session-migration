# Windows 原生会话恢复向导

向导把 Mac 导出的会话 ZIP 恢复到 Windows 上已初始化的 Codex 和 DeepSeek Harness。准备好 Windows 上的项目目录，并选择有足够空间的本地恢复目录。每批目标备份、冲突检查和进度记录都保存在该目录。

## 启动向导

在源码目录运行 `tools\windows-restore.cmd`。未收到命令行参数时，向导会询问 ZIP、恢复目录和两个 Windows 项目根目录。项目使用 Python 3.12 和 `requirements.txt`。已有安装时可直接使用 `ai-session-migration-windows-restore` 命令。

窗口及应用包查询优先调用已安装的 PowerShell 7。

如果本机还没有项目环境，在源码目录运行以下命令。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

先预览再恢复时，在 PowerShell 中运行以下命令，并将路径替换为本机实际位置。

```powershell
.\tools\windows-restore.cmd preview --archive "D:\Backups\sessions.zip" --project-roots "E:\Prive" "E:\Work" --run-dir "D:\Restore\sessions-run"
.\tools\windows-restore.cmd guide --run-dir "D:\Restore\sessions-run"
```

向导按 Mac 项目根的相对路径匹配 Windows 上已存在的目录。候选或缺失路径写入 `pending-paths.json`。确认了别名或工作树路径后，可以在新的恢复目录用 `--mapping-file` 提供 Mac 路径到 Windows 路径的 JSON 对象。目标仍须位于指定的 Windows 项目根目录下。相同 ZIP 和映射继续使用原恢复目录。新 ZIP 或变更后的映射使用独立目录。

## 恢复与验收

确认预览后，向导先用少量样本在隔离副本里验证。保存手头工作并退出要写入的 DSH 客户端。向导会正常退出 Codex，发布样本，分别用原生客户端回读，然后分批恢复。一个客户端内部按父子依赖顺序写入，独立客户端并行处理。终端显示校验、备份、写入和回读的实际进度。

每批先校验目标副本和可读的回滚备份，再发布到正式库。同 ID 记录保持本机已有版本。归属到 Windows 的工作目录和 Codex 的本地项目登记一起回查。向导完成后重新打开 Codex。请保留恢复窗口和 `progress.json` 到验收结束。

使用 Windows 默认的 `.codex` 或 `.dsh` 目录时，事务结束并核验后会把留存的回退目录和暂存目录归档到 `%USERPROFILE%\AppData\Local\AI-Session-Migration\restore-residue`。同一批次仍使用原导入回执回退，回退引擎会从归档读取保全副本。处理中断时，从回执检查发布阶段和保留位置。

Claude 历史在图形客户端只读查看。对于旧版 Claude 恢复任务，可查阅已有进度和备份。新版停止原生导入、侧栏登记和旧计划续写。

DSH 会话恢复包含 v4 日志、关联附件和工作区索引。渠道和第三方插件属于独立资料，迁移目标需要另行确认配置、兼容版本和本机认证方式。

```powershell
.\tools\windows-restore.cmd status --run-dir "D:\Restore\sessions-run"
.\tools\windows-restore.cmd audit --run-dir "D:\Restore\sessions-run"
```

`account-and-dsh-audit.json` 记录会话范围和本机配置的哈希复核结果。审计也支持旧版向导留下的 `progress.json`，运行时通过 `--preview-file` 指向它原有的预览文件。

需要回退单批时，退出对应客户端，从本次恢复目录的 `backups` 中选择对应的 `导入结果.json`，运行 `tools\windows-restore.cmd rollback --run-dir <恢复目录> --journal <导入回执>`。引擎先比对目标与该批回执。如果后续会话或项目登记已改变目标资料，回退会停在检查阶段并保留备份。
