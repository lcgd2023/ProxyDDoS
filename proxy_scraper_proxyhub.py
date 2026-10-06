#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬取 https://proxyhub.me/ 上所有页面的免费代理 IP 与端口。

站点结构与分页规则（实测）：
  * 列表页每页 20 条，分页参数为 `?page=N`，分页器里的「» N」给出总页数；
  * 全量列表的规范地址是 `/`（`/en/all-free-proxy-list.html` 会被重定向回 `/`，
    并顺手丢掉 `?page=N`，所以全量列表必须用 `/` 作基础地址）；
  * 筛选用路径而不是查询参数：`/{lang}/{country}-{type}-proxy-list.html?page=N`，
    匿名度则靠 Cookie `anonymity=transparent|anonymous|elite`；
  * 请求的页码超过总页数时，站点会重定向回第 1 页并丢弃 page 参数 ——
    因此必须以分页器的总页数为准，脚本里另加了「最终 URL 丢了 page 参数」的保险。

用法示例：
    python proxy_scraper_proxyhub.py                      # 全量列表所有页 -> proxieshub.csv + txt
    python proxy_scraper_proxyhub.py --max-pages 5        # 只抓前 5 页
    python proxy_scraper_proxyhub.py --country cn --type socks5
    python proxy_scraper_proxyhub.py --country us --type http --anonymity elite
    python proxy_scraper_proxyhub.py --validate --only-valid -o alivehub

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

BASE_URL = "https://proxyhub.me"
DEFAULT_LANG = "en"
PER_PAGE = 20
MAX_PAGE = 100                     # 站点分页器最多渲染到第 100 页

COUNTRY_CHOICES = "all 或两位国家代码，如 cn/us/jp/de（见站点国家下拉框）"
TYPE_CHOICES = ["free", "http", "https", "socks4", "socks5", "socks"]
ANONYMITY_CHOICES = ["all", "transparent", "anonymous", "elite"]

DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LAST_PAGE_RE = re.compile(r"page=(\d+)\s*$")

log = logging.getLogger("proxyhub")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Proxy:
    country: str = ""
    ip: str = ""
    port: str = ""
    protocols: str = ""        # HTTP / HTTPS / SOCKS4 / SOCKS5，多个用空格分隔
    anonymity: str = ""        # Transparent / Anonymous / Elite
    last_checked: str = ""     # 如 2026-10-04 13:40 UTC
    valid: str = ""            # --validate 之后才有值：yes / no

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"


CSV_COLUMNS = ["country", "ip", "port", "protocols", "anonymity", "last_checked", "valid"]

IP_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


def is_ip(value: str) -> bool:
    if not IP_RE.fullmatch(value):
        return False
    return all(int(octet) <= 255 for octet in value.split("."))


