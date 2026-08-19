# 0.7.0 开发进度

> 这份文件跟着 `feat/0.7.0-async` 分支走，随时可以中断、按它接着做。
> 发版前删掉。

分支：`feat/0.7.0-async`　基点：`a9e5174`（master，0.6.0 已发布）

---

## 已定的设计决策

| # | 决策 | 理由 |
|---|---|---|
| 异步是**加法**不是替换 | 基类新增可选的 `adownload()`，`supports_async = False` 默认走线程池回退 | README 声明支持自定义适配器，它们只实现同步 `download()`。改基类签名会静默打断所有第三方适配器，且报错信息（`object Response can't be used in 'await' expression`）和真因毫无字面联系 |
| `async_mode` **默认关** | 0.7 实验性，0.8 再翻默认值 | ① 第三方适配器在 executor 里跑，线程安全假设变了；② Web 端「试一试」是同步调用，跨线程投递的错误/取消/超时要重写；③ 0.6.0 刚换过一次并发模型（多进程），连换两次出问题分不清是谁 |
| 分布式限流：`forward=on` 已精确 | 所有任务经入口节点，本来就是全局的 | 缺口只在 `forward=off`（客户端分发），那时是 N × per_host_qps |
| `forward=off` 用**租约协调者** | 按 id 选主，走已有 gRPC 通道批量申请额度；协调者不可达时降级为静态均分 | 不引 Redis。降级方向安全（宁可少发不可超发） |
| 浏览器并发按**内存**不按 CPU | CPU 核数只做上限 | 浏览器页面是内存瓶颈：camoufox 单 context 开销远高于 chromium，8 核开 8 个页面会换页。不同引擎系数不同 |

---

## 阶段进度

### ✅ 阶段 1a：适配器异步接口（基类）
`src/ipclick/adapters/base.py`
- 新增 `supports_async: bool = False`
- 新增 `async adownload()` —— 默认把同步 `download()` 丢 executor
- 新增 `async adownload_stream()` —— 逐个搬分片，不 list() 化（否则丢掉流式的意义）

### ✅ 阶段 1b：curl_cffi / niquests 的真异步实现
- [x] `CurlCffiAdapter`：`AsyncSession`，`supports_async = True`
      - AsyncSession 按 **(事件循环, proxy, verify, impersonate)** 缓存 —— 必须带循环，
        AsyncCurl 绑定在创建它的循环上，跨循环复用会挂 "attached to a different loop"
      - 抽出 `_build_request_kwargs()` 给同步/异步共用（各写一份会失步，
        症状是"同一个请求同步能过异步过不了"，从表象看不出是参数差异）
      - **实测：异步 1351 QPS vs 同步 50 线程 500 QPS ＝ 2.7×**（单进程，600 次请求）
- [x] `NiquestsAdapter`：`AsyncSession`，`supports_async = True`
- [x] `aretry()` 异步重试装饰器 —— 用 `asyncio.sleep`。同步那个里的 `time.sleep`
      在协程里会**阻塞整个事件循环**：不是拖慢这一个请求，是让同循环上所有在飞的
      请求一起停住，默认退避 1+2+4 秒一次重试冻结 worker 七秒，而现象是
      "毫不相干的请求也集体变慢"
- [x] 测试（`tests/test_async_adapters.py`，9 个）
      - 重点守两条：① 只实现同步的适配器 await 照样能用且**真并行**（不是退化成串行）
        ② 流式回退**逐个搬**而不是先 list()（否则丢掉流式的意义，但接口看不出差别）

**阶段 1 实测**（单进程 600 次请求，目标 go-httpbin）：

| 适配器 | 同步 50 线程 | 异步 | 提升 |
|---|---:|---:|---:|
| curl_cffi | 524 QPS | **1361 QPS** | **2.6×** |
| niquests | 660 QPS | **913 QPS** | 1.4× |

### ✅ 阶段 2：TaskService 异步化
`src/ipclick/services/async_task_service.py`（新文件）
- [x] `AsyncTaskService(TaskService)` —— **只覆写四个 RPC 入口**，其余全部继承
- [x] 先把异常→gRPC 状态码的映射抽成 `_response_for_exception()` 给两条路共用。
      抽出来不是为省行数：那里每条分支携带**排障方向**（PERMISSION_DENIED 指向
      SSRF 策略、RESOURCE_EXHAUSTED 指向限流、FAILED_PRECONDITION 指向服务端部署），
      两份拷贝失步的表现是"同一个错误在异步模式下把人指向另一个方向"
