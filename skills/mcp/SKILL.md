# MCP 接入（mcp）

接第三方 MCP server。**核心取舍**：server 的工具不占常驻工具表 —— 先 connect 拉清单，再 call。
不用的 server 不占进程、不占上下文。

两种连接：远程传 `url`（Streamable HTTP，不占本机资源）；本地传 `command`（拉子进程）。

## 工具

| 工具 | 作用 |
|---|---|
| `mcp_servers` | 列已配置 server + 运行状态（零成本，不连接） |
| `mcp_connect(server, url?, headers?, command?, args?, env?, cwd?, allow_heavy?)` | 连接、initialize、返回工具清单。传 `url` 走 HTTP，传 `command` 拉子进程；**连接成功后**才存进 `servers.json` |
| `mcp_call(server, tool, arguments)` | 调用工具；未连接会自动连接 |
| `mcp_disconnect(server)` | 断开（本地杀进程组、远程清会话）；`server="all"` 断全部 |

## 标准流程

```
1. mcp_servers                                   # 看有没有现成的
2. mcp_connect(server="fs", command="npx",
     args=["-y","@modelcontextprotocol/server-filesystem","/tmp"])   # 拿工具清单
3. mcp_call(server="fs", tool="read_file", arguments={"path":"/tmp/a.txt"})
4. mcp_disconnect(server="all")                  # 用完就杀
```

## 远程 server（Streamable HTTP）

公共托管的 MCP 基本都是 URL 端点，传 `url` 就能接，不占本机内存和温度：

```
1. mcp_connect(server="xxx", url="https://host/mcp",
     headers={"Authorization":"Bearer <key>"})     # 不要鉴权的省略 headers
2. mcp_call(server="xxx", tool="...", arguments={...})
3. mcp_disconnect(server="xxx")
```

实现（`mcp_http.py`）照 MCP 2025-03-26 规范：单端点 POST、Accept 同时含
`application/json` 与 `text/event-stream`、服务端回的 `Mcp-Session-Id` 后续请求原样带回；
响应是 JSON 或 SSE 两种都支持。

**旧版 HTTP+SSE（2024-11-05 的 `/sse` 长连接）不支持** —— 那种端点回 405/406，报错里会点明。
key 一律放 headers，别写进 URL 查询串（会进日志）。

## 资源闸门（硬拦截）

这台是 **1GB 内存的 Raspberry Pi 3**，被动散热、空载就 63°C。之前拉无头 chromium
直接把它烧到 SoC 硬关机（journal 整段丢失，大白离线）。提示词里写「注意资源」是软约束，
靠不住——所以拦在代码里（`mcp_client.resource_guard`），连接和调用前都查：

| 条件 | 阀值 | 动作 |
|---|---|---|
| 温度 | ≥ 72°C | 拒绝 |
| 可用内存 | < 180MB | 拒绝 |
| 并发 server | ≥ 2 个 | 拒绝新连接 |
| 命令含浏览器内核 | chromium/chrome/firefox/webkit/playwright | 需 `allow_heavy=true` 才放行 |

被拦时返回原因和当前读数，不是静默失败。传感器读不到时**不拦**（宁可放过，不误伤）。

要跑浏览器类 server：先确认真的必要，再传 `allow_heavy=true`，并且**用完立刻 disconnect**。
但这台机器上基本不该跑——读网页用 `read_web`（httpx，几 MB），比 chromium 轻两个数量级。

## 几条硬规矩

- **参数照清单**：工具名和参数结构必须来自 `mcp_connect` 返回的清单，不要凭印象猜。
- **用完就杀**：常驻进程会一直吃内存，任务结束调 `mcp_disconnect`。
- **首次会慢**：`npx -y` 首次要下载包，超时给 120s 以上；超时了就重试一次，别急着换方案。
- **进程崩了会带 stderr**：报错信息里带 stderr 末尾，直接看那个定位。
- **第三方代码**：connect 等于在本机运行对方代码，`env` 里只给必要的密钥。
- **uvx 本机没有**：Python 系 server 用 `python3 -m ...` 或装 uv 后再用。

## 配置持久化

`skills/mcp/servers.json`，格式：

```json
{
  "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
  "fetch": {"command": "python3", "args": ["-m", "mcp_server_fetch"], "env": {"HTTP_PROXY": "http://127.0.0.1:7890"}},
  "remote": {"url": "https://host/mcp", "headers": {"Authorization": "Bearer xxx"}}
}
```

`mcp_connect` 传过 command 的会自动写进去，之后 `mcp_call` 直接按名字用。

## 排错

- 日志：`skills/mcp/logs/<server>.stderr.log`
- 连不上先手工跑一遍启动命令（`npx -y ...`），确认包能下、命令本身没问题
- 协议自测：`python3 tools/mcp_selftest.py`（项目根；夹具 server 跑通 initialize/list/call/杀进程）
- HTTP 自测：`python3 skills/mcp/tools/mcp_http_test.py`（本地假 server，验 JSON/SSE 两条响应路径、session 回传、401 提示）
- 闸门自测：`python3 skills/mcp/tools/mcp_guard_test.py`（不启任何 server，只验阀值）
- 孤儿自测：`python3 skills/mcp/tools/mcp_orphan_test.py`（假 server 验 pid 对账）
- **热重载会留孤儿**：改 `skills/mcp/*.py` 触发模块重载 → `_SERVERS` 字典清空，但子进程（独立
  进程组）还在跑，工具再也管不到。所以 spawn 时把 pgid 写进 `run/<name>.pid`，
  `mcp_servers` 会报孤儿、`mcp_disconnect(server="all")` 会顺手回收。
- **npx 会 fork 孙进程**（npm exec → sh → node），所以子进程起了独立进程组、`mcp_disconnect` 杀的是整组。
  自己手工 `kill` 时也要按进程组杀（`kill -- -<pgid>`），否则留孤儿。

