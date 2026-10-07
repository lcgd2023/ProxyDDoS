#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP/HTTPS 应用层性能测试工具
⚠️ 仅可用于获得完整书面授权的自有服务器性能测试，禁止用于未授权目标

两种用法，共用同一条 run_test()，统计口径完全一致：
    命令行     python http_pressure_test.py <url> [-p 代理.txt] [-c 并发] [-d 秒] [-t 秒]
                       [-b 上传体] [--max-traffic 流量上限]
    图形界面   双击 打开HTTP压测工具.cmd   （或 pythonw http_pressure_test_gui.py）
"""
import argparse
import asyncio
import os
import random
import sys
import time
from collections import defaultdict
from urllib.parse import urlsplit

import aiohttp

# 输出重定向到文件/管道时，默认编码是 GBK，emoji 会直接抛 UnicodeEncodeError；
# 直连控制台时不动它（走 Console API，emoji 正常）。
if not sys.stdout.isatty():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 常见浏览器User-Agent，用于模拟真实用户访问
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
]


def fmt_bytes(n) -> str:
    """字节数 -> 人话（1 KB = 1024 B），命令行报告和图形界面共用。"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} PB"


def parse_bytes(text, name: str = "流量") -> int:
    """把 `512` / `64k` / `64KB` / `2m` / `1g` 解析成字节数；空串和 0 表示"不设"。

    命令行 -b、--max-traffic 和图形界面的两个输入框共用，输入不合法抛 ValueError。
    """
    s = str(text or "").strip().lower().replace(" ", "")
    if not s or s in ("0", "0b", "-", "none", "off"):
        return 0
    mult = 1
    for suffix, m in (("tib", 1 << 40), ("gib", 1 << 30), ("mib", 1 << 20), ("kib", 1 << 10),
                      ("tb", 1 << 40), ("gb", 1 << 30), ("mb", 1 << 20), ("kb", 1 << 10),
                      ("t", 1 << 40), ("g", 1 << 30), ("m", 1 << 20), ("k", 1 << 10), ("b", 1)):
        if s.endswith(suffix):
            mult = m
            s = s[: -len(suffix)]
            break
    try:
        n = float(s)
    except ValueError:
        raise ValueError(f"{name}格式不对：{text!r}（示例：512、64k、2m、1g）")
    if n < 0:
        raise ValueError(f"{name}不能是负数")
    return int(n * mult)


# 单请求上传体上限：再大就该用专业工具（且一次生成 100MB 随机体意义不大）
MAX_BODY_SIZE = 64 * 1024 * 1024


class Stats:
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.status_codes = defaultdict(int)
        self.errors = defaultdict(int)   # 失败原因 -> 次数（否则界面只知道"全挂了"）
        self.total_resp_time = 0.0
        self.ok_bytes = 0                # 成功请求实际读到的字节数（响应头 + 已读响应体）
        self.sent_bytes = 0              # 成功请求发出去的字节数（请求头 + 请求体）
        self.start_time = time.time()

    def add_success(self, status_code, resp_time, nbytes=0, sent=0):
        self.total += 1
        self.success += 1
        self.status_codes[status_code] += 1
        self.total_resp_time += resp_time
        self.ok_bytes += max(0, int(nbytes or 0))
        self.sent_bytes += max(0, int(sent or 0))

    def add_failed(self, reason: str = ""):
        self.total += 1
        self.failed += 1
        if reason:
            self.errors[reason] += 1

    def top_errors(self, n: int = 5) -> list:
        return sorted(self.errors.items(), key=lambda kv: -kv[1])[:n]

    def snapshot(self) -> dict:
        """实时快照，给界面/进度条用（在事件循环里生成，主线程只读）。"""
        elapsed = time.time() - self.start_time
        return {
            "elapsed": elapsed,
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "qps": self.total / elapsed if elapsed > 0 else 0.0,
            "avg_ms": (self.total_resp_time / self.success) * 1000 if self.success else 0.0,
            "ok_bytes": self.ok_bytes,
            "avg_ok_bytes": self.ok_bytes // self.success if self.success else 0,
            "sent_bytes": self.sent_bytes,
            "total_bytes": self.ok_bytes + self.sent_bytes,
            "status_codes": dict(self.status_codes),
            "errors": self.top_errors(),
        }

    def report(self) -> str:
        """完整测试报告（文本），命令行直接打印、图形界面放进日志/导出。"""
        s = self.snapshot()
        traffic = f"📶 成功流量(下行): {fmt_bytes(s['ok_bytes'])}"
        if s["success"] > 0 and s["elapsed"] > 0:
            traffic += (f"（平均 {fmt_bytes(s['ok_bytes'] / s['success'])}/个 · "
                        f"{fmt_bytes(s['ok_bytes'] / s['elapsed'])}/秒）")
        uplink = f"⬆️  上行流量: {fmt_bytes(s['sent_bytes'])}"
        if s["success"] > 0 and s["elapsed"] > 0:
            uplink += (f"（平均 {fmt_bytes(s['sent_bytes'] / s['success'])}/个 · "
                       f"{fmt_bytes(s['sent_bytes'] / s['elapsed'])}/秒）")
        lines = [
            "",
            "=" * 60,
            "📊 测试完成报告",
            f"⏱️  总测试时长: {s['elapsed']:.2f} 秒",
            f"📤 总请求数: {s['total']}",
            f"✅ 成功请求: {s['success']} | ❌ 失败请求: {s['failed']}",
            f"⚡ 每秒请求数(QPS): {s['qps']:.2f}",
            f"⏳ 平均响应时间: {s['avg_ms']:.2f} ms",
            traffic,
            uplink,
        ]
        if s["sent_bytes"] > 0:
            lines.append(f"🔢 总流量(上行+下行): {fmt_bytes(s['total_bytes'])}")
        if s["success"] > 0:
            lines.append("🔢 状态码分布:")
            for code, count in sorted(s["status_codes"].items()):
                percentage = count / s["total"] * 100
                lines.append(f"    HTTP {code}: {count} 次 ({percentage:.1f}%)")
        if s["failed"] > 0:
            lines.append("❌ 失败原因分布:")
            for reason, count in self.top_errors(5):
                percentage = count / s["total"] * 100
                lines.append(f"    {reason}: {count} 次 ({percentage:.1f}%)")
        lines.append("=" * 60)
        return "\n".join(lines)

    def print_report(self):
        print(self.report())


