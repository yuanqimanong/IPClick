---
name: ipclick
description: 通过 IPClick 服务端发 HTTP 请求（带浏览器指纹伪装、可选真实浏览器渲染、代理与集群转发），并查它的链路记录、节点状态与可选组件。当需要抓取网页、调用有反爬/风控的接口、复现某次请求为什么失败，或者对话里出现 ipclick / ipclick.toml / IPCLICK_AUTH_TOKEN 时使用。不要用它做普通的本机 HTTP 调用——那用 curl 就够了。
---

# IPClick

IPClick 是一个 gRPC 服务端，代你发 HTTP 请求。相比直接 curl，它多的是：
浏览器 TLS/JA3 指纹伪装（curl_cffi）、可选的真实浏览器渲染（Camoufox / Patchright /
Playwright / DrissionPage）、统一的代理与重试策略、按 host 限流、SSRF 准入，
以及一份可查的链路记录。集群模式下还能把请求分发到别的出口 IP。

版本 {{VERSION}}。所有命令都是 `ipclick <资源> <动作>`，全部支持 `--json`。

## 先做这一步

任何操作之前先确认服务端在、以及这台机器能干什么：

```bash
ipclick status --json
```

关键字段：

- `server.healthy` — false 就别往下走了，服务端没起来（`ipclick run` 起它）。
- `adapters.ready` — **只能用这里列出来的适配器**。指定一个没装的会拿到
  `FAILED_PRECONDITION`，退出码 5。
- `security.auth_token_configured` — false 表示没开鉴权，任何人都能调用。
- `trace.sqlite_enabled` — false 时 `ipclick trace` 无数据可查。
- `cluster.nodes` — 有节点才谈得上转发。

## 输出契约

加 `--json` 时：**stdout 上有且只有一个 JSON 文档**，成功失败都是。日志和进度走
stderr。所以 `ipclick ... --json | jq` 永远安全。

每个文档都有 `ok`（布尔）和 `exit_code`（数字，和进程退出码一致）。拿不到进程退出码
时读 JSON 里的这两个字段即可，结论完全相同。

退出码告诉你该往哪儿查：

| 码 | 含义 | 下一步 |
|----|------|--------|
| 0 | 成功 | — |
| 1 | 拿到响应但不理想（HTTP >= 400、探测不通、装包失败） | 查目标本身 |
| 2 | 命令行参数写错了 | 看 `--help` |
| 3 | 连不上 IPClick **服务端**（请求根本没发出去） | 查进程 / 地址端口 / 防火墙 |
| 4 | 鉴权失败 | 查令牌（`IPCLICK_AUTH_TOKEN`） |
| 5 | 参数被服务端拒绝，或本地配置不合法 | 改调用参数或 ipclick.toml |

## 发请求

```bash
# 最简
ipclick fetch https://example.com --json

# 带方法、头、体
ipclick fetch https://api.example.com/v1/items \
  -X POST -H 'Content-Type: application/json' \
  --json-body '{"name":"x"}' --json

# 指定适配器（先在 status 的 adapters.ready 里确认它可用）
ipclick fetch https://example.com -a browser --json     # 真实浏览器渲染
ipclick fetch https://example.com -a niquests --json    # HTTP/2、HTTP/3

# 走代理
ipclick fetch https://example.com --proxy http://user:pass@host:8080 --json
ipclick fetch https://example.com --proxy config --json   # 用 ipclick.toml 里的 [PROXY]
```

响应体的处理规则，**这条最容易踩**：

- 加 `--json` 时，`body` 字段默认截断到 64 KiB，并给出 `body_truncated: true` 和
  `body_note`。别把截断的 HTML 当完整页面用。
- 要完整内容：`-o page.html`（写文件，不截断），或 `--max-body 0`。
- 不加 `--json` 时，响应体原样进 stdout、元信息进 stderr，所以
  `ipclick fetch URL > page.html` 拿到的是干净的页面。
- 响应不是合法 UTF-8（图片、gzip）时 `body_encoding` 是 `base64`。小的会完整给出
  （能直接 decode 还原），超过上限的**给空串**——半截 base64 解不出任何东西，所以
  不发；`body_note` 会告诉你去用 `-o` 存文件。

其他常用选项：`--timeout`、`--retries`、`--impersonate chrome124`、`--no-verify`、
`--no-redirects`、`--ignore-status`（4xx/5xx 也退出 0）、`-d @文件`（请求体从文件读，
`@-` 从 stdin 读）。

`status: -1` 表示没拿到 HTTP 响应。这时看 `reached_server`：

- `false` → 没连上 IPClick 服务端本身，退出码 3。
- `true` → IPClick 正常，是它连不上**目标站点**（DNS、超时、对方拒绝），退出码 1，
  具体原因在 `error` 里。

