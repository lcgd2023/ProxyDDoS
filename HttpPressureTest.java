import java.io.BufferedInputStream;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.net.Authenticator;
import java.net.ConnectException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.PasswordAuthentication;
import java.net.Proxy;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.net.UnknownHostException;
import java.nio.charset.Charset;
import java.nio.charset.CharsetEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.IntFunction;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLException;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * HTTP/HTTPS 应用层性能测试工具（Java 版）
 * ⚠️ 仅可用于获得完整书面授权的自有服务器性能测试，禁止用于未授权目标。
 *
 * 零依赖：只用 JDK 标准库（Socket / SSLSocket），单文件直接运行，不需要编译：
 *   java HttpPressureTest.java <url> [-p 代理.txt] [-c 5] [-d 30] [-t 10] [--json]
 *   java HttpPressureTest.java --selftest      # 内置自测（只用本机回环）
 *
 * 与 Python 版 / Node 版统计口径一致；Java 版额外支持 socks4/socks5 代理
 * （SOCKS5 交给 JDK 原生实现，SOCKS4 由本文件内置握手——JDK 不会说 v4）。
 */
public class HttpPressureTest {

  // 常见浏览器 User-Agent，模拟真实用户访问
  static final String[] USER_AGENTS = {
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
  };

  static final String USAGE = """
      HTTP 压力测试工具（Java 版，仅用于合法授权场景）

      用法:
        java HttpPressureTest.java <url> [选项]

      选项:
        -p, --proxies <文件>   代理列表文件，每行一个
                               （支持 ip:port / ip:port:用户:密 / 用户:密@ip:port /
                                 http://… / socks4://… / socks5://…；
                                 SOCKS5 走 JDK，SOCKS4 走内置握手，两种都免依赖）
        -c, --concurrency <n>  最大并发数（默认 5，1~1000）
        -d, --duration <秒>    测试时长（默认 30，1~7200）
        -t, --timeout <秒>     单请求超时（默认 10，1~120）
            --stop-after <秒>  运行 N 秒后优雅停止（等价 Ctrl+C，方便脚本调用）
            --json             结束后额外输出一行 JSON 结果（方便脚本解析）
            --utf8             输出强制 UTF-8（默认跟随控制台编码；看 UTF-8 文件用）
            --selftest         跑内置自测（只用本机回环，不碰外部目标）
        -h, --help             显示本帮助

      示例:
        java HttpPressureTest.java http://127.0.0.1:8080/ -c 10 -d 20
        java HttpPressureTest.java https://example.com/ -p ../proxies.txt -c 5
      """;

  /** 用法类错误：干净地打印提示，不甩堆栈 */
  static final class UsageException extends Exception {
    UsageException(String m) {
      super(m);
    }
  }

  // ---------------------------------------------------------------------------
  // 参数
  // ---------------------------------------------------------------------------

  static final class Opts {
    String url;
    String proxies;
    int concurrency = 5;
    int duration = 30;
    int timeout = 10;
    int stopAfter = 0;
    boolean json;
    boolean help;
    boolean selftest;
    boolean utf8;
  }

  static Opts parseArgs(String[] argv) throws UsageException {
    Opts o = new Opts();
    for (int i = 0; i < argv.length; i++) {
      String a = argv[i];
      switch (a) {
        case "-p", "--proxies" -> o.proxies = nextArg(argv, i++, a);
        case "-c", "--concurrency" -> o.concurrency = parseIntArg(nextArg(argv, i++, a), a);
        case "-d", "--duration" -> o.duration = parseIntArg(nextArg(argv, i++, a), a);
        case "-t", "--timeout" -> o.timeout = parseIntArg(nextArg(argv, i++, a), a);
        case "--stop-after" -> o.stopAfter = parseIntArg(nextArg(argv, i++, a), a);
        case "--json" -> o.json = true;
        case "--selftest" -> o.selftest = true;
        case "--utf8" -> o.utf8 = true;
        case "-h", "--help" -> o.help = true;
        default -> {
          if (a.startsWith("-")) throw new UsageException("未知参数 " + a + "（-h 看帮助）");
          if (o.url != null) throw new UsageException("只能有一个目标地址");
          o.url = a;
        }
      }
    }
    return o;
  }

  static String nextArg(String[] argv, int idx, String flag) throws UsageException {
    if (idx + 1 >= argv.length) throw new UsageException("缺少 " + flag + " 的值");
    return argv[idx + 1];
  }

  static int parseIntArg(String v, String flag) throws UsageException {
    try {
      return Integer.parseInt(v.trim());
    } catch (NumberFormatException e) {
      throw new UsageException(flag + " 要求整数，收到「" + v + "」");
    }
  }

  /** 补全 scheme、校验 http/https，返回规范化的 URL */
  static String normalizeUrl(String raw) throws UsageException {
    String u = raw == null ? "" : raw.trim();
    if (u.isEmpty()) throw new UsageException("请填写目标地址");
    if (!u.contains("://")) u = "http://" + u;
    URI parsed;
    try {
      parsed = URI.create(u);
    } catch (IllegalArgumentException e) {
      throw new UsageException("目标地址不合法，例如 http://127.0.0.1:8080/");
    }
    String scheme = parsed.getScheme();
    if (scheme == null || (!scheme.equalsIgnoreCase("http") && !scheme.equalsIgnoreCase("https"))) {
      throw new UsageException("目标只支持 http / https");
    }
    if (parsed.getHost() == null || parsed.getHost().isEmpty()) {
      throw new UsageException("目标地址不合法，例如 http://127.0.0.1:8080/");
    }
    StringBuilder sb = new StringBuilder();
    sb.append(scheme.toLowerCase(Locale.ROOT)).append("://").append(parsed.getRawAuthority());
    String path = parsed.getRawPath();
    if (path == null || path.isEmpty()) path = "/";
    sb.append(path);
    if (parsed.getRawQuery() != null) sb.append('?').append(parsed.getRawQuery());
    return sb.toString();
  }

  static int toInt(int value, String name, int lo, int hi) throws UsageException {
    if (value < lo || value > hi) throw new UsageException(name + " 要在 " + lo + "~" + hi + " 之间");
    return value;
  }

  // ---------------------------------------------------------------------------
  // 代理
  // ---------------------------------------------------------------------------

  static final class ProxySpec {
    final boolean socks;
    final int version; // SOCKS 协议版本 4/5（非 SOCKS 为 0）
    final String host;
    final int port;
    final String user;
    final String pass;

    ProxySpec(boolean socks, String host, int port, String user, String pass) {
      this(socks, socks ? 5 : 0, host, port, user, pass);
    }

    ProxySpec(boolean socks, int version, String host, int port, String user, String pass) {
      this.socks = socks;
      this.version = socks ? (version == 4 ? 4 : 5) : 0;
      this.host = host;
      this.port = port;
      this.user = user;
      this.pass = pass;
    }

    String key() {
      return host + ":" + port;
    }
  }

  record ProxyFile(List<ProxySpec> list, int skipped) {}

