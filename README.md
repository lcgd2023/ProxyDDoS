# 免费代理爬虫（proxy.scdn.io / 89ip.cn / proxyhub.me）

三个互相独立的爬虫脚本，各自负责一个站点，命令行风格、输出格式完全一致：

| 脚本 | 目标站点 | 默认行为 |
| --- | --- | --- |
| `proxy_scraper.py` | <https://proxy.scdn.io/> | 站点 JSON 接口，1 次请求 100 条，带协议/国家/响应时间 |
| `proxy_scraper_89ip.py` | <https://www.89ip.cn/> | 代理提取接口，1 次请求拿全库约 4400 条 |
| `proxy_scraper_proxyhub.py` | <https://proxyhub.me/> | 逐页抓完全部 100 页，共 2000 条，带国家/协议/匿名度 |
| `proxy_scraper_jiliu.py` | <https://www.jiliuip.com/free/> | 逐页抓完全部 6082 页，带匿名度/类型/位置/响应速度 |

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`beautifulsoup4`；`PySocks` 仅在验证 SOCKS4/SOCKS5 代理时需要。

## 快速开始

```bash
python proxy_scraper.py             --max-pages 1     # scdn：100 条
python proxy_scraper_89ip.py                          # 89ip：全库 4400 条
python proxy_scraper_proxyhub.py                      # proxyhub：全部 100 页 2000 条
python proxy_scraper_jiliu.py --max-pages 50          # jiliuip：先小规模试跑（全量需约 50 分钟）
```

四个脚本都支持 `--formats csv,txt,json`、`--limit`、`--max-pages`、`--delay`、`--validate`、`--only-valid`。

---

# 一、proxy.scdn.io 爬虫

## 常用示例

```bash
python proxy_scraper.py --max-pages 0                # 抓全部页（约 350 页 / 3.5 万条）
python proxy_scraper.py --protocol SOCKS5 --country 中国 --max-pages 5
python proxy_scraper.py --source text -o all --formats txt   # 纯文本全量列表
python proxy_scraper.py --max-pages 0 --validate --only-valid -o alive
python proxy_scraper.py --source page --max-pages 3          # 接口挂了改解析静态页
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--source` | `api` | `api`=站点 JSON 接口，`page`=解析列表页 HTML，`text`=纯文本全量 |
| `--protocol` | 空 | `HTTP` / `HTTPS` / `SOCKS4` / `SOCKS5`，空表示全部（api/page 源有效） |
| `--country` | 空 | 国家/地区名，如 `中国`、`香港`（api/page 源有效） |
| `--per-page` | `100` | 每页条数：10 / 30 / 100 |
| `--max-pages` | `1` | 最多抓几页，`0` = 全部 |
| `--limit` | `0` | 最多收集多少条，`0` = 不限制 |
| `-o, --output` | `proxies` | 输出文件名（不含扩展名） |
| `--formats` | `csv,txt` | `csv`、`txt`、`json`，逗号分隔 |
| `--delay` / `--timeout` / `--retries` | `0.5` / `15` / `3` | 限速、超时、重试 |
| `--validate` | 关 | 抓取后并发实测可用性 |
| `--validate-workers` / `--validate-timeout` | `50` / `6` | 验证并发与超时 |
| `--validate-url` | gstatic generate_204 | 验证时访问的测试地址 |
| `--only-valid` | 关 | 仅输出验证通过的代理（需配合 `--validate`） |

## 输出

- `proxies.csv` — UTF-8 BOM，Excel 可直接打开，列：
  `ip, port, protocol, country, latency, last_checked, valid`
- `proxies.txt` — 每行一个 `ip:port`
- `proxies.json` — 结构化数组，字段同 csv

## 实现说明

- 默认走站点前端用的 `get_proxies.php` 接口，返回 JSON（`table_html` + `totalPages`），
  一次拿到 100 条结构化数据；响应实际编码为 UTF-8，代码里强制指定以免中文乱码。
- `--validate` 标 `HTTPS` 的代理会先试 `https://`、失败再回退 `http://`
  （免费 HTTPS 代理大多只是支持 CONNECT，端口本身不加密）。

---

# 二、89ip.cn 爬虫

## 两种数据源

**`extract`（默认）** — 调用站点「代理IP提取」接口 `tqdl.html`，支持 6 个筛选参数，
一次请求即可取全库（实测 `--num 0` 拿到 4400 条），只有 IP 和端口。

**`page`** — 逐页抓 `index_N.html`（40 条/页），额外拿到
`location`（代理位置）、`isp`（运营商）、`acquired_at`（录取时间）。

