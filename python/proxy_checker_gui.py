#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理批量检验器（图形界面）

用法：
    双击  scripts\\打开代理检验器.cmd        （无黑窗）
    或命令 pythonw proxy_checker_gui.py
    或命令 python  proxy_checker_gui.py     （带控制台，方便看异常）

流程：选择 txt → 设并发/超时/测试地址 → 点「开始检验」→ 自动生成同目录的 〈原名>_可用.txt

支持的行格式（每行一条，# 或 ; 开头视为注释，后面的多余列会忽略）：
    1.2.3.4:8080
    http://1.2.3.4:8080
    socks5://user:pass@1.2.3.4:1080
    1.2.3.4:8080:user:pass
    1.2.3.4,8080
    1.2.3.4:8080  45ms  CN

校验口径与 scripts/check_alive.py 一致（200/204 算可用，https 代理自动降级试 http，
SOCKS 走 PySocks），差别只是：本文件自带界面、支持带账号的代理、结果落成 txt。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk

import requests

DEFAULT_TEST_URL = "http://www.baidu.com"   # 池子偏国内，gstatic 在国内代理上常不可达
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
PAT_SCHEME = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9+.-]*)://"
    r"(?:(?P<user>[^@:/\s]+):(?P<pwd>[^@/\s]*)@)?"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{1,5})$", re.I)
PAT_BARE = re.compile(
    r"^(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{1,5})"
    r"(?::(?P<user>[^:\s]+):(?P<pwd>\S*))?$")
PAT_CSV = re.compile(
    r"^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s*,\s*(?P<port>\d{1,5})"
    r"(?:\s*,\s*(?P<scheme>[a-z][a-z0-9]*))?$", re.I)


def parse_line(line: str) -> dict | None:
    """一行 -> {ip, port, scheme, user, pwd}；认不出来返回 None。"""
    s = line.strip().lstrip("\ufeff")
    if not s or s[0] in "#;":
        return None
    token = s.split()[0].rstrip(",")
    m = (PAT_SCHEME.match(token) or PAT_BARE.match(token) or PAT_CSV.match(token)
         or PAT_SCHEME.match(s) or PAT_BARE.match(s) or PAT_CSV.match(s))
    if not m:
        return None
    g = m.groupdict()
    port = int(g["port"])
    if not 0 < port < 65536:
        return None
    return {"ip": g["ip"], "port": port, "scheme": (g.get("scheme") or "").lower(),
            "user": g.get("user") or "", "pwd": g.get("pwd") or ""}


