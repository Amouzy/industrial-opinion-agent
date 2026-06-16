# 数据库设计文档

本文档基于 `backend/app/database.py` 的 schema、`backend/app/services/*` 的读写逻辑，以及当前 `backend/data/opinion_agent.sqlite3` 的实际结构整理。目标是把每个表、每个字段、每个索引和关键 JSON 字段都讲清楚，方便后续做结构优化、数据治理和性能评估。

## 1. 数据库概览

### 1.1 物理存储

- 数据库文件：`backend/data/opinion_agent.sqlite3`
- 引擎：SQLite
- 运行模式：WAL
- 当前表数：8
- 当前索引数：8

### 1.2 当前库规模

按当前数据库统计：

- `sources`：24 行
- `raw_items`：262 行
- `event_clusters`：236 行
- `processed_items`：262 行
- `item_tags`：3243 行
- `briefs`：2 行
- `workflow_runs`：233 行
- `scheduler_locks`：1 行

### 1.3 表关系总览

- `sources` 1 -> N `raw_items`
- `raw_items` 1 -> 0..1 `processed_items`
- `processed_items` 1 -> N `item_tags`
- `event_clusters` 1 -> N `processed_items`
- `workflow_runs` 1 -> N `processed_items`
- `processed_items` 可回溯到 `raw_items`
- `briefs` 独立保存简报结果
- `scheduler_locks` 独立保存调度租约

## 2. 数据字典说明方式

每张表分两部分：

1. 表级说明：用途、业务写入方、主键、外键、索引。
2. 字段级注释：逐字段解释含义、类型、常见取值、是否可空、来自哪段代码。

字段注释里会优先说明“业务语义”，而不是只重复字段名。

## 3. `sources` 表

### 3.1 表用途

数据源配置表，保存 24 个内置来源和用户自行新增的来源。采集节点会更新其采集状态字段，用来支撑增量抓取和来源健康度判断。

### 3.2 当前结构

主键：

- `id`

索引：

- `idx_sources_type(type)`
- `sqlite_autoindex_sources_1(url)`，唯一索引

无外键。

### 3.3 字段注释

- `id`：来源主键。内置来源使用固定 ID 1-24。
- `name`：来源名称，如“工业和信息化部”“盖世汽车”。
- `type`：来源类型。当前值主要有 `government`、`exchange`、`company`、`media`。
- `url`：来源入口地址。唯一。
- `industry_hint`：弱行业提示，如 `new_energy_vehicle,ai`。只用于辅助判断，不代表最终入库行业。
- `reliability_score`：来源可信度，0 到 1 之间。用于排序、代表项选择和综合评分。
- `enabled`：是否启用。SQLite 中存储为整数，API 返回时转换为布尔值。
- `fetch_interval_minutes`：该来源的采集间隔，默认 120 分钟。
- `last_fetched_at`：最近一次成功抓到候选的时间。
- `last_error`：最近一次采集失败或解析失败的错误信息。
- `last_checked_at`：最近一次检查时间。即使空结果也会更新。

### 3.4 当前值域

- `type` 分布：`company` 10、`exchange` 3、`government` 7、`media` 4
- `enabled` 分布：启用 21、停用 3

### 3.5 写入位置

- 启动种子：`ensure_sources()`，使用 `INSERT ... ON CONFLICT(id) DO UPDATE`
- 采集节点：`collect`
- 源管理 API：`POST /api/sources`、`PATCH /api/sources/{id}`、`DELETE /api/sources/{id}`

## 4. `raw_items` 表

### 4.1 表用途

原文事实账本表。每篇原文一条，不是向量切片，也不是聚类后的事件条目。后续分类、提炼、排序都基于这张表。

### 4.2 当前结构

主键：

- `id`

索引：

- `idx_raw_items_content_hash(content_hash)`
- `idx_raw_items_published_at(published_at)`
- `sqlite_autoindex_raw_items_1(url)`，唯一索引

外键：

- `source_id` -> `sources.id`

### 4.3 字段注释

- `id`：原文记录主键。
- `source_id`：来源 ID，对应 `sources.id`。
- `url`：原文 URL，唯一。
- `canonical_url`：当前写入逻辑通常等于 `url`，用于保留标准化链接。
- `title`：原文标题。
- `author`：作者或署名。
- `published_at`：原文发布时间，字符串形式 ISO 时间。
- `fetched_at`：采集时间，必须有值。
- `raw_content`：清洗后的正文内容。
- `content_excerpt`：正文摘录，默认截取 360 字左右。
- `content_hash`：标题与正文归一化后的 SHA-256 值，用于辅助去重和检索。
- `status`：处理状态，默认 `new`，成功进入后续流程后更新为 `processed`。
- `relevance_industry`：相关性筛选后的行业结果，值通常为 `新能源汽车` 或 `人工智能`。
- `relevance_confidence`：相关性置信度，0 到 1。
- `relevance_reason`：相关性筛选理由。
- `relevance_matched_terms_json`：相关性命中的关键词数组，JSON 文本。
- `relevance_provider`：相关性判断来源，可能是 LLM provider 或 `rules-fallback`。
- `relevance_model`：相关性判断模型名或 `local-rules`。