> ⚠️ `page` 源的分页不稳定：站内按「录取时间」排序，而所有记录时间戳几乎相同，
> 导致相邻页大量重复（实测相邻两页 40 条里有 25 条重合），全站扫完只能拿到约 1500 条唯一值。
> 需要完整清单请用 `extract`，需要位置/运营商字段时再配合 `page` 源补充。

## 常用示例

```bash
python proxy_scraper_89ip.py                       # 全库 ip:port -> proxies89.csv + txt
python proxy_scraper_89ip.py --num 200 --address 安徽 --isp 电信
python proxy_scraper_89ip.py --num 1000 --exclude-address 台湾 --exclude-port 8089
python proxy_scraper_89ip.py --source page --max-pages 0      # 列表页抓到底（带位置/运营商）
python proxy_scraper_89ip.py --num 500 --validate --only-valid -o alive89
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--source` | `extract` | `extract`=提取接口（全量/可筛选），`page`=逐页解析列表页 |
| `--max-pages` | `1` | 最多抓几页/几轮，`0` = 抓到底 |
| `--limit` | `0` | 最多收集多少条，`0` = 不限制 |
| `-o, --output` | `proxies89` | 输出文件名（不含扩展名） |
| `--formats` | `csv,txt` | `csv`、`txt`、`json` |
| `--num` | `0` | 每次提取数量，`0` = 一次取全库（上限 5000） |
| `--rounds` | `1` | 提取轮数，多轮取并集 |
| `--address` / `--exclude-address` | 空 | 地区 / 排除地区，如 `安徽`、`台湾` |
| `--port` / `--exclude-port` | 空 | 只取 / 排除指定端口，如 `8089` |
| `--isp` | 空 | 运营商，如 `电信`、`联通`、`移动`、`阿里云` |
| `--delay` / `--timeout` / `--retries` | `0.5` / `15` / `3` | 限速、超时、重试 |
| `--validate` | 关 | 抓取后并发实测可用性 |
| `--validate-scheme` | `http` | 验证时代理协议（89ip 不提供协议字段） |
| `--validate-workers` / `--validate-timeout` | `50` / `6` | 验证并发与超时 |
| `--validate-url` | `http://www.baidu.com` | 验证地址，默认选国内可达的（池子多为国内代理） |
| `--only-valid` | 关 | 仅输出验证通过的代理 |

## 输出

- `proxies89.csv` — 列：`ip, port, location, isp, acquired_at, valid`
  （`extract` 源只填 ip/port/valid，其余留空）
- `proxies89.txt` — 每行一个 `ip:port`
- `proxies89.json` — 结构化数组

## 实现说明

- 提取结果只从「提取结果」区块里取 `ip:port`，忽略侧栏的当前 IP、广告与统计数字。
- 筛选参数已实测生效：`--port 8089` 返回的端口全部为 8089，
  `--exclude-port 8089` 无残留，不存在的地区返回 0 条。
- 实测站点无明显反爬（连发 10 次秒回、无验证码），但脚本仍保留 `--delay` 限速与指数退避重试。
- 免费代理失效率很高：实测 `--num 300 --validate` 只有 7 个可用（连接拒绝/超时/407 属正常现象），
  需要稳定代理请加大抽样量再筛选。

---

# 三、proxyhub.me 爬虫

抓**所有页面**：默认抓全量列表的全部页（分页器显示 100 页 × 20 条 = 2000 条，
实测 100 页抓完正好 2000 条唯一 IP，页与页之间零重复）。

## 站点规则（实测）

| 事项 | 结论 |
| --- | --- |
| 每页条数 | 20 条 |
| 分页参数 | `?page=N`，总页数看分页器「» N」链接 |
| 全量列表地址 | 必须用 `/`：`/en/all-free-proxy-list.html` 会被重定向回 `/`，**并丢掉 `?page=N`** |
| 筛选方式 | 路径 `/{lang}/{country}-{type}-proxy-list.html?page=N`，不是查询参数 |
| 匿名度筛选 | Cookie `anonymity=transparent\|anonymous\|elite` |
| 超出总页码 | 站点重定向回第 1 页并丢掉 page 参数（不处理会无限循环） |
| 反爬 | 无（`robots.txt` 只屏蔽 SEO 爬虫），15 连发全部 200 |

脚本对后两条都做了处理：以分页器的总页数为准，另外检测「最终 URL 丢了 page 参数」时提前停止。

## 常用示例

