# Claude / Codex 本地备份与 Windows 迁移包

v0.5.0 新增 `bin/migrate.mjs` / `scripts/migrate.py`。它只读取源资料，生成新的备份与数据包；没有导入、还原到应用、账号清理、thinking 转换或上传功能。Python 3.9+，仅使用标准库；加密备份另外需要支持 PBKDF2 的 OpenSSL。原有维护命令保持独立，旧计划/备份协议仍为 v0.4.1，避免破坏旧备份检查与恢复。

## 两种产物

- `mac-preservation.zip.enc`：完整选定应用资料和项目资料的保全备份，含凭据、旧索引和配置。AES-256-CBC、PBKDF2 200000 次，独立 HMAC-SHA256；另有 `backup-auth.json`。随机恢复密钥单独保存在 Mac 指定路径，不加入便携包。
- `windows-portable.zip`：会话正文、压缩日志、分页历史数据库、附件、记忆、技能及选定项目资料。保留现有 thinking 原字节，不重建账号身份。明确的登录文件、可执行配置、旧远程状态不导入此包；Claude 桌面会话索引仅导出安全字段目录，原索引保留在加密备份中。

历史正文和项目文件可能已包含私密信息或过去输出的密钥。凭据文件过滤不代表历史已脱敏。请按私有备份保管便携包，不上传公共仓库。

历史数据库和项目文件可能仍含旧权限等元数据；本版只保存资料，不自动启用任何旧配置、授权或远程连接。未来导入时需单独审阅并重置这些状态。

## 计划与捕获

```sh
node bin/migrate.mjs plan --include-projects --out /PRIVATE/backup.plan.json
node bin/migrate.mjs capture --plan /PRIVATE/backup.plan.json \
  --out /PRIVATE/run-NEW --key-file /SEPARATE/recovery-NEW.key
```

计划列出默认及相邻 Claude profile、Claude Code、Codex 用户目录和 Desktop 目录，也发现 CODEX_HOME / CLAUDE_CONFIG_DIR。只依据已记录 cwd/project roots/Claude 本地索引定位相关项目，不读取或执行历史指令。`--include-projects` 备份这些实际工作目录，保留 `.git`、未提交及未跟踪资料；不递归复制任意用户主目录。项目内 node_modules、虚拟环境及若干可重建缓存不纳入，清单逐项记录。外部符号链接只记录链接本身，不跟随或执行。源路径缺失单列，不虚构恢复。

源和输出目录必须分离。若源项目是当前交付目录，可用重复 `--exclude /ABSOLUTE/OUTPUT` 明确排除新生成物；排除清单也进入包。新计划、输出和密钥不覆盖已有文件。计划哈希用于发现意外篡改，不是签名或来自第三方计划的授权；只执行自己生成并审阅的计划。

默认捕获要求能检查进程且 Claude/Codex 已退出。若明确需要在线备份，传 `--live`：每个文件先稳定复制、每个 SQLite 通过只读连接调用 backup API 并 quick_check，WAL 已提交内容纳入数据库快照，不再次应用原 WAL。它是按文件/数据库分别取样的在线快照，**不是跨应用的同一时点事务快照**。过程新增、修改、消失的源条目会记录；不可稳定读取的文件会使捕获失败。运行中源程序自身产生的变化不等于工具写源数据。

在线模式产物有 `needsColdFinalBackup: true`。在最终离开 Mac 前，等任务结束并退出应用，从普通终端用新计划、不带 `--live` 再做一次最终备份。不自动 kill、不暂停用户任务、不绕过原维护命令的进程保护。Keychain 不导出；App 的二进制缓存与 VM 可以在保全备份中保存，但不能保证跨系统执行。

Chromium 等独占库先取得稳定的主库/WAL/journal 私有文件集，再只在副本上执行恢复和 backup。没有 WAL/journal 的源库先稳定复制主文件，不在源目录创建旁文件。活动文本先读取稳定字节再压缩。无法得到稳定数据时停止，不把部分归档当作成功。

失败批次有精确 `lastAttempt`，且 ZIP 中没有正在写到一半的对象时，可在全新输出目录通过 `--live --resume-from /PRIVATE/FAILED-RUN` 续接。计划摘要必须完全匹配；复用对象会重新读取校验，再追加未完成范围。旧批次不覆盖。时间范围沿用最早快照，仍明确属于在线备份，不冒充冷备份。

## 校验与 Windows 携带

每份归档都含 manifest、逐文件 SHA-256、模式/时间/原路径及会话目录。数据使用编号对象保存，避免 Windows 不合法文件名、大小写冲突、路径长度和符号链接被错误展开。没有自动把编号对象写回应用的功能。

创建时回读并验证两份归档的全部数据对象；保全备份加密后实际解密流式回读，与原 ZIP 哈希比较。成功后仅删除工具自己的临时明文 ZIP。最终 SHA256SUMS 记录交付文件哈希。失败保留 journal 和已有产物，不冒充完成；失败目录可能含未加密 `.part`，仍按私有数据保管。

带到 Windows：`windows-portable.zip`、`migrate.py`、`VERIFY-WINDOWS.cmd`、`README-WINDOWS.txt`、`SHA256SUMS`。安装 Python 3.9+ 后双击验证脚本，或执行：

```powershell
py -3 -B migrate.py verify --archive windows-portable.zip
py -3 -B migrate.py inspect --archive windows-portable.zip
```

验证只读归档，不解压数据对象、不修改应用。`session-catalog.json` 保留 Desktop/CLI ID 对应和各副本来源，不按大小合并不同正文。后续导入须根据目标原生 Windows/WSL、路径映射、账号/组织、模型供应商、重复/分叉和目标已有资料另行执行；本版不宣称 Windows 原生列表或真实模型续聊通过。

## 回退

Mac 源资料不变，因此无需对源执行回滚。需要检查保全备份时：

```sh
python3 -B scripts/migrate.py decrypt --archive /PRIVATE/run/mac-preservation.zip.enc \
  --auth /PRIVATE/run/backup-auth.json --key-file /SEPARATE/recovery.key \
  --out /PRIVATE/NEW-decoded.zip
```

先验证 HMAC，再解密到全新文件并校验归档，不向任何 App 目录写入。恢复密钥丢失将无法解密。真正还原、删除备份或迁入 Windows 都不属于该命令。

## 验证范围

合成测试覆盖 WAL 中已提交数据、源字节/thinking 保持、秘密文件排除、加密/解密回读与错误密钥/校验失败、目标冲突、路径穿越、符号链接、运行中拒绝冷备份、过期计划。Windows 运行、浏览器和真实模型请求尚未验证。
