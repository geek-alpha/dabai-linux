# deploy/release —— 发行版与自动更新

## 一句话原理

**仓库是基因组，实例的磁盘是经历。** 更新只动基因组，永远不碰经历。

三台大白出生时都是同一份代码，之后各自长出不同的信条、长期事业、基因统计、记忆和任务。
自动更新能把代码换成新的，但换不掉任何一台的成长 —— 这是设计出来的结构性保证，
不是「小心一点」。

## 为什么这么分

仓库里原本同时住着两样东西：

| | 例子 | 跨机器 | 覆盖后果 |
|---|---|---|---|
| **基因组** | `agent.py`、`harness/`、`skills/`、`*.example.json` | 三台一样 | 无所谓，本来就该同步 |
| **经历** | `conviction.json`、`long_horizon.json`、`gene_stats.json`、`data/**`、`skills/*/data/**` | 每台不同 | **不可逆**：那个实例不再是它自己 |

git 是基因组的分发通道。经历住在 git 里，就永远处在「更新写入面」上 —— 一次硬更新
就能把某台机器的记忆覆盖成别人的。所以第一步是把经历**移出跟踪面**
（`.gitignore` + `git rm --cached`），此后它连被误伤的资格都没有。

## 三分类清单：`paths.py`

全仓唯一权威，任何写入判定都必须过它。

```
classify("agent.py")               -> code        更新会覆盖
classify("conviction.json")        -> experience  永不写入
classify("venv/bin/python")        -> local       永不写入
```

判定顺序 `LOCAL > EXPERIENCE > CODE`，前两档一票否决。`python paths.py --selftest`
有 24 条断言，包括「祖先目录命中即命中」—— `data/**` 要能拦住未来才新增的任意深度子路径，
保护不能依赖「当前有哪些文件」。

`FLOOR_GLOBS` 是其中的最小冻结子集，**被复制进 `update.py` 体内**。两份清单是有意重复的：

- `MANIFEST` 说「发布方认为该写什么」——可能被改坏、可能被投毒；
- 地板说「更新器自己认为绝不能写什么」——冻结在更新器里，发布方碰不到。

两者取交集。任一方出问题，都到不了经历文件。测试 `test_floor_matches_paths` 断言两份不漂移。

## 发布路径

两道门，一道在脚本里，一道在 GitHub 上。

```
build_release.py           打包：只取「跟踪 ∩ 代码」的文件
   │                       可复现构建（时间戳钉在提交时间，同 commit 两次打包字节一致）
   │                       产出 dabai-<ver>.tar.gz + .sha256 + MANIFEST.json
   ▼
gitguard/safe-push.sh      八道既有闸门：干净树 → 三扫密钥 → git grep 独立复核
   │                       → 硬雷文件未跟踪 → token → 建仓 → 推 → 推后自检
   ▼
GitHub Actions             environment: release 的 required reviewers
   │                       ★ 管理员必须在网页上点 Approve，作业才往下走
   ▼
GitHub Release             tarball + sha256 成为节点可拉取的发行版
```

**为什么最后一道闸放在 GitHub 上**：脚本闸门挡得住「推错东西」，挡不住「谁按下了推送」。
而 `environment` 的 required reviewers 是 GitHub 自己强制的 —— 这是整套体系里唯一
一个连大白自己都绕不过去的门。

启用方式（一次性，仓库网页上做）：
`Settings → Environments → New environment → 名字填 release → 勾 Required reviewers → 选自己`。
没配这个 environment 时工作流照跑，只是没人拦 —— 所以配了才算数。

## 更新路径

`update.py`，九步。任何一步不过，整包作废，不做部分更新。

| 步 | 做什么 | 不过怎么办 |
|---|---|---|
| ① | 包哈希校验（对 `.sha256`） | 拒绝 |
| ② | 解包 + 清单结构校验 + 逐文件 sha256 | 拒绝 |
| ③ | **用自带地板复核清单**，出现受保护路径 | 整包作废 |
| ④ | 版本判定（只升不降，除非 `--force`） | 跳过 |
| ⑤ | 生成写入计划（每条都过地板与越界检查） | 整包拒绝 |
| ⑥ | **停机** + 给全部受保护文件拍哈希快照 | — |
| ⑦ | 逐文件原子替换（`os.replace`），旧版留备份 | 出错则起服务退出 |
| ⑧ | **经历复核**：快照逐个比对 | 有差异 → 立即回滚 |
| ⑨ | 起服务 + 体检（systemd 状态 + 端口 + HTTP） | 体检失败 → 自动回滚 |

