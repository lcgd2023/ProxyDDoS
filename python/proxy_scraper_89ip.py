#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬取 https://www.89ip.cn/ 上的免费代理 IP 与端口。

数据源（--source）：
    extract  调用站点“代理IP提取”接口 https://www.89ip.cn/tqdl.html（默认），
             支持 数量/地区/排除地区/端口/排除端口/运营商 六个筛选参数，
             一次请求即可拿到全库（约 4400 条）ip:port，最快也最全；
             缺点是只有 IP 和端口，没有位置、运营商等附加字段。
    page     逐页抓取列表页 https://www.89ip.cn/index_N.html（40 条/页），
             除 IP、端口外还能拿到 代理位置、运营商、录取时间；
             注意：站内列表按“录取时间”排序，而所有记录的时间戳几乎相同，
             导致分页不稳定、相邻页大量重复（实测相邻两页 40 条里有 25 条重合），
             全量抓取会有遗漏，需要完整清单请用 extract 源。

用法示例：
    python proxy_scraper_89ip.py                          # 一次拿全库 -> proxies89.csv + txt
    python proxy_scraper_89ip.py --source page            # 列表页逐页抓（带位置/运营商）
    python proxy_scraper_89ip.py --source page --max-pages 0
    python proxy_scraper_89ip.py --num 200 --address 安徽 --isp 电信
    python proxy_scraper_89ip.py --num 1000 --exclude-address 台湾 --exclude-port 8089
    python proxy_scraper_89ip.py --validate --only-valid -o alive89

仅用于获取公开的免费代理列表，请遵守目标站点的使用条款并控制抓取频率。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.89ip.cn"
LIST_URL = BASE_URL + "/"                    # 第 1 页
LIST_PAGE_URL = BASE_URL + "/index_{n}.html"  # 第 n>=2 页
EXTRACT_URL = BASE_URL + "/tqdl.html"        # 代理IP提取接口

PER_PAGE = 40                                 # 列表页固定每页 40 条
MAX_EXTRACT_NUM = 5000                        # 提取接口单次数量上限（超出按站点库存返回）

DEFAULT_TEST_URL = "http://www.baidu.com"   # 池子里多为国内代理，测试地址选国内可达的
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
PAIR_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")

log = logging.getLogger("ip89")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Proxy:
    ip: str
    port: str
    location: str = ""        # 代理位置（仅 page 源）
    isp: str = ""             # 运营商（仅 page 源）
    acquired_at: str = ""     # 录取时间（仅 page 源）
    valid: str = ""           # --validate 之后才有值：yes / no

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"


CSV_COLUMNS = ["ip", "port", "location", "isp", "acquired_at", "valid"]


def is_ip(value: str) -> bool:
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value):
        return False
    return all(int(octet) <= 255 for octet in value.split("."))