### 4.4 当前值域

- `status` 分布：`processed` 262
- `relevance_industry` 分布：`新能源汽车` 166、`人工智能` 96

### 4.5 写入位置

- `normalize` 阶段插入新 URL
- 已存在 URL 时只回填相关性字段
- `classify_rank_extract` 成功后更新 `status='processed'`
- 迁移/修复阶段会更新历史 URL

### 4.6 业务说明

这张表是全链路的事实底座。它的设计重点是：

- 原文只存一份，靠 `url` 保证幂等
- 相关性结果可以先落到原文上，再进入结构化处理
- 后续表都可从这里回溯到原始证据

## 5. `event_clusters` 表

### 5.1 表用途

同事件聚类表。用于把多来源同一事件归到一个事件簇下，方便详情页展示相似报道和后续代表项选择。

### 5.2 当前结构

主键：

- `id`

外键：

- 无显式外键

索引：

- 当前无额外索引

### 5.3 字段注释

- `id`：事件簇主键。
- `canonical_title`：簇代表标题。
- `representative_item_id`：代表项 `processed_items.id`，用于详情页和事件页面展示。
- `duplicate_count`：该事件簇下的条目数。
- `first_seen_at`：最早看到该事件的时间。
- `last_seen_at`：最近看到该事件的时间。
- `similarity_reason`：聚类理由，当前规则实现写作 `rule:<cluster_key>`。

### 5.4 当前值域

- 当前行数：236

### 5.5 写入位置

- `deduplicate` 阶段 `_cluster_raw_items()`
- `_update_cluster_representative()` 负责更新代表项

### 5.6 业务说明

当前聚类并不是纯语义聚类，而是规则 key + 标题归一化。这个表的后续优化空间主要在：

- 引入更稳定的相似度策略
- 增加合并/拆分控制
- 让 `similarity_reason` 更可解释

## 6. `processed_items` 表

### 6.1 表用途

结构化情报结果表。每个 `raw_item` 最终对应一条 `processed_items`，保存分类、排序、提炼和证据片段。

### 6.2 当前结构

主键：

- `id`

唯一约束：

- `raw_item_id` 唯一，保证同一原文只处理一次

外键：

- `raw_item_id` -> `raw_items.id`
- `canonical_event_id` -> `event_clusters.id`
- `workflow_run_id` -> `workflow_runs.id`

索引：

- `idx_processed_items_cluster(canonical_event_id)`
- `idx_processed_items_importance(importance_level)`
- `idx_processed_items_rank(rank_score)`
- `sqlite_autoindex_processed_items_1(raw_item_id)`，唯一索引

### 6.3 字段注释

- `id`：结构化结果主键。
- `raw_item_id`：来源原文 ID，对应 `raw_items.id`。
- `canonical_event_id`：所属事件簇 ID。
- `normalized_title`：标准化后的标题，当前通常等于原始标题。
- `summary`：结构化摘要，1-2 句。
- `key_facts_json`：关键事实 JSON 对象，包含 who/what/when/where/why/impact/evidence。
- `entities_json`：实体列表 JSON 文本，元素为对象数组。
- `impact_analysis`：产业影响分析。
- `importance_level`：重要性等级，`high`、`medium`、`low`。
- `importance_reason`：重要性原因。
- `rank_score`：综合价值分，显式公式计算结果。
- `score_breakdown_json`：排序分解 JSON，包含子分和 reasons。
- `rank_reason`：排序原因字符串，通常是多段解释拼接。
- `source_spans_json`：证据片段数组 JSON 文本。
- `llm_provider`：分类节点使用的 provider，可能是 `rules+structured-output` 或 LLM provider。
- `llm_model`：分类节点使用的模型名。
- `confidence`：分类置信度。
- `created_at`：结果生成时间。
- `workflow_run_id`：所属工作流运行 ID。
- `extraction_provider`：提炼节点 provider。
- `extraction_model`：提炼节点 model。
- `extraction_mode`：提炼模式，通常是 `llm` 或 `rules_fallback`。
- `extraction_fallback_reason`：提炼回退原因。

