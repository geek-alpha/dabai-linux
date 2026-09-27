# 拓竹 3D 打印（bambu_lab）

用户机器：**拓竹 Bambu Lab A1**（不是 A1 mini），单色 AMS lite，0.4 喷嘴。
这份文档把 2026-09 那次「切片—上传—打印」全流程踩到的坑固化成可复用的判据。
**改动代码可能被版本更新覆盖，这份经验库与 `data/bambu_lab.json` 配置是独立的。**

## 工具

| 工具 | 用途 |
| --- | --- |
| `bambu_ftp` | FTPS 操作：`probe` 探活 / `list` 列目录 / `size` 探存在 / `upload` / `download` / `delete` / `md5` 回读校验 |
| `bambu_model_check` | 读 3mf 的机型与切片信息（`printer_model` / `printer_settings_id` / 耗时 / 耗材） |
| `bambu_net_probe` | 网络单次探测（ping / ARP / 990、8883、3000、322 各一次） |

配置：`data/bambu_lab.json`（`host` / `access_code` / `user` / `port` / `cache_dir`）。
凭据**只放 data/ 下**（该目录被 .gitignore 忽略）：`skill.py` 的 `_DEFAULT_CFG` 只留空值结构，
换 IP / 换码只改 json —— 写回代码就会跟着资产包发到公开仓库。

## 三条硬约束（违反过，都付了代价）

### 1. FTPS 登录后必须 `prot_p()`

```python
f = ImplicitFTPS(context=ctx)
f.connect(HOST, 990); f.login("bblp", ACCESS_CODE)
f.prot_p()          # ← 缺这一行，之后所有数据操作报 EOFError
```

`FTP_TLS.login()` 默认发 **PROT C**（声明数据连接走明文）。拓竹要求数据连接必须 TLS（PROT P）。
它容忍控制通道，但一收到数据操作命令（NLST/LIST/RETR）就**直接掐断控制连接**；ftplib 读响应
拿到空串，抛 `EOFError`。表象酷似「数据通道坏了」，本质是加密协商被拒。

判据：控制通道 `220 BBL-P003 FTP Server` 正常、PASV 返回数据端口、该端口 TCP 通、TLS 握手成功
—— 四层全绿却仍 EOFError，就是 PROT 的问题，不是网络。

### 2. 端口超时/RST 后禁止循环重试

实测事故：8883 第一次 `TimeoutError` 后连续重连约 40 次，错误从 `TimeoutError` 变成
`ConnectionResetError`；紧接着**原本正常的 990 也开始超时**，最后整机从 ARP 表消失、ping 报
「无法访问目标主机」。服务是被重试压死的，不是自己坏的。

正确动作：**停手 → 冷却 → 换通道**。换离线通道（SD 卡 / 打印机屏幕内部存储），而不是继续修通道。
`bambu_net_probe` 每个端口只探一次，就是按这条纪律写的。

### 3. 上传后必须回读校验

- `SIZE` 显示完整**不代表内容完整**（可能只是预分配）；
- 拓竹收完最后一段**常常不回 226**，`storbinary` 会一直等到超时 —— 这是已知行为，不是失败；
- 超时后控制连接被残留响应污染，**回读要用新连接**（`_do_upload` 就是这么做的）；
- 判定标准：远端 `SIZE` 字节数 == 本地字节数 **且** 远端 md5 == 本地 md5。

## 机型判断：只认字段，不认印象

3mf 是 zip，权威字段在 `Metadata/project_settings.config`：

```
"printer_model": "Bambu Lab A1",            ← 这份是 A1 的
"printer_model": "Bambu Lab A1 mini",       ← 这份是 A1 mini 的
"printer_settings_id": "Bambu Lab A1 mini 0.4 nozzle",
```

事故：凭上一轮印象断言机型，把一版**已经切对的 A1 产出**推翻重切，白烧一轮，还让主人按错版本准备。
**A1 与 A1 mini 的切片不通用**（打印面积差一倍），拿不准就跑 `bambu_model_check`。

## 3mf 内部字段速查（实测抓的，别猜）

`Metadata/project_settings.config` 是 **JSON**：

```
"printer_model": "Bambu Lab A1",              ← 单值字段：字符串
"printer_settings_id": "Bambu Lab A1 0.4 nozzle",
"nozzle_diameter": [                          ← 数组字段：值在下一行，正则要吃跨行空白
    "0.4"
],
"filament_type": [
    "PLA"
],
```

`Metadata/slice_info.config` 是 **XML**，属性写法完全不同：

```xml
<metadata key="nozzle_diameters" value="0.4"/>
<metadata key="prediction" value="30407"/>     <!-- 秒 -->
<metadata key="weight" value="67.48"/>         <!-- 克 -->
<filament id="1" type="PLA" color="#F2754E" used_m="22.27" used_g="67.48" volume_type="Standard"/>
```

