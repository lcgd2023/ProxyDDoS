#!/usr/bin/env node
// HTTP/HTTPS 应用层性能测试工具（Node.js 版）
// ⚠️ 仅可用于获得完整书面授权的自有服务器性能测试，禁止用于未授权目标
//
// 用法：
//   node http_pressure_test.mjs <url> [-p 代理.txt] [-c 并发] [-d 时长秒] [-t 超时秒] [--json]
//
// 与 Python 版 http_pressure_test.py 统计口径一致（-c/-d/-t = 并发/时长/超时，-p = 代理文件），
// 本版特点：零依赖（只用 Node 内置模块）、支持 http 代理（http 目标走绝对 URI，
// https 目标走 CONNECT 隧道）、失败原因分类。
// 浏览器版见同目录 index.html（双击即用，但不支持代理、受 CORS 限制）。

import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import process from 'node:process';
import { pathToFileURL } from 'node:url';

/** 用法类错误：干净地打印提示，不甩堆栈 */
class UsageError extends Error {}

// 常见浏览器 User-Agent，模拟真实用户访问
const USER_AGENTS = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
];

const REASON_ZH = {
  ECONNREFUSED: '连接被拒',
  ETIMEDOUT: '连接超时',
  ENOTFOUND: 'DNS 解析失败',
  EAI_AGAIN: 'DNS 临时失败',
  ECONNRESET: '连接被重置',
  EHOSTUNREACH: '主机不可达',
  ENETUNREACH: '网络不可达',
  EPIPE: '连接中断',
  TIMEOUT: '请求超时',
  UNEXPECTED_EOF: '连接被关闭',
  ERR_TLS_CERT_ALTNAME_INVALID: '证书域名不符',
  DEPTH_ZERO_SELF_SIGNED_CERT: '自签证书',
};

const USAGE = `HTTP 压力测试工具（Node.js 版，仅用于合法授权场景）

用法:
  node http_pressure_test.mjs <url> [选项]

选项:
  -p, --proxies <文件>   代理列表文件，每行一个（支持 ip:port / ip:port:用户:密 / http://…）
  -c, --concurrency <n>  最大并发数（默认 5，1~1000）
  -d, --duration <秒>    测试时长（默认 30，1~7200）
  -t, --timeout <秒>     单请求超时（默认 10，1~120）
      --json             结束后额外输出一行 JSON 结果（方便脚本解析）
  -h, --help             显示本帮助

示例:
  node http_pressure_test.mjs http://127.0.0.1:8080/ -c 10 -d 20
  node http_pressure_test.mjs https://example.com/ -p proxies.txt -c 5
`;

// --------------------------------------------------------------------------- #
// 统计
// --------------------------------------------------------------------------- #
class Stats {
  constructor() {
    this.total = 0;
    this.success = 0;
    this.failed = 0;
    this.statusCodes = new Map();
    this.errors = new Map();
    this.totalRespTime = 0;
    this.peakQps = 0;
    this.startTime = Date.now();
  }

  _bump(map, key) {
    map.set(key, (map.get(key) || 0) + 1);
  }

  addSuccess(status, ms) {
    this.total += 1;
    this.success += 1;
    this._bump(this.statusCodes, status);
    this.totalRespTime += ms;
  }

  addFailed(reason) {
    this.total += 1;
    this.failed += 1;
    if (reason) this._bump(this.errors, reason);
  }

  _sorted(map) {
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }

  snapshot() {
    const elapsed = (Date.now() - this.startTime) / 1000;
    const qps = elapsed > 0 ? this.total / elapsed : 0;
    if (qps > this.peakQps) this.peakQps = qps;
    return {
      elapsed,
      total: this.total,
      success: this.success,
      failed: this.failed,
      qps,
      peakQps: this.peakQps,
      avgMs: this.success ? (this.totalRespTime / this.success) : 0,
      statusCodes: this._sorted(this.statusCodes),
      errors: this._sorted(this.errors).slice(0, 5),
    };
  }

