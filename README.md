# git-sync

Git 同步面板。网页可触发已配置项目的同步并查看日志；项目配置见 [`config.example.json`](config.example.json)。

## 部署

1. 将 `config.example.json` 复制到服务器 `/data/git-sync/config.json`，填写允许访问的 IP 和项目。
   `log_level` 控制任务日志的最低显示等级：`DEBUG` 显示阶段耗时、Git 命令耗时和拉取进度；`INFO` 显示常规同步信息；`WARNING` 仅显示警告和错误；`ERROR` 仅显示错误。默认值为 `INFO`。
2. 在 Docker 宿主机创建并授权持久化目录：

   ```sh
   sudo mkdir -p /data/git-sync/state /data/workspaces
   sudo chown -R 10001:10001 /data/git-sync/state /data/workspaces
   ```

3. 设置 SSH 环境变量 `GIT_PRIVATE_KEY`、`GIT_KNOWN_HOSTS`，使用已核验的主机密钥。
4. 使用 `docker-compose.yml` 启动，然后通过 `http://服务器IP:8188` 访问。确保服务器防火墙允许 `8188` 端口。

仅向可信网络开放面板，并在 `allowed_ips` 中限制可访问的客户端 IP；面板没有登录功能。

## 本地开发

需要 Python 3.11+ 和 Git：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:APP_CONFIG = (Resolve-Path .\config.json).Path
$env:WORKSPACE_DIR = Join-Path (Get-Location) "workspaces"
$env:STATE_DIR = Join-Path (Get-Location) "state"
New-Item -ItemType Directory -Force $env:WORKSPACE_DIR, $env:STATE_DIR | Out-Null
$env:GIT_PRIVATE_KEY = Get-Content .\private-key.txt -Raw
$env:GIT_KNOWN_HOSTS = Get-Content .\known-hosts.txt -Raw

python -m flask --app wsgi run --host 127.0.0.1 --port 8080
```
