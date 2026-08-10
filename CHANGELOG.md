# 更新日志

本文件记录 IPClick 的重要变更。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

P1–P6 六个阶段的开发成果。P1 让存量配置真正生效，P2 补安全与可运维，
P3 扩展传输能力，P4 做集群，P5 补齐适配器，P6 加限流与可插拔浏览器引擎。

### 新增

- **令牌鉴权**（P2-1）：gRPC 拦截器校验 `authorization: Bearer <token>`，
  常量时间比较，支持多令牌轮换与环境变量注入。健康检查端点免鉴权。
- **标准健康检查**（P2-2）：实现 `grpc.health.v1`，K8s 探针与服务网格开箱即用。
  优雅下线时先置 `NOT_SERVING` 再停服务，让流量有机会先摘走。
- **Prometheus 指标**（P2-3）：请求量 / 延迟 / 重试 / 拒绝等。`prometheus-client`
  为可选依赖，未安装时所有埋点降级为无操作。标签基数受测试约束，
  不允许出现 URL、主机名这类无界标签。
- **真流式下载**（P3-1）：新增 server-streaming RPC `SendStream`，
  服务端与客户端都不再把整个响应体驻留内存。
- **批量请求**（P3-2）：新增 bidi-streaming RPC `SendBatch`，结果按完成顺序返回。
- **异步客户端**（P3-3）：`ipclick.aio.AsyncDownloader`，基于 `grpc.aio`，
  接口与同步版对应。
- **集群客户端**（P4）：`ipclick.cluster.ClusterDownloader`，多节点负载均衡
  （轮询 / 随机 / 加权）、基于 `grpc.health.v1` 的健康探测、请求级故障转移。
  摘除与恢复都用连续计数阈值，避免一次抖动就让流量反复横跳。
- **只读集群状态页**（P4）：`StatusPageServer` 提供 HTML 页面与 `/api/nodes`
  JSON，仅标准库实现。刻意做成只读，且默认只监听 `127.0.0.1`。
- **`requests` 适配器**（P5-1）：可选依赖 `ipclick[requests]`。
- **`playwright` 浏览器渲染适配器**（P5-2）：可选依赖 `ipclick[browser]`。
  起真实浏览器执行 JS 并返回渲染后的 DOM，支持等待选择器、滚动懒加载、整页截图、
  资源类型拦截。`[BROWSER]` 配置节至此第一次真正有消费方。

### 变更

- **`[BROWSER]` 配置节重写**。原来那一节写的插件目录、`.crx`/`.dll` 列表、
  缓存上限 MB 数、`sandbox.level = strict/moderate/off` 没有任何一项有消费方，
  也没有任何一项能落到 playwright 上，全部删除。现在只保留能真正生效的键。
- **参数校验错误不再被重试装饰器吞成 `-1` 响应**。`ValidationError` 现在直接
  上抛：重试多少次都是同样的结果，默认配置下还要先睡满 1+2+4 秒；
  伪装成网络失败也会误导调用方去查网络。`TaskService` 那边本来就会把它映射成
  `INVALID_ARGUMENT`，只是此前根本没机会看到。
  受影响的行为：适配器收到不支持的 HTTP 方法时由"返回 `status_code == -1`
  的响应"改为"抛 `ValidationError`"。
- **服务端错误按类型翻译回客户端**，不再一律吞成 `status_code == -1`：
  - `INVALID_ARGUMENT` → `ValidationError`（参数写错了，改调用代码）
  - `FAILED_PRECONDITION` → `AdapterError`（这个服务端做不到，改部署）

  此前两者都变成 `TransportError`，再被 `request()` 吞成 `-1` 响应，错误信息写着
  "gRPC 调用失败"——调用方会去排查网络。客户端**本地**发现的参数错误早就是直接
  抛出的，服务端发现的没理由不一致：同一个错误不该因为"谁先发现"而表现不同。
  真正的传输失败（连不上、超时）仍然返回 `-1` 响应，行为不变。
- 服务端相应地把 `AdapterError` 从 `INVALID_ARGUMENT` 拆到 `FAILED_PRECONDITION`。
  "适配器不存在 / 依赖没装 / 浏览器渲染被关掉"不是调用方参数写错，
  报成 `INVALID_ARGUMENT` 会让人去改自己的参数，而实际要改的是服务端部署。
