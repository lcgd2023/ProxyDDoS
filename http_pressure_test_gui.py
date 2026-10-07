#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP 压力测试工具（图形界面）
⚠️ 仅可用于获得完整书面授权的自有服务器性能测试，禁止用于未授权目标

用法：
    双击 打开HTTP压测工具.cmd        （无黑窗）
    或 pythonw http_pressure_test_gui.py
    或 python  http_pressure_test_gui.py   （带控制台，方便看异常）

与命令行 http_pressure_test.py 共用 run_test()，统计口径完全一致：
    并发 / 时长 / 超时  = 命令行的 -c / -d / -t
    代理文件            = 命令行的 -p   （每行一个，# 开头是注释）

代理文件怎么选：
    ·下拉框里列出了本目录已有的 txt（proxies.txt、proxies89.txt …）可直接点选
    ·或点「浏览…」用文件对话框挑任意位置的 txt
    ·选完立刻显示条数；测试时随机轮换这些代理发请求（不选 = 用本机 IP）
"""

from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

# 重定向到文件/管道时编码是 GBK，emoji 会炸；直连控制台或 pythonw 时不动它
if not sys.stdout.isatty():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from http_pressure_test import (  # noqa: E402
    MAX_BODY_SIZE,
    Stats,
    fmt_bytes,
    load_proxies_detail,
    parse_bytes,
    run_test,
)

# 视觉风格与 代理批量检验器 保持一致
BG_DARK = "#171d2b"
FG_MUTED = "#8b97ad"
FG_TEXT = "#e8ecf5"
GREEN = "#2ecc71"
RED = "#ff5f6d"
BLUE = "#4da3ff"
DEFAULT_TARGET = "http://127.0.0.1:8080/"


# --------------------------------------------------------------------------- #
# 参数小工具（可无界面单独测试）
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """补全 scheme 并做基本校验，不合格抛 ValueError。"""
    u = (url or "").strip()
    if not u:
        raise ValueError("请填写目标地址")
    if "://" not in u:
        u = "http://" + u
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        raise ValueError("目标只支持 http / https")
    if not p.netloc:
        raise ValueError("地址不完整，例如 http://127.0.0.1:8080/")
    return u


def to_int(value, name: str, lo: int, hi: int) -> int:
    """把输入框内容转成 int 并限定范围，不合格抛 ValueError。"""
    try:
        n = int(float(str(value).strip()))
    except Exception:
        raise ValueError(f"{name} 必须是整数")
    if not lo <= n <= hi:
        raise ValueError(f"{name} 要在 {lo}~{hi} 之间")
    return n


def local_txt_files() -> list:
    """本目录里现成的 txt（代理列表候选），最多列 30 个。"""
    try:
        names = [n for n in os.listdir(SCRIPT_DIR) if n.lower().endswith(".txt")]
    except OSError:
        return []
    return sorted(names)[:30]


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
class App:
    POLL_MS = 80          # 队列轮询间隔
    SERIES_MAX = 900      # QPS 曲线最多留多少个点

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("HTTP 压力测试工具")
        root.geometry("960x740")
        root.minsize(840, 640)

        self.running = False
        self.stop_evt = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.series = deque(maxlen=self.SERIES_MAX)
        self.stats = Stats()
        self.duration = 30
        self.max_traffic = 0   # 本轮流量上限（0 = 不限），收尾时判断是否到顶
        self.peak_qps = 0.0
        self.last_report = ""

        self.url_var = tk.StringVar(value=DEFAULT_TARGET)
        self.file_var = tk.StringVar()
        self.proxy_var = tk.StringVar(value="未使用代理（本机 IP）")
        self.conc_var = tk.StringVar(value="5")
        self.dur_var = tk.StringVar(value="30")
        self.to_var = tk.StringVar(value="10")
        self.body_var = tk.StringVar(value="0")     # 每请求上传体，0 = GET
        self.cap_var = tk.StringVar(value="")       # 总流量上限，空 = 不限
        self.auth_var = tk.BooleanVar(value=False)

        self.v_total = tk.StringVar(value="0")
        self.v_success = tk.StringVar(value="0")
        self.v_failed = tk.StringVar(value="0")
        self.v_qps = tk.StringVar(value="0.0")
        self.v_peak = tk.StringVar(value="0.0")
        self.v_avg = tk.StringVar(value="0 ms")
        self.v_traffic = tk.StringVar(value="0 B")
        self.v_sent = tk.StringVar(value="0 B")
        self.v_codes = tk.StringVar(value="状态码分布：—")
        self.v_errors = tk.StringVar(value="")
        self.v_progress = tk.StringVar(value="0%")
        self.v_status = tk.StringVar(value="就绪")

        self._build()
        self.root.after(self.POLL_MS, self._poll)

    # ---------------- 构建界面 ---------------- #
    def _build(self):
        # 顶部队规条
        warn = tk.Frame(self.root, bg=RED, padx=8, pady=4)
        warn.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(warn,
                 text="⚠️ 仅用于已获得完整书面授权的自有服务器；禁止对未授权目标发起压力测试。",
                 bg=RED, fg="#ffffff",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        params = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        params.pack(fill="x")

        # 目标地址 + 代理文件
        row1 = ttk.Frame(params)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="目标地址", width=8).pack(side="left")
        ttk.Entry(row1, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True, padx=(0, 12))

        ttk.Label(row1, text="代理 txt", width=8).pack(side="left")
        self.cmb_file = ttk.Combobox(row1, textvariable=self.file_var, values=local_txt_files())
        self.cmb_file.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.cmb_file.bind("<<ComboboxSelected>>", lambda e: self.preview_proxy())
        self.cmb_file.bind("<FocusOut>", lambda e: self.preview_proxy())
        ttk.Button(row1, text="浏览…", width=8, command=self.choose_file).pack(
            side="left", padx=(0, 4))
        ttk.Button(row1, text="清空", width=6, command=self.clear_file).pack(side="left")

        # 代理状态 + 参数
        row2 = ttk.Frame(params)
        row2.pack(fill="x", pady=(4, 2))
        self.lbl_proxy = tk.Label(row2, textvariable=self.proxy_var,
                                  fg=FG_MUTED, font=("Microsoft YaHei UI", 9))
        self.lbl_proxy.pack(side="left")

        row3 = ttk.Frame(params)
        row3.pack(fill="x", pady=(6, 2))

        def spin(label, var, lo, hi, w=5):
            ttk.Label(row3, text=label).pack(side="left")
            ttk.Spinbox(row3, from_=lo, to=hi, textvariable=var, width=w).pack(
                side="left", padx=(3, 12))

        spin("并发", self.conc_var, 1, 1000)
        spin("时长(秒)", self.dur_var, 1, 7200)
        spin("超时(秒)", self.to_var, 1, 120, w=4)

        ttk.Checkbutton(row3, text="我已获得该目标的完整书面授权",
                        variable=self.auth_var).pack(side="left", padx=(4, 0))

        # 流量控制：每请求上传体 + 总流量上限（空/0 = 不设）
        row3b = ttk.Frame(params)
        row3b.pack(fill="x", pady=(4, 2))
        ttk.Label(row3b, text="上传体").pack(side="left")
        ttk.Entry(row3b, textvariable=self.body_var, width=9).pack(
            side="left", padx=(3, 4))
        ttk.Label(row3b, text="0=GET，可写 64k / 2m",
                  foreground=FG_MUTED).pack(side="left", padx=(0, 14))
        ttk.Label(row3b, text="流量上限").pack(side="left")
        ttk.Entry(row3b, textvariable=self.cap_var, width=11).pack(
            side="left", padx=(3, 4))
        ttk.Label(row3b, text="填了就只按流量跑（忽略时长），如 500m / 1t",
                  foreground=FG_MUTED).pack(side="left")

        row4 = ttk.Frame(params)
        row4.pack(fill="x", pady=(8, 2))
        self.btn_start = ttk.Button(row4, text="▶ 开始测试", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row4, text="■ 停止", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        ttk.Label(row4, textvariable=self.v_status, foreground=FG_MUTED).pack(
            side="right")

        # 进度条
        prog = ttk.Frame(self.root, padding=(10, 6, 10, 2))
        prog.pack(fill="x")
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True)
        ttk.Label(prog, textvariable=self.v_progress, width=16).pack(
            side="left", padx=(8, 0))

        # 指标卡片
        cards = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        cards.pack(fill="x")
        for i in range(8):
            cards.columnconfigure(i, weight=1)
        for i, (title, var) in enumerate((
                ("总请求", self.v_total), ("成功", self.v_success),
                ("失败", self.v_failed), ("当前 QPS", self.v_qps),
                ("峰值 QPS", self.v_peak), ("平均响应", self.v_avg),
                ("成功流量", self.v_traffic), ("上行流量", self.v_sent))):
            self._card(cards, title, var).grid(row=0, column=i, sticky="ew", padx=4)

        # QPS 曲线
        chart_box = ttk.Frame(self.root, padding=(10, 6, 10, 2))
        chart_box.pack(fill="x")
        self.canvas = tk.Canvas(chart_box, height=150, bg=BG_DARK, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self._draw_chart())

        ttk.Label(self.root, textvariable=self.v_codes, padding=(10, 4, 10, 0)).pack(
            fill="x")
        ttk.Label(self.root, textvariable=self.v_errors, foreground=RED,
                  padding=(10, 0, 10, 2)).pack(fill="x")

        # 日志 / 报告
        log_box = ttk.Frame(self.root, padding=(10, 4, 10, 4))
        log_box.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_box, height=7, state="disabled", wrap="word",
                                bg=BG_DARK, fg=FG_MUTED, relief="flat",
                                padx=6, pady=4, font=("Consolas", 10))
        vsb = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for tag, color in (("ok", GREEN), ("err", RED), ("cfg", BLUE),
                           ("info", FG_MUTED)):
            self.log_text.tag_configure(tag, foreground=color)

        foot = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        foot.pack(fill="x")
        ttk.Button(foot, text="复制报告", command=self.copy_report).pack(side="left")
        ttk.Button(foot, text="导出报告…", command=self.export_report).pack(
            side="left", padx=6)
        ttk.Button(foot, text="清空日志", command=self.clear_log).pack(side="left")
        ttk.Label(foot, text="本机自测：python -m http.server 8080",
                  foreground=FG_MUTED).pack(side="right")

    def _card(self, parent, title: str, var: tk.StringVar) -> tk.Frame:
        box = tk.Frame(parent, bg=BG_DARK, padx=12, pady=6)
        tk.Label(box, text=title, bg=BG_DARK, fg=FG_MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="center")
        tk.Label(box, textvariable=var, bg=BG_DARK, fg=FG_TEXT,
                 font=("Consolas", 16, "bold")).pack(anchor="center")
        return box

    # ---------------- 小工具 ---------------- #
    def log(self, msg: str, tag: str = ""):
        self.log_text.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _resolve_path(self, raw: str) -> str:
        """把下拉框里的相对文件名补成绝对路径。"""
        v = (raw or "").strip().strip('"')
        if not v:
            return ""
        return v if os.path.isabs(v) else os.path.join(SCRIPT_DIR, v)

    # ---------------- 代理文件 ---------------- #
    def choose_file(self):
        path = filedialog.askopenfilename(
            title="选择代理列表 txt",
            initialdir=SCRIPT_DIR,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.file_var.set(path)
            self.preview_proxy()

    def clear_file(self):
        self.file_var.set("")
        self.proxy_var.set("未使用代理（本机 IP）")
        self.lbl_proxy.configure(fg=FG_MUTED)

    def preview_proxy(self):
        """选完立刻读一遍，把条数显示出来（不发起任何测试）。"""
        path = self._resolve_path(self.file_var.get())
        if not path:
            self.clear_file()
            return
        try:
            usable, skipped = load_proxies_detail(path)
        except Exception as e:
            self.proxy_var.set(f"读取失败：{e}")
            self.lbl_proxy.configure(fg=RED)
            return
        if usable:
            msg = f"✅ 已加载 {len(usable)} 个可用代理（测试时随机轮换）"
            if skipped:
                msg += f"，跳过 {skipped} 条 SOCKS/无法识别"
            self.proxy_var.set(msg)
            self.lbl_proxy.configure(fg=GREEN)
        else:
            self.proxy_var.set("⚠️ 文件里没有可用的 HTTP 代理 → 将用本机 IP")
            self.lbl_proxy.configure(fg=RED)

    # ---------------- 运行控制 ---------------- #
    def start(self):
        if self.running:
            return
        if not self.auth_var.get():
            messagebox.showwarning("需要授权确认",
                                   "请先勾选「我已获得该目标的完整书面授权」。")
            return
        try:
            url = normalize_url(self.url_var.get())
            conc = to_int(self.conc_var.get(), "并发数", 1, 1000)
            dur = to_int(self.dur_var.get(), "测试时长", 1, 7200)
            timeout = to_int(self.to_var.get(), "超时时间", 1, 120)
            body_size = parse_bytes(self.body_var.get(), "上传体大小")
            max_traffic = parse_bytes(self.cap_var.get(), "流量上限")
        except ValueError as e:
            messagebox.showwarning("参数检查", str(e))
            return
        if body_size > MAX_BODY_SIZE:
            messagebox.showwarning("参数检查",
                                   f"单请求上传体最大 {fmt_bytes(MAX_BODY_SIZE)}，"
                                   f"你输入的是 {fmt_bytes(body_size)}。")
            return

        proxies = []
        path = self._resolve_path(self.file_var.get())
        if path:
            try:
                proxies, skipped = load_proxies_detail(path)
            except Exception as e:
                messagebox.showerror("代理文件", f"读取失败：{e}")
                return
            if not proxies:
                self.log("⚠️ 代理文件里没有可用的 HTTP 代理，改用本机 IP 发起请求", "err")
            else:
                msg = f"✅ 已加载 {len(proxies)} 个可用代理（测试时随机轮换）"
                if skipped:
                    msg += f"，跳过 {skipped} 条 SOCKS/无法识别"
                self.proxy_var.set(msg)
                self.lbl_proxy.configure(fg=GREEN)
                if skipped:
                    self.log(f"已跳过 {skipped} 条 SOCKS/无法识别的代理行（本工具只支持 HTTP 代理）",
                             "err")
        else:
            self.clear_file()

        # 填了流量上限 → 时长不再参与：流量是唯一停止条件，跑多久都行
        run_dur = 0 if max_traffic else dur
        self.duration = run_dur
        self.max_traffic = max_traffic
        self.stats = Stats()
        self.series.clear()
        self.peak_qps = 0.0
        self.last_report = ""
        self.stop_evt.clear()
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.bar["value"] = 0
        for v in (self.v_total, self.v_success, self.v_failed):
            v.set("0")
        self.v_qps.set("0.0")
        self.v_peak.set("0.0")
        self.v_avg.set("0 ms")
        self.v_traffic.set("0 B")
        self.v_sent.set("0 B")
        self.v_codes.set("状态码分布：—")
        self.v_errors.set("")
        self.v_status.set("运行中…")

        extra = ""
        if body_size:
            extra += f" | 上传体 {fmt_bytes(body_size)}（POST）"
        if max_traffic:
            extra += f" | 流量上限 {fmt_bytes(max_traffic)}（忽略时长，跑到量为止）"
        self.log(f"开始测试  目标 {url} | 并发 {conc} | "
                 f"{'时长不限' if run_dur == 0 else f'时长 {run_dur}s'} | 超时 {timeout}s"
                 f" | 代理 {len(proxies)} 个{extra}", "cfg")
        self._draw_chart()
        threading.Thread(target=self._worker,
                         args=(url, proxies, conc, run_dur, timeout, body_size, max_traffic),
                         daemon=True, name="pressure-worker").start()

    def stop(self):
        if not self.running:
            return
        self.stop_evt.set()
        self.btn_stop.configure(state="disabled")
        self.v_status.set("停止中…（等待在途请求）")
        self.log("已请求停止，等待在途请求收尾…", "info")

    def _worker(self, url, proxies, conc, dur, timeout, body_size, max_traffic):
        """独立线程里跑事件循环，回调只往队列投递，绝不碰 tkinter。"""
        try:
            asyncio.run(run_test(
                url, proxies, concurrency=conc, duration=dur, timeout=timeout,
                stats=self.stats,
                on_progress=lambda snap: self.q.put(("tick", snap)),
                should_stop=self.stop_evt.is_set,
                progress_interval=0.5,
                body_size=body_size, max_traffic=max_traffic,
            ))
            self.q.put(("done", self.stats))
        except Exception as e:  # noqa: BLE001 —— 界面上要看到完整异常
            self.q.put(("error", e))

    # ---------------- 界面刷新 ---------------- #
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "tick":
                    self._on_tick(payload)
                elif kind == "done":
                    self._finish(payload, stopped=self.stop_evt.is_set())
                elif kind == "error":
                    self._finish(None, error=payload)
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._poll)

    def _on_tick(self, snap: dict):
        self.v_total.set(str(snap["total"]))
        self.v_success.set(str(snap["success"]))
        self.v_failed.set(str(snap["failed"]))
        self.v_qps.set(f"{snap['qps']:.1f}")
        self.peak_qps = max(self.peak_qps, snap["qps"])
        self.v_peak.set(f"{self.peak_qps:.1f}")
        self.v_avg.set(f"{snap['avg_ms']:.0f} ms")
        self.v_traffic.set(fmt_bytes(snap.get("ok_bytes", 0)))
        self.v_sent.set(fmt_bytes(snap.get("sent_bytes", 0)))

        self.series.append((snap["elapsed"], snap["qps"]))
        if self.max_traffic:
            # 填了流量上限：进度条改显示流量完成度（时长已不参与）
            total = snap.get("total_bytes") or (snap.get("ok_bytes", 0)
                                                + snap.get("sent_bytes", 0))
            pct = min(100.0, total / self.max_traffic * 100)
            self.bar["value"] = pct
            self.v_progress.set(
                f"{pct:.1f}%  {fmt_bytes(total)}/{fmt_bytes(self.max_traffic)}")
        else:
            pct = min(100.0, snap["elapsed"] / max(1, self.duration) * 100)
            self.bar["value"] = pct
            self.v_progress.set(f"{pct:.0f}%  {snap['elapsed']:.1f}/{self.duration}s")

        codes = snap.get("status_codes") or {}
        if codes and snap["total"]:
            self.v_codes.set("状态码分布：" + "   ".join(
                f"HTTP {c}: {n} 次（{n / snap['total'] * 100:.1f}%）"
                for c, n in sorted(codes.items())))
        else:
            self.v_codes.set("状态码分布：—")

        errs = snap.get("errors") or []
        self.v_errors.set("失败原因：" + "   ".join(f"{k} ×{n:,}" for k, n in errs)
                          if snap["failed"] and errs else "")
        self._draw_chart()

    def _finish(self, stats, error=None, stopped=False):
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if error is not None:
            self.v_status.set("出错结束")
            self.log(f"❌ 测试异常：{error}", "err")
            return
        self.v_status.set("已停止" if stopped else "已完成")
        self.bar["value"] = 100
        self._on_tick(stats.snapshot())   # 收尾请求不走 tick，这里补一次最终数字
        if self.max_traffic and (stats.ok_bytes + stats.sent_bytes) >= self.max_traffic:
            self.v_status.set("已达流量上限")
            self.log(f"🛑 总流量已达上限 {fmt_bytes(self.max_traffic)}，自动停止", "info")
        self.last_report = stats.report()
        for line in self.last_report.splitlines():
            if line.strip() and not line.startswith("="):
                self.log(line, "ok")

    def _draw_chart(self):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 40:
            return
        c.delete("all")
        pad_l, pad_r, pad_t, pad_b = 46, 8, 8, 18
        cw, ch = w - pad_l - pad_r, h - pad_t - pad_b
        data = list(self.series)
        ymax = max([q for _, q in data] + [1.0])

        for i in range(4):
            y = pad_t + ch * i / 3
            c.create_line(pad_l, y, w - pad_r, y, fill="#2a3348")
            c.create_text(pad_l - 6, y, text=f"{ymax * (3 - i) / 3:.0f}",
                          anchor="e", fill=FG_MUTED, font=("Consolas", 8))
        c.create_text(w - pad_r, h - 4, text="时间 →", anchor="e",
                      fill=FG_MUTED, font=("Microsoft YaHei UI", 8))

        if len(data) >= 2:
            tmax = data[-1][0] or 1.0
            pts = []
            for t, q in data:
                pts += [pad_l + cw * (t / tmax), pad_t + ch * (1 - q / ymax)]
            c.create_polygon(*pts, pad_l + cw, pad_t + ch, pad_l, pad_t + ch,
                             fill="#1f3a5f", outline="")
            c.create_line(*pts, fill=BLUE, width=2, smooth=True)
            c.create_text(pad_l + 6, pad_t + 2,
                          text=f"峰值 {self.peak_qps:.1f} QPS", anchor="nw",
                          fill=GREEN, font=("Consolas", 9))
        else:
            c.create_text(pad_l + cw / 2, pad_t + ch / 2, text="采集中…",
                          fill=FG_MUTED, font=("Microsoft YaHei UI", 10))

    # ---------------- 报告 ---------------- #
    def copy_report(self):
        if not self.last_report:
            messagebox.showinfo("报告", "还没有测试报告，先跑一轮。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_report)
        self.log("报告已复制到剪贴板", "info")

    def export_report(self):
        if not self.last_report:
            messagebox.showinfo("报告", "还没有测试报告，先跑一轮。")
            return
        path = filedialog.asksaveasfilename(
            title="导出测试报告",
            initialdir=SCRIPT_DIR,
            defaultextension=".txt",
            initialfile=time.strftime("压测报告_%Y%m%d_%H%M%S.txt"),
            filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"目标: {self.url_var.get().strip()}\n"
                        f"并发: {self.conc_var.get()}  时长: {self.dur_var.get()}s  "
                        f"超时: {self.to_var.get()}s\n"
                        f"上传体: {self.body_var.get().strip() or '0'}  "
                        f"流量上限: {self.cap_var.get().strip() or '不限'}\n"
                        f"代理: {self.file_var.get().strip() or '（本机 IP）'}\n"
                        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"{'=' * 60}\n{self.last_report}\n")
            self.log(f"报告已导出：{path}", "ok")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