# --------------------------------------------------------------------------- #
# 抓取器
# --------------------------------------------------------------------------- #
class ProxyHubScraper:
    """按页抓取 proxyhub.me 的代理列表，内置限速、重试、去重与翻页保险。"""

    def __init__(
        self,
        country: str = "all",
        proxy_type: str = "free",
        anonymity: str = "all",
        lang: str = DEFAULT_LANG,
        delay: float = 0.5,
        timeout: float = 15.0,
        retries: int = 3,
    ) -> None:
        self.country = (country or "all").lower()
        self.proxy_type = proxy_type or "free"
        self.anonymity = (anonymity or "all").lower()
        self.lang = lang or DEFAULT_LANG
        self.delay = delay
        self.timeout = timeout
        self.retries = retries

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                "Referer": f"{BASE_URL}/",
            }
        )
        if self.anonymity and self.anonymity != "all":
            # 站点前端就是靠这个 Cookie 过滤匿名度的
            self.session.cookies.set("anonymity", self.anonymity, domain="proxyhub.me")
        self._seen: set[tuple[str, str]] = set()
        self._last_request = 0.0

    # -- 地址 -------------------------------------------------------------- #
    @property
    def list_path(self) -> str:
        """全量列表的规范地址是 `/`；带筛选时用站点自己的路径规则。"""
        if self.country == "all" and self.proxy_type == "free":
            return "/"
        return f"/{self.lang}/{self.country}-{self.proxy_type}-proxy-list.html"

    # -- 基础请求 ---------------------------------------------------------- #
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed + random.uniform(0, 0.2))
        self._last_request = time.monotonic()

    def _get(self, page: int) -> tuple[str, str]:
        """返回 (HTML, 最终 URL)。"""
        url = BASE_URL + self.list_path
        params = {"page": page} if page > 1 else None
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                resp.encoding = "utf-8"      # 站点 charset=utf-8，显式指定避免乱码
                return resp.text, str(resp.url)
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                log.warning("请求失败(%s/%s) %s?page=%s: %s，%.1fs 后重试",
                            attempt, self.retries, self.list_path, page, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"请求失败（已重试 {self.retries} 次）: {url}?page={page}") from last_exc

    # -- 解析 -------------------------------------------------------------- #
    @staticmethod
    def _parse(html: str) -> tuple[list[Proxy], int]:
        """解析表格，返回 (本页数据, 分页器给出的总页数)。"""
        soup = BeautifulSoup(html, "html.parser")
        total_pages = 1
        for a in soup.select("a.page-link"):
            text = a.get_text(strip=True)
            href = a.get("href") or ""
            if text.startswith("»") and LAST_PAGE_RE.search(href):
                total_pages = max(total_pages, int(LAST_PAGE_RE.search(href).group(1)))

        rows: list[Proxy] = []
        for tr in soup.select("table.vpn-table tbody tr"):
            ip_el = tr.select_one(".ip-text")
            port_el = tr.select_one(".port-text")
            if ip_el is None or port_el is None:
                continue
            ip, port = ip_el.get_text(strip=True), port_el.get_text(strip=True)
            if not is_ip(ip) or not port.isdigit():
                continue

            cells = tr.select(".protocols-cell")
            protocols = " ".join(
                c.get_text(" ", strip=True) for c in (cells[0].select(".protocol-chip") if cells else [])
            )
            anonymity = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""

            country_el = tr.select_one(".country-cell a")
            checked_el = tr.select_one(".checked-text")
            rows.append(
                Proxy(
                    country=country_el.get_text(strip=True) if country_el else "",
                    ip=ip,
                    port=port,
                    protocols=protocols,
                    anonymity=anonymity,
                    last_checked=checked_el.get_text(strip=True) if checked_el else "",
                )
            )
        return rows, total_pages

    # -- 抓取流程 ----------------------------------------------------------- #
    def _iter_pages(self) -> Iterator[tuple[list[Proxy], int, int]]:
        """yield (本页数据, 当前页, 总页数)。"""
        page = 1
        total_pages = 1
        while True:
            html, final_url = self._get(page)
            rows, parsed_total = self._parse(html)
            if page == 1:
                total_pages = parsed_total
                if total_pages > 1:
                    log.info("筛选 %s，分页器显示共 %s 页（每页 %s 条）",
                             self.list_path, total_pages, PER_PAGE)
            elif page > 1 and f"page={page}" not in final_url:
                # 站点把超页码重定向回第 1 页并丢掉了 page 参数，必须停，否则会无限循环
                log.warning("第 %s 页被重定向到 %s，提前停止", page, final_url)
                break
            log.info("第 %s/%s 页：本页 %s 条", page, total_pages, len(rows))
            yield rows, page, total_pages
            if page >= total_pages or not rows:
                break
            page += 1

    def crawl(self, max_pages: int = 0, limit: int = 0) -> list[Proxy]:
        """max_pages=0 表示抓完分页器给出的所有页，limit>0 达到即停。"""
        proxies: list[Proxy] = []
        page_no = 0
        for page_no, (rows, page, total_pages) in enumerate(self._iter_pages(), start=1):
            for item in rows:
                key = (item.ip, item.port)
                if key in self._seen:
                    continue
                self._seen.add(key)
                proxies.append(item)
                if limit and len(proxies) >= limit:
                    log.info("已达 --limit %s，停止抓取", limit)
                    return proxies
            if max_pages and page >= max_pages and rows:
                log.info("已达 --max-pages %s，停止抓取", max_pages)
                break
        else:
            log.info("已抓完最后一页（共 %s 页）", page_no)
        return proxies


# --------------------------------------------------------------------------- #
# 可用性验证（可选）
# --------------------------------------------------------------------------- #
_AUTO_SCHEME = {
    "http": "http",
    "https": "http",      # 标 HTTPS 的免费代理大多端口本身不加密，验证走 http
    "socks4": "socks4",
    "socks5": "socks5",
    "socks": "socks5",
    "free": "http",
}


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
        description="爬取 proxyhub.me 上所有页面的免费代理 IP 与端口",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--country", default="all", help=f"国家/地区代码：{COUNTRY_CHOICES}")
    parser.add_argument("--type", dest="proxy_type", default="free", choices=TYPE_CHOICES,
                        help="代理类型")
    parser.add_argument("--anonymity", default="all", choices=ANONYMITY_CHOICES,
                        help="匿名度（站点靠 Cookie 过滤）")
    parser.add_argument("--lang", default=DEFAULT_LANG,
                        help="列表页语言目录，如 en/zh/de（不影响数据）")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="最多抓几页，0 表示抓完分页器给出的全部页")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多收集多少条，0 表示不限制")
    parser.add_argument("-o", "--output", default="proxieshub",
                        help="输出文件名（不含扩展名）")
    parser.add_argument("--formats", default="csv,txt",
                        help="输出格式，逗号分隔：csv,txt,json")

    req = parser.add_argument_group("请求控制")
    req.add_argument("--delay", type=float, default=0.5,
                     help="两次请求之间的最小间隔（秒）")
    req.add_argument("--timeout", type=float, default=15.0, help="单次请求超时（秒）")
    req.add_argument("--retries", type=int, default=3, help="失败重试次数")

    val = parser.add_argument_group("可用性验证")
    val.add_argument("--validate", action="store_true",
                     help="抓取后并发实测代理可用性（较耗时）")
    val.add_argument("--validate-scheme", default="auto",
                     choices=["auto", "http", "https", "socks4", "socks5"],
                     help="验证时代理协议，auto=按 --type 推断")
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

    scraper = ProxyHubScraper(
        country=args.country,
        proxy_type=args.proxy_type,
        anonymity=args.anonymity,
        lang=args.lang,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
    )
    log.info("列表地址：%s", scraper.list_path)

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
        scheme = (_AUTO_SCHEME.get(args.proxy_type, "http")
                  if args.validate_scheme == "auto" else args.validate_scheme)
        validate(proxies, args.validate_workers, args.validate_timeout,
                 args.validate_url, scheme)
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