- [x] `SendBatch` 用 `asyncio.as_completed`（批量本就是协程最划算的场景）
- [x] `_alimited()` 限流包装 —— 未开限流时零开销；**开了的话仍占线程**，
      真正的异步令牌桶留到阶段 4，注释里写明了

### ✅ 阶段 3：grpc.aio 服务端
`src/ipclick/async_server.py`（新文件）+ `[SERVER].async_mode`（默认 `false`）
- [x] `_AsyncAuthInterceptor` 复用同步拦截器的判定逻辑（鉴权规则两条路必须一致）
- [x] 健康检查用 **aio 版** HealthServicer —— 同步版的 `set()` 不是协程，
      `await` 它会把服务端带崩，而症状是"客户端连不上"，极易误判成端口/防火墙
- [x] 与 `[CLUSTER].forward = "on"` **显式互斥**（转发器出站还是同步 stub，
      在事件循环里会把循环阻塞住）—— 直接报错而不是静默降级
- [x] 异步模式下 Web 端暂不启动，启动时打 warning 说明原因
- [x] TLS 正面测试 `TestAsyncServerTls` —— 这是被
      `test_no_plaintext_fallback_anywhere` 护栏逼出来的：新的连接点想进白名单，
      得先有证据证明"开了 TLS 就真走 TLS"，只加白名单等于拆护栏

**阶段 2+3 端到端实测**（⚠️ 本机已从 16 核降到 4 核，客户端只给 2 核且已接近饱和，
只有**比值**有意义）：

| 并发 | sync | async | 提升 |
|---|---:|---:|---:|
| 50 | 58.5 QPS | **108.6** | 1.86× |
| 200 | 119.6 QPS | **208.9** | 1.75× |

两种模式都 100% 成功。

### ⬜ 阶段 3：grpc.aio 服务端
- [ ] `[SERVER].async_mode`（默认 `false`）
- [ ] `grpc.aio.server()` 分支，与多进程（`processes`）叠加
- [ ] 实测对比：同步 vs 异步 × 单进程 vs 多进程

### 🔄 阶段 4（下一步）：limiter / forwarder / Web 桥接
- [ ] limiter：`asyncio.Semaphore` + **带 FIFO 等待队列的异步令牌桶**
      （现在是 `time.sleep` 轮询，1000 协程一起醒会惊群、实际 QPS 超限）
- [ ] forwarder：出站 gRPC 换 aio channel
- [ ] Web 端：`pages.py:364` 的同步 `task_service.Send()` 跨线程投递到事件循环

### ⬜ 阶段 5：浏览器并发上限
- [ ] `max_pages` 默认值按 `min(cpu_count, 可用内存 / 每引擎经验值)` 推导
- [ ] 各引擎不同系数（camoufox ≫ chromium）

### ⬜ 阶段 6：分布式精准限流
- [ ] 异步令牌桶（见阶段 4）
- [ ] `forward=off` 下的租约协调者：选主 + 批量租约 RPC + 降级静态均分
- [ ] 文档说明两种集群形态的限流语义差异

### ⬜ 阶段 7：Web 端对齐
- [ ] 0.6.0 新增的 4 项进可编辑列表：`processes` / `max_concurrent_rpcs` /
      `max_concurrent_streams` / `compression`
- [ ] 0.7.0 的 `async_mode`
- [ ] 多进程下链路记录只看得到 0 号进程 —— 页面上要说明，否则像是丢数据

---

## 待你决策 / 遗留

1. **DNS 那件事**（0.6.0 压测挖出来的，未改）
   - 默认配置下每个打域名的请求做一次同步 DNS：3.76ms（纯 IP 是 0.010ms），解析失败 5 秒
   - curl 随后又解析一遍 —— 每请求两次 DNS
   - ⚠️ 两次独立解析构成 **DNS rebinding TOCTOU**：校验看到的 IP 与 curl 实际连的可以不同
   - 改法有取舍（加缓存削弱防护 / 把解析结果传给 curl 要动适配器接口），需单独决策

2. **trace 测试里那批整齐的 0.51s**（约 10 个用例）
   - `close()` 已有哨兵机制，0.5s 另有来源，未深挖（收益约 5s）

---

## 已完成并合入 master 的（0.6.0 及后续）

- `#4` 0.6.0：多进程 / RPC 准入解耦 / UA 池化 / 日志降级
- `#5` CI 超时（chromium 那步卡死 25 分钟）
- `#6` LogUtil 热路径 2.4×，修日志行号错位
- `#7` CI 只测 3.14 + 可选依赖跳过保护 + 测试提速（223s → 134s）
