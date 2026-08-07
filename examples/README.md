# 示例

所有示例都通过 `examples.base_config` 共享目标地址，因此需要**从仓库根目录**以模块方式运行：

```bash
python -m examples.basic_get
```

直接 `python examples/basic_get.py` 会因为找不到 `examples` 包而报 `ModuleNotFoundError`。

运行前请先启动服务端：

```bash
ipclick run
```

如果服务端没起来，示例不会崩溃——`Downloader` 会返回 `status_code == -1`
且 `error` 非空的响应。

## 列表

| 示例                     | 内容                          |
|------------------------|-----------------------------|
| `basic_get.py`         | 基础 GET、查询参数、指定适配器           |
| `basic_post.py`        | 表单 POST 与 JSON POST         |
| `headers_cookies.py`   | 自定义 headers / cookies / 指纹伪装 |
| `proxy_usage.py`       | 三种代理写法                      |
| `advanced_options.py`  | 超时、重试、重定向控制                 |
