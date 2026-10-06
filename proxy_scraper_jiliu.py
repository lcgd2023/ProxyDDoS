#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬取 https://www.jiliuip.com/free/ 上所有页面的免费代理 IP 与端口。

站点结构与分页规则（实测）：
  * 分页是路径式：第 1 页 `/free/`，第 N 页 `/free/page-N/`，每页 10 条；
  * 分页器给出总页数与总条数（实测「共 60815 条 / 6082 页」），超过末页返回 0 行，
    不会像某些站那样重定向回第 1 页，但脚本仍以分页器的 data-last-page 为准；
  * 地区筛选同样是路径：`/free/beijing/`、`/free/beijing/page-N/`（31 个地区）；
  * 没有 JSON 接口，页面由静态 HTML 直接返回；
  * robots.txt 只屏蔽 /docs/ 与 /api/，/free/ 允许抓取；连发 20 次全 200，无验证码；
  * 列表每小时刷新，页内和页间都存在重复 IP，必须去重。

注意：全量抓取是 6082 页，按默认 `--delay 0.4` 约需 50 分钟。
      站点有滚动窗口频率限制，实测：
        * 单进程 delay=0.4（约 2.5 页/秒）连续抓 2500 页零 403；
        * 两个进程叠加到约 4 次/秒就会开始 403 —— 所以**不要同时跑多个抓取进程**；
        * 一旦 403，限速器自动降速 1.5 倍（上限 4 倍基线）并冻结 8~20s，
          成功后每次按 3% 逐步恢复到基线；仍失败的页在最后自动补抓一轮；
        * `--workers` 只隐藏网络延迟，总频率上限始终是 1/--delay。

用法示例：
    python proxy_scraper_jiliu.py                       # 全站所有页 -> proxiesjiliu.csv + txt
    python proxy_scraper_jiliu.py --max-pages 10        # 只抓前 10 页
    python proxy_scraper_jiliu.py --region beijing --max-pages 5
    python proxy_scraper_jiliu.py --workers 4 --delay 0.15          # 全量提速
    python proxy_scraper_jiliu.py --validate --only-valid -o alivej

仅用于获取公开的免费代理列表，请遵守目标站点的使用条款并控制抓取频率。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.jiliuip.com"
FREE_PATH = "/free/"                 # 全国列表基础路径
PER_PAGE = 10

# 地区筛选路径段：中文名 -> slug（站点首页的 31 个地区按钮）
REGIONS = {
    "北京": "beijing", "天津": "tianjin", "河北": "hebei", "山西": "shanxi",
    "内蒙古": "neimenggu", "辽宁": "liaoning", "吉林": "jilin",
    "黑龙江": "heilongjiang", "上海": "shanghai", "江苏": "jiangsu",
    "浙江": "zhejiang", "安徽": "anhui", "福建": "fujian", "江西": "jiangxi",
    "山东": "shandong", "河南": "henan", "湖北": "hubei", "湖南": "hunan",
    "广东": "guangdong", "广西": "guangxi", "海南": "hainan",
    "重庆": "chongqing", "四川": "sichuan", "贵州": "guizhou", "云南": "yunnan",
    "西藏": "xizang", "陕西": "shaanxi", "甘肃": "gansu", "青海": "qinghai",
    "宁夏": "ningxia", "新疆": "xinjiang",
}
SLUG_SET = set(REGIONS.values())
SLUG_TO_NAME = {slug: name for name, slug in REGIONS.items()}

TYPE_VALUES = ["http", "https", "socks4", "socks5"]

DEFAULT_TEST_URL = "http://www.baidu.com"     # 站点主打国内代理，国内地址更可能可达
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LAST_PAGE_RE = re.compile(r"page-(\d+)")
TOTAL_RE = re.compile(r"共\s*([\d,]+)\s*条")

log = logging.getLogger("jiliuip")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Proxy:
    ip: str = ""
    port: str = ""
    anonymity: str = ""        # 高匿名 / 透明 等
    type: str = ""             # HTTP
    location: str = ""         # 位置，如 柬埔寨 / 北京市
    response: str = ""         # 响应速度（秒）
    last_verified: str = ""    # 最后验证时间
    valid: str = ""            # --validate 之后才有值：yes / no

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"