- `get_adapter()` 新增可选参数 `browser_settings`，供浏览器适配器读取 `[BROWSER]`。
- 缺可选依赖时，`get_adapter()` 的报错从笼统的"尚未支持"改为给出安装命令——
  "没装"和"没实现"的处理方式完全不同。
- CI 增加 `playwright install --with-deps chromium`，让浏览器渲染用例在 CI 上
  真的跑起来，而不是静默 skip。

### 新增（P7）

- **TLS / mTLS**（`[SECURITY.tls]`）：链路加密与双向证书认证。四个建连点全覆盖
  ——同步客户端、异步客户端、服务端绑定、集群健康探活。默认关闭以兼容旧部署，
  监听非回环地址却没开时会打显著告警。
  `require_client_cert` 却不配 `ca_file` 是硬错误，不静默降级。
- **断点续传**（`ipclick.resume`）：`download_to_file()` / `iter_resumable()`，
  中断后用 HTTP Range 接着下。带 `If-Range` 校验，资源变了就丢掉重来而不是把两个
  版本拼在一起；服务端不支持 Range 时退化成整体重下。

### 新增（P6）

- **按 host 的并发与 QPS 限制**（`[DOWNLOADER.concurrency]` /
  `[DOWNLOADER.rate_limit]`）。并发用信号量（硬上限），速率用令牌桶（允许突发），
  都按 host 独立计数，默认关闭。对 `Send` / `SendStream` / `SendBatch` 都生效；
  流式请求的额度持有到整条流结束。空闲 host 条目会回收——爬虫会碰到无穷多域名，
  只增不减就是一条稳定的内存泄漏。
  超时抛 `HostLimitTimeout`（gRPC `RESOURCE_EXHAUSTED`），不伪装成网络故障。
  ⚠️ 服务端是一请求一线程，排队会占 worker 线程，README 已写明取舍。
- **浏览器引擎可插拔**，新增三个引擎，共四个：
  - `camoufox`（Firefox，自带完整指纹伪装）
  - `patchright`（Chromium，Playwright 的反检测分支）
  - `playwright`（原版）
  - `DrissionPage`（CDP 直连本机 Chrome）

  前三个都产出 `playwright.async_api.Browser`，共用同一套线程模型、上下文隔离与
  资源拦截；DrissionPage 是另一套 API，单独实现但对外契约一致。
- **`[BROWSER].engine` 与平台默认**。`auto`（默认）在 Windows 上选 DrissionPage、
  在 Linux/macOS 上选 Camoufox。新增 protobuf 枚举 `CAMOUFOX` / `PATCHRIGHT` /
  `BROWSER`，其中 `BROWSER` 表示"渲染就行，引擎由服务端定"。

### 修复（P6）

- **camoufox 下不再强行覆盖 viewport / User-Agent**。它自己生成一整套自洽指纹，
  再盖一层只会自相矛盾，反而比不伪装更容易被识别。
- **DrissionPage 的按请求代理改为报错而非静默忽略**。它的代理是浏览器进程级的，
  启动后改不了；默默忽略等于让请求从错误的出口 IP 发出去。

### 修复

- **`playwright` 启动级代理占位值会让所有直连请求失败**。playwright 文档里
  `proxy={"server": "per-context"}` 的写法是给旧版 chromium 的；现在每个 context
  单独设代理已能直接生效，而设了那个占位值之后，没配代理的 context 会去连一个
  叫 `per-context` 的代理，于是全部 `ERR_PROXY_CONNECTION_FAILED`。
  （开发期发现，未随任何版本发布。）

## [0.2.3] - 2026-08-10

**首个发布到 PyPI 的 0.2.x 版本。** 0.2.0 / 0.2.1 / 0.2.2 均未上传 PyPI，
其标签与 GitHub Release 已删除，全部内容并入本版。

### 新增

- 自动发布流水线 `.github/workflows/release.yml`：推送 `v*` 标签即触发
  lint / 类型检查 / 测试 / 构建 / 产物校验，通过后停在 `pypi` 环境等待人工审批，
  批准后上传 PyPI 并自动创建 GitHub Release。
- 采用 PyPI **Trusted Publishing（OIDC）**，仓库中不存放任何 API token。
- 发布前自动校验：标签与 `pyproject.toml` 版本号必须一致；wheel 必须包含
  `py.typed` 与默认配置；不得混入 tests/`__pycache__`；README 中不得出现
  相对链接（会成为 PyPI 项目页正文，且上传后不可修改）。

