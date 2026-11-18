# Trip Autopilot

旅行规划 Agent：在同一个行程会话中研究、生成和修订旅行手册，展示保存后的 HTML，并导出速览版与完整版 PDF。

当前交付入口是 `/studio.html`。历史版本已有真实生成、同会话修订、HTML 手册展示、双 PDF 和同浏览器恢复的验收记录；**2026-11-18 这次打包及临时 HTTPS 部署的完整实测仍待完成**。历史首次生成约需 10–12 分钟，具体耗时随行程、来源和网络变化。

## 仓库内容

| 路径 | 内容 |
| --- | --- |
| `apps/v1/` | 对话、进度与手册界面 |
| `tools/start_customer_preview.py` | 启动隔离的应用、渲染服务、认证网关和临时 HTTPS 隧道 |
| `tools/serve_customer_preview.py` | 口令登录与 Cookie 认证网关 |
| `core/packages-session-20261118.tar.gz.enc` | 本次版本的加密后端源码包，解密后得到 `packages/` |
| `core/packages-core.tar.gz.enc` | 保留的旧版本加密归档 |
| `requirements.txt`、`.env.example` | Python 依赖与环境变量示例 |
| `docs/CUSTOMER-PREVIEW-2026-11-18.md` | 运行、访问、验收边界与故障处理说明 |

后端源码属于作者保留的 IP，以 AES-256-CBC、PBKDF2 加密交付。解密口令由作者单独提供给授权使用者，不包含在仓库中。新版本使用 `packages-session-20261118.tar.gz.enc`；旧归档保留供历史追溯。

## 快速启动临时验收环境

以下命令在一个干净的仓库克隆根目录执行，按 macOS、Python 3.13 环境准备。需先安装 Python 3.13、`cloudflared` 和 Poppler（`pdfinfo`、`pdfimages`），并将其加入 `PATH`。例如使用 Homebrew：`brew install python@3.13 cloudflared poppler`。Windows/Linux 未在本轮实测。

```sh
# OpenSSL 会交互式询问解密口令；不要把口令写进命令参数。
openssl enc -d -aes-256-cbc -pbkdf2 \
  -in core/packages-session-20261118.tar.gz.enc -out core.tar.gz
tar -xzf core.tar.gz

python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env
chmod 600 .env
```

编辑 `.env`，填写 `DEEPSEEK_API_KEY`。`JINA_API_KEY`、`PEXELS_API_KEY` 和 `UNSPLASH_ACCESS_KEY` 是可选来源/图片服务配置。启动脚本读取进程环境，不会自动加载 `.env`：

```sh
set -a
. ./.env
set +a

python tools/start_customer_preview.py \
  --state-dir ../trip-autopilot-preview-20261118 \
  --app-port 7393 --gateway-port 7394 --render-port 7395
```

脚本输出本次 `https://…trycloudflare.com/studio.html` 地址。验收口令保存在仓库外的 `../trip-autopilot-preview-20261118/access.json`，由运行者私下提供给授权验收者。浏览器打开该地址，输入口令进入产品。

Mac、网络连接和启动进程须持续在线。终端按 `Ctrl+C` 会关闭本次子服务；重新启动可能获得新的临时域名。新域名属于新的浏览器来源，不承诺沿用旧域名的本地写凭证。

## 数据与验收边界

`--state-dir` 必须在仓库外。应用使用其下的 `sessions/`，渲染使用独立目录；新建一个空目录即得到新的验收数据空间。解密源码本身不会连接或读取旧私有库。保留同一个数据目录可以保留服务端会话，但客户端写凭证仍需保留。

恢复目前限定为同一浏览器、同一访问来源且写凭证仍在的情况。Undo 端点，以及跨浏览器或写凭证丢失后的恢复尚未完成。首次生成速度、小修订速度和本轮完整实测应分别记录，不以测试通过或页面可打开替代产品验收。

完整步骤与本轮待验清单见 [临时 HTTPS 验收说明](docs/CUSTOMER-PREVIEW-2026-11-18.md)。
