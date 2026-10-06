# HTTP 压力测试工具（JS 版）

两个形态，**同一套统计口径**（并发 / 时长 / 超时 / 成功失败定义 / 报告格式都一致）：

| 形态 | 文件 | 适用场景 | 限制 |
| --- | --- | --- | --- |
| Node 命令行 | `http_pressure_test.mjs` | 真压测：高并发、走代理、批量脚本 | 需要 Node（本机 v22.15.1） |
| 浏览器单文件 | `index.html` | 随手验证、有实时曲线和图形界面 | 不支持代理、同域并行约 6、跨域受 CORS 限制 |

> ⚠️ 仅用于已获得完整书面授权的自有服务器性能测试，禁止对未授权目标发起压力测试。

---

## 一、命令行版（真压测）

```bash
cd pressure_test_js

# 最简：不带代理，压本地服务 20 秒
node http_pressure_test.mjs http://127.0.0.1:8080/ -c 10 -d 20

# 带代理（每行一个，支持 ip:port / ip:port:用户:密 / http://…）
node http_pressure_test.mjs https://example.com/ -p ..\proxies.txt -c 5 -d 30

# 结束后多打印一行 JSON，方便脚本解析
node http_pressure_test.mjs http://127.0.0.1:8080/ -d 5 --json
```

### 参数

| 参数 | 含义 | 默认 | 范围 |
| --- | --- | --- | --- |
| `-p, --proxies <文件>` | 代理列表文件 | 不填 = 本机 IP 直连 | — |
| `-c, --concurrency <n>` | 最大并发数 | 5 | 1~1000 |
| `-d, --duration <秒>` | 测试时长 | 30 | 1~7200 |
| `-t, --timeout <秒>` | 单请求超时 | 10 | 1~120 |
| `--json` | 结尾追加一行 JSON 结果 | 关 | — |
| `-h, --help` | 帮助 | — | — |

退出码：`0` 正常结束，`2` 参数/用法错误，`1` 运行时异常。

### 特性

- **零依赖**：只用 Node 内置 `http/https/tls`，`npm install` 都不需要。
- **代理**：`http://` 目标走绝对 URI，`https://` 目标走 CONNECT 隧道；`socks4:// / socks5://` 行会跳过并计数（Node 内置不支持 SOCKS，需要额外实现）。
- **代理行规整**：`1.2.3.4:8080` → 自动补 `http://`；`ip:port:用户:密` → `http://用户:密@ip:port`（Python 版不支持后者的自动转换）。
- **失败原因分类**：`ECONNREFUSED（连接被拒）`、`TIMEOUT（请求超时）`、`PROXY_CONNECT_403` 等，进报告统计。
- **瞬间失败节流**：请求 <2ms 就失败时补 20ms 间隔，不烧 CPU，QPS 不虚高。
- `Ctrl+C` 随时停止，等在途请求收尾后照常出报告。
- 自签证书不校验（与 Python 版 `ssl=False` 一致）。
- 可被 `import` 复用（`Stats / makeRequest / normalizeProxy / loadProxies / runTest` 等已导出），直接执行才跑 CLI。

### 自测

```bash
node test_unit.mjs     # 26 项断言：代理规整、地址/参数校验、统计与报告
```

---

## 二、浏览器版（双击即用）

**打开方式（二选一）：**

1. 双击 `打开浏览器版.cmd`（或直接双击 `index.html`）——`file://` 打开。
   跨域目标会自动降级为 `no-cors` 模式（能测通不通，**看不到状态码**）。
2. 用本地服务器打开（推荐，同源无限制、状态码完整）：
   ```bash
   # 在 proxy 目录下起服务
   python -m http.server 8080 --bind 127.0.0.1
   # 浏览器访问
   http://127.0.0.1:8080/pressure_test_js/index.html
   ```

**用法**：填目标地址 → 勾「我已获得该目标的完整书面授权」→ 点 ▶ 开始测试 → 进度条/曲线/卡片实时刷新 → 结束后可「复制报告」「下载报告…」。

### 浏览器的硬限制（无法绕过）

- **不支持代理**：浏览器不允许自定义出站代理，想走代理请用 Node 版。
- **同域并行约 6 条**（HTTP/1.1）：并发填再大也只是排队；只有 HTTP/2 目标才能多路复用。
- **跨域受 CORS**：目标没开 CORS 时，勾选「跨域自动降级 no-cors」可测连通性，状态码显示为「跨域不可见」。
- 测的是「**你自己的浏览器 → 目标**」这条链路，不含代理链路。

---

## 三、三套工具怎么选

| | Python CLI | Python GUI | Node CLI | 浏览器版 |
| --- | --- | --- | --- | --- |
| 并发/时长/超时 | ✅ | ✅ | ✅ | ✅ |
| 走代理 | ✅ | ✅ | ✅ | ❌ |
| 图形界面 | ❌ | ✅ | ❌ | ✅ |
| 实时曲线 | ❌ | ✅ | ❌ | ✅ |
| 失败原因统计 | ✅ | ✅ | ✅ | ✅ |
| 依赖 | aiohttp | aiohttp + tkinter | 无 | 无 |

- 要**批量走代理压测** → `..\http_pressure_test.py`（CLI）或 `..\http_pressure_test_gui.py`（GUI）。
- 要**最轻量、脚本里直接跑** → 本目录 `http_pressure_test.mjs`。
- 要**打开网页点两下、看曲线** → 本目录 `index.html`。

---

## 四、目录文件

```
pressure_test_js/
├── http_pressure_test.mjs   Node 命令行版（零依赖）
├── index.html               浏览器单文件版（零依赖）
├── 打开浏览器版.cmd           双击打开 index.html
├── test_unit.mjs            单元自测（node test_unit.mjs）
├── _test_proxies.txt        自测用的样例代理文件
└── README.md
```
