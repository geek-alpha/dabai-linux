# 统一搜索（search）

四引擎合一：anysearch（通用+垂直域+批量+URL提取，匿名可用）、tavily（LLM 优化搜索/提取/爬取/深度研究）、exa（语义搜索/答案/相似页）、web（DuckDuckGo+Bing 搜索/读网页/天气/翻墙代理）；外加免 key 的科研引擎 academic_impl（arXiv/OpenAlex/PubMed/Crossref 四源融合 + PDF 全文 + BibTeX），以及极客信息源 tech_impl（Hacker News / StackExchange / GitHub / Wikipedia / HuggingFace / 论文库五源融合 + PyPI/npm/crates/DockerHub/OpenRouter 注册表）。

触发：查实时信息/新闻/文档/教程/垂直域数据（股票/论文/代码/法律等）/读网页/天气/翻墙/科学上网。

## 引擎选择

| 需求 | 工具 | 前置 |
|------|------|------|
| 通用搜索（默认首选） | `search_web` | 无（anysearch 匿名可用） |
| 垂直域搜索（股票/论文/代码/法律等） | `search_subdomains` → `search_web` | 无 |
| 并行批量搜索（最多 5 个） | `search_batch` | 无 |
| URL 全文提取 | `search_extract` | 无 |
| 学术论文检索（多源融合） | `search_paper` | 无（arXiv/OpenAlex/Crossref/PubMed 全免 key） |
| 读论文全文（PDF 正文） | `read_paper` | 无（arXiv 直取；DOI 走 OpenAlex+Unpaywall） |
| 取 BibTeX 引用 | `paper_cite` | 无（Crossref content negotiation / arXiv Atom） |
| 技术选型/踩坑/有没有人做过 | `search_tech` | 无（HN/StackExchange/GitHub/Wikipedia/论文库全免 key；tavily 源需 TAVILY_API_KEY） |
| 包/镜像/模型的真实版本与价格 | `registry_info` | 无（注册表 API 全免 key；GitHub 搜索才需 GITHUB_TOKEN） |
| 语义搜索（按含义找页面） | `search_exa` | EXA_API_KEY（原生 HTTP，未配置时返回配置指引） |
| 直接问答（带引用） | `search_exa_answer` | EXA_API_KEY |
| 找相似页面 | `search_exa_similar` | EXA_API_KEY |
| LLM 优化搜索 | `search_tavily` | TAVILY_API_KEY（原生 HTTP，不需要 tvly CLI） |
| 抽取 URL 正文（JS 页） | `search_tavily_extract` | TAVILY_API_KEY |
| 深度研究（多源带引用） | `search_tavily_research` | TAVILY_API_KEY |
| 关键词搜索（DuckDuckGo+Bing） | `web_search` | 无 |
| 读网页（JS 渲染/表格/链接） | `read_web` | 无 |
| 天气 | `weather_check` | 无 |
| 翻墙/代理 | `fq_ctl` / `proxy_test` | 无 |

## 关键规则