### 包含 0.2.0 – 0.2.2 的全部内容

以下条目原属未发布到 PyPI 的 0.2.0 / 0.2.1 / 0.2.2，一并随本版发布。

## [0.2.2] - 2026-08-07（未发布到 PyPI）

本版修复的三项都会被永久固化进 PyPI 页面或分发包，因此在上传前先行修正。

### 修复

- **`stream=True` 在默认适配器上静默丢弃整个响应体**。curl_cffi 在
  `stream=True` 时返回未消费的流式响应，随后读取 `.content` 得到 `b''`，
  而 `status_code` 仍是 200、`exception` 仍是 `None` —— 调用方完全无从察觉。
  服务端本就要把响应体整个塞进一条 protobuf 消息，没有真正的流式通路，
  故与 httpx 适配器保持一致：忽略该参数（README「尚未实现」已如此声明）。
  该问题自 0.1.3 起就存在。
- **`DownloadResponse.raise_for_status()` 抛的异常类型与文档不符**。实际抛基类
  `IPClickError`，而 README 文档写的是 `RequestError`；后者是前者的**子类**，
  于是按文档写 `except RequestError:` 的代码根本捕获不到。改为抛 `RequestError`。
- **随包分发的默认配置预置了作者本机的代理地址**（`127.0.0.1:7890`，Clash 默认端口）。
  这会让所有用户的 `proxy=True` 指向他们自己机器的该端口 —— 可能是完全不相干的服务。
  改为留空，此时 `proxy=True` 会打警告并直连。
- README 中 `LICENSE` 的相对链接改为绝对地址。README 会作为 `long_description`
  打进 wheel 的 METADATA，即 PyPI 项目页正文；PyPI 独立渲染该页面，相对链接会 404，
  且版本一经上传不可修改。

## [0.2.1] - 2026-08-07（未发布到 PyPI）

### 修复

- **CI 类型检查失败**：`basedpyright` 在只有 warning、没有 error 时退出码同样是 1，
  所以 0.2.0 的 CI 在 74 个 warning 上失败。现已把 warning 清零：
  - 补齐 `@override` 标注、类属性类型标注、缺失的参数类型标注；
  - 移除已失效的 `# pyright: ignore` 注释；
  - 可选依赖改用 `Any` 声明 + 私有别名导入，不再需要逐处忽略；
  - 在 `[tool.pyright]` 中显式关闭那些仅因第三方库缺类型标注而必然触发的规则
    （`reportUnknown*`），并说明理由。
- **CI protobuf 一致性检查失败**：ruff 的 `extend-exclude` 用了
  `src/ipclick/dto/proto/task_pb2*.py`，该 glob 要求文件名以 `.py` 结尾，
  匹配不到 `task_pb2.pyi`。于是 `ruff format` 会改写生成的 stub，导致每次重新
  生成都产生差异。现已同时排除 `*.pyi`。
- `ipclick config-info` 不再打印 `[DOWNLOADER]` 的 `connect_timeout` /
  `download_timeout` —— 这两项当前没有消费方，展示出来会让人误以为改了就生效。
  改为展示真正生效的 `[LOG]` 配置；集群节点数标注为"尚未实现"。

### 文档

- README 新增「尚未实现 / 已知限制」章节，逐条列出当前**不生效**的配置节
  （`[GENERAL]` `[CLUSTER]` `[DOWNLOADER]` `[BROWSER]` `[MONITOR]`）与未实现的功能
  （集群/负载均衡、服务端鉴权、4 个未实现的适配器、浏览器渲染、流式下载、
  文件上传、批量请求、异步客户端、Cookie 持久化、客户端重试、可观测性）。
- `default_config.toml` 在上述配置节上加了「⚠️ 尚未实现」标注。
- 新增本 CHANGELOG。

## [0.2.0] - 2026-08-07（未发布到 PyPI）

### 安全

- **修复 SSL 证书校验默认关闭**：SDK 的 `verify` 默认值是 `None`，protobuf 视其为
  "未设置"，服务端因 proto3 隐式默认值收到 `false`。给 `verify_ssl` /
  `timeout_seconds` / `allow_redirects` / `max_retries` / `retry_backoff_seconds` /
  `stream` 加上 `optional` 显式存在性，服务端改用 `HasField` 区分"未设置"与
  "显式设为 0/false"。