```bash
python proxy_scraper_proxyhub.py                     # 全量列表所有页 -> proxieshub.csv/txt/json
python proxy_scraper_proxyhub.py --max-pages 5       # 只抓前 5 页
python proxy_scraper_proxyhub.py --country cn --type socks5          # 中国 SOCKS5（86 页）
python proxy_scraper_proxyhub.py --type https --anonymity elite      # HTTPS 高匿
python proxy_scraper_proxyhub.py --validate --only-valid -o alivehub
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--country` | `all` | 两位国家代码：`cn`/`us`/`jp`/`de`…（见站点国家下拉框） |
| `--type` | `free` | `free`(全部) / `http` / `https` / `socks4` / `socks5` / `socks` |
| `--anonymity` | `all` | `transparent` / `anonymous` / `elite`（走 Cookie） |
| `--lang` | `en` | 列表页语言目录（en/zh/de…，只影响 URL 不影响数据） |
| `--max-pages` | `0` | 最多抓几页，`0` = 抓完分页器给出的全部页 |
| `--limit` | `0` | 最多收集多少条，`0` = 不限制 |
| `-o, --output` | `proxieshub` | 输出文件名（不含扩展名） |
| `--formats` | `csv,txt` | `csv`、`txt`、`json` |
| `--delay` / `--timeout` / `--retries` | `0.5` / `15` / `3` | 限速、超时、重试 |
| `--validate` | 关 | 抓取后并发实测可用性 |
| `--validate-scheme` | `auto` | `auto`=按 `--type` 推断协议，也可手填 `http/https/socks4/socks5` |
| `--validate-workers` / `--validate-timeout` | `50` / `6` | 验证并发与超时 |
| `--validate-url` | gstatic generate_204 | 验证时访问的测试地址 |
| `--only-valid` | 关 | 仅输出验证通过的代理 |

## 输出

- `proxieshub.csv` — 列：`country, ip, port, protocols, anonymity, last_checked, valid`
- `proxieshub.txt` — 每行一个 `ip:port`
- `proxieshub.json` — 结构化数组

## 实现说明

- 实测全量 2000 条：89 个国家，协议分布 SOCKS5 1070 / HTTP 532 / SOCKS4 367 / HTTPS 31，
  匿名度 Elite 1438 / Anonymous 291 / Transparent 271，非法 `ip:port` 0 条。
- 筛选实测生效：`--country cn --type socks5` 只有 China + SOCKS5，
  `--type https --anonymity elite` 只有 HTTPS + Elite。
- 该站代理质量明显高于前两个站：实测 `--limit 40 --validate` 有 **30/40 可用**。
- 分国家/分类型的子列表页总数可能超过全量列表（全量列表封顶 100 页），
  想榨干数据可以对多个国家分别跑一遍再合并去重。

---

# 四、jiliuip.com 爬虫（/free/）

抓**所有页面**：默认抓 `/free/` 的全部页。站点标称「共 60815 条」，分页器给出
`data-last-page=6082`，每页 10 条，实测末页 5 条（6081×10+5=60815 ✓）。

> ⚠️ 全量约 6082 页，按默认限速约需 **50 分钟**；建议先 `--max-pages 50` 试跑。

## 站点规则（实测）

| 事项 | 结论 |
| --- | --- |
| 每页条数 | 10 条 |
| 分页地址 | 第 1 页 `/free/`，第 N 页 `/free/page-N/`；`?page=N` **不生效** |
| 总页数/总条数 | 跳页框 `data-last-page` + 分页器「共 60815 条」 |
| 地区筛选 | 路径 `/free/beijing/`、`/free/beijing/page-N/`，共 31 个地区 |
| 数据接口 | 无 JSON 接口，静态 HTML（`free_proxy.js` 只做整页跳转） |
| robots.txt | 只屏蔽 `/docs/`、`/api/`，`/free/` 允许 |
| 频率限制 | **有滚动窗口配额**：短时间内连续请求会 403，几秒~几十秒后恢复 |
| 超过末页 | 返回 200 但 0 行（不会重定向回第 1 页） |
| 页内/页间重复 | 严重：每页 10 行常常只有 8~10 个唯一值，北京地区页 10 行只有 2 个唯一值 |

脚本对上述情况的处理：

- **自适应限速**：一旦收到 403，限速器立即降速 1.5 倍（上限 4 倍基线）并冻结 8~20s，
  之后每成功一页按 3% 逐步恢复到基线 —— 无需人工干预；仍失败的页在结尾**自动补抓一轮**；
- **空页重试**：返回 200 却解析出 0 条时，判定为软性拦截，3s 后重试一次；
- **总页数**以分页器为准，超页不会无限循环；
- **跨页去重**：按 `ip:port` 去重，输出仍按页码排序（并发抓取时结果到达顺序是乱的）；
- **全局限速**：`--workers > 1` 时所有线程共用同一个限速器，总频率始终受 `--delay` 约束。

