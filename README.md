# HTTP 压力测试工具（Java 版，零依赖单文件）

> ⚠️ **仅用于已获得完整书面授权的自有服务器性能测试，禁止对未授权目标发起压力测试。**

与 `../http_pressure_test.py`（Python）、`../pressure_test_js/`（Node/浏览器）**同一套统计口径**：
并发、时长、超时的含义一致，「HTTP 2xx/3xx/4xx/5xx = 成功、连接失败/超时 = 失败」的判定一致，
报告格式（状态码分布 + 失败原因分布 + 百分比）也一致，三份报告可以直接对照。

---

## 一、运行方式

**单文件，免编译、免依赖**，装个 JDK（本机 JDK 24）直接跑：

```bash
cd pressure_test_java

# 最简：不带代理，压本地服务 20 秒
java HttpPressureTest.java http://127.0.0.1:8080/ -c 10 -d 20

# 带代理文件（每行一个）
java HttpPressureTest.java https://example.com/ -p ..\proxies.txt -c 5 -d 30

# 结尾多打印一行 JSON，给脚本解析
java HttpPressureTest.java http://127.0.0.1:8080/ -d 5 --json

# 内置自测（只用本机回环，不碰任何外部目标）
java HttpPressureTest.java --selftest
```

想先编译再跑也可以（启动更快）：

```bash
javac -encoding UTF-8 -d out HttpPressureTest.java
java -cp out HttpPressureTest <url> ...
```

### 参数

| 参数 | 含义 | 默认 | 范围 |
| --- | --- | --- | --- |
| `-p, --proxies <文件>` | 代理列表文件 | 不填 = 本机 IP 直连 | — |
| `-c, --concurrency <n>` | 最大并发数 | 5 | 1~1000 |
| `-d, --duration <秒>` | 测试时长 | 30 | 1~7200 |
| `-t, --timeout <秒>` | 单请求超时 | 10 | 1~120 |
| `--stop-after <秒>` | 运行 N 秒后优雅停止（等价 Ctrl+C，给脚本用） | 关 | — |
| `--json` | 结尾追加一行 JSON 结果 | 关 | — |
| `--utf8` | 输出强制 UTF-8 | 关（跟随控制台编码） | — |
| `--selftest` | 跑内置自测 | 关 | — |
| `-h, --help` | 帮助 | — | — |

退出码：`0` 正常结束，`2` 参数/用法错误（干净打印用法，不甩堆栈），`1` 运行时异常。

---

## 二、特性

- **零依赖**：只用 JDK 标准库 `Socket` / `SSLSocket`，没有 `pom.xml`、没有第三方 jar。
- **代理支持比 Python/Node 版都全**：
  - `ip:port`（自动当 http 代理）、`http://…`、`ip:port:用户:密`、`用户:密@ip:port`、IPv6 `[::1]:8080`
  - `socks5://…` → 交给 JDK 原生 SOCKS 客户端（自测实测走 v5 握手）
  - `socks4://…` → **内置握手**（JDK 只会说 v5，且版本是全局属性没法按代理切换，所以自己实现；
    目标是域名时用 SOCKS4a；SOCKS4 只有 USERID 没有密码，带密码的行只用用户名）
  - 走 http 代理时：`http://` 目标发**绝对 URI**，`https://` 目标先 **CONNECT 隧道**再 TLS，
    认证走 `Proxy-Authorization: Basic`
- **失败原因分类**与另外两版一致：`ECONNREFUSED（连接被拒）`、`ENOTFOUND（DNS 解析失败）`、
  `ETIMEDOUT（连接超时）`、`TIMEOUT（请求超时）`、`ECONNRESET（连接被重置）`、
  `PROXY_CONNECT_403（代理拒绝 CONNECT）`、`SOCKS_ERROR`、`SSL_ERROR` …
  全部进「失败原因分布」统计，**不做 top5 截断**（Python/Node 版会截，这里给全量）。
- **成功判定与响应体**：拿到状态码即算成功；响应头按 `\r\n\r\n` 精确读到空行，
  再按 `Content-Length` / chunked 终块 / EOF 读最多 4KB 响应体，
  **读不全或读超时都不改判**（否则服务器不按 `Connection: close` 断开时，一个 200 会被误记成 TIMEOUT）。
- **瞬间失败节流**：请求 <2ms 就失败时补 20ms 间隔，不烧 CPU、QPS 不虚高。
- **不校验证书**（与 Python `ssl=False`、Node `rejectUnauthorized:false` 一致）。
- `Ctrl+C` 随时停止，等在途请求收尾后照常出报告；
  脚本里用 `--stop-after <秒>` 达到同样效果（**Windows 上 Ctrl+Break 是 JVM 的
  「打印线程 dump」保留行为，别拿它当停止键**）。
- 统计与报告格式化一律 `Locale.ROOT`，中文 Windows 下小数点/千分位不会变成逗号。

### 输出编码（中文 Windows 必看）

JDK 在中文 Windows 上的 stdout 编码是 **GBK**（JEP 400 之后 `file.encoding` 变 UTF-8，
但 `stdout.encoding` 仍是原生编码；而且 `System.console()` 在 stdin 还是控制台时照样返回 true，
**没法用来判断是否重定向**）。所以：

- **默认跟随原生编码**，并在「放得下中文、放不下 emoji」时自动**去掉 emoji**——
  中文控制台上不会打印成一串 `?`；
- **重定向到文件、要 UTF-8 内容**就加 `--utf8`（此时在 GBK 控制台里直接看反而会乱码）。

---

## 三、自测

```bash
java HttpPressureTest.java --selftest --utf8     # 63 项断言，约 1~2 秒
```

覆盖：代理行规整（含 socks4/socks5/IPv6/带账密）、代理文件统计、地址与参数校验、
状态行与 CONNECT 应答解析、失败原因分类、统计与报告/JSON、
以及**本机回环的真实链路**——直连 200、端口没人听→`ECONNREFUSED`、服务端不回话→`TIMEOUT`、
经 http 代理转发绝对 URI、CONNECT 被拒→`PROXY_CONNECT_403`、经 SOCKS5 代理、经 SOCKS4 代理。

自测只监听 `127.0.0.1` 随机端口，**不会发出任何外网请求**。

---

## 四、和另外两版的差异

| | Python CLI/GUI | Node CLI | **Java** |
| --- | --- | --- | --- |
| 运行前提 | `pip install aiohttp` | Node | JDK |
| 形态 | CLI + GUI | CLI + 浏览器单文件 | **只有 CLI** |
| 代理 | http（`socks*` 行跳过） | http（`socks*` 行跳过） | **http + socks4 + socks5** |
| `ip:port:用户:密` 自动转 URL | ❌ | ✅ | ✅ |
| `--json` | — | ✅ | ✅ |
| 失败原因 | ✅ | top5 截断 | ✅ 全量 |
| 自测 | — | 26 项断言 | 63 项断言（含回环链路） |

---

## 五、目录文件

```
pressure_test_java/
├── HttpPressureTest.java   单文件 CLI（零依赖，java 直接跑 .java 源文件）
└── README.md
```

本机自测：先在上级目录 `python -m http.server 8080`，目标填 `http://127.0.0.1:8080/`；
**测本机就别选代理文件**（直连 100% 成功，选了代理就是「经代理访问」链路，
免费代理大多已失效，会体现在失败数和失败原因里）。
