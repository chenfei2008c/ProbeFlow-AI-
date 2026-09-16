# ProbeFlow

个人使用的中文 AI 深度访谈平台，依据 [V1.1 规格](AI_INTERVIEW_V1_SPEC.md) 开发。录音、转写及全部修订、报告和引用永久保存，允许主动删除和撤回。

**开发与验收进行中。** 模拟模式可用于软件流程测试，不能替代真实百炼接口、中文语音质量、真人 60 分钟和手机设备验收。实际进度见 [验收记录](docs/acceptance-results.md)。

## 本机运行

需要 Python 3.12+、uv、Node.js 22.12+ 和 FFmpeg（含 ffprobe）。

```bash
uv sync --frozen
cd frontend
npm ci
npm run build
cd ..
./scripts/probeflow init-admin
./scripts/probeflow start
```

打开 [http://localhost:8765](http://localhost:8765)，使用刚设置的管理员密码登录。按 `Ctrl+C` 停止。默认模式为 `mock`，不需要 API 密钥；模拟转写是固定样本，模拟音频只有提示音，不是真实普通话播报。

应用采用一个进程、SQLite 持久任务和普通 HTTP 轮询；请勿以多 Uvicorn worker 启动。后端直接托管构建后的前端，无需同时运行两个服务。

## 真实接口配置

将 `.env.example` 复制为 `.env`，在本机填写自己的百炼密钥，再将 `MODE` 改为 `live`。不要将密钥粘贴到聊天、提交仓库或放在前端配置。四种模型角色可分别设置 HTTPS 服务地址、模型、地区及密钥环境变量名称；实际账户及模型兼容性见 [供应商说明](docs/provider-compatibility.md)。

先运行 `./scripts/probeflow check`，再用虚构内容核验识别、下一问及普通话播放。接口成功不代表中文深访质量已经验收。计费样例按规格快照核算，费用账本明确区分实际用量、估算和未知；正式使用前核对供应商价格和账户预算。

## 文件与备份

macOS 默认数据目录为 `~/Library/Application Support/ProbeFlow`，备份目录为同级 `ProbeFlow-backups`，会话与研究删除清单为同级 `ProbeFlow-deletions.jsonl`、`ProbeFlow-study-deletions.jsonl`。可在本机 `.env` 配置仓库之外的 `DATA_DIR` 和 `BACKUP_DIR`。所有真实数据、密码、音频及备份均不进入 Git。

```bash
./scripts/probeflow backup
./scripts/probeflow restore /path/to/backup /path/to/empty-recovery-directory
```

恢复命令只向独立空目录写入，先应用最新删除清单再核验关联文件；确认恢复结果后再停止服务并调整 `DATA_DIR`。迁移时需要同时保留两份最新删除清单。默认分别保留最近 7 个日备份和 7 个手动备份，均为完整副本；手动备份不挤掉日备份。操作前阅读 [保存与恢复说明](docs/privacy-and-retention.md) 和 [存储增长估算](docs/storage-capacity.md)。

## 开发验证

```bash
uv run pytest
uv run ruff check backend
cd frontend
npm test
npm run build
```

依赖版本由 `uv.lock` 和 `frontend/package-lock.json` 固定。当前本机：Python 3.12.13、Node.js 24.18.0、npm 12.0.1；实际命令输出持续登记在验收记录中。

远程访问按 [HTTPS 部署与验收说明](docs/deployment-https.md)配置；应用默认只监听本机，代理转发头仅信任显式指定的 IP。手机通过局域网 HTTP 不能视为受支持的麦克风环境；公网部署与真实参与者邀请在相应验收完成后再使用。