  /**
   * 解析一行代理。返回 null 表示跳过（空行/注释/无法识别）。
   * 与 Python/Node 版一致：裸 ip:port 自动补 http://、ip:port:用户:密 自动转 URL；
   * 额外：socks4:// socks5:// 都认（v5 交 JDK，v4 内置握手），不跳过。
   */
  static ProxySpec normalizeProxy(String line) {
    if (line == null) return null;
    String s = line.trim();
    if (s.isEmpty() || s.startsWith("#")) return null;
    String token = s.split("\\s+")[0];
    if (token.endsWith(",")) token = token.substring(0, token.length() - 1);

    boolean socks = false;
    int socksVersion = 0;
    String rest = token;
    if (token.contains("://")) {
      int i = token.indexOf("://");
      String scheme = token.substring(0, i).toLowerCase(Locale.ROOT);
      rest = token.substring(i + 3);
      switch (scheme) {
        case "socks4" -> {
          socks = true;
          socksVersion = 4;
        }
        case "socks", "socks5" -> {
          socks = true;
          socksVersion = 5;
        }
        case "http", "https" -> socks = false;
        default -> {
          return null; // 其它 scheme（ssh:// 之类）跳过
        }
      }
    }

    String auth = null;
    int at = rest.lastIndexOf('@');
    if (at >= 0) {
      auth = rest.substring(0, at);
      rest = rest.substring(at + 1);
    }

    String host = "";
    int port = -1;
    String user = null;
    String pass = null;

    if (rest.startsWith("[")) { // IPv6: [2001:db8::1]:8080[:用户:密]
      int end = rest.indexOf(']');
      if (end < 0) return null;
      host = rest.substring(1, end);
      String tail = rest.substring(end + 1);
      if (!tail.startsWith(":")) return null;
      String[] seg = tail.substring(1).split(":", 3);
      if (seg.length >= 1) port = parseIntQuiet(seg[0]);
      if (seg.length >= 3) {
        user = seg[1];
        pass = seg[2];
      }
    } else {
      String[] seg = rest.split(":", 4); // host:port[:用户[:密]]
      if (seg.length < 2) return null;
      host = seg[0];
      port = parseIntQuiet(seg[1]);
      if (auth == null && seg.length >= 4) {
        user = seg[2];
        pass = seg[3];
      }
    }

    if (auth != null) { // 用户:密@host:port
      int c = auth.indexOf(':');
      if (c <= 0) return null;
      user = auth.substring(0, c);
      pass = auth.substring(c + 1);
    }

    if (host.isEmpty() || port < 1 || port > 65535) return null;
    if (user != null && user.isEmpty()) user = null;
    if (user != null && pass == null) pass = "";
    return new ProxySpec(socks, socksVersion, host, port, user, pass);
  }

  static int parseIntQuiet(String v) {
    try {
      return Integer.parseInt(v.trim());
    } catch (Exception e) {
      return -1;
    }
  }

  static ProxyFile loadProxies(String path) throws IOException {
    byte[] bytes = Files.readAllBytes(Path.of(path));
    String text;
    try {
      text = new String(bytes, StandardCharsets.UTF_8);
      if (text.indexOf('�') >= 0) throw new IOException("not utf8");
    } catch (IOException e) {
      text = new String(bytes, Charset.defaultCharset());
    }
    List<ProxySpec> list = new ArrayList<>();
    int skipped = 0;
    for (String line : text.split("\r?\n")) {
      ProxySpec p = normalizeProxy(line);
      if (p != null) list.add(p);
      else if (!line.trim().isEmpty() && !line.trim().startsWith("#")) skipped++;
    }
    return new ProxyFile(list, skipped);
  }

  /** SOCKS 代理带账密时，JDK 会向系统 Authenticator 要凭据；这里按 host:port 定向返回 */
  static void installAuthenticator(List<ProxySpec> proxies) {
    Map<String, String[]> map = new ConcurrentHashMap<>();
    for (ProxySpec p : proxies) {
      if (p.user != null) map.put(p.key(), new String[] {p.user, p.pass == null ? "" : p.pass});
    }
    if (map.isEmpty()) return;
    try {
      Authenticator.setDefault(new Authenticator() {
        @Override
        protected PasswordAuthentication getPasswordAuthentication() {
          if (getRequestorType() == RequestorType.PROXY) {
            String[] c = map.get(getRequestingHost() + ":" + getRequestingPort());
            if (c != null) return new PasswordAuthentication(c[0], c[1].toCharArray());
          }
          return null;
        }
      });
    } catch (SecurityException ignored) {
      // 装不上就退化成无认证 SOCKS
    }
  }

  // ---------------------------------------------------------------------------
  // 统计
  // ---------------------------------------------------------------------------

  record Snapshot(double elapsed, int total, int success, int failed, double qps, double peakQps,
                  double avgMs, List<Map.Entry<String, Integer>> codes, List<Map.Entry<String, Integer>> errors) {}

  static final class Stats {
    private int total;
    private int success;
    private int failed;
    private long totalRespTime;
    private double peakQps;
    private final Map<String, Integer> statusCodes = new LinkedHashMap<>();
    private final Map<String, Integer> errors = new LinkedHashMap<>();
    private final long startMs = System.currentTimeMillis();

    long startMillis() {
      return startMs;
    }

    synchronized void addSuccess(int status, long ms) {
      total++;
      success++;
      statusCodes.merge(String.valueOf(status), 1, Integer::sum);
      totalRespTime += ms;
    }

    synchronized void addFailed(String reason) {
      total++;
      failed++;
      if (reason != null && !reason.isEmpty()) errors.merge(reason, 1, Integer::sum);
    }

    synchronized Snapshot snapshot() {
      double elapsed = (System.currentTimeMillis() - startMs) / 1000.0;
      double qps = elapsed > 0 ? total / elapsed : 0;
      if (qps > peakQps) peakQps = qps;
      List<Map.Entry<String, Integer>> codes = new ArrayList<>(statusCodes.entrySet());
      codes.sort((a, b) -> {
        boolean na = isNumeric(a.getKey());
        boolean nb = isNumeric(b.getKey());
        if (na && nb) return Integer.compare(Integer.parseInt(a.getKey()), Integer.parseInt(b.getKey()));
        if (na) return -1;
        if (nb) return 1;
        return a.getKey().compareTo(b.getKey());
      });
      List<Map.Entry<String, Integer>> errs = new ArrayList<>(errors.entrySet());
      errs.sort((a, b) -> b.getValue() - a.getValue());
      return new Snapshot(elapsed, total, success, failed, qps, peakQps,
          success > 0 ? (double) totalRespTime / success : 0, codes, errs);
    }

    /** 与 Python/Node 版完全一致的报告格式；首字符是换行，方便接在进度行后面 */
    String report() {
      Snapshot s = snapshot();
      IntFunction<Double> share = n -> s.total() > 0 ? (n * 100.0) / s.total() : 0.0;
      StringBuilder b = new StringBuilder();
      b.append('\n').append("=".repeat(60)).append('\n');
      b.append("📊 测试完成报告\n");
      b.append(String.format(Locale.ROOT, "⏱️  总测试时长: %.2f 秒\n", s.elapsed()));
      b.append("📤 总请求数: ").append(s.total()).append('\n');
      b.append("✅ 成功请求: ").append(s.success()).append(" | ❌ 失败请求: ").append(s.failed()).append('\n');
      b.append(String.format(Locale.ROOT, "⚡ 每秒请求数(QPS): %.2f\n", s.qps()));
      b.append(String.format(Locale.ROOT, "⏳ 平均响应时间: %.2f ms\n", s.avgMs()));
      if (s.success() > 0) {
        b.append("🔢 状态码分布:\n");
        for (Map.Entry<String, Integer> e : s.codes()) {
          String k = e.getKey();
          String head = isNumeric(k) ? "    HTTP " + k : "    " + k;
          b.append(String.format(Locale.ROOT, "%s: %d 次 (%.1f%%)\n", head, e.getValue(), share.apply(e.getValue())));
        }
      }
      if (s.failed() > 0) {
        b.append("❌ 失败原因分布:\n");
        for (Map.Entry<String, Integer> e : s.errors()) {
          b.append(String.format(Locale.ROOT, "    %s: %d 次 (%.1f%%)\n", e.getKey(), e.getValue(), share.apply(e.getValue())));
        }
      }
      b.append("=".repeat(60));
      return b.toString();
    }
  }

  static boolean isNumeric(String s) {
    if (s.isEmpty()) return false;
    for (int i = 0; i < s.length(); i++) {
      if (!Character.isDigit(s.charAt(i))) return false;
    }
    return true;
  }

  // ---------------------------------------------------------------------------
  // 单次请求（裸 Socket，和 Node 版逻辑一一对应）
  // ---------------------------------------------------------------------------

  static final class Result {
    final boolean ok;
    final int status;
    final long ms;
    final String reason;

