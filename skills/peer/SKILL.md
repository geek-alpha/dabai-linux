# 大白联邦（peer）

让散落在不同机器上的大白互相找到、互相说话、互相打电话。

## 一句话原理

每个实例都有自己的公网域名（cloudflared 隧道 + DNS），**域名就是电话号码**。
再加一把共享密钥，谁有谁能进。寻址和传输都已经存在，这里只补了密钥和语义。

所以没有中转服务器、没有新端口、没有新隧道 —— 实例之间点对点直连各自的域名。

**打电话和留言的差别只有一个字：时延。** 留言是「对方下次醒来才看到」，打电话是
「对方当场响」。补一个常驻耳朵（peer_watch）就够了，不需要长连接框架。

## 联邦成员

| 实例 | 域名 | 位置 |
|---|---|---|
| `rpi` | dabai.battlephoenix.tech | 树莓派（本地生产） |
| `aliyun` | aliyun.battlephoenix.tech | 阿里云 ECS 39.106.53.2 |
| `wsl` | wsl.battlephoenix.tech | Windows 上的 Debian WSL |

## 工具

| 工具 | 用途 |
|---|---|
| `peer_list` | 联邦点名：谁在线、各自负载/内存/温度/在线时长、能不能接电话 |
| `peer_call` | **打电话**：发一句并当场等它回话（默认等 90 秒） |
| `peer_task` | **派活**：让对面起一个后台子智能体真的去执行，干完把结论回你收件箱 |
| `peer_say` | 异步留言：对方下次醒来才看到 |
| `peer_inbox` | 读本实例收到的留言（读完自动标已读） |
| `peer_state` | 只问一个实例的实时状态 |

**来信会自动进上下文**：`agent.py` 每轮往 system prompt 注入一段 `【联邦来信】`
（未读数 + 最近两条摘要，只数行不消费已读）。不用主动惦记查收件箱 —— 看到那段就去
`peer_inbox` 拆全文。空箱或已全读时返回空串，零开销。

## 命令行

```bash
python peer_mesh.py status                       # 联邦点名
python peer_mesh.py call aliyun "你那边怎么样"   # 打电话（等对方回话）
python peer_mesh.py call aliyun "..." --wait 30  # 自定义等待上限
python peer_mesh.py say aliyun "在吗"            # 异步留言
python peer_mesh.py inbox                        # 读收件箱（--read 标记已读）
python peer_mesh.py state aliyun                 # 对方状态
python peer_mesh.py key                          # 打印共享密钥
python peer_mesh.py add-peer <名字> <域名>       # 手工加同伴
python peer_watch.py --once --replay             # 耳朵跑一轮就退（自测）
python peer_watch.py --no-reply                  # 只响铃不回话
```

## 耳朵（peer_watch）

`server.py` 启动时把它丢进线程池常驻，不用单独部署、不用另开进程。

每 0.4 秒看一眼自己的收件箱（读本地文件，不走网络），有新消息当场处理：

| 收到 | 行为 |
|---|---|
| `kind=call` | 桌面通知 + 立刻回一句（对面正守着等） |
| `kind=reply` | 只记日志，**绝不回话**（否则两个大白互相刷屏） |
| `kind=say` | 桌面通知，不回话（留言就该是留言） |
| `kind=task` | 桌面通知，不回话；`server.py` 那侧已把它变成一次性定时任务派给子智能体执行 |

回话用的是**大白本体的 LLM 档位**（settings.json 当前激活供应商）配本机实时状态，
所以「你那边怎么样」是真答得上来的。

节流：同一个同伴一分钟最多回 3 次 —— 对方程序出错时不能变成无限对话。

心跳：循环每轮更新 `_last_beat`，并按 `BEAT_WRITE_EVERY` 节流落一份到
`/dev/shm/dabai_peer_ear.beat`（内存盘，不磨 SD 卡）。

为什么必须落盘：`server.py` 里 `import peer_watch` 拿到的是自己进程内的副本，
内存变量在跨进程读时恒为 0 —— 表现为耳朵明明在跑，`state` 的 `ear` 却永远是 false。
读侧（`local_state`）先信内存，再回退读心跳文件，两者都带 `max_age` 兜底。

## 派活（kind=task）

第一性原理：同伴要的不是「我替它跑命令」，是「让它那台的执行器动起来」。那台机器
本来就有无人值守的执行入口 —— `scheduler.py` 每 15 秒从 `data/scheduled_tasks.json`
重读一次，外部进程写进去就会被捡到。所以这里不新增执行通道，只把 task 消息翻译成
一条一次性定时任务（`once=true`）。**耳朵照旧只响铃、不执行** —— shell 不进耳朵：
联邦消息只凭密钥认证，给耳朵挂 shell 等于在链路上开一个「同伴说一句话就能在对面
跑命令」的洞。

三道闸门，按代价从低到高：

| 闸门 | 行为 |
|---|---|
| 总开关 | `settings.json` → `peer.allow_remote_task`（缺省开）；关掉则一单不接 |
| 危险模式 | `rm -rf` / `mkfs` / `dd of=/dev/` / `shutdown` / `curl…\|sh` / `chmod -R 777 /` 等**不可逆**动作命中即拒，只记录不执行 |
| 配额 | 同一同伴每小时最多 6 单 —— 对方程序出错时不能变成刷屏 |

