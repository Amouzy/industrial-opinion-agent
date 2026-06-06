<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  feedInspectionStatus as getFeedInspectionStatus,
  isFeedAutoInspecting
} from "./feedInspection.js";
import { formatDisplayTime, itemDisplayTimestamp } from "./timeDisplay.js";

const API_BASE = import.meta.env.VITE_API_BASE || "";

const summary = ref(null);
const taxonomy = ref(null);
const llmStatus = ref(null);
const items = ref([]);
const selectedItem = ref(null);
const selectedRun = ref(null);
const briefs = ref([]);
const runs = ref([]);
const sources = ref([]);
const feedListRef = ref(null);
const activePage = ref("intelligence");
const loading = ref(false);
const manualRunning = ref(false);
const moreFilters = ref(true);
const isFeedHovered = ref(false);
const isFeedManuallyPaused = ref(false);
const isReturningToTop = ref(false);
const expandedTraceNode = ref("");
const errorMessage = ref("");
const filters = ref({
  keyword: "",
  industry: "",
  industry_subtag: "",
  event_type: "",
  importance: "",
  signal_attribute: "",
  subject_role: "",
  source_type: "",
  time_range: "7d",
  limit: "50",
  sort: "rank"
});

const navItems = [
  ["intelligence", "情报总览"],
  ["briefs", "简报中心"],
  ["runs", "采集运行"],
  ["sources", "数据源配置"],
  ["taxonomy", "标签体系"],
  ["ranking", "评分规则"]
];

const timeRangeOptions = [
  ["24h", "24小时"],
  ["72h", "72小时"],
  ["7d", "7天"],
  ["30d", "30天"],
  ["all", "全部"]
];

const limitOptions = ["20", "50", "100", "200"];

const sourceTypeLabels = computed(() => taxonomy.value?.source_types || {});
const visibleSubtags = computed(() => {
  const subtags = taxonomy.value?.industry_subtags || {};
  if (filters.value.industry) {
    return subtags[filters.value.industry] || [];
  }
  return Object.values(subtags).flat();
});
const selectedOrFirstItem = computed(() => selectedItem.value || items.value[0] || null);
const selectedOrFirstRun = computed(() => selectedRun.value || runs.value[0] || null);
const selectedTraceNodes = computed(() => selectedOrFirstItem.value?.item_processing_trace || []);
const canAutoInspectFeed = computed(() => items.value.length > 5);
const isFeedInspectionRunning = computed(() =>
  isFeedAutoInspecting({
    canInspect: canAutoInspectFeed.value,
    isHovered: isFeedHovered.value,
    isManuallyPaused: isFeedManuallyPaused.value
  })
);
const feedInspectionStatus = computed(() => {
  return getFeedInspectionStatus({
    canInspect: canAutoInspectFeed.value,
    isHovered: isFeedHovered.value,
    isManuallyPaused: isFeedManuallyPaused.value
  });
});
const activeWindowLabel = computed(() => {
  const label = timeRangeOptions.find(([value]) => value === filters.value.time_range)?.[1] || "7天";
  const rangeLabel = label === "全部" ? "全部时间" : `最近 ${label}`;
  return `情报流：${rangeLabel} · 最多 ${filters.value.limit} 条`;
});
const llmModeLabel = computed(() => {
  if (!llmStatus.value) return "LLM：读取中";
  if (llmStatus.value.configured) {
    return `LLM：${llmStatus.value.provider} / ${llmStatus.value.model}`;
  }
  return "LLM：规则兜底";
});
const llmModeDescription = computed(() => {
  if (!llmStatus.value) return "正在读取后端运行配置";
  if (llmStatus.value.configured) {
    return `相关性准入使用 ${llmStatus.value.mode} 模式，温度 ${llmStatus.value.temperature}`;
  }
  return "未配置 API Key，相关性准入只使用显式规则";
});

const AUTO_SCROLL_SPEED = 0.018;
let autoScrollFrameId = 0;
let lastAutoScrollTime = 0;
let autoSelectingItemId = null;

