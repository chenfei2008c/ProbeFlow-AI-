# 远程 HTTPS 部署与验收

本说明采用同一台服务器上的 Caddy → 单进程 ProbeFlow，适合 V1 的两场并行上限。当前仓库完成本机模拟测试，**尚未在真实域名、证书和手机网络上完成本说明的验收**。以下配置示例不是已经上线的服务。

## 1. 准备运行目录

在一台可持续运行的主机安装 README 指定的 Python、uv、Node.js 和 FFmpeg；Caddy 从[官方安装说明](https://caddyserver.com/docs/install)安装。不要使用开发热更新服务接收正式访谈。

源码放在独立目录，正式数据及备份使用仓库外的持久目录。例如源码 `/opt/probeflow`，`DATA_DIR=/srv/probeflow/data`，`BACKUP_DIR=/srv/probeflow/backups`。这些仅是路径示例，应由运行账户拥有并限制其他账户读取。数据库、音频、删除清单、备份和本机 `.env` 均不进入 Git；反向代理不能把它们当作静态网站目录。

在源码目录完成 `uv sync --frozen`、前端 `npm ci` 和 `npm run build`；复制 `.env.example` 为本机 `.env`，由运行账户保存。配置示例：

```dotenv
MODE=mock
DATA_DIR=/srv/probeflow/data
BACKUP_DIR=/srv/probeflow/backups
PUBLIC_BASE_URL=https://interview.example.com
ALLOWED_ORIGINS=https://interview.example.com
RETENTION_POLICY=permanent
TEMP_RETENTION_HOURS=24
BACKUP_COUNT=7
MAX_ACTIVE_SESSIONS=2
```

把示例域名替换为自己控制的实际域名。使用单一站点根路径，不在地址中追加 `/app` 等子路径。首次运行 `./scripts/probeflow init-admin`，在终端设置管理员密码；不要把密码写进命令历史。真实 API 密钥只在本机配置，先保留 `MODE=mock` 完成部署验证。

## 2. 启动应用与代理

应用只监听本机回环地址：

```bash
./scripts/probeflow start --host 127.0.0.1 --port 8765 --trusted-proxy 127.0.0.1
```

`--trusted-proxy` 只信任指定 IP 的转发头，用于取得真实客户端地址并实施登录／邀请限速；默认不信任任何代理，可重复指定明确 IP，拒绝 `*`。Caddy 和应用均在同一主机，所以这里只信任 `127.0.0.1`。不要把 8765 端口对外开放，也不要配置多个 Uvicorn worker。转发头的信任范围依据 [Uvicorn 官方说明](https://www.uvicorn.org/deployment/)设置。

将 [Caddyfile 示例](../deploy/Caddyfile.example)复制到受控运行位置，修改域名。将域名 A／AAAA 记录指向主机，让入站 80／443 到达 Caddy，并确保其证书数据目录可持久写入；Caddy 会申请和续期公开证书，并把 HTTP 引导至 HTTPS。IPv6 记录只有在对应路径可达时才配置。[Caddy HTTPS 条件](https://caddyserver.com/docs/automatic-https)

示例只转发应用，不启用响应缓存或访问日志。其代理写法见 [Caddy 反向代理文档](https://caddyserver.com/docs/quick-starts/reverse-proxy)。在准备好的运行目录执行：

```bash
caddy validate --config Caddyfile --adapter caddyfile
caddy run --config Caddyfile --adapter caddyfile
```

配置检查与前台启动命令见 [Caddy 命令行文档](https://caddyserver.com/docs/command-line)。

正式运行时用主机的服务管理器维持 Caddy 和应用两个进程，设定固定工作目录和运行账户，并持久保存 `.env`、数据、备份及证书目录。机器休眠、进程停止期间不能接收访谈或执行每日备份。应用重启会按持久任务状态恢复，不应由进程管理器创建额外并行副本。

## 3. 用虚构资料逐项验收

1. 从另一台设备打开实际 HTTPS 域名，证书有效且无浏览器警告；确认 HTTP 会跳转 HTTPS，外部不能直连 8765。
2. 管理员登录后检查 Cookie 的 Secure、HttpOnly、SameSite=Strict；退出后旧凭证不可使用。确认伪造 Origin 的写请求被拒绝，受访者不能打开管理接口及别人的文本、音频、事件。
3. 创建单次邀请：链接使用当前 HTTPS 域名，兑换后地址栏片段消失。验证重复、过期、撤销邀请被拒绝；恢复邀请只恢复原场次且撤销旧访问凭证。
4. 在桌面 Chromium、Android Chrome、iPhone Safari 前台分别完成麦克风检查、录音、分块提交、确认及播放；记录操作系统和浏览器版本。局域网普通 HTTP 和模拟提示音不能替代这一项。
5. 覆盖断网 30 秒、刷新、后台切换、录音中断及服务重启；确认已保存档案与引用仍可读，未完整提交的音频有明确提示，未知计费不会自动重试。
6. 核查正式档案、临时文件、备份容量及备份结果；在隔离目录按[恢复说明](privacy-and-retention.md)恢复，应用最新删除清单后再开放访问。确认空间不足停止新录音，已有档案仍可导出。

部署验收使用虚构数据。实际研究仍需完成规格第 18.2 节的真实百炼调用、真人试访、长会话及质量判断，并将结果写入[验收记录](acceptance-results.md)。更改 `MODE`、模型或服务地址后，先取得更新后的处理同意；已完成场次可通过恢复邀请补充授权，不能因切换配置把历史模拟结果标为真实。

## 4. 更新、停机与恢复

更新前停止接收新场次，让已进行访谈暂停并确认上传状态。先做一致性备份并核对结果，再停止应用、更新代码和锁定依赖、构建前端并启动。启动会运行数据库迁移；保留旧备份及最新的会话／研究删除清单，不直接把新数据库交给旧版代码降级使用。

服务异常时先暂停新邀请并检查诊断、磁盘及进程。旧档案不能作为腾空间手段；扩容或迁移应连同数据库、正式音频和最新删除清单完成，步骤见[存储说明](storage-capacity.md)。Caddy 配置检查成功只证明配置可解析，不能替代证书、网络、权限、录放音及恢复验收。