def normalize_proxy(line: str):
    """把代理行规整成 aiohttp 认识的 URL；用不了的返回 None。

    aiohttp 只接受带 scheme 的 URL，裸 `ip:port` 会在发请求**之前**就抛 InvalidURL
    （表现为 100% 失败、失败数疯涨），所以这里统一补 `http://`；
    socks4/socks5 需要 aiohttp-socks 才能用，本工具不支持 → 跳过并计数。
    """
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    token = s.split()[0].rstrip(",")
    if "://" in token:
        scheme = token.split("://", 1)[0].lower()
        return token if scheme in ("http", "https") else None
    return "http://" + token


def load_proxies_detail(path: str):
    """读取代理文件 -> (可用代理 URL 列表, 被跳过的行数)。"""
    usable, skipped = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            u = normalize_proxy(line)
            if u:
                usable.append(u)
            elif line.strip() and not line.strip().startswith("#"):
                skipped += 1
    return usable, skipped


def load_proxies(path: str) -> list:
    """读取代理文件：每行一个，# 开头视为注释；返回可直接交给 aiohttp 的 URL 列表。"""
    return load_proxies_detail(path)[0]


def estimate_request_bytes(method: str, target_url: str, headers: dict, body_len: int) -> int:
    """上行字节数估算：请求行 + 我们设置的头部 + aiohttp 自动补的 Host/Content-Length + 请求体。

    精确值只能抓包才有（同一请求里 aiohttp 还会按需补 Content-Type 等），
    这里按字段累加，误差几十字节，用于「上行流量」统计足够。
    """
    p = urlsplit(target_url)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    n = len(f"{method} {path} HTTP/1.1\r\n")
    n += sum(len(k) + len(v) + 4 for k, v in headers.items())   # "Name: value\r\n"
    host = p.hostname or ""
    if p.port and p.port not in (80, 443):
        host += f":{p.port}"
    n += len(f"Host: {host}\r\n")
    if body_len:
        n += len(f"Content-Length: {body_len}\r\n")
    n += 2   # 头部结束的空行
    return n + body_len


async def send_request(session, target_url, proxy, stats, semaphore, timeout, body=None):
    async with semaphore:
        req_start = time.time()
        method = "POST" if body is not None else "GET"
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive"
        }
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        uplink = estimate_request_bytes(method, target_url, headers,
                                         len(body) if body is not None else 0)
        try:
            kwargs = dict(headers=headers, proxy=proxy, timeout=timeout,
                          ssl=False)  # 若测试HTTPS站点可改为True，或根据证书情况调整
            if body is not None:
                kwargs["data"] = body
            async with session.request(method, target_url, **kwargs) as response:
                # 统计「实际读到的字节」= 响应头（状态行 + 各头部字段 + 结束空行）+ 已读响应体。
                # aiohttp 会自动解压 gzip/br，所以这是应用层真正收到的数据量，不是网线字节数；
                # 响应体仍按原样只读前 2048 字节，QPS / 响应时间口径与之前完全一致。
                header_bytes = len(f"HTTP/1.1 {response.status} {response.reason or ''}\r\n") + 2
                for key, value in response.raw_headers:
                    header_bytes += len(key) + len(value) + 4   # "Name: value\r\n"
                body_read = await response.content.read(2048)
                nbytes = header_bytes + len(body_read)
                resp_time = time.time() - req_start
                stats.add_success(response.status, resp_time, nbytes, uplink)
        except Exception as e:
            stats.add_failed(fail_reason(e))


