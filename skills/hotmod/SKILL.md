# 服务内模块热重载（hotmod）

**一句话**：改完 `tools/` 下的代码，不用重启 myservice 就能生效。

## 为什么需要

`harness/hot_reload.py` 只盯两类文件：

| 变化 | 后果 |
| --- | --- |
| 根目录 `*.py`、`harness/*.py` | 整进程重启（掐断在途对话几秒） |
| `skills/`、`plugins/` 下的文件 | 只重载技能/插件自身 |

`tools/` 不在扫描范围。而 `server.py` 读它们是**函数内 import**：

```python
from tools.longrun.status_view import snapshot as _longrun_snap   # server.py:2065
```

`sys.modules` 命中即返回，磁盘上的新代码永远进不来。

实测（2026-09-13）：`tools/longrun/status_view.py` 08:45:23 改完，09:00 时
`/api/tasks` 仍返回旧标题「长跑引擎 · 第 9 轮 · biz-negotiate」；同一时刻本地直接
`snapshot()` 已经是「长跑引擎 · 第 10 轮进行中 · longrun-engine（已跑 1分12秒）」。

## 怎么用

```bash
# 1) 改完 tools/ 下的代码
# 2) 让服务进程丢掉旧模块缓存
curl -sX POST http://127.0.0.1:8001/api/harness/skills/hotmod/reload
# 3) 验证接口返回值已变
curl -s http://127.0.0.1:8001/api/tasks/longrun-engine
```

对话里也可以直接调工具 `hotmod_reload(dirs="tools/longrun")`。

默认清单在 `targets.json`，加目录就往 `dirs` 里加一项（相对项目根）。

## 边界

- **只逐出，不重启**：下一次函数内 import 才真正读新代码，所以刷新后要再打一次接口才算验证。
- **别对带模块级副作用的模块用**（开连接、起线程、建全局实例）：重新 import 会再来一遍。
  `harness._reload.evict_dir_modules` 的 `shared` 参数保护 `video_lib`（它和 server 共用实例状态）。
- 目录必须是项目根的**真子目录**，否则一律不动手（fail-safe）。
- 根因修法是让 `tools/` 进热重载扫描；在那之前，这是不掐断对话的替代路径。