# --------------------------------------------------------------------------- #
# 抓取器
# --------------------------------------------------------------------------- #
class I89Scraper:
    """按页或按提取接口抓取 89ip 代理，内置限速、重试与去重。"""

    def __init__(
        self,
        source: str = "extract",
        num: int = 0,
        rounds: int = 1,
        address: str = "",
        exclude_address: str = "",
        port: str = "",
        exclude_port: str = "",
        isp: str = "",
        delay: float = 0.5,
        timeout: float = 15.0,
        retries: int = 3,
    ) -> None:
        self.source = source
        self.num = num
        self.rounds = rounds
        self.address = address
        self.exclude_address = exclude_address
        self.port = port
        self.exclude_port = exclude_port
        self.isp = isp
        self.delay = delay
        self.timeout = timeout
        self.retries = retries

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Referer": f"{BASE_URL}/ti.html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self._seen: set[tuple[str, str]] = set()
        self._last_request = 0.0

    # -- 基础请求 ---------------------------------------------------------- #
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed + random.uniform(0, 0.2))
        self._last_request = time.monotonic()

    def _get(self, url: str, params: dict | None = None) -> str:
        """返回页面 HTML（已按 UTF-8 解码）。"""
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                resp.encoding = "utf-8"   # 站点 charset=utf-8，显式指定避免中文乱码
                return resp.text
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                log.warning("请求失败(%s/%s) %s: %s，%.1fs 后重试",
                            attempt, self.retries, url, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"请求失败（已重试 {self.retries} 次）: {url}") from last_exc

    # -- 解析 -------------------------------------------------------------- #
    @staticmethod
    def _parse_list_page(html: str) -> list[Proxy]:
        """解析列表页的 layui-table（IP / 端口号 / 代理位置 / 运营商 / 录取时间）。"""
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="layui-table")
        if table is None:
            return []
        result: list[Proxy] = []
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            cells = [td.get_text(" ", strip=True) for td in tds[:5]]
            ip, port = cells[0], cells[1]
            if not is_ip(ip) or not port.isdigit():
                continue
            result.append(
                Proxy(ip=ip, port=port, location=cells[2],
                      isp=cells[3], acquired_at=cells[4])
            )
        return result

    @staticmethod
    def _parse_total_ips(html: str) -> int:
        """侧栏 'IP总量：4499个'，用来估算总页数。"""
        m = re.search(r"IP总量：\s*([\d,]+)\s*个", html)
        return int(m.group(1).replace(",", "")) if m else 0

    @staticmethod
    def _parse_extract_page(html: str) -> list[Proxy]:
        """解析“提取结果”区块里的 ip:port 行，忽略页面其余部分的广告与统计信息。"""
        start = html.find("提取结果")
        end = html.find("更好用的代理ip")   # 结果列表结尾的推广文案
        segment = html[start:end] if start != -1 and end > start else html
        return [Proxy(ip=ip, port=port) for ip, port in PAIR_RE.findall(segment)]

    # -- 两种抓取流程 -------------------------------------------------------- #
    def _iter_page(self) -> Iterator[list[Proxy]]:
        """逐页 yield 代理；页码 1 用首页，其余用 index_N.html。"""
        page = 1
        estimated_pages = 0
        while True:
            url = LIST_URL if page == 1 else LIST_PAGE_URL.format(n=page)
            html = self._get(url)
            if page == 1:
                total_ips = self._parse_total_ips(html)
                if total_ips:
                    estimated_pages = math.ceil(total_ips / PER_PAGE) + 1
                    log.info("站点统计 IP 总量约 %s 个，估算共 %s 页", total_ips, estimated_pages)
            rows = self._parse_list_page(html)
            log.info("第 %s 页%s：本页 %s 条",
                     page, f"/约{estimated_pages}" if estimated_pages else "", len(rows))
            yield rows
            if not rows or (estimated_pages and page >= estimated_pages):
                break
            page += 1

    def _iter_extract(self) -> Iterator[list[Proxy]]:
        """按提取接口分轮获取，每轮 yield 一次。"""
        params: dict = {"num": self.num or MAX_EXTRACT_NUM}
        for key, value in (
            ("address", self.address),
            ("kill_address", self.exclude_address),
            ("port", self.port),
            ("kill_port", self.exclude_port),
            ("isp", self.isp),
        ):
            if value:
                params[key] = value
        for rnd in range(1, self.rounds + 1):
            html = self._get(EXTRACT_URL, params)
            rows = self._parse_extract_page(html)
            log.info("提取接口第 %s/%s 轮：本批 %s 条（参数 %s）",
                     rnd, self.rounds, len(rows), params)
            yield rows
            if not rows:
                break

    # -- 对外入口 ------------------------------------------------------------ #
    def crawl(self, max_pages: int = 0, limit: int = 0) -> list[Proxy]:
        """max_pages=0 表示抓到底（page 源受估算页数/空页约束），limit>0 达到即停。"""
        proxies: list[Proxy] = []
        page_no = 0
        iterator = self._iter_page() if self.source == "page" else self._iter_extract()
        for page_no, rows in enumerate(iterator, start=1):
            for item in rows:
                key = (item.ip, item.port)
                if key in self._seen:
                    continue
                self._seen.add(key)
                proxies.append(item)
                if limit and len(proxies) >= limit:
                    log.info("已达 --limit %s，停止抓取", limit)
                    return proxies
            # 空页/空批次由生成器内部处理（page 源还会撞上估算页数上限）
            if max_pages and page_no >= max_pages and rows:
                log.info("已达 --max-pages %s，停止抓取", max_pages)
                break
        else:
            log.info("数据源已抓完（共 %s 页/轮）", page_no)
        return proxies