## 查发生了什么

```bash
ipclick trace list -n 20 --json               # 最近 20 条
ipclick trace list --status error -k example  # 只看失败的、URL 含 example
ipclick trace list --since 2                  # 最近 2 小时
ipclick trace stats --days 7 --json           # 成功率、耗时、按天趋势、站点排行
```

只能查**落盘**的记录（`[TRACE].sqlite_enabled = true`）。没开的话内存里那份只有
服务端进程自己看得到，Web 端的「请求流」页能看，CLI 看不到——命令会明确这么告诉你。

## 集群

```bash
ipclick node list --json
ipclick node probe --json                    # 探全部
ipclick node probe node-b --json             # 探一个
ipclick node probe --address 10.0.0.7:9528   # 探一个还没写进配置的地址
```

`probe` 分开报告 `reachable`（连得上吗）和 `authenticated`（令牌配对吗）——这两件事
的排查方向完全相反，别混着看。

要组集群时，让人去 Web 管理端的 **配置 → 集群设置**：加节点、开转发，然后为每台
子节点生成 `ipclick.toml` + `.env` + 启动命令（可打包下载）。CLI 这边只读不写。

## 组件

```bash
ipclick component list --json
ipclick component install camoufox --json        # 装 Python 包
ipclick component browser camoufox --json        # 下浏览器本体（约 1 GB，会很慢）
ipclick component install camoufox --dry-run     # 只看会执行什么命令
```

包名走白名单常量，只认这五个：`niquests` `camoufox` `patchright` `playwright`
`drissionpage`。**装包和下浏览器本体是两件事**，只做前者的话第一次请求会当场去下
1 GB 然后超时。`component list` 里 `ready: false` 但 `package: true` 就是这个状态。

**装完要重启服务端。** 从 CLI 装是另一个进程，磁盘上有了，但正在跑的服务端仍按启动时
那份适配器注册表工作。症状很迷惑：`status` 说它就绪（探的是磁盘），而 `fetch -a 它`
收到"需要额外依赖"。`component install` 的输出里 `restart_required: true` 就是在说这件
事——别去重装那个已经装好的包，去重启 `ipclick run`。

卸载只卸 Python 包，不删浏览器本体（那可能是 1 GB，删除不可逆）。

CLI 只管**本机**。要在集群的某台子节点上装，用 Web 管理端的组件页——那里能点名机器
（前提是那台自己开了 `[CLUSTER].allow_remote_install`）。

## 配置

```bash
ipclick config show --json                 # 全部生效配置，机密已脱敏
ipclick config show -s DOWNLOADER --json   # 只看一节
ipclick config get SERVER.port             # 取一项
ipclick config-info                        # 给人看的一屏摘要
```

机密（token / secret / password / auth_key）一律显示为 `<已配置>`，不会回显真值。
需要改配置就直接编辑 `ipclick.toml`，或用 Web 端的「配置」页——CLI 刻意不提供写入口。

## 起服务

```bash
ipclick init                       # 生成 ipclick.toml + .env（600 权限，预填随机 Web 密码）
ipclick run                        # 起 gRPC 服务端（默认 9528）
ipclick run -w                     # 再带上 Web 管理端（http://127.0.0.1:9527）
ipclick run -w --web-lan           # Web 端监听 0.0.0.0，局域网可访问
ipclick health                     # 探活，健康退出 0

# 同一台机器上起多个实例：各读各的配置
ipclick init --port 8001           # 生成 ipclick-8001.toml
ipclick run  --port 8001           # 自动读它
```

**端口**：gRPC 默认 **9528**，Web 管理端默认 **9527**。0.5.0 之前分别是 9527 / 9530——
连不上而端口正好是 9527 时，多半是连到 Web 端口上了，错误信息里会提示这一点。

`--web-lan` 是明文 HTTP，密码会在网络上裸奔。局域网内自用可以接受，跨网段暴露请放在
做了 TLS 终止的反向代理之后。

## 别做这些

- **别拿它当本机 curl 用。** 它要连一个 gRPC 服务端，起不来就是退出码 3。
  普通的本机 HTTP 调用用 curl。
- **别在 `--json` 的输出里指望有日志。** 日志在 stderr。
- **别忽略 `body_truncated`。** 截断的页面拿去做解析会得到无声的错误结果。
- **别猜适配器名。** 以 `ipclick status --json` 的 `adapters.ready` 为准。
- **别把令牌写进命令行。** 用 `IPCLICK_AUTH_TOKEN` 环境变量或 `.env`——命令行参数会
  进 shell 历史和进程列表。
- **别用 `ipclick fetch` 打内网地址**再去抱怨被拦。`[SECURITY].block_private_networks`
  存在的意义就是拦它。
