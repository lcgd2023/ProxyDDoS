#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬取 https://proxy.scdn.io/ 上的免费代理 IP 与端口。

数据源（--source）：
    api   调用站点自身的 get_proxies.php 接口，返回 JSON（内含表格 HTML 片段），
          能拿到 IP / 端口 / 协议 / 国家 / 响应时间 / 最后验证时间，默认且最快。
    page  直接请求列表页 https://proxy.scdn.io/?page=N&per_page=M 并解析静态 HTML，
          作为接口变更时的兜底方案。
    text  请求 https://proxy.scdn.io/text.php，一次性拿到全部 "IP:端口" 纯文本，
          只有 IP 和端口，没有协议等附加字段。

用法示例：
    python proxy_scraper.py                          # 抓 1 页(100条) -> proxies.csv + proxies.txt
    python proxy_scraper.py --max-pages 0            # 抓全部页（约 3.5 万条）
    python proxy_scraper.py --protocol SOCKS5 --country 中国 --max-pages 5
    python proxy_scraper.py --source text -o all     # 纯文本全量列表
    python proxy_scraper.py --validate --limit 500   # 抓完再并发实测可用性

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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://proxy.scdn.io"
API_URL = f"{BASE_URL}/get_proxies.php"   # GET page,per_page,protocol,country -> JSON
LIST_URL = f"{BASE_URL}/"                 # GET page,per_page,protocol,country -> HTML
TEXT_URL = f"{BASE_URL}/text.php"         # 全量 "IP:端口" 纯文本

DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log = logging.getLogger("proxy_scraper")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Proxy:
    ip: str
    port: str
    protocol: str = ""
    country: str = ""
    latency: str = ""
    last_checked: str = ""
    valid: str = ""          # --validate 之后才会有值：yes / no

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"


CSV_COLUMNS = ["ip", "port", "protocol", "country", "latency", "last_checked", "valid"]


# --------------------------------------------------------------------------- #
# 抓取器
# --------------------------------------------------------------------------- #
class ProxyScraper:
    """负责按页抓取代理数据，内置重试、限速与去重。"""

    def __init__(
        self,
        source: str = "api",
        protocol: str = "",
        country: str = "",
        per_page: int = 100,
        delay: float = 0.5,
        timeout: float = 15.0,
        retries: int = 3,
    ) -> None:
        self.source = source
        self.protocol = protocol
        self.country = country
        self.per_page = per_page
        self.delay = delay
        self.timeout = timeout
        self.retries = retries

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"{BASE_URL}/",
                "X-Requested-With": "XMLHttpRequest",
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

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                # 站点声明 charset=UTF-8，这里强制一次，避免 requests 猜错编码导致中文乱码
                resp.encoding = "utf-8"
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                log.warning("请求失败(%s/%s) %s: %s，%.1fs 后重试",
                            attempt, self.retries, url, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"请求失败（已重试 {self.retries} 次）: {url}") from last_exc

    # -- 解析 -------------------------------------------------------------- #
    @staticmethod
    def _parse_table_html(table_html: str) -> list[Proxy]:
        """解析 get_proxies.php 返回的 <tr> 片段（或列表页中的 tbody）。"""
        soup = BeautifulSoup(table_html, "html.parser")
        result: list[Proxy] = []
        for row in soup.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 5:
                continue
            ip = tds[0].get_text(strip=True)
            port = tds[1].get_text(strip=True)
            if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) or not port.isdigit():
                continue
            badge = tds[2].find("span") or tds[2]
            result.append(
                Proxy(
                    ip=ip,
                    port=port,
                    protocol=badge.get_text(strip=True),
                    country=tds[3].get_text(strip=True),
                    latency=tds[4].get_text(strip=True),
                    last_checked=tds[5].get_text(strip=True) if len(tds) > 5 else "",
                )
            )
        return result

    @staticmethod
    def _parse_total_pages(pagination_html: str, fallback: int = 0) -> int:
        """从 '第 1 / 3519 页' 或末页链接里取出总页数。"""
        m = re.search(r"/\s*(\d+)\s*页", pagination_html)
        if m:
            return int(m.group(1))
        m = re.search(r"page=(\d+)", pagination_html)
        return int(m.group(1)) if m else fallback

    def _params(self, page: int) -> dict:
        return {
            "page": page,
            "per_page": self.per_page,
            "protocol": self.protocol,
            "country": self.country,
        }

    # -- 各数据源 ----------------------------------------------------------- #
    def _iter_api(self) -> Iterator[tuple[list[Proxy], int, int]]:
        """yield (本页数据, 当前页, 总页数)。"""
        page = 1
        total_pages = 1
        while True:
            resp = self._get(API_URL, self._params(page))
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(f"接口返回的不是 JSON: {resp.text[:200]}") from exc
            rows = self._parse_table_html(data.get("table_html", ""))
            total_pages = int(data.get("totalPages") or self._parse_total_pages(
                data.get("pagination_html", ""), fallback=page))
            log.info("api 第 %s/%s 页，本页 %s 条", page, total_pages, len(rows))
            yield rows, page, total_pages
            if page >= total_pages or not rows:
                break
            page += 1

    def _iter_page(self) -> Iterator[tuple[list[Proxy], int, int]]:
        page = 1
        total_pages = 1
        while True:
            resp = self._get(LIST_URL, self._params(page))
            soup = BeautifulSoup(resp.text, "html.parser")
            tbody = soup.select_one("#proxyTableBody")
            rows = self._parse_table_html(str(tbody)) if tbody else []
            info = soup.select_one("#pagination-container")
            total_pages = self._parse_total_pages(str(info) if info else "", fallback=page)
            log.info("page 第 %s/%s 页，本页 %s 条", page, total_pages, len(rows))
            yield rows, page, total_pages
            if page >= total_pages or not rows:
                break
            page += 1

    def _iter_text(self) -> Iterator[tuple[list[Proxy], int, int]]:
        resp = self._get(TEXT_URL)
        rows = []
        for line in resp.text.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            ip, _, port = line.rpartition(":")
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) and port.isdigit():
                rows.append(Proxy(ip=ip, port=port))
        log.info("text 共 %s 条", len(rows))
        yield rows, 1, 1

    # -- 对外入口 ------------------------------------------------------------ #
    def crawl(self, max_pages: int = 0, limit: int = 0) -> list[Proxy]:
        """抓取代理；max_pages=0 表示抓全部，limit>0 表示达到该数量即停止。"""
        iters = {"api": self._iter_api, "page": self._iter_page, "text": self._iter_text}
        proxies: list[Proxy] = []

        for rows, page, total_pages in iters[self.source]():
            for item in rows:
                key = (item.ip, item.port)
                if key in self._seen:
                    continue
                self._seen.add(key)
                proxies.append(item)
                if limit and len(proxies) >= limit:
                    log.info("已达 --limit %s，停止抓取", limit)
                    return proxies
            if max_pages and page >= max_pages:
                log.info("已达 --max-pages %s，停止抓取", max_pages)
                break
        return proxies