def fail_reason(e: Exception) -> str:
    """失败原因归类：异常类型 + 系统错误码，避免 100 个代理产生 100 种文案。"""
    inner = getattr(e, "os_error", None)
    code = getattr(inner, "winerror", None) or getattr(inner, "errno", None)
    name = type(e).__name__
    return f"{name}(错误码 {code})" if code else name


async def run_test(url, proxy_list=(), *, concurrency=5, duration=30, timeout=10,
                   stats=None, on_progress=None, should_stop=None,
                   progress_interval=1.0, body_size=0, max_traffic=0) -> Stats:
    """跑一轮压测并返回 Stats。命令行与图形界面共用。

    参数：
        proxy_list      代理地址列表（空 = 用本机 IP）
        concurrency     最大并发连接数（= 命令行 -c）
        duration        测试时长秒（= -d）
        timeout         单请求超时秒（= -t）
        stats           外部传入的 Stats，中断时也能拿到部分结果
        on_progress     每 progress_interval 秒回调一次快照；它跑在事件循环里，
                        图形界面里请只往队列投递，别直接碰 tkinter
        should_stop     返回 True 就收尾结束（停止按钮 / Ctrl+C）
        body_size       每请求上传体字节数（>0 走 POST，= 0 走 GET）
        max_traffic     总流量上限（上行+下行），累计到该值就不再发新请求；
                        在途请求会收尾，所以实际值会略超上限
        duration        <=0 表示**不限时长**——设了 max_traffic 就靠它当唯一停止条件，
                        跑多久都行，跑到量为止
    """
    stats = stats if stats is not None else Stats()
    concurrency = max(1, int(concurrency))
    proxy_list = list(proxy_list or ())
    semaphore = asyncio.Semaphore(concurrency)
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    body = os.urandom(max(0, int(body_size))) if body_size else None   # 全程复用同一块
    max_traffic = max(0, int(max_traffic))

    async with aiohttp.ClientSession() as session:
        running_tasks = []
        # duration<=0 → 不设结束时刻（float("inf")），只由流量上限/停止信号收尾
        test_end_time = (stats.start_time + duration
                         if duration and duration > 0 else float("inf"))
        last_tick = 0.0
        try:
            while time.time() < test_end_time:
                iter_start = time.time()
                if should_stop is not None and should_stop():
                    break
                if max_traffic and (stats.ok_bytes + stats.sent_bytes) >= max_traffic:
                    break   # 流量到顶，不再发新请求（在途的下面统一收尾）

                # 动态提交新任务，避免一次性创建过多协程
                if len(running_tasks) < concurrency * 3:
                    current_proxy = random.choice(proxy_list) if proxy_list else None
                    running_tasks.append(asyncio.create_task(
                        send_request(session, url, current_proxy, stats,
                                     semaphore, timeout_config, body)))

                # 清理已完成的任务（0.1s 轮询，兼作停止/进度的响应点）
                if running_tasks:
                    _, pending = await asyncio.wait(
                        running_tasks, timeout=0.1,
                        return_when=asyncio.FIRST_COMPLETED)
                    running_tasks = list(pending)

                # 请求"瞬间失败"（代理地址非法、端口秒拒）时循环会疯狂空转，
                # 失败数和 QPS 全是虚的 —— 补一点间隔：不烧 CPU，数字也真实
                if time.time() - iter_start < 0.002:
                    await asyncio.sleep(0.02)

                current_time = time.time()
                if on_progress and current_time - last_tick >= progress_interval:
                    last_tick = current_time
                    on_progress(stats.snapshot())
        finally:
            # 等待所有正在运行的任务完成
            if running_tasks:
                await asyncio.gather(*running_tasks, return_exceptions=True)

    return stats


