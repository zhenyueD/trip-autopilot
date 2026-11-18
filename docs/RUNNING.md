# 运行与复验

本说明面向本版源码发布；没有替你部署或启动付费旅行任务。当前用户入口是 `/studio.html`。解密口令由作者单独提供；API key 不在仓库内。

## 环境与授权解密

本轮实际使用 macOS / Python 3.13.12 / Node.js。Python 依赖锁在 `requirements-interview.lock`；Linux/Windows 未在本轮实装验证。需要 OpenSSL、Poppler（pdfinfo/pdfimages）和 Chromium；临时 HTTPS 另需 cloudflared。macOS 可用 Homebrew 安装 `python@3.13 node poppler cloudflared`。

从干净克隆开始。不要混用 core 下旧包：

```sh
git clone https://github.com/zhenyueD/trip-autopilot.git
cd trip-autopilot
# OpenSSL 交互询问口令，不在命令行参数中放口令。
openssl enc -d -aes-256-cbc -pbkdf2 \
  -in core/runtime-reliability-20261118.tar.gz.enc -out core.tar.gz
tar -xzf core.tar.gz
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-interview.lock
python -m playwright install chromium
```

解密得到 `packages/` 与 `tests/`，787 文件的 SHA256 在 `core/runtime-source-manifest.json`。解密归档与后端明文均保持本机，不提交。当前公开前端与工具均可直接阅读；这不是完整开源许可。

## 配置

```sh
cp .env.example .env
chmod 600 .env
# 在本机编辑 .env，填写 DEEPSEEK_API_KEY。
set -a
. ./.env
set +a
```

主路径当前使用 DeepSeek 官方通道与 `deepseek-v4-pro`，不是任意模型名称即可无差别替换。可选的 Jina、Pexels、Unsplash 凭据按需配置；公开来源也受可达性与许可限制。启动器不会自行读取 .env。

提交真实生成/修订会消耗模型、检索或媒体请求。请求计数不是人民币消费硬限额；应用不会替你设置供应商账户预算。正式实验请单独记录总费用、请求、输入/输出与实际等待，未知费用不记零。

## 本机隔离入口

选择空闲端口和仓库外数据目录。两个终端使用同一个虚拟环境与以下环境变量，分别启动：

```sh
export TRIP_DEMO_STATE="$HOME/.local/share/trip-autopilot/local-preview"
export TRAVEL_RENDER_BRIDGE="$TRIP_DEMO_STATE/render-bridge"
export TRAVEL_RENDER_OUTPUT="$TRIP_DEMO_STATE/render-captures"
export TRAVEL_PDF_PYTHON="$PWD/.venv/bin/python"

# 终端 A：渲染服务
python tools/render_service.py 7401

# 终端 B：同样先激活虚拟环境、加载 .env 并设置上面四个变量
python tools/serve_single_session.py \
  --directory "$TRIP_DEMO_STATE/sessions" --port 7400 --static-root apps/v1 \
  --max-requests 50 --turn-seconds 3600
```

打开 `http://127.0.0.1:7400/studio.html`。这条路径只绑定 loopback，没有公网认证网关。`/api/agent/capabilities` 是能力声明，planner_available 只反映 key 是否存在，不能当作真实 provider 联通验证。此次打包复验只启动空数据服务、GET 页面及健康检查，不提交模型任务。

数据目录保留会话、来源、账本与正式版本。刷新恢复依赖同一浏览器来源和有效写凭据；服务端有文件不代表丢失客户端凭据后即可继续写。不要同时运行多个写服务共享一个数据目录。

## 临时 HTTPS 验收

需要外部访问时，使用既有受口令保护的隔离启动器：

```sh
python tools/start_customer_preview.py \
  --state-dir "$HOME/.local/share/trip-autopilot/https-preview" \
  --app-port 7410 --gateway-port 7411 --render-port 7412
```

它设置独立的渲染队列与输出目录，输出临时 HTTPS 地址，口令写入该 state-dir 的 `access.json`，由运行者私下转交。主机与网络必须保持在线，重启可能改变域名。网站登录 Cookie 和行程读写凭据是不同凭据；新域名不保证继承旧浏览器存储。7377/7391/7392 是启动器保留端口。

本次源码同步没有重开隧道、更新已有公网进程或解决公网实测验收；旧临时地址不作为此版本入口。

## 零产品调用的发布回归

```sh
export PYTHONPATH=.:packages:tests
export PYTHONDONTWRITEBYTECODE=1
python -m pytest -q $(cat docs/release-test-files.txt)
python -m pytest -q tests/test_render_service_startup.py \
  tests/test_render_queue_timeout.py tests/test_supply_render_queue.py \
  tests/test_supply_rendered_reader.py
node --test --test-timeout=10000 apps/v1/*.test.js
```

这些是发布范围内的离线测试，不是假装付费模型成功。历史私有研究回包没有随仓库分发，下面 5 个测试文件未列入第一条公开复验命令，文件本身仍原样保留在授权归档里：

- `test_adjudication_hardening.py`
- `test_handbook_assets.py`
- `test_handbook_polish.py`
- `test_single_session_tool_call_degrade.py`
- `test_single_session_sizing.py`

此外有一个原有测试要求显式设置 `TRAVEL_REVISION_REPLAY_SESSION`，本次未设置而 skip。不执行全部旧架构测试，不能把所列命令称作仓库全量测试。当前平台实测数量及已知限制见 [发布说明](RELEASE-2026-11-18.md)。

## 阅读代码

先读 [产品逻辑](PRODUCT-LOGIC.md)，再按 [架构索引](ARCHITECTURE.md) 定位当前入口。旧分阶段管线和独立能力模块仍在源码中，不能由文件存在推断 Studio 已使用它们。当前单会话入口明确声明 attachments=false；不要拿旧上传界面的存在当材料理解已经接通。
