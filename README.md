# 产业舆情 Agent V2

面向产业研究员的产业情报工作台，聚焦新能源汽车和人工智能。系统持续监控公开来源，自动完成采集、标准化、去重、分类打标、排序、核心信息提炼和简报生成。

## 技术栈

- Frontend: Vue3 + Vite
- Backend: FastAPI + SQLite + APScheduler
- Workflow: LangGraph 作为运行时主编排框架，当前 source interval scan 通过 compiled graph 执行
- Vector: Chroma 适配器边界已预留；本地演示无 Chroma 也可运行
- LLM: OpenAI-compatible Provider 边界已预留；MVP 使用规则 + 结构化校验保证可演示和可解释

## 交付文档

- [PRD 文档](docs/PRD_产业舆情Agent.md)
- [工作流编排示意图](docs/WORKFLOW_ARCHITECTURE.md)
- [标签体系设计说明](docs/TAG_TAXONOMY.md)
- [架构与实现说明](docs/ARCHITECTURE.md)

## 本地启动

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

前端：

```powershell
cd frontend
npm install
npm run dev
```

访问地址：

- 前端工作台：http://localhost:5173
- 后端 API：http://localhost:8000
- OpenAPI 文档：http://localhost:8000/docs

## 依赖清单

后端运行环境：

- Python 3.11+（建议）
- FastAPI `0.115.6`
- Uvicorn `0.34.0`
- Pydantic `2.10.4`
- python-dotenv `1.0.1`
- APScheduler `3.10.4`
- LangGraph `0.2.60`
- langchain-core `0.3.28`
- ChromaDB `0.5.23`（向量能力预留，本地演示不依赖其实际运行）
- SQLite（Python 标准库 `sqlite3`，数据文件默认写入 `backend/data`）

前端运行环境：

- Node.js 20+（建议）
- Vue `^3.5.13`
- Vite `^6.0.6`
- `@vitejs/plugin-vue` `^5.2.1`

可选外部依赖：

- OpenAI-compatible LLM Provider：用于相关性筛选、分类打标、事实提炼的结构化 JSON 调用；未配置时系统自动使用本地规则兜底。

## 核心能力

- 采集标准化：按数据源增量采集公开文章，首采默认覆盖最近 24 小时，后续按 `last_fetched_at` 到本次扫描时间的发布窗口抓取；正文清洗后写入 `raw_items`，保留原文链接、发布时间、抓取时间、正文 hash 和展示摘要。
- 相关性筛选：进入处理链路前先判断候选是否属于新能源汽车或人工智能产业情报，过滤内部会议、泛社会新闻、导航页、栏目模板和低信息正文。
- 去重聚类：基于 URL、规范化标题、内容 hash 和规则 key 对同一事件聚类，选择更权威、更完整、更近的代表项；Chroma 语义去重仍为预留能力，当前不冒充向量召回。
- 分类打标：围绕固定 taxonomy 生成行业、行业细分、事件类型、重要性、主体角色、信号属性、关键主体等级等标签；LLM 可用时按结构化契约输出，失败或越界时回退规则结果。
- 排序评分：后端 Rank 节点按显式加权公式计算综合价值分，综合重要性、置信度、来源可靠性、时效性、多源覆盖、事件类型和关键主体因素，详情页展示分项分数和原因。
- 事实提炼：从清洗后的正文和上游分类上下文中提炼摘要、关键事实、产业影响和证据片段；所有结论要求回到原文证据，不编造主体、时间、金额、地点或因果。
- 简报生成：支持晨报和手动筛选简报，汇总高价值情报条目、重点事件和简要影响说明；每天 08:20 补采，08:30 生成晨报。
- 工作流编排：运行时使用 LangGraph `StateGraph` 编排 `collect -> relevance_screen -> process_candidates`，并在处理链路中完成 normalize、deduplicate、classify/rank/extract、brief 等节点，`workflow_runs.node_trace_json` 记录每次运行的节点输入输出。
- 持续监控：APScheduler 定时触发 source interval scan；工作日 09:00-18:00 周期监控，高权威源额外检查；工作台支持手动补采和生成筛选简报。
- 数据源管理：内置 24 个公开来源配置，覆盖政府/监管、交易所、企业官网、行业媒体，记录源级抓取窗口、错误和最近成功抓取时间。
- 原文追溯：详情页“打开原文链接”来自 `raw_items.url`，不使用伪造链接；处理结果可回溯到原始正文、来源、发布时间和节点轨迹。

## 数据来源说明

系统首批内置 24 个公开来源，按来源类型配置可靠性分、抓取间隔和行业提示：

- 政府/监管：工业和信息化部、国家发展改革委、国家能源局、国家市场监督管理总局、科学技术部、商务部、中国政府网政策文件库。
- 交易所公告：上海证券交易所公告、深圳证券交易所公告、香港交易所披露易。
- 企业官网：比亚迪新闻中心、宁德时代新闻中心、特斯拉官方新闻、蔚来新闻中心、小鹏汽车新闻中心、英伟达新闻中心、OpenAI News、Anthropic News、Google AI Blog、Microsoft AI Blog。
- 行业媒体：机器之心、量子位、盖世汽车、第一电动网。

采集策略按数据源分别执行：首次采集默认抓取最近 24 小时发布的文章，后续采集抓取 `sources.last_fetched_at` 到本次扫描时间之间发布的文章。采集结果必须保留原文 URL、发布时间、抓取时间和清洗正文；列表页、导航页、栏目模板和正文信息不足的候选会被过滤。

## 测试

```powershell
cd backend
python -m unittest discover -s tests -v
```

测试覆盖分类标签枚举约束、综合价值分公式、原始数据保留策略和同事件多源代表项选择。

## Docker

```powershell
docker compose up --build
```

## 目录

- `backend/app/services`: 采集标准化、分类、去重、排序、事实提炼、简报、工作流。
- `backend/app/api.py`: 工作台 API。
- `frontend/src/App.vue`: Vue3 单页工作台。
- `docs/ARCHITECTURE.md`: 架构与数据来源说明。
- `docs/PRD_产业舆情Agent.md`: PRD 交付文档。
- `docs/WORKFLOW_ARCHITECTURE.md`: 工作流编排示意图。
- `docs/TAG_TAXONOMY.md`: 标签体系设计说明。
- `docs/superpowers/plans`: 实现计划。
- `docs/ui-design`: UI 设计稿副本。