- **垂直域搜索**：先 `search_subdomains` 拿 sub_domain 与参数格式，再 `search_web` 传 domain/sub_domain/params。required 参数必须全传，没有就传空串。
- **exa 查询**：要『描述想找的页面』而非关键词；2-3 个不同角度并行搜覆盖更全。
- **tavily**：`depth` 只认 basic/advanced；`time_range` 认 day/week/month/year；`search_tavily_research` 是异步任务，内部自动轮询，超时会返回 request_id——传 `request_id` 复查结果，不要重跑一遍。
- **查实时信息先搜再答**，不凭空编造；结果用 read_web 打开看详情，不凭标题猜。
- **科研检索**：`search_paper` 一次并行四源并做 RRF 融合去重（同一篇的 arXiv 版与出版社版会并成一条，DOI/被引/PDF 字段互补）。追最新进展用 `sort=latest` 配 `days=N`——该模式有时间排序 + 相关性闸门，未来日期（期刊卷期/online-first）自动沉底。拿到 arXiv ID/DOI 后直接 `read_paper` 读正文、`paper_cite` 出引用。
- **PDF 只认文本型**：扫描版/图片型抽不出字，工具会明确提示需 OCR；单次读页上限 60，长文用 `pages` 分段读。
- **翻墙/配代理**：先 proxy_test 摸清端口 → fq_ctl 起协议 → 用完恢复系统代理。
- **缺 key 时如实说明并给出配置指引**，不假装成功、不反复重试。
- **极客检索**：`search_tech` 每条结果带权威度标记——[A] 一手/官方（注册表、论文原文、官方文档）、[B] 同行评审或高信誉问答、[C] 社区讨论。**C 当线索用，别当结论**；`✓N源` 表示同一内容被多个源引用（交叉印证）。默认 auto = hn+so+gh+wiki+academic+tavily：tavily 补的是官方文档/厂商博客这类灰色文献——前五个免 key 源拿不到（实测搜「mem0」有论文和 HN 讨论，拿不到 docs.mem0.ai）。tavily 源的 tier 逐条按域名判（docs.* 与官方域 → A），短 query（≤3 词）会自动多查一次 `{query} documentation` 并限定到第一轮识别出的官方域，**2 credits/次**。`devto` 只能按 tag 匹配（tag 命中 ≠ 内容相关），要教程文章时显式 `sources="devto"`。
- **官方文档有确定来源**：`official` 源 = GitHub 仓库 `homepage` 字段（实测 mem0ai/mem0 → mem0.ai、langchain-ai/langchain → docs.langchain.com）→ 抓该域的 `llms.txt`（文档站通用约定，实测 docs.mem0.ai 44KB / docs.langchain.com 24KB / docs.anthropic.com 69KB 都是 200）→ 按分区与入口页排序取前 N 条。比 tavily 的限定轮便宜（1 次 GET vs 1 credit）且确定：拿到的是文档站自己的目录，不是「搜索引擎觉得像文档的页面」。站点没提供 llms.txt 时会明确报错（docs.vllm.ai 实测 404），不静默返回空。
- **实体词闸门**：query 里含数字的词（mem0/gpt-4/k8s/http2）会被当实体词，命中不了它们的条目在融合前就被滤掉——RRF 只看排名不看相关度，不挡的话 Wikipedia/论文库的词面撞车会占满前排。滤掉多少条会写在结果末尾，不静默丢弃；query 不含数字词时不做闸门。
- **版本号不凭记忆**：写依赖、写 Dockerfile、选模型之前先 `registry_info` 查真实版本与发布时间。GitHub 搜索必须认证（未认证实测 403），缺 GITHUB_TOKEN 时该源会明确报错，不会静默返回空。
- **Wikipedia 的 date 是最后修订时间**，不是内容发布时间；技术类条目常被频繁微修，别把「10天前」当成「内容很新」。

## 去哪查（信息源路由表）

| 问题类型 | 去哪 | 为什么是它 |
|----------|------|-----------|
| 这个库/API 怎么用、有什么坑 | `search_tech`（sources=so） | StackExchange 有可投票的答案与「已解决」标记 |
| 真实工程评价、有没有人踩过雷 | `search_tech`（sources=hn） | HN 讨论里有维护者与一线工程师的反对意见 |
| 有没有人做过 / 现成轮子 | `search_tech`（sources=gh） | 仓库 star / pushed_at / license 是一手信号 |
| 前沿方法、SOTA | `search_paper` → `read_paper` | 论文原文 + 被引数，不是二手转述 |
| 概念定义、术语边界 | `search_tech`（sources=wiki） | 稳定定义，先对齐术语再动手 |
| 包/库版本、发布时间、许可证 | `registry_info` | 注册表 API 一手元数据，杜绝幻觉版本号 |
| 容器镜像的 tag 与拉取量 | `registry_info`（kind=docker） | DockerHub 官方 API |
| 模型选型（上下文/价格） | `registry_info`（kind=llm） | OpenRouter 聚合的一手价格 |
| 模型/数据集下载量与热度 | `registry_info`（kind=model/dataset） | HuggingFace 官方 API |
| 时事新闻、站点综述 | `search_web` / `search_tavily` | 通用网页索引覆盖新闻与官网 |
| 官方文档正文 | `search_tech`（auto 含 official + tavily 源）→ `read_web` / `search_tavily_extract` | official 从 llms.txt 拿文档站自己的目录（确定），tavily 补厂商博客/榜单（概率），再直读原文避免摘要失真 |