def read_text_any(path: str) -> str:
    """Windows 上的 txt 常是 GBK/ANSI，按 utf-8 -> gb18030 -> latin-1 逐个试。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def infer_scheme(item: dict, override: str = "auto") -> str:
    """按协议字段推断验证用的 scheme；override 为界面里选的强制值。"""
    if override and override != "auto":
        return override
    s = (item.get("scheme") or "").upper()
    if "SOCKS5" in s or s == "SOCKS":
        return "socks5"
    if "SOCKS4" in s:
        return "socks4"
    if s in ("HTTPS", "SSL"):
        return "https"
    return "http"


# --------------------------------------------------------------------------- #
# 校验 / 输出
# --------------------------------------------------------------------------- #
def check_one(item: dict, scheme: str, test_url: str, timeout: float) -> dict:
    ip, port = item["ip"], item["port"]
    auth = f"{item['user']}:{item['pwd']}@" if item.get("user") else ""
    candidates = [f"{scheme}://{auth}{ip}:{port}"]
    if scheme == "https":
        candidates.append(f"http://{auth}{ip}:{port}")   # 端口没开 TLS 时换 http 再试

    started = time.monotonic()
    detail = ""
    for purl in candidates:
        try:
            resp = requests.get(test_url, proxies={"http": purl, "https": purl},
                                timeout=timeout, headers={"User-Agent": UA})
            elapsed = round((time.monotonic() - started) * 1000)
            ok = resp.status_code in (200, 204)
            return {"ip": ip, "port": port, "scheme": scheme, "user": item.get("user", ""),
                    "pwd": item.get("pwd", ""), "status": "alive" if ok else "dead",
                    "latency": elapsed, "detail": str(resp.status_code)}
        except ValueError:
            continue                       # 代理端口没开 TLS，换下一个候选地址
        except requests.RequestException as exc:
            detail = type(exc).__name__
            break
    return {"ip": ip, "port": port, "scheme": scheme, "user": item.get("user", ""),
            "pwd": item.get("pwd", ""), "status": "dead", "latency": None,
            "detail": detail or "unknown"}


def fmt_proxy(r: dict, keep_scheme: bool) -> str:
    auth = f"{r['user']}:{r['pwd']}@" if r.get("user") else ""
    base = f"{auth}{r['ip']}:{r['port']}"
    # http 默认写裸 ip:port（最好用）；socks4/5 等非 http 才带前缀，免得被当成 http 用
    if keep_scheme and r.get("scheme") not in ("", "http"):
        return f"{r['scheme']}://{base}"
    return base


def write_alive(src_path: str, results: list[dict], keep_scheme: bool) -> tuple[str, int]:
    """把可用代理写成 〈原文件名>_可用.txt，按延迟升序。返回 (输出路径, 条数)。"""
    alive = [r for r in results if r["status"] == "alive"]
    alive.sort(key=lambda r: (r["latency"] is None, r["latency"] or 0))
    stem, _ = os.path.splitext(src_path)
    out_path = f"{stem}_可用.txt"
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for r in alive:
            fh.write(fmt_proxy(r, keep_scheme) + "\n")
    return out_path, len(alive)


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
class App:
    MAX_ROWS = 10000          # 界面上最多铺多少行，超出只计数不插行（防大文件卡死）

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("代理批量检验器")
        root.geometry("820x600")
        root.minsize(680, 480)

        self.proxies: list[dict] = []
        self.results: list[dict] = []
        self.out_path = ""
        self.running = False
        self.stop_evt = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.done_n = self.alive_n = 0
        self.t0 = 0.0
        self.started = False
        self._capped = False

        self.file_var = tk.StringVar()
        self.workers_var = tk.StringVar(value="50")
        self.timeout_var = tk.StringVar(value="6")
        self.url_var = tk.StringVar(value=DEFAULT_TEST_URL)
        self.scheme_var = tk.StringVar(value="auto")
        self.only_alive_var = tk.BooleanVar(value=False)
        self.keep_scheme_var = tk.BooleanVar(value=True)
        self.stat_var = tk.StringVar(value="未选择文件")
        self.out_var = tk.StringVar(value="")

        self._build()

    # ---------------- 构建界面 ---------------- #
    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="代理文件：").pack(side="left")
        ttk.Entry(top, textvariable=self.file_var).pack(side="left", fill="x", expand=True,
                                                        padx=6)
        ttk.Button(top, text="选择 txt…", command=self.choose_file).pack(side="left")

        opts = ttk.Frame(self.root, padding=(10, 4, 10, 4))
        opts.pack(fill="x")

        def spin(var, lo, hi, w=5):
            return ttk.Spinbox(opts, from_=lo, to=hi, textvariable=var, width=w)

        ttk.Label(opts, text="并发").pack(side="left")
        spin(self.workers_var, 1, 300).pack(side="left", padx=(3, 10))
        ttk.Label(opts, text="超时(秒)").pack(side="left")
        spin(self.timeout_var, 1, 60, w=4).pack(side="left", padx=(3, 10))
        ttk.Label(opts, text="测试地址").pack(side="left")
        ttk.Combobox(opts, textvariable=self.url_var, width=26, values=(
            DEFAULT_TEST_URL,
            "https://www.baidu.com",
            "http://www.gstatic.com/generate_204",
        )).pack(side="left", padx=(3, 10))
        ttk.Label(opts, text="协议").pack(side="left")
        ttk.Combobox(opts, textvariable=self.scheme_var, width=7, state="readonly", values=(
            "auto", "http", "https", "socks4", "socks5",
        )).pack(side="left", padx=(3, 12))

        self.btn_start = ttk.Button(opts, text="开始检验", command=self.start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(opts, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")

        prog = ttk.Frame(self.root, padding=(10, 6, 10, 2))
        prog.pack(fill="x")
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1)
        self.bar.pack(side="left", fill="x", expand=True)
        self.pct_var = tk.StringVar(value="0%")
        ttk.Label(prog, textvariable=self.pct_var, width=8).pack(side="left", padx=(8, 0))

        stat = ttk.Frame(self.root, padding=(10, 4, 10, 4))
        stat.pack(fill="x")
        ttk.Label(stat, textvariable=self.stat_var).pack(side="left")
        ttk.Checkbutton(stat, text="只显示可用", variable=self.only_alive_var).pack(
            side="right", padx=(8, 0))
        ttk.Checkbutton(stat, text="非 HTTP 带协议前缀", variable=self.keep_scheme_var).pack(
            side="right")

        mid = ttk.Frame(self.root, padding=(10, 4, 10, 4))
        mid.pack(fill="both", expand=True)
        cols = ("proxy", "scheme", "status", "ms", "detail")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=14)
        heads = (("proxy", "代理", 190), ("scheme", "协议", 80), ("status", "状态", 70),
                 ("ms", "延迟(ms)", 90), ("detail", "说明", 200))
        for cid, text, width in heads:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor="w", stretch=(cid == "detail"))
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("alive", foreground="#2ecc71")
        self.tree.tag_configure("dead", foreground="#8b97ad")

        bot = ttk.Frame(self.root, padding=(10, 4, 10, 10))
        bot.pack(fill="x")
        self.log_text = tk.Text(bot, height=4, state="disabled", wrap="word",
                                background="#171d2b", foreground="#8b97ad",
                                relief="flat", padx=6, pady=4)
        self.log_text.pack(fill="x")
        self.log_text.tag_configure("err", foreground="#ff5f6d")

        foot = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        foot.pack(fill="x")
        self.btn_open = ttk.Button(foot, text="打开结果文件", state="disabled",
                                   command=self.open_result)
        self.btn_open.pack(side="left")
        ttk.Checkbutton(foot, text="结果文件里 socks 代理带前缀",
                        variable=self.keep_scheme_var).pack(side="left", padx=12)
        ttk.Label(foot, textvariable=self.out_var, foreground="#8b97ad").pack(
            side="left", padx=8)

        self.root.after(120, self._poll)

    # ---------------- 小工具 ---------------- #
    def log(self, msg: str, tag: str = ""):
        self.log_text.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def choose_file(self):
        path = filedialog.askopenfilename(
            title="选择代理 txt 文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        self.file_var.set(path)
        self.load_file(path)

    def load_file(self, path: str):
        try:
            text = read_text_any(path)
        except OSError as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        items, bad = [], 0
        for line in text.splitlines():
            item = parse_line(line)
            if item:
                items.append(item)
            elif line.strip() and line.strip()[0] not in "#;":
                bad += 1
        self.proxies = items
        self.started = False
        self.out_path = ""
        self.out_var.set("")
        self.btn_open.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.bar.configure(maximum=max(len(items), 1), value=0)
        self.pct_var.set("0%")
        self.stat_var.set(f"已载入 {len(items)} 条" + (f"，跳过无法识别的 {bad} 行" if bad else ""))
        self.log(f"载入 {os.path.basename(path)}：有效 {len(items)} 条"
                 + (f"，跳过 {bad} 行" if bad else ""))

    # ---------------- 执行 ---------------- #
    def start(self):
        if self.running:
            return
        if not self.proxies:
            messagebox.showwarning("提示", "请先选择一个代理 txt 文件")
            return
        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("提示", "测试地址要以 http:// 或 https:// 开头")
            return
        try:
            workers = max(1, int(self.workers_var.get()))
            timeout = max(1.0, float(self.timeout_var.get()))
        except ValueError:
            messagebox.showwarning("提示", "并发和超时要填数字")
            return

        self.running = True
        self.started = True
        self.stop_evt = threading.Event()
        self.results = []
        self.done_n = self.alive_n = 0
        self._capped = False
        self.t0 = time.monotonic()
        self.tree.delete(*self.tree.get_children())
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_open.configure(state="disabled")
        self.out_var.set("")
        self.bar.configure(maximum=len(self.proxies), value=0)
        self.log(f"开始：{len(self.proxies)} 条，并发 {workers}，超时 {timeout:g}s，"
                 f"协议 {self.scheme_var.get()}，测试地址 {url}")
        threading.Thread(target=self._work,
                         args=(list(self.proxies), self.scheme_var.get(), url,
                               timeout, workers),
                         daemon=True).start()

    def _work(self, proxies, scheme_override, url, timeout, workers):
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(check_one, p, infer_scheme(p, scheme_override), url, timeout): p
                    for p in proxies
                }
                for fut in as_completed(futures):
                    if self.stop_evt.is_set():
                        break
                    item = futures[fut]
                    try:
                        r = fut.result()
                    except Exception as exc:      # 单条怪异常不该中断整轮
                        r = {"ip": item["ip"], "port": item["port"],
                             "scheme": infer_scheme(item, scheme_override),
                             "user": item.get("user", ""), "pwd": item.get("pwd", ""),
                             "status": "dead", "latency": None,
                             "detail": f"{type(exc).__name__}"}
                    self.q.put(("r", r))
                if self.stop_evt.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
            self.q.put(("done", None))
        except Exception as exc:
            self.q.put(("err", f"{type(exc).__name__}: {exc}"))

    # ---------------- 收结果 ---------------- #
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "r":
                    self._on_result(payload)
                elif kind == "done":
                    self._on_done()
                elif kind == "err":
                    self._finish_running()
                    self.log(f"出错：{payload}", "err")
                    messagebox.showerror("检验出错", payload)
        except queue.Empty:
            pass
        if self.running:
            elapsed = time.monotonic() - self.t0
            total = len(self.proxies)
            self.stat_var.set(f"已测 {self.done_n}/{total}　可用 {self.alive_n}　"
                              f"失效 {self.done_n - self.alive_n}　用时 {elapsed:.0f}s")
        self.root.after(120, self._poll)

    def _on_result(self, r: dict):
        self.results.append(r)          # 统计用 counts，落盘用 results，两者都要
        self.done_n += 1
        if r["status"] == "alive":
            self.alive_n += 1
        self.bar.configure(value=self.done_n)
        total = max(len(self.proxies), 1)
        self.pct_var.set(f"{100 * self.done_n // total}%")

        show = (not self.only_alive_var.get()) or r["status"] == "alive"
        rows = len(self.tree.get_children())
        if show:
            if rows < self.MAX_ROWS:
                self.tree.insert("", "end", tags=(r["status"],), values=(
                    f"{r['ip']}:{r['port']}", r["scheme"] or "-",
                    "可用" if r["status"] == "alive" else "失效",
                    r["latency"] if r["latency"] is not None else "-",
                    r["detail"]))
            elif not self._capped:
                self._capped = True    # 大文件只截断界面显示，统计与输出文件不受影响
                self.log(f"结果超过 {self.MAX_ROWS} 行，界面只显示前 {self.MAX_ROWS} 行")

    def _finish_running(self):
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _on_done(self):
        stopped = self.stop_evt.is_set()
        self._finish_running()
        elapsed = time.monotonic() - self.t0
        total = len(self.proxies)
        src = self.file_var.get().strip()

        if not src:
            self.stat_var.set(f"已测 {self.done_n}/{total}　可用 {self.alive_n}")
            return
        try:
            out_path, n = write_alive(src, self.results, self.keep_scheme_var.get())
        except OSError as exc:
            self.log(f"写文件失败：{exc}", "err")
            messagebox.showerror("写文件失败", str(exc))
            return
        self.out_path = out_path
        self.btn_open.configure(state="normal")
        self.out_var.set(os.path.basename(out_path))
        head = "已停止（部分结果）" if stopped else "完成"
        self.stat_var.set(f"{head}：可用 {self.alive_n}/{self.done_n} 条，用时 {elapsed:.0f}s")
        self.log(f"{head}：检测 {self.done_n} 条，可用 {self.alive_n} 条，"
                 f"耗时 {elapsed:.0f}s → {out_path}")
        messagebox.showinfo(
            "检验完成",
            f"{head}\n\n"
            f"检测：{self.done_n} 条\n可用：{self.alive_n} 条\n"
            f"失效：{self.done_n - self.alive_n} 条\n用时：{elapsed:.0f} 秒\n\n"
            f"结果已写入：\n{out_path}"
            + ("" if self.alive_n else "\n\n（没有可用代理，文件是空的）"))

    def stop(self):
        if not self.running:
            return
        self.stop_evt.set()
        self.btn_stop.configure(state="disabled")
        self.log("正在停止…未完成的请求会被取消")

    def open_result(self):
        if self.out_path and os.path.exists(self.out_path):
            if hasattr(os, "startfile"):
                os.startfile(self.out_path)
            else:                                   # 非 Windows 兜底
                import subprocess
                subprocess.Popen(["xdg-open", self.out_path])


# --------------------------------------------------------------------------- #
def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            reconf(encoding="utf-8", errors="replace")
    try:                                            # 高 DPI 下字号别糊成一团
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
