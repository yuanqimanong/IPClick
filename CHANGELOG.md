# 更新日志

本文件记录 IPClick 的重要变更。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.2] - 2026-08-07

首个发布到 PyPI 的 0.2.x 版本（0.2.0 / 0.2.1 仅有 GitHub Release）。
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

## [0.2.1] - 2026-08-07

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

## [0.2.0] - 2026-08-07

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