    Result(boolean ok, int status, long ms, String reason) {
      this.ok = ok;
      this.status = status;
      this.ms = ms;
      this.reason = reason;
    }

    static Result ok(int status, long ms) {
      return new Result(true, status, ms, null);
    }

    static Result fail(String reason, long ms) {
      return new Result(false, 0, ms, reason);
    }
  }

  static long elapsedMs(long t0) {
    return (System.nanoTime() - t0) / 1_000_000L;
  }

  /**
   * 发一次请求，返回 {ok, status, ms, reason}。
   * 与其它两版一致：拿到状态码就算成功、读 4KB 就断开、证书不校验、不跟随重定向。
   */
  static Result request(String target, ProxySpec proxy, int timeoutMs) {
    long t0 = System.nanoTime();
    Socket socket = null;
    try {
      URI u = URI.create(target);
      String host = u.getHost();
      if (host == null || host.isEmpty()) throw new UnknownHostException(target);
      boolean https = u.getScheme().equalsIgnoreCase("https");
      int port = u.getPort() != -1 ? u.getPort() : (https ? 443 : 80);
      String path = u.getRawPath();
      if (path == null || path.isEmpty()) path = "/";
      if (u.getRawQuery() != null) path += "?" + u.getRawQuery();

      // 1) 建 TCP（直连 / http 代理 / SOCKS 代理）
      if (proxy == null) {
        socket = new Socket();
        socket.setSoTimeout(timeoutMs);
        socket.connect(resolve(host, port), timeoutMs);
      } else if (!proxy.socks) {
        socket = new Socket();
        socket.setSoTimeout(timeoutMs);
        socket.connect(resolve(proxy.host, proxy.port), timeoutMs);
      } else if (proxy.version == 4) {
        // JDK 的 SOCKS 客户端只会说 v5（版本由全局 socksProxyVersion 属性定，没法按代理切换），
        // 所以 socks4:// 这类代理由这里手写握手，之后照样是裸 socket 收发
        socket = new Socket();
        socket.setSoTimeout(timeoutMs);
        socket.connect(resolve(proxy.host, proxy.port), timeoutMs);
        socks4Connect(socket, host, port, proxy.user);
      } else {
        socket = new Socket(new Proxy(Proxy.Type.SOCKS, resolve(proxy.host, proxy.port)));
        // SOCKS 握手发生在 connect() 内部，此时 connect 超时未必兜得住，先设读超时
        socket.setSoTimeout(timeoutMs);
        socket.connect(resolve(host, port), timeoutMs);
      }
      socket.setTcpNoDelay(true);

      boolean viaHttpProxy = proxy != null && !proxy.socks;
      boolean absoluteForm = viaHttpProxy && !https; // http 目标走代理才用绝对 URI
      OutputStream out;
      InputStream in;

      if (https) {
        if (viaHttpProxy) {
          // CONNECT 隧道：先跟代理握手，再在隧道上做 TLS
          StringBuilder cr = new StringBuilder();
          cr.append("CONNECT ").append(host).append(':').append(port).append(" HTTP/1.1\r\n");
          cr.append("Host: ").append(host).append(':').append(port).append("\r\n");
          appendProxyAuth(cr, proxy);
          cr.append("Connection: close\r\n\r\n");
          socket.getOutputStream().write(cr.toString().getBytes(StandardCharsets.UTF_8));
          socket.getOutputStream().flush();
          String head = readHead(socket.getInputStream());
          int code = parseStatus(firstLine(head));
          if (code != 200) {
            return Result.fail("PROXY_CONNECT_" + code + "（代理拒绝 CONNECT）", elapsedMs(t0));
          }
        }
        SSLSocket ssl = (SSLSocket) permissiveSslFactory().createSocket(socket, host, port, true);
        ssl.setSoTimeout(timeoutMs);
        ssl.startHandshake();
        out = ssl.getOutputStream();
        in = new BufferedInputStream(ssl.getInputStream()); // 逐字节读响应头走缓冲，避免每字节一次系统调用
        socket = ssl; // finally 关这个即可（autoClose 会连带关底层）
      } else {
        out = socket.getOutputStream();
        in = new BufferedInputStream(socket.getInputStream());
      }

      // 2) 写请求
      StringBuilder req = new StringBuilder();
      req.append("GET ").append(absoluteForm ? target : path).append(" HTTP/1.1\r\n");
      req.append("Host: ").append(host).append((port == 80 || port == 443) ? "" : ":" + port).append("\r\n");
      req.append("User-Agent: ").append(randomUa()).append("\r\n");
      req.append("Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n");
      req.append("Accept-Language: zh-CN,zh;q=0.9,en;q=0.8\r\n");
      req.append("Accept-Encoding: identity\r\n");
      if (absoluteForm) appendProxyAuth(req, proxy); // 隧道内是源站，不再发代理认证
      req.append("Connection: close\r\n\r\n");
      out.write(req.toString().getBytes(StandardCharsets.UTF_8));
      out.flush();

      // 3) 读状态码 + 响应头 + 少量响应体
      // 口径与 Python/Node 版一致：拿到状态码即算成功，响应体读不完、读超时都不改判
      int code = parseStatus(readLine(in));
      if (code < 0) return Result.fail("BAD_STATUS（响应不合法）", elapsedMs(t0));
      readTail(socket, in, timeoutMs);
      return Result.ok(code, elapsedMs(t0));
    } catch (Exception e) {
      return Result.fail(classify(e), elapsedMs(t0));
    } finally {
      closeQuietly(socket);
    }
  }

  /**
   * 读完响应头、再读一小段响应体（最多 4KB）。
   * 状态码已经拿到，所以这一步里的任何超时/断流都吞掉，不影响本次成功判定——
   * 否则服务器不按 Connection: close 断开时，一个 200 会被误判成 TIMEOUT。
   */
  static void readTail(Socket socket, InputStream in, int timeoutMs) {
    long contentLength = -1;
    boolean framed = false; // 有明确的结束边界（Content-Length 或 chunked）
    try {
      String head = readHead(in);
      contentLength = parseContentLength(head);
      framed = contentLength >= 0 || head.toLowerCase(Locale.ROOT).contains("transfer-encoding: chunked");
      // 头都读全了，说明响应在正常下发：把超时还原成整体超时
      socket.setSoTimeout(timeoutMs);
    } catch (IOException ignored) {
      // 响应头读不全（对方直接断流）：按“没有响应体”往下走
    }
    try {
      // 没有结束边界的响应（keep-alive 却不关连接）只给一个短读窗口，别把延迟撑大
      if (!framed) socket.setSoTimeout(Math.max(1, Math.min(timeoutMs, 800)));
      byte[] buf = new byte[4096];
      int want = contentLength >= 0 ? (int) Math.min(contentLength, buf.length) : buf.length;
      int got = 0;
      while (got < want) {
        int n = in.read(buf, got, want - got);
        if (n <= 0) break;
        got += n;
        if (contentLength < 0 && hasFinalChunk(buf, got)) break; // chunked 终块 "0\r\n\r\n"
      }
    } catch (IOException ignored) {
      // 超时 / 连接被重置：状态码已到手，照样算成功
    }
  }

  static long parseContentLength(String head) {
    for (String line : head.split("\r?\n")) {
      int i = line.indexOf(':');
      if (i < 0) continue;
      if (!line.substring(0, i).trim().equalsIgnoreCase("Content-Length")) continue;
      try {
        return Long.parseLong(line.substring(i + 1).trim());
      } catch (NumberFormatException e) {
        return -1;
      }
    }
    return -1;
  }

  /** 缓冲区末尾是否已出现 chunked 终块标记 */
  static boolean hasFinalChunk(byte[] b, int len) {
    if (len < 5) return false;
    int from = Math.max(0, len - 64);
    String tail = new String(b, from, len - from, StandardCharsets.ISO_8859_1);
    return tail.contains("0\r\n\r\n");
  }