### 6.4 当前值域

- `importance_level` 分布：`high` 168、`medium` 85、`low` 9
- `llm_provider` 分布：
  - `rules+structured-output / local-rules` 123
  - `dashscope / qwen3.7-max` 88
  - `dashscope / qwen3.6-27b` 35
  - `dashscope / qwen-plus` 16
- `extraction_mode` 分布：
  - `rules_fallback` 112
  - `llm` 150

### 6.5 写入位置

- `classify_rank_extract` 直接插入
- API 详情页和列表页从这里取结构化展示字段

### 6.6 业务说明

这张表是“可展示的最终情报层”。它同时承担：

- 分类结果存储
- 排序结果存储
- 提炼结果存储
- 运行追踪关联

因此它也是后续做数据质量检查的核心对象。

## 7. `item_tags` 表

### 7.1 表用途

多维标签表。把一个 `processed_item` 拆成多条标签行，便于过滤和聚合。

### 7.2 当前结构

主键：

- `id`

外键：

- `processed_item_id` -> `processed_items.id`

索引：

- `idx_item_tags_dimension_value(tag_dimension, tag_value)`

### 7.3 字段注释

- `id`：标签行主键。
- `processed_item_id`：所属结构化情报 ID。
- `tag_dimension`：标签维度，如 `industry`、`industry_subtag`、`event_type`、`subject_role`、`signal_attribute`。
- `tag_value`：标签值。
- `confidence`：该条标签的置信度，通常继承分类置信度。
- `evidence_json`：支撑标签的证据 JSON 文本，通常来自分类器的 evidence 列表。

### 7.4 当前值域

- `industry` 262
- `industry_subtag` 590
- `event_type` 908
- `subject_role` 691
- `signal_attribute` 792

### 7.5 写入位置

- `ClassificationResult.to_tags()` 生成
- `classify_rank_extract` 写入

### 7.6 业务说明

这个表的好处是查询简单、过滤灵活，代价是行数会膨胀。后续如果要做更复杂的检索或可视化，可以考虑：

- 保留此表作为规范化结果
- 同时生成汇总视图或物化统计

## 8. `briefs` 表

### 8.1 表用途

简报落库表，保存晨报和手动简报的最终 Markdown 内容。

### 8.2 当前结构

主键：

- `id`

外键：

- 无显式外键

索引：

- 当前无额外索引

### 8.3 字段注释

- `id`：简报主键。
- `brief_type`：简报类型，当前值主要有 `morning`、`manual`。
- `time_range_start`：简报覆盖范围起点。
- `time_range_end`：简报覆盖范围终点。
- `title`：简报标题，如“每日晨报”或“筛选简报”。
- `content_markdown`：简报正文 Markdown。
- `item_ids_json`：本次简报使用的 `processed_items.id` 列表。
- `generated_at`：生成时间。

### 8.4 当前值域

- `brief_type`：当前库里只有 `manual` 2 条

### 8.5 写入位置

- `generate_brief()`
- `/api/briefs/generate`
- `scheduled_morning_brief`
- 工作流内部在特定触发类型下也会生成

## 9. `workflow_runs` 表

### 9.1 表用途

工作流运行记录表，用于记录一次采集/处理任务的生命周期、计数和轨迹。

### 9.2 当前结构

主键：

- `id`

索引：

- `idx_workflow_runs_started(started_at)`
- `idx_workflow_runs_one_running(status)`，部分唯一索引，仅约束 `status='running'`

外键：

- 无显式外键

### 9.3 字段注释

- `id`：运行主键。
- `trigger_type`：触发类型，如 `source_interval_scan`、`scheduled_monitor`、`manual_collect`。
- `started_at`：运行开始时间。
- `ended_at`：运行结束时间。
- `status`：运行状态，当前可见值有 `running`、`success`、`failed`、`skipped`。
- `collected_count`：采集到的候选数。
- `deduped_count`：去重后事件簇数。
- `classified_count`：完成分类打标的条目数。
- `extracted_count`：完成提炼的条目数。
- `failed_count`：过程中的失败数。
- `error_summary`：失败或跳过原因摘要。
- `node_trace_json`：节点级输入输出轨迹 JSON。

### 9.4 当前值域

- `status` 分布：`success` 164、`skipped` 64、`failed` 5
- `trigger_type` 分布：
  - `source_interval_scan` 204
  - `high_authority_monitor` 16
  - `scheduled_monitor` 9
  - `manual_collect` 4

### 9.5 写入位置

