# 飞书（lark-cli）

larksuite 官方 CLI 的 dabai 适配层。上游：[github.com/larksuite/cli](https://github.com/larksuite/cli)（MIT，Go 实现），
二进制里内嵌 28 个 Agent Skill，覆盖 20+ 业务域。

上游是「Agent 自己拼 shell」的形态；这里收成 4 个工具，命令仍然透传，所以上游文档就是权威用法来源。

## 工具

| 工具 | 用途 |
|---|---|
| `lark(args=[...])` | 执行任意 lark-cli 命令（透传）。读操作直接走；`--yes` 会被剥掉，高风险写要用户确认后 `confirm=true` |
| `lark_status()` | 版本 + `doctor` 体检 + `auth status`（配置/登录态一屏看完） |
| `lark_login(mode, finish, domains)` | `mode="app"` 建/绑应用；`mode="auth"` 发起用户授权；`finish=true` 收尾 |
| `lark_skill(action, name)` | 读内嵌技能文档：`list` 列 28 个域，`read` 读某个域全文 |

## 认证（两步，都要用户在浏览器操作）

1. **配应用**：`lark_login(mode="app")` → 返回授权链接（内部后台跑 `config init --new`，它阻塞等浏览器，所以不占对话）。用户在浏览器创建/授权应用，配置自动写入。
2. **用户授权**：`lark_login(mode="auth")` → 返回链接（内部 `auth login --recommend --no-wait --json`，device code 落盘 `.device_code`）。用户授权完成后调 `lark_login(mode="auth", finish=true)` 收尾。
3. **验证**：`lark_status()`。

链接原样转发，不改 query、不加标点。上游建议配二维码（`lark-cli auth qrcode <url> --ascii`）。

## 先读文档再动手

命令别猜。三条路：

- `lark_skill(action="read", name="lark-im")` —— 读该域 SKILL.md 全文（概念、shortcuts、references 路径）
- `lark(args=["im", "--help"])` —— 命令树
- `lark(args=["schema", "im.message.create"])` —— 单个 API 方法的参数/类型/权限点

优先用 `+shortcut`（如 `calendar +agenda`、`im +send`），它们比裸 API 资源多一层工作流逻辑。

## 域路由（28 个内嵌技能）

| 技能 | 干什么 |
|---|---|
| `lark-shared` | 底座：认证、身份（user/bot）、权限域、输出契约。**动手前先读** |
| `lark-im` | 收发消息、群聊管理、消息搜索、图片/文件上传下载、表情回复、交互卡片 |
| `lark-doc` | 文档创建/读取/更新/搜索（Markdown 形态） |
| `lark-markdown` | 云盘原生 Markdown 文件的创建/读取/覆盖 |
| `lark-sheets` | 电子表格创建、读写、追加、查找、导出 |
| `lark-base` | 多维表格：表/字段/记录/视图/仪表盘/聚合分析 |
| `lark-slides` | 幻灯片创建与管理、读取内容、增删页 |
| `lark-calendar` | 日程创建更新、议程、忙闲查询、时间建议、会议室、回执 |
| `lark-task` | 任务、任务清单、子任务、提醒、指派 |
| `lark-mail` | 邮件浏览/搜索/读/发/回/转/草稿、新邮件监听 |
| `lark-drive` | 云盘文件上传下载、权限与评论管理 |
| `lark-wiki` | 知识空间、节点、文档 |
| `lark-contact` | 按姓名/邮箱/电话搜人、读用户资料 |
| `lark-approval` | 审批待办/已办/实例查询、同意/拒绝/转交、撤回与抄送 |
| `lark-attendance` | 个人考勤打卡记录 |
| `lark-okr` | OKR 查询与创建更新、对齐、指标、进展 |
| `lark-meeting` | 实时/历史会议检索、参会人与产物、转写分析、纪要 |
| `lark-minutes` | 妙记内容与元数据 |
| `lark-note` | 会议笔记详情与统一转写 |
| `lark-vc` / `lark-vc-agent` | 视频会议与会议纪要；VC agent |
| `lark-event` | 实时事件订阅（WebSocket）、正则路由 |
| `lark-whiteboard` | 白板/图表 DSL 渲染 |
| `lark-apps` | 妙搭应用开发与托管（本地/云端开发、部署、日志、协作权限） |
| `lark-openapi-explorer` | 从官方文档探索底层 API |
| `lark-skill-maker` | 自定义技能创建框架 |
| `lark-workflow-meeting-summary` | 工作流：会议纪要聚合与结构化报告 |
| `lark-workflow-standup-report` | 工作流：议程与待办汇总 |

## 硬规则（来自上游 AGENTS.md / lark-shared）

1. **成功判定看 `ok == true`**，不是 `code == 0`。成功信封没有顶层 `code` 字段，按老格式判断会把成功全判成失败。
2. **高风险写命令要 `--yes`**：本层会剥掉未经确认的 `--yes`；用户明确同意后再传 `confirm=true`。
3. **身份决定代表谁**：`--as user` 代表用户本人（能碰其日历、云空间），`--as bot` 代表应用自己（查用户资源返回空成功，不报错）。动手前先确认身份。
4. **不输出密钥**：app secret、access token 不落终端明文。
5. `--dry-run` 可预览危险请求；`--jq <expr>` 过滤输出。

## 本机安装（已装，备查）

```bash
npm install -g --prefix ~/.local @larksuite/cli   # 装到 /usr/local 会 EACCES
~/.local/bin/lark-cli --version                   # 1.0.96
```

技能目录：`/home/wxf/dabai/skills/lark/`（`skill.py` 适配层、`.device_code` device code、`.config-init.log` 配置流程日志）。

## 两个身份，别搞混（实测）

| 想做的事 | 用哪个身份 | 写法 |
|---|---|---|
| 读日历 / 文档 / 消息 / 搜群 | `user`（默认 auto 会挑 user） | `lark calendar +agenda` |
| 发一条新消息 / 建群 | **bot**（`im/v1/messages` 只认 tenant token，用户身份没有发消息的 API） | 见下 |

发消息（CLI 没有 `im +send`，走原始 API）：

```json
lark(args=["api","POST","/open-apis/im/v1/messages",
           "--params","{\"receive_id_type\":\"open_id\"}",
           "--data","{\"receive_id\":\"ou_xxx\",\"msg_type\":\"text\",\"content\":\"{\\\"text\\\":\\\"hi\\\"}\"}",
           "--as","bot"])
```

注意 `content` 是**字符串化的 JSON**，不是对象——这是飞书消息 API 的老规矩。

## 授权状态与续期

`lark_status()` 给体检表。user token 短期有效（约 2 小时），refresh token 约 7 天；
过期就 `lark_login(mode="auth")` 重发一次 device flow 链接，用户点一下即可，不用重建应用。