被拦下的不占配额、不落单，全部写进 `data/peer_tasks.jsonl` 留痕。
接单后子智能体干完，按任务里自带的要求把结论 `say` 回发起方收件箱。

## 协议

全部 `POST`，JSON body，头 `X-Phoenix-Key` 带共享密钥。
路由挂在自己的实例上，路径在 `server.py` 的 `_AUTH_EXEMPT_PREFIX` 里豁免了会话中间件
（别的实例没有、也不该有本机的会话 cookie）。

| 路径 | 作用 |
|---|---|
| `/api/peer/whoami` | 握手：验密钥，回身份 |
| `/api/peer/say` | 收信：验签名，落收件箱 |
| `/api/peer/inbox` | 读信 |
| `/api/peer/state` | 状态：负载/内存/温度/开机时长/耳朵是否在跑 |

**签名**：`HMAC-SHA256(key, "from|ts|text")`，时间窗 ±300 秒。
为什么密钥之外还要签名 —— 密钥在三个实例上都存着，只验密钥的话，任一实例被拿下
就能冒充其他实例发话。签名把「拿到密钥」和「冒充某个身份」拆成两件独立的事。

**cid**（这通电话的编号）和 **re**（在回哪条消息）不进签名域：加进去会让新旧版本的
验签算法分叉，而联邦是滚动升级的，三台机器不会同时换代码；伪造它们本身也需要先
拿到密钥。

**认回音用 cid 而不是时间戳**：两台机器的时钟不需要同步。旧版服务端不落 cid 时，
退化成「不早于我拨号时间的同源 reply」兜底。

## 落盘

| 文件 | 内容 |
|---|---|
| `data/cluster.key` | 32 字节共享密钥（0600），全集群同一把 |
| `data/node.json` | 本实例身份 `{"node_id": "rpi", "label": "树莓派"}` |
| `data/peers.json` | 地址簿 `{"nodes": {"aliyun": {"url": ..., "label": ...}}}` |
| `data/peer_inbox.jsonl` | 收件箱（追加写） |
| `data/peer_cursor.json` | 大白的已读游标 |
| `data/peer_watch_cursor.json` | **耳朵的**游标（独立！） |
| `data/peer_watch.log` | 耳朵日志（超过 256KB 自动截尾） |
| `data/peer_tasks.jsonl` | 派活审计：每条 task 的原文 + 结局（accepted / blocked / quota / ...） |
| `data/peer_task_quota.json` | 派活配额：每同伴一小时的派单时刻表 |
| `data/scheduled_tasks.json` | 接单后落的一次性任务（`once=true`，跑完自动退役） |

两个游标必须分开：「大白自己读到哪」和「耳朵听到哪」是两件事，共用的话耳朵先听到
就等于大白永远看不到这条消息。

## 踩过的坑

1. **Cloudflare 会 403 掉 urllib 的默认 User-Agent**（`Python-urllib/3.x`）。
   实测同一请求：不带头 403，带任意自定义 UA 200。所以 `_post` 里必须设 UA。
2. **urllib 默认读环境变量里的代理**。WSL 的 `dabai.service` 设了 `HTTPS_PROXY`
   （那是给出国用的），会让「两个大白说话」变成「经过第三方代理中转」，
   代理一挂联邦就断。`_post` 用 `ProxyHandler({})` 显式绕开。
3. **`data/` 权限必须是 0700**：`cluster.key` 是联邦的唯一凭证。
4. **不能只认一个 LLM 档位来源**：阿里云实测 `codex_config.json` 的 llm 段指向一把
   失效的 key（siliconflow，401），而 `settings.json` 激活的 deepseek 档位是好的。
   耳朵按「本体档位优先、失败自动换下一个」找脑子，试通的那个记住。
5. **首启动不回灌历史**：游标文件不存在时先把游标推到末尾，否则第一次开耳朵会把
   积压的旧留言全当成新来电，挨个回话刷屏。
6. **核心代码改动不会自动重启**（`harness.core_autorestart=false`）——
   改完 `peer_mesh.py` / `peer_watch.py` / `server.py` 要手动重启服务才生效。
   表现是「文件改了但行为没变」。
7. 通过 Windows 上的 WSL 传文件时，`ssh win "wsl -d Debian -- bash -lc '... && ...'"`
   里的 `&&` 会被 Windows 的 cmd 解析掉，改用 `;` 并把命令包在双引号里。
8. 阿里云没有 SSH（publickey denied），部署走 `workbench exec`；
   文件用 `gzip | base64` 内联传，两个文件一起约 11KB 载荷一次能过。

## 加新实例

1. 把 `peer_mesh.py`、`peer_watch.py`、`tools/peer_mesh_install.py` 拷到新机器
2. `python tools/peer_mesh_install.py --id <名字> --label <显示名> --key <共享密钥>`
3. 在 `server.py` 的 lifespan 里挂耳朵（照抄现有那段）
4. 重启该机器的服务
5. 在**所有已有实例**上 `python peer_mesh.py add-peer <名字> <域名>`（地址簿不是自动同步的）