# --------------------------------------------------------------------------- #
# 可用性验证（可选）
# --------------------------------------------------------------------------- #
def _check_one(proxy: Proxy, scheme: str, test_url: str, timeout: float) -> bool:
    """89ip 不给协议字段，用 --validate-scheme 指定（默认 http）。"""
    purl = f"{scheme}://{proxy.ip}:{proxy.port}"
    candidates = [purl] if scheme != "https" else [purl, f"http://{proxy.ip}:{proxy.port}"]
    for url in candidates:
        try:
            resp = requests.get(
                test_url,
                proxies={"http": url, "https": url},
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
        description="爬取 89ip.cn 上的免费代理 IP 与端口",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", choices=["extract", "page"], default="extract",
                        help="数据源：extract=代理提取接口(全量/可筛选), page=逐页解析列表页")
    parser.add_argument("--max-pages", type=int, default=1,
                        help="最多抓几页/几轮，0 表示抓到底")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多收集多少条，0 表示不限制")
    parser.add_argument("-o", "--output", default="proxies89",
                        help="输出文件名（不含扩展名）")
    parser.add_argument("--formats", default="csv,txt",
                        help="输出格式，逗号分隔：csv,txt,json")

    ext = parser.add_argument_group("提取接口参数（--source extract 有效）")
    ext.add_argument("--num", type=int, default=0,
                     help=f"每次提取数量，0 表示一次取全库（上限 {MAX_EXTRACT_NUM}）")
    ext.add_argument("--rounds", type=int, default=1, help="提取轮数（多轮取并集）")
    ext.add_argument("--address", default="", help="地区，如 安徽")
    ext.add_argument("--exclude-address", default="", help="排除地区，如 台湾")
    ext.add_argument("--port", default="", help="只取指定端口，如 8089")
    ext.add_argument("--exclude-port", default="", help="排除指定端口")
    ext.add_argument("--isp", default="", help="运营商，如 电信")

    req = parser.add_argument_group("请求控制")
    req.add_argument("--delay", type=float, default=0.5,
                     help="两次请求之间的最小间隔（秒）")
    req.add_argument("--timeout", type=float, default=15.0, help="单次请求超时（秒）")
    req.add_argument("--retries", type=int, default=3, help="失败重试次数")

    val = parser.add_argument_group("可用性验证")
    val.add_argument("--validate", action="store_true",
                     help="抓取后并发实测代理可用性（较耗时）")
    val.add_argument("--validate-scheme", default="http",
                     choices=["http", "https", "socks4", "socks5"],
                     help="验证时代理的协议（89ip 不提供协议字段）")
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

    scraper = I89Scraper(
        source=args.source,
        num=args.num,
        rounds=args.rounds,
        address=args.address,
        exclude_address=args.exclude_address,
        port=args.port,
        exclude_port=args.exclude_port,
        isp=args.isp,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
    )

    try:
        proxies = scraper.crawl(max_pages=args.max_pages, limit=args.limit)
    except (RuntimeError, requests.RequestException) as exc:
        log.error("%s", exc)
        return 1

    if not proxies:
        log.warning("没有抓到任何代理")
        return 0
    log.info("抓取完成，去重后共 %s 条", len(proxies))

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
        log.info("已写出 %s（%s 条）", path, len(proxies))
    return 0


if __name__ == "__main__":
    sys.exit(main())
