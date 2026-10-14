# git-sync

Git 同步面板。网页可触发已配置项目的同步并查看日志；项目配置见 [`config.example.json`](config.example.json)。

## 部署

1. 将 `config.example.json` 复制到服务器 `/data/git-sync/config.json`，填写允许访问的 IP 和项目。
2. 准备 `/data/git-sync/state`、`/data/workspaces`，并确保容器用户 `10001` 可写。
3. 设置 SSH 环境变量 `GIT_PRIVATE_KEY`、`GIT_KNOWN_HOSTS`，使用已核验的主机密钥。
4. 使用 `docker-compose.yml` 启动。面板监听容器内 `8080`，通过内网代理访问。

也可在本地使用 `docker-compose.local.yml`，将服务映射到 `localhost:8080`。

## 本地开发

需要 Python 3.11+ 和 Git：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:APP_CONFIG = (Resolve-Path .\config.example.json).Path
\.venv\Scripts\python.exe -m flask --app wsgi run --host 127.0.0.1 --port 8080
```