function queryString() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters.value)) {
    if (value) params.set(key, value);
  }
  return params.toString();
}

async function request(path, options) {
  const response = await fetch(`${API_BASE}${path}`, options);
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

async function loadAll() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const [summaryData, taxonomyData, briefsData, runsData, sourcesData, llmData] = await Promise.all([
      request("/api/intelligence/summary"),
      request("/api/taxonomy"),
      request("/api/briefs"),
      request("/api/runs"),
      request("/api/sources"),
      request("/api/llm/status")
    ]);
    summary.value = summaryData;
    taxonomy.value = taxonomyData;
    briefs.value = briefsData;
    runs.value = runsData;
    sources.value = sourcesData;
    llmStatus.value = llmData;
    if (!runsData.find((run) => run.id === selectedRun.value?.id)) {
      selectedRun.value = runsData[0] || null;
    }
    await loadItems();
  } catch (error) {
    errorMessage.value = `无法连接后端 API：${error.message}`;
  } finally {
    loading.value = false;
  }
}

async function loadItems() {
  const data = await request(`/api/intelligence/items?${queryString()}`);
  items.value = data;
  const nextItem = data.find((item) => item.id === selectedItem.value?.id) || data[0] || null;
  selectedItem.value = nextItem ? await request(`/api/intelligence/items/${nextItem.id}`) : null;
  await nextTick();
  if (feedListRef.value) feedListRef.value.scrollTop = 0;
  syncSelectedItemWithScroll();
}

async function selectItem(item) {
  selectedItem.value = await request(`/api/intelligence/items/${item.id}`);
}

function handleFeedMouseEnter() {
  isFeedHovered.value = true;
  lastAutoScrollTime = 0;
}

function handleFeedMouseLeave() {
  isFeedHovered.value = false;
  lastAutoScrollTime = 0;
}

function toggleFeedManualPause() {
  if (!canAutoInspectFeed.value) return;
  isFeedManuallyPaused.value = !isFeedManuallyPaused.value;
  lastAutoScrollTime = 0;
}

function shouldAutoScrollFeed() {
  const feed = feedListRef.value;
  return Boolean(
    feed &&
      activePage.value === "intelligence" &&
      isFeedInspectionRunning.value &&
      !isReturningToTop.value &&
      (typeof document === "undefined" || !document.hidden) &&
      feed.scrollHeight > feed.clientHeight
  );
}

function syncSelectedItemWithScroll() {
  const feed = feedListRef.value;
  if (!feed || !items.value.length) return;
  const cards = Array.from(feed.querySelectorAll("[data-intel-id]"));
  if (!cards.length) return;
  const feedRect = feed.getBoundingClientRect();
  const feedCenter = feedRect.top + feedRect.height / 2;
  let closestCard = cards[0];
  let closestDistance = Number.POSITIVE_INFINITY;
  for (const card of cards) {
    const cardRect = card.getBoundingClientRect();
    const cardCenter = cardRect.top + cardRect.height / 2;
    const distance = Math.abs(cardCenter - feedCenter);
    if (distance < closestDistance) {
      closestDistance = distance;
      closestCard = card;
    }
  }
  const itemId = Number(closestCard.dataset.intelId);
  const item = items.value.find((entry) => entry.id === itemId);
  if (!item || selectedOrFirstItem.value?.id === item.id || autoSelectingItemId === item.id) return;
  autoSelectingItemId = item.id;
  selectItem(item)
    .catch((error) => {
      errorMessage.value = error.message;
    })
    .finally(() => {
      if (autoSelectingItemId === item.id) autoSelectingItemId = null;
    });
}

