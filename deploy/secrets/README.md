# DABAI 密钥环境变量化（deploy/secrets）

把散落在 JSON 配置里的 API Key 汇聚成**一份系统环境变量文件**，并在配置变化时**实时同步**。

## 架构

```
  真源（UI 改这里）
  settings.json / codex_config.json / stt_config.json / tts_config.json
        │
        │  sync_secrets.py         ← JSON 一变就重新生成
        ▼
  派生（不手改，会被覆盖）
  /etc/dabai/secrets.env            root:wxf 0640
        │
        ├── systemd  EnvironmentFile=      → myservice 及其全部子进程
        └── login    /etc/profile.d/*.sh   → wxf 的交互式 shell
```

**单向派生**，不是双向同步 —— 双真源必然产生「改了哪边才算数」的歧义。

## 装了什么

| 路径 | 作用 | 权限 |
|---|---|---|
| `/usr/local/lib/dabai-secrets/sync_secrets.py` | 同步器本体 | `root:root 755` |
| `/usr/local/sbin/dabai-secrets` | CLI（仓库路径已烧入） | `root:root 755` |
| `/etc/dabai/secrets.env` | 派生环境变量 | `root:wxf 0640` |
| `/etc/systemd/system/dabai-secrets.path` | **实时同步**：监听 4 个 JSON | `root:root 644` |
| `/etc/systemd/system/dabai-secrets-sync.service` | 一次性同步单元 | 同上 |
| `/etc/systemd/system/dabai-secrets-sync.timer` | 10 分钟兜底（防 inotify 漏事件） | 同上 |
| `/etc/profile.d/00-dabai-secrets.sh` | 登录 shell 注入（先判 `[ -r ]`） | `root:root 644` |
| `/etc/systemd/system/myservice.service.d/30-secrets.conf` | `EnvironmentFile=-/etc/dabai/secrets.env` | `root:root 644` |
| `/etc/sudoers.d/dabai-secrets` | 只给 `sync` 免密 | `root:root 0440` |
| `/var/backups/dabai-configs/` | **源配置滚动快照**（最近 20 份，只在内容变化时新增） | `root:root 0700` |
| `/var/lib/dabai-configs-snapshot.sha256` | 快照去重用的内容摘要 | `root:root 0644` |

## 变量命名

| 变量 | 来源 |
|---|---|
| `DABAI_API_KEY` / `DABAI_BASE_URL` / `DABAI_MODEL` | `settings.json` 顶层 |
| `DABAI_IMAGES_API_KEY` / `DABAI_IMAGES_BASE_URL` | `settings.json` 图像生成 |
| `DABAI_PROV_<ID>_API_KEY` / `_BASE_URL` / `_MODEL` | `settings.json` → `llm_providers[]` |
| `DABAI_ACTIVE_API_KEY` / `_BASE_URL` / `_MODEL` | 当前激活供应商（`llm_provider_id` 指向的那个） |
| `DABAI_PROFILE_<NAME>_API_KEY` | `settings.json` → `llm_profiles`（旧格式，兼容保留） |
| `DABAI_CODEX_*` | `codex_config.json` → `llm` |
| `DABAI_STT_API_KEY` | `stt_config.json` |
| `DABAI_TTS_API_KEY` | `tts_config.json` |

`<ID>` 由供应商 `id` 派生：`prov-5cd04264` → `5CD04264`，`prov-ollama` → `OLLAMA`。

**为什么用 id 而不是名称**：名称是中文、可改；id 稳定，变量名才不会因为改个显示名就断掉。

## 手工变量共存

`EXA_API_KEY` / `TAVILY_API_KEY` / `GITHUB_TOKEN` 这类**纯环境变量型**密钥没有 JSON 真源，
可以写在 `secrets.env` 的 `MANAGED` 块**之外**，同步器永不触碰：

```bash
sudo -n /usr/local/sbin/dabai-secrets set GITHUB_TOKEN ghp_xxx
```

变量名有白名单（`DABAI_` / `EXA_` / `TAVILY_` / `GITHUB_` / `OPENAI_` / `ANTHROPIC_` / `HF_` …），
`PATH` / `LD_PRELOAD` / `IFS` 一律拒绝 —— 否则这个文件就成了注入任意环境变量的提权跳板。

## 源配置快照（防误删 / 改坏）

`settings.json` / `nodes.json` 这类文件**既含密钥又未被 git 跟踪** —— git 救不了它们。
实测事故：一条 `>` 重定向覆盖 + 一次 `rm`，`settings.json` 就彻底没了。

所以每次同步顺带做一份滚动快照：

```
/var/backups/dabai-configs/20260912-004007-8ba3a388/
    settings.json  codex_config.json  stt_config.json  tts_config.json
    cards.json     character_cards.json  nodes.json
```

设计要点：

- **只在内容或文件集合变化时新增**，保留最近 20 份（内容改回去不会重复占位）
- 目录名 = `时间戳-内容摘要前8位`，所以**同一秒内多次变化不会互相覆盖**
  （第一版只用时间戳，实测同一秒内 3 次变化只剩 1 份 —— 直接丢掉了可回退的历史）