  report() {
    const s = this.snapshot();
    const pct = (n) => (s.total ? (n / s.total) * 100 : 0);
    const lines = [
      '',
      '='.repeat(60),
      '📊 测试完成报告',
      `⏱️  总测试时长: ${s.elapsed.toFixed(2)} 秒`,
      `📤 总请求数: ${s.total}`,
      `✅ 成功请求: ${s.success} | ❌ 失败请求: ${s.failed}`,
      `⚡ 每秒请求数(QPS): ${s.qps.toFixed(2)}`,
      `⏳ 平均响应时间: ${s.avgMs.toFixed(2)} ms`,
    ];
    if (s.success > 0) {
      lines.push('🔢 状态码分布:');
      for (const [code, n] of [...s.statusCodes].sort((a, b) => a[0] - b[0])) {
        lines.push(`    HTTP ${code}: ${n} 次 (${pct(n).toFixed(1)}%)`);
      }
    }
    if (s.failed > 0) {
      lines.push('❌ 失败原因分布:');
      for (const [reason, n] of this._sorted(this.errors).slice(0, 5)) {
        lines.push(`    ${reason}: ${n} 次 (${pct(n).toFixed(1)}%)`);
      }
    }
    lines.push('='.repeat(60));
    return lines.join('\n');
  }
}

// --------------------------------------------------------------------------- #
// 参数
// --------------------------------------------------------------------------- #
function parseArgs(argv) {
  const opts = { proxies: null, concurrency: 5, duration: 30, timeout: 10, json: false, url: null, help: false };
  const next = (i, flag) => {
    const v = argv[i + 1];
    if (v === undefined) throw new UsageError(`缺少 ${flag} 的值`);
    return v;
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    switch (a) {
      case '-p': case '--proxies': opts.proxies = next(i, a); i++; break;
      case '-c': case '--concurrency': opts.concurrency = Number(next(i, a)); i++; break;
      case '-d': case '--duration': opts.duration = Number(next(i, a)); i++; break;
      case '-t': case '--timeout': opts.timeout = Number(next(i, a)); i++; break;
      case '--json': opts.json = true; break;
      case '-h': case '--help': opts.help = true; break;
      default:
        if (a.startsWith('-')) throw new UsageError(`未知参数 ${a}（-h 看帮助）`);
        if (opts.url) throw new UsageError('只能有一个目标地址');
        opts.url = a;
    }
  }
  return opts;
}

function normalizeUrl(raw) {
  let u = String(raw || '').trim();
  if (!u) throw new UsageError('请填写目标地址');
  if (!u.includes('://')) u = 'http://' + u;
  let parsed;
  try {
    parsed = new URL(u);
  } catch {
    throw new UsageError('目标地址不合法，例如 http://127.0.0.1:8080/');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new UsageError('目标只支持 http / https');
  }
  return parsed;
}

function toInt(value, name, lo, hi) {
  const n = Number(value);
  if (!Number.isFinite(n) || !Number.isInteger(n)) throw new UsageError(`${name} 必须是整数`);
  if (n < lo || n > hi) throw new UsageError(`${name} 要在 ${lo}~${hi} 之间`);
  return n;
}

// --------------------------------------------------------------------------- #
// 代理
// --------------------------------------------------------------------------- #
function normalizeProxy(line) {
  const s = String(line || '').trim();
  if (!s || s.startsWith('#')) return null;
  const token = s.split(/\s+/)[0].replace(/,+$/, '');
  if (token.includes('://')) {
    const scheme = token.split('://', 1)[0].toLowerCase();
    return scheme === 'http' || scheme === 'https' ? token : null; // socks 需额外实现，跳过
  }
  // ip:port:用户:密  ->  http://用户:密@ip:port
  const m = token.match(/^([^:\s]+):(\d{1,5})(?::([^:\s]+):(\S*))?$/);
  if (m) {
    const [, host, port, user, pass] = m;
    if (user) return `http://${encodeURIComponent(user)}:${encodeURIComponent(pass || '')}@${host}:${port}`;
    return `http://${host}:${port}`;
  }
  if (token.includes('@')) return 'http://' + token; // 用户:密@ip:port
  return 'http://' + token;
}

function loadProxies(path) {
  const text = fs.readFileSync(path, 'utf8');
  const list = [];
  let skipped = 0;
  for (const line of text.split(/\r?\n/)) {
    const u = normalizeProxy(line);
    if (u) list.push(u);
    else if (line.trim() && !line.trim().startsWith('#')) skipped += 1;
  }
  return { list, skipped };
}

function reasonOf(err) {
  const code = err?.code || err?.cause?.code || err?.name || 'Error';
  const zh = REASON_ZH[code];
  return zh ? `${code}（${zh}）` : String(code);
}

