# 长跑引擎（longrun）—— 无人值守推进长期目标

目标：**一旦没完成就一直跑**。不是靠模型记住，是靠磁盘记住；不是靠动力，是靠循环。

## 一、设计出处（都是别人验证过的，不是自创）

| 范式 | 出处 | 这里怎么用 |
|---|---|---|
| Ralph loop | [ghuntley.com/loop](https://ghuntley.com/loop/)、[how-to-ralph-wiggum](https://github.com/ghuntley/how-to-ralph-wiggum) | 外层 `while` 无限循环，每轮**全新上下文**（`dabai_cli.py` 独立进程），状态全落磁盘 |
| 长跑 harness | [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | worker 每轮只推**一个**原子动作，收工必须留下交接物（progress + next）；上下文重置优于压缩 |
| 长应用 harness | [Anthropic: Harness design for long-running app development](https://www.anthropic.com/engineering/harness-design-long-running-apps) | 交接物必须含状态 + 下一步；先定交付物、路径交给 agent 自己走 |
| 持久化执行 | Temporal / Restate / DBOS 的 durable execution 范式 | 每轮幂等、checkpoint 原子落盘（tmp + fsync + `os.replace`）、崩溃后从最后一个已完成轮 resume |
| 空档整理记忆 | [Letta sleep-time compute](https://docs.letta.com/guides/agents/architectures/sleeptime/) | 主循环之外用日志/台账做整理（journal → 台账 next），不占主任务上下文 |
| 自愈 | systemd `Restart=always` + 心跳看门狗 | 进程死了自动起（30s）；进程卡住由 watchdog 定时器强制重启 |

## 二、一轮的生命周期

```
读台账(long_horizon.json) → 选目标（最久没跑的 active，轮询不饿死）
   → 取该目标的 next 当「本轮唯一动作」
   → 独立进程跑一轮（全新上下文，带铁律 prompt）
   → 用「台账有没有变」判定进展（next/log/progress 任一变化 = 有进展）
   → journal.jsonl 追加一条 + state.json 原子更新 + 刷心跳
   → 睡 INTERVAL 秒，回到第一步
```

判定进展的口径是**台账指纹**，不是模型自称完成——模型说「做完了」但没落盘，就等于没做。

## 三、四道刹车

1. **预算闸门**：每日调用上限 `LONGRUN_MAX_CALLS`（默认 200），用完睡到次日 23:59。
2. **防打转**：同一目标连续 3 轮无进展 → 冷却 6 小时，换别的目标推。
3. **急停**：`touch data/longrun/STOP` → 优雅退出，看门狗也不许拉起；删掉即恢复。
4. **单实例锁**：`data/longrun/runner.lock`（flock），服务/定时器/手动同时起只会有一个在跑。

## 四、运维命令

```sh
# 手动看状态（轮次/预算/心跳/冷却/最近 5 轮）
venv/bin/python tools/longrun/runner.py --status
# 人话汇报（干了什么 / 产出在哪 / 要主人做什么）——给主人看的，不是给工程师看的
venv/bin/python tools/longrun/runner.py --report
# 只跑一轮 / 只看本轮会派什么
venv/bin/python tools/longrun/runner.py --once
venv/bin/python tools/longrun/runner.py --dry-run
# 常驻（前台调试）
LONGRUN_INTERVAL=60 venv/bin/python tools/longrun/runner.py --loop

# 装成用户服务（开机自启 + 崩了自动起 + 卡死看门狗）
ln -sf /home/wxf/dabai/deploy/systemd/dabai-longrun.service ~/.config/systemd/user/
ln -sf /home/wxf/dabai/deploy/systemd/dabai-longrun-watchdog.service ~/.config/systemd/user/
ln -sf /home/wxf/dabai/deploy/systemd/dabai-longrun-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dabai-longrun.service dabai-longrun-watchdog.timer
systemctl --user status dabai-longrun.service
journalctl --user-unit=dabai-longrun -f

# 停 / 急停
systemctl --user stop dabai-longrun.service
touch data/longrun/STOP

# 彻底关停（连看门狗一起，重启也不自启）
systemctl --user disable --now dabai-longrun.service dabai-longrun-watchdog.timer
# 恢复：disable 会把 symlink 删掉，所以先补回来再 enable
ln -sf /home/wxf/dabai/deploy/systemd/dabai-longrun.service ~/.config/systemd/user/
ln -sf /home/wxf/dabai/deploy/systemd/dabai-longrun-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dabai-longrun.service dabai-longrun-watchdog.timer
```

## 五、文件

| 路径 | 作用 |
|---|---|
| `tools/longrun/runner.py` | 主循环（stdlib only） |
| `tools/longrun/watchdog.sh` | 心跳看门狗（心跳过期 → 重启服务） |
| `data/longrun/journal.jsonl` | append-only 事件流（每轮一条，崩溃不丢） |
| `data/longrun/report.md` | 给主人看的人话汇报（每轮重写，不是 append） |
| `data/longrun/state.json` | checkpoint：轮次/连续失败/冷却/预算（原子写） |
| `data/longrun/heartbeat` | 心跳时间戳（每 60s 刷） |
| `data/longrun/STOP` | 急停闸（存在即停） |
| `long_horizon.json` | 目标台账（长期事业的唯一事实源，人也能读写） |

## 六、想让引擎推一个新目标

```sh
venv/bin/python tools/long_horizon.py new <id> \
  --title "目标名" --why "为什么做" --value "解决谁的什么问题" \
  --done "什么算完成（可验收）" --next "下一个原子动作（必须具体到能直接开跑）"
```

`stage=active` 且 `next` 非空的目标才会被推；`next` 空了等于没想清下一步，引擎会跳过它——
这是刻意的：宁可空转，不许瞎转。

## 七、和发布闸门的关系（运维必读）

引擎在干活时会**直接改工作区**（agent.py、tests/ 等），所以 `deploy/gitguard/safe-push.sh` 的
「① 工作区必须干净」会拦住推送——这是对的，不该为了推而放宽闸门。
要发布时的正确顺序：

```sh
systemctl --user stop dabai-longrun.service   # 先让引擎停下来（不杀正在跑的一轮）
touch data/longrun/STOP                       # 再拉急停闸，防止被看门狗/重启拉起
# 此时再审阅引擎产出的改动 → git add/commit → safe-push.sh
rm data/longrun/STOP && systemctl --user start dabai-longrun.service   # 恢复长跑
```

## 八、汇报通道（主人 ↔ 引擎）

引擎的产出以前只落在 `journal.jsonl` 和隔离工作区里：主人不看终端就永远不知道它干了什么、
卡在哪。实测过最坏的一种——三个目标全卡在主人身上时，引擎安静空转了 27 轮，
屏幕上一点提示都没有。这条通道把「引擎想说的话」搬到主人本来就会看的地方：

| 方向 | 通道 | 看什么 |
|---|---|---|
| 引擎 → 主人 | `data/longrun/report.md` | 每轮结束自动重写：⚠ 等你决定 / 最近几轮干了什么 / 产出在哪 |
| 引擎 → 主人 | 任务中心的「长跑引擎」条目 | 标题带 `⏳ 等你决定 N 件`，steps 首行列出卡点 |
| 主人 → 引擎 | `long_horizon.py next/log/block/unblock` | 改接力棒、给证据、挂起或恢复目标 |

汇报里唯一需要主人动手的是 **⚠ 等你决定** 一段，其余都是「知道一下」；
空转 ≥3 轮会在汇报顶部直接标出来，不用主人自己去数。

```sh
venv/bin/python tools/longrun/runner.py --report   # 打印并顺手刷新 report.md
```