- 快照失败**绝不抛异常**：它是兜底，不能因为它坏了而连累主同步链
- 清理只认自己建的目录名（正则匹配），不碰目录里别的东西

回退一份配置：

```bash
sudo ls -1t /var/backups/dabai-configs/            # 按时间列快照
sudo cp /var/backups/dabai-configs/<快照>/settings.json ~/dabai/settings.json
sudo -n /usr/local/sbin/dabai-secrets sync          # 让派生变量跟上
```

## 常用命令

```bash
sudo bash deploy/secrets/install-secrets.sh            # 安装 / 更新
sudo bash deploy/secrets/install-secrets.sh --check    # 只体检
sudo bash deploy/secrets/install-secrets.sh --uninstall # 卸载（保留 secrets.env）
sudo bash deploy/secrets/install-secrets.sh --purge     # 卸载并删除

sudo -n /usr/local/sbin/dabai-secrets sync      # 立即同步（免密）
/usr/local/sbin/dabai-secrets list              # 列出全部变量（脱敏）
/usr/local/sbin/dabai-secrets check             # 校验一致性，不一致返回码 1
/usr/local/sbin/dabai-secrets show DABAI_API_KEY          # 看单个（脱敏）
/usr/local/sbin/dabai-secrets show DABAI_API_KEY --reveal # 明文
/usr/local/sbin/dabai-secrets env               # 输出 export 语句，供脚本 source
```

当前 shell 立即加载（不改会话重启）：

```bash
. /etc/profile.d/00-dabai-secrets.sh
```

## 安全设计（每条都有实测依据）

1. **用 `EnvironmentFile=`，不用 `Environment=`**
   `systemctl show myservice` 会把 `Environment=` 的内容**明文列给任何用户**（实测确认）；
   `EnvironmentFile=` 只暴露路径，内容受文件权限保护。

2. **绝不写 `/etc/environment`**
   那是 `0644` 全局可读。密钥放进去等于广播。

3. **`profile.d` 钩子先判 `[ -r ]`**
   文件是 `0640 root:wxf`，非 wxf 组用户 `-r` 判false，静默跳过，拿不到任何密钥。

4. **值只接受安全字符集**
   systemd `EnvironmentFile` 与 POSIX shell 对转义的处理规则**不同**，
   唯一双方都安全的是「不含单引号/换行/反斜杠的安全字符集」。
   遇到越界字符**直接报错拒绝**，不猜、不硬转义。

5. **原子写 + 先落权限**
   `mkstemp` → 写入 → `fsync` → `chmod 0640` → `os.replace`。
   避免「先写后 chmod」的窗口期里文件是 0644 被读走。

6. **`apply_perms` 不碰目录权限**
   目标路径可由 `DABAI_SECRETS_FILE` 指向任意位置；无条件 `chmod` 目录会改坏别人的目录
   （实测：隔离测试时它试图 `chmod /tmp` 而被拒）。目录权限由安装脚本一次性设定。

7. **改 myservice 配置后不重启服务**
   它的 `MainPID` 就是大白本体（`venv/bin/python server.py`），
   重启 = 杀掉调用方自己，回复会断在半路。`daemon-reload` 足够，新变量下次启动生效。

8. **幂等**
   内容无变化时**不写盘**。否则 systemd path unit 会被自己触发成死循环。

## 故障排查

```bash
# 实时同步有没有工作
systemctl status dabai-secrets.path
journalctl -u dabai-secrets-sync.service -n 20 --no-pager

# 改了 JSON 但变量没更新
sudo -n /usr/local/sbin/dabai-secrets sync      # 手动跑一次，看报错
sudo -n /usr/local/sbin/dabai-secrets check     # 定位不一致项

# 服务里有没有拿到变量（不重启的前提下）
systemctl show myservice -p EnvironmentFiles    # 应列出 /etc/dabai/secrets.env
cat /proc/$(systemctl show myservice -p MainPID --value)/environ | tr '\0' '\n' | grep DABAI_

# 注意：已运行的进程不会自动拿到新变量，要等重启
```

## 已知边界

- **已运行的 myservice 进程不会热更新环境变量**。Linux 没有「给运行中进程加环境变量」的接口
  （只能改 `/proc/PID/environ`，但那只影响新 exec 的子进程，且长度不能超）。
  新变量在下次重启后生效；要立刻用就 `dabai-secrets env` 或 `. /etc/profile.d/00-dabai-secrets.sh`。
- **`PathModified` 依赖 inotify**。网络文件系统 / 某些编辑器的原子替换可能漏事件，
  故配了 10 分钟 timer 兜底。`sync` 幂等，频繁触发无副作用。
- **密钥明文仍存在于 JSON 真源里**（`settings.json` 等，0644）。
  要彻底消除，需把 JSON 里的 key 清空、改为只从环境变量读 —— 那是下一步，
  需要同步改 `server.py` 的 `load_config()`，风险更高，单独评估。
