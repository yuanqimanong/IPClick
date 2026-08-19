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

### 🔄 阶段 2：TaskService 异步化（下一步）
- [ ] `Send` / `SendStream` / `SendBatch` / `Ping` → `async def`
- [ ] 按 `supports_async` 分派：真异步 or executor 回退
- [ ] 保留同步类供 async_mode=off 使用（两套并存，别删同步路径）

### ⬜ 阶段 3：grpc.aio 服务端
- [ ] `[SERVER].async_mode`（默认 `false`）
- [ ] `grpc.aio.server()` 分支，与多进程（`processes`）叠加
- [ ] 实测对比：同步 vs 异步 × 单进程 vs 多进程

### ⬜ 阶段 4：limiter / forwarder / Web 桥接
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