async def main():
    parser = argparse.ArgumentParser(description="可控HTTP应用层性能测试工具（仅用于合法授权场景）")
    parser.add_argument("url", help="目标服务器URL，例如 http://1.2.3.4:80/ 或 https://yourdomain.com/api")
    parser.add_argument("-p", "--proxies", help="代理列表文件路径，每行一个代理，支持http/https/socks5格式", default=None)
    parser.add_argument("-c", "--concurrency", type=int, help="最大并发连接数", default=5)
    parser.add_argument("-d", "--duration", type=int, default=None,
                        help="测试持续时间（秒）。给了 --max-traffic 时不填此项 = 不限时长，"
                             "跑到流量为止；显式填了才作为第二道保险（谁先到谁停）")
    parser.add_argument("-t", "--timeout", type=int, help="单个请求超时时间（秒）", default=10)
    parser.add_argument("-b", "--body-size", default="0",
                        help="每请求上传体大小（字节，支持 64k/2m/1g；0=不带体走 GET）")
    parser.add_argument("--max-traffic", default="0",
                        help="总流量上限（上行+下行，支持 500m/1g），到顶自动停；0=不限")
    args = parser.parse_args()

    # 参数合法性校验（流量类输入支持 64k/2m 这种写法）
    try:
        body_size = parse_bytes(args.body_size, "上传体大小")
        max_traffic = parse_bytes(args.max_traffic, "流量上限")
    except ValueError as e:
        print(f"❌ {e}")
        return
    if body_size > MAX_BODY_SIZE:
        print(f"❌ 单请求上传体最大 {fmt_bytes(MAX_BODY_SIZE)}（输入了 {fmt_bytes(body_size)}）")
        return

    # 加载代理列表
    proxy_list = []
    if args.proxies:
        try:
            proxy_list = load_proxies(args.proxies)
            if not proxy_list:
                print("⚠️  代理文件为空，将使用本机IP发起请求")
            else:
                print(f"✅ 已加载 {len(proxy_list)} 个代理节点")
        except Exception as e:
            print(f"❌ 加载代理文件失败: {str(e)}")
            return
    else:
        print("ℹ️  未指定代理文件，将使用本机IP直接发起请求")

    # 参数合法性校验
    if args.concurrency < 1:
        print("❌ 并发数必须大于等于1")
        return
    if args.timeout < 1:
        print("❌ 超时时间必须大于等于1秒")
        return
    if args.duration is not None and args.duration < 1:
        print("❌ 测试时长必须大于等于1秒")
        return
    # 设了流量上限又没显式给 -d → 不限时长：流量是唯一的停止条件，跑多久都行
    duration = (0 if max_traffic else 30) if args.duration is None else args.duration

    print(f"\n🚀 开始压测配置:")
    print(f"🎯 目标地址: {args.url}")
    print(f"🔀 并发数: {args.concurrency}")
    if duration == 0:
        print(f"⏰ 测试时长: 不限，直到总流量达到 {fmt_bytes(max_traffic)} 才停")
    elif max_traffic:
        print(f"⏰ 测试时长: {duration}秒（与流量上限 {fmt_bytes(max_traffic)} 谁先到谁停）")
    else:
        print(f"⏰ 测试时长: {duration}秒")
    print(f"⌛ 请求超时: {args.timeout}秒")
    if body_size:
        print(f"📦 每请求上传体: {fmt_bytes(body_size)}（POST）")
    if max_traffic:
        print(f"🛑 流量上限: {fmt_bytes(max_traffic)}（上行+下行，达到即停）")
    print(f"💡 提示: 按 Ctrl+C 可随时停止测试\n")

    stats = Stats()
    stopped = {"v": False}
    last_print = {"t": 0.0}

    # 处理停止信号
    def handle_stop_signal():
        print("\n\n🛑 收到停止指令，正在等待剩余请求完成...")
        stopped["v"] = True

    def on_progress(snap):
        now = time.time()
        if now - last_print["t"] >= 2:
            last_print["t"] = now
            if max_traffic:
                pct = min(100.0, snap["total_bytes"] / max_traffic * 100)
                seg = (f"流量: {fmt_bytes(snap['total_bytes'])}"
                       f"/{fmt_bytes(max_traffic)} ({pct:.1f}%)")
            else:
                seg = f"成功流量: {fmt_bytes(snap['ok_bytes'])}"
            up = f" | 上行: {fmt_bytes(snap['sent_bytes'])}" if body_size else ""
            print(
                f"\r⏱️  已运行 {snap['elapsed']:.0f}s | 已发送: {snap['total']} | "
                f"成功: {snap['success']} | 失败: {snap['failed']} | "
                f"{seg}{up} | 当前QPS: {snap['qps']:.1f}",
                end="",
            )

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(2, handle_stop_signal)  # 捕获Ctrl+C
    except (NotImplementedError, RuntimeError, OSError, ValueError):
        pass  # Windows环境下默认通过KeyboardInterrupt处理

    try:
        await run_test(
            args.url, proxy_list,
            concurrency=args.concurrency, duration=duration, timeout=args.timeout,
            stats=stats, on_progress=on_progress,
            should_stop=lambda: stopped["v"], progress_interval=2.0,
            body_size=body_size, max_traffic=max_traffic,
        )
    except KeyboardInterrupt:
        handle_stop_signal()

    if max_traffic and (stats.ok_bytes + stats.sent_bytes) >= max_traffic:
        print(f"\n🛑 总流量已达上限 {fmt_bytes(max_traffic)}，自动停止")
    stats.print_report()


if __name__ == "__main__":
    asyncio.run(main())
