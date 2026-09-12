# deploy/gitguard —— 提交前密钥防线

## 解决什么问题

这个仓库曾经有 11 个提交带着明文 API key（`settings.json` /
`codex_config.json` / `stt_config.json`），`server.py` 里还硬编码了一个。
`.gitignore` 只能拦住「还没被跟踪的文件」—— 对**已经在历史里的**毫无作用，
而 `git push` 推的是**全部历史**，不是当前快照。

所以防线分两层：

| 层 | 作用 | 位置 |
|---|---|---|
| **止血** | 停止跟踪含密钥文件、清空历史中的密钥 | `.gitignore` + 一次性 `git filter-repo` |
| **防复发** | 每次提交前扫暂存区，命中就拒绝 | `pre-commit` 钩子 |

## 三条判据

`secretscan.py` 的判断逻辑，精度是刻意调过的 —— **误报多了钩子就会被
`--no-verify` 绕过，等于没有**。

1. **值形态**：`sk-` / `sk-ant-` / `ghp_` / `AKIA` / `tvly-` / `AIza` /
   `xoxb-` / `hf_` / `glpat-` / JWT / PEM 私钥头
2. **字段名 + 长度**：字段名按下划线/驼峰分词后含密钥词根
   （`api_key`、`access_token`、`password`…），且值 ≥ 20 字符且像密钥
3. **高熵长串**：值 ≥ 32 字符、字符集单一、信息熵 ≥ 3.3 位/字符
   —— 兜住「没有固定前缀的自建 key」，例如 32 位 hex

### 刻意不报的（否则钩子会被绕过）

```
max_tokens: 8192                        # 分词成 [max, tokens] → 含 NON_SECRET 词根
state_key = "2|2|20260909"              # 值含 | → 不是密钥形态
TOKEN_STATS_KEY = "xxx_v1"              # 含 stats → 状态常量而非密钥
sha = "14206abc..."                     # 名字不含密钥词根
"api_base": "https://..."               # URL 显式排除（字符集和熵都像密钥）
TOKEN_REMOTE_URL = "/api/v1/sys/token"  # 路由常量：以 / 开头 → 判为路径
PASSWORD_LOGIN_PREFIX = "/api/v1/..."   # 同上，名字带 password 也不报
serviceCode = "FAST_DELIVERY_CODE"      # 不含密钥词根（service 不算）
"api": "mtop.gaia.queryUserInfoById"    # 不含密钥词根（api 不算）
for tag in ('-----BEGIN' + ' RSA PRIVATE KEY-----', …)   # PEM 头没独占一行
api_key = "YOUR_KEY_HERE"               # 占位符
prompt = "很长的中文提示词…"              # 字符集不匹配
```

后四条是 2026-09 在 `xianyu-auto-reply` 上实测踩出来的：那一个仓库原本
15 条告警里有 10 条是这种误报，剩下 5 条才是真的硬编码密钥。

回归用例：

```bash
python3 deploy/gitguard/rules_regress.py   # 18 条，退出码 0 = 全过
```

`install.sh --check` 会自动带上这一步 —— **改判据必须保证它仍然 18/18**，
放宽误报时顺手把真密钥一起放过就麻烦了。

## 用法

```bash
# 安装（钩子 + .gitignore 规则 + 脱敏模板）
bash deploy/gitguard/install.sh

# 体检（只读，退出码 1 = 有问题）
bash deploy/gitguard/install.sh --check
bash deploy/gitguard/install.sh --check --deep   # 加上全历史扫描（慢）

# 卸载
bash deploy/gitguard/install.sh --uninstall

# 手动扫描
python3 deploy/gitguard/secretscan.py --staged        # 暂存区
python3 deploy/gitguard/secretscan.py --tree          # 工作区
python3 deploy/gitguard/secretscan.py --history       # 历史（仅可达对象 = 会被 push 的）
python3 deploy/gitguard/secretscan.py --history-all   # 偏执模式：含悬空对象
python3 deploy/gitguard/secretscan.py --files a.py b.json
```

### `--history` 与 `--history-all` 的区别

`--history` 只扫**可达对象**（`git rev-list --all --objects`）—— 这正是
`git push` 会传的东西。

`--history-all` 连**悬空对象**一起扫：比如你 `git add` 了一个含密钥的文件、
发现不对又 `git rm --cached`，那个 blob 会留在对象库里，**不会被 push**，
但 `--history-all` 能看见它。两者都查一遍最稳妥；查完用
`git prune --expire=now && git gc --prune=now` 清掉悬空对象。

（早先版本只扫 `--batch-all-objects`，结果长期挂着两条「悬空 blob 里的假密钥」
噪音。**扫描结果里常驻噪音 = 人会开始忽略扫描结果 = 钩子名存实亡**，
所以默认口径改成了可达对象。）

## 误报了怎么办

按优先级选：

1. **行尾加注释**：`api_key = "假密钥"  # allowlist secret`
2. **写进 `.gitguard-allow`**（正则，每行一条，`#` 开头为注释）
3. 确属判据缺陷 → 改 `secretscan.py`，**同时补一条回归用例**

**不要**养成 `git commit --no-verify` 的习惯。

## 与 deploy/secrets 的分工

```
配置真源（JSON，本地磁盘，不进 git）
    │  sync_secrets.py
    ▼
/etc/dabai/secrets.env  ← 运行时密钥的唯一出口
    ├── systemd EnvironmentFile  → myservice 及子进程
    └── /etc/profile.d/*.sh      → 交互式 shell
```

- **gitguard** 管「别把密钥提交出去」
- **secrets** 管「密钥怎么送达运行时」

## 已知边界（不防什么）

- **不扫二进制/大文件**（> 4MB 跳过）—— 密钥不会藏在 PNG 里，但会藏在
  `.env` 打包成的 zip 里，这类靠 `.gitignore` 兜
- **不防已经 push 出去的历史** —— 那时必须轮换密钥，清史没用
- **不防 `--no-verify`** —— 工具管不住决心
- **钩子只在本地** —— clone 的人需要自己跑一次 `install.sh`
  （或设 `git config core.hooksPath`，脚本已兼容该配置）

## 清史备忘（已执行）

```bash
# 1) 用字面量替换表把历史里的密钥值换成 ***REMOVED***
python3 git-filter-repo --force --replace-text replacements.txt
# 2) 把含密钥的配置文件整个从历史移除
python3 git-filter-repo --force --invert-paths \
  --path settings.json --path codex_config.json --path stt_config.json \
  --path tts_config.json --path cards.json --path character_cards.json
# 3) 收尾
git reflog expire --expire=now --all && git gc --prune=now
```

⚠️ 清史会**改写所有提交 hash**。执行前必须先 `git bundle create` 全量备份，
且确认没有别人基于旧 hash 在干活。