function autoScrollFeed(timestamp) {
  const feed = feedListRef.value;
  if (shouldAutoScrollFeed()) {
    if (!lastAutoScrollTime) lastAutoScrollTime = timestamp;
    const delta = Math.min(timestamp - lastAutoScrollTime, 120);
    lastAutoScrollTime = timestamp;
    const nextTop = feed.scrollTop + delta * AUTO_SCROLL_SPEED;
    if (nextTop + feed.clientHeight >= feed.scrollHeight - 2) {
      isReturningToTop.value = true;
      feed.scrollTo({ top: 0, behavior: "smooth" });
      window.setTimeout(() => {
        isReturningToTop.value = false;
        lastAutoScrollTime = 0;
        syncSelectedItemWithScroll();
      }, 900);
    } else {
      feed.scrollTop = nextTop;
      syncSelectedItemWithScroll();
    }
  } else {
    lastAutoScrollTime = 0;
  }
  autoScrollFrameId = window.requestAnimationFrame(autoScrollFeed);
}

function handleVisibilityChange() {
  lastAutoScrollTime = 0;
}

async function manualCollect() {
  manualRunning.value = true;
  try {
    await request("/api/collect/manual", { method: "POST" });
    await loadAll();
  } finally {
    manualRunning.value = false;
  }
}

async function generateBrief() {
  const brief = await request("/api/briefs/generate", { method: "POST" });
  briefs.value = [brief, ...briefs.value];
  activePage.value = "briefs";
}

function setFilter(key, value) {
  filters.value[key] = filters.value[key] === value ? "" : value;
}

function setPersistentFilter(key, value) {
  filters.value[key] = value;
}

function setIndustryFilter(industry) {
  const nextIndustry = filters.value.industry === industry ? "" : industry;
  filters.value.industry = nextIndustry;
  const allowedSubtags = nextIndustry ? taxonomy.value?.industry_subtags?.[nextIndustry] || [] : visibleSubtags.value;
  if (filters.value.industry_subtag && !allowedSubtags.includes(filters.value.industry_subtag)) {
    filters.value.industry_subtag = "";
  }
}

function activeTagValues() {
  return [
    filters.value.industry,
    filters.value.industry_subtag,
    filters.value.event_type,
    filters.value.signal_attribute,
    filters.value.subject_role
  ].filter(Boolean);
}

function isActiveTag(tag) {
  return activeTagValues().includes(tag.tag_value);
}

