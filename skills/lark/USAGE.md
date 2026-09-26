# 大白 · 飞书 CLI 使用手册

> 面向：大白（AI）自己 + 使用者本人
> 事实来源：`lark-cli 1.0.96` 的 `--help` 实跑输出 + 本机实测（每条命令都跑过，标 🔬 的带真实返回）
> 落地位置：本机 `/home/wxf/.local/bin/lark-cli`，大白适配层 `skills/lark/`

---

## 一、这套东西是什么

飞书官方 CLI（上游 [github.com/larksuite/cli](https://github.com/larksuite/cli)，MIT，Go 实现），二进制里内嵌 28 个 Agent Skill 文档，覆盖 22 个业务域。

大白在它外面包了一层 `skills/lark/`，把「Agent 自己拼 shell」收成 4 个工具调用，命令仍然透传——所以上游文档就是权威用法来源。

**一句话心智模型**：`lark-cli` 是一把万能钥匙，`skills/lark` 是把它挂到大白腰上的挂扣。

---

## 二、当前环境状态（已完成，勿重装）

| 项 | 值 |
|---|---|
| 二进制 | `/home/wxf/.local/bin/lark-cli` |
| 版本 | `1.0.96`（`lark-cli update` 可升级） |
| 应用 App ID | `cli_aa3ca5f7d7799cb3` |
| 用户身份 | ready，openId `ou_32bd6986eb24be4e9e1ff8a1bbd56f98`，token 有效 |
| 授权范围 | 全部业务域（`--recommend`），含 `offline_access`（自动续期） |
| 大白技能目录 | `skills/lark/`（`skill.json` + `skill.py` + `SKILL.md` + 本手册） |

### 换机 / 重装四步

```bash
# 1. 装（装到 /usr/local 会 EACCES，必须用用户级 prefix）
npm install -g --prefix ~/.local @larksuite/cli

# 2. 建/绑应用：会阻塞等浏览器，必须后台跑 + 轮询日志抓 URL
#    大白侧已封装：lark_login(mode="app")
lark-cli config init --new

# 3. 用户身份授权（拿链接，不阻塞）
lark-cli auth login --domain all --no-wait --json
#    用户在浏览器点完，用 device_code 收尾
lark-cli auth login --device-code <device_code>

# 4. 体检
lark-cli doctor && lark-cli auth status --json --verify
```

⚠️ 授权链接**不能跨轮缓存**：每次重新发起都要用 `--no-wait --json` 生成新链接，旧 device_code 会过期。

---

## 三、大白侧的 4 个入口

| 工具 | 用途 | 典型调用 |
|---|---|---|
| `lark_status()` | 体检：二进制路径、版本、配置、登录态 | 动手前先跑 |
| `lark(args=[...])` | 执行任意 `lark-cli` 命令（透传） | `lark(args=["calendar","+agenda"])` |
| `lark_login(mode="app"\|"auth", finish=?)` | 两步式授权，不阻塞对话 | 换机时用 |
| `lark_skill(action="list"\|"read", name=?)` | 读内嵌的 28 个技能文档 | 用某域前先读 |

**28 个技能文档不落盘**：走 `skills list` / `skills read` 直接从二进制里取，CLI 一升级文档自动同步，不会版本错位。

### 高风险动作的确认闸

- 命令自带风险等级：`read` / `write` / `high-risk-write`
- `high-risk-write` 必须 `--yes`，且**只在用户明确确认后**才加
- 大白侧另有一道闸：`lark` 工具会剥掉 `--yes`，需要 `confirm=true` 才放行
- 发消息、建群这类写操作，先确认收件人和内容再发

---

## 四、命令体系四层（优先级从高到低）

```bash
# ① +shortcut —— 高层任务封装，优先用这个
lark-cli calendar +agenda

# ② typed —— 某个 API 方法的类型化封装
lark-cli mail user_mailbox.messages list --user-mailbox-id me

# ③ schema —— 调用前先查参数、类型、所需 scope
lark-cli schema mail.user_mailbox.messages.list

# ④ api —— 兜底逃生舱，按 HTTP 路径直调任意端点
lark-cli api GET /open-apis/calendar/v4/calendars
```

选择原则：**能匹配上 `+shortcut` 就用它**；没有封装才退到 typed；再没有走 `api`。

三个通用开关：

| 开关 | 作用 |
|---|---|
| `--jq '<expr>'` | 过滤 JSON 输出，省 token |
| `--dry-run` | 只预览请求，不真发 |
| `--as user\|bot` | 切换身份 |

---

## 五、身份模型（实测校准，别踩错）

| 身份 | 标识 | 适用 | 注意 |
|---|---|---|---|
| `user` | `--as user` | 用户自己的资源：日历、云盘、文档、消息读取 | 代表本人操作 |
| `bot` | `--as bot` | 应用级操作：发消息、建群 | **查用户资源会返回「空成功」而不是报错** |

### 两条实测结论

**① 发消息：bot 身份开箱可用**

```bash
lark-cli im +messages-send --user-id ou_xxx --text "内容" --as bot
# 🔬 {"ok":true,"identity":"bot","data":{"chat_id":"oc_06cf...","message_id":"om_x100b64558c33f4a4c10676af24b15c0"}}
```

**② 发消息：user 身份要先补一个 scope**

```bash
lark-cli im +messages-send --user-id ou_xxx --text "内容" --as user
# 🔬 退出码 3：missing required scope(s): im:message.send_as_user
```

补法（会再弹一次授权，需要用户点链接）：

```bash
lark-cli auth login --scope "im:message.send_as_user" --no-wait --json
```

> 早期笔记里写过「用户身份没有发消息的 API」——**这句是错的**，API 存在，只是默认授权包没带这个 scope。以本节为准。

### 判断成功看哪个字段

`--format json`（默认）下判断成功用 `ok == true` 或退出码 0。**不要看 `code == 0`**——成功信封根本没有顶层 `code`，按老 OpenAPI 格式判断会把所有成功调用误判成失败。

---

## 六、业务域速查（22 个域）

| 域 | 命令 | 能干什么 |
|---|---|---|
| 消息 / 群 | `im` | 收发消息、搜索消息、群管理、书签、reaction |
| 日历 | `calendar` | 日程增删改查、空闲忙、会议室、RSVP、改期 |
| 云文档 | `docs` | 建/读/改文档、插入图片附件、历史版本、搜索 |
| 电子表格 | `sheets` | 单元格/CSV/表格读写、图表、条件格式、透视、导入导出 |
| 多维表格 | `base` | 表/字段/记录/视图/仪表盘/表单/工作流/权限 |
| 任务 | `task` | 任务、清单、子任务、提醒、评论、附件 |
| 云盘 | `drive` | 上传下载、复制移动删除、权限、评论、版本、目录镜像同步 |
| 知识库 | `wiki` | 空间与节点管理、节点复制移动 |
| 通讯录 | `contact` | 按姓名/邮箱解析 open_id，反查部门/邮箱 |
| 邮箱 | `mail` | 收发邮件、草稿、文件夹、标签 |
| 审批 | `approval` | 待办/已办/实例查询，发起审批 |
| 考勤 | `attendance` | 打卡记录查询 |
| 会议 / 妙记 | `meeting`、`vc`、`minutes`、`note` | 会议记录、纪要、逐字稿、妙记 |
| 幻灯片 | `slides` | 创建管理幻灯片、读内容 |
| 画板 | `whiteboard` | 创建编辑画板（支持 mermaid / plantuml） |
| 思维笔记 | `mindnotes` | 节点列表、创建、更新 |
| OKR | `okr` | 目标、关键结果、对齐、进度 |
| 妙搭应用 | `apps` | 应用开发托管、触发器、日志监控 |
| 实时事件 | `event` | 订阅消费实时事件流 |
| 原生 Markdown | `markdown` | 飞书原生 md 文件创建/覆盖 |
| 开放平台应用 | `application` | 当前绑定应用的斜杠命令 |
| 工具 | `api` / `schema` / `skills` | 兜底调用、查参数、读技能文档 |
| CLI 管理 | `auth` / `config` / `doctor` / `profile` / `update` / `whoami` | 授权、配置、体检、升级 |

---

## 七、踩坑清单（都是真踩过的）

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | 以为发消息命令是 `im +send` | 不存在。正确是 `im +messages-send`，另有 `+messages-reply`、`+messages-edit` |
| 2 | user 身份发消息报 `missing_scope` | 补授权 `im:message.send_as_user`，或直接用 `--as bot` |
| 3 | 用 `code == 0` 判断成功 | 用 `ok == true` 或退出码 0 |
| 4 | 删重复日程只删掉一天 | 加 `--apply-to all` 删整个系列；只删单次用其他取值 |
| 5 | npm 全局装报 EACCES | 加 `--prefix ~/.local`，别装系统目录 |
| 6 | `config init --new` 卡住不返回 | 它会阻塞等浏览器，必须后台跑 + 轮询日志抓 URL |
| 7 | 多行 `--content` 被 shell 转义搞坏 | 用 `@文件` 或 `-`（stdin） |
| 8 | 拿 bot 身份查用户日历，返回空还显示成功 | bot 查用户资源是「空成功」，别当没数据；换 `--as user` |
| 9 | Markdown 里写 `<tag>` 被当标签解析 | 左尖括号转义 `\<`；行首的 `+` `-` `#` `>` 也要转义 |
| 10 | 授权链接放久了失效 | 每轮重新 `--no-wait --json` 生成，不复用旧 device_code |

---

## 八、排障速查

```bash
lark-cli doctor                      # 配置 + 授权 + 连通性 一次性体检
lark-cli whoami                      # 当前实际生效的身份（JSON）
lark-cli auth status --json --verify # 登录态、openId、token 状态、scope
lark-cli skills list                 # 28 个技能清单
lark-cli skills read lark-doc        # 读某个域的完整用法（版本自动同步）
lark-cli update                      # 升级 CLI
lark-cli <域> --help                 # 该域所有子命令
lark-cli <命令> --help               # 参数、示例、所需 scope、风险等级
```

机器读取 JSON 时想避开升级提示噪音，命令前加：

```bash
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 lark-cli auth status --json
```

---

## 九、常用配方（可直接照抄）

> 参数名全部来自对应命令的 `--help` 实测输出；`🔬` = 本机实跑成功过。
> 占位符：`ou_xxx` 用户 open_id、`oc_xxx` 群 chat_id、`tblxxx` 表 ID、`TOKEN` 各类 token。

### 日程

```bash
# 看今天（不加参数默认今天）
lark-cli calendar +agenda

# 建日程（时间用 ISO 8601；加 --rrule 可建重复日程，RFC5545 格式）
lark-cli calendar +create --summary "评审" \
  --start "2026-09-26T14:00:00+08:00" --end "2026-09-26T15:00:00+08:00" \
  --attendee-ids "ou_xxx,ou_yyy"

# 查空闲（type=common_free 找共同空档；--min-duration 30m 限最短时长）
lark-cli calendar +freebusy --user-id "ou_xxx" \
  --start "2026-09-26T09:00:00+08:00" --end "2026-09-26T18:00:00+08:00" --type common_free

# 删日程（重复系列必须带 --apply-to all，否则只删一次）
lark-cli calendar +delete --event-id EVENT_ID --apply-to all --yes    # 🔬
```

### 消息

```bash
# 发给某人（bot 身份开箱可用；user 身份要先补 im:message.send_as_user）
lark-cli im +messages-send --user-id ou_xxx --text "内容" --as bot    # 🔬

# 发到群
lark-cli im +messages-send --chat-id oc_xxx --text "内容" --as bot

# 发富文本 / 带附件（--markdown 或 --msg-type post，附件用 --attachment file_xxx）
lark-cli im +messages-send --chat-id oc_xxx --markdown "**粗体**" --as bot

# 按名字找群，拿 chat_id
lark-cli im +chat-search --query "项目"

# 搜消息 / 读群消息
lark-cli im +messages-search --query "项目进度"
lark-cli im +chat-messages-list --chat-id oc_xxx

# 回复、编辑（编辑仅 bot 可用）
lark-cli im +messages-reply --message-id om_xxx --text "收到"
lark-cli im +messages-edit --message-id om_xxx --text "更正后的内容"
```

### 云文档

```bash
# 建文档（Markdown 保真导入；长内容必须走 @文件，别塞命令行）
lark-cli docs +create --doc-format markdown --content @./draft.md --title "文档标题"

# 读全文 / 只读大纲 / 按关键词定位
lark-cli docs +fetch --doc DOC_URL_OR_TOKEN --detail with-ids
lark-cli docs +fetch --doc DOC_URL_OR_TOKEN --scope outline
lark-cli docs +fetch --doc DOC_URL_OR_TOKEN --keyword "关键词"

# 改内容（str_replace 改文字；块级操作要配合 --block-id / --start-block-id）
lark-cli docs +update --doc DOC_URL_OR_TOKEN --command str_replace \
  --pattern "旧文本" --content "新文本"

# 搜文档 / 知识库 / 表格
lark-cli docs +search --query "关键词"

# 文末插图片或附件（4 步编排 + 失败自动回滚）
lark-cli docs +media-insert --doc DOC_URL_OR_TOKEN --file ./shot.png
```

### 电子表格

```bash
# 新建表（可直接带初始数据；--sheets 支持带类型的结构化表）
lark-cli sheets +workbook-create --title "数据表" --values '[["姓名","分数"],["alice",95]]'

# 写一片单元格（RFC 4180 CSV，起始格 A1；sheet 用 --sheet-id 或 --sheet-name 指定）
lark-cli sheets +csv-put --spreadsheet-token TOKEN --sheet-id SHEET_ID \
  --start-cell A1 --csv @./data.csv

# 其余常用：+csv-get 读成 CSV、+table-get 读回带类型表格、+workbook-export 导出 xlsx
lark-cli sheets +csv-get --help      # 参数以 --help 为准
```

### 多维表格

```bash
# 列记录（--filter-json / --sort-json 支持 @file；--limit 上限 200）
lark-cli base +record-list --base-token TOKEN --table-id tblxxx --limit 50 --format json

# 批量建记录（JSON 走 --json 或 @file）
lark-cli base +record-batch-create --base-token TOKEN --table-id tblxxx --json @./records.json

# 聚合分析用 +data-query（JSON DSL：过滤 / 排序 / 聚合）
lark-cli base +data-query --help
```

### 任务

```bash
# 建任务（--due 支持 ISO / date:YYYY-MM-DD / 相对:+2d / 毫秒时间戳）
lark-cli task +create --summary "写周报" --due "+2d" --assignee ou_xxx

# 我的任务（--complete false 只看未完成）
lark-cli task +get-my-tasks --complete false --page-all
```

### 云盘

```bash
# 上传（超 20MB 自动分片；--folder-token 指定目标文件夹）
lark-cli drive +upload --file ./报告.pdf --folder-token TOKEN

# 下载
lark-cli drive +download --file-token TOKEN --output ./报告.pdf --overwrite

# 导出云文档为本地文件（docx / pdf / xlsx / csv / markdown）
lark-cli drive +export --url DOC_URL --file-extension pdf --output-dir ./out

# 本地目录与云盘双向同步 / 单向镜像
lark-cli drive +push ./local --help
```

### 通讯录

```bash
# 按姓名解析 open_id（发消息、加日程参会人之前常用）
lark-cli contact +search-user --query "张三" --as user

# 已知 open_id 反查详情；me 表示自己
lark-cli contact +search-user --user-ids "ou_xxx,me" --as user

# 查自己的信息
lark-cli contact +get-user
```

---

## 十、扩展：这套能力还能怎么用

- **写飞书文档**：大白生成 Markdown → `docs +create` 直接落成在线文档，可带附件和图片
- **定时播报**：`calendar +agenda` + `task +get-my-tasks` 组合，官方就有现成工作流技能 `lark-workflow-standup-report`
- **数据落表**：分析结果 → `sheets +csv-put` 或 `base +record-batch-create` 入库
- **跨端搬运**：飞书云盘 ↔ 本机目录用 `drive +sync` / `+push` / `+pull`
- **兜底**：任何没封装的能力，`lark-cli schema <service.resource.method>` 查参数后 `api` 直调

想加新玩法，先 `lark_skill(action="read", name="lark-xxx")` 读对应域说明书，再动手。