- 新增服务端目标 URL 准入策略（SSRF 防护）：协议白名单、云元数据地址拦截、
  可选内网拦截，通过 `[SECURITY]` 配置。
- 响应不再回传 `original_request`（其中含代理账号密码）。
- 启动日志不再 dump 整份配置（含代理密码、`db_uri`）。
- 适配器默认不再信任环境变量中的代理。libcurl 会自行读取 `http_proxy`，
  `Session(trust_env=False)` 拦不住，须显式传空字符串；httpx 默认
  `trust_env=True` 同样会捡起 `ALL_PROXY`。

### 修复

- `server.py` 用 `port` 计算 `max_workers`：`ipclick run --port 9527` 会创建
  9527 个线程的线程池。
- `Downloader.delete()` 不转发 `url`，调用必抛 `TypeError`。
- httpx 适配器：传了 httpx 0.28 已移除的 `proxies=`（走代理必崩）；把 `kwargs`
  JSON 字符串当 dict 索引；`json` / `files` / `allow_redirects` 被静默丢弃；
  回退到不存在的 `self.user_agent`。
- curl_cffi 适配器对空 `kwargs` 无条件 `json.loads` 导致 `JSONDecodeError`。
- 适配器缓存只读不写，每个请求都新建适配器和连接，`cleanup()` 关不掉任何东西。
- `cleanup()` 清空全局 `ADAPTER_CLASSES`，清理一次后进程内再也造不出适配器。
- 优雅停机是假的：`server.stop()` 返回 Event 但没 `wait` 就 `sys.exit`。
- 重试：`max_retries=0` / `retry_delay=0` 被 falsy 判断当作未传；退避最长 600 秒
  占住 worker 线程；重试日志挂在适配器并不存在的 `logger` 属性上，全程静默。
- `request()` 吞异常后返回 `None`，与 `-> DownloadResponse` 的签名不符。
- `DownloadResponse.from_response()` 漏传必填字段，调用必崩。
- `adapter_type` 返回 protobuf 枚举整数而非适配器名称。
- `config-info` 读取小写配置键，永远取不到值。
- `SecureUtil.md5` 在循环中误用 `isinstance(data, ...)` 而非 `isinstance(_d, ...)`。

### 新增

- 公共 API 导出 `HttpMethod` / `DownloadTask` / `DownloadResponse` / `Response`
  及异常类型（README 首个示例原本因 `HttpMethod` 未导出而 `ImportError`）。
- 新增 `exceptions.py` 异常层次，取代到处 `raise Exception`。
- 新增 `py.typed`。
- `Downloader` 支持 `close()` 与上下文管理器；复用 gRPC channel。
- 适配器改用 Session/Client 连接池；服务端补 `maximum_concurrent_rpcs`。
- `register_adapter()` 支持注册自定义适配器。
- `[LOG]` 配置节现在真正生效。
- 新增 proto 生成脚本，固化此前手工修改生成代码导入语句的步骤。

### 变更

- 适配器名/枚举值无法识别时报错，不再静默退回 `curl_cffi`。
- `log_util` 不再在 import 时清空宿主应用的 loguru handler。
- 依赖升级：curl-cffi 0.14→0.16、grpcio 1.76→1.83、protobuf 6→7、click 8.3→8.4、
  python-box 7.3→7.4、uuid-utils 0.14→0.17、ruff 0.14→0.16。移除未使用的
  pydantic，补上漏声明的 typing-extensions。

### 工程

- 启用 ruff 规则集（原本 `select` 全被注释，只能查出 4 个问题）。
- basedpyright 类型错误 52 → 0。
- 从零补齐测试套件，覆盖率 82%，含真实 gRPC 端到端与本地 HTTP 服务测试。
- 新增 GitHub Actions CI：3 个 Python 版本的 lint/format/类型/测试、wheel 内容
  校验、Docker 构建与冒烟。
- Dockerfile 改为多阶段构建、非 root 运行、带 healthcheck；`.dockerignore` 移到
  仓库根目录（放在 `docker/` 下 Docker 根本不会读取）。

## [0.1.3] 及更早

见 git 提交历史。