function visibleTags(item) {
  const tags = item?.tags || [];
  const matched = tags.filter(isActiveTag);
  const rest = tags.filter((tag) => !isActiveTag(tag));
  const seen = new Set();
  return [...matched, ...rest].filter((tag) => {
    const key = `${tag.tag_dimension}:${tag.tag_value}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 8);
}

function originalUrl(item) {
  return item?.canonical_url || item?.url || "";
}

function selectRun(run) {
  selectedRun.value = run;
}

function nodeLabel(node) {
  return {
    collect: "采集",
    normalize: "标准化",
    deduplicate: "去重聚类",
    classify_tag: "分类打标",
    rank: "排序评分",
    extract: "核心信息提炼",
    classify_rank_extract: "分类/排序/提炼",
    generate_brief: "简报生成"
  }[node] || node;
}

function statusLabel(status) {
  return { success: "成功", failed: "失败", running: "运行中" }[status] || status || "未知";
}

function traceNodeSummary(node) {
  const input = node?.input || {};
  const output = node?.output || {};
  if (node.node === "collect") {
    return `从 ${input.source_name || "配置数据源"} 采集原文，保留标题、链接和发布时间。`;
  }
  if (node.node === "normalize") {
    return `生成 raw_item #${output.raw_item_id || "-"}，完成标题、链接、摘要和正文长度标准化。`;
  }
  if (node.node === "deduplicate") {
    return `归入事件簇 #${output.canonical_event_id || "-"}，合并 ${output.duplicate_count || 1} 条相似报道。`;
  }
  if (node.node === "classify_tag") {
    const dimensions = Object.keys(output.tags_by_dimension || {}).length;
    return `产出 ${dimensions} 类结构化标签，重要性为 ${importanceLabel(output.importance_level)}。`;
  }
  if (node.node === "rank") {
    return `按可解释评分公式计算综合价值分 ${formatScore(output.rank_score)}。`;
  }
  if (node.node === "extract") {
    const factCount = Object.keys(output.key_facts || {}).length;
    return `提炼摘要、影响分析和 ${factCount} 个核心事实字段。`;
  }
  return "节点已完成处理，可展开查看入参与出参。";
}

function toggleTraceNode(nodeName) {
  expandedTraceNode.value = expandedTraceNode.value === nodeName ? "" : nodeName;
}

function runDuration(run) {
  if (!run?.started_at || !run?.ended_at) return "运行中";
  const seconds = Math.max(0, Math.round((new Date(run.ended_at) - new Date(run.started_at)) / 1000));
  return `${seconds}s`;
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

function importanceLabel(value) {
  return { high: "高", medium: "中", low: "低" }[value] || value;
}

function formatScore(value) {
  return Number(value || 0).toFixed(2);
}

function formatTime(value) {
  return formatDisplayTime(value);
}

function scoreRows(item) {
  const breakdown = item?.score_breakdown || {};
  return [
    ["重要性", breakdown.importance_score],
    ["来源权威", breakdown.source_score],
    ["时效性", breakdown.freshness_score],
    ["事件类型", breakdown.event_type_score],
    ["覆盖度", breakdown.coverage_score],
    ["关键主体", breakdown.key_actor_score]
  ];
}

watch(filters, () => loadItems().catch((error) => (errorMessage.value = error.message)), { deep: true });

onMounted(() => {
  loadAll();
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", handleVisibilityChange);
  }
  autoScrollFrameId = window.requestAnimationFrame(autoScrollFeed);
});

onUnmounted(() => {
  if (autoScrollFrameId) window.cancelAnimationFrame(autoScrollFrameId);
  if (typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", handleVisibilityChange);
  }
});
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">IA</div>
        <div>
          <h1>产业舆情 Agent</h1>
          <p>Industrial Intelligence</p>
        </div>
      </div>

      <div class="nav-title">Workspace</div>
      <button
        v-for="[key, label] in navItems"
        :key="key"
        class="nav-item"
        :class="{ active: activePage === key }"
        @click="activePage = key"
      >
        <span>{{ label }}</span>
        <span v-if="key === 'intelligence'" class="badge">{{ items.length }}</span>
      </button>

      <div class="monitor-card">
        <div class="pulse-row"><i class="pulse"></i><span>自动监控运行中</span></div>
        <strong>{{ summary?.enabled_source_count || 24 }} 源</strong>
        <p>工作日 09:00-18:00 每 2 小时监控，高权威源每小时检查；08:20 补采，08:30 生成晨报。</p>
      </div>
    </aside>

    <main class="main">
      <section class="topbar glass">
        <div class="page-title">
          <div class="eyebrow">产业情报工作台</div>
          <h2>先看结论，再追证据</h2>
          <p>持续监控状态、筛选上下文、情报流和详情证据保持在同一个工作台里。</p>
        </div>
        <div class="top-actions">
          <span class="status-pill">{{ activeWindowLabel }}</span>
          <span class="llm-status-card" :class="{ fallback: llmStatus && !llmStatus.configured }">
            <strong>{{ llmModeLabel }}</strong>
            <small>{{ llmModeDescription }}</small>
          </span>
          <button class="secondary" @click="generateBrief">生成筛选简报</button>
          <button class="primary" :disabled="manualRunning" @click="manualCollect">
            {{ manualRunning ? "补采中..." : "手动补采" }}
          </button>
        </div>
      </section>

      <p v-if="errorMessage" class="error-banner">{{ errorMessage }}</p>

      <template v-if="activePage === 'intelligence'">
        <section class="summary-strip">
          <article class="summary-card glass lead">
            <span class="label">今日主判断</span>
            <strong>{{ summary?.today_judgement || "正在载入产业信号判断..." }}</strong>
            <p>最近监控：{{ formatTime(summary?.last_monitor_at) }} · 下一轮：按 APScheduler 计划触发</p>
          </article>
          <article class="summary-card glass">
            <span class="label">新增情报</span>
            <strong>{{ summary?.total_count || 0 }}</strong>
            <p>已完成去重、分类、提炼和排序</p>
          </article>
          <article class="summary-card glass">
            <span class="label">高价值</span>
            <strong>{{ summary?.high_count || 0 }}</strong>
            <p>优先进入研究员审阅流</p>
          </article>
          <article class="summary-card glass">
            <span class="label">晨报状态</span>
            <strong>{{ summary?.morning_brief_status || "待生成" }}</strong>
            <p>覆盖前一日 18:00 至当日 08:30</p>
          </article>
        </section>

        <section class="workbench glass" :class="{ expanded: moreFilters }">
          <aside class="filter-panel">
            <h2 class="section-title">情报筛选</h2>
            <div class="filter-group">
              <label>关键词</label>
              <input v-model="filters.keyword" class="input-like" placeholder="宁德时代 / AI 芯片 / 充换电..." />
            </div>

            <div class="filter-group">
              <label>时间范围</label>
              <div class="chip-row compact">
                <button
                  v-for="[value, label] in timeRangeOptions"
                  :key="value"
                  class="chip"
                  :class="{ active: filters.time_range === value }"
                  @click="setPersistentFilter('time_range', value)"
                >
                  {{ label }}
                </button>
              </div>
            </div>

            <div class="filter-group">
              <label>行业</label>
              <div class="chip-row">
                <button
                  v-for="industry in taxonomy?.industries || []"
                  :key="industry"
                  class="chip"
                  :class="{ active: filters.industry === industry }"
                  @click="setIndustryFilter(industry)"
                >
                  {{ industry }}
                </button>
              </div>
            </div>

            <div class="filter-group">
              <label>行业细分标签</label>
              <div class="chip-row compact">
                <button
                  v-for="tag in visibleSubtags"
                  :key="tag"
                  class="chip soft"
                  :class="{ active: filters.industry_subtag === tag }"
                  @click="setFilter('industry_subtag', tag)"
                >
                  {{ tag }}
                </button>
              </div>
            </div>

            <div class="filter-group">
              <label>事件类型标签</label>
              <div class="chip-row compact">
                <button
                  v-for="tag in (taxonomy?.event_types || []).slice(0, moreFilters ? 12 : 6)"
                  :key="tag"
                  class="chip"
                  :class="{ active: filters.event_type === tag }"
                  @click="setFilter('event_type', tag)"
                >
                  {{ tag }}
                </button>
              </div>
            </div>

            <div class="filter-group">
              <label>重要性等级</label>
              <div class="chip-row">
                <button
                  v-for="level in ['high', 'medium', 'low']"
                  :key="level"
                  class="chip"
                  :class="{ active: filters.importance === level }"
                  @click="setFilter('importance', level)"
                >
                  {{ importanceLabel(level) }}
                </button>
              </div>
            </div>

            <template v-if="moreFilters">
              <div class="filter-group">
                <label>主体角色标签</label>
                <div class="chip-row compact">
                  <button
                    v-for="tag in taxonomy?.subject_roles || []"
                    :key="tag"
                    class="chip"
                    :class="{ active: filters.subject_role === tag }"
                    @click="setFilter('subject_role', tag)"
                  >
                    {{ tag }}
                  </button>
                </div>
              </div>

              <div class="filter-group">
                <label>信号属性标签</label>
                <div class="chip-row compact">
                  <button
                    v-for="tag in taxonomy?.signal_attributes || []"
                    :key="tag"
                    class="chip"
                    :class="{ active: filters.signal_attribute === tag }"
                    @click="setFilter('signal_attribute', tag)"
                  >
                    {{ tag }}
                  </button>
                </div>
              </div>

              <div class="filter-group">
                <label>来源类型</label>
                <div class="chip-row compact">
                  <button
                    v-for="(label, type) in sourceTypeLabels"
                    :key="type"
                    class="chip"
                    :class="{ active: filters.source_type === type }"
                    @click="setFilter('source_type', type)"
                  >
                    {{ label }}
                  </button>
                </div>
              </div>

              <div class="filter-group">
                <label>返回条数</label>
                <div class="chip-row compact">
                  <button
                    v-for="value in limitOptions"
                    :key="value"
                    class="chip"
                    :class="{ active: filters.limit === value }"
                    @click="setPersistentFilter('limit', value)"
                  >
                    {{ value }} 条
                  </button>
                </div>
              </div>
            </template>

            <button class="more-button" @click="moreFilters = !moreFilters">
              {{ moreFilters ? "收起筛选" : "更多筛选条件" }}
            </button>
          </aside>

          <section class="feed-panel" @mouseenter="handleFeedMouseEnter" @mouseleave="handleFeedMouseLeave">
            <div class="feed-header">
              <div>
                <h2 class="section-title">高价值情报流</h2>
                <p>按综合价值分排序，自动巡检时右侧详情会随中线情报同步。</p>
              </div>
              <div class="feed-tools">
                <span class="feed-result-count">当前展示 {{ items.length }} 条</span>
                <button
                  class="feed-inspection-pill"
                  :class="{ paused: isFeedHovered || isFeedManuallyPaused, disabled: !canAutoInspectFeed }"
                  type="button"
                  :disabled="!canAutoInspectFeed"
                  :aria-pressed="isFeedManuallyPaused"
                  @click="toggleFeedManualPause"
                >
                  {{ feedInspectionStatus }}
                </button>
                <select v-model="filters.sort" class="sort-select">
                  <option value="rank">综合价值优先</option>
                  <option value="time">发布时间优先</option>
                </select>
              </div>
            </div>

            <div
              ref="feedListRef"
              class="feed-list"
              :class="{ loading, inspecting: isFeedInspectionRunning, paused: canAutoInspectFeed && !isFeedInspectionRunning }"
              @scroll.passive="syncSelectedItemWithScroll"
            >
              <article
                v-for="item in items"
                :key="item.id"
                :data-intel-id="item.id"
                class="intel-card"
                :class="{ active: selectedOrFirstItem?.id === item.id }"
                @click="selectItem(item)"
              >
                <div class="card-top">
                  <span class="score">{{ formatScore(item.rank_score) }}</span>
                  <span class="importance" :class="item.importance_level">{{ importanceLabel(item.importance_level) }}</span>
                  <span>{{ item.source_name }}</span>
                  <span>{{ formatTime(itemDisplayTimestamp(item)) }}</span>
                </div>
                <h3>{{ item.normalized_title }}</h3>
                <p class="impact">{{ item.impact_analysis }}</p>
                <p class="summary">{{ item.summary }}</p>
                <div class="tag-line">
                  <span
                    v-for="tag in visibleTags(item)"
                    :key="`${item.id}-${tag.tag_dimension}-${tag.tag_value}`"
                    :class="{ matched: isActiveTag(tag) }"
                  >
                    {{ tag.tag_value }}
                  </span>
                </div>
                <div class="evidence-line">
                  <span>{{ item.duplicate_count || 1 }} 条相似报道</span>
                  <span>原文可追溯</span>
                </div>
              </article>
            </div>
          </section>

          <aside class="detail-panel">
            <h2 class="section-title">选中事件详情</h2>
            <template v-if="selectedOrFirstItem">
              <div class="detail-head">
                <span class="score big">{{ formatScore(selectedOrFirstItem.rank_score) }}</span>
                <div>
                  <h3>{{ selectedOrFirstItem.normalized_title }}</h3>
                  <a :href="originalUrl(selectedOrFirstItem)" target="_blank" rel="noreferrer">打开原文链接</a>
                </div>
              </div>

              <section class="detail-block">
                <h3>核心信息提炼</h3>
                <ul class="fact-list">
                  <li>谁：{{ selectedOrFirstItem.key_facts?.who }}</li>
                  <li>做了什么：{{ selectedOrFirstItem.key_facts?.what }}</li>
                  <li>何时：{{ formatTime(selectedOrFirstItem.key_facts?.when) }}</li>
                  <li>可能影响：{{ selectedOrFirstItem.key_facts?.impact }}</li>
                </ul>
              </section>

              <section class="detail-block">
                <h3>评分解释</h3>
                <div class="score-breakdown">
                  <div v-for="[label, value] in scoreRows(selectedOrFirstItem)" :key="label" class="bar-row">
                    <span>{{ label }}</span>
                    <div class="bar"><i :style="{ width: `${Math.round((value || 0) * 100)}%` }"></i></div>
                    <b>{{ formatScore(value) }}</b>
                  </div>
                </div>
                <p class="rank-reason">{{ selectedOrFirstItem.rank_reason }}</p>
              </section>

              <section class="detail-block">
                <h3>相似报道与证据链</h3>
                <ul class="source-list">
                  <li v-for="report in selectedOrFirstItem.similar_reports || []" :key="report.url">
                    <span>{{ report.source_name }}</span>
                    <a :href="report.url" target="_blank" rel="noreferrer">权威 {{ formatScore(report.reliability_score) }}</a>
                  </li>
                </ul>
              </section>

              <section class="detail-block">
                <div class="detail-block-title">
                  <h3>处理轨迹</h3>
                  <span class="trace-count">{{ selectedTraceNodes.length }} 个节点</span>
                </div>
                <div v-if="selectedTraceNodes.length" class="trace-chain" role="list">
                  <article
                    v-for="(node, index) in selectedTraceNodes"
                    :key="node.node"
                    class="trace-chain-item"
                    :class="{ expanded: expandedTraceNode === node.node }"
                    role="listitem"
                  >
                    <span class="trace-step-marker" :class="node.status">{{ index + 1 }}</span>
                    <div class="trace-step-card">
                      <button
                        class="trace-step-button"
                        type="button"
                        :aria-expanded="expandedTraceNode === node.node"
                        @click="toggleTraceNode(node.node)"
                      >
                        <span class="trace-step-main">
                          <strong>{{ node.label || nodeLabel(node.node) }}</strong>
                          <span>{{ traceNodeSummary(node) }}</span>
                        </span>
                        <span class="trace-step-meta">
                          <b>{{ statusLabel(node.status) }}</b>
                          <i>{{ expandedTraceNode === node.node ? "收起" : "展开" }}</i>
                        </span>
                      </button>
                      <Transition name="trace-expand">
                        <div v-if="expandedTraceNode === node.node" class="trace-step-detail">
                          <div>
                            <h4>入参</h4>
                            <pre>{{ prettyJson(node.input) }}</pre>
                          </div>
                          <div>
                            <h4>出参</h4>
                            <pre>{{ prettyJson(node.output) }}</pre>
                          </div>
                        </div>
                      </Transition>
                    </div>
                  </article>
                </div>
                <p v-else class="trace-empty">该情报尚未返回处理轨迹，请重新选择或刷新列表。</p>
              </section>
            </template>
          </aside>
        </section>
      </template>

      <section v-else-if="activePage === 'briefs'" class="page-panel glass">
        <h2>简报中心</h2>
        <article v-for="brief in briefs" :key="brief.id" class="brief-card">
          <div><strong>{{ brief.title }}</strong><span>{{ formatTime(brief.generated_at) }}</span></div>
          <pre>{{ brief.content_markdown }}</pre>
        </article>
      </section>

      <section v-else-if="activePage === 'runs'" class="page-panel glass">
        <h2>采集运行</h2>
        <div class="runs-layout">
          <div class="run-list-panel">
            <button
              v-for="run in runs"
              :key="run.id"
              class="run-row"
              :class="{ active: selectedOrFirstRun?.id === run.id }"
              @click="selectRun(run)"
            >
              <strong>{{ run.trigger_type }}</strong>
              <span>{{ run.status }} · {{ runDuration(run) }}</span>
              <span>采集 {{ run.collected_count }} · 去重 {{ run.deduped_count }} · 分类 {{ run.classified_count }}</span>
              <span>{{ formatTime(run.started_at) }}</span>
            </button>
          </div>
          <article v-if="selectedOrFirstRun" class="trace-panel">
            <div class="trace-head">
              <div>
                <span class="label">Workflow Trace</span>
                <h3>{{ selectedOrFirstRun.trigger_type }} #{{ selectedOrFirstRun.id }}</h3>
              </div>
              <span class="status-pill">{{ selectedOrFirstRun.status }}</span>
            </div>
            <div class="trace-meta">
              <span>开始：{{ formatTime(selectedOrFirstRun.started_at) }}</span>
              <span>结束：{{ formatTime(selectedOrFirstRun.ended_at) }}</span>
              <span>失败：{{ selectedOrFirstRun.failed_count }}</span>
            </div>
            <section v-for="node in selectedOrFirstRun.node_trace || []" :key="node.node" class="trace-node">
              <div class="trace-node-title">
                <strong>{{ nodeLabel(node.node) }}</strong>
                <span>{{ node.node }}</span>
              </div>
              <div class="trace-io-grid">
                <div>
                  <h4>入参</h4>
                  <pre>{{ prettyJson(node.input) }}</pre>
                </div>
                <div>
                  <h4>出参</h4>
                  <pre>{{ prettyJson(node.output) }}</pre>
                </div>
              </div>
            </section>
          </article>
        </div>
      </section>

      <section v-else-if="activePage === 'sources'" class="page-panel glass">
        <h2>数据源配置</h2>
        <div class="source-grid">
          <article v-for="source in sources" :key="source.id" class="source-card">
            <strong>{{ source.name }}</strong>
            <span>{{ sourceTypeLabels[source.type] || source.type }} · 权威 {{ formatScore(source.reliability_score) }}</span>
            <span>每 {{ source.fetch_interval_minutes }} 分钟检查 · {{ source.enabled ? "启用" : "停用" }}</span>
            <a :href="source.url" target="_blank" rel="noreferrer">{{ source.url }}</a>
          </article>
        </div>
      </section>

      <section v-else-if="activePage === 'taxonomy'" class="page-panel glass">
        <h2>标签体系</h2>
        <div class="taxonomy-grid">
          <article>
            <h3>行业</h3>
            <p>{{ (taxonomy?.industries || []).join("、") }}</p>
          </article>
          <article class="taxonomy-wide">
            <h3>行业细分标签</h3>
            <div v-for="industry in taxonomy?.industries || []" :key="industry" class="taxonomy-section">
              <strong>{{ industry }}</strong>
              <p>{{ (taxonomy?.industry_subtags?.[industry] || []).join("、") }}</p>
            </div>
          </article>
          <article>
            <h3>事件类型</h3>
            <p>{{ (taxonomy?.event_types || []).join("、") }}</p>
          </article>
          <article>
            <h3>重要性等级</h3>
            <p>{{ (taxonomy?.importance_levels || []).map(importanceLabel).join("、") }}</p>
          </article>
          <article>
            <h3>主体角色</h3>
            <p>{{ (taxonomy?.subject_roles || []).join("、") }}</p>
          </article>
          <article>
            <h3>信号属性</h3>
            <p>{{ (taxonomy?.signal_attributes || []).join("、") }}</p>
          </article>
        </div>
      </section>

      <section v-else class="page-panel glass">
        <h2>评分规则</h2>
        <p class="formula">rank_score = 重要性*0.35 + 来源*0.20 + 时效*0.15 + 事件类型*0.15 + 覆盖度*0.10 + 关键主体*0.05</p>
        <p>最终总分由后端 Rank 节点计算，LLM 只参与结构化标签和理由判断，不直接输出总分。</p>
      </section>
    </main>
  </div>
</template>
