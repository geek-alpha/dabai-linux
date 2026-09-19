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

推一个 `v*` tag 触发，打包与发布都在 GitHub 上跑完：

```
git push origin v1.0.0     推 v* tag（或网页上手动 dispatch）
   ▼
.github/workflows/release.yml
   ├─ build    paths.py --selftest → build_release.py --out dist
   │           （含解包回验：sha256 全对、包内无受保护路径）
   ▼
   ├─ publish  environment: release 的 required reviewers
   │           ★ 管理员必须在网页上点 Approve，作业才往下走
   ▼
   └─ gh release create        tarball + sha256 成为节点可拉取的发行版
                               发布后自检：gh release view 核对资产名
```

发一版：

```bash
# 1. 升版本号（改 VERSION，或 build_release.py --bump patch 自动升）
# 2. 提交推送
git add -A && git commit -m "..." && git push origin main
# 3. 打 tag 推送 —— tag 名必须是 v<VERSION>
git tag -a v1.0.0 -m "大白 v1.0.0" && git push origin v1.0.0
```

tag 名与清单版本不一致会被 CI 自己拦住（工作流里那条「确认 tag 与清单版本一致」），
不用手工核对。产物由 CI 现场打包，本地 `dist/` 只是开发时的临时目录（已在 .gitignore）。

**盯落地（推荐）**：push tag 后跑 `watch_release.py`，它轮询 Actions 直到 release
落地（tar.gz + sha256 资产齐全）才返回 0，失败/超时给明确退出码，不用人肉刷新网页：

```bash
# 盯到 release 落地再继续（publish 需管理员在网页点 Approve，脚本会提示等待）
python deploy/release/watch_release.py v1.0.0
```

只读观察者，不产生任何发布能力——建 release 的仍是 CI 的 publish 步骤，不违背
「发布只能有一个实现」。

**为什么最后一道闸放在 GitHub 上**：脚本闸门挡得住「推错东西」，挡不住「谁按下了推送」。
而 `environment` 的 required reviewers 是 GitHub 自己强制的 —— 这是整套体系里唯一
一个连大白自己都绕不过去的门。

启用方式（一次性，仓库网页上做）：
`Settings → Environments → New environment → 名字填 release → 勾 Required reviewers → 选自己`。
没配这个 environment 时工作流照跑，只是没人拦 —— 所以配了才算数。

**刻意不做本地直发脚本**：多一条能上传资产的本地路径，就等于在这道门旁边开了个洞，
发布这件事只能有一个实现。本地要验包，跑 `build_release.py` 看回验输出即可。

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
- **更新器副本不会自我更新**：装发行版只刷新仓库里的 `deploy/release/update.py`，
  systemd 跑的是 `/usr/local/lib/dabai-update/` 那份副本，只有 `install-update.sh` 会换它。
  代价是新能力可能静默不生效 —— v1.0.0 的包里没有 `--tag`，装了它的机器反而切不了版本。
  现在更新器会自查，在更新日志和 `--check` 里报出来，照着提示重跑一次即可。
  不让它自己换自己，是因为仓库对普通用户可写：给它这份权限等于给「能写仓库的人」任意 root。

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

`tests/test_release_update.py`，23 条，全过。测的不是「正常能跑通」，是**坏情况能不能挡住**：

```
test_floor_matches_paths                     内嵌地板与 paths.py 不漂移
test_packed_assets_match                     受管资产白名单两份不漂移
test_packed_assets_pass_floor                点名的放行、没点名的仍拦住
test_validators_agree                        两份清单校验器判定一致
test_update_writes_code_and_spares_experience 代码更新了，5 个经历文件字节未变
test_refuses_package_declaring_protected_path 投毒包 → 整包作废，且无文件被改
test_refuses_tampered_file                   包内被改 → 拒绝
test_refuses_wrong_package_hash              包哈希不符 → 拒绝
test_rollback_restores_previous_and_spares_experience
test_real_repo_package_has_no_protected_path 真仓库打包：包内无受保护路径
test_real_repo_has_no_import_gaps            包内 import 的本地模块都进了包
test_real_repo_has_no_frontend_gaps          前端引用的本地资源都进了包
test_vendor_is_packaged                      web/vendor 必须在包里（否则 3D 前端 404）
test_release_sha256_is_file_hash_and_updater_accepts_it
                                             真产物喂真更新器：.sha256 语义两端对得上
```

最后一条是补一个真实事故的：`build_release.py` 曾把 gzip 前的 tar 内容哈希写进 `.sha256`，
而 `update.py` 下载后算的是文件哈希 —— 两个不同的对象，永远不可能相等。症状极隐蔽：
打包成功、解包回验通过、测试套全绿，发布后每台机器都在第一步「包哈希不符」拒绝更新。
原因是测试夹具自己用的就是文件哈希，全绿恰恰掩盖了生产端的错 —— 两端各自自洽，
接口对不上。现在这条断言拿真产物喂真更新器，谁改回去它立刻红。

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