# --------------------------------------------------------------------------- #
# 可用性验证（可选）
# --------------------------------------------------------------------------- #
_SCHEME = {"HTTP": "http", "HTTPS": "https", "SOCKS4": "socks4", "SOCKS5": "socks5"}


def _check_one(proxy: Proxy, test_url: str, timeout: float) -> bool:
    scheme = _SCHEME.get(proxy.protocol.upper(), "http")
    # 标 HTTPS 的免费代理大多只是“支持 CONNECT 隧道”，代理端口本身并不加密，
    # 先按 https:// 连，失败（代理没开 TLS）再回退 http://。
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
            # 代理端口没开 TLS，换下一个候选地址
            continue
        except requests.RequestException:
            return False
    return False


def validate(proxies: list[Proxy], workers: int, timeout: float, test_url: str) -> None:
    """并发实测每个代理，把结果写回 proxy.valid（yes/no）。"""
    log.info("开始验证 %s 个代理（%s 线程，超时 %.1fs）", len(proxies), workers, timeout)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_one, p, test_url, timeout): p for p in proxies}
        for fut in as_completed(futures):
            proxy = futures[fut]
            try:
                proxy.valid = "yes" if fut.result() else "no"
            except Exception as exc:  # 单个代理的怪异异常不该中断整轮验证
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
        path.write_text(
            "\n".join(p.address for p in proxies) + "\n", encoding="utf-8"
        )
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
        description="爬取 proxy.scdn.io 上的免费代理 IP 与端口",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", choices=["api", "page", "text"], default="api",
                        help="数据源：api=JSON接口, page=解析列表页, text=纯文本全量")
    parser.add_argument("--protocol", default="",
                        choices=["", "HTTP", "HTTPS", "SOCKS4", "SOCKS5"],
                        help="按协议筛选（api/page 源有效）")
    parser.add_argument("--country", default="",
                        help="按国家/地区筛选，如 中国、香港（api/page 源有效）")
    parser.add_argument("--per-page", type=int, default=100, choices=[10, 30, 100],
                        help="每页条数")
    parser.add_argument("--max-pages", type=int, default=1,
                        help="最多抓几页，0 表示全部")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多收集多少条，0 表示不限制")
    parser.add_argument("-o", "--output", default="proxies",
                        help="输出文件名（不含扩展名）")
    parser.add_argument("--formats", default="csv,txt",
                        help="输出格式，逗号分隔：csv,txt,json")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="两次请求之间的最小间隔（秒）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时（秒）")
    parser.add_argument("--retries", type=int, default=3, help="失败重试次数")
    parser.add_argument("--validate", action="store_true",
                        help="抓取后并发实测代理可用性（较耗时）")
    parser.add_argument("--validate-workers", type=int, default=50,
                        help="验证并发线程数")
    parser.add_argument("--validate-timeout", type=float, default=6.0,
                        help="单个代理验证超时（秒）")
    parser.add_argument("--validate-url", default=DEFAULT_TEST_URL,
                        help="验证时访问的测试地址")
    parser.add_argument("--only-valid", action="store_true",
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

    scraper = ProxyScraper(
        source=args.source,
        protocol=args.protocol,
        country=args.country,
        per_page=args.per_page,
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
        validate(proxies, args.validate_workers, args.validate_timeout, args.validate_url)
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