// --------------------------------------------------------------------------- #
// 单次请求
// --------------------------------------------------------------------------- #
function baseHeaders() {
  return {
    'User-Agent': USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)],
    Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'identity',
    Connection: 'keep-alive',
  };
}

/**
 * 发一次请求，返回 {ok, status, ms, reason}
 * 与 Python 版一致：拿到状态码就算成功（读前 4KB 就断开）；ssl 证书不校验。
 */
function makeRequest(target, proxy, timeoutMs) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    let settled = false;
    let timer = null;
    let req = null;

    const done = (ok, status, reason) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve({ ok, status: status || 0, ms: Date.now() - t0, reason });
    };
    const fail = (e) => done(false, 0, reasonOf(e || new Error('unknown')));
    const onResp = (res) => {
      let got = 0;
      res.on('data', (chunk) => {
        got += chunk.length;
        if (got > 4096 && typeof res.destroy === 'function') res.destroy();
      });
      res.on('end', () => done(true, res.statusCode));
      res.on('aborted', () => done(true, res.statusCode));
      res.on('error', () => done(true, res.statusCode));
    };

    timer = setTimeout(() => {
      const e = new Error('timeout');
      e.code = 'TIMEOUT';
      try { if (req) req.destroy(); } catch { /* 忽略 */ }
      done(false, 0, reasonOf(e));
    }, timeoutMs);

    try {
      const u = normalizeUrl(target);
      const path = (u.pathname || '/') + (u.search || '');
      const isHttps = u.protocol === 'https:';
      const headers = baseHeaders();

      if (!proxy) {
        const mod = isHttps ? https : http;
        req = mod.request({
          hostname: u.hostname,
          port: u.port || (isHttps ? 443 : 80),
          path,
          method: 'GET',
          headers,
          rejectUnauthorized: false, // 与 Python 版 ssl=False 一致
        }, onResp);
        req.on('error', fail);
        req.end();
        return;
      }

      const p = new URL(proxy);
      const pPort = Number(p.port || (p.protocol === 'https:' ? 443 : 80));
      const pHeaders = { host: u.host };
      if (p.username) {
        pHeaders['proxy-authorization'] =
          'Basic ' + Buffer.from(`${decodeURIComponent(p.username)}:${decodeURIComponent(p.password || '')}`).toString('base64');
      }

      if (!isHttps) {
        // http 目标走 http 代理：请求行用绝对 URI
        req = http.request({
          hostname: p.hostname,
          port: pPort,
          path: u.toString(),
          method: 'GET',
          headers: { ...headers, ...pHeaders },
        }, onResp);
        req.on('error', fail);
        req.end();
        return;
      }

      // https 目标走 http 代理：CONNECT 建隧道，再在隧道上做 TLS
      const connect = http.request({
        hostname: p.hostname,
        port: pPort,
        method: 'CONNECT',
        path: `${u.hostname}:${u.port || 443}`,
        headers: pHeaders,
      });
      req = connect;
      connect.on('error', fail);
      connect.on('connect', (ires, socket, head) => {
        if (ires.statusCode !== 200) {
          socket.destroy();
          const e = new Error(`CONNECT ${ires.statusCode}`);
          e.code = `PROXY_CONNECT_${ires.statusCode}`;
          fail(e);
          return;
        }
        if (head && head.length) socket.unshift(head);
        const ts = tls.connect({ socket, servername: u.hostname, rejectUnauthorized: false }, () => {
          const r2 = https.request({
            hostname: u.hostname,
            port: u.port || 443,
            path,
            method: 'GET',
            headers,
            agent: false,
            rejectUnauthorized: false,
            createConnection: () => ts,
          }, onResp);
          req = r2;
          r2.on('error', fail);
          r2.end();
        });
        ts.on('error', fail);
      });
      connect.end();
    } catch (e) {
      fail(e);
    }
  });
}