> ⚠️ **别同时跑两个抓取进程**：实测单进程 `delay=0.4`（约 2.5 页/秒）连续 2500 页零 403，
> 但两个进程叠加到约 4 次/秒就开始大面积 403，反而更慢。

## 常用示例

```bash
python proxy_scraper_jiliu.py --max-pages 50                # 先试跑 50 页
python proxy_scraper_jiliu.py                               # 全量 6082 页（约 50 分钟）
python proxy_scraper_jiliu.py --delay 0.6                   # 更保守的限速
python proxy_scraper_jiliu.py --region beijing --max-pages 5
python proxy_scraper_jiliu.py --region 广东 --max-pages 5    # 也接受中文地区名
python proxy_scraper_jiliu.py --validate --only-valid -o alivej
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--region` | 空 | 地区筛选，slug（`beijing`）或中文名（`北京`/`广东`），空 = 全国 |
| `--max-pages` | `0` | 最多抓几页，`0` = 抓完分页器给出的全部页 |
| `--limit` | `0` | 最多收集多少条，`0` = 不限制 |
| `-o, --output` | `proxiesjiliu` | 输出文件名（不含扩展名） |
| `--formats` | `csv,txt` | `csv`、`txt`、`json` |
| `--delay` | `0.4` | 两次请求最小间隔（秒，多线程共享同一限速器） |
| `--workers` | `1` | 抓取并发线程数（只隐藏延迟，不突破 `--delay` 总频率） |
| `--timeout` / `--retries` | `15` / `3` | 超时、重试次数（403 走更长的独立退避） |
| `--validate` | 关 | 抓取后并发实测可用性 |
| `--validate-scheme` | `http` | 验证时代理协议（列表标注为 HTTP） |
| `--validate-workers` / `--validate-timeout` | `50` / `6` | 验证并发与超时 |
| `--validate-url` | `http://www.baidu.com` | 验证地址，站点主打国内代理，默认选国内可达地址 |
| `--only-valid` | 关 | 仅输出验证通过的代理 |

## 输出

- `proxiesjiliu.csv` — 列：`ip, port, anonymity, type, location, response, last_verified, valid`
- `proxiesjiliu.txt` — 每行一个 `ip:port`
- `proxiesjiliu.json` — 结构化数组

## 实现说明

- 分页是**确定性切片**：同一 URL 反复抓内容完全一致（相隔 2 分钟也一致），
  因此重复抓取不会「刷出」新数据，抓过的页无需重来。
- 但**相邻页高度重合**：实测 `page-100..110` 共 110 行只有 34 个唯一 IP，
  而 `page-1000..1010` 有 96 个且与前者仅 2 个重合 ——
  即开头一大段是同一批代理的重复行，**远处页才有新数据**，所以全量抓取确实有价值。
- 实测 300 页仅得 219 条唯一 IP，且前 270 页几乎零新增，属站点数据分布问题，非 bug。
- 验证结果：`--limit 40 --validate` 对 baidu / gstatic 各只有 1/32 可用
  （列表的「最后验证时间」多为几天前，失效率高是常态）。

---

## 通用

- 日志输出强制 UTF-8，Windows 控制台不会出现中文乱码。
- 四个脚本都内置请求间隔（`--delay` + 随机抖动）、失败重试与跨页去重。

> 本脚本仅用于获取公开的免费代理列表，请遵守目标站点的使用条款，控制抓取频率。
> 免费代理质量参差不齐，涉及敏感场景请自行验证后再使用。

## 云端代理池（proxy_pool/）

本地脚本的进阶形态：定时把四个站点抓进 Cloudflare D1、自动测活、可视化筛选导出。
详见 [`proxy_pool/README.md`](proxy_pool/README.md)。

- 面板（国内可直连）：<https://proxy-pool-web.pages.dev>
- 海外直连：<https://proxy-pool.proxy-pool.workers.dev>
- 部署方式：Worker（cron 抓取 + D1 + API + 面板）+ Pages（国内入口反代）
- 本地配合：`scripts/check_alive.py` 领任务测活回传、`scripts/push_proxies.py` 导入本地成果

---

# 五、HTTP 压力测试工具（`http_pressure_test.py`）

> ⚠️ **仅用于已获得完整书面授权的自有服务器性能测试，禁止用于未授权目标。**
> 界面上必须勾选「我已获得该目标的完整书面授权」才能开始。

两种用法共用同一条 `run_test()`，统计口径完全一致：

