# 看图（vision）

把图片**像素**直接送进模型上下文的能力。给图片 URL / 本地路径 / data URL，模型看到的是画面本身，
不是「用文字描述图片」的中转。

## 工具：see_image

| 参数 | 说明 |
| --- | --- |
| `images` | 一张或多张图。JSON 数组、或每行一个。每项为 `http(s)` 图片 URL / 本地绝对路径 / `data:image/...;base64,...` |
| `url` | 便捷参数：只传一张图时直接用它（与 `images` 等价） |

```
see_image(url="https://example.com/a.png")
see_image(images=["https://example.com/a.png", "/tmp/b.jpg", "data:image/png;base64,iVBOR..."])
```

## 边界（都是硬约束）

| 项 | 值 | 为什么 |
| --- | --- | --- |
| 单次张数 | 4 张 | agent 侧单次工具结果最多注入 4 张（`agent._IMG_MAX_PER_RESULT`），工具先卡同一数字 |
| 单图体积 | 8MB | 更大的图基本是原图/扫描件，注入前也会被压到 1280px，先拒比先下载省事 |
| 支持格式 | PNG/JPEG/GIF/WEBP/BMP/AVIF/TIFF/ICO | 用 PIL 真解码校验，不是看扩展名 |
| 落盘 | `data/vision_images/<sha1_16>.<ext>` | 内容寻址，同一张图重复看只存一份；保留最近 60 张 |
| 下载 | 直连优先，失败且本地代理（127.0.0.1:7890）在监听时走代理重试一次 | 与 search 技能同一策略 |

## 沙箱边界（只对普通用户生效）

`images` / `url` 不在 `sandbox._PATH_KEYS` 里，所以本工具自己过闸门（取 `sandbox.current()`）：

- 本地路径：非管理员按自己的沙箱解析，越界即拒（`sandbox.resolve_path`）
- URL：非管理员访问本机/内网地址（127.0.0.0/8、10./172.16-31./192.168.、::1、*.local…）直接拒
  —— 否则它就是一台上能探内网端口、读内网图片的探针

管理员不受限：本机摄像头快照 `http://127.0.0.1:8080/snapshot.jpg` 这类要看就能看。

## 读图能力判定（不支持就调不动）

调用前先跑 `harness.vision_probe.current_can_see(settings.json)`，与 agent 侧注入用的是**同一套判定**
（优先级：角色卡显式 > 供应商显式 > 被拒实测 > 成功实测 > 模型名线索 > 默认否）。

不支持时工具直接返回 `【读图不可用】` + 判定依据，**不注入任何图片**——静默失败会让模型按文件名猜画面。
要放行：换支持读图的模型（名字含 vision/vl/4o/claude/gemini 等），或在配置页把该供应商的「读图能力」
设为「支持读图」。

## 链路

```
see_image → 下载/解码 → PIL 校验 → data/vision_images/xxx.png
          → 结果开头写 [[IMG:/abs/xxx.png]]
          → agent.py 注入通道（_append_img_messages）→ 多模态 user 消息（1280px JPEG）→ 模型
```

标记放在结果**开头**：工具结果会被截断，标记在末尾等于没有。

## 排错

- `【读图不可用】`：当前模型被判为不支持读图，看括号里的判定依据
- `不是图片（Content-Type: text/html）`：给的是网页地址，读正文用 `read_web`
- `HTTP 403/404`：图源防盗链或已失效，换直链
- 图注入但模型答不出画面：模型侧并非真视觉模型，去配置页显式勾「支持读图」后重试