CSV_COLUMNS = ["ip", "port", "anonymity", "type", "location", "response",
               "last_verified", "valid"]

IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def is_ip(value: str) -> bool:
    if not IP_RE.fullmatch(value):
        return False
    return all(int(octet) <= 255 for octet in value.split("."))


# --------------------------------------------------------------------------- #
# 全局限速器（多线程共享，保证无论几个线程都按 --delay 控制总频率）
# 自适应：遇到 403 自动拉长间隔并冻结一段时间，成功后逐步降回基线
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, delay: float) -> None:
        self.base = max(0.0, delay)
        self.delay = self.base
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        delay = self.delay
        if delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + delay + random.uniform(0, delay * 0.25)
        pause = start - now
        if pause > 0:
            time.sleep(pause)

    def penalize(self, cooldown: float = 10.0, reason: str = "403") -> None:
        """被限流：间隔×1.5（上限 4 倍基线或 2s），并冻结 cooldown 秒。"""
        with self._lock:
            self.delay = min(self.delay * 1.5, max(self.base * 4, 2.0))
            frozen = time.monotonic() + max(0.0, cooldown)
            self._next = max(self._next, frozen)
        log.warning("限流(%s)：自动降速到 %.2fs/请求，冷却 %.0fs", reason, self.delay, cooldown)

    def reward(self) -> None:
        """请求成功：间隔按 3% 逐步降回基线。"""
        with self._lock:
            if self.delay > self.base:
                self.delay = max(self.base, self.delay * 0.97)