  static InetSocketAddress resolve(String host, int port) throws UnknownHostException {
    InetSocketAddress a = new InetSocketAddress(host, port);
    if (a.isUnresolved()) throw new UnknownHostException(host);
    return a;
  }

  /**
   * 手写 SOCKS4/4a 握手（JDK 只支持 SOCKS5）。
   * 目标是点分 IPv4 就直接填 IP；是域名则用 SOCKS4a：IP 填 0.0.0.1，域名跟在 USERID 后面。
   * SOCKS4 只有 USERID 没有密码，认证信息只用用户名。
   */
  static void socks4Connect(Socket s, String host, int port, String user) throws IOException {
    byte[] uid = (user == null ? "" : user).getBytes(StandardCharsets.UTF_8);
    byte[] ip = ipv4Bytes(host);
    ByteArrayOutputStream req = new ByteArrayOutputStream();
    req.write(4);            // VN
    req.write(1);            // CD = CONNECT
    req.write((port >> 8) & 0xff);
    req.write(port & 0xff);
    if (ip != null) {
      req.write(ip, 0, 4);
    } else {
      req.write(0); req.write(0); req.write(0); req.write(1); // SOCKS4a 占位 IP
    }
    req.write(uid, 0, uid.length);
    req.write(0);            // USERID 结束
    if (ip == null) {
      byte[] name = host.getBytes(StandardCharsets.UTF_8);
      req.write(name, 0, name.length);
      req.write(0);          // 域名结束
    }
    OutputStream out = s.getOutputStream();
    out.write(req.toByteArray());
    out.flush();

    byte[] rep = s.getInputStream().readNBytes(8);
    if (rep.length < 8) throw new IOException("SOCKS4: 代理应答只有 " + rep.length + " 字节");
    if (rep[1] != 0x5A) {
      throw new IOException("SOCKS4: 代理拒绝连接 (CD=" + (rep[1] & 0xff) + ")");
    }
  }

  /** 点分 IPv4 → 4 字节；不是合法 IPv4 就返回 null（走 SOCKS4a 带域名） */
  static byte[] ipv4Bytes(String host) {
    String[] p = host.split("\\.");
    if (p.length != 4) return null;
    byte[] b = new byte[4];
    for (int i = 0; i < 4; i++) {
      if (p[i].isEmpty() || p[i].length() > 3) return null;
      for (char c : p[i].toCharArray()) if (c < '0' || c > '9') return null;
      int v;
      try {
        v = Integer.parseInt(p[i]);
      } catch (NumberFormatException e) {
        return null;
      }
      if (v < 0 || v > 255) return null;
      b[i] = (byte) v;
    }
    return b;
  }

  static void appendProxyAuth(StringBuilder sb, ProxySpec p) {
    if (p == null || p.user == null) return;
    String raw = p.user + ":" + (p.pass == null ? "" : p.pass);
    sb.append("Proxy-Authorization: Basic ")
        .append(Base64.getEncoder().encodeToString(raw.getBytes(StandardCharsets.UTF_8)))
        .append("\r\n");
  }

  static String randomUa() {
    return USER_AGENTS[ThreadLocalRandom.current().nextInt(USER_AGENTS.length)];
  }

  /** 读一行（不含 CRLF）；空响应抛 IOException */
  static String readLine(InputStream in) throws IOException {
    ByteArrayOutputStream buf = new ByteArrayOutputStream();
    int b;
    while ((b = in.read()) != -1) {
      if (b == '\n') break;
      if (b != '\r') {
        buf.write(b);
        if (buf.size() > 2048) break;
      }
    }
    if (buf.size() == 0) throw new IOException("空响应");
    return buf.toString(StandardCharsets.UTF_8);
  }

  /** 逐字节读到空行为止（不会多读，CONNECT 之后还要接 TLS 握手） */
  static String readHead(InputStream in) throws IOException {
    ByteArrayOutputStream buf = new ByteArrayOutputStream();
    int b, p1 = -1, p2 = -1, p3 = -1; // 前 1/2/3 个字节
    while ((b = in.read()) != -1) {
      buf.write(b);
      if (buf.size() > 8192) break;
      if (b == '\n') {
        // 请求/响应头以 CRLF CRLF 结尾，也要兼容只用 LF 的实现
        if (p1 == '\n' || (p1 == '\r' && p2 == '\n' && p3 == '\r')) break;
      }
      p3 = p2;
      p2 = p1;
      p1 = b;
    }
    return buf.toString(StandardCharsets.UTF_8);
  }

  static String firstLine(String head) {
    int i = head.indexOf('\n');
    String line = i >= 0 ? head.substring(0, i) : head;
    return line.trim();
  }

  static int parseStatus(String statusLine) {
    if (statusLine == null || statusLine.isEmpty()) return -1;
    String[] p = statusLine.trim().split("\\s+");
    if (p.length < 2) return -1;
    try {
      int code = Integer.parseInt(p[1]);
      return (code >= 100 && code <= 599) ? code : -1;
    } catch (NumberFormatException e) {
      return -1;
    }
  }

  /** 异常 → 和 Node 版同一套原因标签，方便三份报告对照 */
  static String classify(Throwable e) {
    String msg = e.getMessage() == null ? "" : e.getMessage();
    String cls = e.getClass().getSimpleName();
    if (e instanceof UnknownHostException) return "ENOTFOUND（DNS 解析失败）";
    if (e instanceof SocketTimeoutException) {
      return msg.toLowerCase(Locale.ROOT).contains("connect") ? "ETIMEDOUT（连接超时）" : "TIMEOUT（请求超时）";
    }
    if (e instanceof SSLException) return "SSL_ERROR（证书/TLS 错误: " + abbrev(cls) + "）";
    String lower = msg.toLowerCase(Locale.ROOT);
    if (cls.contains("Socks") || lower.contains("socks")) return "SOCKS_ERROR（SOCKS 代理错误: " + abbrev(msg) + "）";
    if (lower.contains("refused")) return "ECONNREFUSED（连接被拒）";
    if (lower.contains("timed out")) return "ETIMEDOUT（连接超时）";
    if (lower.contains("reset") || lower.contains("broken pipe")) return "ECONNRESET（连接被重置）";
    if (lower.contains("unreachable") || lower.contains("no route")) return "EHOSTUNREACH（主机不可达）";
    if (e instanceof ConnectException) return "ECONNREFUSED（连接被拒）";
    return cls + "（" + abbrev(msg) + "）";
  }

  static String abbrev(String s) {
    if (s == null || s.isEmpty()) return "无详情";
    return s.length() > 60 ? s.substring(0, 57) + "…" : s;
  }

  /** 压测不校验证书（与 Python ssl=False / Node rejectUnauthorized:false 一致） */
  static SSLSocketFactory permissiveSslFactory() {
    try {
      SSLContext ctx = SSLContext.getInstance("TLS");
      ctx.init(null, new TrustManager[] {new X509TrustManager() {
        @Override
        public void checkClientTrusted(X509Certificate[] chain, String authType) {}

        @Override
        public void checkServerTrusted(X509Certificate[] chain, String authType) {}

        @Override
        public X509Certificate[] getAcceptedIssuers() {
          return new X509Certificate[0];
        }
      }}, new SecureRandom());
      return ctx.getSocketFactory();
    } catch (Exception e) {
      return (SSLSocketFactory) SSLSocketFactory.getDefault();
    }
  }

  // ---------------------------------------------------------------------------
  // 主循环
  // ---------------------------------------------------------------------------

  static final AtomicBoolean STOP = new AtomicBoolean(false);
  static volatile CountDownLatch MAIN_DONE;

