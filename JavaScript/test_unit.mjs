// 单元自测：node test_unit.mjs
import assert from 'node:assert/strict';

import { Stats, loadProxies, normalizeProxy, normalizeUrl, toInt } from './http_pressure_test.mjs';

let n = 0;
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); n += 1; };
const ok = (v, msg) => { assert.ok(v, msg); n += 1; };
const throws = (fn, msg) => { assert.throws(fn, msg); n += 1; };

// 代理规整（与 Python 版一致 + 额外支持 ip:port:用户:密）
eq(normalizeProxy('18.163.99.118:80'), 'http://18.163.99.118:80', '裸 ip:port 补 scheme');
eq(normalizeProxy('  36.6.145.177:8089 '), 'http://36.6.145.177:8089', '带空白');
eq(normalizeProxy('http://1.2.3.4:8080'), 'http://1.2.3.4:8080', '已带 scheme 不重复');
eq(normalizeProxy('socks5://1.2.3.4:1080'), null, 'socks 跳过');
eq(normalizeProxy('# 注释'), null, '注释跳过');
eq(normalizeProxy(''), null, '空行跳过');
eq(normalizeProxy('103.118.124.137:6969:myuser:mypass'),
   'http://myuser:mypass@103.118.124.137:6969', 'ip:port:用户:密');

// 目标地址
eq(normalizeUrl('127.0.0.1:8080').toString(), 'http://127.0.0.1:8080/', '自动补 scheme');
eq(normalizeUrl('https://example.com/a?b=1').toString(), 'https://example.com/a?b=1', 'https 保留');
throws(() => normalizeUrl('ftp://x'), '只支持 http/https');
throws(() => normalizeUrl(''), '空地址');

// 参数范围
eq(toInt('12', '并发数', 1, 1000), 12, '整数解析');
throws(() => toInt('0', '并发数', 1, 1000), '下界');
throws(() => toInt('abc', '并发数', 1, 1000), '非数字');

// 代理文件
import { fileURLToPath } from 'node:url';
const { list, skipped } = loadProxies(fileURLToPath(new URL('./_test_proxies.txt', import.meta.url)));
eq(list.length, 4, '可用代理 4 条');
eq(skipped, 1, '跳过 1 条 socks');
ok(list.includes('http://myuser:mypass@103.118.124.137:6969'), '带账号代理已转成 URL');

// 统计与报告
const st = new Stats();
st.addSuccess(200, 16);
st.addSuccess(200, 24);
st.addFailed('ECONNREFUSED（连接被拒）');
const snap = st.snapshot();
eq(snap.total, 3, '总数');
eq(snap.success, 2, '成功数');
eq(snap.failed, 1, '失败数');
eq(snap.avgMs, 20, '平均响应 20ms');
eq(snap.statusCodes, [[200, 2]], '状态码分布');
eq(snap.errors, [['ECONNREFUSED（连接被拒）', 1]], '失败原因');
const rep = st.report();
ok(rep.includes('状态码分布'), '报告含状态码');
ok(rep.includes('失败原因分布'), '报告含失败原因');
ok(rep.includes('HTTP 200: 2 次 (66.7%)'), '报告百分比');

console.log(`✅ 全部通过：${n} 项断言`);
