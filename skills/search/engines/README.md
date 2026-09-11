# skills/search/engines —— 搜索引擎实现目录

本目录存放 `skills/search/skill.py` 通过 CLI 契约调用的引擎脚本。

## anysearch-skill-main/scripts/anysearch_cli.py（本地重建实现）

**背景**：原 `anysearch-skill-main` 引擎脚本整体丢失（本目录曾不存在），导致
`search_web` / `search_batch` / `search_subdomains` / `search_extract` 四个工具
一律返回「引擎脚本缺失」。2026-09-11 按 `skill.py` 期望的 CLI 契约重建。

**CLI 契约**（与 `skill.py` 调用方式一一对应）：

```bash
anysearch_cli.py search <query> [--domain D] [--sub_domain S] [--params P] \
                            [--zone cn|intl] [--language L] [--max_results N]
anysearch_cli.py batch_search [--queries JSON] [--query Q ...] [--max_results N]
anysearch_cli.py get_sub_domains [--domain D] [--domains D1,D2]
anysearch_cli.py extract <url>
```

**实现分层**（第一性原理：垂直域搜索 = 直连该域结构化数据源，而非换关键词搜网页）：

| 层 | 域 | 数据源 | 本机实测 |
|----|----|--------|----------|
| API 层 | `code` | GitHub repositories API（免认证） | ✅ 0.8s |
| | `academic` | OpenAlex（主）→ Crossref（兜底） | ✅ 3.7s |
| | `finance` | 新浪行情 → 腾讯行情 → 东财快讯 | ✅ 0.1s |
| | `news` | Bing News RSS | ✅ 0.4s |
| 通用层 | `general` | `web_impl` 多引擎链（Bing 主引擎） | ✅ 0.2s |
| 降级层 | 其余 13 个域 | 通用搜索 + 权威站点限定 | ✅ |

**实测不可用、故未采用**：DuckDuckGo（连接超时，被墙）、arXiv API（连接超时）、
Semantic Scholar（429 限流）、Wikipedia API（连接超时）、GitHub code search（401 需认证）、
百度/搜狗（反爬验证码）。

## 环境变量

- `DABAI_SEARCH_ENGINES`：覆盖引擎根目录（默认即本目录）
- `ANYSEARCH_API_KEY`：保留位（当前本地实现不需要）