# --------------------------------------------------------------------------- #
# 抓取器
# --------------------------------------------------------------------------- #
class JiliuScraper:
    """按页抓取 jiliuip.com 免费代理列表，支持地区筛选、限速、多线程与去重。"""

    def __init__(
        self,
        region: str = "",
        delay: float = 0.4,
        timeout: float = 15.0,
        retries: int = 3,
        workers: int = 1,
    ) -> None:
        self.region = self._normalize_region(region)
        self.timeout = timeout
        self.retries = retries
        self.workers = max(1, workers)
        self._limiter = RateLimiter(delay)
        self._local = threading.local()          # 每线程一个 Session
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()

    # -- 地区与地址 --------------------------------------------------------- #
    @staticmethod
    def _normalize_region(region: str) -> str:
        region = (region or "").strip().lower()
        if not region or region in {"all", "全国"}:
            return ""
        if region in REGIONS:                    # 中文名，如 北京
            return REGIONS[region]
        return region

    @property
    def base_path(self) -> str:
        """列表基础路径：`/free/` 或 `/free/beijing/`（始终以 / 结尾）。"""
        if self.region:
            return f"{FREE_PATH}{self.region}/"
        return FREE_PATH

    def page_path(self, page: int) -> str:
        return self.base_path if page <= 1 else f"{self.base_path}page-{page}/"

    # -- 请求 --------------------------------------------------------------- #
    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": BASE_URL + FREE_PATH,
            })
            self._local.session = sess
        return sess

    def _get(self, page: int) -> str:
        """抓一页 HTML，带全局限速与指数退避重试。"""
        url = BASE_URL + self.page_path(page)
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._limiter.wait()
            try:
                resp = self._session().get(url, timeout=self.timeout)
                resp.raise_for_status()
                resp.encoding = "utf-8"          # 页面声明 charset=utf-8，显式指定防乱码
                self._limiter.reward()           # 成功一次就把间隔往基线收
                return resp.text
            except requests.RequestException as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 403:
                    # 站点滚动配额：交给限速器自动降速 + 冷却，这里只做短等待后重试
                    self._limiter.penalize(
                        cooldown=8 + 4 * attempt,
                        reason=f"403 {attempt}/{self.retries} {self.page_path(page)}",
                    )
                    time.sleep(1.0 + random.uniform(0, 1))
                else:
                    wait = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                    log.warning("请求失败(%s/%s) %s: %s，%.1fs 后重试",
                                attempt, self.retries, self.page_path(page), exc, wait)
                    time.sleep(wait)
        raise RuntimeError(f"请求失败（已重试 {self.retries} 次）: {url}") from last_exc

    # -- 解析 --------------------------------------------------------------- #
    @staticmethod
    def _parse(html: str) -> tuple[list[Proxy], int, int]:
        """返回 (本页数据, 总页数, 总条数)。"""
        soup = BeautifulSoup(html, "html.parser")

        # 总页数：优先取跳页框的 data-last-page，其次分页器「末页」链接
        total_pages = 1
        jump = soup.select_one("input[data-jump-mode]")
        if jump and (jump.get("data-last-page") or "").isdigit():
            total_pages = int(jump.get("data-last-page"))
        else:
            last = soup.select_one(".v2-free-pagination__last")
            if last:
                m = LAST_PAGE_RE.search(last.get("href") or "")
                if m:
                    total_pages = int(m.group(1))

        # 总条数：分页器里的「共 60815 条」
        total_records = 0
        pager = soup.select_one(".v2-free-pagination")
        if pager:
            m = TOTAL_RE.search(pager.get_text(" ", strip=True))
            if m:
                total_records = int(m.group(1).replace(",", ""))

        rows: list[Proxy] = []
        for tr in soup.select(".v2-free-table__row"):
            cells = tr.select("[role=cell]")
            if len(cells) < 2:
                continue
            text = [c.get_text(" ", strip=True) for c in cells]
            text += [""] * (7 - len(text))
            ip, port = text[0], text[1]
            if not is_ip(ip) or not port.isdigit():
                continue
            rows.append(Proxy(
                ip=ip,
                port=port,
                anonymity=text[2],
                type=text[3],
                location=text[4],
                response=text[5],
                last_verified=text[6],
            ))
        return rows, total_pages, total_records

    def _fetch_page(self, page: int) -> tuple[int, list[Proxy], int, int]:
        html = self._get(page)
        rows, total_pages, total_records = self._parse(html)
        if not rows:
            # 中途出现空页多半是被软性拦截（HTTP 200 但没数据），稍等重试一次
            log.warning("第 %s 页解析出 0 条，3s 后重试一次", page)
            time.sleep(3)
            html = self._get(page)
            rows, total_pages, total_records = self._parse(html)
        return page, rows, total_pages, total_records

    # -- 抓取流程 ----------------------------------------------------------- #
    def crawl(self, max_pages: int = 0, limit: int = 0) -> list[Proxy]:
        """max_pages=0 表示抓完分页器给出的所有页，limit>0 达到即停。"""
        t0 = time.monotonic()

        # 第 1 页先单独抓，用它确定总页数
        _, rows, total_pages, total_records = self._fetch_page(1)
        target = total_pages if not max_pages else min(total_pages, max_pages)
        if self.region and self.region not in SLUG_SET:
            log.warning("地区 slug %r 不在已知列表内，站点可能返回空页", self.region)
        log.info("列表 %s：站点共 %s 条 / %s 页，本次抓 %s 页（每页 %s 条）",
                 self.page_path(1), f"{total_records:,}" if total_records else "?",
                 total_pages, target, PER_PAGE)
        if target > 500:
            rate = 1.0 / max(self._limiter.delay, 0.05)
            eta = target / max(rate, 1)
            log.info("全量抓取预计 %.0f 分钟（delay=%.2fs，workers=%s，上限约 %.1f 页/秒）；"
                     "触发 403 会自动降速冷却，请勿同时运行多个抓取进程",
                     eta / 60, self._limiter.delay, self.workers, rate)

        results: dict[int, list[Proxy]] = {}
        failed: list[int] = []
        done = 0

        def handle(page: int, page_rows: list[Proxy]) -> bool:
            """登记一页结果；返回 False 表示已达到 --limit，可以停了。"""
            nonlocal done
            results[page] = page_rows
            done += 1
            step = max(1, target // 50)
            if done % step == 0 or done == target:
                unique = len({k for rows_ in results.values() for k in
                              ((r.ip, r.port) for r in rows_)})
                elapsed = time.monotonic() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (target - done) / rate if rate else 0
                log.info("进度 %s/%s 页（%.1f%%），唯一代理 %s 条，"
                         "耗时 %.0fs，预计还需 %.0f 分钟",
                         done, target, done * 100.0 / target, f"{unique:,}",
                         elapsed, eta / 60)
            return not (limit and self._count_unique(results) >= limit)

        handle(1, rows)
        if target <= 1 or not rows or (limit and self._count_unique(results) >= limit):
            return self._ordered(results, limit)

        pages = list(range(2, target + 1))
        if self.workers == 1:
            stopped = False
            for page in pages:
                try:
                    _, page_rows, _, _ = self._fetch_page(page)
                except Exception as exc:
                    log.warning("第 %s 页失败：%s", page, exc)
                    failed.append(page)
                    continue
                if not handle(page, page_rows):
                    stopped = True
                    break
            if not stopped and not limit:
                log.info("已抓完最后一页（共 %s 页）", target)
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self._fetch_page, p): p for p in pages}
                stop = False
                for fut in as_completed(futures):
                    page = futures[fut]
                    try:
                        _, page_rows, _, _ = fut.result()
                    except Exception as exc:
                        log.warning("第 %s 页失败：%s", page, exc)
                        failed.append(page)
                        done += 1
                        continue
                    if not handle(page, page_rows):
                        stop = True
                        break
                if stop:
                    pool.shutdown(wait=False, cancel_futures=True)

        # 失败页补抓一次（通常是限流，隔一会儿配额就恢复了）
        if failed and (not limit or self._count_unique(results) < limit):
            log.warning("共 %s 页失败，开始补抓：%s", len(failed), failed[:20])
            for page in sorted(failed):
                try:
                    _, page_rows, _, _ = self._fetch_page(page)
                except Exception as exc:
                    log.error("第 %s 页补抓仍失败：%s", page, exc)
                    continue
                results[page] = page_rows

        return self._ordered(results, limit)

    @staticmethod
    def _count_unique(results: dict[int, list[Proxy]]) -> int:
        seen: set[tuple[str, str]] = set()
        for page in sorted(results):
            for r in results[page]:
                seen.add((r.ip, r.port))
        return len(seen)

    @staticmethod
    def _ordered(results: dict[int, list[Proxy]], limit: int = 0) -> list[Proxy]:
        """按页码顺序输出并跨页去重（并发抓取时结果到达顺序是乱的）。"""
        seen: set[tuple[str, str]] = set()
        out: list[Proxy] = []
        for page in sorted(results):
            for r in results[page]:
                key = (r.ip, r.port)
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
                if limit and len(out) >= limit:
                    return out
        return out