- `_insert_running_run()`
- `_insert_skipped_run()`
- `_mark_success()`
- `_mark_failed()`
- `recover_interrupted_runs()`

### 9.6 业务说明

这是最重要的运行审计表。它回答的问题是：

- 这次工作流什么时候跑的
- 由什么触发
- 跑到了哪一步
- 失败还是成功
- 每一步输入输出是什么

## 10. `scheduler_locks` 表

### 10.1 表用途

调度器租约表。用于保证同一数据库只会有一个调度器实例在工作。

### 10.2 当前结构

主键：

- `name`

外键：

- 无

索引：

- `sqlite_autoindex_scheduler_locks_1(name)`，主键索引

### 10.3 字段注释

- `name`：租约名，当前固定 `main`。
- `owner_id`：当前持有租约的进程标识。
- `acquired_at`：租约获取时间。
- `heartbeat_at`：最近续租时间。
- `expires_at`：租约过期时间。

### 10.4 当前值域

- 当前行数：1

### 10.5 写入位置

- `start_scheduler()`
- `_renew_scheduler_lease()`
- `_release_scheduler_lease()`

## 11. 索引汇总

- `idx_sources_type`：按来源类型查询来源
- `idx_raw_items_content_hash`：按内容 hash 过滤和排查重复
- `idx_raw_items_published_at`：按发布时间排序和筛选
- `idx_processed_items_cluster`：按事件簇查情报
- `idx_processed_items_importance`：按重要性筛情报
- `idx_processed_items_rank`：按评分排序
- `idx_item_tags_dimension_value`：按标签维度和值过滤
- `idx_workflow_runs_started`：按运行时间分页
- `idx_workflow_runs_one_running`：保证 running 运行唯一

## 12. 外键汇总

- `raw_items.source_id` -> `sources.id`
- `processed_items.raw_item_id` -> `raw_items.id`
- `processed_items.canonical_event_id` -> `event_clusters.id`
- `processed_items.workflow_run_id` -> `workflow_runs.id`
- `item_tags.processed_item_id` -> `processed_items.id`

## 13. JSON 字段说明

### 13.1 `raw_items.relevance_matched_terms_json`

数组，元素为命中的相关性关键词或短语。

### 13.2 `processed_items.key_facts_json`

对象，固定字段：

- `who`
- `what`
- `when`
- `where`
- `why`
- `impact`
- `evidence`

### 13.3 `processed_items.entities_json`

数组，元素通常包含：

- `text`
- `type`
- `start`
- `end`
- `confidence`

### 13.4 `processed_items.score_breakdown_json`

对象，通常包含：

- `importance_score`
- `source_score`
- `freshness_score`
- `event_type_score`
- `coverage_score`
- `key_actor_score`
- `weighted_total`
- `reasons`

### 13.5 `processed_items.source_spans_json`

数组，元素通常包含：

- `field`
- `start`
- `end`
- `quote`

### 13.6 `item_tags.evidence_json`

数组，通常是分类器的证据说明。

### 13.7 `briefs.item_ids_json`

数组，简报使用的 `processed_items.id` 列表。

### 13.8 `workflow_runs.node_trace_json`

数组，每项结构为：

```json
{
  "node": "collect",
  "input": {},
  "output": {}
}
```

## 14. 当前实现的几个结构特征

### 14.1 幂等性

- `sources.url` 唯一
- `raw_items.url` 唯一
- `processed_items.raw_item_id` 唯一

### 14.2 可追溯性

- 每条 processed item 都能回溯到 raw item
- 每次运行都能回溯到 node trace
- 每条标签都能回溯到 evidence

### 14.3 可解释性

- 分类、提炼、评分都落了独立字段
- LLM provider/model 和 fallback reason 也被保存

## 15. 当前库与源码的一个小差异

源码里的 `SCHEMA` 使用 `CREATE TABLE IF NOT EXISTS` 和迁移补列逻辑；当前实际库中可以看到：

- `sources` 已补出 `last_checked_at`
- `processed_items` 已补出 `extraction_provider`、`extraction_model`、`extraction_mode`、`extraction_fallback_reason`

所以这份文档以“当前库真实结构”为准，同时兼顾源码定义。

## 16. 建议的后续优化点

- 给 `event_clusters` 增加更明确的索引或唯一约束，防止规则聚类重复写入
- 给 `briefs` 增加按 `generated_at` 的索引，方便历史简报分页
- 如果 `item_tags` 继续增长，可考虑增加 `processed_item_id` 索引，优化详情页标签查询
- 如果要做 LLM 调优，建议新增一张专门的 `llm_calls` 表，记录 prompt 摘要、token、耗时和返回校验结果

