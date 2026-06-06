# 产业舆情 Agent V2

面向产业研究员的产业情报工作台，聚焦新能源汽车和人工智能。系统持续监控公开来源，自动完成采集、标准化、去重、分类打标、排序、核心信息提炼和简报生成。

## 技术栈

- Frontend: Vue3 + Vite
- Backend: FastAPI + SQLite + APScheduler
- Workflow: LangGraph 作为设计上的主编排框架；本地 MVP 提供顺序执行 fallback，节点边界与 LangGraph 节点一致
- Vector: Chroma 适配器边界已预留；本地演示无 Chroma 也可运行
- LLM: OpenAI-compatible Provider 边界已预留；MVP 使用规则 + 结构化校验保证可演示和可解释

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

## 核心能力

- 持续监控：工作日 09:00-18:00 每 2 小时执行 `scheduled_monitor`，高权威源每小时检查。
- 晨报：每天 08:20 补采，08:30 生成晨报。
- 手动触发：工作台支持手动补采和生成筛选简报。
- 数据源：内置 24 个公开来源配置，覆盖政府/监管、交易所、企业官网、行业媒体。
- 原文追溯：详情页“打开原文链接”来自 `raw_items.url`，不使用伪造链接。
- 评分解释：后端 Rank 节点按显式公式计算总分，详情页展示分项分数和原因。

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
- `docs/superpowers/plans`: 实现计划。
- `docs/ui-design`: UI 设计稿副本。