# --------------------------------------------------------------------------- #
# 可用性验证（可选）
# --------------------------------------------------------------------------- #
def _check_one(proxy: Proxy, scheme: str, test_url: str, timeout: float) -> bool:
    candidates = [f"{scheme}://{proxy.ip}:{proxy.port}"]
    if scheme == "https":
        candidates.append(f"http://{proxy.ip}:{proxy.port}")
    for purl in candidates:
        try:
            resp = requests.get(
                test_url,
                proxies={"http": purl, "https": purl},
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            return resp.status_code in (200, 204)
        except ValueError:
            continue        # 代理端口没开 TLS，换下一个候选地址
        except requests.RequestException:
            return False
    return False


def validate(proxies: list[Proxy], workers: int, timeout: float,
             test_url: str, scheme: str) -> None:
    """并发实测每个代理，把结果写回 proxy.valid（yes/no）。"""
    log.info("开始验证 %s 个代理（%s 线程，超时 %.1fs，scheme=%s）",
             len(proxies), workers, timeout, scheme)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_one, p, scheme, test_url, timeout): p for p in proxies}
        for fut in as_completed(futures):
            proxy = futures[fut]
            try:
                proxy.valid = "yes" if fut.result() else "no"
            except Exception as exc:   # 单个代理的怪异异常不该中断整轮验证
                log.debug("验证 %s 出错: %s", proxy.address, exc)
                proxy.valid = "no"
            done += 1
            if done % 200 == 0:
                log.info("已验证 %s/%s", done, len(proxies))


