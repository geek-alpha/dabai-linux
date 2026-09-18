# x_ip —— 时事推文 IP 工作流

把「中文热点 + 你的定位」变成有立场的推文草稿，审阅后发到 X。
默认不发：除了 `post` 和 `run --auto-post`，所有命令都只读或只写本地库。

## 为什么不是「热搜榜转发器」

实测（2026-09-18）微博+百度+B站+60秒 共 65 条中文热搜里，**只有 2 条**跟技术 IP 有关，
其余是睡姿、便当、演唱会求婚。纯热点榜的排序是「热度 ÷ 名次」，追它只会得到一个
什么都聊、什么都不专的账号。

所以这个工具做了两件事：

1. **换了主料**：主源是 V2EX latest（中文开发者真实在聊的事）和 GitHub Trending，
   微博/百度降为辅助。加源后 85 条里 21 条过技术 IP 契合阈值。
2. **改了排序**：`score = 源权重^0.5 × 契合度^1.8 × 跨源共振`。
   契合度指数 1.8 是故意的——线性相乘时，微博 #1 的生活话题能压过 V2EX 的真技术帖，
   因为名次差距（3 倍）大于契合度差距（2 倍）。

## 文件分工

| 文件 | 管什么 |
|---|---|
| `sources.py` | 7 个源采集 + 跨源去重指纹 |
| `topics.py` | 选题打分（热度 × IP 契合度 × 跨源共振），违禁词归零 |
| `compose.py` | LLM 生成 3 条不同角度的观点草稿 + 加权字数校验 + 超长压缩 |
| `x_api.py` | X API v2 发布，手写 OAuth 1.0a 签名（不依赖 tweepy） |
| `store.py` | SQLite：选题/草稿/已发三张表，防重复防复读 |
| `cli.py` | 编排 + 三道发布闸门 |
| `selftest.py` | 50 项自检，不联网、不真发 |
| `persona.json` | **IP 定位配置——改这里比改代码重要** |
| `x_credentials.json` | X 凭证（待填） |

## 前置

1. **代理**：`api.x.com` 和 `www.v2ex.com` 直连超时，默认走 `127.0.0.1:7890`。
   改端口：`export X_IP_PROXY=http://127.0.0.1:PORT`
2. **X 凭证**：去 developer.x.com 建 App，User authentication settings 设为 **Read and write**，
   把 API Key/Secret + Access Token/Secret 填进 `x_credentials.json`。
   Free 档每月 500 条写入，个人 IP 够用。
3. **LLM**：自动按顺序试三个通道——环境变量 → `settings.json` 主配置 → `stt_config.json` 的
   siliconflow key。任一可用即可，全挂才退到模板骨架。

## 上手

```bash
cd /home/wxf/dabai
A="venv/bin/python -m tools.x_ip.cli"

$A verify                 # 校验签名/凭证/LLM 通道
$A collect --per 15       # 采集入库（约 5 秒）
$A top --limit 15         # 看选题排行，带契合理由
$A draft --count 3        # 给榜首选题生成 3 条不同角度的草稿
$A drafts                 # 列出待审草稿
$A approve 4              # 批准
$A post 4 --dry-run       # 先干跑看一遍
$A post 4                 # 真发
$A stats                  # 状态概览
```

改稿：`$A edit 4 "新的正文"`
直接发一条不进选题库：`$A post-text "正文"`

## 定时任务

已挂在 crontab（**不是** sched_add 的定时任务——那条走子智能体，中间多一个 LLM 调用，
实测会因 harness 的 DeepSeek 欠费 402 直接挂掉，而这是条确定性命令，不需要 LLM 在环）：

```cron
0 9,20 * * * cd /home/wxf/dabai && venv/bin/python -m tools.x_ip.cli run --n 2 --count 3 >> tools/x_ip/cron.log 2>&1
```

每天 09:00 和 20:00 各跑一次（对应 `persona.post_windows`），只生成草稿不发布。
撤销：`crontab -e` 删掉这行，或 `crontab < tools/x_ip/crontab.bak-*` 回滚。

手动跑同一条：

```bash
venv/bin/python -m tools.x_ip.cli run --window --n 2 --count 3
```

`--window` 会再查一次发布时段（cron 已经卡在点上，这是双保险，手动跑时有用）。

要全自动发布就在命令末尾加 `--auto-post`（每个选题发第一条合规草稿）。
建议先跑一周草稿模式，确认质量稳定再开。

## 三道发布闸门

`post` 会依次检查，任一不过就拒绝：

1. **状态**：草稿必须是 `approved`（`--force` 可越）
2. **内容**：加权字数 ≤ 262、话题标签 ≤ 2、无套话、无感叹号堆砌
3. **去重 + 配额**：同一热点只发一次（IP 最忌讳复读）；当日 ≤ `daily_quota` 条

`run` 额外跳过已有待审草稿的选题，避免同一话题反复生成。

## 字数怎么算

X 按 twitter-text 加权计长：**CJK 字符算 2，ASCII 算 1**，标准上限 280。
所以「中文 270 字」在 X 上等于 540，会被直接拒。`compose.weighted_len()` 实现的就是这个规则，
`persona.safe_weighted = 262` 是留了余量的安全线。

模型算不准加权字数（实测生成过 284 和 266），所以 `compose.repair()` 会二次压缩，
压缩也失败就按行硬截断——超长草稿绝不会进发布链路。

## 自检

```bash
venv/bin/python -m tools.x_ip.selftest
```

50 项，覆盖 OAuth 签名（正向量 + 篡改密钥必须失败）、加权字数、状态库去重、
选题打分（技术选题必须压过生活话题）、超长压缩、GitHub HTML 解析、五道发布闸门、
发布时段（含跨午夜）。全部离线，不碰真实库、不发帖。

## 已知边界

- **V2EX 走代理**：直连超时，`sources.v2ex()` 里写死 `proxy=True`
- **36Kr / 知乎没接**：36Kr 热榜是 JS 壳（HTML 里没有 `window.initialState`），知乎 401 要 cookie
- **HN 很慢**：每条详情单独请求，默认源列表里不含它（`--sources hn` 可手动加）
- **GitHub Trending 是抓 HTML**：GitHub 改版会打破正则。`selftest.py` 里存了内嵌样本，
  改版后自检会先红，比线上静默失败好
- **签名只签 query 不签 JSON body**：OAuth 1.0a 只把 `application/x-www-form-urlencoded`
  的 body 并进签名，X API v2 用的是 JSON body，所以 `_request` 里 body 不进签名。
  换签名实现时别顺手把 body 加进去——`selftest` 的官方向量就是靠这条才能对上