坑：`<filament>` 标签里既有 `type="PLA"` 也有 `volume_type="Standard"`。正则必须写成
`\stype="([^"]+)"`（属性前带空白），否则会先咬到 `volume_type` 的值，输出「耗材 Standard」。
拿不准就先把这个文件原样打出来看，别按猜的写正则。

## 切片与建模流水线（可复用脚本在 `scripts/`）

### 1. 模型 → 水密 STL（`scripts/model_to_watertight_stl.py`）

Blender headless：导入 glTF → 删杂物 → join → 缩到目标高度 → 置中贴底 →
**voxel remesh（0.4mm）** → manifold 检查 → 导出 STL。

- remesh 是拿到水密实体的手段：原始 glTF 带边界边，直接切会出问题
- 验收判据看输出：`MANIFOLD_CHECK nonmanifold_edges=0 boundary_edges=0`
- 单位坑：Blender 里 STL 按米制读，要 ×1000 才是 mm；摆位要把模型挪到打印板中心

### 2. STL → 3mf（`scripts/slice_with_orca.py`）

```
orca-slicer.exe --slice 0 --outputdir <目录> --export-3mf out.3mf \
  --load-settings "<machine.json>;<process.json>" --load-filaments <filament.json> \
  --allow-newer-file --ensure-on-bed --min-save --arrange 1 model.stl
```

三个坑（都踩过）：

- **profile 带 `inherits` 继承链，CLI 吃不了**：必须先递归展平成完整配置
  （合并 inherits 链、丢掉 `inherits`/`name`/`setting_id`/`filament_id`/`instantiation`），
  否则参数静默不生效。展平脚本就是 `scripts/flatten_profiles.py`（递归解析 `inherits` 链）。
- MCP 的 `slice_stl` **不暴露支撑开关**，切出来 `enable_support=0`；带 `template_3mf_path`
  会走 Bambu Studio CLI，这台机器上是坏的 → 只能手工组 process 覆盖再喂 OrcaSlicer CLI。
- 树形支撑覆盖写法：`enable_support=1` / `support_type=tree(auto)` /
  `support_threshold_angle=30` / `support_style=tree_slim`。
- 切完**必须回读 `printer_model` 核对机型**，别把 A1 mini 的片传进 A1。

### 3. 上传：见上文三条硬约束（`scripts/ftp_push.py`）

### 4. MQTT 远程起印（⚠ 未验证成功，用前先想清楚）

8883 MQTT，user `bblp` + 访问码，topic `device/<serial>/request`：

```json
{"print": {"command": "project_file", "param": "Metadata/plate_1.gcode",
           "url": "file:///sdcard/cache/<文件名>", "md5": "<plate_1.gcode 的 md5>",
           "bed_type": "textured_plate", "use_ams": false,
           "bed_leveling": true, "flow_cali": true, "timelapse": false}}
```

路径映射要记牢：FTP 看到的 `/cache/xxx.3mf`，MQTT 里必须写成
`file:///sdcard/cache/xxx.3mf`。

**状态：这条路没有成功证据** —— 试的那次打印机已被重试打掉线，指令根本没发出去。
真要用，先确认打印机能连、再单次发一条，并且**留人在机器旁边能物理急停**。

## 离线通道（网络不通也能打）

| 通道 | 做法 |
| --- | --- |
| microSD 卡 | 3mf 拷到卡根目录 → 插回机器 → 屏幕选文件打印。打印机最认自家格式 |
| 内部存储 | 屏幕「打印文件 / 内部存储」里列的是 `/cache` 下的文件，切好的 `*_plate_1.gcode` 也在 |

`/cache` 目录的典型内容（一次真实抓取）：

```
bf_final.3mf                 35,710,391 字节   ← A1 版，与本地备份逐字节一致
bf_final_plate_1.gcode                         ← 机器已解成可打印文件
1_bf_final.bbl
bf_a1m_sup02.3mf             19,929,777 字节   ← A1 mini 错版，别点
bf_a1m_sup02_plate_1.gcode
1_bf_a1m_sup02.bbl
timelapse
```

结论：**文件已在机器里时不要重复上传**，`bambu_ftp action=size` 探一下字节数就能定论。

## 排查顺序（从便宜到贵）

1. `bambu_net_probe` —— 在不在网上（ARP 无表项 = 它根本没在这张网里说过话）
2. `bambu_ftp action=probe` —— 控制通道 + 登录 + prot_p 能不能过
3. `bambu_ftp action=size <远端路径>` —— 纯控制通道，不依赖数据连接，是唯一可靠的存在性探针
4. 数据操作报 EOFError —— 先查 `prot_p()` 有没有漏，再谈网络
5. 仍不通 —— 停手，改走离线通道，别重试

## 切片参数备忘

- 机器：A1，0.4 喷嘴，单色（AMS lite 单料盘）
- 本轮模型：86 × 52 × 100.5 mm，水密校验通过
- 参考耗时：35.7MB 的 A1 版 3mf，切片预估 30407 秒 ≈ 8.4 小时（单色、含支撑）
- 切片器：OrcaSlicer / BambuStudio 命令行均可用，产出必须回读 `printer_model` 核对机型
