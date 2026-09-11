# 统一搜索（search）

四引擎合一：anysearch（通用+垂直域+批量+URL提取，匿名可用）、tavily（LLM 优化搜索/提取/爬取/深度研究）、exa（语义搜索/答案/相似页）、web（DuckDuckGo+Bing 搜索/读网页/天气/翻墙代理）。

触发：查实时信息/新闻/文档/教程/垂直域数据（股票/论文/代码/法律等）/读网页/天气/翻墙/科学上网。

## 引擎选择

| 需求 | 工具 | 前置 |
|------|------|------|
| 通用搜索（默认首选） | `search_web` | 无（anysearch 匿名可用） |
| 垂直域搜索（股票/论文/代码/法律等） | `search_subdomains` → `search_web` | 无 |
| 并行批量搜索（最多 5 个） | `search_batch` | 无 |
| URL 全文提取 | `search_extract` | 无 |
| 语义搜索（按含义找页面） | `search_exa` | EXA_API_KEY |
| 直接问答（带引用） | `search_exa_answer` | EXA_API_KEY |
| 找相似页面 | `search_exa_similar` | EXA_API_KEY |
| LLM 优化搜索 | `search_tavily` | tvly CLI + TAVILY_API_KEY |
| 深度研究（多源带引用） | `search_tavily_research` | tvly CLI + TAVILY_API_KEY |
| 关键词搜索（DuckDuckGo+Bing） | `web_search` | 无 |
| 读网页（JS 渲染/表格/链接） | `read_web` | 无 |
| 天气 | `weather_check` | 无 |
| 翻墙/代理 | `fq_ctl` / `proxy_test` | 无 |

## 关键规则

- **垂直域搜索**：先 `search_subdomains` 拿 sub_domain 与参数格式，再 `search_web` 传 domain/sub_domain/params。required 参数必须全传，没有就传空串。
- **exa 查询**：要『描述想找的页面』而非关键词；2-3 个不同角度并行搜覆盖更全。
- **tavily**：所有命令支持 `--json` 结构化输出；URL 必须加引号。
- **查实时信息先搜再答**，不凭空编造；结果用 read_web 打开看详情，不凭标题猜。
- **翻墙/配代理**：先 proxy_test 摸清端口 → fq_ctl 起协议 → 用完恢复系统代理。
- 引擎不可用时如实说明（缺 key/未装 CLI），不假装成功。

## 配置

- anysearch：匿名可用；配 ANYSEARCH_API_KEY 提高限额（.env 或环境变量）
- exa：EXA_API_KEY（https://dashboard.exa.ai/api-keys）
- tavily：`pip install tavily-cli` + `tvly login --api-key tvly-xxx`（或 TAVILY_API_KEY 环境变量）

详细文档：原技能目录已备份至 `skills/_merged_backup_20260829/` 下对应子目录。