  static Stats runTest(String target, List<ProxySpec> proxies, int concurrency, int duration, int timeout,
      int stopAfter) throws InterruptedException {
    Stats stats = new Stats();
    ThreadFactory threadMaker = r -> {
      Thread t = new Thread(r, "pressure-worker");
      t.setDaemon(true);
      return t;
    };
    ExecutorService pool = Executors.newFixedThreadPool(concurrency, threadMaker);
    CountDownLatch done = new CountDownLatch(concurrency);
    long deadline = System.currentTimeMillis() + duration * 1000L;
    for (int i = 0; i < concurrency; i++) {
      pool.execute(() -> {
        try {
          while (!STOP.get() && System.currentTimeMillis() < deadline) {
            ProxySpec p = proxies.isEmpty() ? null : proxies.get(ThreadLocalRandom.current().nextInt(proxies.size()));
            long t0 = System.currentTimeMillis();
            Result r = request(target, p, timeout * 1000);
            if (r.ok) stats.addSuccess(r.status, r.ms);
            else stats.addFailed(r.reason);
            // 瞬间失败时补一点间隔：不烧 CPU、QPS 也不虚高
            if (System.currentTimeMillis() - t0 < 2) Thread.sleep(20);
          }
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
        } finally {
          done.countDown();
        }
      });
    }

    long lastPrint = 0;
    while (!done.await(300, TimeUnit.MILLISECONDS)) {
      long now = System.currentTimeMillis();
      if (stopAfter > 0 && now - stats.startMillis() >= stopAfter * 1000L) STOP.set(true);
      if (now - lastPrint >= 2000) {
        printProgress(stats.snapshot());
        lastPrint = now;
      }
    }
    pool.shutdown();
    return stats;
  }

  static void printProgress(Snapshot s) {
    System.out.print(vis(String.format(Locale.ROOT,
        "\r⏱️  已运行 %ds | 已发送: %d | 成功: %d | 失败: %d | 当前QPS: %.1f   ",
        (long) s.elapsed(), s.total(), s.success(), s.failed(), s.qps())));
    System.out.flush();
  }

  static String buildJson(Snapshot s) {
    StringBuilder b = new StringBuilder("{");
    b.append(String.format(Locale.ROOT, "\"elapsed\":%.3f", s.elapsed()));
    b.append(String.format(Locale.ROOT, ",\"total\":%d", s.total()));
    b.append(String.format(Locale.ROOT, ",\"success\":%d", s.success()));
    b.append(String.format(Locale.ROOT, ",\"failed\":%d", s.failed()));
    b.append(String.format(Locale.ROOT, ",\"qps\":%.3f", s.qps()));
    b.append(String.format(Locale.ROOT, ",\"peakQps\":%.3f", s.peakQps()));
    b.append(String.format(Locale.ROOT, ",\"avgMs\":%.2f", s.avgMs()));
    b.append(",\"statusCodes\":[");
    for (int i = 0; i < s.codes().size(); i++) {
      Map.Entry<String, Integer> e = s.codes().get(i);
      if (i > 0) b.append(',');
      b.append('[').append(isNumeric(e.getKey()) ? e.getKey() : "\"" + jsonEscape(e.getKey()) + "\"")
          .append(',').append(e.getValue()).append(']');
    }
    b.append("],\"errors\":[");
    for (int i = 0; i < s.errors().size(); i++) {
      Map.Entry<String, Integer> e = s.errors().get(i);
      if (i > 0) b.append(',');
      b.append("[\"").append(jsonEscape(e.getKey())).append("\",").append(e.getValue()).append(']');
    }
    b.append("]}");
    return b.toString();
  }

  static String jsonEscape(String s) {
    StringBuilder b = new StringBuilder();
    for (int i = 0; i < s.length(); i++) {
      char ch = s.charAt(i);
      switch (ch) {
        case '"' -> b.append("\\\"");
        case '\\' -> b.append("\\\\");
        case '\n' -> b.append("\\n");
        case '\r' -> b.append("\\r");
        case '\t' -> b.append("\\t");
        default -> {
          if (ch < 0x20) b.append(String.format("\\u%04x", (int) ch));
          else b.append(ch);
        }
      }
    }
    return b.toString();
  }

