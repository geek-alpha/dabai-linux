# 浏览器自动化（playwright）

无头 Chromium 干活的技能。**没有常驻浏览器**：每次调用起一个 node 进程，跑完就退——这台机器的内存和会话结束后会变孤儿的 daemon 都不值得。

## 工具

| 工具 | 用途 |
|---|---|
| `pw_shot(url, ...)` | 截图存文件（默认整页），返回路径/尺寸/字节/标题 |
| `pw_text(url, ...)` | 抓页面可见文本或某元素文本 |
| `pw_run(script, ...)` | 跑任意 Playwright 脚本（逃生舱） |
| `pw_doctor()` | 环境自检：node / playwright 版本 / 内核路径 |

## 什么时候用哪个

- 要「看一眼页面长什么样」→ `pw_shot`
- 要页面内容（文章/文档/列表）→ `pw_text`，比截图再 OCR 便宜得多
- 要点击、填表、翻页、下 cookie、导 PDF → `pw_run`

## pw_run 的契约

脚本主体跑在一个已经建好的浏览器里，**这些变量直接可用**：`chromium`、`browser`、`context`、`page`（视口 1280x800，`setDefaultTimeout(30000)`）。脚本结束浏览器自动关。

```js
await page.goto('https://example.com');
console.log(await page.title());            // console.log 的输出原样返回
```

多页/新标签自己开：`const p2 = await context.newPage()`。登录态：`await context.addCookies([...])`。

## 边界（踩过的坑）

- **只能无头**：WSL 里没有显示服务器，`headless: false` 会失败；`playwright open`/`codegen` 同理用不了。
- **默认超时**：导航 45s、整体 90s（`timeout` 可调，上限 600s）。超时会连 chromium 子进程一起 SIGKILL，不留僵尸。
- **整页截图对超长页面会很大**：几千像素的页面截出来是几十 MB 的 PNG，先 `full_page: false` 看视口。
- **前端 SPA 首屏是空的**：`domcontentloaded` 之后 JS 还没渲染完，给 `wait_ms: 1500~3000`，或者用 `selector` 等元素出现。
- 依赖链：`node` + 全局 `playwright@1.63`（`npm i -g playwright`），内核在 `/root/.cache/ms-playwright/chromium-1243`。`skill.py` 自己拼 `NODE_PATH=$(npm root -g)`，不依赖外部 `pwjs` 包装器。