| | 命令行 | 图形界面 |
|---|---|---|
| 启动 | `python http_pressure_test.py <url> [-p 代理.txt] [-c 5] [-d 30] [-t 10]` | 双击 `打开HTTP压测工具.cmd`（或 `pythonw http_pressure_test_gui.py`） |
| 选代理 | `-p` 指定文件 | 下拉框直接点选本目录已有的 txt（`proxies.txt`、`proxies89.txt`…），或「浏览…」挑任意位置的文件，**选完立刻显示条数** |
| 实时进度 | 每 2 秒一行 | 6 张指标卡 + QPS 曲线 + 进度条 + 状态码分布 |
| 结束报告 | 打印到终端 | 日志区彩色显示，可「复制报告」/「导出报告…」成 txt |
| 中途停止 | Ctrl+C | 「■ 停止」按钮（等在途请求收尾后再出报告） |

- 图形界面：`http_pressure_test_gui.py`（tkinter，跑在独立线程 + 队列刷新，不卡界面）
- 参数对应：界面上的 并发/时长/超时 = `-c`/`-d`/`-t`，代理 txt = `-p`
- **代理行会自动补 `http://`**（aiohttp 不认裸 `ip:port`，否则请求发不出去、100% 失败）；
  `socks4://`/`socks5://` 行本工具不支持（需 `aiohttp-socks`），加载时会跳过并提示条数
- **失败会给出原因**（`ClientOSError(错误码 1225)`、`TimeoutError`…），
  直接显示在界面红字行和终端报告里，不再只有干巴巴的"失败 N 次"
- 依赖：`aiohttp`（已写进 `requirements.txt`，`pip install -r requirements.txt`）
- 本机自测：先 `python -m http.server 8080`，目标地址填 `http://127.0.0.1:8080/`
  （**测本机就别选代理文件**——直连 100% 成功；选了代理就是"经代理访问"的链路，
  免费代理大多已失效，会体现在失败数与失败原因里）

## 同款 JS 版（`pressure_test_js/`，零依赖）

统计口径、报告格式与 Python 版一致，两个形态：

| | 命令行 | 浏览器版 |
|---|---|---|
| 文件 | `pressure_test_js/http_pressure_test.mjs` | `pressure_test_js/index.html` |
| 启动 | `node pressure_test_js/http_pressure_test.mjs <url> [-p 代理.txt] [-c 5] [-d 30] [-t 10]` | 双击 `pressure_test_js\打开浏览器版.cmd`，或本地服务器打开 `http://127.0.0.1:8080/pressure_test_js/index.html` |
| 走代理 | ✅（http 走绝对 URI，https 走 CONNECT；`ip:port:用户:密` 自动转 URL） | ❌ 浏览器不支持自定义出站代理 |
| 图形界面 | ❌ 实时进度行 + 结束报告，`--json` 可给脚本解析 | ✅ 6 指标卡 + QPS 曲线 + 进度条 + 复制/下载报告 |

- 浏览器版限制（无法绕过）：**同域并行约 6 条**（HTTP/1.1）、**跨域受 CORS**；
  跨域时勾着「自动降级 no-cors」只能测通不通（状态码显示「跨域不可见」），关掉则记为
  `TypeError（CORS 被拦或网络不可达）` 失败。
- 想看状态码就用**同源打开**：本地服务器方式下页面与目标同源，无 CORS 问题。
- 自测：`node pressure_test_js/test_unit.mjs`（26 项断言），详见 `pressure_test_js/README.md`。

## 同款 Java 版（`pressure_test_java/`，零依赖单文件）

只有命令行形态，**单文件免编译免依赖**，装个 JDK 直接跑 `.java` 源文件：

```bash
java pressure_test_java/HttpPressureTest.java http://127.0.0.1:8080/ -c 10 -d 20
java pressure_test_java/HttpPressureTest.java --selftest      # 63 项断言，约 1~2 秒
```

- 统计口径、报告格式与 Python/JS 版一致，三份报告可直接对照。
- **代理支持最全**：http 代理 + `socks5://`（JDK 原生）+ `socks4://`（内置握手，JDK 不会说 v4）；
  代理行支持 `ip:port`、`ip:port:用户:密`、`用户:密@ip:port`、IPv6。
- 附加开关：`--json`（脚本解析）、`--stop-after <秒>`（等价 Ctrl+C 的优雅停止）、`--utf8`（重定向到文件时用）。
- 中文 Windows 输出策略：默认跟控制台编码（GBK）并自动去掉 emoji，避免打印成 `?`；
  要 UTF-8 文件才加 `--utf8`。
- 详见 `pressure_test_java/README.md`。