第 ⑥⑧ 步的顺序是有讲究的：先停机再拍快照，窗口里没有别的进程在写盘，所以「经历哈希没变」
是干净的证据，而不是被服务自身写入干扰过的噪声。

其它保护：

- **对话轮保护**：`data/turn_checkpoints/` 里有 120 秒内活动 → 跳过本次。
  更新可以等五分钟，用户的话等不了。
- **更新器跑在仓库之外**（`/usr/local/lib/dabai-update/`）：仓库正是被更新的对象，
  用它自己的代码更新它自己，会在替换到一半时把正在执行的脚本换掉。
- **窄口径免密**：更新器按普通用户跑（否则写出来的文件属主全变 root），
  只在停/起/查这一个服务上升权，范围钉死到具体命令，不给 systemctl 通配。
- **状态与备份在仓库之外**（`/var/lib/dabai-update/`）：更新器自己的痕迹不落进被更新的目录。

## 出生与成长

新实例出生时，经历文件**不存在**。已逐个验证加载器会给出空结构：

| 文件 | 读取端 | 缺文件时 |
|---|---|---|
| `long_horizon.json` | `tools/long_horizon.py:37` | `{}` + 空列表 |
| `conviction.json` | `tools/conviction.py:43` | `{}` + 空列表 |
| `long_horizon.json`（注入） | `agent.py:2248` | 返回空串 |
| `conviction.json`（注入） | `agent.py:2299` | 返回空串 |

所以：**出生时一样（都是空），之后长成什么样，只由它自己的经历决定。**

## 三机铺开

每台机器各跑一次：

```bash
sudo bash deploy/release/install-update.sh
```

脚本会装更新器副本、写配置、写窄口径免密（`visudo` 预校验，写坏就撤回）、
启用定时器（每天 04:30 前后随机错开），然后跑接线自检，包括**验证免密范围没有越界**
（试着重启一个不存在的服务，能成功就说明范围过宽，直接报错退出）。

前置条件：`GITHUB_TOKEN` 得有着落（仓库是私有的，拉发行版必须带）。已有的
`dabai-secrets` 通道就是干这个的。

## 已验证的证据

`tests/test_release_update.py`，12 条，全过。测的不是「正常能跑通」，是**坏情况能不能挡住**：

```
test_floor_matches_paths                     内嵌地板与 paths.py 不漂移
test_validators_agree                        两份清单校验器判定一致
test_hidden_file_keeps_leading_dot           .gitattributes 不会被吃成 gitattributes
test_forbidden_covers_ancestors_and_future_paths
test_update_writes_code_and_spares_experience 代码更新了，5 个经历文件字节未变
test_dry_run_writes_nothing
test_refuses_package_declaring_protected_path 投毒包 → 整包作废，且无文件被改
test_refuses_tampered_file                    包内被改 → 拒绝
test_refuses_wrong_package_hash               包哈希不符 → 拒绝
test_refuses_downgrade_without_force
test_rollback_restores_previous_and_spares_experience
test_real_repo_package_has_no_protected_path  真仓库打包：包内无受保护路径
```

打包器自身还有解包回验：解出来逐个核对 sha256，并断言包内不存在任何受保护路径。

## 已知限制（诚实版）

1. **本地闸门挡不住我。** 我在 `wxf` 用户下能读 token、能跑命令，所以脚本层的
   「管理员批准」对我不是硬约束。真正硬的那道是 GitHub 的 `environment`
   required reviewers —— 所以那个 environment 必须配，否则整套只有自觉。
2. **体检只证明服务活着**，不证明功能对。端口通、HTTP 有回应就算过。
   真要验功能得跑 `tests/`，那不在自动更新能承受的时间预算里。
3. **回滚只覆盖代码**。经历本来就没被写，所以不需要回滚；但如果新代码自己改了经历文件
   （业务逻辑所致，不是更新器所致），那是另一个问题，不在本机制的防护范围内。
4. **`--prune` 默认关闭**。新包里已不存在的旧代码文件默认保留并报告，要删得显式加参数。
   保守选择：留着无害，删错了要命。