  static void registerStopHook(int timeoutSec) {
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      CountDownLatch l = MAIN_DONE;
      if (l == null || l.getCount() == 0) return;
      STOP.set(true);
      try {
        System.out.println();
        p("🛑 收到停止指令，正在等待剩余请求完成...");
        System.out.flush();
      } catch (Throwable ignored) {
      }
      try {
        l.await(timeoutSec + 3L, TimeUnit.SECONDS);
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
      }
    }, "pressure-stop-hook"));
  }

  // ---------------------------------------------------------------------------
  // 入口
  // ---------------------------------------------------------------------------

  /**
   * 输出编码策略：
   * Java 在中文 Windows 上的 stdout 编码是 GBK（哪怕重定向到文件也一样，而
   * System.console() 在 stdin 还是控制台时仍返回 true，没法用来判断重定向）。
   * 强改 UTF-8 会让控制台上的中文变乱码，所以默认跟随原生编码；
   * 只在「放得下中文、放不下 emoji」（=GBK 这类）时把 emoji 去掉，避免打印成 '?'。
   * 想要 UTF-8 文件就加 --utf8（此时别在控制台里看，中文会乱码）。
   */
  static Charset OUT_CS = StandardCharsets.UTF_8;
  static boolean STRIP_EMOJI = false;

  static void initOutput(boolean utf8) {
    if (utf8) {
      try {
        System.setOut(new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8));
        System.setErr(new PrintStream(new FileOutputStream(FileDescriptor.err), true, StandardCharsets.UTF_8));
        OUT_CS = StandardCharsets.UTF_8;
        STRIP_EMOJI = false;
        return;
      } catch (Throwable ignored) {
        // 换不了就退回原生编码
      }
    }
    OUT_CS = resolveOutCharset();
    STRIP_EMOJI = !canEncode(OUT_CS, "⚡") && canEncode(OUT_CS, "中");
  }

  static Charset resolveOutCharset() {
    Charset cs = System.out.charset();
    if (cs != null) return cs;
    for (String key : new String[] {"stdout.encoding", "native.encoding"}) {
      String v = System.getProperty(key);
      if (v != null) {
        try {
          return Charset.forName(v);
        } catch (Exception ignored) {
          // 继续试下一个
        }
      }
    }
    return Charset.defaultCharset();
  }

  static boolean canEncode(Charset cs, String s) {
    try {
      return cs.newEncoder().canEncode(s);
    } catch (Exception e) {
      return false;
    }
  }

  /** 输出前处理：当前编码放不下的字符去掉（连同紧跟的一个空格），中文始终保留 */
  static String vis(String s) {
    if (s == null || s.isEmpty() || !STRIP_EMOJI) return s;
    if (canEncode(OUT_CS, s)) return s;
    CharsetEncoder enc = OUT_CS.newEncoder();
    StringBuilder b = new StringBuilder(s.length());
    for (int i = 0; i < s.length(); i++) {
      char ch = s.charAt(i);
      if (enc.canEncode(String.valueOf(ch))) {
        b.append(ch);
      } else if (i + 1 < s.length() && s.charAt(i + 1) == ' ') {
        i++; // emoji 后面那个空格一起去掉
      }
    }
    return b.toString();
  }

  static void p(String s) {
    System.out.println(vis(s));
  }

  static void ps(String s) {
    System.out.print(vis(s));
  }

  static void pe(String s) {
    System.err.println(vis(s));
  }

  public static void main(String[] args) {
    boolean wantUtf8 = false;
    for (String a : args) {
      if ("--utf8".equals(a)) wantUtf8 = true;
    }
    initOutput(wantUtf8);
    Opts opts;
    try {
      opts = parseArgs(args);
    } catch (UsageException e) {
      pe("❌ " + e.getMessage());
      System.err.println();
      System.err.print(vis(USAGE));
      System.exit(2);
      return;
    }
    if (opts.help) {
      System.out.print(vis(USAGE));
      return;
    }
    if (opts.selftest) {
      System.exit(runSelfTest() ? 0 : 1);
      return;
    }

    MAIN_DONE = new CountDownLatch(1);
    int exitCode = 0;
    registerStopHook(opts.timeout);
    try {
      run(opts);
    } catch (UsageException e) {
      pe("❌ " + e.getMessage());
      System.err.println();
      System.err.print(vis(USAGE));
      exitCode = 2;
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      exitCode = 1;
    } catch (Throwable t) {
      t.printStackTrace();
      exitCode = 1;
    } finally {
      MAIN_DONE.countDown();
    }
    System.exit(exitCode);
  }

  static void run(Opts o) throws UsageException, InterruptedException {
    String target = normalizeUrl(o.url == null ? "" : o.url);
    int concurrency = toInt(o.concurrency, "并发数", 1, 1000);
    int duration = toInt(o.duration, "测试时长", 1, 7200);
    int timeout = toInt(o.timeout, "超时时间", 1, 120);
    int stopAfter = o.stopAfter < 0 ? 0 : o.stopAfter;

    List<ProxySpec> proxies = List.of();
    int skipped = 0;
    if (o.proxies != null) {
      ProxyFile pf;
      try {
        pf = loadProxies(o.proxies);
      } catch (IOException e) {
        throw new UsageException("加载代理文件失败: " + e.getMessage());
      }
      proxies = pf.list();
      skipped = pf.skipped();
      if (proxies.isEmpty()) {
        p("⚠️  代理文件里没有可用的代理，将使用本机 IP 发起请求");
      } else {
        String line = "✅ 已加载 " + proxies.size() + " 个可用代理";
        if (skipped > 0) line += "（跳过 " + skipped + " 条无法识别）";
        p(line);
        if (proxies.stream().anyMatch(x -> x.user != null)) installAuthenticator(proxies);
      }
    } else {
      p("ℹ️  未指定代理文件，将使用本机 IP 直接发起请求");
    }

    System.out.println();
    p("🚀 开始压测配置:");
    p("🎯 目标地址: " + target);
    p("🔀 并发数: " + concurrency);
    p("⏰ 测试时长: " + duration + "秒");
    p("⌛ 请求超时: " + timeout + "秒");
    p("💡 提示: 按 Ctrl+C 可随时停止测试");
    System.out.println();

    Stats stats = runTest(target, proxies, concurrency, duration, timeout, stopAfter);
    System.out.println();
    p(stats.report());
    if (o.json) System.out.println(buildJson(stats.snapshot()));
  }

  // ---------------------------------------------------------------------------
  // 内置自测：单元断言 + 本机回环（直连 / http 代理 / CONNECT / SOCKS5）
  // ---------------------------------------------------------------------------

  static final class Check {
    int pass;
    final List<String> fails = new ArrayList<>();

    void ok(String name, boolean cond, String detail) {
      if (cond) pass++;
      else fails.add(name + (detail == null || detail.isEmpty() ? "" : " → " + detail));
    }

    void ok(String name, boolean cond) {
      ok(name, cond, "");
    }

    void eq(String name, Object actual, Object expected) {
      if (Objects.equals(actual, expected)) pass++;
      else fails.add(name + " → 期望「" + expected + "」，实际「" + actual + "」");
    }

    void contains(String name, String haystack, String needle) {
      ok(name, haystack != null && haystack.contains(needle), "缺少「" + abbrev(needle) + "」");
    }
  }

  @FunctionalInterface
  interface SocketHandler {
    void handle(Socket s) throws Exception;
  }

  /**
   * 起一个本机回环小服务：accept 到 ServerSocket 关闭为止、每连接一个线程，
   * 异常打到 stderr（自测排障用——之前异常被吞掉，只会表现为客户端超时）。
   */
  static void serveLoop(ServerSocket ss, String name, SocketHandler handler) {
    Thread acceptor = new Thread(() -> {
      while (!ss.isClosed()) {
        Socket s;
        try {
          s = ss.accept();
        } catch (Throwable e) {
          if (!ss.isClosed()) System.err.println("[selftest:" + name + "] accept 异常: " + e);
          return; // ServerSocket 已关闭
        }
        Thread worker = new Thread(() -> {
          try {
            handler.handle(s);
          } catch (Throwable e) {
            System.err.println("[selftest:" + name + "] " + e);
            e.printStackTrace(System.err);
          } finally {
            closeQuietly(s);
          }
        }, "selftest-" + name);
        worker.setDaemon(true);
        worker.start();
      }
    }, "selftest-accept-" + name);
    acceptor.setDaemon(true);
    acceptor.start();
  }

  static ServerSocket loopback() throws IOException {
    return new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
  }

  static void copy(InputStream in, OutputStream out) throws IOException {
    byte[] buf = new byte[8192];
    int n;
    while ((n = in.read(buf)) != -1) {
      out.write(buf, 0, n);
    }
    out.flush();
  }

  /** 回环 HTTP 服务：读完请求头，回 200，然后由 serveLoop 关连接 */
  static void handlerEcho(Socket s) throws IOException {
    readHead(s.getInputStream());
    s.getOutputStream().write(
        "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            .getBytes(StandardCharsets.UTF_8));
    s.getOutputStream().flush();
  }

  /** 收到什么请求都不回，用来测读超时 */
  static void handlerStall(Socket s) throws Exception {
    Thread.sleep(3000);
  }

  /** 最小 HTTP 代理：要求请求行是绝对 URI，转发给真实目标再回写 */
  static void handlerAbsoluteProxy(AtomicReference<String> reqLine, Socket s) throws IOException {
    String head = readHead(s.getInputStream());
    String line = firstLine(head);
    reqLine.set(line);
    String[] parts = line.split("\\s+");
    if (parts.length < 2) return;
    URI target = URI.create(parts[1]);
    try (Socket up = new Socket()) {
      up.connect(new InetSocketAddress(target.getHost(), target.getPort()), 2000);
      String path = target.getRawPath();
      if (path == null || path.isEmpty()) path = "/";
      if (target.getRawQuery() != null) path += "?" + target.getRawQuery();
      String fwd = "GET " + path + " HTTP/1.1\r\nHost: " + target.getHost()
          + "\r\nConnection: close\r\n\r\n";
      up.getOutputStream().write(fwd.getBytes(StandardCharsets.UTF_8));
      up.getOutputStream().flush();
      copy(up.getInputStream(), s.getOutputStream());
    }
  }

  /** 直接拒绝 CONNECT，用来测隧道握手路径 */
  static void handlerRejectConnect(Socket s) throws IOException {
    readHead(s.getInputStream());
    s.getOutputStream().write("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n".getBytes(StandardCharsets.UTF_8));
    s.getOutputStream().flush();
  }

  /**
   * 最小 SOCKS 服务端（v5/v4、无认证）：验证 JDK 的 SOCKS 客户端链路。
   * versionOut 记录客户端实际用的协议版本（JDK 的 socksProxyVersion 默认值要靠它确认）。
   */
  static void handlerSocks(Socket client, AtomicInteger versionOut) throws Exception {
    InputStream in = client.getInputStream();
    OutputStream out = client.getOutputStream();
    int ver = in.read();
    versionOut.set(ver);

    if (ver == 4) {
      // SOCKS4: CMD(1) PORT(2) IP(4) USERID(NUL结尾)  [SOCKS4a: 域名 + NUL]
      int cmd = in.read();
      byte[] pb = in.readNBytes(2);
      int port = ((pb[0] & 0xff) << 8) | (pb[1] & 0xff);
      byte[] ip = in.readNBytes(4);
      readCString(in); // user id
      String host;
      if (ip[0] == 0 && ip[1] == 0 && ip[2] == 0 && ip[3] != 0) {
        host = readCString(in); // SOCKS4a：域名在 userid 后面
      } else {
        host = (ip[0] & 0xff) + "." + (ip[1] & 0xff) + "." + (ip[2] & 0xff) + "." + (ip[3] & 0xff);
      }
      if (cmd != 1) {
        out.write(new byte[] {0x00, 0x5B, 0, 0, 0, 0, 0, 0});
        out.flush();
        return;
      }
      try (Socket up = new Socket()) {
        up.connect(new InetSocketAddress(host, port), 2000);
        out.write(new byte[] {0x00, 0x5A, pb[0], pb[1], 0, 0, 0, 0}); // granted
        out.flush();
        relay(client, up);
      }
      return;
    }

    if (ver != 5) return;
    int nMethods = in.read();
    if (nMethods < 1) return;
    in.readNBytes(nMethods);
    out.write(new byte[] {0x05, 0x00});
    out.flush();

    int cmdVer = in.read();
    int cmd = in.read();
    in.read(); // rsv：SOCKS5 请求里 RSV 只有 1 字节（VER CMD RSV ATYP ...）
    int atyp = in.read();
    String host;
    if (atyp == 1) {
      byte[] a = in.readNBytes(4);
      host = (a[0] & 0xff) + "." + (a[1] & 0xff) + "." + (a[2] & 0xff) + "." + (a[3] & 0xff);
    } else if (atyp == 3) {
      int len = in.read();
      host = new String(in.readNBytes(len), StandardCharsets.UTF_8);
    } else if (atyp == 4) {
      byte[] a = in.readNBytes(16);
      StringBuilder sb = new StringBuilder();
      for (int i = 0; i < 16; i += 2) {
        if (i > 0) sb.append(':');
        sb.append(String.format("%x", ((a[i] & 0xff) << 8) | (a[i + 1] & 0xff)));
      }
      host = sb.toString();
    } else {
      return;
    }
    byte[] pb = in.readNBytes(2);
    int port = ((pb[0] & 0xff) << 8) | (pb[1] & 0xff);
    if (cmdVer != 5 || cmd != 1) {
      out.write(new byte[] {0x05, 0x07, 0x00, 0x01, 0, 0, 0, 0, 0, 0});
      out.flush();
      return;
    }

    try (Socket up = new Socket()) {
      up.connect(new InetSocketAddress(host, port), 2000);
      out.write(new byte[] {0x05, 0x00, 0x00, 0x01, 127, 0, 0, 1, 0, 0});
      out.flush();
      relay(client, up);
    }
  }

  /** 读到 NUL 为止的字符串（SOCKS4 userid / SOCKS4a hostname） */
  static String readCString(InputStream in) throws IOException {
    ByteArrayOutputStream b = new ByteArrayOutputStream();
    int c;
    while ((c = in.read()) > 0) {
      b.write(c);
    }
    return b.toString(StandardCharsets.UTF_8);
  }

  /** 两个方向各开一个线程搬运字节，直到任一端关闭 */
  static void relay(Socket client, Socket up) throws Exception {
    Thread t = new Thread(() -> {
      try {
        copy(up.getInputStream(), client.getOutputStream());
      } catch (IOException ignored) {
      }
    }, "socks-relay");
    t.setDaemon(true);
    t.start();
    copy(client.getInputStream(), up.getOutputStream());
    t.join(2000);
  }

  static void expectThrows(Check c, String name, ThrowingRunnable r, Class<? extends Throwable> type) {
    try {
      r.run();
      c.ok(name, false, "没有抛 " + type.getSimpleName());
    } catch (Throwable t) {
      c.ok(name, type.isInstance(t), "抛的是 " + t.getClass().getSimpleName() + ": " + t.getMessage());
    }
  }

  @FunctionalInterface
  interface ThrowingRunnable {
    void run() throws Exception;
  }

  static boolean runSelfTest() {
    Check c = new Check();
    List<ServerSocket> opened = new ArrayList<>();
    try {
      // 1) 代理行规整
      ProxySpec p1 = normalizeProxy("18.163.99.118:80");
      c.ok("裸 ip:port", p1 != null && !p1.socks && "18.163.99.118".equals(p1.host) && p1.port == 80 && p1.user == null,
          "实际=" + p1);
      ProxySpec p2 = normalizeProxy("  36.6.145.177:8089 ");
      c.ok("带空白", p2 != null && p2.port == 8089, "实际=" + p2);
      ProxySpec p3 = normalizeProxy("http://1.2.3.4:8080");
      c.ok("已带 http scheme", p3 != null && !p3.socks && "1.2.3.4".equals(p3.host), "实际=" + p3);
      ProxySpec p4 = normalizeProxy("socks5://1.2.3.4:1080");
      c.ok("socks5 识别为 SOCKS", p4 != null && p4.socks && p4.port == 1080, "实际=" + p4);
      ProxySpec p5 = normalizeProxy("socks4://1.2.3.4:1080");
      c.ok("socks4 识别为 SOCKS", p5 != null && p5.socks && p5.version == 4, "实际=" + p5);
      c.eq("注释跳过", normalizeProxy("# 注释"), null);
      c.eq("空行跳过", normalizeProxy(""), null);
      c.eq("未知 scheme 跳过", normalizeProxy("ssh://1.2.3.4:22"), null);
      ProxySpec p6 = normalizeProxy("103.118.124.137:6969:myuser:mypass");
      c.ok("ip:port:用户:密", p6 != null && "myuser".equals(p6.user) && "mypass".equals(p6.pass) && !p6.socks,
          "实际=" + p6);
      ProxySpec p7 = normalizeProxy("myuser2:mypass2@202.1.2.3:8080");
      c.ok("用户:密@ip:port", p7 != null && "myuser2".equals(p7.user) && "mypass2".equals(p7.pass), "实际=" + p7);
      ProxySpec p8 = normalizeProxy("[2001:db8::1]:8080");
      c.ok("IPv6", p8 != null && "2001:db8::1".equals(p8.host) && p8.port == 8080, "实际=" + p8);
      ProxySpec p9 = normalizeProxy("socks5://user:pw@9.9.9.9:1080");
      c.ok("SOCKS 带账密", p9 != null && p9.socks && "user".equals(p9.user) && "pw".equals(p9.pass), "实际=" + p9);

      // 2) 代理文件统计
      Path tmp = Files.createTempFile("proxies_selftest", ".txt");
      try {
        Files.writeString(tmp, "# 注释\n1.2.3.4:80\nsocks5://1.2.3.4:1080\nnot-a-proxy\n\nhttp://5.6.7.8:8080\n",
            StandardCharsets.UTF_8);
        ProxyFile pf = loadProxies(tmp.toString());
        c.eq("代理文件可用条数", pf.list().size(), 3);
        c.eq("代理文件跳过条数", pf.skipped(), 1);
      } finally {
        Files.deleteIfExists(tmp);
      }

      // 3) 目标地址
      c.eq("自动补 scheme", normalizeUrl("127.0.0.1:8080"), "http://127.0.0.1:8080/");
      c.eq("https 保留", normalizeUrl("https://example.com/a?b=1"), "https://example.com/a?b=1");
      c.eq("补根路径", normalizeUrl("https://example.com"), "https://example.com/");
      expectThrows(c, "拒绝 ftp", () -> normalizeUrl("ftp://x"), UsageException.class);
      expectThrows(c, "拒绝空地址", () -> normalizeUrl("  "), UsageException.class);
      expectThrows(c, "拒绝坏地址", () -> normalizeUrl("http://"), UsageException.class);

      // 4) 参数范围
      expectThrows(c, "并发下界", () -> toInt(0, "并发数", 1, 1000), UsageException.class);
      expectThrows(c, "时长上界", () -> toInt(7201, "测试时长", 1, 7200), UsageException.class);
      c.eq("合法参数", toInt(12, "并发数", 1, 1000), 12);

      // 5) 响应行解析
      c.eq("读状态行", readLine(new ByteArrayInputStream("HTTP/1.1 200 OK\r\n".getBytes(StandardCharsets.UTF_8))),
          "HTTP/1.1 200 OK");
      c.eq("解析状态码", parseStatus("HTTP/1.1 404 Not Found"), 404);
      c.eq("非法状态行", parseStatus("garbage"), -1);
      c.eq("空状态行", parseStatus(""), -1);
      String head = readHead(
          new ByteArrayInputStream("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n".getBytes(StandardCharsets.UTF_8)));
      c.eq("读 CONNECT 应答头", parseStatus(firstLine(head)), 403);

      // 6) 失败原因分类
      c.eq("拒绝连接", classify(new ConnectException("Connection refused: connect")), "ECONNREFUSED（连接被拒）");
      c.eq("DNS 失败", classify(new UnknownHostException("no.such.host")), "ENOTFOUND（DNS 解析失败）");
      c.eq("读超时", classify(new SocketTimeoutException("Read timed out")), "TIMEOUT（请求超时）");
      c.eq("连接超时", classify(new SocketTimeoutException("connect timed out")), "ETIMEDOUT（连接超时）");
      c.eq("连接被重置", classify(new IOException("Connection reset")), "ECONNRESET（连接被重置）");
      c.eq("SOCKS 错误", classify(new IOException("SOCKS: connection refused")), "SOCKS_ERROR（SOCKS 代理错误: SOCKS: connection refused）");
      c.eq("SSL 错误", classify(new SSLException("handshake_failure")), "SSL_ERROR（证书/TLS 错误: SSLException）");
      c.eq("兜底原因", classify(new IllegalStateException("boom")), "IllegalStateException（boom）");

      // 7) 统计与报告
      Stats st = new Stats();
      st.addSuccess(200, 16);
      st.addSuccess(200, 24);
      st.addFailed("ECONNREFUSED（连接被拒）");
      Snapshot snap = st.snapshot();
      c.eq("总数", snap.total(), 3);
      c.eq("成功数", snap.success(), 2);
      c.eq("失败数", snap.failed(), 1);
      c.eq("平均响应 20ms", snap.avgMs(), 20.0);
      c.ok("QPS>0", snap.qps() >= 0);
      c.eq("状态码条目数", snap.codes().size(), 1);
      c.eq("状态码计数", snap.codes().get(0).getValue(), Integer.valueOf(2));
      c.eq("失败原因条目数", snap.errors().size(), 1);
      String rep = st.report();
      c.contains("报告含状态码", rep, "状态码分布");
      c.contains("报告含失败原因", rep, "失败原因分布");
      c.contains("报告百分比", rep, "HTTP 200: 2 次 (66.7%)");
      String json = buildJson(snap);
      c.contains("JSON 总数", json, "\"total\":3");
      c.contains("JSON 成功", json, "\"success\":2");
      c.contains("JSON 状态码", json, "[200,2]");
      c.contains("JSON 失败原因", json, "ECONNREFUSED（连接被拒）");

      // 8) 回环：直连 200
      ServerSocket echo = loopback();
      opened.add(echo);
      serveLoop(echo, "echo", HttpPressureTest::handlerEcho);
      String echoUrl = "http://127.0.0.1:" + echo.getLocalPort() + "/";
      Result r1 = request(echoUrl, null, 3000);
      c.ok("回环直连 200", r1.ok && r1.status == 200, "ok=" + r1.ok + " status=" + r1.status + " reason=" + r1.reason);
      c.ok("耗时有值", r1.ms >= 0, "ms=" + r1.ms);

      // 9) 回环：端口没人听 → 连接被拒
      int deadPort;
      try (ServerSocket tmpSs = loopback()) {
        deadPort = tmpSs.getLocalPort();
      }
      Result r2 = request("http://127.0.0.1:" + deadPort + "/", null, 1500);
      c.eq("连接被拒", r2.reason, "ECONNREFUSED（连接被拒）");

      // 10) 回环：服务端不回话 → 读超时
      ServerSocket stall = loopback();
      opened.add(stall);
      serveLoop(stall, "stall", HttpPressureTest::handlerStall);
      Result r3 = request("http://127.0.0.1:" + stall.getLocalPort() + "/", null, 600);
      c.eq("读超时原因", r3.reason, "TIMEOUT（请求超时）");

      // 11) 回环：经 http 代理（绝对 URI 转发）
      AtomicReference<String> proxiedLine = new AtomicReference<>();
      ServerSocket proxy = loopback();
      opened.add(proxy);
      serveLoop(proxy, "proxy", s -> handlerAbsoluteProxy(proxiedLine, s));
      Result r4 = request(echoUrl + "probe?a=1", new ProxySpec(false, "127.0.0.1", proxy.getLocalPort(), null, null),
          3000);
      c.ok("经 HTTP 代理 200", r4.ok && r4.status == 200, "ok=" + r4.ok + " status=" + r4.status + " reason=" + r4.reason);
      c.ok("代理收到绝对 URI",
          proxiedLine.get() != null && proxiedLine.get().startsWith("GET " + echoUrl + "probe?a=1 "),
          "请求行=" + proxiedLine.get());

      // 12) 回环：CONNECT 被代理拒绝（带 Proxy-Authorization）
      ServerSocket crej = loopback();
      opened.add(crej);
      AtomicReference<String> connectLine = new AtomicReference<>();
      serveLoop(crej, "connect", s -> {
        String h = readHead(s.getInputStream());
        connectLine.set(firstLine(h));
        c.contains("CONNECT 带代理认证头", h, "Proxy-Authorization: Basic dTpw");
        s.getOutputStream().write("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n".getBytes(StandardCharsets.UTF_8));
        s.getOutputStream().flush();
      });
      Result r5 = request("https://example.com/",
          new ProxySpec(false, "127.0.0.1", crej.getLocalPort(), "u", "p"), 3000);
      c.eq("CONNECT 被拒原因", r5.reason, "PROXY_CONNECT_403（代理拒绝 CONNECT）");
      c.ok("CONNECT 请求行", connectLine.get() != null && connectLine.get().startsWith("CONNECT example.com:443 "),
          "请求行=" + connectLine.get());

      // 13) 回环：经 SOCKS 代理（v4/v5 都支持，版本跟着 JDK 走）
      ServerSocket socks = loopback();
      opened.add(socks);
      AtomicInteger socksVer = new AtomicInteger(-1);
      serveLoop(socks, "socks", s -> handlerSocks(s, socksVer));
      Result r6 = request(echoUrl + "socks", new ProxySpec(true, "127.0.0.1", socks.getLocalPort(), null, null), 3000);
      p("ℹ️  JDK 实际使用的 SOCKS 协议版本: v" + socksVer.get());
      c.ok("经 SOCKS 代理 200", r6.ok && r6.status == 200,
          "ok=" + r6.ok + " status=" + r6.status + " reason=" + r6.reason);

      // 14) 回环：SOCKS4（JDK 只会说 v5，v4 是本文件手写的握手）
      ServerSocket s4 = loopback();
      opened.add(s4);
      AtomicInteger s4Ver = new AtomicInteger(-1);
      serveLoop(s4, "socks4", s -> handlerSocks(s, s4Ver));
      Result r7 = request(echoUrl + "s4", new ProxySpec(true, 4, "127.0.0.1", s4.getLocalPort(), "u4", null), 3000);
      c.ok("SOCKS4 握手被服务端识别", s4Ver.get() == 4, "服务端读到版本=" + s4Ver.get());
      c.ok("经 SOCKS4 代理 200", r7.ok && r7.status == 200,
          "ok=" + r7.ok + " status=" + r7.status + " reason=" + r7.reason);
    } catch (Throwable t) {
      c.fails.add("自测执行异常: " + t);
    } finally {
      for (ServerSocket ss : opened) closeQuietly(ss);
    }

    if (c.fails.isEmpty()) {
      p("✅ 内置自测通过：" + c.pass + " 项断言");
      return true;
    }
    p("❌ 自测失败：" + (c.pass) + " 项通过 / " + c.fails.size() + " 项失败");
    for (String f : c.fails) p("   - " + f);
    return false;
  }

  static void closeQuietly(AutoCloseable c) {
    try {
      if (c != null) c.close();
    } catch (Exception ignored) {
    }
  }
}