## 配置

- anysearch：匿名可用；配 ANYSEARCH_API_KEY 提高限额（.env 或环境变量）
- exa：EXA_API_KEY（https://dashboard.exa.ai/api-keys）
- tavily：TAVILY_API_KEY（https://app.tavily.com/home）
- 两个引擎都是原生 HTTP（exa_impl.py / tavily_impl.py），不需要 exa-skills 脚本、不需要 tvly CLI。
- key 落点：`/etc/dabai/secrets.env` 的**手工变量区**（deploy/secrets 的同步器只重写 MANAGED 块，块外永不触碰），或临时 `export`。两个 impl 按「环境变量 → secrets.env」顺序自动查找。

## 本机引擎可用性（2026-09-26 实测，动手前先看这张表）

| 引擎/工具 | 状态 | 说明 |
|-----------|------|------|
| anysearch（`search_web`/`search_batch`/`search_extract`/`search_subdomains`） | ✅ 可用 | 匿名；academic 域实际走 OpenAlex |
| `web_search`/`read_web`/`weather_check`/`fq_ctl`/`proxy_test` | ✅ 可用 | 直连 + 本地代理 127.0.0.1:7890 兜底 |
| `search_paper`/`read_paper`/`paper_cite` | ✅ 可用 | 无需任何 key；arXiv/OpenAlex/PubMed/Crossref 实测通 |
| exa（`search_exa`/`_answer`/`_similar`） | ✅ 可用 | EXA_API_KEY 已落 `/etc/dabai/secrets.env`；三个工具真检索通过（search 返回 arXiv 论文并报花费，answer 带 8 条引用，similar 相似度排序正常） |
| tavily（`search_tavily`/`_extract`/`_research`） | ✅ 可用 | TAVILY_API_KEY 同上；search 带直接答案、extract 抽 arXiv 页 1802 字符、research 多源报告（request_id 复查通） |
| 极客信息源（`search_tech`） | ✅ 可用 | HN Algolia / StackExchange / GitHub（GITHUB_TOKEN 已配）/ Wikipedia / arXiv+OpenAlex+Crossref+PubMed / tavily 实测全通；实测搜「mem0 memory layer」同时召回 HN 讨论 + arXiv 论文 + docs.mem0.ai 官方文档 |
| 官方文档源（`search_tech` 的 official） | ✅ 可用 | GitHub 仓库 homepage → llms.txt：docs.mem0.ai 44KB / docs.langchain.com 24KB / docs.anthropic.com 69KB / docs.ollama.com 4.6KB 实测 200；docs.vllm.ai 404（站点未提供，会明确报错） |
| 注册表（`registry_info`） | ✅ 可用 | PyPI / npm / crates.io / DockerHub / HuggingFace / OpenRouter 实测全通 |
| 已失效的源（别试） | ❌ 不可用 | Reddit 403、Lobsters 反爬挑战页、Semantic Scholar 429 限流、paperswithcode.com 已关停（API 返回 HTML） |

无 key 时工具**直接返回配置指引**（含申请地址与落盘位置），不要反复重试；语义检索替代方案：`search_paper`（学术）/ `search_web`+`read_web`（通用）。

成本量级（实测）：tavily 的 basic 深度 = 1 credit/次，`search_tech` 的 tavily 源短 query 走两轮 = 2 credits；免费额度 1000 credits/月，日常检索够用。exa 工具会自报 costDollars，看返回值别猜。`search_tavily_research` 最贵，别拿它做普通查询。

**tavily 结果非确定性**：同一 query 两次调用返回的页面集合不同（实测搜「mem0 memory layer」两次结果几乎不重叠）。所以验证 tavily 相关的改动要看「是否召回目标类型」（官方文档/厂商博客），别指望某条 URL 稳定出现；`search_tech` 的验收也一样。

`search_tavily_research` 的两个时间坑：① 默认 `timeout=180` 会超出调用层的 120s 上限被杀，**长研究请传 `timeout<=60`**；② 若仍返回「超时」并带回 `request_id`，**不要重建任务**（重复扣额度），用 `request_id=...` 复查同一任务。

详细文档：原技能目录已备份至 `skills/_merged_backup_20260829/` 下对应子目录。