// --------------------------------------------------------------------------- #
// 主循环
// --------------------------------------------------------------------------- #
async function runTest({ url, proxies, concurrency, duration, timeout, onProgress, shouldStop, progressInterval = 2 }) {
  const stats = new Stats();
  const deadline = Date.now() + duration * 1000;
  let stopped = false;

  const worker = async () => {
    while (!stopped && Date.now() < deadline) {
      if (shouldStop && shouldStop()) {
        stopped = true;
        break;
      }
      const proxy = proxies.length ? proxies[Math.floor(Math.random() * proxies.length)] : null;
      const t0 = Date.now();
      const r = await makeRequest(url, proxy, timeout * 1000);
      if (r.ok) stats.addSuccess(r.status, r.ms);
      else stats.addFailed(r.reason);
      // 请求"瞬间失败"（代理非法、端口秒拒）时补一点间隔：不烧 CPU、数字也真实
      if (Date.now() - t0 < 2) await new Promise((res) => setTimeout(res, 20));
      if (stopped) break;
    }
  };

  const timer = setInterval(() => onProgress && onProgress(stats.snapshot()), progressInterval * 1000);
  try {
    await Promise.all(Array.from({ length: concurrency }, () => worker()));
  } finally {
    clearInterval(timer);
  }
  return stats;
}

// --------------------------------------------------------------------------- #
// 入口
// --------------------------------------------------------------------------- #
function printProgress(s) {
  process.stdout.write(
    `\r⏱️  已运行 ${s.elapsed.toFixed(0)}s | 已发送: ${s.total} | 成功: ${s.success} | ` +
    `失败: ${s.failed} | 当前QPS: ${s.qps.toFixed(1)}   `,
  );
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) { process.stdout.write(USAGE); return; }
  if (!opts.url) throw new UsageError('缺少目标地址');

  const target = normalizeUrl(opts.url);
  const concurrency = toInt(opts.concurrency, '并发数', 1, 1000);
  const duration = toInt(opts.duration, '测试时长', 1, 7200);
  const timeout = toInt(opts.timeout, '超时时间', 1, 120);

  let proxies = [];
  let skipped = 0;
  if (opts.proxies) {
    try {
      ({ list: proxies, skipped } = loadProxies(opts.proxies));
    } catch (e) {
      process.stderr.write(`❌ 加载代理文件失败: ${e.message}\n`);
      process.exit(2);
    }
    if (!proxies.length) {
      process.stdout.write('⚠️  代理文件里没有可用的 HTTP 代理，将使用本机 IP 发起请求\n');
    } else {
      process.stdout.write(`✅ 已加载 ${proxies.length} 个可用代理`);
      if (skipped) process.stdout.write(`（跳过 ${skipped} 条 SOCKS/无法识别）`);
      process.stdout.write('\n');
    }
  } else {
    process.stdout.write('ℹ️  未指定代理文件，将使用本机 IP 直接发起请求\n');
  }

  process.stdout.write(
    `\n🚀 开始压测配置:\n🎯 目标地址: ${target.toString()}\n🔀 并发数: ${concurrency}\n` +
    `⏰ 测试时长: ${duration}秒\n⌛ 请求超时: ${timeout}秒\n` +
    `💡 提示: 按 Ctrl+C 可随时停止测试\n\n`,
  );

  let stopFlag = false;
  process.on('SIGINT', () => {
    if (stopFlag) process.exit(1);
    stopFlag = true;
    process.stdout.write('\n\n🛑 收到停止指令，正在等待剩余请求完成...\n');
  });

  const stats = await runTest({
    url: target.toString(),
    proxies,
    concurrency,
    duration,
    timeout,
    onProgress: printProgress,
    shouldStop: () => stopFlag,
    progressInterval: 2,
  });

  process.stdout.write('\n' + stats.report() + '\n');
  if (opts.json) {
    const s = stats.snapshot();
    process.stdout.write(JSON.stringify(s) + '\n');
  }
}

// 只有被直接执行时才跑 CLI；被 import 时只导出可复用的函数（方便写测试）
const invokedUrl = process.argv[1] ? pathToFileURL(process.argv[1]).href : '';
const isDirectRun = !!invokedUrl && (
  import.meta.url === invokedUrl ||
  (process.platform === 'win32' && import.meta.url.toLowerCase() === invokedUrl.toLowerCase())
);

if (isDirectRun) {
  main().catch((e) => {
    if (e instanceof UsageError) {
      process.stderr.write(`❌ ${e.message}\n\n${USAGE}`);
      process.exit(2);
    }
    process.stderr.write(`❌ ${e && e.stack ? e.stack : e}\n`);
    process.exit(1);
  });
}

export { Stats, loadProxies, makeRequest, normalizeProxy, normalizeUrl, runTest, toInt };