# --------------------------------------------------------------------------- #
# 结果输出
# --------------------------------------------------------------------------- #
def save(proxies: list[Proxy], base: str, formats: list[str]) -> list[Path]:
    stem = Path(base)
    stem.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if "csv" in formats:
        path = stem.with_suffix(".csv")
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for p in proxies:
                writer.writerow({k: getattr(p, k) for k in CSV_COLUMNS})
        written.append(path)

    if "txt" in formats:
        path = stem.with_suffix(".txt")
        path.write_text("\n".join(p.address for p in proxies) + "\n", encoding="utf-8")
        written.append(path)

    if "json" in formats:
        path = stem.with_suffix(".json")
        path.write_text(
            json.dumps([asdict(p) for p in proxies], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(path)

    return written


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="爬取 jiliuip.com 免费代理列表所有页面的 IP 与端口",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--region", default="",
                        help="地区筛选，slug 或中文名：beijing/广东…，空 = 全国")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="最多抓几页，0 表示抓完分页器给出的全部页")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多收集多少条，0 表示不限制")
    parser.add_argument("-o", "--output", default="proxiesjiliu",
                        help="输出文件名（不含扩展名）")
    parser.add_argument("--formats", default="csv,txt",
                        help="输出格式，逗号分隔：csv,txt,json")

    req = parser.add_argument_group("请求控制")
    req.add_argument("--delay", type=float, default=0.4,
                     help="两次请求之间的最小间隔（秒，多线程共享同一限速器）")
    req.add_argument("--workers", type=int, default=1,
                     help="并发抓取线程数，>1 时总频率仍受 --delay 约束")
    req.add_argument("--timeout", type=float, default=15.0, help="单次请求超时（秒）")
    req.add_argument("--retries", type=int, default=3, help="失败重试次数")

    val = parser.add_argument_group("可用性验证")
    val.add_argument("--validate", action="store_true",
                     help="抓取后并发实测代理可用性（较耗时）")
    val.add_argument("--validate-scheme", default="http",
                     choices=TYPE_VALUES,
                     help="验证时代理协议（列表本身标注为 HTTP）")
    val.add_argument("--validate-workers", type=int, default=50, help="验证并发线程数")
    val.add_argument("--validate-timeout", type=float, default=6.0,
                     help="单个代理验证超时（秒）")
    val.add_argument("--validate-url", default=DEFAULT_TEST_URL,
                     help="验证时访问的测试地址")
    val.add_argument("--only-valid", action="store_true",
                     help="仅输出验证通过的代理（需配合 --validate）")

    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，改成 UTF-8 以免日志里的中文变乱码
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            reconf(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    scraper = JiliuScraper(
        region=args.region,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        workers=args.workers,
    )

    try:
        proxies = scraper.crawl(max_pages=args.max_pages, limit=args.limit)
    except (RuntimeError, requests.RequestException) as exc:
        log.error("%s", exc)
        return 1

    if not proxies:
        log.warning("没有抓到任何代理")
        return 0
    log.info("抓取完成，去重后共 %s 条", f"{len(proxies):,}")

    if args.validate:
        validate(proxies, args.validate_workers, args.validate_timeout,
                 args.validate_url, args.validate_scheme)
        alive = sum(1 for p in proxies if p.valid == "yes")
        log.info("验证完成：可用 %s / 共 %s", alive, len(proxies))
        if args.only_valid:
            proxies = [p for p in proxies if p.valid == "yes"]

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    unknown = set(formats) - {"csv", "txt", "json"}
    if unknown:
        log.error("不支持的输出格式: %s", ", ".join(sorted(unknown)))
        return 2

    written = save(proxies, args.output, formats)
    for path in written:
        log.info("已写出 %s（%s 条）", path, f"{len(proxies):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
