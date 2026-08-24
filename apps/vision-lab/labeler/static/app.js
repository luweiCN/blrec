/* 虚荣视觉标注工作台 —— 前端逻辑(原生 JS) */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let CFG = null;
let queue = [];          // 当前队列帧
let qIdx = 0;
let cur = null;          // 当前帧(含 annotation/boxes/predictions)
let drawMode = null;     // 'viewport' | 'result_panel' | null
let playing = false;
let playTimer = null;
let liveMode = false;   // 实时打标模式:标一帧抽下一帧
let bpReviewQueue = [];
let bpReviewIndex = 0;
let bpVisualCondition = 'clear';
let bpCollectTimer = null;
let bpWorkerSyncTimer = null;
let keyReviewQueue = [];
let keyReviewIndex = 0;
let keyVisualCondition = 'clear';
let keyWorkerSyncTimer = null;
let trainingPollTimer = null;
let trainingLogRunId = null;
let candidateQueue = [];
let candidateIndex = 0;
let candidateSourceScope = 'new';
let candidateLoadedSourceScope = 'new';
let candidateLoadedStatus = 'needs_review';
let candidateFilteredTotal = 0;
let candidateSessionCompleted = 0;
let candidateReviewStats = {};
let candidateModelQuality = null;
const candidateModelQualitySelection = new Map();
let candidateDraft = null;
let candidateBoxes = [];
let candidateDrawStart = null;
let candidateHeroCatalog = [];
let candidateHeroCatalogPromise = null;
let candidateFilterOptionsLoadedScope = '';
let candidateFilterOptionsPromise = null;
let candidateHeroFilters = new Set();
let candidateHeroScope = 'all';
let candidateHeroLineup = null;
let candidateHeroDraft = new Map();
let candidateHeroManualSlots = new Set();
let candidateHeroDirty = false;
let candidateHeroLoading = false;
let candidateHeroPrefillRunning = false;
let candidateHeroPrefillToken = 0;
let candidateHeroLoadToken = 0;
let candidateHeroGeometryRevision = 0;
const candidateHeroSlotRecognitionStates = new Map();
const candidateHeroPendingRecognitionSlots = new Map();
let candidateHeroRecognitionDebounceTimer = null;
let candidateHeroPersistQueue = Promise.resolve();
const CANDIDATE_HERO_RECOGNITION_DEBOUNCE_MS = 300;
let candidateHeroPickerSlot = null;
let candidateHeroPlayerSlot = null;
let candidateHeroPlayerStatus = 'pending';
let candidateHeroAfkReviewRequired = false;
let candidateHeroTeamSizeExplicit = false;
let candidateHeroTeamSizeOverride = null;
let candidateHeroDrawMode = false;
let candidateHeroEdit = null;
let candidateFormTouched = false;
let candidateHeroContextTouched = false;
const candidateHeroPrefetchRequests = new Map();
const candidateImagePrefetches = new Map();
const candidatePreparationRequests = new Map();
const CANDIDATE_READY_TARGET = 24;
const CANDIDATE_IMAGE_PREFETCH_TARGET = 2;
const CANDIDATE_PAGE_SIZE = 50;
const CANDIDATE_REFILL_LOW_WATER = CANDIDATE_READY_TARGET;
const CANDIDATE_DEFAULT_SOURCE_TYPE = 'new_model_prefill';
let candidateReviewRefillPromise = null;
let candidatePreparationRunnerPromise = null;
let candidateReviewLoadToken = 0;
let candidateReviewAbortController = null;
let modelTestRuns = [];
let modelTestSamples = [];
let modelTestIndex = 0;
let modelTestPrediction = null;
let modelTestBatchReport = null;
let modelPackages = [];
let modelDeploymentData = null;
let modelDeploymentPollTimer = null;
const initialTask = new URLSearchParams(window.location.search).get('task');
const state = {
  task: initialTask === 'mode_gate' ? 'mode_gate' : 'result_detector',
  gateRound: null,
};

function isGateTask() {
  return state.task === 'mode_gate';
}

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text.slice(0, 200)}`);
  }
  return res.json();
};

const delay = (milliseconds) => new Promise(
  (resolve) => window.setTimeout(resolve, milliseconds));

async function waitForVisionJob(jobId, timeoutMs = 60000, signal = null) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (signal && signal.aborted) throw new DOMException('Aborted', 'AbortError');
    const response = await api(`/api/vision-jobs/${encodeURIComponent(jobId)}`, {
      signal,
    });
    const job = response.job || {};
    if (['succeeded', 'failed', 'cancelled'].includes(job.status)) return job;
    await delay(1000);
  }
  return null;
}

// ---------- 初始化 ----------
async function init() {
  CFG = await api('/api/config');
  buildSelects();
  buildBoxToolbar();
  buildStrategySelect();
  bindNav();
  bindShortcuts();
  bindBpReview();
  bindKeyScreenReview();
  bindCandidateReview();
  bindModelTesting();
  setTask(state.task, false);
  if (CFG.control_plane_only) {
    const sourceNav = $('.nav-item[data-view="source"]');
    if (sourceNav) sourceNav.classList.add('hidden');
    const candidateNav = $('.nav-item[data-view="candidates"][data-candidate-source="new"]');
    if (candidateNav) activateNav(candidateNav);
    const foot = $('.side-foot');
    if (foot) foot.textContent = 'NAS 控制面 · 重任务由 Worker 执行';
    loadCandidateReview();
    return;
  }
  loadStats();
  loadDatasets();
  loadPairs();
  loadVideos();
  if (isGateTask()) {
    const gateNav = $('.nav-item[data-task="mode_gate"]');
    if (gateNav) activateNav(gateNav);
  }
  loadLiveList();
}
document.addEventListener('DOMContentLoaded', init);

// ---------- 视图切换 ----------
function bindNav() {
  $$('.nav-item').forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.task) setTask(btn.dataset.task, false);
      activateNav(btn);
      if (btn.dataset.view === 'label') loadLiveList();
      if (btn.dataset.view === 'bp-review') loadBpReview();
      if (btn.dataset.view === 'key-screen-review') loadKeyScreenReview();
      if (btn.dataset.view === 'candidates') {
        setCandidateSourceScope(
          btn.dataset.candidateSource || 'new',
          btn.dataset.candidateStatus || 'needs_review',
          false,
        );
        loadCandidateReview();
      }
      if (btn.dataset.view === 'model-tests') loadModelTesting();
      if (btn.dataset.view === 'records') loadTrainingDashboard();
      if (btn.dataset.view !== 'records' && trainingPollTimer) {
        clearInterval(trainingPollTimer);
        trainingPollTimer = null;
      }
    };
  });
}

function activateNav(btn) {
  $$('.nav-item').forEach((b) => b.classList.remove('active'));
  btn.classList.add('active');
  $$('.view').forEach((v) => v.classList.remove('active'));
  $('#view-' + btn.dataset.view).classList.add('active');
}

function setTask(task, reload = true) {
  if (liveMode) exitLive(false);
  state.task = task === 'mode_gate' ? 'mode_gate' : 'result_detector';
  $('#task-name').textContent = state.task;
  $('#lbl-task').textContent = isGateTask() ? '光栅专项' : 'result_detector';
  $('#view-label').classList.toggle('gate-task-active', isGateTask());
  $('#inspector').classList.toggle('gate-task', isGateTask());
  $('#gate-inspector').classList.toggle('hidden', !isGateTask());
  $('#live-work-hint').textContent = isGateTask()
    ? '播放或时间按钮用来寻找地图入口。看到有效证据后，用右侧专项按钮标记。'
    : '播放=每秒取一帧前进一个间隔;时间按钮=跳到该时间点的帧。当前帧就是打标帧,右侧标注后点「完成并下一张」';
  if (reload) loadLiveList();
}

// ---------- Worker 候选数据 ----------
const TRAINING_REVIEW_FIELDS = [
  {
    key: 'match_flow_label', suggestion: 'match_flow', title: '1. 是否在对局流程中',
    help: '英雄选择属于“非对局画面”；战斗、积分板、商店、胜负动画和结算页属于“对局流程中”。',
    labels: {
      match_flow: '对局流程中', not_match_flow: '非对局画面', unreadable: '看不清',
    },
  },
  {
    key: 'match_mode_label', suggestion: 'match_mode', title: '2. 对局模式',
    help: '只在对局画面中判断。商店或其他无法看出地图模式的画面选“看不出模式”。',
    labels: {
      '3v3': '3V3', aram: '大乱斗', '5v5': '5V5', blitz: '闪电战',
      unreadable: '看不出模式',
    },
  },
  {
    key: 'hero_select_label', suggestion: 'hero_select', title: '3. 是否是英雄选择界面',
    help: '匹配接受／拒绝不是英雄选择。能看出英雄选择时，同时标出模式。',
    labels: {
      not_select: '不是英雄选择', select_3v3: '3V3 英雄选择',
      select_aram: '大乱斗英雄选择', select_5v5: '5V5 英雄选择',
      select_blitz: '闪电战英雄选择',
      unreadable: '看不清',
    },
  },
  {
    key: 'result_panel_label', suggestion: 'result_panel', title: '4. 是否有真正结算面板',
    help: '积分板不是结算。选择“有结算面板”后，直接在左图框完整面板；一张图只保留一个大框。',
    labels: {
      result_panel: '有结算面板', no_result_panel: '没有结算面板', unreadable: '看不清',
    },
  },
];

const TRAINING_REVIEW_LABEL_TEXT = Object.fromEntries(
  TRAINING_REVIEW_FIELDS.map((field) => [field.suggestion, field.labels]));
const CANDIDATE_REVIEW_DEFAULTS_STORAGE_KEY =
  'vainglory-vision-lab.training-review-defaults.v1';
const CANDIDATE_CONTEXT_CACHE_MAX_GAP_MS = 10 * 60 * 1000;
const candidateMatchContextCache = new Map();

const CANDIDATE_MATCH_KINDS = {
  pvp: '真人对战',
  bot: '人机对战',
  practice: '单人练习',
  unreadable: '看不清',
};

const CANDIDATE_VIEW_CONTEXTS = {
  played: '本人操作',
  spectated: '观战',
  replay: '回放',
  unreadable: '看不清',
};

const CANDIDATE_SUGGESTION_TITLES = {
  match_flow: '对局流程',
  match_mode: '对局模式',
  hero_select: '英雄选择',
  result_panel: '结算面板',
};

const CANDIDATE_SOURCE_LABELS = {
  legacy: '历史标签迁移',
  worker: 'Worker 新采样',
  manual_correction: '后台人工纠错',
  result_archive: 'NAS 结算归档',
  model_prefill: '新模型预填',
  hero_model_prefill: '英雄模型预填',
  other: '其他本地素材',
};

const CANDIDATE_QUEUE_OPTIONS = {
  all: [['confirmed', '全部已确认训练数据']],
  new: [
    ['needs_review', '待确认'],
    ['missing_player', '只补本人标记'],
    ['missing_afk', '挂机状态待补'],
    ['confirmed', '已确认'],
    ['skipped', '已跳过'],
    ['all', '全部新图'],
  ],
  legacy: [
    ['migration_review', '迁移待人工复核'],
    ['needs_review', '旧标签不完整'],
    ['legacy_hero', '头像待补齐（按局折叠）'],
    ['missing_afk', '挂机状态待补'],
    ['human_confirmed', '新流程人工已确认'],
    ['skipped', '已跳过'],
    ['all', '全部历史图'],
  ],
};

const CANDIDATE_HERO_LAYOUTS = {
  gameplay_hud: '游戏中顶部 HUD',
  scoreboard: '积分板',
  result_page: '结算界面',
  none: '没有可标的英雄头像',
  unreadable: '看不清／无法判断',
};

const CANDIDATE_HERO_LAYOUT_SHORT_LABELS = {
  gameplay_hud: 'HUD',
  scoreboard: '积分板',
  result_page: '结算',
  none: '无头像',
  unreadable: '看不清',
};

const CANDIDATE_HERO_SCREEN_TYPES = new Set([
  'gameplay_hud', 'scoreboard', 'result_page',
]);

const CANDIDATE_HERO_SELECT_VARIANTS = {
  bp: 'BP／征召',
  blind: '盲选',
  random: '随机英雄',
  unreadable: '看不清选择方式',
};

const CANDIDATE_HERO_SELECT_VISIBILITY = {
  clear: '清晰',
  occluded: '有遮挡但仍能确认',
};

function currentCandidate() {
  return candidateQueue[candidateIndex] || null;
}

function renderCandidateReviewReasonFilter() {
  const status = $('#candidate-status-filter').value;
  const visible = ['confirmed', 'human_confirmed'].includes(status);
  $('#candidate-review-reason-field').classList.toggle('hidden', !visible);
  if (!visible) $('#candidate-review-reason-filter').value = '';
}

function setCandidateSourceScope(scope, status = 'needs_review', syncNav = true) {
  candidateSourceScope = ['new', 'legacy', 'all'].includes(scope) ? scope : 'new';
  candidateReviewStats = {};
  renderCandidateMaterialSuggestionButton();
  const options = CANDIDATE_QUEUE_OPTIONS[candidateSourceScope];
  const selected = options.some(([value]) => value === status)
    ? status : 'needs_review';
  $('#candidate-status-filter').replaceChildren(
    ...options.map(([value, label]) => new Option(label, value)),
  );
  $('#candidate-status-filter').value = selected;
  renderCandidateReviewReasonFilter();
  const historical = candidateSourceScope === 'legacy';
  const confirmedTraining = candidateSourceScope === 'all';
  const sourceType = $('#candidate-source-type-filter');
  if (sourceType) {
    sourceType.value = historical || confirmedTraining
      ? '' : CANDIDATE_DEFAULT_SOURCE_TYPE;
  }
  $('#candidate-page-title').textContent = confirmedTraining
    ? '已确认训练数据'
    : historical ? '历史人工数据' : 'Worker 待复核';
  $('#candidate-queue-metrics').classList.toggle(
    'hidden', historical || confirmedTraining);
  if (!historical && !confirmedTraining) {
    $('#candidate-worker-total').textContent = '—';
    $('#candidate-prefill-ready').textContent = '—';
    $('#candidate-ready-for-review').textContent = '—';
  }
  $('#candidate-scope-summary').textContent = confirmedTraining
    ? '正在读取全部已确认训练数据…'
    : historical
      ? '正在读取历史人工数据…'
      : '这里只展示已经完成模型预打标、可以直接核对的素材。';
  candidateFilterOptionsLoadedScope = '';
  if (syncNav) {
    $$('.nav-item').forEach((button) => button.classList.remove('active'));
    const nav = $(`.nav-item[data-view="candidates"]` +
      `[data-candidate-source="${candidateSourceScope}"]`);
    if (nav) nav.classList.add('active');
  }
}

function candidateSourceText(item) {
  const categories = Array.from(new Set(item && item.source_categories || []));
  if (!categories.length) return '来源未记录';
  return categories.map((value) => CANDIDATE_SOURCE_LABELS[value] || value).join('＋');
}

function candidateSuggestion(item, task) {
  return item.suggestions && item.suggestions[task] || null;
}

function candidateSuggestedValue(item, field) {
  const suggestion = candidateSuggestion(item, field.suggestion);
  return suggestion && field.labels[suggestion.label] ? suggestion.label : null;
}

function candidateCachedReviewLabels() {
  try {
    const stored = JSON.parse(
      window.localStorage.getItem(CANDIDATE_REVIEW_DEFAULTS_STORAGE_KEY) || '{}');
    if (!stored || typeof stored !== 'object' || Array.isArray(stored)) return {};
    const values = Object.fromEntries(TRAINING_REVIEW_FIELDS.flatMap((field) => {
      const value = stored[field.key];
      return Object.prototype.hasOwnProperty.call(field.labels, value)
        ? [[field.key, value]] : [];
    }));
    if (['bp', 'blind'].includes(stored.hero_select_variant)) {
      values.hero_select_variant = stored.hero_select_variant;
    }
    return values;
  } catch (_error) {
    return {};
  }
}

function cacheCandidateReviewLabels(draft) {
  const values = candidateCachedReviewLabels();
  Object.assign(values, Object.fromEntries(TRAINING_REVIEW_FIELDS.flatMap((field) => {
    const value = draft[field.key];
    return Object.prototype.hasOwnProperty.call(field.labels, value)
      ? [[field.key, value]] : [];
  })));
  if (['bp', 'blind'].includes(draft.hero_select_variant)) {
    values.hero_select_variant = draft.hero_select_variant;
  }
  try {
    window.localStorage.setItem(
      CANDIDATE_REVIEW_DEFAULTS_STORAGE_KEY, JSON.stringify(values));
  } catch (_error) {
    // 浏览器禁止本地存储时仍可继续打标，只是不跨图片沿用。
  }
}

function candidateCachedMatchContext(item) {
  const videoId = Number(item && item.video_id);
  const timestampMs = Number(item && item.timestamp_ms);
  if (!videoId || !Number.isFinite(timestampMs)) return null;
  const cached = candidateMatchContextCache.get(videoId);
  if (!cached || Math.abs(timestampMs - cached.timestampMs) >
      CANDIDATE_CONTEXT_CACHE_MAX_GAP_MS) return null;
  return cached;
}

function cacheCandidateMatchContext(item, draft) {
  const videoId = Number(item && item.video_id);
  const timestampMs = Number(item && item.timestamp_ms);
  if (!videoId || !Number.isFinite(timestampMs) ||
      draft.match_flow_label !== 'match_flow') return;
  if (!CANDIDATE_MATCH_KINDS[draft.match_kind_label] ||
      !CANDIDATE_VIEW_CONTEXTS[draft.view_context_label]) return;
  candidateMatchContextCache.set(videoId, {
    timestampMs,
    match_kind_label: draft.match_kind_label,
    view_context_label: draft.view_context_label,
  });
}

function applyCandidateMatchContextDefaults(draft, item) {
  if (draft.match_flow_label !== 'match_flow') {
    draft.match_kind_label = null;
    draft.view_context_label = null;
    return;
  }
  const cached = candidateCachedMatchContext(item) || {};
  if (!CANDIDATE_MATCH_KINDS[draft.match_kind_label]) {
    draft.match_kind_label = item.match_kind_label ||
      cached.match_kind_label || 'pvp';
  }
  if (!CANDIDATE_VIEW_CONTEXTS[draft.view_context_label]) {
    draft.view_context_label = item.view_context_label ||
      cached.view_context_label || 'played';
  }
}

function candidateResultHeroCountMode(item) {
  for (const source of item.sources || []) {
    if (source.source_type !== 'result_archive') continue;
    const count = Number((source.metadata || {}).hero_slot_count || 0);
    if (count === 6) return '3v3';
    if (count >= 7 && count <= 10) return '5v5';
  }
  return null;
}

function candidateHeroContextSuggestion(item) {
  for (const source of item.sources || []) {
    const value = source.metadata && source.metadata.hero_context_suggestion;
    if (!value || !Object.prototype.hasOwnProperty.call(
      CANDIDATE_HERO_LAYOUTS, value.screen_type)) continue;
    if (CANDIDATE_HERO_SCREEN_TYPES.has(value.screen_type) &&
        ![3, 5].includes(Number(value.team_size))) continue;
    return value;
  }
  return null;
}

function candidateSourceScreenTypes(source) {
  const metadata = source && source.metadata || {};
  const values = [metadata.screen_type, metadata.stage_class];
  for (const output of metadata.model_outputs || []) {
    values.push(output && output.stage_class);
  }
  return values.filter((value) => typeof value === 'string' && value);
}

function candidateDefaultDraft(item) {
  const hasHumanLabels = TRAINING_REVIEW_FIELDS.some(
    (field) => Boolean(item[field.key]));
  const heroSelectSuggestion = candidateSuggestion(item, 'hero_select');
  const suggestedHeroSelect = !hasHumanLabels &&
    String(heroSelectSuggestion && heroSelectSuggestion.label || '')
      .startsWith('select_') &&
    Number(heroSelectSuggestion.confidence || 0) >= 0.8
      ? heroSelectSuggestion.label : null;
  const preferCachedLabels = item.review_status === 'pending' ||
    item.review_status === 'partial' || Boolean(item.needs_player_hero_review);
  const cached = !hasHumanLabels || preferCachedLabels
    ? candidateCachedReviewLabels() : {};
  const resultHeroCountMode = hasHumanLabels
    ? null : candidateResultHeroCountMode(item);
  const draft = {};
  TRAINING_REVIEW_FIELDS.forEach((field) => {
    const resultMode = field.key === 'match_mode_label'
      ? resultHeroCountMode : null;
    const itemValue = item[field.key];
    const suggestion = candidateSuggestion(item, field.suggestion);
    const newModelValue = suggestion &&
      suggestion.origin === 'new_model_prefill'
      ? candidateSuggestedValue(item, field) : null;
    draft[field.key] = itemValue || resultMode || newModelValue ||
      (preferCachedLabels ? cached[field.key] : null) ||
      candidateSuggestedValue(item, field) || cached[field.key] || null;
  });
  if (suggestedHeroSelect) {
    draft.match_flow_label = 'not_match_flow';
    draft.match_mode_label = null;
    draft.hero_select_label = suggestedHeroSelect;
    draft.result_panel_label = 'no_result_panel';
  }
  draft.match_flow_label ||= 'unreadable';
  draft.result_panel_label ||= 'no_result_panel';
  if (draft.match_flow_label === 'match_flow') {
    draft.match_mode_label ||= 'unreadable';
    draft.hero_select_label = 'not_select';
  } else if (draft.match_flow_label === 'not_match_flow') {
    draft.match_mode_label = null;
    draft.hero_select_label ||= 'not_select';
  } else {
    draft.match_mode_label = null;
    draft.hero_select_label ||= 'unreadable';
  }
  if (draft.hero_select_label.startsWith('select_')) {
    draft.result_panel_label = 'no_result_panel';
  } else if (draft.result_panel_label === 'result_panel') {
    draft.match_flow_label = 'match_flow';
    draft.match_mode_label ||= 'unreadable';
    draft.hero_select_label = 'not_select';
  }
  draft.hero_select_variant = preferCachedLabels
    ? cached.hero_select_variant || item.hero_select_variant || null
    : item.hero_select_variant || cached.hero_select_variant || null;
  normalizeCandidateHeroSelectVariant(draft);
  draft.hero_select_visibility = item.hero_select_visibility || null;
  if (draft.hero_select_label.startsWith('select_') &&
      !['clear', 'occluded', 'unknown'].includes(
        draft.hero_select_visibility)) {
    draft.hero_select_visibility = 'clear';
  }
  draft.hero_layout_label = item.hero_layout_label || null;
  if (CANDIDATE_HERO_SCREEN_TYPES.has(item.legacy_hero_screen_type)) {
    draft.hero_layout_label = item.legacy_hero_screen_type;
  }
  if (!draft.hero_layout_label) {
    if (draft.result_panel_label === 'result_panel') {
      draft.hero_layout_label = 'result_page';
    } else {
      const heroContext = candidateHeroContextSuggestion(item);
      if (heroContext) draft.hero_layout_label = heroContext.screen_type;
      for (const source of item.sources || []) {
        if (draft.hero_layout_label) break;
        const screens = candidateSourceScreenTypes(source);
        if (screens.some((screen) =>
          ['scoreboard', 'death_scoreboard'].includes(screen))) {
          draft.hero_layout_label = 'scoreboard';
          break;
        }
        if (screens.some((screen) =>
          ['gameplay', 'gameplay_hud', 'in_match'].includes(screen))) {
          draft.hero_layout_label = 'gameplay_hud';
          break;
        }
      }
    }
  }
  draft.hero_layout_label ||= 'none';
  if (draft.match_flow_label !== 'match_flow') {
    draft.hero_layout_label = draft.match_flow_label === 'unreadable'
      ? 'unreadable' : 'none';
  } else if (draft.result_panel_label === 'result_panel') {
    draft.hero_layout_label = 'result_page';
  } else if (draft.hero_layout_label === 'result_page') {
    draft.hero_layout_label = 'none';
  }
  draft.panel_render_state = item.panel_render_state || 'clear';
  if (!candidateDraftHasRenderState(draft)) {
    draft.panel_render_state = 'clear';
  }
  draft.ocr_usable = item.ocr_usable || 'yes';
  draft.result_occlusion = item.result_occlusion || 'none';
  draft.occluder_types = Array.isArray(item.occluder_types)
    ? [...item.occluder_types] : [];
  applyCandidateMatchContextDefaults(draft, item);
  return draft;
}

function normalizeCandidateHeroSelectVariant(draft) {
  const label = draft.hero_select_label || '';
  if (label === 'select_aram') {
    draft.hero_select_variant = 'random';
    draft.hero_select_visibility ||= 'clear';
    return;
  }
  if (['select_3v3', 'select_5v5', 'select_blitz'].includes(label)) {
    if (!['bp', 'blind', 'unreadable'].includes(draft.hero_select_variant)) {
      draft.hero_select_variant = null;
    }
    draft.hero_select_visibility ||= 'clear';
    return;
  }
  draft.hero_select_variant = null;
  draft.hero_select_visibility = null;
}

function candidateSuggestedResultBox(item) {
  const newModelSource = (item.sources || []).find(
    (source) => source.source_type === 'new_model_prefill');
  const newModelBox = (newModelSource && newModelSource.metadata &&
    newModelSource.metadata.suggested_boxes || []).find((value) =>
    ['result_panel', ''].includes(value.type || value.box_type || ''));
  if (['pending', 'partial'].includes(item.review_status)) {
    return newModelBox
      ? {...newModelBox, type: 'result_panel', source: 'new_model'}
      : null;
  }
  if (item.boxes && item.boxes.result_panel) {
    return {...item.boxes.result_panel, type: 'result_panel', source: 'saved'};
  }
  if (newModelBox) {
    return {...newModelBox, type: 'result_panel', source: 'new_model'};
  }
  for (const source of item.sources || []) {
    const boxes = source.metadata && source.metadata.suggested_boxes || [];
    const box = boxes.find((value) =>
      ['result_panel', ''].includes(value.type || value.box_type || ''));
    if (box) return {...box, type: 'result_panel', source: 'legacy_suggestion'};
  }
  return null;
}

function candidateHeroClamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function candidateHeroDisplayCrop(crop, layerRect) {
  if (!crop || !layerRect.width || !layerRect.height) return crop;
  const maximumDiameter = Math.min(layerRect.width, layerRect.height);
  const rawWidth = Math.max(0, crop.w * layerRect.width);
  const rawHeight = Math.max(0, crop.h * layerRect.height);
  const rawDiameter = Math.sqrt(rawWidth * rawHeight);
  const diameter = Math.min(maximumDiameter, Math.max(16, rawDiameter));
  const centerX = (crop.x + crop.w / 2) * layerRect.width;
  const centerY = (crop.y + crop.h / 2) * layerRect.height;
  const left = candidateHeroClamp(
    centerX - diameter / 2, 0, layerRect.width - diameter);
  const top = candidateHeroClamp(
    centerY - diameter / 2, 0, layerRect.height - diameter);
  return {
    x: left / layerRect.width,
    y: top / layerRect.height,
    w: diameter / layerRect.width,
    h: diameter / layerRect.height,
  };
}

function applyCandidateHeroCrop(node, crop) {
  node.style.left = `${crop.x * 100}%`;
  node.style.top = `${crop.y * 100}%`;
  node.style.width = `${crop.w * 100}%`;
  node.style.height = `${crop.h * 100}%`;
}

function candidateHeroSlot(slotKey) {
  if (!candidateHeroLineup) return null;
  return candidateHeroLineup.slots.find((slot) =>
    candidateHeroKey(slot.side, slot.slot) === slotKey) || null;
}

function startCandidateHeroEdit(event, node, slotKey, mode) {
  if (event.button !== 0) return;
  const layerRect = $('#candidate-box-layer').getBoundingClientRect();
  const slot = candidateHeroSlot(slotKey);
  if (!slot || !layerRect.width || !layerRect.height) return;
  event.preventDefault();
  event.stopPropagation();
  candidateHeroPickerSlot = null;
  $('#candidate-hero-picker').classList.add('hidden');
  const displayCrop = candidateHeroDisplayCrop(slot.crop, layerRect);
  candidateHeroEdit = {
    pointerId: event.pointerId,
    slotKey,
    mode,
    startX: event.clientX,
    startY: event.clientY,
    originalSlots: candidateHeroLineup.slots.map((value) => ({
      ...value,
      crop: {...value.crop},
    })),
    displayCrop,
    layerWidth: layerRect.width,
    layerHeight: layerRect.height,
    moved: false,
  };
  applyCandidateHeroCrop(node, displayCrop);
  node.classList.add('editing');
  node.setPointerCapture(event.pointerId);
}

function moveCandidateHeroEdit(event) {
  const edit = candidateHeroEdit;
  if (!edit || edit.pointerId !== event.pointerId) return;
  const slot = candidateHeroSlot(edit.slotKey);
  if (!slot) return;
  event.preventDefault();
  event.stopPropagation();
  const dx = event.clientX - edit.startX;
  const dy = event.clientY - edit.startY;
  edit.moved ||= Math.hypot(dx, dy) >= 1;
  const original = edit.displayCrop;
  let crop;
  if (edit.mode === 'resize') {
    const originalDiameter = original.w * edit.layerWidth;
    const delta = Math.abs(dx) >= Math.abs(dy) ? dx : dy;
    const left = original.x * edit.layerWidth;
    const top = original.y * edit.layerHeight;
    const maximumDiameter = Math.min(
      edit.layerWidth - left, edit.layerHeight - top);
    const minimumDiameter = Math.min(16, maximumDiameter);
    const diameter = candidateHeroClamp(
      originalDiameter + delta, minimumDiameter, maximumDiameter);
    crop = {
      x: original.x,
      y: original.y,
      w: diameter / edit.layerWidth,
      h: diameter / edit.layerHeight,
    };
  } else {
    crop = {
      x: candidateHeroClamp(
        original.x + dx / edit.layerWidth, 0, 1 - original.w),
      y: candidateHeroClamp(
        original.y + dy / edit.layerHeight, 0, 1 - original.h),
      w: original.w,
      h: original.h,
    };
  }
  candidateHeroLineup.slots = candidateHeroLinkedSlots(
    edit.originalSlots,
    edit.slotKey,
    crop,
    candidateHeroLineup.screen_type,
    edit.mode,
  );
  applyCandidateHeroSlotsToCanvas();
}

function finishCandidateHeroEdit(event, node) {
  const edit = candidateHeroEdit;
  if (!edit || edit.pointerId !== event.pointerId) return;
  event.preventDefault();
  event.stopPropagation();
  candidateHeroEdit = null;
  node.classList.remove('editing');
  if (node.hasPointerCapture(event.pointerId)) {
    node.releasePointerCapture(event.pointerId);
  }
  const slot = candidateHeroSlot(edit.slotKey);
  if (!slot) return;
  if (!edit.moved) {
    candidateHeroLineup.slots = edit.originalSlots;
    if (edit.mode === 'move') openCandidateHeroPicker(node, edit.slotKey);
    else renderCandidateBoxes();
    return;
  }
  markCandidateHeroGeometryEdited();
  const changedSlots = candidateHeroChangedSlots(
    edit.originalSlots, candidateHeroLineup.slots);
  clearCandidateHeroRecognition(changedSlots);
  scheduleCandidateHeroRecognition(changedSlots);
  $('#candidate-save-state').textContent =
    '英雄圆框的位置和大小已更新，正在重新识别受影响的头像';
}

function cancelCandidateHeroEdit(event) {
  const edit = candidateHeroEdit;
  if (!edit || edit.pointerId !== event.pointerId) return;
  candidateHeroLineup.slots = edit.originalSlots;
  candidateHeroEdit = null;
  renderCandidateHeroLineup();
}

function applyCandidateHeroSlotsToCanvas() {
  const layer = $('#candidate-box-layer');
  const layerRect = layer.getBoundingClientRect();
  (candidateHeroLineup && candidateHeroLineup.slots || []).forEach((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    const node = [...layer.querySelectorAll('.candidate-hero-circle')]
      .find((value) => value.dataset.heroSlot === key);
    if (node) {
      const displayCrop = candidateHeroDisplayCrop(slot.crop, layerRect);
      applyCandidateHeroCrop(node, displayCrop);
    }
  });
}

function renderCandidateBoxes() {
  const layer = $('#candidate-box-layer');
  const layerRect = layer.getBoundingClientRect();
  layer.innerHTML = '';
  candidateBoxes.forEach((box) => {
    const node = document.createElement('div');
    node.className = 'candidate-box result-box';
    node.title = box.source === 'new_model'
      ? '新结算模型在本图检测出的结算框'
      : box.source === 'saved'
        ? '本图已经保存的结算框'
        : '历史候选在本图给出的结算框';
    node.style.left = `${box.x * 100}%`;
    node.style.top = `${box.y * 100}%`;
    node.style.width = `${box.w * 100}%`;
    node.style.height = `${box.h * 100}%`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = '删除这个框';
    remove.onpointerdown = (event) => event.stopPropagation();
    remove.onclick = (event) => {
      event.stopPropagation();
      candidateBoxes = [];
      renderCandidateBoxes();
    };
    node.appendChild(remove);
    layer.appendChild(node);
  });
  if (!candidateHeroLineup) return;
  (candidateHeroLineup.slots || []).forEach((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    const box = candidateHeroDisplayCrop(slot.crop, layerRect);
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'candidate-hero-circle';
    node.dataset.heroSlot = key;
    node.classList.toggle('selected', candidateHeroPickerSlot === key);
    const recognizing = candidateHeroSlotIsRecognizing(key);
    node.classList.toggle('recognizing', recognizing);
    node.setAttribute('aria-busy', String(recognizing));
    const isPlayer = ['scoreboard', 'result_page'].includes(
      candidateHeroLineup.screen_type) &&
      candidateHeroPlayerStatus === 'identified' &&
      candidateHeroPlayerSlot === key;
    node.classList.toggle('player', isPlayer);
    applyCandidateHeroCrop(node, box);
    const label = candidateHeroDraft.get(key) || '';
    const hero = candidateHeroDisplay(label);
    node.title = hero
      ? `${slot.side === 'left' ? '左队' : '右队'} ${slot.slot}：${hero.name}`
      : `${slot.side === 'left' ? '左队' : '右队'} ${slot.slot}：点击选择英雄`;
    if (isPlayer) node.title += '（主播本人）';
    if (candidateHeroLineup.screen_type === 'gameplay_hud') {
      node.title += slot.slot === 2
        ? '；拖动可调整本队间距和整排高度'
        : '；拖动可微调横向位置和整排高度';
    }
    const tag = document.createElement('span');
    tag.className = 'candidate-hero-circle-label';
    tag.textContent = `${slot.side === 'left' ? '左' : '右'}${slot.slot}` +
      (isPlayer ? ' · 本人' : '') + (recognizing ? ' · 识别中' : '');
    node.appendChild(tag);
    const resizeHandle = document.createElement('span');
    resizeHandle.className = 'candidate-hero-resize-handle';
    resizeHandle.title = '拖动调整圆框大小';
    resizeHandle.onpointerdown = (event) =>
      startCandidateHeroEdit(event, node, key, 'resize');
    node.appendChild(resizeHandle);
    node.onpointerdown = (event) =>
      startCandidateHeroEdit(event, node, key, 'move');
    node.onpointermove = (event) => moveCandidateHeroEdit(event);
    node.onpointerup = (event) => finishCandidateHeroEdit(event, node);
    node.onpointercancel = cancelCandidateHeroEdit;
    node.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.detail === 0) openCandidateHeroPicker(node, key);
    };
    layer.appendChild(node);
  });
}

function renderCandidateSuggestions(item) {
  const suggestions = $('#candidate-suggestion');
  suggestions.innerHTML = '';
  const historical = (item.source_categories || []).includes('legacy');
  const hasNewModelPrefill = (item.sources || []).some(
    (source) => source.source_type === 'new_model_prefill');
  $('.candidate-model-title').textContent = historical
    ? '新模型对照' : '模型建议';
  if (historical && !hasNewModelPrefill) {
    suggestions.textContent = '正在生成新模型结果；旧人工标签不会被覆盖';
    $('#candidate-reason').textContent = '';
    $('#candidate-reason').title = '';
    return;
  }
  TRAINING_REVIEW_FIELDS.forEach((field) => {
    const suggestion = candidateSuggestion(item, field.suggestion);
    if (!suggestion) return;
    const line = document.createElement('div');
    line.className = 'candidate-suggestion-line';
    const label = field.labels[suggestion.label] || suggestion.label;
    const title = CANDIDATE_SUGGESTION_TITLES[field.suggestion] ||
      field.title.replace(/^\d+\.\s*/, '');
    line.textContent = `${title}：${label} · ` +
      `${(Number(suggestion.confidence || 0) * 100).toFixed(1)}%`;
    suggestions.appendChild(line);
  });
  if (!suggestions.childElementCount) {
    suggestions.textContent = historical
      ? '新模型没有产出可用结果；旧人工标签仍然保留'
      : '没有模型建议';
  }
  const reasons = new Set();
  Object.values(item.suggestions || {}).forEach((suggestion) => {
    if (suggestion.reason) reasons.add(suggestion.reason);
  });
  (item.sources || []).forEach((source) => {
    const metadata = source.metadata || {};
    if (metadata.selection_reason) reasons.add(metadata.selection_reason);
    (metadata.model_outputs || []).forEach((output) => {
      if (output.selection_reason) reasons.add(output.selection_reason);
    });
  });
  const reasonText = [...reasons].join('；');
  $('#candidate-reason').textContent = reasonText;
  $('#candidate-reason').title = reasonText;
  renderCandidatePrefillStatus(item);
}

function renderCandidatePrefillStatus(item) {
  const core = (item.sources || []).find(
    (source) => source.source_type === 'new_model_prefill');
  const heroes = (item.sources || []).find(
    (source) => source.source_type === 'new_model_hero_prefill');
  const parts = [];
  const modelText = (source) => Object.entries(
    source && source.metadata && source.metadata.model_runs || {})
    .map(([task, run]) => `${task}=${run}`)
    .join(' · ');
  if (core) {
    const errors = Object.keys(core.metadata && core.metadata.errors || {});
    parts.push(errors.length
      ? `分类预填部分完成（未产出：${errors.join('、')}）`
      : '分类与结算框预填已完成');
    const models = modelText(core);
    if (models) parts.push(models);
  } else if (['pending', 'partial'].includes(String(item.review_status || ''))) {
    parts.push('分类预填等待 Vision Worker');
  }
  if (heroes) {
    const metadata = heroes.metadata || {};
    parts.push(metadata.complete === false
      ? `头像模型已运行但未找全：${metadata.reason || '请人工修正'}`
      : '头像位置与英雄预填已完成');
    const models = modelText(heroes);
    if (models) parts.push(models);
  }
  $('#candidate-prefill-status').textContent = parts.join(' · ');
}

function candidateHeroKey(side, slot) {
  return `${side}:${slot}`;
}

function candidateHeroPlayerKey(lineup) {
  if (!lineup || !lineup.player_side || !lineup.player_slot) return null;
  return candidateHeroKey(lineup.player_side, lineup.player_slot);
}

function candidateHeroPlayerStatusForLineup(lineup) {
  const status = String(lineup && lineup.player_status || '');
  if (['pending', 'identified', 'unreadable'].includes(status)) return status;
  return candidateHeroPlayerKey(lineup) ? 'identified' : 'pending';
}

function candidateHeroPlayerPosition() {
  if (candidateHeroPlayerStatus !== 'identified' ||
      !candidateHeroPlayerSlot) return null;
  const slot = candidateHeroSlot(candidateHeroPlayerSlot);
  return slot ? {side: slot.side, slot: slot.slot} : null;
}

function candidateHeroByLabel(label) {
  return candidateHeroCatalog.find((hero) => hero.label === label) || null;
}

function candidateHeroDraftForLineup(item, lineup, previousDraft = new Map()) {
  return new Map((lineup.slots || []).map((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    const previous = previousDraft.get(key) || '';
    if (previous && candidateHeroManualSlots.has(key)) return [key, previous];
    const recognized = slot.confirmed_label || slot.suggested_label || '';
    if (recognized) return [key, recognized];
    return [key, previous];
  }));
}

function candidateHeroKnownTeamSize(item, draft = candidateDraft) {
  const selectedMode = draft && draft.match_mode_label || '';
  if (selectedMode === '5v5') return 5;
  if (['3v3', 'aram', 'blitz'].includes(selectedMode)) return 3;
  const legacySize = Number(item && item.legacy_hero_team_size);
  if ([3, 5].includes(legacySize)) return legacySize;
  const suggestion = candidateHeroContextSuggestion(item || {});
  const suggestedSize = Number(suggestion && suggestion.team_size);
  return [3, 5].includes(suggestedSize) ? suggestedSize : null;
}

function candidateHeroContext(
  item, draft = candidateDraft, useInteractiveState = true) {
  if (!item || !draft) return null;
  const screenType = draft.hero_layout_label;
  if (!CANDIDATE_HERO_SCREEN_TYPES.has(screenType)) return null;
  const knownTeamSize = candidateHeroKnownTeamSize(item, draft);
  const teamSize = knownTeamSize || (
    useInteractiveState && candidateHeroTeamSizeExplicit
      ? candidateHeroTeamSizeOverride
        : useInteractiveState && candidateHeroLineup &&
          candidateHeroLineup.screen_type === screenType
          ? candidateHeroLineup.team_size : null
  );
  return {screenType, teamSize};
}

async function ensureCandidateHeroCatalog() {
  if (candidateHeroCatalog.length) return candidateHeroCatalog;
  if (!candidateHeroCatalogPromise) {
    candidateHeroCatalogPromise = api('/api/training-review/heroes')
      .then((value) => {
        candidateHeroCatalog = value.heroes || [];
        return candidateHeroCatalog;
      })
      .finally(() => { candidateHeroCatalogPromise = null; });
  }
  return candidateHeroCatalogPromise;
}

function closeCandidateHeroPicker() {
  candidateHeroPickerSlot = null;
  $('#candidate-hero-picker').classList.add('hidden');
  renderCandidateBoxes();
}

function resetCandidateHeroReview() {
  candidateHeroLoadToken += 1;
  candidateHeroPrefillToken += 1;
  candidateHeroLineup = null;
  candidateHeroDraft = new Map();
  candidateHeroManualSlots = new Set();
  candidateHeroPlayerSlot = null;
  candidateHeroPlayerStatus = 'pending';
  candidateHeroAfkReviewRequired = false;
  candidateHeroDirty = false;
  candidateHeroLoading = false;
  candidateHeroPrefillRunning = false;
  candidateHeroGeometryRevision = 0;
  candidateHeroSlotRecognitionStates.clear();
  candidateHeroPendingRecognitionSlots.clear();
  if (candidateHeroRecognitionDebounceTimer !== null) {
    window.clearTimeout(candidateHeroRecognitionDebounceTimer);
    candidateHeroRecognitionDebounceTimer = null;
  }
  candidateHeroDrawMode = false;
  candidateHeroEdit = null;
  closeCandidateHeroPicker();
  $('#candidate-hero-review').classList.add('hidden');
  $('#candidate-hero-teams').innerHTML = '';
  $('#candidate-hero-status').textContent = '';
  renderCandidateBoxes();
}

function candidateHeroDisplay(label) {
  if (label === 'unreadable') {
    return {label, name: '看不清／无法确认', image_url: ''};
  }
  return candidateHeroByLabel(label);
}

function candidateHeroExpectedPositions(teamSize) {
  const result = [];
  for (const side of ['left', 'right']) {
    for (let slot = 1; slot <= teamSize; slot += 1) {
      result.push({side, slot});
    }
  }
  return result;
}

function candidateNextHeroPosition() {
  if (!candidateHeroLineup || !candidateHeroLineup.team_size) return null;
  const occupied = new Set(
    (candidateHeroLineup.slots || []).map((slot) =>
      candidateHeroKey(slot.side, slot.slot))
  );
  return candidateHeroExpectedPositions(candidateHeroLineup.team_size)
    .find((position) => !occupied.has(
      candidateHeroKey(position.side, position.slot))) || null;
}

function candidateHeroAllowsPartialLineup(draft = candidateDraft) {
  return Boolean(draft && (
    draft.match_kind_label === 'practice' ||
    (draft.result_panel_label === 'result_panel' &&
      draft.result_occlusion === 'occluded')
  ));
}

function candidateHeroLayoutComplete() {
  return Boolean(
    candidateHeroLineup &&
    candidateHeroLineup.team_size &&
    candidateHeroLineup.slots.length === candidateHeroLineup.team_size * 2
  );
}

function markCandidateHeroGeometryEdited() {
  candidateHeroGeometryRevision += 1;
  candidateHeroDirty = true;
}

function candidateHeroSameCrop(left, right) {
  return ['x', 'y', 'w', 'h'].every((name) =>
    Math.abs(Number(left && left[name]) - Number(right && right[name])) < 0.000001);
}

function candidateHeroChangedSlots(previousSlots, nextSlots) {
  const previous = new Map((previousSlots || []).map((slot) => [
    candidateHeroKey(slot.side, slot.slot), slot,
  ]));
  return (nextSlots || []).filter((slot) => {
    const old = previous.get(candidateHeroKey(slot.side, slot.slot));
    return !old || !candidateHeroSameCrop(old.crop, slot.crop);
  });
}

function clearCandidateHeroRecognition(slots) {
  (slots || []).forEach((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    candidateHeroDraft.delete(key);
    candidateHeroManualSlots.delete(key);
    slot.suggested_label = '';
    slot.suggestion_confidence = 0;
  });
}

function candidateHeroSlotIsRecognizing(slotKey) {
  const state = candidateHeroSlotRecognitionStates.get(slotKey);
  return Boolean(state && ['queued', 'running'].includes(state.status));
}

function candidateHeroRecognitionCount() {
  return [...candidateHeroSlotRecognitionStates.values()].filter(
    (state) => ['queued', 'running'].includes(state.status)).length;
}

function candidateHeroRecognitionTargets(slots) {
  return new Map((slots || []).map((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    const state = candidateHeroSlotRecognitionStates.get(key);
    return [key, {
      generation: Number(state && state.generation || 0),
      crop: {...slot.crop},
    }];
  }));
}

function setCandidateHeroRecognitionStatus(targets, status) {
  targets.forEach((target, key) => {
    const state = candidateHeroSlotRecognitionStates.get(key);
    if (!state || state.generation !== target.generation) return;
    candidateHeroSlotRecognitionStates.set(key, {...state, status});
  });
}

function removeCandidateHeroRecognitionState(slotKey) {
  candidateHeroSlotRecognitionStates.delete(slotKey);
  candidateHeroPendingRecognitionSlots.delete(slotKey);
}

function scheduleCandidateHeroRecognition(slots) {
  if (!slots || !slots.length) return;
  (slots || []).forEach((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    const previous = candidateHeroSlotRecognitionStates.get(key);
    const state = {
      generation: Number(previous && previous.generation || 0) + 1,
      status: 'queued',
      crop: {...slot.crop},
    };
    candidateHeroSlotRecognitionStates.set(key, state);
    candidateHeroPendingRecognitionSlots.set(key, state.generation);
  });
  if (candidateHeroRecognitionDebounceTimer !== null) {
    window.clearTimeout(candidateHeroRecognitionDebounceTimer);
  }
  candidateHeroRecognitionDebounceTimer = window.setTimeout(() => {
    candidateHeroRecognitionDebounceTimer = null;
    flushCandidateHeroRecognition();
  }, CANDIDATE_HERO_RECOGNITION_DEBOUNCE_MS);
  renderCandidateHeroLineup();
}

function flushCandidateHeroRecognition() {
  if (!candidateHeroLineup || !candidateHeroPendingRecognitionSlots.size) return;
  const pending = new Map(candidateHeroPendingRecognitionSlots);
  candidateHeroPendingRecognitionSlots.clear();
  const slots = candidateHeroLineup.slots.filter((slot) => {
    const key = candidateHeroKey(slot.side, slot.slot);
    const state = candidateHeroSlotRecognitionStates.get(key);
    return state && state.generation === pending.get(key) &&
      candidateHeroSameCrop(state.crop, slot.crop);
  });
  if (!slots.length) return;
  const targets = candidateHeroRecognitionTargets(slots);
  void persistCandidateHeroLayout(candidateHeroLineup.slots, {
    recognizeSlots: slots,
    recognitionTargets: targets,
  });
}

function candidateHeroCropCenter(crop) {
  return {
    x: crop.x + crop.w / 2,
    y: crop.y + crop.h / 2,
  };
}

function candidateHeroCropAtCenter(center, referenceCrop) {
  return {
    x: candidateHeroClamp(center.x - referenceCrop.w / 2, 0, 1 - referenceCrop.w),
    y: candidateHeroClamp(center.y - referenceCrop.h / 2, 0, 1 - referenceCrop.h),
    w: referenceCrop.w,
    h: referenceCrop.h,
  };
}

function candidateHeroLinkedSlots(
  originalSlots, selectedKey, editedCrop, screenType, mode) {
  const result = originalSlots.map((slot) => ({...slot, crop: {...slot.crop}}));
  const selected = result.find((slot) =>
    candidateHeroKey(slot.side, slot.slot) === selectedKey);
  if (!selected) return result;

  if (mode === 'resize') {
    result.forEach((slot) => {
      const center = candidateHeroCropCenter(slot.crop);
      slot.crop = candidateHeroCropAtCenter(center, editedCrop);
    });
    selected.crop = {...editedCrop};
    return result;
  }

  const editedCenter = candidateHeroCropCenter(editedCrop);
  if (['result_page', 'scoreboard'].includes(screenType)) {
    result.forEach((slot) => {
      const center = candidateHeroCropCenter(slot.crop);
      if (slot.side === selected.side) center.x = editedCenter.x;
      if (slot.slot === selected.slot) center.y = editedCenter.y;
      slot.crop = candidateHeroCropAtCenter(center, slot.crop);
    });
  } else if (screenType === 'gameplay_hud') {
    result.forEach((slot) => {
      const center = candidateHeroCropCenter(slot.crop);
      center.y = editedCenter.y;
      if (candidateHeroKey(slot.side, slot.slot) === selectedKey) {
        center.x = editedCenter.x;
      }
      slot.crop = candidateHeroCropAtCenter(center, slot.crop);
    });
    if (selected.slot === 2) {
      const anchor = result.find((slot) =>
        slot.side === selected.side && slot.slot === 1);
      if (anchor) {
        const anchorCenter = candidateHeroCropCenter(anchor.crop);
        const selectedCenter = candidateHeroCropCenter(selected.crop);
        const stepX = selectedCenter.x - anchorCenter.x;
        result.filter((slot) => slot.side === selected.side).forEach((slot) => {
          const center = candidateHeroCropCenter(slot.crop);
          center.x = anchorCenter.x + stepX * (slot.slot - 1);
          slot.crop = candidateHeroCropAtCenter(center, slot.crop);
        });
      }
    }
    return result;
  }
  selected.crop = {...editedCrop};
  return result;
}

function candidateHeroAutofillSlots(slots, screenType, teamSize) {
  const result = slots.map((slot) => ({...slot, crop: {...slot.crop}}));
  const reference = result[0];
  if (!reference) return result;
  const findSlot = (side, slot) => result.find((value) =>
    value.side === side && value.slot === slot);
  const addSlot = (side, slot, center) => {
    if (findSlot(side, slot)) return;
    result.push({
      side,
      slot,
      crop: candidateHeroCropAtCenter(center, reference.crop),
    });
  };

  if (screenType === 'gameplay_hud') {
    const rowAnchor = findSlot('left', 1) || findSlot('right', 1) || reference;
    const rowCenterY = candidateHeroCropCenter(rowAnchor.crop).y;
    result.forEach((slot) => {
      const center = candidateHeroCropCenter(slot.crop);
      slot.crop = candidateHeroCropAtCenter(
        {x: center.x, y: rowCenterY}, slot.crop);
    });
    for (const side of ['left', 'right']) {
      const first = findSlot(side, 1);
      const second = findSlot(side, 2);
      if (!first || !second) continue;
      const firstCenter = candidateHeroCropCenter(first.crop);
      const secondCenter = candidateHeroCropCenter(second.crop);
      const stepX = secondCenter.x - firstCenter.x;
      for (let slot = 3; slot <= teamSize; slot += 1) {
        addSlot(side, slot, {
          x: firstCenter.x + stepX * (slot - 1),
          y: rowCenterY,
        });
      }
    }
    return result;
  }

  const right1 = findSlot('right', 1);
  if (!right1) return result;
  const rightCenter = candidateHeroCropCenter(right1.crop);
  for (let slot = 2; slot <= teamSize; slot += 1) {
    const matchingLeft = findSlot('left', slot);
    if (!matchingLeft) return result;
    addSlot('right', slot, {
      x: rightCenter.x,
      y: candidateHeroCropCenter(matchingLeft.crop).y,
    });
  }
  return result;
}

function candidateCurrentImageUrl() {
  const image = $('#candidate-image');
  if (image && image.src) return image.src;
  const item = currentCandidate();
  return item ? candidateImageUrl(item) : '';
}

function candidateHeroCropPreview(box, alt) {
  const crop = document.createElement('div');
  crop.className = 'candidate-hero-crop';
  crop.title = '截图中圈出的原始头像';
  crop.setAttribute('role', 'img');
  crop.setAttribute('aria-label', alt);
  const width = Math.max(0.0001, Number(box && box.w) || 0);
  const height = Math.max(0.0001, Number(box && box.h) || 0);
  const x = Number(box && box.x) || 0;
  const y = Number(box && box.y) || 0;
  const source = document.createElement('img');
  source.className = 'candidate-hero-crop-source';
  source.src = candidateCurrentImageUrl();
  source.alt = '';
  source.draggable = false;
  source.style.width = `${100 / width}%`;
  source.style.height = `${100 / height}%`;
  source.style.left = `${(-100 * x) / width}%`;
  source.style.top = `${(-100 * y) / height}%`;
  crop.appendChild(source);
  return crop;
}

function renderCandidateHeroLineup() {
  const review = $('#candidate-hero-review');
  const teams = $('#candidate-hero-teams');
  const tools = $('.candidate-hero-tools');
  const progress = $('#candidate-hero-progress');
  const progressText = $('#candidate-hero-progress-text');
  const busy = candidateHeroLoading || candidateHeroPrefillRunning;
  const recognizingCount = candidateHeroRecognitionCount();
  progress.classList.toggle('hidden', !busy && !recognizingCount);
  progressText.textContent = candidateHeroPrefillRunning
    ? '正在用模型识别头像位置和英雄…'
    : candidateHeroLoading
      ? '正在读取英雄标注…'
      : `${recognizingCount} 个头像正在后台识别，可继续画框或调整`;
  teams.innerHTML = '';
  const context = candidateHeroContext(currentCandidate());
  if (!context) {
    review.classList.toggle('hidden', !currentCandidate() || !candidateDraft);
    teams.classList.add('hidden');
    tools.classList.add('hidden');
    $('#candidate-hero-status').textContent =
      '先选择头像来源；没有可标头像时可选“无头像”或“看不清”。';
    renderCandidateBoxes();
    return;
  }
  review.classList.remove('hidden');
  tools.classList.remove('hidden');
  const recognizeButton = $('#btn-candidate-hero-recognize');
  const drawButton = $('#btn-candidate-hero-draw');
  const clearButton = $('#btn-candidate-hero-clear');
  const playerUnreadable = $('#btn-candidate-player-unreadable');
  recognizeButton.textContent = candidateHeroPrefillRunning
    ? 'AI 识别中…' : 'AI 识别';
  recognizeButton.disabled = busy || !context.teamSize;
  if (!candidateHeroLineup) {
    teams.classList.add('hidden');
    drawButton.classList.remove('hidden');
    drawButton.disabled = true;
    clearButton.disabled = true;
    playerUnreadable.classList.add('hidden');
    $('#candidate-hero-status').textContent = candidateHeroLoading
      ? '正在读取本图的模型结果和人工标注…'
      : '请先选择每队 3 人或每队 5 人。';
    renderCandidateBoxes();
    return;
  }
  teams.classList.remove('hidden');
  const recognized = candidateHeroLineup.slots.filter(
    (slot) => slot.suggested_label).length;
  const screenName = CANDIDATE_HERO_LAYOUTS[candidateHeroLineup.screen_type]
    || candidateHeroLineup.screen_type;
  const complete = candidateHeroLayoutComplete();
  const allowsPartial = candidateHeroAllowsPartialLineup(candidateDraft);
  const marksPlayer = candidateDraft.view_context_label === 'played' &&
    ['scoreboard', 'result_page'].includes(candidateHeroLineup.screen_type);
  const marksAfk = candidateHeroLineup.screen_type === 'result_page';
  const playerPosition = candidateHeroPlayerPosition();
  playerUnreadable.classList.toggle('hidden', !marksPlayer);
  playerUnreadable.classList.toggle(
    'selected', marksPlayer && candidateHeroPlayerStatus === 'unreadable');
  playerUnreadable.classList.remove('needs-attention');
  playerUnreadable.disabled = candidateHeroLoading;
  playerUnreadable.setAttribute(
    'aria-pressed', String(candidateHeroPlayerStatus === 'unreadable'));
  const next = candidateNextHeroPosition();
  const status = candidateHeroLineup.review_status === 'confirmed'
    ? '阵容已经人工确认；修改任意下拉框后会更新。'
    : allowsPartial && candidateHeroLineup.slots.length
      ? `已标出 ${candidateHeroLineup.slots.length} 个可见头像；` +
        '当前场景只需确认实际能看见的头像。'
    : complete
      ? `算法预填 ${recognized}/${candidateHeroLineup.slots.length} 个；` +
        '正确的不用改，只修改错误或空白的位置。'
      : next
        ? `已画 ${candidateHeroLineup.slots.length}/` +
          `${candidateHeroLineup.team_size * 2} 个；下一框是` +
          `${next.side === 'left' ? '左队' : '右队'}第 ${next.slot} 个。`
        : '还没有英雄圆框。';
  const drawingHint = !complete && !allowsPartial
    ? candidateHeroLineup.slots.length
      ? ' 后续圆框沿用第一个大小，可直接点头像中心。'
      : ' 先拖出第一个圆框确定大小。'
    : '';
  const playerHint = !marksPlayer ? ''
    : candidateHeroPlayerStatus === 'unreadable'
      ? ' 主播本人位置：看不清。'
      : playerPosition
        ? ` 主播本人：${playerPosition.side === 'left' ? '左' : '右'}队第 ` +
          `${playerPosition.slot} 个。`
        : ' 请点击“设为本人”，或选择“本人看不清”。';
  const editHint = candidateHeroLineup.screen_type === 'gameplay_hud'
    ? ' 上下拖任意框会统一整排高度；拖左右第 2 个框调整本队间距；' +
      '拖其他框只微调横向位置；拖黄点统一缩放。'
    : ' 拖圆框移动，拖黄点缩放。';
  $('#candidate-hero-status').textContent =
    `${screenName} · ${candidateHeroLineup.team_size}V${candidateHeroLineup.team_size} · ` +
    `${status}${drawingHint}${playerHint}${editHint}`;
  drawButton.disabled = candidateHeroLoading || complete;
  drawButton.classList.toggle('hidden', complete);
  drawButton.classList.toggle('selected', candidateHeroDrawMode);
  drawButton.textContent = candidateHeroDrawMode && next
      ? `正在画${next.side === 'left' ? '左' : '右'}${next.slot}`
      : '补画头像';
  clearButton.disabled = candidateHeroLoading || !candidateHeroLineup.slots.length;
  for (const side of ['left', 'right']) {
    const team = document.createElement('section');
    team.className = 'candidate-hero-team';
    const title = document.createElement('div');
    title.className = 'candidate-hero-team-title';
    title.textContent = side === 'left' ? '左队' : '右队';
    team.appendChild(title);
    const slots = document.createElement('div');
    slots.className = `candidate-hero-slots team-size-${candidateHeroLineup.team_size}`;
    candidateHeroLineup.slots.filter((slot) => slot.side === side).forEach((slot) => {
      const key = candidateHeroKey(slot.side, slot.slot);
      const selected = candidateHeroDraft.get(key) || '';
      const hero = candidateHeroDisplay(selected);
      const isPlayer = marksPlayer &&
        candidateHeroPlayerStatus === 'identified' &&
        candidateHeroPlayerSlot === key;
      const card = document.createElement('article');
      card.className = 'candidate-hero-slot';
      card.dataset.heroSlot = key;
      card.classList.toggle('player', isPlayer);
      const recognizing = candidateHeroSlotIsRecognizing(key);
      card.classList.toggle('recognizing', recognizing);
      card.setAttribute('aria-busy', String(recognizing));
      const index = document.createElement('span');
      index.className = 'candidate-hero-slot-index';
      index.textContent = String(slot.slot);
      card.appendChild(index);
      const comparison = document.createElement('div');
      comparison.className = 'candidate-hero-comparison';
      comparison.setAttribute('aria-label', '截图头像与当前标注头像对照');
      const crop = candidateHeroCropPreview(
        slot.crop,
        `${side === 'left' ? '左队' : '右队'}第 ${slot.slot} 个截图头像`,
      );
      comparison.appendChild(crop);
      if (hero && hero.image_url) {
        const reference = document.createElement('img');
        reference.className = 'candidate-hero-reference';
        reference.src = hero.image_url;
        reference.alt = `当前标注的标准头像：${hero.name} · ${hero.label}`;
        reference.title = reference.alt;
        reference.draggable = false;
        comparison.appendChild(reference);
      } else {
        const reference = document.createElement('span');
        reference.className = 'candidate-hero-reference empty';
        reference.textContent = '?';
        reference.title = hero ? '当前标注为看不清' : '尚未选择英雄';
        reference.setAttribute('aria-label', reference.title);
        comparison.appendChild(reference);
      }
      card.appendChild(comparison);
      const details = document.createElement('div');
      details.className = 'candidate-hero-slot-details';
      details.classList.toggle('with-player-action', marksPlayer || marksAfk);
      const select = document.createElement('button');
      select.type = 'button';
      select.className = 'candidate-hero-select';
      select.dataset.heroSlot = key;
      select.classList.toggle('missing', !hero);
      const name = document.createElement('span');
      name.className = 'candidate-hero-selected-name';
      name.textContent = hero
        ? `${hero.name}${hero.label === 'unreadable' ? '' : ` · ${hero.label}`}`
        : recognizing ? 'AI 识别中…' : '请选择英雄';
      select.title = name.textContent;
      select.appendChild(name);
      if (hero && selected === slot.suggested_label) {
        const identityConfidence = document.createElement('span');
        identityConfidence.className = 'candidate-hero-identity-confidence';
        identityConfidence.textContent =
          `${(Number(slot.suggestion_confidence || 0) * 100).toFixed(1)}%`;
        identityConfidence.title =
          `英雄识别置信度 ${identityConfidence.textContent}`;
        identityConfidence.setAttribute('aria-label', identityConfidence.title);
        select.appendChild(identityConfidence);
        select.title += ` · ${identityConfidence.title}`;
      }
      select.onclick = () => openCandidateHeroPicker(select, key);
      details.appendChild(select);
      const slotActions = document.createElement('div');
      slotActions.className = 'candidate-hero-slot-actions';
      if (marksPlayer) {
        const playerButton = document.createElement('button');
        playerButton.type = 'button';
        playerButton.className = 'candidate-hero-player';
        playerButton.classList.toggle('selected', isPlayer);
        playerButton.dataset.heroSlot = key;
        playerButton.setAttribute('aria-pressed', String(isPlayer));
        playerButton.textContent = isPlayer ? '✓ 本人' : '设为本人';
        playerButton.title = isPlayer
          ? '这个英雄是主播本人使用的英雄'
          : '将这个英雄标记为主播本人使用的英雄';
        playerButton.onclick = () => {
          candidateHeroPlayerStatus = 'identified';
          candidateHeroPlayerSlot = key;
          candidateHeroDirty = true;
          $('#candidate-save-state').classList.remove('error');
          $('#candidate-save-state').textContent = '';
          renderCandidateHeroLineup();
        };
        slotActions.appendChild(playerButton);
      }
      if (marksAfk) {
        const afkButton = document.createElement('button');
        const isAfk = slot.is_afk === true;
        afkButton.type = 'button';
        afkButton.className = 'candidate-hero-afk';
        afkButton.classList.toggle('selected', isAfk);
        afkButton.dataset.heroSlot = key;
        afkButton.setAttribute('aria-pressed', String(isAfk));
        afkButton.textContent = isAfk ? '✓ 挂机' : '挂机';
        afkButton.title = isAfk
          ? '这个位置已标记为挂机，再点一次取消'
          : '这个位置的玩家挂机时勾选';
        afkButton.onclick = () => {
          slot.is_afk = !isAfk;
          candidateHeroDirty = true;
          $('#candidate-save-state').classList.remove('error');
          $('#candidate-save-state').textContent = '';
          renderCandidateHeroLineup();
        };
        slotActions.appendChild(afkButton);
        const afkPrediction = document.createElement('span');
        afkPrediction.className = 'muted small candidate-hero-afk-prediction';
        const predictionStatus = slot.afk_prediction_status || 'pending';
        if (predictionStatus === 'succeeded' &&
            slot.afk_prediction_probability !== null &&
            slot.afk_prediction_probability !== undefined) {
          afkPrediction.textContent =
            `${(Number(slot.afk_prediction_probability) * 100).toFixed(1)}%`;
          afkPrediction.title =
            `挂机概率 ${afkPrediction.textContent} · ` +
            `预测 ${slot.afk_prediction_label || '未知'} · ` +
            `模型 ${slot.afk_prediction_model_run_id || '未知版本'}`;
        } else if (predictionStatus === 'failed') {
          afkPrediction.textContent = '失败';
          afkPrediction.title = slot.afk_prediction_error || '挂机模型运行失败';
        } else {
          afkPrediction.textContent = predictionStatus === 'running'
            ? '识别中' : predictionStatus === 'queued' ? '排队中' : '待识别';
        }
        slotActions.appendChild(afkPrediction);
      }
      if (slotActions.childElementCount) details.appendChild(slotActions);
      card.appendChild(details);
      slots.appendChild(card);
    });
    team.appendChild(slots);
    teams.appendChild(team);
  }
  renderCandidateBoxes();
}

function renderCandidateHeroOptions() {
  const options = $('#candidate-hero-options');
  options.innerHTML = '';
  if (!candidateHeroPickerSlot) return;
  const query = $('#candidate-hero-search').value.trim().toLocaleLowerCase();
  const current = candidateHeroDraft.get(candidateHeroPickerSlot) || '';
  const values = [
    {label: 'unreadable', name: '看不清／无法确认', image_url: ''},
    ...candidateHeroCatalog,
  ].filter((hero) => !query ||
    `${hero.name} ${hero.label}`.toLocaleLowerCase().includes(query));
  values.forEach((hero) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'candidate-hero-option';
    button.classList.toggle('selected', hero.label === current);
    if (hero.image_url) {
      const image = document.createElement('img');
      image.src = hero.image_url;
      image.alt = '';
      image.loading = 'lazy';
      button.appendChild(image);
    }
    const names = document.createElement('span');
    names.textContent = hero.name;
    if (hero.label !== 'unreadable') {
      const english = document.createElement('small');
      english.textContent = hero.label;
      names.appendChild(english);
    }
    button.appendChild(names);
    button.onclick = () => {
      candidateHeroDraft.set(candidateHeroPickerSlot, hero.label);
      candidateHeroManualSlots.add(candidateHeroPickerSlot);
      candidateHeroDirty = true;
      $('#candidate-save-state').classList.remove('error');
      $('#candidate-save-state').textContent = '';
      closeCandidateHeroPicker();
      renderCandidateHeroLineup();
    };
    options.appendChild(button);
  });
}

function openCandidateHeroPicker(anchor, slotKey) {
  candidateHeroPickerSlot = slotKey;
  $('#btn-candidate-hero-delete').disabled = false;
  $('#candidate-hero-search').value = '';
  renderCandidateHeroOptions();
  const picker = $('#candidate-hero-picker');
  picker.classList.remove('hidden');
  const anchorRect = anchor.getBoundingClientRect();
  const pickerRect = picker.getBoundingClientRect();
  const left = Math.max(
    12, Math.min(window.innerWidth - pickerRect.width - 12, anchorRect.left));
  const below = anchorRect.bottom + 6;
  const top = below + pickerRect.height <= window.innerHeight - 12
    ? below : Math.max(12, anchorRect.top - pickerRect.height - 6);
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
  renderCandidateBoxes();
  $('#candidate-hero-search').focus();
}

function showCandidateMissingHero(label) {
  const key = candidateHeroKey(label.side, label.slot);
  const sideName = label.side === 'left' ? '左队' : '右队';
  const saveState = $('#candidate-save-state');
  saveState.classList.add('error');
  saveState.textContent =
    `${sideName}第 ${label.slot} 个英雄还没选，已为你打开选择面板`;
  const select = [...$$('.candidate-hero-select')]
    .find((value) => value.dataset.heroSlot === key);
  if (!select) return;
  const card = select.closest('.candidate-hero-slot');
  if (card) card.classList.add('needs-attention');
  select.scrollIntoView({block: 'center', inline: 'nearest'});
  requestAnimationFrame(() => openCandidateHeroPicker(select, key));
}

function showCandidateMissingPlayerHero() {
  showCandidateSaveError(
    '请标出主播本人，或选择“本人看不清”');
  const buttons = $$('.candidate-hero-player');
  buttons.forEach((button) => button.classList.add('needs-attention'));
  $('#btn-candidate-player-unreadable').classList.add('needs-attention');
  if (buttons[0]) buttons[0].scrollIntoView({block: 'center', inline: 'nearest'});
}

function candidateHeroPrefetchKey(item, context, recognize = false) {
  return [
    Number(item && item.frame_id),
    context && context.screenType || '',
    Number(context && context.teamSize || 0),
    recognize ? 'recognize' : 'read',
  ].join(':');
}

function candidateHeroLineupUrl(
  item, context, {recognize = false, refresh = false} = {}) {
  const query = new URLSearchParams({screen_type: context.screenType});
  if (context.teamSize) query.set('team_size', String(context.teamSize));
  if (recognize) query.set('recognize', 'true');
  if (refresh) query.set('refresh', 'true');
  return `/api/training-review/items/${item.frame_id}/hero-lineup?${query}`;
}

function prepareCandidateHeroLineup(
  item, context, {recognize = false, refresh = false} = {}) {
  const key = candidateHeroPrefetchKey(item, context, recognize);
  if (recognize) {
    const previous = candidateHeroPrefetchRequests.get(key);
    if (previous) previous.controller.abort();
    candidateHeroPrefetchRequests.delete(key);
  }
  const existing = candidateHeroPrefetchRequests.get(key);
  if (existing) return existing;
  const controller = new AbortController();
  const entry = {
    key,
    frameId: Number(item.frame_id),
    controller,
    initialPromise: Promise.all([
      ensureCandidateHeroCatalog(),
      api(candidateHeroLineupUrl(item, context, {recognize, refresh}), {
        signal: controller.signal,
      }),
    ]).then(([, lineup]) => lineup),
    finalPromise: null,
  };
  entry.initialPromise.catch(() => {
    if (candidateHeroPrefetchRequests.get(key) === entry) {
      candidateHeroPrefetchRequests.delete(key);
    }
  });
  candidateHeroPrefetchRequests.set(key, entry);
  return entry;
}

function completeCandidateHeroLineupPrefetch(item, context, entry) {
  if (entry.finalPromise) return entry.finalPromise;
  entry.finalPromise = entry.initialPromise.then(async (lineup) => {
    const jobId = String((lineup.prefill_job || {}).id || '');
    if (!jobId) return {lineup, refreshed: false};
    const finished = await waitForVisionJob(
      jobId, 60000, entry.controller.signal);
    if (!finished || finished.status !== 'succeeded') {
      return {lineup, refreshed: false};
    }
    const application = (finished.result || {}).application || {};
    if (application.applied === false) {
      throw new Error(application.reason || '模型结果没有应用');
    }
    const refreshed = await api(candidateHeroLineupUrl(item, context), {
      signal: entry.controller.signal,
    });
    delete refreshed.prefill_job;
    entry.initialPromise = Promise.resolve(refreshed);
    entry.finalPromise = null;
    return {lineup: refreshed, refreshed: true};
  });
  entry.finalPromise.catch(() => {
    if (candidateHeroPrefetchRequests.get(entry.key) === entry) {
      candidateHeroPrefetchRequests.delete(entry.key);
    }
  });
  return entry.finalPromise;
}

function applyCandidateHeroLineup(item, context, lineup, previousDraft = null) {
  lineup.slots ||= [];
  const previousAfk = new Map(
    ((candidateHeroLineup && candidateHeroLineup.slots) || [])
      .filter((slot) => typeof slot.is_afk === 'boolean')
      .map((slot) => [
        candidateHeroKey(slot.side, slot.slot), slot.is_afk,
      ]),
  );
  lineup.slots.forEach((slot) => {
    const previous = previousAfk.get(candidateHeroKey(slot.side, slot.slot));
    if (slot.is_afk == null && typeof previous === 'boolean') {
      slot.is_afk = previous;
    }
  });
  candidateHeroLineup = lineup;
  candidateHeroAfkReviewRequired =
    lineup.screen_type === 'result_page' &&
    lineup.slots.some((slot) => slot.is_afk == null);
  candidateHeroPlayerStatus = candidateHeroPlayerStatusForLineup(lineup);
  candidateHeroPlayerSlot = candidateHeroPlayerKey(lineup);
  if (candidateHeroPlayerStatus === 'pending' && lineup.player_suggestion) {
    const suggestion = lineup.player_suggestion;
    const suggestedKey = candidateHeroKey(
      suggestion.side, Number(suggestion.slot));
    if (lineup.slots.some((slot) =>
      candidateHeroKey(slot.side, slot.slot) === suggestedKey)) {
      candidateHeroPlayerStatus = 'identified';
      candidateHeroPlayerSlot = suggestedKey;
    }
  }
  if (!context.teamSize && lineup.team_size) {
    candidateHeroTeamSizeExplicit = true;
    candidateHeroTeamSizeOverride = lineup.team_size;
    renderCandidateChoices();
  }
  candidateHeroDraft = candidateHeroDraftForLineup(
    item, lineup, previousDraft || new Map());
}

async function loadCandidateHeroLineup(item, contextOverride = null) {
  const context = contextOverride || candidateHeroContext(item);
  if (!context) {
    resetCandidateHeroReview();
    renderCandidateHeroContextControls();
    renderCandidateHeroLineup();
    $('#btn-candidate-save').disabled = false;
    return;
  }
  const token = ++candidateHeroLoadToken;
  candidateHeroGeometryRevision += 1;
  candidateHeroLoading = true;
  candidateHeroLineup = null;
  candidateHeroDraft = new Map();
  candidateHeroManualSlots = new Set();
  candidateHeroPlayerSlot = null;
  candidateHeroPlayerStatus = 'pending';
  candidateHeroAfkReviewRequired = false;
  candidateHeroDirty = false;
  candidateHeroDrawMode = false;
  closeCandidateHeroPicker();
  $('#candidate-hero-review').classList.remove('hidden');
  $('#candidate-hero-teams').innerHTML = '';
  $('#candidate-hero-status').textContent = '正在读取本图的模型结果和人工标注…';
  $('#btn-candidate-save').disabled = true;
  renderCandidateHeroLineup();
  const entry = prepareCandidateHeroLineup(item, context);
  try {
    const lineup = await entry.initialPromise;
    if (token !== candidateHeroLoadToken || currentCandidate() !== item) return;
    if (!lineup.applicable) {
      resetCandidateHeroReview();
      $('#btn-candidate-save').disabled = false;
      return;
    }
    if (lineup.needs_team_size) {
      candidateHeroLineup = null;
      renderCandidateHeroLineup();
      return;
    }
    applyCandidateHeroLineup(item, context, lineup);
    renderCandidateHeroLineup();
  } catch (error) {
    if (token !== candidateHeroLoadToken || currentCandidate() !== item) return;
    candidateHeroLineup = null;
    $('#candidate-hero-review').classList.remove('hidden');
    $('#candidate-hero-status').textContent = '英雄标注读取失败：' + error.message;
  } finally {
    if (token === candidateHeroLoadToken) {
      candidateHeroLoading = false;
      $('#btn-candidate-save').disabled = false;
      renderCandidateHeroLineup();
    }
  }
}

async function recognizeCandidateHeroes() {
  const item = currentCandidate();
  const context = candidateHeroContext(item);
  if (!item || !context || !context.teamSize || candidateHeroPrefillRunning) {
    if (item && context && !context.teamSize) {
      $('#candidate-hero-status').textContent =
        '请先选择每队 3 人或每队 5 人，再使用 AI 识别。';
    }
    return;
  }
  const prefillToken = ++candidateHeroPrefillToken;
  const loadToken = candidateHeroLoadToken;
  const geometryRevision = candidateHeroGeometryRevision;
  const previousDraft = new Map(candidateHeroDraft);
  const previousPlayerSlot = candidateHeroPlayerSlot;
  const previousPlayerStatus = candidateHeroPlayerStatus;
  const previousDirty = candidateHeroDirty;
  candidateHeroPrefillRunning = true;
  $('#candidate-save-state').classList.remove('error');
  $('#candidate-save-state').textContent = '正在用 AI 识别头像位置和英雄…';
  renderCandidateHeroLineup();
  try {
    const entry = prepareCandidateHeroLineup(
      item, context, {recognize: true, refresh: true});
    const initial = await entry.initialPromise;
    const queuedJob = Boolean((initial.prefill_job || {}).id);
    const completed = await completeCandidateHeroLineupPrefetch(
      item, context, entry);
    if (queuedJob && !completed.refreshed) {
      throw new Error('模型任务未成功完成，请稍后重试');
    }
    if (prefillToken !== candidateHeroPrefillToken ||
        loadToken !== candidateHeroLoadToken || currentCandidate() !== item ||
        geometryRevision !== candidateHeroGeometryRevision) return;
    const lineup = completed.lineup;
    if (!lineup || !lineup.applicable || lineup.needs_team_size) return;
    candidateHeroManualSlots.clear();
    applyCandidateHeroLineup(item, context, lineup, previousDraft);
    const previousPlayerStillExists = previousPlayerSlot && lineup.slots.some(
      (slot) => candidateHeroKey(slot.side, slot.slot) === previousPlayerSlot);
    if (previousPlayerStatus === 'identified' && previousPlayerStillExists) {
      candidateHeroPlayerStatus = 'identified';
      candidateHeroPlayerSlot = previousPlayerSlot;
    } else if (previousPlayerStatus === 'unreadable') {
      candidateHeroPlayerStatus = 'unreadable';
      candidateHeroPlayerSlot = null;
    }
    candidateHeroDirty = previousDirty;
    candidateHeroPrefetchRequests.delete(
      candidateHeroPrefetchKey(item, context));
    const found = lineup.slots.length;
    const expected = Number(lineup.team_size || context.teamSize) * 2;
    $('#candidate-save-state').textContent = found
      ? `AI 识别完成，找到 ${found}/${expected} 个头像，请核对或补画。`
      : 'AI 没有识别到头像，请使用“补画头像”。';
  } catch (error) {
    if (currentCandidate() === item) {
      $('#candidate-save-state').classList.add('error');
      $('#candidate-save-state').textContent =
        'AI 识别失败：' + error.message;
    }
  } finally {
    if (prefillToken === candidateHeroPrefillToken) {
      candidateHeroPrefillRunning = false;
      renderCandidateHeroLineup();
    }
  }
}

function refreshCandidateHeroReview() {
  const item = currentCandidate();
  const context = candidateHeroContext(item);
  if (!context) {
    resetCandidateHeroReview();
    renderCandidateHeroContextControls();
    renderCandidateHeroLineup();
    $('#btn-candidate-save').disabled = !item;
    return;
  }
  if (candidateHeroLineup &&
      candidateHeroLineup.screen_type === context.screenType &&
      (!context.teamSize || candidateHeroLineup.team_size === context.teamSize)) {
    $('#candidate-hero-review').classList.remove('hidden');
    renderCandidateHeroLineup();
    return;
  }
  loadCandidateHeroLineup(item);
}

function candidateHeroSlotsPayload(slots = null) {
  return (slots || (candidateHeroLineup && candidateHeroLineup.slots) || [])
    .map((slot) => ({
      side: slot.side,
      slot: slot.slot,
      crop: {...slot.crop},
    }));
}

function applyCandidateHeroRecognitionResult(lineup, targets) {
  if (!candidateHeroLineup || !lineup || !targets.size) return;
  const recognized = new Map((lineup.slots || []).map((slot) => [
    candidateHeroKey(slot.side, slot.slot), slot,
  ]));
  targets.forEach((target, key) => {
    const state = candidateHeroSlotRecognitionStates.get(key);
    const current = candidateHeroSlot(key);
    const incoming = recognized.get(key);
    if (!state || state.generation !== target.generation || !current ||
        !candidateHeroSameCrop(current.crop, target.crop) || !incoming ||
        !candidateHeroSameCrop(incoming.crop, target.crop)) return;
    current.suggested_label = incoming.suggested_label || '';
    current.suggestion_confidence = Number(
      incoming.suggestion_confidence || 0);
    if (!candidateHeroManualSlots.has(key)) {
      if (current.suggested_label) {
        candidateHeroDraft.set(key, current.suggested_label);
      } else {
        candidateHeroDraft.delete(key);
      }
    }
    candidateHeroSlotRecognitionStates.set(key, {...state, status: 'done'});
  });
}

async function persistCandidateHeroLayout(
  slots, {recognizeSlots = [], recognitionTargets = new Map()} = {}) {
  const item = currentCandidate();
  const context = candidateHeroContext(item);
  if (!item || !context || !context.teamSize) return false;
  const loadToken = candidateHeroLoadToken;
  const slotPayload = candidateHeroSlotsPayload(slots);
  const recognizePayload = candidateHeroSlotsPayload(recognizeSlots);
  const targets = new Map(recognitionTargets);
  const image = $('#candidate-image');
  const imageWidth = Number(image && image.naturalWidth || item.width || 0);
  const imageHeight = Number(image && image.naturalHeight || item.height || 0);
  const save = async () => {
    setCandidateHeroRecognitionStatus(targets, 'running');
    if (currentCandidate() === item && loadToken === candidateHeroLoadToken) {
      renderCandidateHeroLineup();
    }
    try {
      const lineup = await api(
        `/api/training-review/items/${item.frame_id}/hero-layout`, {
          method: 'PUT',
          body: JSON.stringify({
            screen_type: context.screenType,
            team_size: context.teamSize,
            slots: slotPayload,
            recognize: recognizePayload.length > 0,
            recognize_slots: recognizePayload,
            image_width: imageWidth,
            image_height: imageHeight,
          }),
        });
      const active = currentCandidate() === item &&
        loadToken === candidateHeroLoadToken;
      const queuedJobId = String((lineup.prefill_job || {}).id || '');
      if (active && queuedJobId) {
        void refreshCandidateHeroLayoutAfterWorker(
          item, queuedJobId, targets, loadToken);
      } else if (active && targets.size) {
        applyCandidateHeroRecognitionResult(lineup, targets);
      }
      if (active) candidateHeroDirty = true;
      return true;
    } catch (error) {
      setCandidateHeroRecognitionStatus(targets, 'failed');
      if (currentCandidate() === item && loadToken === candidateHeroLoadToken) {
        $('#candidate-save-state').textContent =
          '英雄圆框保存失败：' + error.message;
      }
      return false;
    } finally {
      if (currentCandidate() === item && loadToken === candidateHeroLoadToken) {
        renderCandidateHeroLineup();
        renderCandidateChoices();
      }
    }
  };
  const queued = candidateHeroPersistQueue.then(save, save);
  candidateHeroPersistQueue = queued.then(() => undefined, () => undefined);
  return queued;
}

async function refreshCandidateHeroLayoutAfterWorker(
  item, jobId, targets, loadToken) {
  try {
    const finished = await waitForVisionJob(jobId);
    if (!finished || finished.status !== 'succeeded' ||
        currentCandidate() !== item ||
        loadToken !== candidateHeroLoadToken) return;
    const context = candidateHeroContext(item);
    if (!context) return;
    const query = new URLSearchParams({screen_type: context.screenType});
    if (context.teamSize) query.set('team_size', String(context.teamSize));
    const lineup = await api(
      `/api/training-review/items/${item.frame_id}/hero-lineup?${query}`);
    if (currentCandidate() !== item || !lineup.applicable ||
        loadToken !== candidateHeroLoadToken) return;
    applyCandidateHeroRecognitionResult(lineup, targets);
    renderCandidateHeroLineup();
  } catch (_error) {
    // Worker 不在线时继续保留人工选择，不把异步错误盖到当前操作上。
  } finally {
    if (currentCandidate() === item && loadToken === candidateHeroLoadToken) {
      targets.forEach((target, key) => {
        const state = candidateHeroSlotRecognitionStates.get(key);
        if (state && state.generation === target.generation &&
            state.status === 'running') {
          candidateHeroSlotRecognitionStates.set(
            key, {...state, status: 'failed'});
        }
      });
      renderCandidateHeroLineup();
    }
  }
}

function addCandidateHeroCircle(crop) {
  const next = candidateNextHeroPosition();
  if (!next || !candidateHeroLineup) return;
  const previousSlots = candidateHeroLineup.slots.map((slot) => ({
    ...slot, crop: {...slot.crop},
  }));
  const manuallyAdded = [
    ...candidateHeroLineup.slots,
    {...next, crop},
  ];
  const slots = candidateHeroAutofillSlots(
    manuallyAdded,
    candidateHeroLineup.screen_type,
    candidateHeroLineup.team_size,
  );
  const complete = slots.length === candidateHeroLineup.team_size * 2;
  const addedSlots = candidateHeroChangedSlots(previousSlots, slots);
  clearCandidateHeroRecognition(addedSlots);
  markCandidateHeroGeometryEdited();
  candidateHeroLineup.slots = slots;
  scheduleCandidateHeroRecognition(addedSlots);
  renderCandidateHeroLineup();
  if (complete) {
    candidateHeroDrawMode = false;
    const automaticallyAdded = slots.length - manuallyAdded.length;
    if (automaticallyAdded > 0) {
      $('#candidate-save-state').textContent =
        `已自动补齐 ${automaticallyAdded} 个英雄圆框，正在识别新增头像`;
    }
  }
  renderCandidateHeroLineup();
}

async function deleteCandidateHeroSlot() {
  if (!candidateHeroPickerSlot || !candidateHeroLineup) return;
  const key = candidateHeroPickerSlot;
  const slots = candidateHeroLineup.slots.filter((slot) =>
    candidateHeroKey(slot.side, slot.slot) !== key);
  candidateHeroDraft.delete(key);
  candidateHeroManualSlots.delete(key);
  if (candidateHeroPlayerSlot === key) {
    candidateHeroPlayerSlot = null;
    candidateHeroPlayerStatus = 'pending';
  }
  closeCandidateHeroPicker();
  markCandidateHeroGeometryEdited();
  candidateHeroLineup.slots = slots;
  removeCandidateHeroRecognitionState(key);
  candidateHeroDrawMode = true;
  void persistCandidateHeroLayout(slots);
  renderCandidateHeroLineup();
}

async function clearCandidateHeroLayout() {
  if (!candidateHeroLineup || !candidateHeroLineup.slots.length) return;
  candidateHeroDraft = new Map();
  candidateHeroManualSlots = new Set();
  candidateHeroPlayerSlot = null;
  candidateHeroPlayerStatus = 'pending';
  closeCandidateHeroPicker();
  markCandidateHeroGeometryEdited();
  candidateHeroLineup.slots = [];
  candidateHeroSlotRecognitionStates.clear();
  candidateHeroPendingRecognitionSlots.clear();
  if (candidateHeroRecognitionDebounceTimer !== null) {
    window.clearTimeout(candidateHeroRecognitionDebounceTimer);
    candidateHeroRecognitionDebounceTimer = null;
  }
  candidateHeroDrawMode = true;
  void persistCandidateHeroLayout([]);
  renderCandidateHeroLineup();
}

function renderCandidateHeroContextControls() {
  const section = $('#candidate-hero-context');
  const review = $('#candidate-hero-review');
  const layoutActions = $('#candidate-hero-layout-actions');
  const sizeGroup = $('#candidate-hero-size-group');
  const sizeActions = $('#candidate-hero-size-actions');
  layoutActions.innerHTML = '';
  sizeActions.innerHTML = '';
  const item = currentCandidate();
  if (!item || !candidateDraft) {
    section.classList.add('hidden');
    review.classList.add('hidden');
    return;
  }
  review.classList.remove('hidden');
  section.classList.remove('hidden');
  Object.entries(CANDIDATE_HERO_LAYOUTS).forEach(([value, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = CANDIDATE_HERO_LAYOUT_SHORT_LABELS[value] || label;
    button.title = label;
    button.classList.toggle(
      'selected', candidateDraft.hero_layout_label === value);
    button.onclick = () => selectCandidateHeroLayout(value);
    layoutActions.appendChild(button);
  });
  const hasHeroLayout = CANDIDATE_HERO_SCREEN_TYPES.has(
    candidateDraft.hero_layout_label);
  const teamSizeAlreadyKnown = Boolean(candidateHeroKnownTeamSize(item));
  const needsTeamSizeChoice = hasHeroLayout && !teamSizeAlreadyKnown;
  sizeGroup.classList.toggle('hidden', !needsTeamSizeChoice);
  if (needsTeamSizeChoice) {
    const context = candidateHeroContext(item);
    [3, 5].forEach((teamSize) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `${teamSize} 人`;
      button.classList.toggle(
        'selected', context && context.teamSize === teamSize);
      button.onclick = () => {
        candidateHeroContextTouched = true;
        candidateHeroTeamSizeExplicit = true;
        candidateHeroTeamSizeOverride = teamSize;
        candidateHeroDrawMode = false;
        renderCandidateHeroContextControls();
        loadCandidateHeroLineup(item);
      };
      sizeActions.appendChild(button);
    });
  }
}

function candidateDraftHasRenderState(draft = candidateDraft) {
  if (!draft) return false;
  return draft.result_panel_label === 'result_panel' ||
    ['gameplay_hud', 'scoreboard', 'result_page'].includes(
      draft.hero_layout_label);
}

function candidateResultQualitySummary() {
  if (!candidateDraft) return '异常：无';
  const parts = [];
  if (candidateDraft.panel_render_state === 'translucent') {
    parts.push('显示：半透明／过渡中');
  } else if (candidateDraft.panel_render_state === 'unknown') {
    parts.push('显示：看不清');
  }
  const isResultPanel = candidateDraft.result_panel_label === 'result_panel';
  if (!isResultPanel) {
    return parts.length ? parts.join(' · ') : '显示：正常';
  }
  if (candidateDraft.ocr_usable === 'no') parts.push('OCR 不可用');
  else if (candidateDraft.ocr_usable === 'unknown') parts.push('OCR 不确定');
  if (candidateDraft.result_occlusion === 'occluded') {
    const labels = Object.fromEntries(CFG.occluder_types || []);
    const types = (candidateDraft.occluder_types || [])
      .map((value) => labels[value]).filter(Boolean);
    parts.push(types.length ? `遮挡：${types.join('、')}` : '有遮挡');
  } else if (candidateDraft.result_occlusion === 'unknown') {
    parts.push('遮挡不确定');
  }
  return parts.length ? parts.join(' · ') : '显示：正常 · 异常：无';
}

function appendCandidateResultQualityRow(
  panel, title, values, selectedValues, onSelect,
) {
  const row = document.createElement('div');
  row.className = 'candidate-result-quality-row';
  const label = document.createElement('span');
  label.className = 'candidate-result-quality-label';
  label.textContent = title;
  row.appendChild(label);
  const buttons = document.createElement('div');
  buttons.className = 'candidate-result-quality-buttons';
  Object.entries(values).forEach(([value, text]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    const selected = selectedValues.has(value);
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.onclick = (event) => {
      event.preventDefault();
      onSelect(value);
    };
    buttons.appendChild(button);
  });
  row.appendChild(buttons);
  panel.appendChild(row);
}

function renderCandidateResultQualityDetails(details) {
  details.innerHTML = '';
  const summary = document.createElement('summary');
  summary.textContent = candidateResultQualitySummary();
  summary.title = '展开填写 HUD／面板显示状态、结算遮挡和 OCR 可用性';
  details.appendChild(summary);
  const panel = document.createElement('div');
  panel.className = 'candidate-result-quality-panel';
  appendCandidateResultQualityRow(
    panel, '显示', CFG.panel_render_states || {
      clear: '正常显示',
      translucent: '半透明／过渡中',
      unknown: '看不清',
    },
    new Set([candidateDraft.panel_render_state || 'clear']),
    (value) => {
      candidateDraft.panel_render_state = value;
      renderCandidateResultQualityDetails(details);
    },
  );
  if (candidateDraft.result_panel_label !== 'result_panel') {
    const hint = document.createElement('p');
    hint.className = 'hint small';
    hint.textContent = '半透明是 HUD／积分版淡入淡出时的过渡状态，不算遮挡。';
    panel.appendChild(hint);
    details.appendChild(panel);
    return;
  }
  appendCandidateResultQualityRow(
    panel, 'OCR', CFG.ocr_usable,
    new Set([candidateDraft.ocr_usable || 'yes']),
    (value) => {
      candidateDraft.ocr_usable = value;
      renderCandidateResultQualityDetails(details);
    },
  );
  appendCandidateResultQualityRow(
    panel, '遮挡', CFG.result_occlusion,
    new Set([candidateDraft.result_occlusion || 'none']),
    (value) => {
      candidateDraft.result_occlusion = value;
      if (value !== 'occluded') candidateDraft.occluder_types = [];
      renderCandidateResultQualityDetails(details);
    },
  );
  if (candidateDraft.result_occlusion === 'occluded') {
    appendCandidateResultQualityRow(
      panel, '遮挡物', Object.fromEntries(CFG.occluder_types || []),
      new Set(candidateDraft.occluder_types || []),
      (value) => {
        const selected = new Set(candidateDraft.occluder_types || []);
        if (selected.has(value)) selected.delete(value);
        else selected.add(value);
        candidateDraft.occluder_types = [...selected];
        renderCandidateResultQualityDetails(details);
      },
    );
  }
  const hint = document.createElement('p');
  hint.className = 'hint small';
  hint.textContent = '半透明是 HUD／面板过渡状态，不算遮挡；有其他 UI 盖住内容才标“有遮挡”。';
  panel.appendChild(hint);
  details.appendChild(panel);
}

function createCandidateResultQualityDetails() {
  const details = document.createElement('details');
  details.className = 'candidate-result-quality';
  details.classList.toggle(
    'hidden', !candidateDraftHasRenderState());
  renderCandidateResultQualityDetails(details);
  return details;
}

function appendCandidateHeroSelectVariant(group) {
  const label = candidateDraft.hero_select_label || '';
  if (!label.startsWith('select_')) return;
  const heading = document.createElement('p');
  heading.className = 'candidate-subheading';
  heading.textContent = '选择方式';
  group.appendChild(heading);
  const buttons = document.createElement('div');
  buttons.className = 'candidate-review-buttons compact';
  const values = label === 'select_aram'
    ? ['random'] : ['bp', 'blind', 'unreadable'];
  values.forEach((value) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = CANDIDATE_HERO_SELECT_VARIANTS[value];
    button.classList.toggle(
      'selected', candidateDraft.hero_select_variant === value);
    button.setAttribute(
      'aria-pressed', String(candidateDraft.hero_select_variant === value));
    button.onclick = () => {
      candidateDraft.hero_select_variant = value;
      renderCandidateChoices();
    };
    buttons.appendChild(button);
  });
  group.appendChild(buttons);
  const item = currentCandidate();
  const selectedVariant = candidateDraft.hero_select_variant;
  const variantSuggestion = candidateSuggestion(item || {}, 'hero_select_variant');
  const variantSource = document.createElement('p');
  variantSource.className = 'hint small';
  if (variantSuggestion && variantSuggestion.label === selectedVariant &&
      variantSuggestion.origin === 'new_model_prefill') {
    variantSource.textContent = `新模型建议 · ${(
      Number(variantSuggestion.confidence || 0) * 100).toFixed(1)}%`;
  } else if (item && item.hero_select_variant === selectedVariant &&
      selectedVariant) {
    variantSource.textContent = '本图已经人工保存；不是本次模型新判断。';
  } else if (label === 'select_aram' && selectedVariant === 'random') {
    variantSource.textContent = '按大乱斗规则预填“随机英雄”；不是独立模型结果。';
  } else if (candidateCachedReviewLabels().hero_select_variant === selectedVariant &&
      selectedVariant) {
    variantSource.textContent = '沿用上一次人工选择的缓存；不是模型结果。';
  } else {
    variantSource.textContent = '当前模型尚不判断 BP／盲选，请人工选择。';
  }
  group.appendChild(variantSource);

  const visibilityHeading = document.createElement('p');
  visibilityHeading.className = 'candidate-subheading';
  visibilityHeading.textContent = '画面情况';
  group.appendChild(visibilityHeading);
  const visibilityButtons = document.createElement('div');
  visibilityButtons.className = 'candidate-review-buttons compact';
  Object.entries(CANDIDATE_HERO_SELECT_VISIBILITY).forEach(([value, text]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    const selected = candidateDraft.hero_select_visibility === value;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.onclick = () => {
      candidateDraft.hero_select_visibility = value;
      renderCandidateChoices();
    };
    visibilityButtons.appendChild(button);
  });
  group.appendChild(visibilityButtons);
  const visibilityHelp = document.createElement('p');
  visibilityHelp.className = 'hint small';
  visibilityHelp.textContent =
    '局部被挡住但仍能确认模式时选“有遮挡”；挡到无法确认模式时，直接选上面的“看不清”。';
  group.appendChild(visibilityHelp);
}

function selectCandidateMatchContext(field, value) {
  if (!candidateDraft) return;
  candidateDraft[field] = value;
  if (field === 'match_kind_label' && value === 'practice') {
    candidateDraft.match_flow_label = 'match_flow';
    candidateDraft.match_mode_label = '5v5';
    candidateDraft.hero_select_label = 'not_select';
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
  }
  if (field === 'view_context_label' && value !== 'played') {
    candidateHeroPlayerStatus = 'pending';
    candidateHeroPlayerSlot = null;
    candidateHeroDirty = true;
  }
  renderCandidateChoices();
  refreshCandidateHeroReview();
}

function appendCandidateMatchContext(actions) {
  if (!candidateDraft || candidateDraft.match_flow_label !== 'match_flow') return;
  const details = document.createElement('details');
  details.className = 'candidate-match-context';
  details.open = candidateDraft.match_kind_label !== 'pvp' ||
    candidateDraft.view_context_label !== 'played';
  const summary = document.createElement('summary');
  summary.textContent = `对局性质：${
    CANDIDATE_MATCH_KINDS[candidateDraft.match_kind_label] || '未填写'} · ` +
    `观看方式：${
      CANDIDATE_VIEW_CONTEXTS[candidateDraft.view_context_label] || '未填写'}`;
  details.appendChild(summary);
  const panel = document.createElement('div');
  panel.className = 'candidate-match-context-panel';
  [
    ['match_kind_label', '对局性质', CANDIDATE_MATCH_KINDS],
    ['view_context_label', '观看方式', CANDIDATE_VIEW_CONTEXTS],
  ].forEach(([field, title, values]) => {
    const row = document.createElement('div');
    row.className = 'candidate-match-context-row';
    const label = document.createElement('strong');
    label.textContent = title;
    row.appendChild(label);
    const buttons = document.createElement('div');
    buttons.className = 'candidate-review-buttons compact';
    Object.entries(values).forEach(([value, text]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = text;
      const selected = candidateDraft[field] === value;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.onclick = () => selectCandidateMatchContext(field, value);
      buttons.appendChild(button);
    });
    row.appendChild(buttons);
    panel.appendChild(row);
  });
  const help = document.createElement('p');
  help.className = 'hint small';
  help.textContent =
    '真人／人机不会交给画面模型猜；新素材默认真人。观战和回放没有“主播本人英雄”。';
  panel.appendChild(help);
  details.appendChild(panel);
  actions.appendChild(details);
}

function renderCandidateChoices() {
  const item = currentCandidate();
  const actions = $('#candidate-label-actions');
  actions.innerHTML = '';
  renderCandidateHeroContextControls();
  if (!item || !candidateDraft) return;
  TRAINING_REVIEW_FIELDS.forEach((field) => {
    const group = document.createElement('section');
    group.className = 'candidate-review-group';
    if (field.key === 'match_mode_label' &&
        candidateDraft.match_flow_label !== 'match_flow') {
      group.classList.add('hidden');
    }
    const headingRow = document.createElement('div');
    headingRow.className = 'candidate-review-heading';
    const heading = document.createElement('h4');
    heading.textContent = field.title;
    headingRow.appendChild(heading);
    if (field.key === 'result_panel_label') {
      headingRow.appendChild(createCandidateResultQualityDetails());
    }
    group.appendChild(headingRow);
    const buttons = document.createElement('div');
    buttons.className = 'candidate-review-buttons';
    const suggestion = candidateSuggestedValue(item, field);
    Object.entries(field.labels).forEach(([value, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label + (value === suggestion ? ' · 模型建议' : '');
      button.classList.toggle('selected', candidateDraft[field.key] === value);
      button.onclick = () => selectCandidateReviewLabel(field.key, value);
      buttons.appendChild(button);
    });
    group.appendChild(buttons);
    if (field.key === 'hero_select_label') {
      appendCandidateHeroSelectVariant(group);
    }
    const help = document.createElement('p');
    help.className = 'hint small';
    help.textContent = field.help;
    group.appendChild(help);
    actions.appendChild(group);
    if (field.key === 'match_mode_label') {
      appendCandidateMatchContext(actions);
    }
  });
  const needsBox = candidateDraft.result_panel_label === 'result_panel';
  $('#candidate-draw-hint').textContent = candidateHeroDrawMode
    ? '正在画英雄圆框；画完当前英雄后会自动切到下一个位置。'
    : needsBox
      ? '在左图空白处拖动可框住完整结算面板；点“画英雄圆框”后则改为画头像。'
      : '确认右侧四项和图片下方的英雄标注后，点“确认并下一张”。';
  $('#btn-candidate-clear-boxes').disabled = !needsBox || !candidateBoxes.length;
}

function selectCandidateHeroLayout(value) {
  if (!candidateDraft || !CANDIDATE_HERO_LAYOUTS[value]) return;
  if (candidateDraft.hero_layout_label !== value) {
    candidateHeroContextTouched = true;
  }
  candidateDraft.hero_layout_label = value;
  candidateHeroDrawMode = false;
  if (CANDIDATE_HERO_SCREEN_TYPES.has(value)) {
    candidateDraft.match_flow_label = 'match_flow';
    candidateDraft.match_mode_label ||= 'unreadable';
    applyCandidateMatchContextDefaults(candidateDraft, currentCandidate() || {});
    candidateDraft.hero_select_label = 'not_select';
    if (value === 'result_page') {
      candidateDraft.result_panel_label = 'result_panel';
    } else {
      candidateDraft.result_panel_label = 'no_result_panel';
      candidateBoxes = [];
    }
  } else {
    resetCandidateHeroReview();
  }
  if (!candidateDraftHasRenderState()) {
    candidateDraft.panel_render_state = 'clear';
  }
  normalizeCandidateHeroSelectVariant(candidateDraft);
  renderCandidateBoxes();
  renderCandidateChoices();
  refreshCandidateHeroReview();
}

function selectCandidateReviewLabel(field, value) {
  if (!candidateDraft) return;
  if (candidateDraft[field] !== value && [
    'match_flow_label', 'match_mode_label', 'hero_select_label',
    'result_panel_label',
  ].includes(field)) {
    candidateHeroContextTouched = true;
  }
  candidateDraft[field] = value;
  if (field === 'match_flow_label') {
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
    if (value === 'match_flow') {
      candidateDraft.match_mode_label ||= 'unreadable';
      candidateDraft.hero_select_label = 'not_select';
      applyCandidateMatchContextDefaults(candidateDraft, currentCandidate() || {});
    } else {
      candidateDraft.match_mode_label = null;
      candidateDraft.match_kind_label = null;
      candidateDraft.view_context_label = null;
      if (value === 'unreadable') candidateDraft.hero_select_label = 'unreadable';
      else candidateDraft.hero_select_label ||= 'not_select';
      candidateDraft.result_panel_label = value === 'unreadable'
        ? 'unreadable' : 'no_result_panel';
      candidateDraft.hero_layout_label = value === 'unreadable'
        ? 'unreadable' : 'none';
      candidateBoxes = [];
      renderCandidateBoxes();
    }
  } else if (field === 'match_mode_label') {
    candidateDraft.match_flow_label = 'match_flow';
    candidateDraft.hero_select_label = 'not_select';
    applyCandidateMatchContextDefaults(candidateDraft, currentCandidate() || {});
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
  } else if (field === 'hero_select_label' && value.startsWith('select_')) {
    const storedItem = currentCandidate();
    if (storedItem && storedItem.hero_select_label === value) {
      candidateDraft.hero_select_variant = storedItem.hero_select_variant || null;
    } else {
      candidateDraft.hero_select_variant =
        candidateCachedReviewLabels().hero_select_variant || null;
    }
    candidateDraft.match_flow_label = 'not_match_flow';
    candidateDraft.match_mode_label = null;
    candidateDraft.match_kind_label = null;
    candidateDraft.view_context_label = null;
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
    candidateDraft.result_panel_label = 'no_result_panel';
    candidateDraft.hero_layout_label = 'none';
    candidateDraft.hero_select_visibility =
      storedItem && storedItem.hero_select_label === value
        ? storedItem.hero_select_visibility || 'clear' : 'clear';
    candidateBoxes = [];
    renderCandidateBoxes();
  } else if (field === 'hero_select_label' && value === 'unreadable' &&
      candidateDraft.match_flow_label === 'match_flow') {
    candidateDraft.match_flow_label = 'unreadable';
    candidateDraft.match_mode_label = null;
  } else if (field === 'result_panel_label' && value === 'result_panel') {
    candidateDraft.match_flow_label = 'match_flow';
    candidateDraft.match_mode_label ||= 'unreadable';
    candidateDraft.hero_select_label = 'not_select';
    candidateDraft.hero_layout_label = 'result_page';
    applyCandidateMatchContextDefaults(candidateDraft, currentCandidate() || {});
  } else if (field === 'result_panel_label' && value !== 'result_panel') {
    candidateDraft.ocr_usable = 'yes';
    candidateDraft.result_occlusion = 'none';
    candidateDraft.occluder_types = [];
    if (candidateDraft.hero_layout_label === 'result_page') {
      candidateDraft.hero_layout_label = 'none';
    }
    candidateBoxes = [];
    renderCandidateBoxes();
  }
  if (!candidateDraftHasRenderState()) {
    candidateDraft.panel_render_state = 'clear';
  }
  normalizeCandidateHeroSelectVariant(candidateDraft);
  renderCandidateChoices();
  refreshCandidateHeroReview();
}

function candidateItemMatchesStatus(item, status) {
  if (!item) return false;
  if (item.review_filter_completed) return false;
  if (status === 'all') return true;
  if (status === 'legacy_hero') {
    return Boolean(item.legacy_hero_needs_review);
  }
  if (status === 'migration_review') {
    return Boolean(item.legacy_migration_needs_review);
  }
  if (status === 'human_confirmed') {
    return item.review_status === 'confirmed' &&
      Boolean(item.unified_manual_reviewed);
  }
  if (status === 'needs_review') {
    return ['pending', 'partial'].includes(item.review_status);
  }
  if (status === 'missing_player') {
    return Boolean(item.needs_player_hero_review);
  }
  if (status === 'missing_afk') {
    return Boolean(item.needs_afk_review);
  }
  return item.review_status === status;
}

function candidateReviewTotal(stats, status) {
  if (candidateSourceScope === 'all') {
    if (status === 'all') return Number(stats.total || 0);
    return Number((stats.statuses || {})[status] || 0);
  }
  const scope = (stats.source_scopes || {})[candidateSourceScope] || {};
  const statuses = scope.statuses || {};
  if (status === 'all') return Number(scope.total || 0);
  if (status === 'legacy_hero') {
    return Number(
      (stats.legacy_hero_filtered || stats.legacy_hero || {}).remaining_groups || 0
    );
  }
  if (status === 'migration_review') {
    return Number(scope.migration_pending_review || 0);
  }
  if (status === 'human_confirmed') {
    return Number(scope.human_confirmed || 0);
  }
  if (status === 'needs_review') {
    return Number(scope.needs_review || 0);
  }
  if (status === 'missing_player') {
    return Number(scope.missing_player_hero || 0);
  }
  return Number(statuses[status] || 0);
}

function candidateStatusIsReviewQueue(status) {
  return [
    'needs_review', 'missing_player', 'pending', 'partial', 'legacy_hero',
    'migration_review', 'missing_afk', 'confirmed', 'human_confirmed',
  ].includes(status);
}

function candidateReviewQuery(status, offset = null) {
  const query = new URLSearchParams({
    status, limit: String(CANDIDATE_PAGE_SIZE), source_scope: candidateSourceScope,
    include_stats: 'false',
  });
  if (offset !== null) query.set('offset', String(offset));
  if (status === 'legacy_hero') {
    const streamer = $('#candidate-legacy-streamer').value;
    const screenType = $('#candidate-legacy-screen').value;
    if (streamer) query.set('streamer', streamer);
    if (screenType) query.set('hero_screen_type', screenType);
  }
  const filters = {
    source_type: $('#candidate-source-type-filter').value,
    scene: $('#candidate-scene-filter').value,
    match_mode: $('#candidate-mode-filter').value,
    match_kind: $('#candidate-match-kind-filter').value,
    view_context: $('#candidate-view-context-filter').value,
    confidence: $('#candidate-confidence-filter').value,
    afk_prediction: $('#candidate-afk-prediction-filter').value,
    review_reason: $('#candidate-review-reason-filter').value,
    streamer: $('#candidate-streamer-filter').value,
  };
  Object.entries(filters).forEach(([key, value]) => {
    if (value) query.set(key, value);
  });
  [...candidateHeroFilters].sort().forEach((hero) => query.append('hero', hero));
  if (candidateHeroFilters.size && candidateHeroScope !== 'all') {
    query.set('hero_scope', candidateHeroScope);
  }
  return `/api/training-review/items?${query}`;
}

async function loadCandidateReviewStats(status, sourceScope) {
  const query = new URLSearchParams({
    status, source_scope: sourceScope,
  });
  if (status === 'legacy_hero') {
    const streamer = $('#candidate-legacy-streamer').value;
    const screenType = $('#candidate-legacy-screen').value;
    if (streamer) query.set('streamer', streamer);
    if (screenType) query.set('hero_screen_type', screenType);
  }
  try {
    const data = sourceScope === 'new'
      ? await api(`/api/training-review/queue-summary?${query}`)
      : await api(`/api/training-review/stats?${query}`);
    if (sourceScope !== candidateSourceScope ||
        status !== $('#candidate-status-filter').value) return;
    candidateReviewStats = sourceScope === 'new'
      ? {...candidateReviewStats, queue_summary: data.summary || {}}
      : data.stats || {};
    renderCandidateLegacyControls(candidateReviewStats, status);
    renderCandidateSyncStats(candidateReviewStats);
    renderCandidateMaterialSuggestionButton();
    if ($('#candidate-material-dialog').open) {
      renderCandidateMaterialSuggestions();
    }
  } catch (error) {
    $('#candidate-scope-summary').textContent =
      '统计数据加载失败：' + error.message;
    $('#candidate-material-suggestion-count').textContent = '失败';
  }
}

function candidateMaterialSuggestions() {
  return Array.isArray(candidateReviewStats.material_suggestions)
    ? candidateReviewStats.material_suggestions : [];
}

function renderCandidateMaterialSuggestionButton() {
  const count = $('#candidate-material-suggestion-count');
  if (!count) return;
  if (!Object.keys(candidateReviewStats).length) {
    count.textContent = '查看';
    return;
  }
  if (!Array.isArray(candidateReviewStats.material_suggestions)) {
    count.textContent = '查看';
    return;
  }
  const suggestions = candidateMaterialSuggestions();
  const shortages = suggestions.filter(
    (suggestion) => suggestion.status !== 'sufficient').length;
  count.textContent = shortages ? String(shortages) : '充足';
}

function candidateMaterialSeverity(value) {
  return {
    urgent: '急需补充',
    scarce: '数量很少',
    low: '相对偏少',
    sufficient: '已达建议线',
  }[value] || '建议补充';
}

function candidateModelRunLabel(runId) {
  const value = String(runId || '');
  const date = value.match(/20\d{2}(\d{4})/);
  return date ? date[1] : value || '未知版本';
}

function candidateQualityPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function candidateQualityContextLabel(context) {
  const scene = CANDIDATE_HERO_LAYOUT_SHORT_LABELS[context.screen_type]
    || (context.screen_type === 'hero_select' ? '英雄选择' : '其他画面');
  const mode = {aram: '大乱斗', '3v3': '3V3', '5v5': '5V5'}[
    context.match_mode] || context.match_mode || '';
  return mode ? `${scene} · ${mode}` : scene;
}

function candidateQualityClassLabel(taskId, label) {
  const taskLabel = MODEL_TASKS[taskId]?.labels?.[label];
  if (taskLabel) return taskLabel;
  const hero = candidateHeroByLabel(label);
  return hero ? hero.name : label;
}

function renderCandidateModelQuality() {
  const container = $('#candidate-model-quality');
  if (!container) return;
  container.replaceChildren();
  if (candidateModelQuality === null) {
    const loading = document.createElement('div');
    loading.className = 'candidate-model-quality-empty';
    loading.textContent = '正在读取各版本人工纠错结果…';
    container.appendChild(loading);
    return;
  }
  const tasks = Array.isArray(candidateModelQuality.tasks)
    ? candidateModelQuality.tasks : [];
  if (!tasks.length) {
    const empty = document.createElement('div');
    empty.className = 'candidate-model-quality-empty';
    empty.textContent = '还没有可与人工确认结果对照的模型数据。';
    container.appendChild(empty);
    return;
  }
  tasks.forEach((task) => {
    const versions = Array.isArray(task.versions) ? task.versions : [];
    if (!versions.length) return;
    const selectedRun = candidateModelQualitySelection.get(task.id)
      || task.latest_run_id || versions[0].run_id;
    const version = versions.find((item) => item.run_id === selectedRun)
      || versions[0];
    candidateModelQualitySelection.set(task.id, version.run_id);

    const row = document.createElement('div');
    row.className = 'candidate-model-quality-row';

    const identity = document.createElement('div');
    identity.className = 'candidate-model-quality-identity';
    const name = document.createElement('strong');
    name.textContent = task.name || task.id;
    const select = document.createElement('select');
    select.setAttribute('aria-label', `${task.name || task.id}模型版本`);
    versions.forEach((item) => {
      const option = new Option(
        `${candidateModelRunLabel(item.run_id)}${item.latest ? ' · 当前' : ''}`,
        item.run_id,
      );
      option.title = item.run_id;
      select.appendChild(option);
    });
    select.value = version.run_id;
    select.title = version.run_id;
    select.onchange = () => {
      candidateModelQualitySelection.set(task.id, select.value);
      renderCandidateModelQuality();
    };
    identity.append(name, select);

    const score = document.createElement('div');
    score.className = `candidate-model-quality-score ${version.status || ''}`;
    const metric = document.createElement('strong');
    metric.textContent = candidateQualityPercent(version.accuracy);
    const metricName = document.createElement('span');
    metricName.textContent = version.metric === 'complete_rate'
      ? '自动找齐率' : '人工一致率';
    score.append(metric, metricName);

    const detail = document.createElement('div');
    detail.className = 'candidate-model-quality-detail';
    const compared = Number(version.compared || 0);
    const wrong = Number(version.wrong || 0);
    const highConfidenceWrong = Number(version.high_confidence_wrong || 0);
    const change = version.change_points;
    const parts = [
      `已复核 ${compared.toLocaleString('zh-CN')}`,
      `人工改正 ${wrong.toLocaleString('zh-CN')}`,
    ];
    if (highConfidenceWrong) parts.push(`高置信错 ${highConfidenceWrong}`);
    if (change !== null && change !== undefined) {
      parts.push(`比上一版本${Number(change) >= 0 ? ' +' : ' '}${change} 个百分点`);
    }
    const commonConfusion = (version.confusions || [])[0];
    if (commonConfusion) {
      const [confirmedLabel, predictedLabel] = String(
        commonConfusion.labels || '').split('→');
      if (confirmedLabel && predictedLabel) {
        parts.push(
          `常见错误：${candidateQualityClassLabel(task.id, confirmedLabel)} ` +
          `被识别成 ${candidateQualityClassLabel(task.id, predictedLabel)}` +
          `（${commonConfusion.count}）`,
        );
      }
    }
    detail.textContent = parts.join(' · ');

    const contexts = document.createElement('div');
    contexts.className = 'candidate-model-quality-contexts';
    (version.contexts || []).slice(0, 4).forEach((context) => {
      const chip = document.createElement('span');
      chip.textContent = `${candidateQualityContextLabel(context)} ` +
        `改 ${context.wrong}/${context.compared} · ` +
        `一致 ${candidateQualityPercent(context.accuracy)}`;
      contexts.appendChild(chip);
    });

    row.append(identity, score, detail, contexts);
    container.appendChild(row);
  });
}

function applyCandidateMaterialSuggestion(suggestion, heroScope = 'direct') {
  const filters = suggestion.filters || {};
  setCandidateSourceScope(
    suggestion.source_scope || 'new', filters.status || 'needs_review', true);
  $('#candidate-source-type-filter').value = '';
  $('#candidate-scene-filter').value = filters.scene || '';
  $('#candidate-mode-filter').value = filters.match_mode || '';
  $('#candidate-match-kind-filter').value = '';
  $('#candidate-view-context-filter').value = '';
  $('#candidate-confidence-filter').value = '';
  $('#candidate-review-reason-filter').value = '';
  $('#candidate-streamer-filter').value = '';
  candidateHeroFilters = new Set(filters.hero ? [filters.hero] : []);
  candidateHeroScope = filters.hero ? heroScope : 'all';
  $('#candidate-hero-filter-search').value = '';
  const selectedHero = filters.hero && candidateHeroByLabel(filters.hero);
  $('#candidate-hero-filter-summary').textContent = filters.hero
    ? `${selectedHero ? selectedHero.name : filters.hero}（${
      candidateHeroScope === 'direct' ? '本图直接命中' : '含同局／同视频待排查'}）`
    : '全部英雄';
  $('#candidate-hero-filter').open = false;
  renderCandidateHeroFilter();
  $('#candidate-material-dialog').close();
  loadCandidateReview();
}

function renderCandidateMaterialSuggestions() {
  const list = $('#candidate-material-list');
  const summary = $('#candidate-material-summary');
  const suggestions = candidateMaterialSuggestions();
  list.replaceChildren();
  if (!Array.isArray(candidateReviewStats.material_suggestions)) {
    summary.textContent = '正在统计训练素材分布…';
    const loading = document.createElement('div');
    loading.className = 'candidate-material-empty';
    loading.textContent = '读取中…';
    list.appendChild(loading);
    return;
  }
  if (!suggestions.length) {
    summary.textContent = '当前还没有可以统计的素材类别。';
    const empty = document.createElement('div');
    empty.className = 'candidate-material-empty';
    empty.textContent = '暂无素材分布数据。';
    list.appendChild(empty);
    return;
  }
  const shortages = suggestions.filter(
    (suggestion) => suggestion.status !== 'sufficient');
  const actionable = shortages.filter(
    (suggestion) => Number(suggestion.candidate_count || 0) > 0).length;
  summary.textContent = `共 ${suggestions.length} 类素材，` +
    `${shortages.length} 类未达建议线；其中 ${actionable} 类已有候选可直接复核。`;
  suggestions.forEach((suggestion) => {
    const row = document.createElement('div');
    row.className = 'candidate-material-row';

    const name = document.createElement('div');
    name.className = 'candidate-material-name';
    if (suggestion.kind === 'hero_scene' && suggestion.hero_label) {
      const heroImage = document.createElement('img');
      heroImage.className = 'candidate-material-hero-image';
      heroImage.src = `/api/training-review/heroes/${encodeURIComponent(
        suggestion.hero_label)}/image`;
      heroImage.alt = '';
      heroImage.loading = 'lazy';
      name.appendChild(heroImage);
    }
    const title = document.createElement('span');
    title.textContent = suggestion.kind === 'hero_scene'
      ? `${suggestion.hero_name || suggestion.hero_label} · ` +
        `${suggestion.scene_label}头像`
      : suggestion.kind === 'afk_status'
        ? '真正结算图 · 挂机状态'
        : `${suggestion.mode_label} · ${suggestion.scene_label}`;
    const severity = document.createElement('span');
    severity.className = `candidate-material-severity ${suggestion.severity || ''}`;
    severity.textContent = suggestion.status === 'model_errors'
      ? '当前模型仍易错' : candidateMaterialSeverity(suggestion.severity);
    name.append(title, severity);

    const counts = document.createElement('div');
    counts.className = 'candidate-material-counts';
    const confirmed = document.createElement('strong');
    confirmed.textContent = String(suggestion.confirmed_count || 0);
    const confirmedSources = [
      {key: 'legacy_confirmed_count', label: '历史'},
      {key: 'new_confirmed_count', label: '新素材'},
    ];
    if (Number(suggestion.other_confirmed_count || 0) > 0) {
      confirmedSources.push({key: 'other_confirmed_count', label: '其他来源'});
    }
    const confirmedBreakdown = '（' + confirmedSources.map((source) =>
      `${source.label} ${Number(suggestion[source.key] || 0)}`).join(' · ') + '）';
    const available = Number(suggestion.candidate_count || 0);
    const relatedAvailable = Number(suggestion.related_candidate_count || 0);
    const queueName = suggestion.source_scope === 'legacy'
      ? '历史队列' : 'Worker 队列';
    if (suggestion.kind === 'afk_status') {
      const active = Number(suggestion.active_count || 0);
      const afk = Number(suggestion.afk_count || 0);
      const activeShortage = Number(suggestion.active_shortage_count || 0);
      const afkShortage = Number(suggestion.afk_shortage_count || 0);
      counts.append(
        '挂机 ', confirmed,
        ` / ${suggestion.afk_target_count || 0} 个 · ` +
          `正常 ${active} / ${suggestion.active_target_count || 0} 个 · ` +
          `${queueName}待补 ${available} 张结算图` +
          (afkShortage || activeShortage
            ? ` · 还差挂机 ${afkShortage}、正常 ${activeShortage}`
            : ' · 已达建议线'),
      );
    } else if (suggestion.kind === 'hero_scene') {
      const modelPrefill = Number(suggestion.model_prefill_count || 0);
      const sameMatch = Number(suggestion.same_match_candidate_count || 0);
      const sameVideo = Number(suggestion.same_video_candidate_count || 0);
      const missingScene = Number(
        suggestion.matches_without_scene_candidate || 0);
      counts.append(
        '已确认 ', confirmed,
        ` 个头像${confirmedBreakdown} / 建议 ${suggestion.target_count || 0} 个 · `,
        `${queueName}直接候选 ${available} 张：模型直接认出 ${modelPrefill} 张；` +
          `同局待排查 ${sameMatch} 张、同视频兜底 ${sameVideo} 张；` +
          `直接候选头像 ${suggestion.candidate_crop_count || 0} 个` +
          (missingScene ? ` · 另有 ${missingScene} 局没有${suggestion.scene_label}候选` : ''),
      );
    } else {
      const waiting = Number(suggestion.prefill_waiting_count || 0);
      const failed = Number(suggestion.prefill_failed_count || 0);
      counts.append(
        '已确认 ', confirmed,
        ` 张${confirmedBreakdown} / 建议 ${suggestion.target_count || 0} 张 · `,
        `${queueName}可立即复核 ${available} 张` +
          (waiting ? ` · 已采集待模型预填 ${waiting} 张` : '') +
          (failed ? ` · 预填失败 ${failed} 张` : ''),
      );
    }
    if (suggestion.model_quality) {
      const quality = suggestion.model_quality;
      counts.append(
        document.createElement('br'),
        `当前 ${candidateModelRunLabel(quality.run_id)}：人工改正 ` +
          `${quality.wrong}/${quality.compared} ` +
          `(${candidateQualityPercent(quality.correction_rate)})`,
      );
    }

    const actions = document.createElement('div');
    actions.className = 'candidate-material-actions';
    const action = document.createElement('button');
    action.type = 'button';
    action.disabled = available <= 0;
    action.textContent = available > 0
      ? suggestion.kind === 'afk_status'
        ? `去补挂机（${available}）`
        : suggestion.status === 'sufficient'
          ? `继续打标（${available}）`
          : `去打标（${available}）`
      : '暂无候选';
    action.title = available > 0
      ? '打开对应待确认素材'
      : '当前候选库没有这类图片，需要等待 Worker 后续采集';
    action.onclick = () => applyCandidateMaterialSuggestion(suggestion, 'direct');
    actions.appendChild(action);
    if (suggestion.kind === 'hero_scene' && relatedAvailable > 0) {
      const relatedAction = document.createElement('button');
      relatedAction.type = 'button';
      relatedAction.textContent = `排查关联（${available + relatedAvailable}）`;
      relatedAction.title = '包含同局和同视频兜底素材，不代表本图已经识别出该英雄';
      relatedAction.onclick = () => applyCandidateMaterialSuggestion(
        suggestion, 'all');
      actions.appendChild(relatedAction);
    }

    row.append(name, counts, actions);
    list.appendChild(row);
  });
}

async function openCandidateMaterialSuggestions() {
  renderCandidateMaterialSuggestions();
  renderCandidateModelQuality();
  const dialog = $('#candidate-material-dialog');
  if (!dialog.open) dialog.showModal();
  const materialRequest = api('/api/training-review/material-suggestions')
      .then((data) => {
        candidateReviewStats.material_suggestions = data.material_suggestions || [];
        renderCandidateMaterialSuggestions();
        renderCandidateMaterialSuggestionButton();
      })
      .catch((error) => {
        $('#candidate-material-summary').textContent =
          '素材建议加载失败：' + error.message;
      });
  const qualityRequest = api('/api/training-review/model-quality')
      .then((data) => {
        candidateModelQuality = data;
        renderCandidateModelQuality();
      })
      .catch((error) => {
        candidateModelQuality = {tasks: []};
        renderCandidateModelQuality();
        const container = $('#candidate-model-quality');
        if (container) container.textContent =
          '模型纠错分析加载失败：' + error.message;
      });
  await Promise.all([materialRequest, qualityRequest]);
}

function renderCandidateHeroFilter() {
  const query = $('#candidate-hero-filter-search').value.trim().toLocaleLowerCase();
  const options = $('#candidate-hero-filter-options');
  options.innerHTML = '';
  candidateHeroCatalog
    .filter((hero) => !query ||
      `${hero.name} ${hero.label}`.toLocaleLowerCase().includes(query))
    .forEach((hero) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'candidate-filter-hero-option';
      const selected = candidateHeroFilters.has(hero.label);
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      const image = document.createElement('img');
      image.src = hero.image_url;
      image.alt = '';
      image.loading = 'lazy';
      const name = document.createElement('span');
      name.textContent = hero.name;
      button.append(image, name);
      button.onclick = () => {
        candidateHeroScope = 'all';
        if (candidateHeroFilters.has(hero.label)) {
          candidateHeroFilters.delete(hero.label);
        } else {
          candidateHeroFilters.add(hero.label);
        }
        renderCandidateHeroFilter();
        const count = candidateHeroFilters.size;
        $('#candidate-hero-filter-summary').textContent = count
          ? `已选 ${count} 个英雄（含同局）` : '全部英雄';
        loadCandidateReview();
      };
      options.appendChild(button);
    });
}

function ensureCandidateFilterOptions() {
  if (candidateFilterOptionsLoadedScope === candidateSourceScope) {
    return Promise.resolve();
  }
  if (candidateFilterOptionsPromise) {
    return candidateFilterOptionsPromise.then(ensureCandidateFilterOptions);
  }
  const promise = loadCandidateFilterOptions().finally(() => {
    if (candidateFilterOptionsPromise === promise) {
      candidateFilterOptionsPromise = null;
    }
  });
  candidateFilterOptionsPromise = promise;
  return promise;
}

async function loadCandidateFilterOptions() {
  const scope = candidateSourceScope;
  const heroCatalog = ensureCandidateHeroCatalog()
    .then(() => null)
    .catch((error) => error);
  try {
    const filters = await api(
      `/api/training-review/filter-options?source_scope=${encodeURIComponent(scope)}`);
    if (scope !== candidateSourceScope) return;
    const select = $('#candidate-streamer-filter');
    const selected = select.value;
    select.replaceChildren(
      new Option('全部主播', ''),
      ...(filters.streamers || []).map((value) =>
        new Option(`${value.name} · ${value.frame_count}`, value.name)),
    );
    if ([...select.options].some((option) => option.value === selected)) {
      select.value = selected;
    }
    candidateFilterOptionsLoadedScope = scope;
    const heroError = await heroCatalog;
    if (scope !== candidateSourceScope) return;
    renderCandidateHeroFilter();
    if (heroError) {
      $('#candidate-hero-filter-options').textContent =
        '英雄头像暂时加载失败，不影响其他筛选。';
    }
  } catch (error) {
    $('#candidate-scope-summary').textContent = '筛选项加载失败：' + error.message;
  }
}

function renderCandidateLegacyControls(stats, status) {
  const historical = candidateSourceScope === 'legacy';
  const confirmedTraining = candidateSourceScope === 'all';
  const active = historical && status === 'legacy_hero';
  $('#candidate-legacy-filters').classList.toggle('hidden', !active);
  $('#candidate-page-title').textContent = confirmedTraining
    ? '已确认训练数据'
    : historical ? '历史人工数据' : 'Worker 待复核';
  $('#candidate-page-hint').textContent = active
    ? '这里只把历史 HUD、积分板和结算图按主播、同一局和画面类型折叠成代表组；你补一张代表图即可，不需要把约 7000 张旧图逐张重标。未补头像的旧图不会被当作“没有头像”的负样本。'
    : status === 'missing_afk'
      ? '这里只显示挂机状态尚未补齐的真正结算图。点亮最终挂机的英雄；其余可见英雄会在你确认时明确保存为“未挂机”。积分板上的临时掉线不作为挂机训练真值。'
    : historical && status === 'migration_review'
      ? '这些图片只是把旧格式标签迁移成了新字段，还没有在统一打标页面由你重新确认。旧标签和新模型结果都只作为预填；请像新数据一样核对完整分类，并按画面选择 HUD、积分板、结算、无头像或看不清。确认后才会进入“新流程人工已确认”。'
      : historical && status === 'human_confirmed'
        ? '这里只显示你在统一打标页面亲自确认过的历史图片；迁移程序自动带入的旧标签不会出现在这里。'
        : confirmedTraining
          ? '这里汇总新旧来源中已经人工确认并进入训练集的图片。可以按来源、画面、模式、置信度、主播、英雄或复查原因筛选，修改后直接保存最新标签。'
        : historical
          ? '这里展示旧格式人工标签及其迁移状态，与新数据使用同一套分类、头像来源、圆框、英雄和本人位置标注。没有头像框的旧图不会成为头像模型的负样本。'
          : 'Worker 产出的候选会在 NAS 上自动建立索引，不需要再同步到本机。新模型会预填分类、结算框、头像位置、英雄和主播本人；你只需核对和修正。';
  if (!active) return;
  const globalStats = stats.legacy_hero || {};
  const filteredStats = stats.legacy_hero_filtered || globalStats;
  const streamerSelect = $('#candidate-legacy-streamer');
  const selectedStreamer = streamerSelect.value;
  const options = (globalStats.by_streamer || []).map((value) => ({
    value: value.streamer || '',
    label: `${value.streamer || '未知主播'} · ${value.groups || 0} 组`,
  }));
  streamerSelect.replaceChildren(
    new Option('全部主播（连续排列）', ''),
    ...options.map((value) => new Option(value.label, value.value)),
  );
  if (options.some((value) => value.value === selectedStreamer)) {
    streamerSelect.value = selectedStreamer;
  }
  const screenNames = {
    gameplay_hud: 'HUD', scoreboard: '积分板', result_page: '结算',
  };
  const breakdown = Object.entries(filteredStats.by_screen_type || {})
    .filter(([, count]) => Number(count) > 0)
    .map(([screen, count]) => `${screenNames[screen] || screen} ${count} 组`)
    .join(' · ');
  $('#candidate-legacy-summary').textContent =
    `待补 ${filteredStats.remaining_groups || 0} 组，覆盖 ` +
    `${filteredStats.remaining_frames || 0} 张旧图` +
    (breakdown ? ` · ${breakdown}` : '');
}

function renderCandidateProgress() {
  if (['confirmed', 'human_confirmed'].includes(candidateLoadedStatus)) {
    $('#candidate-progress').textContent = currentCandidate()
      ? `当前第 ${candidateIndex + 1} / ${candidateFilteredTotal} 张`
      : `共 ${candidateFilteredTotal} 张`;
    return;
  }
  const activeReview = candidateStatusIsReviewQueue(candidateLoadedStatus);
  if (activeReview) {
    const sessionTotal = candidateFilteredTotal + candidateSessionCompleted;
    const currentPosition = Math.min(
      sessionTotal,
      candidateSessionCompleted + (currentCandidate() ? 1 : 0),
    );
    $('#candidate-progress').textContent =
      `当前第 ${currentPosition} / ${sessionTotal} 张 · ` +
      `剩余 ${candidateFilteredTotal} 张`;
    return;
  }
  $('#candidate-progress').textContent = currentCandidate()
    ? `共 ${candidateFilteredTotal} · 当前第 ${candidateIndex + 1} 张`
    : `共 ${candidateFilteredTotal}`;
}

function renderCandidateSyncStats(stats) {
  const scopes = stats.source_scopes || {};
  const legacy = scopes.legacy || {};
  if (candidateSourceScope === 'all') {
    $('#candidate-scope-summary').textContent =
      `共 ${trainingNumber((stats.statuses || {}).confirmed || 0)} 张已确认训练图片；` +
      '可按下方任意维度筛选并重新修改。';
    return;
  }
  if (candidateSourceScope === 'legacy') {
    const hero = stats.legacy_hero || {};
    $('#candidate-scope-summary').textContent =
      `共 ${legacy.total || 0} 张旧图 · 迁移待人工复核 ` +
      `${legacy.migration_pending_review || 0} · 新流程人工已确认 ` +
      `${legacy.human_confirmed || 0} · 旧标签不完整 ${legacy.needs_review || 0} · ` +
      `可复核 ${legacy.prefill_ready || 0} · ` +
      `后台待预标 ${legacy.prefill_waiting || 0} · ` +
      `预标失败 ${legacy.prefill_failed || 0} · ` +
      `头像待补 ${hero.remaining_groups || 0} 组`;
    return;
  }
  const queue = stats.queue_summary || {};
  $('#candidate-worker-total').textContent = trainingNumber(queue.total || 0);
  $('#candidate-prefill-ready').textContent = trainingNumber(
    queue.prefill_ready || 0);
  $('#candidate-ready-for-review').textContent = trainingNumber(
    queue.ready_for_review || 0);
  $('#candidate-scope-summary').textContent =
    `后台待预标 ${trainingNumber(queue.prefill_waiting || 0)} · ` +
    `预标失败 ${trainingNumber(queue.prefill_failed || 0)}；` +
    '下方只会出现已经完成预打标的素材。';
}

function candidateImageUrl(item) {
  const version = item.sha256 || item.frame_id;
  return `/api/frames/${item.frame_id}/image?v=${encodeURIComponent(version)}`;
}

function prefetchCandidateImage(item) {
  const frameId = Number(item && item.frame_id);
  if (!frameId) return Promise.resolve(false);
  const existing = candidateImagePrefetches.get(frameId);
  if (existing) return existing.promise;
  const image = new Image();
  image.decoding = 'async';
  const entry = {image, promise: null, settled: false, resolve: null};
  entry.promise = new Promise((resolve) => {
    entry.resolve = resolve;
    const settle = (loaded) => {
      if (entry.settled) return;
      entry.settled = true;
      image.onload = null;
      image.onerror = null;
      resolve(loaded);
    };
    image.onload = async () => {
      try {
        await image.decode();
      } catch (_error) {
        // 部分浏览器不支持显式解码；下载完成仍可复用内存缓存。
      }
      settle(true);
    };
    image.onerror = () => settle(false);
    image.src = candidateImageUrl(item);
  });
  candidateImagePrefetches.set(frameId, entry);
  return entry.promise;
}

function pruneCandidatePrefetches(keepFrameIds = new Set()) {
  candidateHeroPrefetchRequests.forEach((entry, key) => {
    if (keepFrameIds.has(entry.frameId)) return;
    entry.controller.abort();
    candidateHeroPrefetchRequests.delete(key);
  });
  candidateImagePrefetches.forEach((entry, frameId) => {
    if (keepFrameIds.has(frameId)) return;
    entry.image.src = '';
    if (!entry.settled && entry.resolve) {
      entry.settled = true;
      entry.resolve(false);
    }
    candidateImagePrefetches.delete(frameId);
  });
  candidatePreparationRequests.forEach((_promise, frameId) => {
    if (!keepFrameIds.has(frameId)) candidatePreparationRequests.delete(frameId);
  });
}

function pruneCandidateNavigationPrefetches() {
  const items = [currentCandidate(), ...nextMatchingCandidates(
    CANDIDATE_IMAGE_PREFETCH_TARGET)];
  pruneCandidatePrefetches(new Set(
    items.filter(Boolean).map((item) => Number(item.frame_id))));
}

function prepareCandidateForReview(item) {
  const frameId = Number(item && item.frame_id);
  if (!frameId) return Promise.resolve();
  const existing = candidatePreparationRequests.get(frameId);
  if (existing) return existing;
  const promise = Promise.all([
    prefetchCandidateImage(item),
    Promise.resolve().then(() => {
      const draft = candidateDefaultDraft(item);
      const context = candidateHeroContext(item, draft, false);
      if (!context) return undefined;
      return prepareCandidateHeroLineup(item, context).initialPromise;
    }),
  ]).then(() => undefined);
  promise.catch(() => {
    if (candidatePreparationRequests.get(frameId) === promise) {
      candidatePreparationRequests.delete(frameId);
    }
  });
  candidatePreparationRequests.set(frameId, promise);
  return promise;
}

function nextMatchingCandidate() {
  return candidateQueue.find((value, index) =>
    index > candidateIndex &&
    candidateItemMatchesStatus(value, candidateLoadedStatus)) || null;
}

function nextMatchingCandidates(limit = CANDIDATE_READY_TARGET) {
  return candidateQueue.filter((value, index) =>
    index > candidateIndex &&
    candidateItemMatchesStatus(value, candidateLoadedStatus)).slice(0, limit);
}

function ensureCandidateReviewQueueRefill() {
  if (!candidateReviewRefillPromise) {
    const loadToken = candidateReviewLoadToken;
    const promise = refillCandidateReviewQueue(loadToken)
      .finally(() => {
        if (candidateReviewRefillPromise === promise) {
          candidateReviewRefillPromise = null;
        }
      });
    candidateReviewRefillPromise = promise;
  }
  return candidateReviewRefillPromise;
}

async function warmCandidateReviewQueue(loadToken) {
  while (loadToken === candidateReviewLoadToken) {
    let upcoming = nextMatchingCandidates();
    const knownRemaining = candidateQueue.filter(
      (value) => candidateItemMatchesStatus(
        value, candidateLoadedStatus)).length;
    if (upcoming.length < CANDIDATE_READY_TARGET &&
        candidateFilteredTotal > knownRemaining) {
      await ensureCandidateReviewQueueRefill();
      if (loadToken !== candidateReviewLoadToken) return;
      upcoming = nextMatchingCandidates();
    }
    upcoming.slice(0, CANDIDATE_IMAGE_PREFETCH_TARGET)
      .forEach((item) => prefetchCandidateImage(item));
    const item = upcoming[0];
    if (!item) return;
    await prepareCandidateForReview(item);
    return;
  }
}

function ensureCandidateReviewWarm() {
  if (candidatePreparationRunnerPromise) return candidatePreparationRunnerPromise;
  const loadToken = candidateReviewLoadToken;
  const promise = warmCandidateReviewQueue(loadToken)
    .catch(() => undefined)
    .finally(() => {
      if (candidatePreparationRunnerPromise === promise) {
        candidatePreparationRunnerPromise = null;
      }
      if (loadToken !== candidateReviewLoadToken && currentCandidate()) {
        prefetchNextCandidate();
      }
    });
  candidatePreparationRunnerPromise = promise;
  return promise;
}

async function prefetchNextCandidate() {
  let upcoming = nextMatchingCandidates();
  const knownRemaining = candidateQueue.filter(
    (value) => candidateItemMatchesStatus(
      value, candidateLoadedStatus)).length;
  if (knownRemaining <= CANDIDATE_REFILL_LOW_WATER &&
      candidateFilteredTotal > knownRemaining) {
    ensureCandidateReviewQueueRefill().catch(() => undefined);
  }
  if (!upcoming.length) {
    if (candidateFilteredTotal > knownRemaining) {
      try {
        await ensureCandidateReviewQueueRefill();
      } catch (_error) {
        return;
      }
      upcoming = nextMatchingCandidates();
    }
  }
  upcoming.slice(0, CANDIDATE_IMAGE_PREFETCH_TARGET)
    .forEach((item) => prefetchCandidateImage(item));
  if (upcoming.length) ensureCandidateReviewWarm();
}

function renderCandidateItem() {
  const item = currentCandidate();
  pruneCandidateNavigationPrefetches();
  const image = $('#candidate-image');
  const empty = $('#candidate-empty');
  renderCandidateProgress();
  $('#btn-candidate-prev').disabled = !item || candidateIndex <= 0;
  $('#btn-candidate-next').disabled = !item || candidateIndex >= candidateQueue.length - 1;
  if (!item) {
    image.onload = null;
    image.removeAttribute('src');
    $('#candidate-image-wrap').classList.add('hidden');
    empty.classList.remove('hidden');
    $('#candidate-meta').textContent = '';
    renderCandidateHeroFilterReasons(null);
    $('#candidate-label-actions').innerHTML = '';
    $('#candidate-suggestion').textContent = '--';
    $('#candidate-prefill-status').textContent = '';
    $('#candidate-reason').textContent = '';
    $('#btn-candidate-save').disabled = true;
    candidateDraft = null;
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
    candidateBoxes = [];
    resetCandidateHeroReview();
    renderCandidateHeroContextControls();
    renderCandidateBoxes();
    return;
  }
  empty.classList.add('hidden');
  $('#candidate-image-wrap').classList.remove('hidden');
  const frameId = item.frame_id;
  candidateFormTouched = false;
  candidateHeroContextTouched = false;
  image.onload = () => {
    if (currentCandidate() && currentCandidate().frame_id === frameId) {
      renderCandidateBoxes();
    }
  };
  const prefetchedImage = candidateImagePrefetches.get(Number(frameId));
  image.src = prefetchedImage && prefetchedImage.image.src
    ? prefetchedImage.image.src : candidateImageUrl(item);
  candidateHeroTeamSizeExplicit = false;
  candidateHeroTeamSizeOverride = null;
  candidateHeroDrawMode = false;
  candidateDraft = candidateDefaultDraft(item);
  const suggestedBox = candidateDraft.result_panel_label === 'result_panel'
    ? candidateSuggestedResultBox(item) : null;
  candidateBoxes = suggestedBox ? [suggestedBox] : [];
  $('#candidate-notes').value = item.notes || '';
  const legacyMeta = item.legacy_hero_needs_review
    ? ` · 历史第 ${item.legacy_hero_match_index} 局的 ` +
      `${CANDIDATE_HERO_LAYOUT_SHORT_LABELS[item.legacy_hero_screen_type]}，` +
      `本组 ${item.legacy_hero_group_size} 张只保留这张代表图补英雄`
    : '';
  $('#candidate-meta').textContent =
    `${item.streamer || '未知主播'} / ${item.filename || ''} · ` +
    `${(item.timestamp_ms / 1000).toFixed(1)}s · frame #${item.frame_id} · ` +
    `来源：${candidateSourceText(item)}（${item.source_count || 0} 条记录）${legacyMeta}`;
  renderCandidateHeroFilterReasons(item);
  renderCandidateSuggestions(item);
  renderCandidateChoices();
  $('#btn-candidate-save').disabled = false;
  $('#candidate-save-state').classList.remove('error');
  const historical = (item.source_categories || []).includes('legacy');
  $('#candidate-save-state').textContent = item.legacy_hero_needs_review
    ? '历史分类标签已迁移并保留；这里只补头像来源、圆框、英雄和本人位置'
    : item.legacy_migration_needs_review
      ? '旧格式标签已经预填，但你尚未按新流程确认；请核对完整分类和英雄标注'
    : item.needs_afk_review
    ? '请点亮实际挂机的英雄；未点亮的可见英雄会在确认时保存为“未挂机”'
    : item.needs_player_hero_review
    ? '原标注已保留，请补齐英雄阵容并标出主播本人'
    : item.review_status === 'confirmed'
      ? historical
        ? '历史人工标签已保留；顶部新模型结果只作对照，不会覆盖真值'
        : '这张图已经人工确认'
      : item.review_status === 'partial'
      ? '历史数据只覆盖了部分标签，请补齐后确认'
      : item.review_status === 'skipped' ? '已跳过' : '';
  $('#btn-candidate-skip').disabled =
    Boolean(item.legacy_hero_needs_review) ||
    (item.review_status === 'confirmed' &&
      !item.legacy_migration_needs_review);
  renderCandidateBoxes();
  loadCandidateHeroLineup(item);
  prefetchNextCandidate();
}

function renderCandidateHeroFilterReasons(item) {
  const container = $('#candidate-hero-filter-reasons');
  if (!container) return;
  const matches = Array.isArray(item && item.hero_filter_matches)
    ? item.hero_filter_matches : [];
  container.replaceChildren();
  container.classList.toggle('hidden', !matches.length);
  const reasonNames = {
    direct_confirmed: '本图已人工确认',
    direct_suggested: '本图模型识别',
    same_match: '同局阵容包含，本图未识别',
    same_video: '仅同视频相关',
  };
  matches.forEach((match) => {
    const hero = candidateHeroByLabel(match.hero_label);
    const evidence = match.evidence_source === 'human' ? '人工' : '模型';
    const badge = document.createElement('span');
    badge.className = 'candidate-hero-filter-reason ' +
      String(match.reason || '').replaceAll('_', '-');
    const reason = reasonNames[match.reason] || '英雄相关素材';
    badge.textContent = `${hero ? hero.name : match.hero_label} · ` +
      `${['same_match', 'same_video'].includes(match.reason) ? evidence : ''}${reason}`;
    if (match.match_id) badge.title = `关联对局 #${match.match_id}`;
    container.appendChild(badge);
  });
}

async function loadCandidateReview() {
  const status = $('#candidate-status-filter').value;
  const sourceScope = candidateSourceScope;
  const loadToken = ++candidateReviewLoadToken;
  if (candidateReviewAbortController) candidateReviewAbortController.abort();
  const controller = new AbortController();
  candidateReviewAbortController = controller;
  const filterState = $('#candidate-filter-state');
  filterState.textContent = '正在筛选素材…';
  filterState.classList.add('loading');
  candidateReviewStats = {};
  renderCandidateMaterialSuggestionButton();
  void loadCandidateReviewStats(status, sourceScope);
  try {
    const data = await api(candidateReviewQuery(status), {
      signal: controller.signal,
    });
    if (loadToken !== candidateReviewLoadToken ||
        sourceScope !== candidateSourceScope ||
        status !== $('#candidate-status-filter').value) return;
    candidateLoadedSourceScope = sourceScope;
    candidateLoadedStatus = status;
    renderCandidateLegacyControls(candidateReviewStats, status);
    candidateFilteredTotal = Number(
      data.filtered_total ?? candidateReviewTotal(data.stats || {}, status));
    candidateSessionCompleted = 0;
    pruneCandidatePrefetches();
    candidateReviewRefillPromise = null;
    candidateQueue = data.items || [];
    candidateIndex = 0;
    renderCandidateItem();
    filterState.textContent = `筛选完成 · 共 ${candidateFilteredTotal} 张`;
  } catch (error) {
    if (error.name === 'AbortError') return;
    $('#candidate-save-state').textContent = '加载失败：' + error.message;
    filterState.textContent = '筛选失败：' + error.message;
  } finally {
    if (candidateReviewAbortController === controller) {
      candidateReviewAbortController = null;
      filterState.classList.remove('loading');
    }
  }
}

async function startCandidateAfkBackfill() {
  const button = $('#btn-candidate-afk-backfill');
  const state = $('#candidate-afk-backfill-state');
  button.disabled = true;
  state.textContent = '正在启动…';
  try {
    const data = await api('/api/training-review/afk-predictions/backfill', {
      method: 'POST',
      body: JSON.stringify({retry_failed: true}),
    });
    const counts = data.counts || {};
    state.textContent = `已重新排队 ${data.model_run_id || ''} · ` +
      `有挂机 ${counts.afk || 0} / 无挂机 ${counts.active || 0} / ` +
      `未运行 ${counts.pending || 0} / 失败 ${counts.failed || 0}`;
    await loadCandidateReview();
  } catch (error) {
    state.textContent = '启动失败：' + error.message;
  } finally {
    button.disabled = false;
  }
}

async function refillCandidateReviewQueue(loadToken = candidateReviewLoadToken) {
  const status = candidateLoadedStatus;
  const sourceScope = candidateLoadedSourceScope;
  const offset = candidateQueue.filter(
    (item) => candidateItemMatchesStatus(item, status)).length;
  const data = await api(
    candidateReviewQuery(status, offset));
  if (loadToken !== candidateReviewLoadToken ||
      $('#candidate-status-filter').value !== status ||
      candidateSourceScope !== sourceScope) return 0;
  candidateFilteredTotal = Number(
    data.filtered_total ?? candidateReviewTotal(candidateReviewStats, status));
  renderCandidateSyncStats(candidateReviewStats);
  const known = new Set(candidateQueue.map((item) => item.frame_id));
  const additions = (data.items || []).filter(
    (item) => !known.has(item.frame_id));
  candidateQueue.push(...additions);
  return additions.length;
}

function moveCandidate(offset) {
  if (!candidateQueue.length) return;
  candidateIndex = Math.max(
    0, Math.min(candidateQueue.length - 1, candidateIndex + offset));
  renderCandidateItem();
}

function showCandidateSaveError(message) {
  const saveState = $('#candidate-save-state');
  saveState.classList.add('error');
  saveState.textContent = message;
}

function candidateAfterSaveHint(loadedStatus) {
  const forward = nextMatchingCandidate();
  if (forward || !candidateStatusIsReviewQueue(loadedStatus)) return forward;
  return candidateQueue.find((value, index) =>
    index !== candidateIndex &&
    candidateItemMatchesStatus(value, loadedStatus)) || null;
}

async function prepareCandidateAfterSave(item) {
  if (!item) return;
  try {
    await prepareCandidateForReview(item);
  } catch (_error) {
    // 当前标注仍应正常保存；切换后会按普通加载流程重试下一张。
  }
}

async function saveCandidateReview(skip = false) {
  const item = currentCandidate();
  if (!item || !candidateDraft) return;
  const loadedStatus = candidateLoadedStatus;
  const matchedBeforeSave = candidateItemMatchesStatus(item, loadedStatus);
  $('#candidate-save-state').classList.remove('error');
  if (!skip && candidateHeroLoading) {
    showCandidateSaveError('英雄预填仍在进行，请稍等一下');
    return;
  }
  if (!skip && candidateDraft.result_panel_label === 'result_panel' &&
      !candidateBoxes.length) {
    showCandidateSaveError('请先在左侧框出完整结算面板');
    return;
  }
  if (!skip && ['select_3v3', 'select_5v5', 'select_blitz'].includes(
    candidateDraft.hero_select_label) && !candidateDraft.hero_select_variant) {
    showCandidateSaveError('请再选择这个英雄选择界面是 BP、盲选还是看不清');
    return;
  }
  if (!skip && candidateDraft.match_flow_label === 'match_flow' &&
      (!CANDIDATE_MATCH_KINDS[candidateDraft.match_kind_label] ||
       !CANDIDATE_VIEW_CONTEXTS[candidateDraft.view_context_label])) {
    showCandidateSaveError('请补充对局性质和观看方式');
    return;
  }
  const heroContext = candidateHeroContext(item);
  if (!skip && heroContext) {
    if (!heroContext.teamSize) {
      showCandidateSaveError('请先选择每队人数');
      return;
    }
    if (!candidateHeroLineup ||
        candidateHeroLineup.screen_type !== heroContext.screenType ||
        candidateHeroLineup.team_size !== heroContext.teamSize) {
      showCandidateSaveError('英雄阵容尚未加载完成，请稍等后再确认');
      return;
    }
    const allowsPartial = candidateHeroAllowsPartialLineup(candidateDraft);
    if (!candidateHeroLineup.slots.length) {
      showCandidateSaveError(allowsPartial
        ? '请至少圈出一个实际可见的英雄头像'
        : '请按顺序画满全部英雄圆框');
      return;
    }
    if (!allowsPartial && !candidateHeroLayoutComplete()) {
      showCandidateSaveError('请按顺序画满全部英雄圆框');
      return;
    }
  }
  const heroLabels = !skip && heroContext && candidateHeroLineup
    ? candidateHeroLineup.slots.map((slot) => ({
      side: slot.side,
      slot: slot.slot,
      hero_label: candidateHeroDraft.get(
        candidateHeroKey(slot.side, slot.slot)) || '',
      ...(heroContext.screenType === 'result_page'
        ? {is_afk: slot.is_afk === true} : {}),
    })) : null;
  const missingHero = heroLabels && heroLabels.find(
    (value) => !value.hero_label);
  if (missingHero) {
    showCandidateMissingHero(missingHero);
    return;
  }
  const playerPosition = candidateHeroPlayerPosition();
  const marksPlayer = !skip && heroContext &&
    candidateDraft.view_context_label === 'played' &&
    ['scoreboard', 'result_page'].includes(heroContext.screenType);
  if (marksPlayer && (
    candidateHeroPlayerStatus === 'pending' ||
    (candidateHeroPlayerStatus === 'identified' && !playerPosition)
  )) {
    showCandidateMissingPlayerHero();
    return;
  }
  $('#candidate-save-state').textContent = '正在保存，同时准备下一张…';
  try {
    if (!skip && heroContext) {
      if (candidateHeroRecognitionDebounceTimer !== null) {
        window.clearTimeout(candidateHeroRecognitionDebounceTimer);
        candidateHeroRecognitionDebounceTimer = null;
        flushCandidateHeroRecognition();
      }
      await candidateHeroPersistQueue;
    }
    const heroLineupPayload = heroLabels && (
      candidateHeroDirty || candidateHeroAfkReviewRequired ||
      loadedStatus === 'missing_afk' ||
      candidateHeroLineup.review_status !== 'confirmed'
    ) ? {
      heroes: heroLabels,
      player_status: candidateHeroPlayerStatus,
      player_side: playerPosition && playerPosition.side,
      player_slot: playerPosition && playerPosition.slot,
    } : null;
    const labels = skip ? {
      match_flow_label: null, match_mode_label: null,
      match_kind_label: null, view_context_label: null,
      hero_select_label: null, hero_select_variant: null,
      hero_select_visibility: null,
      result_panel_label: null,
      hero_layout_label: null,
    } : candidateDraft;
    const savePromise = api(`/api/training-review/items/${item.frame_id}`, {
      method: 'PUT',
      body: JSON.stringify({
        ...labels,
        review_status: skip ? 'skipped' : 'confirmed',
        result_box: !skip && candidateDraft.result_panel_label === 'result_panel'
          ? candidateBoxes[0] : null,
        hero_lineup: heroLineupPayload,
        notes: $('#candidate-notes').value,
      }),
    });
    const nextHint = candidateAfterSaveHint(loadedStatus);
    const nextPreparation = prepareCandidateAfterSave(nextHint);
    const knownBeforeSave = candidateQueue.filter(
      (value) => candidateItemMatchesStatus(value, loadedStatus)).length;
    const refillPreparation = !nextHint &&
        candidateFilteredTotal > knownBeforeSave
      ? ensureCandidateReviewQueueRefill()
        .then(() => null)
        .catch((error) => error)
      : Promise.resolve(null);
    const saved = await savePromise;
    if (saved.hero_lineup) {
      candidateHeroLineup = saved.hero_lineup;
      candidateHeroPlayerStatus = candidateHeroPlayerStatusForLineup(
        candidateHeroLineup);
      candidateHeroPlayerSlot = candidateHeroPlayerKey(candidateHeroLineup);
      candidateHeroAfkReviewRequired =
        candidateHeroLineup.screen_type === 'result_page' &&
        candidateHeroLineup.slots.some((slot) => slot.is_afk == null);
      saved.needs_afk_review = candidateHeroAfkReviewRequired;
      candidateHeroDirty = false;
    }
    const updated = {...item, ...saved};
    if (['confirmed', 'human_confirmed'].includes(loadedStatus) &&
        $('#candidate-review-reason-filter').value) {
      updated.review_filter_completed = true;
    }
    if (!skip) {
      cacheCandidateReviewLabels(candidateDraft);
      cacheCandidateMatchContext(item, candidateDraft);
      candidateHeroManualSlots.clear();
    }
    candidateQueue[candidateIndex] = updated;
    if (matchedBeforeSave && !candidateItemMatchesStatus(updated, loadedStatus)) {
      candidateFilteredTotal = Math.max(0, candidateFilteredTotal - 1);
      candidateSessionCompleted += 1;
    }
    const nextMatchingIndex = () => candidateQueue.findIndex(
      (value, index) => index > candidateIndex &&
        candidateItemMatchesStatus(value, loadedStatus));
    let nextIndex = nextMatchingIndex();
    let refillError = null;
    const knownRemaining = candidateQueue.filter(
      (value) => candidateItemMatchesStatus(value, loadedStatus)).length;
    if (nextIndex < 0 && candidateFilteredTotal > knownRemaining) {
      refillError = await refillPreparation;
      if (!refillError && !nextHint) {
        const preparedAfterRefill = candidateAfterSaveHint(loadedStatus);
        await prepareCandidateAfterSave(preparedAfterRefill);
      } else if (!refillError && nextHint) {
        try {
          await ensureCandidateReviewQueueRefill();
        } catch (error) {
          refillError = error;
        }
      }
      nextIndex = nextMatchingIndex();
    }
    if (nextIndex < 0 && candidateStatusIsReviewQueue(loadedStatus)) {
      nextIndex = candidateQueue.findIndex(
        (value) => candidateItemMatchesStatus(value, loadedStatus));
    }
    if (nextIndex >= 0) {
      candidateIndex = nextIndex;
      const nextItem = candidateQueue[nextIndex];
      if (nextHint && Number(nextHint.frame_id) === Number(nextItem.frame_id)) {
        await nextPreparation;
      } else {
        await prepareCandidateAfterSave(nextItem);
      }
    }
    renderCandidateItem();
    void loadCandidateReviewStats(loadedStatus, candidateLoadedSourceScope);
    if (refillError) {
      showCandidateSaveError('本张已保存，但加载下一批失败：' + refillError.message);
    } else if (candidateStatusIsReviewQueue(loadedStatus) &&
        candidateFilteredTotal === 0) {
      $('#candidate-save-state').textContent = '当前筛选条件下已经全部标完';
    }
  } catch (error) {
    showCandidateSaveError('保存失败：' + error.message);
  }
}

function candidatePoint(event) {
  const rect = $('#candidate-box-layer').getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

function candidatePixelPoint(event) {
  const rect = $('#candidate-box-layer').getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(rect.width, event.clientX - rect.left)),
    y: Math.max(0, Math.min(rect.height, event.clientY - rect.top)),
    width: rect.width,
    height: rect.height,
  };
}

function candidateHeroDrawingDiameter(width, height) {
  const first = candidateHeroLineup && candidateHeroLineup.slots[0];
  if (!first) return null;
  const crop = candidateHeroDisplayCrop(first.crop, {width, height});
  const diameter = crop.w * width;
  return Number.isFinite(diameter) && diameter > 0 ? diameter : null;
}

function candidateHeroClickCrop(point) {
  const diameter = candidateHeroDrawingDiameter(point.width, point.height);
  if (!diameter) return null;
  const size = Math.min(diameter, point.width, point.height);
  const left = candidateHeroClamp(
    point.x - size / 2, 0, point.width - size);
  const top = candidateHeroClamp(
    point.y - size / 2, 0, point.height - size);
  return {
    x: left / point.width,
    y: top / point.height,
    w: size / point.width,
    h: size / point.height,
  };
}

function bindCandidateReview() {
  const candidateView = $('#view-candidates');
  ['pointerdown', 'input', 'change'].forEach((eventName) => {
    candidateView.addEventListener(eventName, (event) => {
      if (event.isTrusted) candidateFormTouched = true;
    }, true);
  });
  setCandidateSourceScope('new', 'needs_review', false);
  $('#btn-candidate-material-suggestions').onclick =
    openCandidateMaterialSuggestions;
  $('#btn-candidate-material-close').onclick = () => {
    $('#candidate-material-dialog').close();
  };
  $('#candidate-material-dialog').onclick = (event) => {
    if (event.target === $('#candidate-material-dialog')) {
      $('#candidate-material-dialog').close();
    }
  };
  $('#candidate-status-filter').onchange = () => {
    renderCandidateReviewReasonFilter();
    loadCandidateReview();
  };
  $('#candidate-legacy-streamer').onchange = loadCandidateReview;
  $('#candidate-legacy-screen').onchange = loadCandidateReview;
  [
    '#candidate-source-type-filter', '#candidate-scene-filter',
    '#candidate-mode-filter', '#candidate-match-kind-filter',
    '#candidate-view-context-filter', '#candidate-confidence-filter',
    '#candidate-review-reason-filter', '#candidate-streamer-filter',
  ].forEach((selector) => {
    $(selector).onchange = loadCandidateReview;
  });
  $('#candidate-afk-prediction-filter').onchange = () => {
    if ($('#candidate-afk-prediction-filter').value) {
      $('#candidate-source-type-filter').value = '';
    }
    loadCandidateReview();
  };
  $('#btn-candidate-clear-filters').onclick = () => {
    [
      '#candidate-source-type-filter', '#candidate-scene-filter',
      '#candidate-mode-filter', '#candidate-match-kind-filter',
      '#candidate-view-context-filter', '#candidate-confidence-filter',
      '#candidate-review-reason-filter', '#candidate-streamer-filter',
      '#candidate-afk-prediction-filter',
    ].forEach((selector) => { $(selector).value = ''; });
    if (candidateSourceScope === 'new') {
      $('#candidate-source-type-filter').value = CANDIDATE_DEFAULT_SOURCE_TYPE;
    }
    candidateHeroFilters = new Set();
    candidateHeroScope = 'all';
    $('#candidate-hero-filter-search').value = '';
    $('#candidate-hero-filter-summary').textContent = '全部英雄';
    renderCandidateHeroFilter();
    loadCandidateReview();
  };
  $('#btn-candidate-refresh').onclick = () => {
    candidateFilterOptionsLoadedScope = '';
    ensureCandidateFilterOptions();
    loadCandidateReview();
  };
  $('#btn-candidate-afk-backfill').onclick = startCandidateAfkBackfill;
  $('#candidate-streamer-filter').onfocus = ensureCandidateFilterOptions;
  $('#candidate-hero-filter').ontoggle = () => {
    if ($('#candidate-hero-filter').open) ensureCandidateFilterOptions();
  };
  $('#candidate-hero-filter-search').oninput = renderCandidateHeroFilter;
  $('#btn-candidate-prev').onclick = () => moveCandidate(-1);
  $('#btn-candidate-next').onclick = () => moveCandidate(1);
  $('#btn-candidate-clear-boxes').onclick = () => {
    candidateBoxes = [];
    renderCandidateBoxes();
  };
  $('#btn-candidate-skip').onclick = () => saveCandidateReview(true);
  $('#btn-candidate-save').onclick = () => saveCandidateReview();
  $('#candidate-hero-search').oninput = renderCandidateHeroOptions;
  $('#btn-candidate-hero-picker-close').onclick = closeCandidateHeroPicker;
  $('#btn-candidate-hero-delete').onclick = deleteCandidateHeroSlot;
  $('#btn-candidate-hero-recognize').onclick = recognizeCandidateHeroes;
  $('#btn-candidate-hero-draw').onclick = () => {
    if (!candidateHeroLineup || candidateHeroLayoutComplete()) return;
    candidateHeroDrawMode = !candidateHeroDrawMode;
    renderCandidateHeroLineup();
    renderCandidateChoices();
  };
  $('#btn-candidate-hero-clear').onclick = clearCandidateHeroLayout;
  $('#btn-candidate-player-unreadable').onclick = () => {
    candidateHeroPlayerStatus = 'unreadable';
    candidateHeroPlayerSlot = null;
    candidateHeroDirty = true;
    $('#candidate-save-state').classList.remove('error');
    $('#candidate-save-state').textContent = '';
    renderCandidateHeroLineup();
  };
  document.addEventListener('pointerdown', (event) => {
    const picker = $('#candidate-hero-picker');
    if (!picker.classList.contains('hidden') &&
        !picker.contains(event.target) &&
        !event.target.closest('.candidate-hero-select') &&
        !event.target.closest('.candidate-hero-circle')) {
      closeCandidateHeroPicker();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeCandidateHeroPicker();
  });
  const layer = $('#candidate-box-layer');
  layer.onpointerdown = async (event) => {
    const item = currentCandidate();
    if (!item || !candidateDraft ||
        event.target.closest('.candidate-box') ||
        event.target.closest('.candidate-hero-circle')) return;
    const canDrawHero = candidateHeroDrawMode &&
      candidateHeroLineup && candidateNextHeroPosition();
    const canDrawResult = !candidateHeroDrawMode &&
      candidateDraft.result_panel_label === 'result_panel';
    if (!canDrawHero && !canDrawResult) return;
    event.preventDefault();
    if (canDrawHero) {
      const point = candidatePixelPoint(event);
      const crop = candidateHeroClickCrop(point);
      if (crop) {
        candidateDrawStart = null;
        addCandidateHeroCircle(crop);
        return;
      }
    }
    layer.setPointerCapture(event.pointerId);
    candidateDrawStart = canDrawHero
      ? {kind: 'hero', point: candidatePixelPoint(event)}
      : {kind: 'result', point: candidatePoint(event)};
  };
  layer.onpointerup = async (event) => {
    if (!candidateDrawStart) return;
    const start = candidateDrawStart;
    candidateDrawStart = null;
    if (start.kind === 'hero') {
      const end = candidatePixelPoint(event);
      const dx = end.x - start.point.x;
      const dy = end.y - start.point.y;
      const fixedSize = candidateHeroDrawingDiameter(end.width, end.height);
      let size = fixedSize || Math.max(Math.abs(dx), Math.abs(dy));
      size = Math.min(size, end.width, end.height);
      if (!fixedSize && size < 14) return;
      const rawLeft = fixedSize
        ? (start.point.x + end.x - size) / 2
        : dx < 0 ? start.point.x - size : start.point.x;
      const rawTop = fixedSize
        ? (start.point.y + end.y - size) / 2
        : dy < 0 ? start.point.y - size : start.point.y;
      const left = Math.max(0, Math.min(end.width - size, rawLeft));
      const top = Math.max(0, Math.min(end.height - size, rawTop));
      addCandidateHeroCircle({
        x: left / end.width,
        y: top / end.height,
        w: size / end.width,
        h: size / end.height,
      });
      return;
    }
    const end = candidatePoint(event);
    const x = Math.min(start.point.x, end.x);
    const y = Math.min(start.point.y, end.y);
    const w = Math.abs(end.x - start.point.x);
    const h = Math.abs(end.y - start.point.y);
    if (w < 0.01 || h < 0.01) return;
    candidateBoxes = [{type: 'result_panel', x, y, w, h}];
    renderCandidateBoxes();
    renderCandidateChoices();
  };
  layer.onpointercancel = () => { candidateDrawStart = null; };
}

// ---------- BP 主动学习复核 ----------
const BP_REVIEW_LABELS = {
  bp_3v3: '3V3 BP',
  bp_aram: '大乱斗 BP',
  bp_5v5: '5V5 BP',
  not_bp: '不是选英雄画面',
};
const BP_VISUAL_CONDITIONS = {
  clear: '清晰',
  occluded: '有遮挡',
  windowed: '小窗／非全屏',
  occluded_windowed: '遮挡＋小窗',
  unreadable: '模式看不清（不进入模式训练）',
};

function bpPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function bpCurrentItem() {
  return bpReviewQueue[bpReviewIndex] || null;
}

function renderBpVisualCondition(enabled = true) {
  $$('.bp-condition-actions button').forEach((button) => {
    button.classList.toggle(
      'selected', button.dataset.bpCondition === bpVisualCondition);
    button.disabled = !enabled;
  });
  $('#bp-condition-state').textContent = enabled
    ? `当前：${BP_VISUAL_CONDITIONS[bpVisualCondition]}` : '';
}

function selectBpVisualCondition(value) {
  if (!BP_VISUAL_CONDITIONS[value] || !bpCurrentItem()) return;
  bpVisualCondition = value;
  renderBpVisualCondition(true);
}

function renderBpStats(stats) {
  const statuses = (stats && stats.statuses) || {};
  const existing = (stats && stats.existing_human_labels) || {};
  const existingText = ['3v3', 'aram', '5v5']
    .map((mode) => {
      const item = existing[mode] || { frames: 0, videos: 0 };
      return `${mode}:${item.frames}张/${item.videos}视频`;
    })
    .join(' · ');
  $('#bp-review-counts').textContent =
    `待确认 ${statuses.pending || 0} · 已确认 ${statuses.confirmed || 0} · ` +
    `已跳过 ${statuses.skipped || 0}｜原有人工标签 ${existingText}`;
}

function renderBpReviewItem() {
  const item = bpCurrentItem();
  const img = $('#bp-review-img');
  const empty = $('#bp-review-empty');
  $('#bp-review-progress').textContent = bpReviewQueue.length
    ? `${bpReviewIndex + 1}/${bpReviewQueue.length}` : '0/0';
  $$('.bp-review-actions button').forEach((button) => {
    button.classList.toggle(
      'model-choice', item && button.dataset.bpLabel === item.suggested_label);
    button.disabled = !item;
  });
  bpVisualCondition = (item && item.visual_condition) || 'clear';
  renderBpVisualCondition(Boolean(item));
  if (!item) {
    img.classList.remove('active');
    img.removeAttribute('src');
    empty.classList.remove('hidden');
    $('#bp-review-meta').textContent = '';
    $('#bp-suggestion').textContent = '--';
    $('#bp-model-detail').textContent = '';
    $('#bp-selection-reason').textContent = '--';
    $('#btn-bp-confirm-suggestion').disabled = true;
    return;
  }
  empty.classList.add('hidden');
  img.src = `/api/frames/${item.frame_id}/image?t=${item.frame_id}`;
  img.classList.add('active');
  $('#bp-review-meta').textContent =
    `${item.streamer} / ${item.filename} · ${(item.timestamp_ms / 1000).toFixed(1)}s · ` +
    `frame #${item.frame_id}`;
  $('#bp-suggestion').textContent =
    `${BP_REVIEW_LABELS[item.suggested_label] || item.suggested_label} ` +
    `${bpPercent(item.suggestion_confidence)}`;
  $('#bp-model-detail').textContent =
    `阶段=${item.stage_class} ${bpPercent(item.stage_confidence)}；` +
    `pre_match=${bpPercent(item.pre_match_confidence)}；` +
    `模式=${item.mode_class} ${bpPercent(item.mode_confidence)}`;
  $('#bp-selection-reason').textContent = item.selection_reason;
  $('#btn-bp-confirm-suggestion').disabled = false;
  const reviewed = item.review_status === 'confirmed'
    ? `人工确认：${BP_REVIEW_LABELS[item.confirmed_label] || item.confirmed_label}；` +
      `画面：${BP_VISUAL_CONDITIONS[item.visual_condition] || '清晰'}`
    : item.review_status === 'skipped' ? '人工处理：已跳过' : '';
  $('#bp-review-save-state').textContent = reviewed;
}

async function loadBpReview() {
  const status = $('#bp-review-filter').value;
  try {
    const data = await api(`/api/bp-review/items?status=${encodeURIComponent(status)}&limit=1000`);
    bpReviewQueue = data.items || [];
    bpReviewIndex = 0;
    renderBpStats(data.stats);
    renderBpReviewItem();
    await refreshBpCollectState(false);
    await refreshBpWorkerSyncState(false);
  } catch (error) {
    $('#bp-review-save-state').textContent = '加载失败：' + error.message;
  }
}

function moveBpReview(offset) {
  if (!bpReviewQueue.length) return;
  bpReviewIndex = Math.max(
    0, Math.min(bpReviewQueue.length - 1, bpReviewIndex + offset));
  renderBpReviewItem();
}

async function saveBpReview(label) {
  const item = bpCurrentItem();
  if (!item) return;
  $('#bp-review-save-state').textContent = '正在保存…';
  try {
    const visualCondition = (label === 'skip' || label === 'not_bp')
      ? 'clear' : bpVisualCondition;
    const updated = await api(`/api/bp-review/items/${item.frame_id}`, {
      method: 'PUT', body: JSON.stringify({
        label, visual_condition: visualCondition,
      }),
    });
    const filter = $('#bp-review-filter').value;
    if (filter === 'pending') {
      bpReviewQueue.splice(bpReviewIndex, 1);
      bpReviewIndex = Math.min(bpReviewIndex, Math.max(0, bpReviewQueue.length - 1));
    } else {
      bpReviewQueue[bpReviewIndex] = updated;
    }
    $('#bp-review-save-state').textContent = label === 'skip'
      ? '已跳过'
      : `已确认：${BP_REVIEW_LABELS[label]}；` +
        `画面：${BP_VISUAL_CONDITIONS[visualCondition]}`;
    renderBpReviewItem();
    const state = await api('/api/bp-review/state');
    renderBpStats(state.review);
  } catch (error) {
    $('#bp-review-save-state').textContent = '保存失败：' + error.message;
  }
}

async function refreshBpCollectState(reloadWhenDone = true) {
  const state = await api('/api/bp-review/state');
  renderBpStats(state.review);
  $('#btn-bp-collect').disabled = Boolean(state.running);
  if (state.error) {
    $('#bp-collect-state').textContent = `收集失败：${state.error}`;
  } else if (state.running) {
    $('#bp-collect-state').textContent =
      `正在推理 ${state.scanned}/${state.total || '?'}，失败 ${state.failed || 0}`;
  } else if (state.total) {
    $('#bp-collect-state').textContent =
      `已扫描 ${state.scanned} 张，挑出 ${state.selected} 张，新增 ${state.inserted} 张`;
  } else {
    $('#bp-collect-state').textContent = '';
  }
  if (!state.running && bpCollectTimer) {
    clearInterval(bpCollectTimer);
    bpCollectTimer = null;
    if (reloadWhenDone) loadBpReview();
  }
  return state;
}

async function startBpCollection() {
  const maximumScan = Number($('#bp-scan-limit').value) || 3000;
  const maximumItems = Number($('#bp-item-limit').value) || 300;
  $('#bp-collect-state').textContent = '正在启动…';
  try {
    await api('/api/bp-review/collect', {
      method: 'POST',
      body: JSON.stringify({
        model_name: 'multi-v2', maximum_scan: maximumScan,
        maximum_items: maximumItems, maximum_per_video: 24,
      }),
    });
    $('#btn-bp-collect').disabled = true;
    if (bpCollectTimer) clearInterval(bpCollectTimer);
    bpCollectTimer = setInterval(() => refreshBpCollectState(true), 1000);
    refreshBpCollectState(true);
  } catch (error) {
    $('#bp-collect-state').textContent = '无法启动：' + error.message;
  }
}

async function refreshBpWorkerSyncState(reloadWhenDone = true) {
  const state = await api('/api/bp-review/state');
  const sync = state.worker_sync || {};
  renderBpStats(state.review);
  $('#btn-bp-sync-worker').disabled = Boolean(sync.running);
  if (sync.error) {
    $('#bp-worker-sync-state').textContent = `同步失败：${sync.error}`;
  } else if (sync.running) {
    $('#bp-worker-sync-state').textContent =
      `正在同步 ${sync.processed || 0}/${sync.total || '?'}，` +
      `已下载 ${sync.downloaded || 0}，失败 ${sync.failed || 0}`;
  } else if (sync.processed) {
    $('#bp-worker-sync-state').textContent =
      `已处理 ${sync.processed} 张，新增 ${sync.inserted}，` +
      `已登记 ${sync.unchanged || 0}，更新 ${sync.updated}，` +
      `下载 ${sync.downloaded}，失败 ${sync.failed}` +
      (sync.last_error ? `（最近错误：${sync.last_error}）` : '');
  } else {
    $('#bp-worker-sync-state').textContent = '';
  }
  if (!sync.running && bpWorkerSyncTimer) {
    clearInterval(bpWorkerSyncTimer);
    bpWorkerSyncTimer = null;
    if (reloadWhenDone) loadBpReview();
  }
  return sync;
}

async function startBpWorkerSync() {
  $('#bp-worker-sync-state').textContent = '正在连接 NAS…';
  try {
    await api('/api/bp-review/sync-worker', {
      method: 'POST', body: JSON.stringify({ maximum: 10000 }),
    });
    $('#btn-bp-sync-worker').disabled = true;
    if (bpWorkerSyncTimer) clearInterval(bpWorkerSyncTimer);
    bpWorkerSyncTimer = setInterval(
      () => refreshBpWorkerSyncState(true), 1000);
    refreshBpWorkerSyncState(true);
  } catch (error) {
    $('#bp-worker-sync-state').textContent = '无法同步：' + error.message;
  }
}

async function exportBpDataset() {
  $('#bp-export-state').textContent = '正在导出…';
  try {
    const result = await api('/api/export', {
      method: 'POST', body: JSON.stringify({ task_id: 'bp_review' }),
    });
    const counts = result.by_label || {};
    $('#bp-export-state').textContent =
      `${result.version}：3V3 ${counts.bp_3v3 || 0}，大乱斗 ${counts.bp_aram || 0}，` +
      `5V5 ${counts.bp_5v5 || 0}，非BP ${counts.not_bp || 0}；` +
      `模式看不清未导出 ${result.excluded_unreadable || 0}`;
    loadDatasets();
  } catch (error) {
    $('#bp-export-state').textContent = '导出失败：' + error.message;
  }
}

function bindBpReview() {
  $('#btn-bp-sync-worker').onclick = startBpWorkerSync;
  $('#btn-bp-collect').onclick = startBpCollection;
  $('#btn-bp-export').onclick = exportBpDataset;
  $('#btn-bp-refresh').onclick = loadBpReview;
  $('#bp-review-filter').onchange = loadBpReview;
  $('#btn-bp-prev').onclick = () => moveBpReview(-1);
  $('#btn-bp-next').onclick = () => moveBpReview(1);
  $('#btn-bp-confirm-suggestion').onclick = () => {
    const item = bpCurrentItem();
    if (item) saveBpReview(item.suggested_label);
  };
  $$('.bp-review-actions button').forEach((button) => {
    button.onclick = () => saveBpReview(button.dataset.bpLabel);
  });
  $$('.bp-condition-actions button').forEach((button) => {
    button.onclick = () => selectBpVisualCondition(button.dataset.bpCondition);
  });
  document.addEventListener('keydown', (event) => {
    if (!$('#view-bp-review').classList.contains('active')) return;
    if (event.target.tagName === 'INPUT' || event.target.tagName === 'SELECT') return;
    const item = bpCurrentItem();
    if (!item) return;
    const key = event.key.toLowerCase();
    const labels = { '1': 'bp_3v3', '2': 'bp_aram', '3': 'bp_5v5',
                     n: 'not_bp', s: 'skip' };
    if (labels[key]) {
      event.preventDefault();
      saveBpReview(labels[key]);
    } else if (key === 'enter') {
      event.preventDefault();
      saveBpReview(item.suggested_label);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault(); moveBpReview(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault(); moveBpReview(1);
    }
  });
}

// ---------- 结算页 / 计分板主动学习复核 ----------
const KEY_SCREEN_LABELS = {
  result_page: '赛后结算页',
  scoreboard: '对局中计分板',
  other: '其他画面',
};
const KEY_VISUAL_CONDITIONS = {
  clear: '清晰',
  occluded: '有遮挡',
  unreadable: '看不清（不进入训练）',
};

function keyCurrentItem() {
  return keyReviewQueue[keyReviewIndex] || null;
}

function renderKeyVisualCondition(enabled = true) {
  $$('.key-condition-actions button').forEach((button) => {
    button.classList.toggle(
      'selected', button.dataset.keyCondition === keyVisualCondition);
    button.disabled = !enabled;
  });
  $('#key-condition-state').textContent = enabled
    ? `当前：${KEY_VISUAL_CONDITIONS[keyVisualCondition]}` : '';
}

function selectKeyVisualCondition(value) {
  if (!KEY_VISUAL_CONDITIONS[value] || !keyCurrentItem()) return;
  keyVisualCondition = value;
  renderKeyVisualCondition(true);
}

function renderKeyStats(stats) {
  const statuses = (stats && stats.statuses) || {};
  const existing = (stats && stats.existing_human_labels) || {};
  $('#key-review-counts').textContent =
    `待确认 ${statuses.pending || 0} · 已确认 ${statuses.confirmed || 0} · ` +
    `已跳过 ${statuses.skipped || 0}｜原有人工标签：` +
    `结算 ${existing.result_page || 0}、计分板 ${existing.scoreboard || 0}、` +
    `其他 ${existing.other || 0}`;
}

function renderKeyScreenReviewItem() {
  const item = keyCurrentItem();
  const img = $('#key-review-img');
  const empty = $('#key-review-empty');
  $('#key-review-progress').textContent = keyReviewQueue.length
    ? `${keyReviewIndex + 1}/${keyReviewQueue.length}` : '0/0';
  $$('.key-review-actions button').forEach((button) => {
    button.classList.toggle(
      'model-choice', item && button.dataset.keyLabel === item.suggested_label);
    button.disabled = !item;
  });
  keyVisualCondition = (item && item.visual_condition) || 'clear';
  renderKeyVisualCondition(Boolean(item));
  if (!item) {
    img.classList.remove('active');
    img.removeAttribute('src');
    empty.classList.remove('hidden');
    $('#key-review-meta').textContent = '';
    $('#key-suggestion').textContent = '--';
    $('#key-selection-reason').textContent = '--';
    $('#btn-key-confirm-suggestion').disabled = true;
    return;
  }
  empty.classList.add('hidden');
  img.src = `/api/frames/${item.frame_id}/image?t=${item.frame_id}`;
  img.classList.add('active');
  $('#key-review-meta').textContent =
    `${item.streamer} / ${item.filename} · ` +
    `${(item.timestamp_ms / 1000).toFixed(1)}s · frame #${item.frame_id}`;
  $('#key-suggestion').textContent =
    `${KEY_SCREEN_LABELS[item.suggested_label] || item.suggested_label} ` +
    `${bpPercent(item.suggestion_confidence)}`;
  $('#key-selection-reason').textContent = item.selection_reason;
  $('#btn-key-confirm-suggestion').disabled = false;
  const reviewed = item.review_status === 'confirmed'
    ? `人工确认：${KEY_SCREEN_LABELS[item.confirmed_label] || item.confirmed_label}；` +
      `画面：${KEY_VISUAL_CONDITIONS[item.visual_condition] || '清晰'}`
    : item.review_status === 'skipped' ? '人工处理：已跳过' : '';
  $('#key-review-save-state').textContent = reviewed;
}

async function loadKeyScreenReview() {
  const status = $('#key-review-filter').value;
  try {
    const data = await api(
      `/api/key-screen-review/items?status=${encodeURIComponent(status)}&limit=1000`);
    keyReviewQueue = data.items || [];
    keyReviewIndex = 0;
    renderKeyStats(data.stats);
    renderKeyScreenReviewItem();
    await refreshKeyWorkerSyncState(false);
  } catch (error) {
    $('#key-review-save-state').textContent = '加载失败：' + error.message;
  }
}

function moveKeyScreenReview(offset) {
  if (!keyReviewQueue.length) return;
  keyReviewIndex = Math.max(
    0, Math.min(keyReviewQueue.length - 1, keyReviewIndex + offset));
  renderKeyScreenReviewItem();
}

async function saveKeyScreenReview(label) {
  const item = keyCurrentItem();
  if (!item) return;
  $('#key-review-save-state').textContent = '正在保存…';
  try {
    const visualCondition = label === 'skip' ? 'clear' : keyVisualCondition;
    const updated = await api(
      `/api/key-screen-review/items/${item.frame_id}`,
      {
        method: 'PUT',
        body: JSON.stringify({
          label,
          visual_condition: visualCondition,
        }),
      });
    const filter = $('#key-review-filter').value;
    if (filter === 'pending') {
      keyReviewQueue.splice(keyReviewIndex, 1);
      keyReviewIndex = Math.min(
        keyReviewIndex, Math.max(0, keyReviewQueue.length - 1));
    } else {
      keyReviewQueue[keyReviewIndex] = updated;
    }
    $('#key-review-save-state').textContent = label === 'skip'
      ? '已跳过'
      : `已确认：${KEY_SCREEN_LABELS[label]}；` +
        `画面：${KEY_VISUAL_CONDITIONS[visualCondition]}`;
    renderKeyScreenReviewItem();
    const currentState = await api('/api/key-screen-review/state');
    renderKeyStats(currentState.review);
  } catch (error) {
    $('#key-review-save-state').textContent = '保存失败：' + error.message;
  }
}

async function refreshKeyWorkerSyncState(reloadWhenDone = true) {
  const currentState = await api('/api/key-screen-review/state');
  const sync = currentState.worker_sync || {};
  renderKeyStats(currentState.review);
  $('#btn-key-sync-worker').disabled = Boolean(sync.running);
  if (sync.error) {
    $('#key-worker-sync-state').textContent = `同步失败：${sync.error}`;
  } else if (sync.running) {
    $('#key-worker-sync-state').textContent =
      `正在同步 ${sync.processed || 0}/${sync.total || '?'}，` +
      `已下载 ${sync.downloaded || 0}，失败 ${sync.failed || 0}`;
  } else if (sync.processed) {
    const taskCounts = (sync.by_task || {}).key_screen_review || {};
    $('#key-worker-sync-state').textContent =
      `本队列新增 ${taskCounts.inserted || 0}、更新 ${taskCounts.updated || 0}；` +
      `全部候选共处理 ${sync.processed} 张，下载 ${sync.downloaded || 0}`;
  } else {
    $('#key-worker-sync-state').textContent = '';
  }
  if (!sync.running && keyWorkerSyncTimer) {
    clearInterval(keyWorkerSyncTimer);
    keyWorkerSyncTimer = null;
    if (reloadWhenDone) loadKeyScreenReview();
  }
  return sync;
}

async function startKeyWorkerSync() {
  $('#key-worker-sync-state').textContent = '正在连接 NAS…';
  try {
    await api('/api/worker-candidates/sync', {
      method: 'POST',
      body: JSON.stringify({ maximum: 10000 }),
    });
    $('#btn-key-sync-worker').disabled = true;
    if (keyWorkerSyncTimer) clearInterval(keyWorkerSyncTimer);
    keyWorkerSyncTimer = setInterval(
      () => refreshKeyWorkerSyncState(true), 1000);
    refreshKeyWorkerSyncState(true);
  } catch (error) {
    $('#key-worker-sync-state').textContent = '无法同步：' + error.message;
  }
}

async function exportKeyScreenDataset() {
  $('#key-export-state').textContent = '正在导出…';
  try {
    const result = await api('/api/export', {
      method: 'POST',
      body: JSON.stringify({ task_id: 'key_screen_review' }),
    });
    const counts = result.by_label || {};
    $('#key-export-state').textContent =
      `${result.version}：结算 ${counts.result_page || 0}，` +
      `计分板 ${counts.scoreboard || 0}，其他 ${counts.other || 0}；` +
      `看不清未导出 ${result.excluded_unreadable || 0}`;
    loadDatasets();
  } catch (error) {
    $('#key-export-state').textContent = '导出失败：' + error.message;
  }
}

function bindKeyScreenReview() {
  $('#btn-key-sync-worker').onclick = startKeyWorkerSync;
  $('#btn-key-export').onclick = exportKeyScreenDataset;
  $('#btn-key-refresh').onclick = loadKeyScreenReview;
  $('#key-review-filter').onchange = loadKeyScreenReview;
  $('#btn-key-prev').onclick = () => moveKeyScreenReview(-1);
  $('#btn-key-next').onclick = () => moveKeyScreenReview(1);
  $('#btn-key-confirm-suggestion').onclick = () => {
    const item = keyCurrentItem();
    if (item) saveKeyScreenReview(item.suggested_label);
  };
  $$('.key-review-actions button').forEach((button) => {
    button.onclick = () => saveKeyScreenReview(button.dataset.keyLabel);
  });
  $$('.key-condition-actions button').forEach((button) => {
    button.onclick = () => selectKeyVisualCondition(button.dataset.keyCondition);
  });
  document.addEventListener('keydown', (event) => {
    if (!$('#view-key-screen-review').classList.contains('active')) return;
    if (event.target.tagName === 'INPUT' || event.target.tagName === 'SELECT') return;
    const item = keyCurrentItem();
    if (!item) return;
    const key = event.key.toLowerCase();
    const labels = {
      '1': 'result_page',
      '2': 'scoreboard',
      n: 'other',
      s: 'skip',
    };
    if (labels[key]) {
      event.preventDefault();
      saveKeyScreenReview(labels[key]);
    } else if (key === 'enter') {
      event.preventDefault();
      saveKeyScreenReview(item.suggested_label);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      moveKeyScreenReview(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      moveKeyScreenReview(1);
    }
  });
}

// ---------- 选择器构建(动态按钮组在 renderInspector 中按需构建) ----------
function buildSelects() {
  const family = $('#grp-family');
  Object.entries(CFG.content_families).forEach(([v, l], i) => {
    family.insertAdjacentHTML('beforeend',
      `<button class="opt" data-v="${v}">${l}</button>`);
  });
  const nonvg = $('#grp-nonvg');
  Object.entries(CFG.non_vainglory_types).forEach(([v, l]) => {
    nonvg.insertAdjacentHTML('beforeend',
      `<button class="opt" data-v="${v}">${l}</button>`);
  });
  const bars = $('#sel-bars');
  CFG.black_bars.forEach((v) => {
    bars.insertAdjacentHTML('beforeend', `<option value="${v}">${v}</option>`);
  });
  $('#screen-hint').textContent = CFG.scoreboard_vs_result_hint;
}

function buildStrategySelect() {
  const sel = $('#extract-strategy');
  Object.entries(CFG.strategies).forEach(([v, l]) => {
    sel.insertAdjacentHTML('beforeend', `<option value="${v}">${v} - ${l}</option>`);
  });
  updateStrategyHint();
  sel.onchange = updateStrategyHint;
}

const STRATEGY_HINTS = {
  uniform_every_n_seconds: '全片每「间隔(秒)」抽 1 帧原始分辨率(默认 5 秒)。不依赖旧模型,最简单可靠;2 小时视频约 1440 帧,适合做负样本。',
  existing_model_hits: '旧模型全片粗扫找结算面板命中点,命中前后各「候选窗口」秒按「密集帧率」抽原图。抓结算正样本用,但依赖旧模型(可能不准)。',
  dense_around_candidate: '同 existing_model_hits,但候选时间点由 API 的 candidates 参数指定(前端暂未暴露),一般用不到。',
  uniform_random: '全片随机选「随机负样本数」个时间点,每点抽 1 帧原图。用于收集"不是结算画面"的负样本,必须混入,否则模型会乱报。',
  manual_timestamps: '按「手动时间点」精确抽帧(每点 1 帧原图)。适合你已知道结算出现的确切时间。',
  dense_interval: '在「起止」区间内按「密集帧率」抽原图。适合对局结尾段(结算+积分板出现区间)整段密集抓取。',
  transition_windows: '粗扫检测画面大幅突变处(HUD 出现/消失、切场景),突变点前后 3 秒密集抽帧。用于转场/游戏内外切换。',
};

function updateStrategyHint() {
  const v = $('#extract-strategy').value;
  $('#strategy-hint').textContent = STRATEGY_HINTS[v] || '';
}

// ---------- 数据源 ----------
async function loadVideos() {
  const q = new URLSearchParams();
  const st = $('#filter-streamer').value.trim();
  const rm = $('#filter-room').value.trim();
  const sz = $('#filter-size').value;
  if (st) q.set('streamer', st);
  if (rm) q.set('room_id', rm);
  if (sz) q.set('min_size_bytes', sz);
  const videos = await api('/api/videos?' + q);
  const tbody = $('#video-table tbody');
  tbody.innerHTML = '';
  videos.forEach((v) => {
    const mins = v.duration_seconds ? Math.round(v.duration_seconds / 60) + 'min' : '';
    const mb = (v.size_bytes / 1048576).toFixed(0) + 'MB';
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td><input type="checkbox" class="vcheck" data-id="${v.id}"></td>
        <td>${esc(v.streamer)}</td>
        <td>${esc(v.filename)}</td>
        <td>${mins}</td><td>${mb}</td>
        <td>${v.frame_count}</td><td>${v.labeled_count}</td>
        <td class="status-${v.status}">${v.status}${v.error ? ' ' + esc(v.error) : ''}</td>
      </tr>`);
  });
  $('#sync-msg').textContent = `共 ${videos.length} 个视频`;
}
$('#filter-streamer').oninput = debounce(loadVideos, 400);
$('#filter-room').oninput = debounce(loadVideos, 400);
$('#filter-size').onchange = loadVideos;

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

$('#btn-sync').onclick = async () => {
  $('#sync-msg').textContent = '同步中(扫描 NAS,30~60 秒)…';
  await api('/api/sync', { method: 'POST' });
  const timer = setInterval(async () => {
    const st = await api('/api/sync/state');
    if (!st.running) {
      clearInterval(timer);
      $('#sync-msg').textContent = st.error
        ? '同步失败: ' + st.error : `同步完成,共 ${st.videos} 个视频`;
      loadVideos();
    }
  }, 2000);
};
$('#check-all').onchange = (e) => {
  $$('.vcheck').forEach((c) => (c.checked = e.target.checked));
};
$('#btn-extract').onclick = async () => {
  const ids = $$('.vcheck:checked').map((c) => +c.dataset.id);
  if (!ids.length) { alert('请先勾选要抽帧的视频'); return; }
  const strategy = $('#extract-strategy').value;
  const params = {};
  if (strategy === 'uniform_every_n_seconds') {
    params.interval_seconds = +$('#p-interval-sec').value || 5;
  } else if (strategy === 'existing_model_hits' || strategy === 'dense_around_candidate') {
    params.window_seconds = +$('#p-window').value || 5;
    params.fps = +$('#p-fps').value || 4;
  } else if (strategy === 'uniform_random') {
    params.count = +$('#p-count').value || 200;
  } else if (strategy === 'dense_interval') {
    const [a, b] = $('#p-interval').value.split(',').map((x) => +x.trim());
    if (!a && !b) { alert('请填起止时间(ms),如 600000,660000'); return; }
    params.start_ms = a; params.end_ms = b; params.fps = +$('#p-fps').value || 4;
  } else if (strategy === 'manual_timestamps') {
    params.timestamps = $('#p-timestamps').value.split(',').map((x) => +x.trim())
      .filter((x) => x > 0);
    if (!params.timestamps.length) { alert('请填时间点(ms),逗号分隔'); return; }
  }
  try {
    await api('/api/extract', {
      method: 'POST',
      body: JSON.stringify({ video_ids: ids, strategy, params }),
    });
  } catch (e) {
    alert('无法启动抽帧: ' + e.message);
    return;
  }
  $('#btn-cancel').disabled = false;
  pollExtract();
};
$('#btn-cancel').onclick = async () => { await api('/api/extract/cancel', { method: 'POST' }); };
$('#btn-auto').onclick = async () => {
  try {
    const pick = await api('/api/videos/auto-pick?per_streamer=5&min_size_bytes=1073741824');
    if (!pick.video_ids.length) { alert('没有符合条件的视频(>1GB)'); return; }
    const estGb = (pick.videos * 2.5).toFixed(0);
    const ok = confirm(
      `将自动抽帧 ${pick.videos} 个视频(覆盖 ${pick.streamers} 个主播,每主播 5 个),\n` +
      `每 5 秒一帧,预计约 ${pick.estimated_frames} 帧,本地占用约 ${estGb}GB,\n` +
      `并行 3 路,预计耗时 40~90 分钟(取决于视频时长)。\n\n确定开始?`);
    if (!ok) return;
    await api('/api/extract', {
      method: 'POST',
      body: JSON.stringify({
        video_ids: pick.video_ids,
        strategy: 'uniform_every_n_seconds',
        params: { interval_seconds: 5 },
      }),
    });
  } catch (e) {
    alert('无法启动: ' + e.message);
    return;
  }
  $('#btn-cancel').disabled = false;
  pollExtract();
};
$('#btn-group').onclick = async () => {
  const r = await api('/api/events/auto-group', { method: 'POST', body: '{}' });
  alert(`已建立 ${r.events} 个事件`);
  loadVideos();
};

function pollExtract() {
  const box = $('#extract-progress');
  const timer = setInterval(async () => {
    const st = await api('/api/extract/state');
    if (!st.running && st.summary) {
      clearInterval(timer);
      $('#btn-cancel').disabled = true;
      const s = st.summary;
      const failed = s.failed || [];
      box.classList.remove('hidden');
      if (s.cancelled) {
        box.innerHTML = '<div>任务已取消</div>';
      } else if (failed.length) {
        box.innerHTML = `<div style="color:#ffa198">完成:${s.videos - failed.length}/${s.videos} 个视频成功,新增 ${s.added} 帧;失败 ${failed.length} 个:</div>` +
          failed.map((f) => `<div style="font-size:11px">  video#${f.video_id}: ${esc(f.error)}</div>`).join('');
      } else {
        box.innerHTML = `<div style="color:#7ee787">抽帧完成:${s.videos} 个视频,新增 ${s.added} 帧</div>`;
      }
      setTimeout(() => box.classList.add('hidden'), 8000);
      loadVideos();
      return;
    }
    if (!st.running) return; // 等待 summary(任务刚结束瞬间)
    box.classList.remove('hidden');
    // 显示并行中的多个视频进度(前 4 个)
    const items = Object.values(st.progress).slice(0, 4)
      .map((p) => {
        const scan = p.scan ? `(扫描 ${p.scan})` : '';
        const err = p.error ? ` <span style="color:#ffa198">${esc(p.error)}</span>` : '';
        return `<div>${esc(p.streamer || '')}/${esc(p.filename || '')}: 已入库 ${p.current} 帧 ${scan} ${p.status}${err}</div>`;
      })
      .join('');
    box.innerHTML =
      `<div>并行抽帧中(${Object.keys(st.progress).length} 路活跃)…</div>${items}`;
  }, 1000);
}

// ---------- 标注 ----------
// 主播级默认框:同一主播跨视频记忆(不换设备坐标基本固定)
// viewport 任何帧都带出;result_panel/scoreboard_panel/shop_panel 按界面类型带出
let streamerBoxes = null;  // {streamer, boxes: {type: {x,y,w,h}}}

async function applyStreamerBoxes() {
  if (!cur) return;
  if (!streamerBoxes || streamerBoxes.streamer !== cur.streamer) {
    const r = await api(`/api/videos/${cur.video_id}/streamer-boxes`)
      .catch(() => ({ streamer: cur.streamer, boxes: {} }));
    streamerBoxes = { streamer: r.streamer, boxes: r.boxes || {} };
  }
  if (!streamerBoxes.boxes) return;
  const a = cur.annotation || {};
  const boxes = cur.boxes || (cur.boxes = {});
  // 各类型带出条件
  const need = {
    viewport: a.content_family === 'vainglory',  // 游戏窗口只属于虚荣画面,非虚荣不带出
    result_panel: a.screen_type === 'result_page',
    scoreboard_panel: ['scoreboard', 'death_scoreboard'].includes(a.screen_type),
    shop_panel: a.screen_type === 'ingame_shop',
    equipment_panel: a.screen_type === 'equipment_select',
    talent_panel: a.screen_type === 'talent_select',
  };
  Object.entries(need).forEach(([type, cond]) => {
    if (!cond || boxes[type] || !streamerBoxes.boxes[type]) return;
    boxes[type] = { ...streamerBoxes.boxes[type] };
    // 自动保存到本帧(用户可拖动修改)
    api(`/api/frames/${cur.id}/box`, {
      method: 'PUT',
      body: JSON.stringify({ box_type: type, ...boxes[type] }),
    }).catch(() => {});
  });
  renderBoxes();
  updateCompleteButton();  // 带出框后刷新「完成并下一张」状态,消除"缺框"误报
  // 带出的历史框闪烁提示,让用户一眼看到"已自动加载"
  Object.keys(need).forEach((type) => {
    if (need[type] && boxes[type] && streamerBoxes.boxes[type]) {
      const el = $('#box-' + type);
      if (el) {
        el.classList.remove('flash');
        void el.offsetWidth;
        el.classList.add('flash');
      }
    }
  });
}

// 检查器高度顶满:flex 布局自动拉伸(#insp-scroll 滚动,footer 吸底),无需 JS 同步

async function loadQueue() {
  const sel = $('#queue-select').value;
  const q = new URLSearchParams({ limit: '500' });
  if (sel === 'unlabeled') q.set('labeled', '0');
  else if (sel === 'draft') q.set('status', 'draft');
  else if (sel === 'needs_review') q.set('status', 'needs_review');
  else if (sel === 'result_pos') q.set('screen_type', 'result_page');
  else if (sel === 'scoreboard') q.set('screen_type', 'scoreboard');
  else if (sel === 'event') {
    if (!cur || !cur.event_id) { queue = []; qIdx = 0; showFrame(null); return; }
    q.set('event_id', String(cur.event_id));
  }
  const data = await api('/api/frames?' + q);
  queue = data.frames;
  qIdx = 0;
  if (queue.length) showFrame(queue[0]);
  else showFrame(null);
  $('#lbl-progress').textContent = queue.length ? `1/${queue.length}` : '0/0';
}
$('#queue-select').onchange = loadQueue;

async function showFrame(f) {
  cur = f ? await api(`/api/frames/${f.id}`) : null;
  if (!cur) {
    $('#frame-img').src = '';
    $('#lbl-video').textContent = '队列为空,请先抽帧或切换队列';
    resetInspector();
    renderTimeline([]);
    return;
  }
  $('#frame-img').src = `/api/frames/${cur.id}/thumb?t=${cur.id}`;
  $('#frame-img').dataset.frameId = cur.id;
  $('#frame-img').dataset.original = '';
  $('#lbl-video').textContent = `${cur.streamer} / ${cur.filename}`;
  $('#lbl-time').textContent =
    `${(cur.timestamp_ms / 1000).toFixed(1)}s` +
    (cur.part_index ? ` (part ${cur.part_index})` : '');
  $('#lbl-progress').textContent = `${qIdx + 1}/${queue.length}`;
  const hint = cur.predictions && cur.predictions.length
    ? `旧模型预判:${cur.predictions[0].pred_type} conf=${cur.predictions[0].confidence}`
    : '';
  $('#model-hint').textContent = hint;
  $('#model-hint').classList.toggle('hidden', !hint);
  if (isGateTask() && state.gateRound) {
    const gate = await api(
      `/api/mode-gate/rounds/${state.gateRound.id}/frames/${cur.id}`);
    cur.gate_annotation = gate.annotation;
    cur.gate_expected_mode = gate.expected_mode;
    if (gate.annotation && gate.annotation.evidence === 'no_evidence') {
      gateEvidence = null;
      drawMode = null;
    } else {
      gateEvidence = gate.annotation
        ? gate.annotation.evidence
        : gate.expected_mode === '3v3' ? 'open_entrance' : 'blocked_gate';
      drawMode = 'mode_gate';
    }
    renderGateInspector();
    renderBoxes();
    clearModelBoxes();
    return;
  }
  renderInspector();
  renderBoxes();
  renderTimeline();
  clearModelBoxes();  // 切帧时清除上次的模型测试框
  if (!testMode) applyStreamerBoxes();  // 主播级默认框自动带出(测试模式不写框)
  $('#lbl-event-info').textContent = cur.event_id
    ? `事件 #${cur.event_id}` : '不属于事件(可在数据源视图自动分组)';
}
$('#btn-prev').onclick = () => stepFrame(-1);
$('#btn-next').onclick = () => stepFrame(1);
// 禁止浏览器拖动图片本身；画框和移动标注框由 pointer 事件处理。
$('#frame-img').addEventListener('dragstart', (event) => event.preventDefault());
// 单击图片:缩略图 ↔ 原图(同一帧只加载一次原图)
$('#frame-img').addEventListener('click', async () => {
  const img = $('#frame-img');
  if (!img.dataset.frameId) return;
  const fid = img.dataset.frameId;
  if (img.dataset.original) {
    img.src = `/api/frames/${fid}/thumb?t=${fid}`;
    img.dataset.original = '';
  } else {
    img.src = `/api/frames/${fid}/image?t=${fid}`;
    img.dataset.original = '1';
  }
});

// ---------- 实时打标(视频列表 → 下载本地 → 播放器 + 本地取帧) ----------
let liveHistory = [];      // 本视频已抽帧(按 pts 升序):[{id, pts_ms, sha256}]
let liveVideoId = 0;
let liveDurationMs = 0;
let liveVideoList = [];    // 列表视频 [{id, streamer, filename, ...}]

function liveIntervalMs() {
  return Math.round((+$('#live-interval').value || 5) * 1000);
}

// ---- 视频列表 ----
async function loadLiveList() {
  let data;
  try {
    data = await api(isGateTask()
      ? '/api/mode-gate/rounds/active'
      : '/api/live/videos');
  } catch (err) {
    liveVideoList = [];
    $('#live-table tbody').innerHTML = '';
    $('#live-list-hint').textContent = '加载失败: ' + err.message;
    return;
  }
  if (isGateTask()) {
    state.gateRound = data;
    liveVideoList = data.videos.map((v) => ({ ...v, id: v.video_id }));
    renderGateLiveList();
    return;
  }
  state.gateRound = null;
  liveVideoList = data.videos;
  renderStandardLiveList();
}

function renderStandardLiveList() {
  $('#gate-round-intro').classList.add('hidden');
  $('#live-list-hint').textContent =
    '全部 >1GB 视频,自己选要打标的文件:点「进入打标」后先下载到本地;本地列标 ✓ 表示已下载。';
  $('#live-table-head').innerHTML = `<tr>
    <th>主播</th><th>文件名</th><th>大小</th><th>时长</th>
    <th>本地</th><th>打标进度</th><th>已标/总帧</th><th>上次位置</th><th></th>
  </tr>`;
  const tbody = $('#live-table tbody');
  tbody.innerHTML = '';
  liveVideoList.forEach((v) => {
    const mins = v.duration_seconds ? Math.round(v.duration_seconds / 60) + 'min' : '—';
    const gb = (v.size_bytes / 1073741824).toFixed(1) + 'GB';
    const pos = v.last_pts_ms != null
      ? (v.last_pts_ms / 1000).toFixed(0) + 's' : '—';
    const localCell = v.local_ready
      ? '<span class="local-ready" title="已下载到本地,秒开">✓</span>'
      : '<span class="muted small" title="未下载,进入时需下载">—</span>';
    let progCell;
    if (v.progress_pct != null) {
      progCell = `<div class="mini-bar"><div style="width:${Math.min(100, v.progress_pct)}%"></div></div>
                  <span class="small muted">${v.progress_pct}%</span>`;
    } else {
      progCell = '<span class="muted small">—</span>';
    }
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td>${esc(v.streamer)}</td>
        <td>${esc(v.filename)}</td>
        <td>${gb}</td><td>${mins}</td>
        <td>${localCell}</td>
        <td>${progCell}</td>
        <td>${v.labeled_count}/${v.frame_count}</td>
        <td>${pos}</td>
        <td><button class="primary live-enter" data-id="${v.id}">进入打标</button>
            <button class="opt live-test" data-id="${v.id}" title="对任意帧跑模型推理,看输出">模型测试</button></td>
      </tr>`);
  });
  tbody.querySelectorAll('.live-enter').forEach((btn) => {
    btn.onclick = () => enterLiveVideo(+btn.dataset.id);
  });
  tbody.querySelectorAll('.live-test').forEach((btn) => {
    btn.onclick = () => enterTestVideo(+btn.dataset.id);
  });
}

function renderGateLiveList() {
  const round = state.gateRound;
  const intro = $('#gate-round-intro');
  intro.classList.remove('hidden');
  intro.innerHTML = `
    <h3>${esc(round.name)}</h3>
    <p>${esc(round.description)}</p>
    <p class="small muted">我已挑好 ${round.videos.length} 个视频：大乱斗看黄色光栅，3V3 看同一入口没有光栅的样子。专项已标 ${round.annotation_count} 张。</p>`;
  $('#live-list-hint').textContent =
    '只显示本轮挑选的视频。它们都已在本机，点“进入圈光栅”即可；每个视频会从建议位置或上次位置继续。';
  $('#live-table-head').innerHTML = `<tr>
    <th>模式</th><th>主播</th><th>画面</th><th>文件名</th><th>本地</th>
    <th>专项已标</th><th>上次位置</th><th>挑选理由</th><th></th>
  </tr>`;
  const tbody = $('#live-table tbody');
  tbody.innerHTML = '';
  liveVideoList.forEach((v) => {
    const mode = v.expected_mode === 'aram'
      ? '<span class="mode-chip aram">大乱斗</span>'
      : '<span class="mode-chip three">3V3 对照</span>';
    const posMs = v.last_pts_ms != null ? v.last_pts_ms : v.start_ms;
    const pos = `${(posMs / 1000).toFixed(0)}s` +
      (v.last_pts_ms != null ? '（继续）' : '（建议起点）');
    const localCell = v.local_ready
      ? '<span class="local-ready">✓ 已就绪</span>'
      : '<span class="muted">需下载</span>';
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td>${mode}</td><td>${esc(v.streamer)}</td><td>${esc(v.dimensions || '—')}</td>
        <td>${esc(v.filename)}</td><td>${localCell}</td>
        <td>${v.annotation_count}（光栅 ${v.blocked_count} / 开放 ${v.open_count}）</td>
        <td>${pos}</td><td class="small muted">${esc(v.notes)}</td>
        <td><button class="primary live-enter" data-id="${v.id}">进入圈光栅</button></td>
      </tr>`);
  });
  tbody.querySelectorAll('.live-enter').forEach((btn) => {
    btn.onclick = () => enterLiveVideo(+btn.dataset.id);
  });
}
$('#btn-live-refresh').onclick = loadLiveList;

// ---- 进入视频:检查/发起下载 → 播放器 ----
let downloadTimer = null;

async function ensureLocalVideo(videoId) {
  let st = await api(`/api/live/videos/${videoId}/download-state`);
  if (st.status === 'done') return true;
  $('#download-box').classList.remove('hidden');
  $('#download-bar').style.width = '0%';
  if (st.status !== 'downloading' && st.status !== 'converting') {
    await api(`/api/live/videos/${videoId}/download`, { method: 'POST' });
  }
  return new Promise((resolve) => {
    clearInterval(downloadTimer);
    downloadTimer = setInterval(async () => {
      st = await api(`/api/live/videos/${videoId}/download-state`);
      $('#download-bar').style.width = (st.progress || 0) + '%';
      if (st.status === 'done') {
        clearInterval(downloadTimer);
        $('#download-box').classList.add('hidden');
        resolve(true);
      } else if (st.status === 'failed') {
        clearInterval(downloadTimer);
        $('#download-box').classList.add('hidden');
        alert('下载失败: ' + (st.error || '未知错误'));
        resolve(false);
      }
    }, 1000);
  });
}

async function enterLiveVideo(videoId) {
  liveVideoId = videoId;
  const v = liveVideoList.find((x) => x.id === videoId);
  const modeLabel = v && isGateTask()
    ? (v.expected_mode === 'aram' ? '大乱斗' : '3V3 对照') + ' · '
    : '';
  $('#live-video-name').textContent = v
    ? `${modeLabel}${v.streamer} / ${v.filename}` : `视频#${videoId}`;
  $('#live-list-box').classList.add('hidden');
  $('#live-work-box').classList.remove('hidden');
  liveMode = true;
  liveHistory = [];
  streamerBoxes = null;  // 主播默认框按视频重置
  livePlayStop();
  $('#btn-play').classList.add('hidden');  // 实时模式只用上方帧播放按钮
  // 1) 下载到本地
  const ok = await ensureLocalVideo(videoId);
  if (!ok) { exitLive(); return; }
  $('#download-box').classList.add('hidden');
  // 2) 恢复进度或从头
  let resumeFrom = null;
  if (isGateTask() && v) {
    resumeFrom = v.last_pts_ms != null ? v.last_pts_ms : v.start_ms;
  } else if (v && v.last_pts_ms != null) {
    const cont = confirm(
      `该视频上次打到 ${(v.last_pts_ms / 1000).toFixed(0)}s(已标 ${v.labeled_count} 帧)。\n` +
      `确定=继续上次位置,取消=从头开始`);
    if (cont) {
      resumeFrom = v.last_pts_ms;
    } else {
      await api(`/api/live/videos/${videoId}/progress`, {
        method: 'PUT', body: JSON.stringify({ last_pts_ms: null, last_frame_id: null }),
      });
    }
  }
  const startPts = resumeFrom != null ? resumeFrom : 0;
  // 3) 取起点附近一帧显示
  loadLiveMarked();
  // 用主播默认框补齐已标帧缺失的框(之前保存失败的商店/积分板框等)
  if (!isGateTask()) {
    api(`/api/videos/${videoId}/backfill-boxes`, { method: 'POST', body: '{}' })
      .catch(() => {});
  }
  const r = await localTakeFrame(startPts);
  // 兜底:起点取帧失败/超界时,从视频开头再试一次,保证不黑屏
  if (!r && resumeFrom != null) {
    $('#live-info').textContent = '上次位置不可用,已从头开始';
    await localTakeFrame(0);
  }
}

// ---- 模型测试模式(复用取帧/画布,右侧为模型选择+结果) ----
let testMode = false;
let testModels = [];

async function enterTestVideo(videoId) {
  liveVideoId = videoId;
  const v = liveVideoList.find((x) => x.id === videoId);
  $('#live-video-name').textContent = v ? `${v.streamer} / ${v.filename}` : `视频#${videoId}`;
  $('#live-list-box').classList.add('hidden');
  $('#live-work-box').classList.remove('hidden');
  liveMode = true;
  testMode = true;
  liveHistory = [];
  streamerBoxes = null;
  livePlayStop();
  $('#btn-play').classList.add('hidden');
  $('#inspector').classList.add('hidden');   // 测试模式不显示标注表单
  $('#test-panel').classList.remove('hidden');
  $('#test-result').innerHTML = '<p class="muted small">找到帧后,选模型点「运行测试」</p>';
  const ok = await ensureLocalVideo(videoId);
  if (!ok) { exitLive(); return; }
  $('#download-box').classList.add('hidden');
  await loadTestModels();
  await localTakeFrame(0);
  $('#live-info').textContent = '模型测试模式:跳转/播放找到目标帧 → 右侧运行模型';
}

async function loadTestModels() {
  testModels = await api('/api/models');
  const sel = $('#test-model');
  sel.innerHTML = '';
  if (!testModels.length) {
    sel.innerHTML = '<option value="">(暂无模型,先训练)</option>';
    return;
  }
  testModels.forEach((m) => {
    const kind = m.task === 'detect' ? '检测' : (m.task === 'classify' ? '分类' : '未知');
    const cls = m.task === 'classify' && m.classes.length
      ? `(${m.classes.join('/')})` : '';
    sel.insertAdjacentHTML('beforeend',
      `<option value="${m.name}">${m.name} · ${kind} ${cls}</option>`);
  });
}

// 模型测试框:在画布上叠加显示检测结果(青色粗框,与标注框区分)
function clearModelBoxes() {
  $$('#canvas-wrap .bbox-model').forEach((el) => el.remove());
}
function drawModelBoxes(r) {
  clearModelBoxes();
  if (!r || r.task !== 'detect') return;
  const wrap = $('#canvas-wrap');
  r.detections.forEach((d, i) => {
    const [x, y, w, h] = d.xywh_norm;
    const el = document.createElement('div');
    el.className = 'bbox-model';
    el.style.left = (x * 100) + '%';
    el.style.top = (y * 100) + '%';
    el.style.width = (w * 100) + '%';
    el.style.height = (h * 100) + '%';
    el.innerHTML = `<span class="bbox-model-tag">${d.label} ${(d.conf * 100).toFixed(1)}%</span>`;
    wrap.appendChild(el);
  });
}

// 格式化结果展示
function renderTestResult(r) {
  const box = document.createElement('div');
  box.className = 'model-result';
  if (r.task === 'detect') {
    if (r.detections.length === 0) {
      box.innerHTML = `<div class="result-card none"><b>未检测到结算面板</b>
        <span class="muted small">最高置信度 ${r.raw_top_conf}</span></div>`;
    } else {
      r.detections.forEach((d) => {
        box.insertAdjacentHTML('beforeend', `
          <div class="result-card hit">
            <b>${d.label}</b> <span class="tag">${(d.conf * 100).toFixed(1)}%</span>
            <div class="small muted">像素坐标 x1y1x2y2: [${d.xyxy_px.join(', ')}]</div>
            <div class="small muted">归一化 xywh: [${d.xywh_norm.join(', ')}]</div>
          </div>`);
      });
    }
  } else if (r.task === 'classify') {
    const t = r.top1;
    box.innerHTML = `<div class="result-card hit">
        <b>${t.label}</b> <span class="tag">${(t.prob * 100).toFixed(1)}%</span>
        <div class="small muted">class=${t.class}</div></div>`;
    r.top5.slice(1).forEach((c) => {
      box.insertAdjacentHTML('beforeend', `
        <div class="result-card">
          ${c.label} <span class="muted small">${(c.prob * 100).toFixed(1)}%</span>
          <span class="small muted">(${c.class})</span></div>`);
    });
  } else if (r.task === 'multi') {
    // 多输出头:content / stage / mode 三行结果
    const HEAD_TITLES = { content: '是否虚荣', stage: '阶段', mode: '模式' };
    for (const head of ['content', 'stage', 'mode']) {
      const h = r[head];
      if (!h) continue;
      const t = h.top1;
      const headBox = document.createElement('div');
      headBox.className = 'result-card hit';
      headBox.innerHTML = `
        <div class="small muted" style="margin-bottom:2px">${HEAD_TITLES[head] || head}</div>
        <b>${t.label}</b> <span class="tag">${(t.prob * 100).toFixed(1)}%</span>
        <div class="small muted">class=${t.class}</div>`;
      if (head === 'mode' && h.ambiguous) {
        headBox.insertAdjacentHTML('beforeend',
          `<div class="small" style="color:#ffb84d">⚠ ${esc(h.note || '可能是 3v3 或大乱斗,待确认')}</div>`);
      }
      h.top5.slice(1).forEach((c) => {
        headBox.insertAdjacentHTML('beforeend', `
          <div class="small">${c.label}
            <span class="muted">${(c.prob * 100).toFixed(1)}%</span>
            <span class="small muted">(${c.class})</span></div>`);
      });
      box.appendChild(headBox);
    }
  } else {
    box.innerHTML = '<p class="muted">未知任务类型</p>';
  }
  // 原始输出(可折叠)
  const raw = document.createElement('details');
  raw.className = 'raw-dump';
  raw.innerHTML = `<summary>原始输出 (JSON)</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre>`;
  box.appendChild(raw);
  $('#test-result').innerHTML = '';
  $('#test-result').appendChild(box);
  drawModelBoxes(r);  // 检测框叠加到画布(青色粗框)
}

$('#btn-run-test').onclick = async () => {
  if (!cur) { $('#test-result').innerHTML = '<p class="muted small">请先取一帧</p>'; return; }
  const name = $('#test-model').value;
  if (!name) { $('#test-result').innerHTML = '<p class="muted small">请先选择模型</p>'; return; }
  $('#test-result').innerHTML = '<p class="muted small">推理中…</p>';
  try {
    const r = await api(`/api/models/${name}/test`, {
      method: 'POST',
      body: JSON.stringify({ frame_id: cur.id, conf_thr: +$('#test-conf').value || 0.25 }),
    });
    renderTestResult(r);
  } catch (e) {
    $('#test-result').innerHTML = `<p class="err">测试失败: ${esc(e.message)}</p>`;
  }
};

function exitLive(reload = true) {
  liveMode = false;
  testMode = false;
  liveVideoId = 0;
  liveHistory = [];
  livePlayStop();
  $('#btn-play').classList.remove('hidden');
  clearInterval(downloadTimer);
  $('#download-box').classList.add('hidden');
  $('#live-list-box').classList.remove('hidden');
  $('#live-work-box').classList.add('hidden');
  $('#inspector').classList.remove('hidden');
  $('#test-panel').classList.add('hidden');
  $('#queue-select').value = 'unlabeled';
  if (reload) loadLiveList();
}
$('#btn-live-back').onclick = exitLive;

// ---- 本地取帧(秒回) ----
async function localTakeFrame(ptsMs) {
  if (!liveVideoId) return null;
  // 快照本次预选:即使上次取帧未完成(连点),预选也不会被别的调用消费掉
  const willPrefill = prefillNext;
  const prefill = prefillData;
  prefillNext = false;
  const r = await api('/api/live/frame-local', {
    method: 'POST',
    body: JSON.stringify({ video_id: liveVideoId, pts_ms: ptsMs,
                           interval_ms: liveIntervalMs() }),
  }).catch((e) => { $('#live-info').textContent = '取帧失败: ' + e.message; return null; });
  if (!r) return null;
  if (r.done) {
    $('#live-info').textContent = '已到视频末尾';
    return null;
  }
  liveDurationMs = r.duration_ms || liveDurationMs;
  await showFrame({ id: r.frame_id });
  // 预选:完成并下一张/时间跳转/播放/跳过带入上一帧的选项(用户可改)
  if (!isGateTask() && willPrefill && prefill) {
    cur.annotation = JSON.parse(prefill);
    renderInspector();
    renderBoxes();
    applyStreamerBoxes();  // 按预选类型带出主播历史框(积分板/商店/结算),不用重画
  }
  if (!isGateTask()) {
    lastSnapshot = snapshotOf();  // 当前帧的基准快照(未修改直接完成会二次确认)
  }
  liveHistory = liveHistory.filter((h) => h.id !== r.frame_id);
  liveHistory.push({ id: r.frame_id, pts_ms: r.pts_ms });
  liveHistory.sort((a, b) => a.pts_ms - b.pts_ms);
  const sec = (r.pts_ms / 1000).toFixed(1);
  const total = r.duration_ms ? '/' + (r.duration_ms / 1000).toFixed(0) + 's' : '';
  $('#live-pos').textContent = `帧位置 ${sec}s${total}`;
  $('#live-info').textContent = isGateTask()
    ? '找到地图入口后，在右侧选“有光栅”或“开放入口”，再拖框。'
    : '当前帧就是打标帧:右侧标注后点「完成并下一张」';
  // 注意:进度只跟「已标帧」走(saveThenNext 里写),浏览/跳转不写进度,
  // 避免最后浏览位置覆盖打标位置(否则重进时恢复点会乱跳)
  return r;
}

// ---- 帧动画播放(每秒取一帧,前进一个间隔) ----
let livePlayTimer = null;
function livePlayToggle() {
  if (livePlayTimer) { livePlayStop(); return; }
  if (!cur) return;
  const btn = $('#btn-live-play');
  btn.textContent = '⏸ 暂停';
  btn.classList.add('selected');
  livePlayTimer = setInterval(async () => {
    if (!cur) { livePlayStop(); return; }
    // 播放前进时也预选当前选项,暂停时表单保持该画面的标注
    if (!isGateTask()) {
      prefillData = JSON.stringify(cur.annotation || {});
      prefillNext = true;
    }
    const r = await localTakeFrame(cur.timestamp_ms + liveIntervalMs());
    if (!r || r.done) livePlayStop();
  }, 1000);
}
function livePlayStop() {
  clearInterval(livePlayTimer);
  livePlayTimer = null;
  const btn = $('#btn-live-play');
  if (btn) { btn.textContent = '▶ 播放(每秒一帧)'; btn.classList.remove('selected'); }
}
$('#btn-live-play').onclick = livePlayToggle;

// ---- 时间跳转按钮(相对当前帧 ±,取帧后预选当前选项,微调清晰度时表单不丢) ----
function jumpToFrame(ptsMs) {
  if (!isGateTask()) {
    prefillData = JSON.stringify(cur.annotation || {});
    prefillNext = true;
  }
  localTakeFrame(Math.max(0, ptsMs));
}
$('#local-video-box').addEventListener('click', (e) => {
  const b = e.target.closest('.jump-btn');
  if (!b || !cur) return;
  const off = +b.dataset.ms;
  jumpToFrame(cur.timestamp_ms + off);
});

// ---- 上一/下一已标帧(在 complete 帧之间导航) ----
let liveMarkedFrames = [];
async function loadLiveMarked() {
  if (!liveVideoId) return;
  const data = await api(isGateTask()
    ? `/api/mode-gate/rounds/${state.gateRound.id}/videos/${liveVideoId}/frames`
    : `/api/frames?video_id=${liveVideoId}&status=complete&limit=100000`);
  liveMarkedFrames = data.frames
    .filter((f) => f.timestamp_ms != null)
    .sort((a, b) => a.timestamp_ms - b.timestamp_ms);
}
async function navMarked(dir) {
  if (!cur) return;
  if (!liveMarkedFrames.length) {
    $('#live-info').textContent = '还没有已标记(完成)的帧';
    return;
  }
  const idx = liveMarkedFrames.findIndex((f) => f.id === cur.id);
  let target = null;
  if (dir < 0) {
    if (idx > 0) {
      target = liveMarkedFrames[idx - 1];
    } else if (idx < 0) {
      // 当前帧未标:找时间上最近的已标帧;都没有则取最后一个
      const before = liveMarkedFrames.filter((f) => f.timestamp_ms < cur.timestamp_ms);
      target = before.length ? before[before.length - 1]
        : liveMarkedFrames[liveMarkedFrames.length - 1];
    }
  } else {
    if (idx >= 0 && idx + 1 < liveMarkedFrames.length) {
      target = liveMarkedFrames[idx + 1];
    } else if (idx < 0) {
      // 当前帧未标:找时间上之后最近的已标帧
      const after = liveMarkedFrames.filter((f) => f.timestamp_ms > cur.timestamp_ms);
      target = after.length ? after[0] : null;
    }
  }
  if (!target) { $('#live-info').textContent = '已是第一/最后一帧'; return; }
  await showFrame(target);
  lastSnapshot = snapshotOf();
  const idx2 = liveMarkedFrames.findIndex((f) => f.id === target.id);
  $('#live-pos').textContent = `帧位置 ${(target.timestamp_ms / 1000).toFixed(1)}s(已标帧 ${idx2 + 1}/${liveMarkedFrames.length})`;
}
$('#btn-live-prev').onclick = () => navMarked(-1);
$('#btn-live-next').onclick = () => navMarked(1);
$('#btn-live-next-video').onclick = async () => {
  if (!liveVideoList.length) await loadLiveList();
  const idx = liveVideoList.findIndex((v) => v.id === liveVideoId);
  const next = liveVideoList[(idx + 1) % liveVideoList.length];
  if (next) await enterLiveVideo(next.id);
};

function stepFrame(d) {
  const n = qIdx + d;
  if (n >= 0 && n < queue.length) { qIdx = n; showFrame(queue[n]); }
}

// ================= 3V3 / 大乱斗光栅专项 =================

let gateEvidence = null;

function gateBoxesOf(annotation = cur && cur.gate_annotation) {
  if (!annotation || annotation.evidence === 'no_evidence') return [];
  if (Array.isArray(annotation.boxes)) return annotation.boxes;
  return annotation.x == null ? [] : [{
    x: annotation.x, y: annotation.y, w: annotation.w, h: annotation.h,
  }];
}

function gateBoxPayload(box) {
  return {
    x: round(box.x), y: round(box.y),
    w: round(box.w), h: round(box.h),
  };
}

function renderGateInspector() {
  if (!cur || !state.gateRound) return;
  const annotation = cur.gate_annotation;
  const boxCount = gateBoxesOf(annotation).length;
  const video = liveVideoList.find((item) => item.id === cur.video_id);
  if (annotation) {
    gateEvidence = annotation.evidence === 'no_evidence'
      ? null : annotation.evidence;
  }
  $('#gate-round-name').textContent = state.gateRound.name;
  const expected = (video || {}).expected_mode || cur.gate_expected_mode;
  $('#gate-video-mode').textContent = expected === 'aram'
    ? '这段按已有标注归入大乱斗候选；仍以当前画面为准，看到黄色光栅才圈。'
    : '这段按已有标注归入 3V3 候选；圈住光栅本来会横着挡住的入口位置。';
  $('#btn-gate-blocked').classList.toggle(
    'selected', gateEvidence === 'blocked_gate');
  $('#btn-gate-open').classList.toggle(
    'selected', gateEvidence === 'open_entrance');
  const status = $('#gate-save-state');
  if (!annotation) {
    status.textContent = '未标，可直接画框';
    status.className = 'status-badge muted';
  } else if (annotation.evidence === 'no_evidence') {
    status.textContent = '无证据，不进训练';
    status.className = 'status-badge review';
  } else {
    status.textContent = annotation.evidence === 'blocked_gate'
      ? `已保存 ${boxCount} 个光栅框`
      : `已保存 ${boxCount} 个开放入口框`;
    status.className = 'status-badge ok';
  }
  $('#btn-gate-next').disabled = !annotation;
  if (drawMode === 'mode_gate') {
    $('#gate-draw-hint').textContent =
      `正在连续画框，已有 ${boxCount} 个；全部圈完按 Esc，或直接点“下一帧”。`;
  } else if (gateEvidence) {
    $('#gate-draw-hint').textContent =
      `已有 ${boxCount} 个框；再点一次当前选项可继续画，已有框可拖动，× 可单独删除。`;
  } else {
    $('#gate-draw-hint').textContent =
      '选“有黄色光栅”或“开放入口”后，再到左边画面拖框。';
  }
}

function chooseGateEvidence(evidence) {
  if (!cur) return;
  const stopDrawing = gateEvidence === evidence && drawMode === 'mode_gate';
  gateEvidence = evidence;
  drawMode = stopDrawing ? null : 'mode_gate';
  $('#btn-gate-blocked').classList.toggle(
    'selected', evidence === 'blocked_gate');
  $('#btn-gate-open').classList.toggle(
    'selected', evidence === 'open_entrance');
  $('#gate-draw-hint').textContent = stopDrawing
    ? '已停止画框；可以拖动已有框，或点 × 删除单个框。'
    : evidence === 'blocked_gate'
      ? '请连续圈出每一处清楚可见的黄色光栅；圈完按 Esc。'
      : '请圈住光栅本来会横着挡住的每一处开放入口；圈完按 Esc。';
  updateDrawButtons();
}

async function saveGateAnnotation(body) {
  if (!cur || !state.gateRound) return null;
  try {
    const annotation = await api(
      `/api/mode-gate/rounds/${state.gateRound.id}/frames/${cur.id}`,
      { method: 'PUT', body: JSON.stringify(body) });
    cur.gate_annotation = annotation;
    gateEvidence = annotation.evidence === 'no_evidence'
      ? null : annotation.evidence;
    renderGateInspector();
    renderBoxes();
    await loadLiveMarked();
    return annotation;
  } catch (err) {
    $('#gate-save-state').textContent = '保存失败: ' + err.message;
    renderBoxes();
    return null;
  }
}

async function nextGateFrame() {
  if (!cur || !cur.gate_annotation) return;
  livePlayStop();
  await localTakeFrame(cur.timestamp_ms + liveIntervalMs());
}

$('#btn-gate-blocked').onclick = () => chooseGateEvidence('blocked_gate');
$('#btn-gate-open').onclick = () => chooseGateEvidence('open_entrance');
$('#btn-gate-no-evidence').onclick = async () => {
  drawMode = null;
  updateDrawButtons();
  const saved = await saveGateAnnotation({ evidence: 'no_evidence' });
  if (saved) await nextGateFrame();
};
$('#btn-gate-next').onclick = nextGateFrame;
$('#btn-gate-clear').onclick = async () => {
  if (!cur || !state.gateRound || !cur.gate_annotation) return;
  await api(`/api/mode-gate/rounds/${state.gateRound.id}/frames/${cur.id}`,
    { method: 'DELETE' });
  cur.gate_annotation = null;
  gateEvidence = null;
  drawMode = null;
  renderGateInspector();
  renderBoxes();
  updateDrawButtons();
  await loadLiveMarked();
};

// 检查器
// ================= 标注状态机(本地优先,草稿/完成分离) =================

function resetInspector() {
  ['blk-nonvg', 'blk-stage', 'blk-screen', 'blk-extra', 'blk-ocr'].forEach((id) =>
    $(`#${id}`).classList.add('hidden'));
  $('#blk-occluder').classList.add('hidden');
  $('#hard-neg-hint').classList.add('hidden');
  $$('#grp-family .opt').forEach((b) => b.classList.remove('selected'));
  // 注意:不清空按钮组 DOM(增量更新,避免选择后滚动锚定跳动)
  $('#notes').value = '';
  $('#sel-bars').value = 'none';
}

function buildBtns(sel, map, selected, clsFn) {
  const grp = $(sel);
  const selArr = Array.isArray(selected) ? selected : (selected ? [selected] : []);
  const key = JSON.stringify(Object.keys(map || {}));
  if (grp.children.length && grp.dataset.key === key) {
    // 按钮已存在(内容未变):只更新选中态,不重建 DOM
    grp.querySelectorAll('.opt').forEach((b) =>
      b.classList.toggle('selected', selArr.includes(b.dataset.v)));
    return;
  }
  grp.innerHTML = '';
  grp.dataset.key = key;
  Object.entries(map || {}).forEach(([v, l]) => {
    const cls = clsFn ? clsFn(v) : '';
    const on = selArr.includes(v);
    grp.insertAdjacentHTML('beforeend',
      `<button class="opt ${cls}${on ? ' selected' : ''}" data-v="${v}">${l}</button>`);
  });
}

function renderInspector() {
  const a = cur.annotation || {};
  resetInspector();
  // 1. 是否为虚荣画面
  $$('#grp-family .opt').forEach((b) =>
    b.classList.toggle('selected', b.dataset.v === a.content_family));
  if (a.content_family === 'not_vainglory') {
    $('#blk-nonvg').classList.remove('hidden');
    buildBtns('#grp-nonvg', CFG.non_vainglory_types, a.non_vainglory_type);
    $('#notes').value = a.notes || '';
    updateCompleteButton();
    return;
  }
  if (a.content_family !== 'vainglory') {
    $('#notes').value = a.notes || '';
    updateCompleteButton();
    return;
  }
  // 2. 对局阶段
  $('#blk-stage').classList.remove('hidden');
  buildBtns('#grp-stage', CFG.game_stages, a.game_context);
  // 3. 具体界面(按阶段)
  const stage = a.game_context;
  if (stage && CFG.stage_screen_types[stage]) {
    $('#blk-screen').classList.remove('hidden');
    buildBtns('#grp-screen', CFG.stage_screen_types[stage], a.screen_type,
      (v) => (v === 'result_page' ? 'good' : (v.includes('scoreboard') ? 'bad' : '')));
    $('#hard-neg-hint').classList.toggle('hidden',
      !['scoreboard', 'death_scoreboard'].includes(a.screen_type));
  }
  // 4. 辅助属性
  $('#blk-extra').classList.remove('hidden');
  buildBtns('#grp-mode', CFG.game_modes, a.game_mode);
  buildBtns('#grp-matchkind', CFG.match_kinds, a.match_kind);
  buildBtns('#grp-view', CFG.view_contexts, a.view_context);
  buildBtns('#grp-quality', Object.fromEntries(CFG.quality_flags), a.quality_flags || []);
  $('#sel-bars').value = a.black_bars || 'none';
  if (a.screen_type === 'result_page') {
    $('#blk-ocr').classList.remove('hidden');
    buildBtns('#grp-ocr', CFG.ocr_usable, a.ocr_usable || 'yes');
    buildBtns('#grp-clarity', CFG.result_clarity, a.result_clarity || 'clear');
    buildBtns('#grp-occlusion', CFG.result_occlusion, a.result_occlusion || 'none');
    // 有遮挡时才显示遮挡物多选
    const occluded = a.result_occlusion === 'occluded';
    $('#blk-occluder').classList.toggle('hidden', !occluded);
    if (occluded) {
      buildBtns('#grp-occluder', Object.fromEntries(CFG.occluder_types),
                a.occluder_types || []);
    }
  }
  $('#notes').value = a.notes || '';
  updateCompleteButton();
}

// 完整性校验(结算检测任务)
function validate() {
  if (!cur) return [];
  const a = cur.annotation || {};
  if (a.content_family === 'not_vainglory') return [];   // 有效负样本
  if (a.content_family === 'uncertain') return [];        // 待复核
  if (a.content_family !== 'vainglory') return ['是否为虚荣画面'];
  const missing = [];
  if (!a.game_context) missing.push('对局阶段');
  if (!a.screen_type) missing.push('具体界面');
  // 游戏进行中:游戏模式/真人机必须明确选择(无法确定不算完成)
  if (a.game_context === 'in_match') {
    if (!a.game_mode || a.game_mode === 'unknown') missing.push('游戏模式');
    if (!a.match_kind || a.match_kind === 'unknown') missing.push('真人/人机');
  }
  // 结算页:游戏模式必须明确(结算页看得出模式,数据按模式均衡很重要)
  if (a.screen_type === 'result_page' && (!a.game_mode || a.game_mode === 'unknown')) {
    missing.push('游戏模式');
  }
  if (a.screen_type === 'result_page' && !(cur.boxes && cur.boxes.result_panel)) {
    missing.push('结算面板边界框');
  }
  return missing;
}

function updateCompleteButton() {
  const btn = $('#btn-done-next');
  const hint = $('#complete-hint');
  const missing = validate();
  const ok = !missing.length;
  btn.disabled = !ok;
  hint.textContent = ok ? '' : `当前仍缺少: ${missing.join('、')}`;
  hint.classList.toggle('hidden', ok);
}

// 界面类型 → 唯一面板框;清掉不匹配的(积分板/商店/结算框互斥,viewport 保留)
function clearMismatchedBoxes(screenType) {
  if (!cur) return;
  const expected = {
    ingame_shop: 'shop_panel',
    equipment_select: 'equipment_panel',
    talent_select: 'talent_panel',
    scoreboard: 'scoreboard_panel',
    death_scoreboard: 'scoreboard_panel',
    result_page: 'result_panel',
  }[screenType];
  ['shop_panel', 'scoreboard_panel', 'result_panel', 'equipment_panel', 'talent_panel'].forEach((bt) => {
    if (bt !== expected && cur.boxes && cur.boxes[bt]) {
      delete cur.boxes[bt];
      api(`/api/frames/${cur.id}/box?box_type=${bt}`, { method: 'DELETE' })
        .catch(() => {});
    }
  });
}

// 选择字段:更新本地 → 清理不兼容 → 立即渲染 → 校验 → 后台草稿保存
function applyField(grpId, value) {
  if (!cur) return;
  const a = cur.annotation = cur.annotation || {};
  switch (grpId) {
    case 'grp-family':
      a.content_family = value;
      if (value !== 'vainglory') {
        delete a.game_context; delete a.screen_type; delete a.game_mode;
        delete a.ocr_usable; delete a.non_vainglory_type;
        a.match_kind = 'unknown'; a.view_context = 'unknown';
        a.quality_flags = []; a.talent_mode = 0;
      }
      break;
    case 'grp-nonvg':
      a.non_vainglory_type = value;
      break;
    case 'grp-stage':
      a.game_context = value;
      delete a.screen_type;         // 阶段变化清除旧界面
      delete a.ocr_usable;
      break;
    case 'grp-screen':
      a.screen_type = value;
      if (value === 'result_page') {
        // 默认:清晰、无遮挡、可 OCR;只在遇到异常时才特意改
        a.ocr_usable = a.ocr_usable || 'yes';
        a.result_clarity = a.result_clarity || 'clear';
        a.result_occlusion = a.result_occlusion || 'none';
      } else {
        delete a.ocr_usable;
      }
      // 界面类型对应的唯一框;清掉不匹配的面板框(积分板/商店/结算互斥)
      clearMismatchedBoxes(value);
      // 先保存新类型,再带出历史框:后端按已保存的类型校验框,
      // 若先带框(旧类型还在库)会被拒绝,出现"亮一下又消失"
      scheduleSave('draft', { delay: 0, onDone: () => applyStreamerBoxes() });
      break;
    case 'grp-mode': a.game_mode = value; break;
    case 'grp-matchkind': a.match_kind = value; break;
    case 'grp-view': a.view_context = value; break;
    case 'grp-quality':
      a.quality_flags = $$('#grp-quality .opt.selected').map((x) => x.dataset.v);
      break;
    case 'grp-ocr': a.ocr_usable = value; break;
    case 'grp-clarity': a.result_clarity = value; break;
    case 'grp-occlusion':
      a.result_occlusion = value;
      if (value !== 'occluded') {
        a.occluder_types = [];  // 无遮挡时清遮挡物
      }
      break;
    case 'grp-occluder':
      a.occluder_types = $$('#grp-occluder .opt.selected').map((x) => x.dataset.v);
      break;
  }
  renderInspector();
  renderBoxes();
  // 其他分组不需要带框;grp-screen 的带框已在保存成功后回调执行
  // 选择后自动滚动到下一层(等渲染完成后,轻微滚动避免乱跳)
  const nextBlock = {
    'grp-family': '#blk-stage', 'grp-stage': '#blk-screen',
    'grp-screen': '#blk-extra', 'grp-nonvg': null,
  }[grpId];
  if (nextBlock) {
    requestAnimationFrame(() => {
      const el = $(nextBlock);
      if (el && !el.classList.contains('hidden')) {
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    });
  }
  setStatus('draft');
  scheduleSave('draft');
}

// 点击委托:所有按钮组
$('#inspector').addEventListener('click', (e) => {
  const b = e.target.closest('.opt');
  if (!b || !b.dataset.v) return;
  const grp = b.closest('.btn-grid');
  if (!grp) return;
  if (grp.id === 'grp-quality' || grp.id === 'grp-occluder') {
    b.classList.toggle('selected');
  } else {
    grp.querySelectorAll('.opt').forEach((x) => x.classList.remove('selected'));
    b.classList.add('selected');
  }
  applyField(grp.id, b.dataset.v);
});
// 黑边/备注 → 草稿保存
$('#sel-bars').addEventListener('change', () => {
  if (!cur) return;
  cur.annotation.black_bars = $('#sel-bars').value;
  setStatus('draft'); scheduleSave('draft');
});
$('#notes').addEventListener('input', () => {
  if (!cur) return;
  setStatus('draft'); scheduleSave('draft');
});

// 保存(草稿,300ms 防抖;完成后由按钮触发)
// saveSeq:保存版本号——清除标注/完成保存时递增,作废仍在飞行中的旧保存
let saveTimer = null;
let saveSeq = 0;
let pendingSave = null;
function scheduleSave(status, opts = {}) {
  if (!cur) return;
  const mySeq = ++saveSeq;
  $('#save-state').textContent = '正在保存…';
  clearTimeout(saveTimer);
  pendingSave = new Promise((resolve) => {
    saveTimer = setTimeout(async () => {
      try {
        const body = currentAnnotation();
        body.annotation_status = status || 'draft';
        await api(`/api/frames/${cur.id}/annotation`, {
          method: 'PUT', body: JSON.stringify(body),
        });
        if (mySeq !== saveSeq) { resolve(); return; }  // 已被清除/完成保存作废
        cur = await api(`/api/frames/${cur.id}`);
        renderInspector();
        renderBoxes();
        setStatus(cur.annotation ? cur.annotation.annotation_status : 'draft');
        if (opts.onDone) opts.onDone();  // 保存成功后的回调(如带出历史框)
      } catch (err) {
        if (mySeq === saveSeq) {
          $('#save-state').textContent = '保存失败: ' + err.message;
        }
      } finally {
        resolve();
      }
    }, opts.delay !== undefined ? opts.delay : 300);
  });
}

function setStatus(st) {
  const map = { draft: '草稿已保存', complete: '标注完整',
                needs_review: '待复核', ignored: '已忽略' };
  const el = $('#save-state');
  el.textContent = map[st] || '草稿已保存';
  el.className = 'status-badge ' +
    (st === 'complete' ? 'ok' : (st === 'needs_review' ? 'review' : 'muted'));
}

function currentAnnotation() {
  const a = cur.annotation || {};
  const body = {
    content_family: a.content_family,
    match_kind: a.match_kind || 'unknown',
    view_context: a.view_context || 'unknown',
    quality_flags: a.quality_flags || [],
    black_bars: a.black_bars || 'none',
    notes: $('#notes').value.trim(),
    talent_mode: a.talent_mode || 0,  // 天赋开关已从界面移除,保留字段兼容旧数据
  };
  if (a.content_family === 'not_vainglory') {
    body.non_vainglory_type = a.non_vainglory_type || undefined;
  } else if (a.content_family === 'vainglory') {
    body.game_context = a.game_context || undefined;
    body.screen_type = a.screen_type || undefined;
    body.game_mode = a.game_mode || undefined;
    if (a.screen_type === 'result_page') {
      // 默认值:清晰/无遮挡/可 OCR,只有遇到异常才改
      body.ocr_usable = a.ocr_usable || 'yes';
      body.result_clarity = a.result_clarity || 'clear';
      body.result_occlusion = a.result_occlusion || 'none';
      body.occluder_types = a.occluder_types || [];
    }
  }
  return body;
}

// 完成并下一张 / 待复核 / 忽略
let lastSnapshot = null;  // 当前帧展示后/保存后的标注快照(用于未修改二次确认)
let prefillNext = false;  // 完成并下一张后,把本帧选项预选到下一帧
let prefillData = null;   // 预选数据(上一帧标注的 JSON)
let saveBusy = false;     // 防连点:完成并下一张处理中

function snapshotOf() {
  const a = cur ? cur.annotation : {};
  return JSON.stringify({
    content_family: a.content_family, non_vainglory_type: a.non_vainglory_type,
    game_context: a.game_context, screen_type: a.screen_type,
    game_mode: a.game_mode, match_kind: a.match_kind, view_context: a.view_context,
    quality_flags: a.quality_flags, black_bars: a.black_bars,
    ocr_usable: a.ocr_usable, talent_mode: a.talent_mode,
    result_clarity: a.result_clarity, result_occlusion: a.result_occlusion,
    occluder_types: a.occluder_types,
  });
}

async function saveThenNext(status) {
  if (!cur || saveBusy) return;  // 防连点:上一次完成前忽略
  saveBusy = true;
  try {
    // 作废未决/飞行中的草稿保存,等它落地后本次完成保存才写入(避免旧草稿晚到覆盖)
    saveSeq++;
    clearTimeout(saveTimer);
    if (pendingSave) await pendingSave.catch(() => {});
    const body = currentAnnotation();
    body.annotation_status = status;
    await api(`/api/frames/${cur.id}/annotation`, {
      method: 'PUT', body: JSON.stringify(body),
    });
    cur = await api(`/api/frames/${cur.id}`);
    // 进度跟已标帧走:完成一张即记住位置(浏览/跳转不覆盖)
    api(`/api/live/videos/${liveVideoId}/progress`, {
      method: 'PUT',
      body: JSON.stringify({ last_pts_ms: cur.timestamp_ms, last_frame_id: cur.id }),
    }).catch(() => {});
    if (liveMode) {
      // 实时模式:按间隔取下一帧,并把本帧选项预选给下一帧
      prefillData = JSON.stringify(cur.annotation || {});
      prefillNext = true;
      livePlayStop();
      loadLiveMarked();  // 刷新已标帧列表(数量/导航即时更新)
      const r = await localTakeFrame(cur.timestamp_ms + liveIntervalMs());
      if (r && !r.done) {
        lastSnapshot = snapshotOf();  // 预选后的快照(未修改时会弹确认)
        $('#live-info').textContent = '已预选上一帧的选项,如无修改请直接确认「完成并下一张」';
      }
    } else {
      stepFrame(1);
      lastSnapshot = snapshotOf();
    }
  } catch (err) {
    $('#save-state').textContent = '保存失败: ' + err.message;
  } finally {
    saveBusy = false;
  }
}
$('#btn-done-next').onclick = () => {
  const missing = validate();
  if (missing.length) {
    focusFirstMissing(missing[0]);
    return;
  }
  saveThenNext('complete');
};
$('#btn-review').onclick = () => saveThenNext('needs_review');
// 清除标注:回到未标注状态(标错了用这个,不用重新打整帧)
$('#btn-clear-annotation').onclick = async () => {
  if (!cur) return;
  if (!confirm('确定清除本帧的标注吗?(标注和框都会删除,回到未标注状态)')) return;
  try {
    saveSeq++;            // 作废所有飞行中的旧保存
    clearTimeout(saveTimer);
    if (pendingSave) await pendingSave.catch(() => {});  // 等旧保存落地,再删除,顺序保证不被复活
    await api(`/api/frames/${cur.id}/annotation`, { method: 'DELETE' });
    cur = await api(`/api/frames/${cur.id}`);
    renderInspector();
    renderBoxes();
    setStatus('');
    $('#save-state').textContent = '标注已清除';
    loadLiveMarked();
  } catch (err) {
    $('#save-state').textContent = '清除失败: ' + err.message;
  }
};
// 下一帧(跳过):不标记当前帧,直接走(实时=按间隔取下一帧并预选当前选项;队列=队列下一张)
$('#btn-next-skip').onclick = async () => {
  if (!cur) return;
  if (liveMode) {
    livePlayStop();
    jumpToFrame(cur.timestamp_ms + liveIntervalMs());
  } else {
    stepFrame(1);
  }
};
// 上一帧:不保存,往回退一帧(同样预选,微调时表单不丢)
$('#btn-prev-skip').onclick = async () => {
  if (!cur) return;
  if (liveMode) {
    livePlayStop();
    jumpToFrame(Math.max(0, cur.timestamp_ms - liveIntervalMs()));
  } else {
    stepFrame(-1);
  }
};

// 定位第一个缺失字段
const FIELD_ANCHORS = {
  '是否为虚荣画面': '#grp-family',
  '对局阶段': '#blk-stage',
  '具体界面': '#blk-screen',
  '游戏模式': '#grp-mode',
  '真人/人机': '#grp-matchkind',
  '结算面板边界框': '.btn-draw-box[data-type="result_panel"]',
};
function focusFirstMissing(name) {
  const sel = FIELD_ANCHORS[name] || '#grp-family';
  const el = $(sel);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}


// ---------- 边界框(通用,支持 config.BOX_TYPES 全部类型) ----------
const STANDARD_BOX_TYPE_KEYS = [
  'viewport', 'result_panel', 'scoreboard_panel', 'shop_panel',
  'equipment_panel', 'talent_panel',
];
const BOX_TYPE_KEYS = [...STANDARD_BOX_TYPE_KEYS, 'mode_gate'];
const BOX_LABELS = {
  viewport: '游戏窗口', result_panel: '结算面板',
  scoreboard_panel: '积分板', shop_panel: '商店面板',
  equipment_panel: '出装面板', talent_panel: '天赋选择面板',
  mode_gate: '黄色光栅',
};

// 确保画布上有每种框的 overlay 元素
function ensureBoxEls() {
  const wrap = $('#canvas-wrap');
  BOX_TYPE_KEYS.forEach((t) => {
    if (!$('#box-' + t)) {
      wrap.insertAdjacentHTML('beforeend',
        `<div class="bbox bbox-${t}" id="box-${t}"><span class="bbox-tag">${BOX_LABELS[t]}</span></div>`);
    }
  });
}

// 边界框工具栏(绘制/清除按钮)
function buildBoxToolbar() {
  const tb = $('#box-toolbar');
  if (!tb || tb.dataset.built) return;
  tb.dataset.built = '1';
  Object.entries(CFG.box_types).forEach(([t, label]) => {
    tb.insertAdjacentHTML('beforeend',
      `<button class="opt btn-draw-box" data-type="${t}">绘制 ${BOX_LABELS[t] || t}</button>`);
    tb.insertAdjacentHTML('beforeend',
      `<button class="opt btn-clear-box" data-type="${t}">清除 ${BOX_LABELS[t] || t}</button>`);
  });
  tb.addEventListener('click', (e) => {
    const d = e.target.closest('.btn-draw-box');
    if (d) { toggleDraw(d.dataset.type); return; }
    const c = e.target.closest('.btn-clear-box');
    if (c) { clearBox(c.dataset.type); }
  });
}

function renderBoxes() {
  ensureBoxEls();
  const boxes = cur ? cur.boxes || {} : {};
  STANDARD_BOX_TYPE_KEYS.forEach((t) => {
    const el = $('#box-' + t);
    const b = boxes[t];
    if (!isGateTask() && b) {
      el.style.left = (b.x * 100) + '%';
      el.style.top = (b.y * 100) + '%';
      el.style.width = (b.w * 100) + '%';
      el.style.height = (b.h * 100) + '%';
      el.classList.add('active');
      el.dataset.boxType = t;
    } else {
      el.classList.remove('active');
    }
  });

  // mode_gate 的固定元素只用于正在绘制的新框；已保存框按实例动态渲染。
  const draft = $('#box-mode_gate');
  draft.classList.remove('active', 'open-entrance');
  draft.dataset.boxType = 'mode_gate';
  $$('#canvas-wrap .gate-box').forEach((el) => el.remove());
  if (isGateTask() && cur && cur.gate_annotation &&
      cur.gate_annotation.evidence !== 'no_evidence') {
    const open = cur.gate_annotation.evidence === 'open_entrance';
    gateBoxesOf(cur.gate_annotation).forEach((box, index) => {
      const el = document.createElement('div');
      el.className = 'bbox bbox-mode_gate gate-box active';
      if (open) el.classList.add('open-entrance');
      el.dataset.boxType = 'mode_gate';
      el.dataset.gateIndex = String(index);
      el.style.left = (box.x * 100) + '%';
      el.style.top = (box.y * 100) + '%';
      el.style.width = (box.w * 100) + '%';
      el.style.height = (box.h * 100) + '%';

      const tag = document.createElement('span');
      tag.className = 'bbox-tag';
      tag.textContent = open ? `3V3 开放入口 ${index + 1}` : `大乱斗光栅 ${index + 1}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'bbox-delete';
      remove.title = '只删除这个框';
      remove.setAttribute('aria-label', `删除第 ${index + 1} 个框`);
      remove.textContent = '×';
      el.append(tag, remove);
      $('#canvas-wrap').appendChild(el);
    });
  }
  bindBoxInteractions();
  updateDrawButtons();
}

let drag = null; // {mode, type, startX, startY, orig}
function bindBoxInteractions() {
  const wrap = $('#canvas-wrap');
  // 绘制
  wrap.onpointerdown = (e) => {
    if (!drawMode || !cur) return;
    if (drawMode === 'mode_gate') {
      const draft = $('#box-mode_gate');
      const open = gateEvidence === 'open_entrance';
      draft.classList.toggle('open-entrance', open);
      draft.querySelector('.bbox-tag').textContent =
        open ? '新开放入口' : '新大乱斗光栅';
    }
    const rect = wrap.getBoundingClientRect();
    drag = { mode: 'draw', type: drawMode, x0: e.clientX, y0: e.clientY, rect };
    wrap.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  wrap.onpointermove = (e) => {
    if (!drag || drag.mode !== 'draw') return;
    const { x0, y0, rect } = drag;
    const nx = Math.min(Math.max((Math.min(e.clientX, x0) - rect.left) / rect.width, 0), 1);
    const ny = Math.min(Math.max((Math.min(e.clientY, y0) - rect.top) / rect.height, 0), 1);
    const w = Math.min(Math.max(Math.abs(e.clientX - x0) / rect.width, 0.01), 1 - nx);
    const h = Math.min(Math.max(Math.abs(e.clientY - y0) / rect.height, 0.01), 1 - ny);
    const el = $('#box-' + drag.type);
    el.style.left = (nx * 100) + '%';
    el.style.top = (ny * 100) + '%';
    el.style.width = (w * 100) + '%';
    el.style.height = (h * 100) + '%';
    el.classList.add('active');
    el.dataset.boxType = drag.type;
  };
  wrap.onpointerup = async (e) => {
    if (!drag || drag.mode !== 'draw') return;
    const { rect, type } = drag;
    const x = Math.min(Math.max((Math.min(e.clientX, drag.x0) - rect.left) / rect.width, 0), 1);
    const y = Math.min(Math.max((Math.min(e.clientY, drag.y0) - rect.top) / rect.height, 0), 1);
    const w = Math.min(Math.max(Math.abs(e.clientX - drag.x0) / rect.width, 0.01), 1 - x);
    const h = Math.min(Math.max(Math.abs(e.clientY - drag.y0) / rect.height, 0.01), 1 - y);
    drag = null;
    if (type !== 'mode_gate') drawMode = null;
    updateDrawButtons();
    await saveBox(type, x, y, w, h);
  };
  // 已有框:拖动移动
  $$('.bbox.active').forEach((el) => {
    const gateIndex = el.dataset.gateIndex == null
      ? null : Number(el.dataset.gateIndex);
    const remove = el.querySelector('.bbox-delete');
    if (remove) {
      remove.onpointerdown = (e) => e.stopPropagation();
      remove.onclick = async (e) => {
        e.stopPropagation();
        await deleteGateBox(gateIndex);
      };
    }
    el.onpointerdown = (e) => {
      if (drawMode) return;
      e.stopPropagation();
      const b = currentBox(el.dataset.boxType, gateIndex);
      if (!b) return;
      el.style.zIndex = 50;  // 拖动的框置顶,重合时不误触其他框
      drag = {
        mode: 'move', type: el.dataset.boxType, gateIndex,
        x0: e.clientX, y0: e.clientY,
        orig: { x: b.x, y: b.y, w: b.w, h: b.h },
        rect: wrap.getBoundingClientRect(),
      };
      el.setPointerCapture(e.pointerId);
    };
    el.onpointermove = (e) => {
      if (!drag || drag.mode !== 'move' || drag.type !== el.dataset.boxType ||
          drag.gateIndex !== gateIndex) return;
      const dx = (e.clientX - drag.x0) / drag.rect.width;
      const dy = (e.clientY - drag.y0) / drag.rect.height;
      const nx = Math.min(Math.max(drag.orig.x + dx, 0), 1 - drag.orig.w);
      const ny = Math.min(Math.max(drag.orig.y + dy, 0), 1 - drag.orig.h);
      el.style.left = (nx * 100) + '%';
      el.style.top = (ny * 100) + '%';
    };
    el.onpointerup = async (e) => {
      if (!drag || drag.mode !== 'move' || drag.type !== el.dataset.boxType ||
          drag.gateIndex !== gateIndex) return;
      const dx = (e.clientX - drag.x0) / drag.rect.width;
      const dy = (e.clientY - drag.y0) / drag.rect.height;
      const nx = Math.min(Math.max(drag.orig.x + dx, 0), 1 - drag.orig.w);
      const ny = Math.min(Math.max(drag.orig.y + dy, 0), 1 - drag.orig.h);
      const t = drag.type;
      drag = null;
      const box = currentBox(t, gateIndex);
      if (!box) return;
      if (t === 'mode_gate') {
        await updateGateBox(gateIndex, nx, ny, box.w, box.h);
      } else {
        await saveBox(t, nx, ny, box.w, box.h);
      }
    };
  });
}

function currentBox(type, gateIndex = null) {
  if (!cur) return null;
  if (type === 'mode_gate') {
    return gateIndex == null ? null : gateBoxesOf()[gateIndex] || null;
  }
  return (cur.boxes || {})[type] || null;
}

async function updateGateBox(index, x, y, w, h) {
  if (!cur || !cur.gate_annotation) return;
  const boxes = gateBoxesOf().map(gateBoxPayload);
  if (!boxes[index]) return;
  boxes[index] = gateBoxPayload({ x, y, w, h });
  await saveGateAnnotation({
    evidence: cur.gate_annotation.evidence,
    boxes,
  });
}

async function deleteGateBox(index) {
  if (!cur || !cur.gate_annotation || index == null) return;
  const evidence = cur.gate_annotation.evidence;
  const boxes = gateBoxesOf().map(gateBoxPayload);
  if (!boxes[index]) return;
  boxes.splice(index, 1);
  if (boxes.length) {
    await saveGateAnnotation({ evidence, boxes });
    return;
  }
  await api(`/api/mode-gate/rounds/${state.gateRound.id}/frames/${cur.id}`,
    { method: 'DELETE' });
  cur.gate_annotation = null;
  gateEvidence = null;
  drawMode = null;
  renderGateInspector();
  renderBoxes();
  await loadLiveMarked();
}

async function saveBox(type, x, y, w, h) {
  if (!type || !cur) return;
  if (type === 'mode_gate') {
    if (!gateEvidence) {
      alert('请先选择“有黄色光栅”或“开放入口”');
      renderBoxes();
      return;
    }
    const boxes = cur.gate_annotation &&
      cur.gate_annotation.evidence === gateEvidence
      ? gateBoxesOf().map(gateBoxPayload) : [];
    boxes.push(gateBoxPayload({ x, y, w, h }));
    await saveGateAnnotation({
      evidence: gateEvidence,
      boxes,
    });
    return;
  }
  try {
    await api(`/api/frames/${cur.id}/box`, {
      method: 'PUT',
      body: JSON.stringify({ box_type: type, x: round(x), y: round(y), w: round(w), h: round(h) }),
    });
    cur.boxes = await api(`/api/frames/${cur.id}`).then((f) => f.boxes);
    // 保存后更新主播级默认框缓存(后续帧/视频自动带出)
    if (streamerBoxes && streamerBoxes.streamer === cur.streamer) {
      streamerBoxes.boxes[type] = { x, y, w, h };
    }
  } catch (err) {
    alert('框保存失败: ' + err.message + '\n(请刷新后重试)');
    // 回滚本地显示,避免误以为已保存
    try {
      cur = await api(`/api/frames/${cur.id}`);
      renderBoxes();
    } catch (_) { /* 忽略 */ }
  }
}
function round(n) { return Math.round(n * 10000) / 10000; }

function toggleDraw(t) {
  drawMode = drawMode === t ? null : t;
  updateDrawButtons();
}
function updateDrawButtons() {
  $$('.btn-draw-box').forEach((b) =>
    b.classList.toggle('selected', drawMode === b.dataset.type));
  // 绘制模式下屏蔽已有框的拖动,避免重合时误编辑其他框
  $$('.bbox.active').forEach((b) => {
    b.style.pointerEvents = drawMode ? 'none' : '';
  });
}
async function clearBox(t) {
  if (!cur) return;
  if (t === 'mode_gate') {
    $('#btn-gate-clear').click();
    return;
  }
  await api(`/api/frames/${cur.id}/box?box_type=${t}`, { method: 'DELETE' });
  // 同步删除主播默认,避免下帧又自动带出
  api(`/api/videos/${cur.video_id}/streamer-box/${t}`, { method: 'DELETE' }).catch(() => {});
  cur = await api(`/api/frames/${cur.id}`);
  renderBoxes();
  // 清除框时同步清除主播默认缓存
  if (streamerBoxes && streamerBoxes.streamer === cur.streamer) {
    delete streamerBoxes.boxes[t];
  }
}
$('#btn-fit').onclick = () => {
  const wrap = $('#canvas-wrap');
  wrap.style.maxWidth = '100%';
  wrap.style.maxHeight = '100%';
};

// ---------- 事件操作 ----------
$('#btn-representative').onclick = async () => {
  if (!cur) return;
  const v = cur.is_representative ? 0 : 1;
  await api(`/api/frames/${cur.id}/representative`, {
    method: 'POST', body: JSON.stringify({ value: v }),
  });
  cur.is_representative = v;
  renderTimeline();
};
$('#btn-propagate').onclick = async () => {
  if (!cur || !cur.event_id) { alert('该帧不属于事件'); return; }
  const fields = ['game_mode', 'view_context', 'match_kind', 'quality_flags', 'black_bars', 'viewport_bbox'];
  const r = await api(`/api/frames/${cur.id}/propagate`, {
    method: 'POST', body: JSON.stringify({ fields }),
  });
  alert(`已传播 ${r.propagated} 帧`);
};

// 撤销
$('#btn-undo').onclick = async () => {
  const r = await api('/api/undo', { method: 'POST', body: '{}' });
  if (r.undone && cur && r.frame_id === cur.id) {
    cur = await api(`/api/frames/${cur.id}`);
    renderInspector();
    renderBoxes();
    setStatus(cur.annotation ? cur.annotation.annotation_status : 'draft');
  }
  $('#save-state').textContent = r.undone ? '已撤销' : '没有可撤销操作';
};

// ---------- 时间轴 ----------
async function renderTimeline() {
  const strip = $('#tl-strip');
  if (!cur || !cur.event_id) {
    strip.innerHTML = '';
    $('#tl-event-label').textContent = cur ? '当前帧不属于事件' : '';
    tlCache = null;
    return;
  }
  // 事件数据缓存:同事件不重复 fetch(播放时每 500ms 切帧)
  if (!tlCache || tlCache.eventId !== cur.event_id) {
    tlCache = { eventId: cur.event_id, data: await api(`/api/events/${cur.event_id}`) };
  }
  const { data } = tlCache;
  $('#tl-event-label').textContent =
    `事件 #${data.event.id} ${data.event.start_ms / 1000}s~${data.event.end_ms / 1000}s · ${data.frames.length} 帧`;
  strip.innerHTML = data.frames.map((f) => {
    const label = f.annotation ? (f.annotation.screen_type || '') : '';
    return `<div class="tl-frame${f.id === cur.id ? ' current' : ''}" data-fid="${f.id}">
      <img src="/api/frames/${f.id}/thumb?t=${f.id}" loading="lazy">
      ${f.is_representative ? '<span class="tl-rep">★</span>' : ''}
      <span class="tl-badge">${(f.timestamp_ms / 1000).toFixed(0)}s ${esc(label)}</span>
    </div>`;
  }).join('');
}
let tlCache = null;
$('#tl-strip').addEventListener('click', (e) => {
  const cell = e.target.closest('.tl-frame');
  if (!cell) return;
  const fid = +cell.dataset.fid;
  const f = queue.find((x) => x.id === fid);
  if (f) { qIdx = queue.indexOf(f); showFrame(f); }
  else if (cur) { qIdx = 0; queue = [cur]; showFrame(cur); }
});
$('#btn-play').onclick = () => togglePlay();
function togglePlay() {
  playing = !playing;
  $('#btn-play').textContent = playing ? '暂停' : '播放';
  if (playing) {
    playTimer = setInterval(() => stepFrame(1), 500);
  } else {
    clearInterval(playTimer);
  }
}
$('#btn-step-prev').onclick = () => { if (cur && cur.event_id) jumpEvent(-1); };
$('#btn-step-next').onclick = () => { if (cur && cur.event_id) jumpEvent(1); };
$('#btn-tl-prev').onclick = () => jumpEvent(-1);
$('#btn-tl-next').onclick = () => jumpEvent(1);
async function jumpEvent(dir) {
  if (!cur || !cur.event_id) return;
  const events = await api('/api/events');
  const mine = events.filter((e) => e.video_id === cur.video_id).sort((a, b) => a.id - b.id);
  const idx = mine.findIndex((e) => e.id === cur.event_id);
  const target = mine[idx + dir];
  if (!target) return;
  const data = await api(`/api/events/${target.id}`);
  if (!data.frames.length) return;
  // 优先切到代表帧
  const rep = data.frames.find((f) => f.is_representative) || data.frames[0];
  queue = data.frames;
  qIdx = queue.findIndex((f) => f.id === rep.id);
  showFrame(rep);
}

// ---------- 快捷键 ----------
function bindShortcuts() {
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
    if (!$('#view-label').classList.contains('active')) return;
    const k = e.key.toLowerCase();
    if (isGateTask()) {
      if (k === 'b') {
        chooseGateEvidence('blocked_gate');
      } else if (k === 'o') {
        chooseGateEvidence('open_entrance');
      } else if (k === 'n') {
        $('#btn-gate-no-evidence').click();
      } else if (k === 'enter') {
        e.preventDefault();
        nextGateFrame();
      } else if (e.code === 'Space') {
        e.preventDefault();
        livePlayToggle();
      } else if (e.key === 'ArrowLeft' && cur) {
        e.preventDefault();
        jumpToFrame(Math.max(0, cur.timestamp_ms - liveIntervalMs()));
      } else if (e.key === 'ArrowRight' && cur) {
        e.preventDefault();
        jumpToFrame(cur.timestamp_ms + liveIntervalMs());
      } else if (k === 'f') {
        $('#btn-fit').click();
      } else if (k === 'escape') {
        drawMode = null;
        updateDrawButtons();
        renderGateInspector();
      }
      return;
    }
    // 快捷整组设置(虚荣 → 阶段 → 界面)
    const quickSet = (stage, screen) => {
      if (!cur) return;
      const a = cur.annotation = cur.annotation || {};
      a.content_family = 'vainglory';
      a.game_context = stage;
      a.screen_type = screen;
      renderInspector();
      renderBoxes();
      setStatus('draft');
      scheduleSave('draft');
    };
    if (k === 'r') {           // 结算页 + 进入画框
      quickSet('post_match', 'result_page');
      toggleDraw('result_panel');
    } else if (k === 's') {    // 积分板
      quickSet('in_match', 'scoreboard');
    } else if (k === 'd') {    // 死亡积分板
      quickSet('in_match', 'death_scoreboard');
    } else if (k === 'g') {    // 普通战斗画面
      quickSet('in_match', 'gameplay');
    } else if (k === 'v') {    // 只选虚荣,展开下一层
      if (!cur) return;
      cur.annotation = cur.annotation || {};
      cur.annotation.content_family = 'vainglory';
      renderInspector();
      setStatus('draft');
      scheduleSave('draft');
      const firstStage = $('#grp-stage .opt');
      if (firstStage) firstStage.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } else if (k === 'x') {    // 非虚荣
      if (!cur) return;
      cur.annotation = cur.annotation || {};
      cur.annotation.content_family = 'not_vainglory';
      renderInspector();
      setStatus('draft');
      scheduleSave('draft');
    } else if (k === 'u') {    // 待复核
      saveThenNext('needs_review');
    } else if (e.code === 'Space') {
      e.preventDefault();
      if (liveMode) {
        livePlayToggle();
      } else {
        togglePlay();
      }
    } else if (e.key === 'ArrowLeft' && e.shiftKey) { e.preventDefault(); jumpEvent(-1); }
    else if (e.key === 'ArrowRight' && e.shiftKey) { e.preventDefault(); jumpEvent(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); stepFrame(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); stepFrame(1); }
    else if (k === 'enter') {
      e.preventDefault();
      if (liveMode) return;  // 实时模式 Enter 不触发完成(防止误触),用按钮
      const missing = validate();
      if (missing.length) { focusFirstMissing(missing[0]); return; }
      saveThenNext('complete');
    }
    else if (k === 'f') { $('#btn-fit').click(); }
    else if ((e.metaKey || e.ctrlKey) && k === 'z') { e.preventDefault(); $('#btn-undo').click(); }
    else if (k === 'escape') { drawMode = null; updateDrawButtons(); }
  });
}

// ---------- 数据检查 ----------
async function loadStats() {
  try {
    const s = await api('/api/stats');
    const grid = $('#stats-grid');
    grid.innerHTML = '';
    const cards = [
      ['videos', '视频数'], ['streamers', '主播数'], ['frames', '总帧数'],
      ['frames_labeled', '已标注帧'], ['events', '独立事件数'],
      ['result_positives', '结算正样本'], ['result_representatives', '结算代表帧'],
      ['result_with_bbox', '有框结算样本'], ['scoreboard_negatives', '积分板 hard negative'],
      ['random_negatives', '负样本(非虚荣/游戏)'],
      ['duplicate_groups', '重复内容组(sha256)'],
    ];
    cards.forEach(([k, l]) => {
      grid.insertAdjacentHTML('beforeend',
        `<div class="stat-card"><div class="num">${s[k] ?? 0}</div><div class="lbl">${l}</div></div>`);
    });
    const issues = $('#stats-issues');
    issues.innerHTML = '<h3>冲突与缺失</h3>';
    const list = [
      ['missing_content_family', '已标帧缺 content_family'],
      ['not_vg_has_game_context', '非虚荣却填了游戏上下文'],
      ['result_without_bbox', '结算正样本缺 result_panel 框(导出时会排除)'],
      ['result_without_ocr', '结算样本未标 OCR 可用性(辅助)'],
    ];
    list.forEach(([k, l]) => {
      const n = s.issues[k] || 0;
      issues.insertAdjacentHTML('beforeend',
        `<div class="issue-item ${n === 0 ? 'ok' : ''}">${l}: <b>${n}</b></div>`);
    });
    issues.insertAdjacentHTML('beforeend',
      `<p class="hint">模式分布: ${JSON.stringify(s.game_modes)}</p>
       <p class="hint">界面分布: ${JSON.stringify(s.screen_types)}</p>
       <p class="hint">黑边: ${JSON.stringify(s.black_bars)}</p>
       <p class="hint">画质异常: ${JSON.stringify(s.quality_flags)}</p>`);
  } catch (e) { /* 服务未就绪 */ }
}
$$('.nav-item').forEach((b) => {
  b.addEventListener('click', () => {
    if (b.dataset.view === 'inspect') loadStats();
    if (b.dataset.view === 'datasets') { loadDatasets(); }
    if (b.dataset.view === 'pairs') loadPairs();
  });
});

// ---------- 导出 ----------
async function loadDatasets() {
  const vs = await api('/api/datasets');
  const tbody = $('#datasets-table tbody');
  tbody.innerHTML = '';
  vs.forEach((v) => {
    const c = v.counts_json || {};
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td>${esc(v.id)}</td><td>${esc(v.task_id)}</td><td>${esc(v.created_at)}</td>
        <td>${c.total ?? 0}</td>
        <td>${c.positive ?? 0}/${c.negative ?? 0}</td>
        <td>${JSON.stringify(c.by_split || {})}</td>
        <td>${esc(v.manifest_path)}</td><td>${esc(v.git_commit)}</td>
      </tr>`);
  });
}
$('#btn-export').onclick = async () => {
  $('#export-result').textContent = '导出中…';
  try {
    const r = await api('/api/export', {
      method: 'POST',
      body: JSON.stringify({
        task_id: 'result_detector',
        include_negatives: $('#export-neg').checked,
      }),
    });
    $('#export-result').textContent =
      `完成: ${r.version} · 正 ${r.positive} / 负 ${r.negative} · ${r.dir}`;
    loadDatasets();
  } catch (e) {
    $('#export-result').textContent = '导出失败: ' + e.message;
  }
};

// ---------- 同局配对 ----------
async function loadPairs() {
  const rows = await api('/api/pairs');
  const tbody = $('#pairs-table tbody');
  tbody.innerHTML = '';
  rows.forEach((r) => {
    tbody.insertAdjacentHTML('beforeend', `
      <tr><td>${r.frame_a_id}</td><td>${r.frame_b_id}</td>
      <td>${esc(r.label)}</td><td>${esc(r.created_at)}</td></tr>`);
  });
}
async function showPairImg(side) {
  const id = +$(`#pair-${side}`).value;
  if (!id) return;
  try {
    const f = await api(`/api/frames/${id}`);
    $(`#pair-${side}-img`).src = `/api/frames/${id}/thumb?t=${id}`;
  } catch (e) { alert('帧不存在: ' + id); }
}
$('#pair-a').onchange = () => showPairImg('a');
$('#pair-b').onchange = () => showPairImg('b');
async function savePair(label) {
  const a = +$('#pair-a').value, b = +$('#pair-b').value;
  if (!a || !b) { alert('请填写帧 A/B 的 id(在时间轴/检查器可查看)'); return; }
  await api('/api/pairs', {
    method: 'POST', body: JSON.stringify({ frame_a_id: a, frame_b_id: b, label }),
  });
  loadPairs();
}
$('#btn-pair-same').onclick = () => savePair('same_match');
$('#btn-pair-diff').onclick = () => savePair('different_match');
$('#btn-pair-uncertain').onclick = () => savePair('uncertain');
$('#btn-pair-refresh').onclick = loadPairs;

// ---------- 模型验收与 Worker 发布候选包 ----------
const MODEL_TASKS = {
  match_flow: {
    name: '是否在对局流程中', kind: 'classify', role: 'match_flow',
    labels: {match_flow: '对局流程中', not_match_flow: '非对局画面'},
  },
  hero_select: {
    name: '英雄选择与模式', kind: 'classify', role: 'hero_select',
    labels: {
      not_select: '不是英雄选择', select_3v3: '3V3 英雄选择',
      select_aram: '大乱斗英雄选择', select_5v5: '5V5 英雄选择',
    },
  },
  match_mode: {
    name: '对局画面模式', kind: 'classify', role: 'match_mode',
    labels: {'3v3': '3V3', aram: '大乱斗', '5v5': '5V5'},
  },
  result_mode: {
    name: '结算图模式', kind: 'classify', role: 'result_mode',
    labels: {'3v3': '3V3', aram: '大乱斗', '5v5': '5V5', blitz: '闪电战'},
  },
  result_detector: {
    name: '结算面板检测', kind: 'detect', role: 'result_panel',
    labels: {result_panel: '有结算面板', no_result_panel: '没有结算面板'},
  },
  hero_avatar_detector: {
    name: '英雄头像位置检测', kind: 'detect', role: 'hero_avatar',
    labels: {hero_avatar: '英雄头像'},
  },
  hero_identity: {
    name: '英雄头像身份识别', kind: 'classify', role: 'hero_identity',
    labels: {},
  },
  player_position: {
    name: '主播本人位置识别', kind: 'classify', role: 'player_position',
    labels: {
      left1: '左队第 1 位', left2: '左队第 2 位', left3: '左队第 3 位',
      left4: '左队第 4 位', left5: '左队第 5 位',
      right1: '右队第 1 位', right2: '右队第 2 位', right3: '右队第 3 位',
    },
  },
  afk_status: {
    name: '结算图挂机识别', kind: 'classify', role: 'afk_status',
    labels: {active: '正常', afk: '挂机'},
  },
  // 保留旧训练结果的可读名称，便于回看历史 run。
  screen_state: {
    name: '旧·画面状态', kind: 'classify', role: 'screen_state',
    labels: {
      gameplay: '对局中', scoreboard: '积分板', result_page: '结算页',
      victory_defeat: '胜负动画', pre_match: '赛前', out_of_match: '游戏外',
      transition: '转场', talent_select: '天赋选择', in_match: '对局中',
      not_vainglory: '非虚荣画面', post_match: '赛后',
    },
  },
  bp_review: {
    name: '旧·BP 模式', kind: 'classify', role: 'bp_classifier',
    labels: {
      bp_3v3: '3V3 BP', bp_aram: '大乱斗 BP', bp_5v5: '5V5 BP', not_bp: '非 BP',
    },
  },
  key_screen_review: {
    name: '旧·结算／计分板', kind: 'classify', role: 'key_screen',
    labels: {other: '其他画面', result_page: '结算页', scoreboard: '计分板'},
  },
  mode_gate: {
    name: '旧·大乱斗光栅', kind: 'detect', role: 'mode_gate',
    labels: {blocked_gate: '有黄色光栅', open_entrance: '开放入口'},
  },
};
const MODEL_PACKAGE_CORE_TASKS = [
  'match_flow', 'hero_select', 'match_mode', 'result_mode', 'result_detector',
];
const MODEL_PACKAGE_HERO_TASKS = [
  'hero_avatar_detector', 'hero_identity', 'player_position', 'afk_status',
];
const MODEL_PACKAGE_REQUIRED_TASKS = [
  ...MODEL_PACKAGE_CORE_TASKS, ...MODEL_PACKAGE_HERO_TASKS,
];
const MODEL_PACKAGE_TASKS = [...MODEL_PACKAGE_REQUIRED_TASKS];

function currentModelTestRun() {
  const runId = $('#model-test-run').value;
  return modelTestRuns.find((run) => run.id === runId) || null;
}

function modelTask(taskId) {
  return MODEL_TASKS[taskId] || {
    name: taskId || '未知模型', kind: 'classify', role: taskId, labels: {},
  };
}

function modelLabel(taskId, value) {
  const text = modelTask(taskId).labels[String(value || '')];
  if (text) return text;
  if (taskId === 'hero_identity') {
    const hero = candidateHeroByLabel(String(value || ''));
    if (hero) return hero.name;
  }
  return String(value || '未知');
}

function modelPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function modelValidationLabel(status) {
  return {passed: '已通过', failed: '未通过', pending: '待验收'}[status] || status;
}

function modelRunKind(run) {
  return (run && run.artifact_metadata && run.artifact_metadata.kind) ||
    modelTask(run && run.task_id).kind;
}

function modelDetectionObject(run) {
  if (run && run.task_id === 'mode_gate') return '黄色光栅';
  if (run && run.task_id === 'hero_avatar_detector') return '英雄头像';
  return '结算面板';
}

function modelTestDataUnit(run) {
  if (!run) return '张';
  if (run.task_id === 'hero_avatar_detector') return '张完整阵容图';
  if (run.task_id === 'hero_identity') return '个可读头像';
  if (run.task_id === 'player_position') return '张完整面板图';
  if (run.task_id === 'afk_status') return '个结算玩家区域';
  return '张';
}

function renderModelTestSummary() {
  const run = currentModelTestRun();
  const root = $('#model-test-summary');
  if (!run) {
    root.innerHTML = '<span class="muted small">请选择一个训练结果</span>';
    return;
  }
  const metadata = run.artifact_metadata || {};
  const input = metadata.input || {};
  const preprocessing = metadata.preprocessing || {};
  const runConfig = run.config_json || {};
  const width = Number(input.width || metadata.imgsz || runConfig.imgsz || 0);
  const height = Number(input.height || metadata.imgsz || runConfig.imgsz || 0);
  const resize = preprocessing.resize === 'aspect_fit_letterbox'
    ? '保留完整画面，等比补边' :
    preprocessing.resize === 'shortest_edge_center_crop'
      ? '旧规则：中心裁剪' :
      preprocessing.resize === 'letterbox'
        ? '等比缩放并补边' : (preprocessing.resize || '未记录');
  const total = Number((run.counts_json || {}).total || 0);
  const gaps = run.evaluation_gaps || [];
  root.innerHTML = `
    <div><span>模型</span><b>${esc(modelTask(run.task_id).name)}</b></div>
    <div><span>训练数据</span><b>${esc(run.dataset_version_id)}</b><small>${total.toLocaleString('zh-CN')} ${esc(modelTestDataUnit(run))}</small></div>
    <div><span>模型输入</span><b>${width && height ? `${width} × ${height}` : '未记录'}</b><small>${esc(resize)}</small></div>
    <div><span>当前结论</span><b class="validation-${esc(run.validation_status)}">${esc(modelValidationLabel(run.validation_status))}</b>` +
      `<small>${gaps.length ? esc(`测试集覆盖不足：${gaps.join('；')}`) : '固定测试集结构完整'}</small></div>`;
}

function modelTestReportLabel(run, value) {
  if (modelRunKind(run) === 'detect') {
    return value === 'found'
      ? `找到${modelDetectionObject(run)}` : `没有${modelDetectionObject(run)}`;
  }
  return modelLabel(run.task_id, value);
}

function modelTestReportGroupTable(title, groups, run, labelMap = {}) {
  const rows = Object.entries(groups || {});
  if (!rows.length) return '';
  return `<section class="model-test-report-block"><h4>${esc(title)}</h4>` +
    '<table><thead><tr><th>类别</th><th>正确</th><th>总数</th><th>准确率</th></tr></thead><tbody>' +
    rows.map(([label, value]) => `<tr><td>${esc(
      labelMap[label] || modelTestReportLabel(run, label)
    )}</td>` +
      `<td>${Number(value.correct || 0)}</td><td>${Number(value.total || 0)}</td>` +
      `<td>${modelPercent(value.accuracy)}</td></tr>`).join('') +
    '</tbody></table></section>';
}

function modelTestConfusionTable(report, run) {
  const confusion = report.confusion || {};
  const labels = [...new Set([
    ...Object.keys(confusion),
    ...Object.values(confusion).flatMap((value) => Object.keys(value || {})),
  ])];
  if (!labels.length) return '';
  if (labels.length > 12) {
    const mistakes = Object.entries(confusion).flatMap(([expected, predicted]) =>
      Object.entries(predicted || {})
        .filter(([value, count]) => value !== expected && Number(count) > 0)
        .map(([value, count]) => ({expected, predicted: value, count: Number(count)}))
    ).sort((left, right) => right.count - left.count);
    return '<section class="model-test-report-block"><h4>主要混淆</h4>' +
      (mistakes.length
        ? '<table><thead><tr><th>人工答案</th><th>模型答案</th><th>数量</th></tr></thead><tbody>' +
          mistakes.slice(0, 20).map((item) =>
            `<tr><td>${esc(modelTestReportLabel(run, item.expected))}</td>` +
            `<td>${esc(modelTestReportLabel(run, item.predicted))}</td>` +
            `<td>${item.count}</td></tr>`).join('') + '</tbody></table>'
        : '<p class="model-test-report-success">没有发现英雄之间的混淆。</p>') +
      '</section>';
  }
  return '<section class="model-test-report-block"><h4>混淆表</h4>' +
    '<p class="hint small">纵向是人工答案，横向是模型答案。</p><div class="table-scroll"><table>' +
    `<thead><tr><th>人工 ＼ 模型</th>${labels.map((label) =>
      `<th>${esc(modelTestReportLabel(run, label))}</th>`).join('')}</tr></thead>` +
    `<tbody>${labels.map((expected) => `<tr><th>${esc(modelTestReportLabel(run, expected))}</th>` +
      labels.map((predicted) => `<td>${Number(
        (confusion[expected] || {})[predicted] || 0
      )}</td>`).join('') + '</tr>').join('')}</tbody></table></div></section>`;
}

function openModelTestReportError(sampleId) {
  const index = modelTestSamples.findIndex((sample) => sample.sample_id === sampleId);
  if (index < 0) {
    $('#model-test-state').textContent = '这张错例不在当前已载入的页面批次中';
    return;
  }
  modelTestIndex = index;
  renderModelTestSample();
  $('#model-test-layout').scrollIntoView({block: 'start'});
  predictModelTestSample();
}

function renderModelTestBatchReport() {
  const root = $('#model-test-report');
  const report = modelTestBatchReport;
  const run = currentModelTestRun();
  root.classList.toggle('hidden', !report || !run);
  if (!report || !run) {
    root.innerHTML = '';
    return;
  }
  const errors = report.errors || [];
  const failures = report.failures || [];
  const limitedErrors = errors.slice(0, 200);
  const scenarioNames = {
    result_panel: '结算面板', scoreboard: '积分板', other_negative: '其他负样本',
    gameplay_hud: '游戏中 HUD', result_page: '结算界面',
  };
  const modeNames = {'3v3': '3V3 计分板', '5v5': '5V5 计分板', aram: '大乱斗计分板', unreadable: '模式看不清的计分板'};
  root.innerHTML = `
    <div class="model-test-report-heading">
      <div><h3>自动验收报告</h3><p class="hint small">${esc(report.run_id)} · ${esc(report.split)} · ${Number(report.elapsed_seconds || 0).toFixed(1)} 秒</p></div>
      <span class="model-test-report-result ${errors.length || failures.length ? 'has-errors' : 'all-correct'}">
        ${errors.length || failures.length ? `${errors.length} 张答案不一致` : '全部正确'}
      </span>
    </div>
    <div class="model-test-report-metrics">
      <div><span>验收总数</span><b>${Number(report.total || 0)}</b></div>
      <div><span>完成推理</span><b>${Number(report.evaluated || 0)}</b></div>
      <div><span>正确</span><b>${Number(report.correct || 0)}</b></div>
      <div><span>准确率</span><b>${modelPercent(report.accuracy)}</b></div>
    </div>
    ${report.truncated ? '<p class="model-test-report-warning">样本超过单次上限，本报告未覆盖全部图片。</p>' : ''}
    ${failures.length ? `<p class="model-test-report-warning">另有 ${failures.length} 张因图片缺失或推理异常未完成，不能算作通过。</p>` : ''}
    <div class="model-test-report-grid">
      ${modelTestReportGroupTable('按人工类别', report.by_label, run)}
      ${modelTestReportGroupTable('按画面难例', report.by_scenario, run, scenarioNames)}
      ${modelTestReportGroupTable('计分板按模式', report.scoreboard_by_mode, run, modeNames)}
      ${modelTestConfusionTable(report, run)}
    </div>
    <section class="model-test-report-errors">
      <h4>错图 ${errors.length ? `(${errors.length})` : ''}</h4>
      ${errors.length ? '<p class="hint small">点一张即可跳到逐图页，并自动重新运行，方便看框和原始输出。</p>' : '<p class="model-test-report-success">没有发现答案不一致的图片。</p>'}
      <div>${limitedErrors.map((item, index) => `
        <button type="button" data-sample-id="${esc(item.sample_id)}">
          <b>#${index + 1} ${esc(item.reason || '答案不一致')}</b>
          <span>${esc(modelTestReportLabel(run, item.expected))} → ${esc(modelTestReportLabel(run, item.predicted))}</span>
          ${run.task_id === 'hero_avatar_detector' && item.expected_count !== undefined
            ? `<small>人工 ${Number(item.expected_count)} · 模型 ${Number(item.predicted_count)} · 匹配 ${Number(item.matched_count)}</small>`
            : item.best_iou === undefined ? '' : `<small>IoU ${modelPercent(item.best_iou)}</small>`}
        </button>`).join('')}</div>
      ${errors.length > limitedErrors.length ? `<p class="hint small">这里只显示前 ${limitedErrors.length} 张错图。</p>` : ''}
    </section>`;
  $$('#model-test-report [data-sample-id]').forEach((button) => {
    button.onclick = () => openModelTestReportError(button.dataset.sampleId);
  });
}

function currentModelTestSample() {
  return modelTestSamples[modelTestIndex] || null;
}

function expectedDetectionBoxes(sample) {
  const expected = sample && sample.expected;
  return expected && Array.isArray(expected.boxes) ? expected.boxes : [];
}

function addModelTestBox(box, type, label) {
  const image = $('#model-test-image');
  const stage = $('#model-test-stage');
  if (!image.complete || !image.naturalWidth || !box) return;
  const xywh = box.xywh_norm;
  if (!Array.isArray(xywh) || xywh.length !== 4) return;
  const imageRect = image.getBoundingClientRect();
  const stageRect = stage.getBoundingClientRect();
  const element = document.createElement('div');
  element.className = `model-test-box ${type}`;
  element.style.left = `${imageRect.left - stageRect.left + Number(xywh[0]) * imageRect.width}px`;
  element.style.top = `${imageRect.top - stageRect.top + Number(xywh[1]) * imageRect.height}px`;
  element.style.width = `${Number(xywh[2]) * imageRect.width}px`;
  element.style.height = `${Number(xywh[3]) * imageRect.height}px`;
  const tag = document.createElement('span');
  tag.textContent = label;
  element.appendChild(tag);
  $('#model-test-detections').appendChild(element);
}

function renderModelTestDetections() {
  const root = $('#model-test-detections');
  root.innerHTML = '';
  const run = currentModelTestRun();
  const sample = currentModelTestSample();
  if (!run || modelRunKind(run) !== 'detect' || !sample) return;
  expectedDetectionBoxes(sample).forEach((box) =>
    addModelTestBox(box, 'expected', '训练框'));
  ((modelTestPrediction && modelTestPrediction.detections) || []).forEach((box) =>
    addModelTestBox(box, 'predicted', `模型 ${modelPercent(box.conf)}`));
}

function renderModelTestExpected(sample, run) {
  const root = $('#model-test-expected');
  if (!sample) {
    root.textContent = '--';
    return;
  }
  if (modelRunKind(run) === 'detect') {
    const expected = sample.expected || {};
    const found = Boolean(expected.found);
    const count = expectedDetectionBoxes(sample).length;
    const objectName = modelDetectionObject(run);
    const isHeroAvatar = run.task_id === 'hero_avatar_detector';
    const modeNames = {'3v3': '3V3', '5v5': '5V5', aram: '大乱斗'};
    const isScoreboard = sample.evaluation_scenario === 'scoreboard';
    const negativeDetail = isScoreboard
      ? `${modeNames[sample.evaluation_mode] || '模式看不清'}计分板：不能误报成结算面板`
      : '这是无框负样本';
    root.innerHTML = `<div class="model-test-primary-answer ${found ? 'positive' : 'negative'}">` +
      `${found
        ? isHeroAvatar ? `应该完整找到 ${count} 个${objectName}` : `应该找到${objectName}`
        : `不应该找到${objectName}`}</div>` +
      `<div class="muted small">${found
        ? `人工框 ${count} 个（图中绿色框）${isHeroAvatar ? '，数量和位置都必须正确' : ''}`
        : negativeDetail}</div>`;
    return;
  }
  if (run.task_id === 'hero_identity') {
    root.innerHTML = renderHeroIdentityAnswer(sample.expected, '人工答案');
    return;
  }
  root.innerHTML = `<div class="model-test-primary-answer">${esc(modelLabel(run.task_id, sample.expected))}</div>` +
    `<div class="muted small">内部标签：${esc(sample.expected)}</div>`;
}

function normalizedBoxIou(first, second) {
  const a = first && first.xywh_norm;
  const b = second && second.xywh_norm;
  if (!Array.isArray(a) || !Array.isArray(b)) return 0;
  const ax2 = Number(a[0]) + Number(a[2]);
  const ay2 = Number(a[1]) + Number(a[3]);
  const bx2 = Number(b[0]) + Number(b[2]);
  const by2 = Number(b[1]) + Number(b[3]);
  const intersection = Math.max(0, Math.min(ax2, bx2) - Math.max(Number(a[0]), Number(b[0]))) *
    Math.max(0, Math.min(ay2, by2) - Math.max(Number(a[1]), Number(b[1])));
  const union = Number(a[2]) * Number(a[3]) + Number(b[2]) * Number(b[3]) - intersection;
  return union > 0 ? intersection / union : 0;
}

function bestDetectionIou(expected, predicted) {
  let best = 0;
  expected.forEach((truth) => predicted.forEach((guess) => {
    best = Math.max(best, normalizedBoxIou(truth, guess));
  }));
  return best;
}

function detectionMatchSummary(expected, predicted, threshold = 0.5) {
  const pairs = [];
  expected.forEach((truth, truthIndex) => predicted.forEach((guess, guessIndex) => {
    pairs.push({
      iou: normalizedBoxIou(truth, guess), truthIndex, guessIndex,
    });
  }));
  pairs.sort((left, right) => right.iou - left.iou);
  const usedTruth = new Set();
  const usedGuess = new Set();
  const matched = [];
  pairs.forEach((pair) => {
    if (pair.iou < threshold || usedTruth.has(pair.truthIndex) ||
        usedGuess.has(pair.guessIndex)) return;
    usedTruth.add(pair.truthIndex);
    usedGuess.add(pair.guessIndex);
    matched.push(pair.iou);
  });
  return {
    expectedCount: expected.length,
    predictedCount: predicted.length,
    matchedCount: matched.length,
    meanIou: matched.length
      ? matched.reduce((total, value) => total + value, 0) / matched.length : 0,
  };
}

function renderModelTestComparison(result) {
  const root = $('#model-test-comparison');
  const sample = currentModelTestSample();
  const run = currentModelTestRun();
  if (!result || !sample || !run) {
    root.className = 'model-test-empty';
    root.textContent = '运行后显示结果';
    return;
  }
  let matched = false;
  let detail = '';
  if (result.task === 'classify') {
    matched = String(result.top1 && result.top1.class) === String(sample.expected);
    detail = `${modelLabel(run.task_id, sample.expected)} → ` +
      `${modelLabel(run.task_id, result.top1 && result.top1.class)}`;
  } else {
    const expectedFound = Boolean(sample.expected && sample.expected.found);
    matched = expectedFound === Boolean(result.found);
    if (expectedFound && result.found) {
      const expectedBoxes = expectedDetectionBoxes(sample);
      const predictedBoxes = result.detections || [];
      if (run.task_id === 'hero_avatar_detector') {
        const summary = detectionMatchSummary(expectedBoxes, predictedBoxes);
        matched = summary.expectedCount === summary.predictedCount &&
          summary.expectedCount === summary.matchedCount;
        detail = `人工 ${summary.expectedCount} 个 · 模型 ${summary.predictedCount} 个 · ` +
          `位置匹配 ${summary.matchedCount} 个 · 平均 IoU ${modelPercent(summary.meanIou)}`;
      } else {
        const iou = bestDetectionIou(expectedBoxes, predictedBoxes);
        detail = `存在性判断正确 · 最佳框重合度 ${modelPercent(iou)}`;
        matched = matched && iou >= 0.5;
      }
    } else {
      const objectName = modelDetectionObject(run);
      detail = expectedFound ? `漏掉了应该存在的${objectName}` :
        (result.found ? `把负样本误报成了${objectName}` : '负样本判断正确');
    }
  }
  root.className = `model-test-comparison ${matched ? 'matched' : 'mismatched'}`;
  root.innerHTML = `<b>${matched ? '答案一致' : '答案不一致'}</b><span>${esc(detail)}</span>`;
}

function renderHeroIdentityAnswer(label, caption = '') {
  const hero = candidateHeroByLabel(String(label || ''));
  const name = hero ? hero.name : modelLabel('hero_identity', label);
  const image = hero && hero.image_url
    ? `<img src="${esc(hero.image_url)}" alt="${esc(name)}">` : '';
  return `<div class="model-hero-answer">${image}<div>` +
    `${caption ? `<small>${esc(caption)}</small>` : ''}` +
    `<b>${esc(name)}</b><span>${esc(label || '未知')}</span></div></div>`;
}

function renderClassificationOutput(result, run) {
  const scores = result.scores || result.top5 || [];
  const top1 = result.top1 || {};
  if (run.task_id === 'hero_identity') {
    return renderHeroIdentityAnswer(top1.class, '模型答案') +
      `<div class="model-hero-confidence">置信度 <b>${modelPercent(top1.prob)}</b></div>` +
      `<div class="model-score-list">${scores.map((score) => `
        <div class="model-score-row">
          <span>${esc(modelLabel(run.task_id, score.class))}</span>
          <div><i style="width:${Math.max(0, Math.min(100, Number(score.prob || 0) * 100))}%"></i></div>
          <b>${modelPercent(score.prob)}</b>
        </div>`).join('')}</div>`;
  }
  return `<div class="model-test-primary-answer">${esc(modelLabel(run.task_id, top1.class))}` +
    `<strong>${modelPercent(top1.prob)}</strong></div>` +
    `<div class="model-score-list">${scores.map((score) => `
      <div class="model-score-row">
        <span>${esc(modelLabel(run.task_id, score.class))}</span>
        <div><i style="width:${Math.max(0, Math.min(100, Number(score.prob || 0) * 100))}%"></i></div>
        <b>${modelPercent(score.prob)}</b>
      </div>`).join('')}</div>`;
}

function renderDetectionOutput(result, run) {
  const detections = result.detections || [];
  const objectName = modelDetectionObject(run);
  return `<div class="model-test-primary-answer ${result.found ? 'positive' : 'negative'}">` +
    `${result.found ? `找到了 ${detections.length} 个${objectName}` : `没有找到${objectName}`}` +
    `<strong>最高 ${modelPercent(result.raw_top_conf)}</strong></div>` +
    (detections.length ? `<div class="model-detection-list">${detections.map((item, index) =>
      `<span>框 ${index + 1} · ${modelPercent(item.conf)}</span>`).join('')}</div>` : '');
}

function renderModelTestPrediction(result) {
  const run = currentModelTestRun();
  modelTestPrediction = result;
  $('#model-test-output').innerHTML = !result || !run ? '尚未运行' :
    (result.task === 'classify'
      ? renderClassificationOutput(result, run) : renderDetectionOutput(result, run));
  const details = $('#model-test-raw-details');
  details.classList.toggle('hidden', !result);
  $('#model-test-raw-output').textContent = result ? JSON.stringify(result, null, 2) : '--';
  renderModelTestComparison(result);
  renderModelTestDetections();
}

function renderModelTestSample() {
  const sample = currentModelTestSample();
  const run = currentModelTestRun();
  $('#model-test-progress').textContent = modelTestSamples.length
    ? `${modelTestIndex + 1}/${modelTestSamples.length}` : '0/0';
  $('#btn-model-test-prev').disabled = !sample || modelTestIndex <= 0;
  $('#btn-model-test-next').disabled =
    !sample || modelTestIndex >= modelTestSamples.length - 1;
  $('#btn-model-test-batch').disabled = !run || !modelTestSamples.length;
  renderModelTestPrediction(null);
  $('#model-test-conf-row').classList.toggle('hidden', modelRunKind(run) !== 'detect');
  if (!sample || !sample.has_snapshot_image) {
    $('#model-test-image').removeAttribute('src');
    $('#model-test-expected').textContent = sample
      ? '这次训练快照中的图片文件缺失' : '--';
    $('#btn-model-test-predict').disabled = true;
    return;
  }
  $('#model-test-image').src =
    `/api/model-tests/runs/${encodeURIComponent($('#model-test-run').value)}` +
    `/samples/${encodeURIComponent(sample.sample_id)}/image` +
    `?split=${encodeURIComponent(sample.split)}`;
  renderModelTestExpected(sample, run);
  $('#btn-model-test-predict').disabled = false;
}

function moveModelTestSample(offset) {
  if (!modelTestSamples.length) return;
  modelTestIndex = Math.max(
    0, Math.min(modelTestSamples.length - 1, modelTestIndex + offset));
  renderModelTestSample();
}

async function loadModelTestSamples() {
  const runId = $('#model-test-run').value;
  modelTestBatchReport = null;
  renderModelTestBatchReport();
  if (!runId) {
    modelTestSamples = [];
    renderModelTestSample();
    return;
  }
  $('#model-test-state').textContent = '正在读取冻结快照…';
  try {
    const run = currentModelTestRun();
    let split = $('#model-test-split').value;
    if (split === 'scoreboard_challenge' && run.task_id !== 'result_detector') {
      split = 'test';
      $('#model-test-split').value = split;
    }
    const data = await api(
      `/api/model-tests/runs/${encodeURIComponent(runId)}/samples` +
      `?split=${encodeURIComponent(split)}&limit=1000`);
    modelTestSamples = data.items || [];
    modelTestIndex = 0;
    $('#model-test-notes').value = (run && run.validation_notes) || '';
    const distribution = data.distribution || {};
    let breakdown = '';
    if (run.task_id === 'result_detector') {
      breakdown = ` · 结算 ${distribution.result_panel || 0} · 积分板 ${distribution.scoreboard || 0} · ` +
        `其他 ${distribution.other_negative || 0}`;
    } else if (run.task_id === 'hero_avatar_detector') {
      const screenCounts = modelTestSamples.reduce((counts, sample) => {
        const key = String(sample.evaluation_scenario || '');
        counts[key] = (counts[key] || 0) + 1;
        return counts;
      }, {});
      breakdown = ` · HUD ${screenCounts.gameplay_hud || 0} · ` +
        `积分板 ${screenCounts.scoreboard || 0} · 结算 ${screenCounts.result_page || 0}`;
    } else if (run.task_id === 'hero_identity') {
      const heroes = new Set(modelTestSamples.map((sample) => sample.expected));
      breakdown = ` · 覆盖 ${heroes.size} 位英雄`;
    } else if (run.task_id === 'player_position') {
      const positions = new Set(modelTestSamples.map((sample) => sample.expected));
      const screens = modelTestSamples.reduce((counts, sample) => {
        const key = String(sample.evaluation_scenario || '');
        counts[key] = (counts[key] || 0) + 1;
        return counts;
      }, {});
      breakdown = ` · 覆盖 ${positions.size} 个位置 · ` +
        `积分板 ${screens.scoreboard || 0} · 结算 ${screens.result_page || 0}`;
    }
    const sourceName = {
      test: '固定测试集', val: '验证集', train: '训练集',
      scoreboard_challenge: '最新计分板难例',
      post_run_challenge: '训练后新增确认集',
    }[split] || split;
    const supplemental = data.is_fixed_snapshot === false
      ? ` · ${Number(data.new_video_count || 0)} 个新视频 · ` +
        '随人工确认增长，与固定测试集分开统计' : '';
    $('#model-test-state').textContent =
      `${sourceName}共 ${data.total || 0} 张${breakdown}${supplemental} · 当前验收：` +
      `${modelValidationLabel((run && run.validation_status) || 'pending')}`;
    renderModelTestSummary();
    renderModelTestSample();
  } catch (error) {
    $('#model-test-state').textContent = '载入失败：' + error.message;
  }
}

async function runModelTestBatch() {
  const runId = $('#model-test-run').value;
  const run = currentModelTestRun();
  if (!runId || !run || !modelTestSamples.length) return;
  const split = $('#model-test-split').value;
  const button = $('#btn-model-test-batch');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '正在测试全部…';
  $('#model-test-state').textContent =
    `正在自动测试 ${modelTestSamples.length} 张，完成后会直接生成报告…`;
  try {
    const response = await api(
      `/api/model-tests/runs/${encodeURIComponent(runId)}/batch`, {
        method: 'POST',
        body: JSON.stringify({
          split,
          conf_thr: Number($('#model-test-conf').value || 0.25),
          iou_threshold: 0.5,
        }),
      });
    if (response.queued && response.job && response.job.id) {
      $('#model-test-state').textContent =
        '验收任务已交给 Vision Worker，正在远程取图并测试…';
      const finished = await waitForVisionJob(response.job.id, 30 * 60 * 1000);
      if (!finished || finished.status !== 'succeeded') {
        throw new Error((finished && finished.error) || 'Vision Worker 验收未完成');
      }
      modelTestBatchReport = (finished.result || {}).report || null;
    } else {
      modelTestBatchReport = response;
    }
    if (!modelTestBatchReport) throw new Error('验收报告为空');
    renderModelTestBatchReport();
    const report = modelTestBatchReport;
    $('#model-test-state').textContent =
      `自动验收完成：${report.correct}/${report.evaluated} 正确，` +
      `准确率 ${modelPercent(report.accuracy)}` +
      (report.failed ? `，另有 ${report.failed} 张未完成` : '');
    $('#model-test-report').scrollIntoView({block: 'start'});
  } catch (error) {
    modelTestBatchReport = null;
    renderModelTestBatchReport();
    $('#model-test-state').textContent = '自动验收失败：' + error.message;
  } finally {
    button.textContent = originalText;
    button.disabled = !modelTestSamples.length;
  }
}

async function predictModelTestSample() {
  const sample = currentModelTestSample();
  const runId = $('#model-test-run').value;
  if (!sample || !sample.has_snapshot_image || !runId) return;
  $('#model-test-output').textContent = '推理中…';
  $('#btn-model-test-predict').disabled = true;
  try {
    const response = await api(
      `/api/model-tests/runs/${encodeURIComponent(runId)}/predict`, {
        method: 'POST',
        body: JSON.stringify({
          sample_id: sample.sample_id,
          split: sample.split,
          conf_thr: Number($('#model-test-conf').value || 0.25),
        }),
      });
    let result = response;
    if (response.queued && response.job && response.job.id) {
      $('#model-test-output').textContent = '已交给 Vision Worker 推理…';
      const finished = await waitForVisionJob(response.job.id);
      if (!finished || finished.status !== 'succeeded') {
        throw new Error((finished && finished.error) || 'Vision Worker 推理未完成');
      }
      result = (finished.result || {}).prediction;
    }
    if (!result) throw new Error('推理结果为空');
    renderModelTestPrediction(result);
  } catch (error) {
    $('#model-test-output').textContent = '推理失败：' + error.message;
    renderModelTestPrediction(null);
    $('#model-test-output').textContent = '推理失败：' + error.message;
  } finally {
    $('#btn-model-test-predict').disabled = false;
  }
}

async function saveModelValidation(status) {
  const runId = $('#model-test-run').value;
  if (!runId) return;
  try {
    await api(`/api/model-tests/runs/${encodeURIComponent(runId)}/validation`, {
      method: 'PUT',
      body: JSON.stringify({
        status,
        notes: $('#model-test-notes').value,
      }),
    });
    $('#model-test-state').textContent =
      status === 'passed' ? '已记录：验收通过' : '已记录：验收不通过';
    await loadModelTesting(runId, false);
  } catch (error) {
    $('#model-test-state').textContent = '保存验收结论失败：' + error.message;
  }
}

function renderModelPackageChoices() {
  const root = $('#model-package-runs');
  root.innerHTML = '';
  MODEL_PACKAGE_TASKS.forEach((taskId, index) => {
    const task = modelTask(taskId);
    const required = MODEL_PACKAGE_REQUIRED_TASKS.includes(taskId);
    const choices = modelTestRuns.filter(
      (run) => run.task_id === taskId && run.validation_status === 'passed' &&
        !(run.evaluation_gaps || []).length);
    const row = document.createElement('div');
    row.className = 'model-package-choice';
    const title = document.createElement('div');
    title.innerHTML = `<span>${index + 1}</span><b>${esc(task.name)}</b>` +
      `<small>${required ? '发布必选' : '可选'}</small>`;
    const select = document.createElement('select');
    select.dataset.packageTask = taskId;
    select.innerHTML = `<option value="">${choices.length ? '请选择通过验收的版本' : '还没有通过验收的版本'}</option>` + choices.map((run) =>
      `<option value="${esc(run.id)}">${esc(run.id)} · ${esc(run.dataset_version_id)}</option>`
    ).join('');
    if (choices.length) select.value = choices[0].id;
    select.onchange = refreshModelPackageBuildState;
    row.append(title, select);
    root.appendChild(row);
  });
  refreshModelPackageBuildState();
}

function refreshModelPackageBuildState() {
  const selected = $$('[data-package-task]').filter((select) => select.value);
  const missing = $$('[data-package-task]').filter((select) =>
    MODEL_PACKAGE_REQUIRED_TASKS.includes(select.dataset.packageTask) && !select.value)
    .map((select) => modelTask(select.dataset.packageTask).name);
  $('#btn-build-model-package').disabled = Boolean(missing.length);
  $('#model-package-state').textContent = missing.length
    ? `还缺发布模型：${missing.join('、')}`
    : `${MODEL_PACKAGE_REQUIRED_TASKS.length} 个模型已齐，当前会打包 ${selected.length} 个模型`;
}

function modelDeploymentStatusLabel(status) {
  return {
    queued: '等待部署', running: '正在部署', succeeded: '部署成功', failed: '部署失败',
  }[status] || status;
}

function renderModelDeploymentState() {
  const data = modelDeploymentData || {};
  const deployments = data.deployments || [];
  const active = deployments.find(
    (item) => ['queued', 'running'].includes(item.status));
  const lastSuccess = deployments.find((item) => item.status === 'succeeded');
  const live = data.live || null;
  const currentPackage = (live && live.package_id) ||
    (lastSuccess && lastSuccess.worker_package_id) || '';
  const liveState = $('#model-worker-live');
  liveState.className = 'model-package-notice';
  liveState.removeAttribute('title');
  if (active) {
    liveState.textContent = `正在部署 ${active.package_id}`;
    liveState.classList.add('deploying');
  } else if (live && live.worker_state === 'running' && currentPackage) {
    liveState.textContent = `Worker 当前：${currentPackage}`;
    liveState.classList.add('running');
  } else if (data.probe_error) {
    liveState.textContent = currentPackage
      ? `最近部署：${currentPackage} · Worker 暂不可达`
      : 'Worker 暂不可达';
    liveState.title = data.probe_error;
    liveState.classList.add('unreachable');
  } else if (currentPackage) {
    liveState.textContent = `最近部署：${currentPackage}`;
    liveState.classList.add('running');
  } else {
    liveState.textContent = '尚无部署记录';
  }

  $('#model-deployment-history').innerHTML = deployments.length
    ? '<div class="model-deployment-history-title">最近部署</div>' +
      deployments.slice(0, 5).map((item) =>
        `<div class="model-deployment-row ${esc(item.status)}">` +
        `<span>${esc(modelDeploymentStatusLabel(item.status))}</span>` +
        `<b>${esc(item.package_id)}</b>` +
        `<small>${esc(item.finished_at || item.started_at || item.created_at || '')}` +
        `${item.error ? ` · ${esc(item.error)}` : ''}</small></div>`
      ).join('')
    : '';
  return {active, currentPackage};
}

function renderModelPackages(packages = modelPackages) {
  modelPackages = packages || [];
  const {active, currentPackage} = renderModelDeploymentState();
  const deploymentBusy = Boolean(active);
  $('#model-package-list').innerHTML = modelPackages.map((item) => {
    const missing = ((item.manifest_json || {}).missing_roles || []).join('、');
    const modelCount = Object.keys(
      (item.manifest_json || {}).models || {}).length;
    const ready = item.status === 'ready';
    const current = item.id === currentPackage;
    const deploying = active && active.package_id === item.id;
    const actionLabel = current ? '当前使用中' : deploying ? '部署中…' :
      currentPackage ? '部署此版本' : '部署到 Worker';
    return `<div class="model-package-item${current ? ' current' : ''}"><div><b>${esc(item.id)}</b>` +
      `<span class="${current ? 'active' : ready ? 'ready' : 'incomplete'}">` +
      `${current ? '已上线' : ready ? '可发布' : '不完整'}</span>` +
      `${missing
        ? `<small>缺少 ${esc(missing)}</small>`
        : `<small>包含 ${modelCount} 个模型及完整追溯信息</small>`}</div>` +
      `<div class="model-package-actions">` +
      `<a href="/api/model-packages/${encodeURIComponent(item.id)}/archive">下载 ZIP</a>` +
      `<button type="button" class="model-package-deploy${current ? '' : ' primary'}" ` +
      `data-package-id="${esc(item.id)}" ` +
      `${!ready || current || deploymentBusy ? 'disabled' : ''}>${actionLabel}</button>` +
      `</div></div>`;
  }).join('') || '<div class="muted small">还没有模型包</div>';
  $$('.model-package-deploy[data-package-id]').forEach((button) => {
    button.onclick = () => deployModelPackage(button.dataset.packageId);
  });
}

async function loadModelDeployments(probe = false) {
  try {
    modelDeploymentData = await api(
      `/api/model-deployments${probe ? '?probe=true' : ''}`);
    renderModelPackages();
    const active = (modelDeploymentData.deployments || []).some(
      (item) => ['queued', 'running'].includes(item.status));
    if (modelDeploymentPollTimer) clearTimeout(modelDeploymentPollTimer);
    modelDeploymentPollTimer = active
      ? setTimeout(() => loadModelDeployments(false), 1500) : null;
  } catch (error) {
    $('#model-worker-live').textContent = '读取 Worker 部署状态失败';
    $('#model-worker-live').title = error.message;
  }
}

async function deployModelPackage(packageId) {
  const confirmed = window.confirm(
    `确定把 ${packageId} 部署到 MacBook Pro Worker 吗？\n\n` +
    '系统会校验完整模型包、切换版本并重启 Worker；启动失败会自动回滚。');
  if (!confirmed) return;
  $('#model-package-state').textContent = `正在创建 ${packageId} 的部署任务…`;
  try {
    await api(
      `/api/model-packages/${encodeURIComponent(packageId)}/deploy-worker`,
      {method: 'POST'});
    $('#model-package-state').textContent =
      `已提交 ${packageId}，正在上传、校验并切换 Worker…`;
    await loadModelDeployments(false);
  } catch (error) {
    $('#model-package-state').textContent = '部署失败：' + error.message;
    await loadModelDeployments(false);
  }
}

async function buildModelPackage() {
  const runIds = $$('[data-package-task]').map((select) => select.value).filter(Boolean);
  const selectedTasks = new Set($$('[data-package-task]')
    .filter((select) => select.value)
    .map((select) => select.dataset.packageTask));
  const missing = MODEL_PACKAGE_REQUIRED_TASKS.filter(
    (taskId) => !selectedTasks.has(taskId));
  if (missing.length) {
    $('#model-package-state').textContent =
      `${MODEL_PACKAGE_REQUIRED_TASKS.length} 个模型全部通过验收并选好后才能生成`;
    return;
  }
  $('#model-package-state').textContent = '正在校验哈希并组装…';
  try {
    const result = await api('/api/model-packages', {
      method: 'POST', body: JSON.stringify({run_ids: runIds}),
    });
    if (result.queued && result.job && result.job.id) {
      $('#model-package-state').textContent =
        `已交给 Vision Worker 组装 ${result.id}，正在生成校验值和模型包…`;
      const finished = await waitForVisionJob(
        result.job.id, 30 * 60 * 1000);
      if (!finished || finished.status !== 'succeeded') {
        throw new Error((finished && finished.error) || 'Vision Worker 组包未完成');
      }
    }
    $('#model-package-state').textContent = result.status === 'ready'
      ? `已生成可部署版本：${result.id}（尚未部署）`
      : `未达到发布条件：${Object.keys(result.evaluation_gaps || {}).length ? '固定测试集覆盖不足' : '模型不完整'}`;
    const packages = await api('/api/model-packages');
    renderModelPackages(packages.packages);
  } catch (error) {
    $('#model-package-state').textContent = '组包失败：' + error.message;
  }
}

async function loadModelTesting(preferredRunId = '', loadSamples = true) {
  try {
    const [runsData, packagesData, deploymentData] = await Promise.all([
      api('/api/model-tests/runs'), api('/api/model-packages'),
      api('/api/model-deployments'),
      ensureCandidateHeroCatalog().catch(() => []),
    ]);
    modelTestRuns = runsData.runs || [];
    modelPackages = packagesData.packages || [];
    modelDeploymentData = deploymentData;
    const select = $('#model-test-run');
    const previous = preferredRunId || select.value;
    select.innerHTML = '<option value="">请选择训练结果</option>' +
      modelTestRuns.map((run) =>
        `<option value="${esc(run.id)}">${esc(modelTask(run.task_id).name)} · ` +
        `${esc(run.id)} · ${esc(modelValidationLabel(run.validation_status))}</option>`
      ).join('');
    if (modelTestRuns.some((run) => run.id === previous)) select.value = previous;
    else if (modelTestRuns.length) select.value = modelTestRuns[0].id;
    renderModelPackageChoices();
    renderModelPackages();
    loadModelDeployments(true);
    renderModelTestSummary();
    if (loadSamples) await loadModelTestSamples();
  } catch (error) {
    $('#model-test-state').textContent = '模型测试页加载失败：' + error.message;
  }
}

function bindModelTesting() {
  $('#model-test-run').onchange = loadModelTestSamples;
  $('#model-test-split').onchange = loadModelTestSamples;
  $('#btn-model-test-load').onclick = loadModelTestSamples;
  $('#btn-model-test-batch').onclick = runModelTestBatch;
  $('#btn-model-test-prev').onclick = () => moveModelTestSample(-1);
  $('#btn-model-test-next').onclick = () => moveModelTestSample(1);
  $('#btn-model-test-predict').onclick = predictModelTestSample;
  $('#btn-model-test-pass').onclick = () => saveModelValidation('passed');
  $('#btn-model-test-fail').onclick = () => saveModelValidation('failed');
  $('#btn-build-model-package').onclick = buildModelPackage;
  $('#model-test-image').onload = renderModelTestDetections;
  window.addEventListener('resize', renderModelTestDetections);
}

// ---------- 训练与模型 ----------
const TRAINING_STATUS_LABELS = {
  queued: '排队中',
  running: '训练中',
  succeeded: '训练成功',
  failed: '训练失败',
  cancelled: '已取消',
  interrupted: '服务重启中断',
};

const TRAINING_TASK_GROUPS = [
  {
    id: 'timeline',
    title: '对局解析核心模型',
    description: '负责找到一局的开始、过程、模式和结算位置。',
    taskIds: [
      'match_flow', 'hero_select', 'match_mode', 'result_mode', 'result_detector',
    ],
  },
  {
    id: 'heroes',
    title: '英雄阵容增强模型',
    description: '只在关键画面按需运行，负责头像、英雄、主播本人和结算挂机状态。',
    taskIds: [
      'hero_avatar_detector', 'hero_identity', 'player_position', 'afk_status',
    ],
  },
];
const openTrainingQualityTasks = new Set();

function trainingNumber(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

function trainingCountMetrics(task) {
  const counts = task.counts || {};
  if (task.id === 'match_flow') {
    const labels = counts.by_label || {};
    return [
      ['有效图片', counts.total], ['对局流程', labels.match_flow],
      ['非对局', labels.not_match_flow], ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'hero_select') {
    const labels = counts.by_label || {};
    return [
      ['有效图片', counts.total], ['非选择', labels.not_select],
      ['3V3', labels.select_3v3], ['大乱斗', labels.select_aram],
      ['5V5', labels.select_5v5], ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'match_mode') {
    const labels = counts.by_label || {};
    return [
      ['有效图片', counts.total], ['3V3', labels['3v3']],
      ['大乱斗', labels.aram], ['5V5', labels['5v5']],
      ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'result_mode') {
    const labels = counts.by_label || {};
    return [
      ['完整结算图', counts.total], ['3V3', labels['3v3']],
      ['大乱斗', labels.aram], ['5V5', labels['5v5']],
      ['闪电战', labels.blitz], ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'result_detector') {
    return [
      ['有效图片', counts.total], ['结算正样本', counts.positive],
      ['负样本', counts.negative], ['计分板难例', counts.hard_negative],
      ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'hero_avatar_detector') {
    const screens = counts.by_screen_type || {};
    return [
      ['完整阵容图', counts.total], ['头像框', counts.boxes],
      ['HUD', screens.gameplay_hud], ['积分板', screens.scoreboard],
      ['结算界面', screens.result_page], ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'hero_identity') {
    const screens = counts.by_screen_type || {};
    return [
      ['可读头像', counts.total], ['英雄种类', counts.classes],
      ['HUD', screens.gameplay_hud], ['积分板', screens.scoreboard],
      ['结算界面', screens.result_page], ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'player_position') {
    const screens = counts.by_screen_type || {};
    return [
      ['有效面板图', counts.total], ['位置类别', counts.classes],
      ['积分板', screens.scoreboard], ['结算界面', screens.result_page],
      ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'afk_status') {
    const labels = counts.by_label || {};
    return [
      ['结算玩家区域', counts.total], ['挂机', labels.afk],
      ['正常', labels.active], ['来源视频', counts.videos],
    ];
  }
  if (task.id === 'result_mode') {
    const states = counts.by_render_state || {};
    return `清晰 ${trainingNumber(states.clear)} 张 · ` +
      `半透明 ${trainingNumber(states.translucent)} 张 · ` +
      `有遮挡 ${trainingNumber(counts.occluded)} 张`;
  }
  if (task.id === 'screen_state') {
    const labels = counts.by_label || {};
    return Object.entries(labels).map(([label, value]) => [label, value]);
  }
  if (task.id === 'bp_review') {
    const labels = counts.by_label || {};
    return Object.entries(labels).map(([label, value]) => [label, value]);
  }
  if (task.id === 'key_screen_review') {
    const labels = counts.by_label || {};
    return Object.entries(labels).map(([label, value]) => [label, value]);
  }
  return [
    ['有效图片', counts.total], ['正样本', counts.positive],
    ['负样本', counts.negative], ['来源视频', counts.videos],
  ];
}

function trainingSupplementalText(task) {
  const counts = task.counts || {};
  if (task.id === 'hero_avatar_detector') {
    const teams = counts.by_team_size || {};
    return `3 人阵容 ${trainingNumber(teams['3'])} 张 · ` +
      `5 人阵容 ${trainingNumber(teams['5'])} 张`;
  }
  if (task.id === 'hero_identity') {
    return `小于 24px 的头像 ${trainingNumber(counts.under_24px)} 个 · ` +
      `小于 48px 的头像 ${trainingNumber(counts.under_48px)} 个`;
  }
  if (task.id === 'player_position') {
    return `HUD ${trainingNumber(counts.excluded_hud)} 张由代码规则处理、不参与训练 · ` +
      `本人看不清 ${trainingNumber(counts.excluded_unreadable)} 张已排除`;
  }
  if (task.id === 'afk_status') {
    return `积分板标记 ${trainingNumber(counts.excluded_scoreboard)} 个仅作辅助审查，` +
      '不会进入挂机训练集';
  }
  return trainingSnapshotNote(task.id);
}

function trainingInputText(task) {
  const width = Number(task.input_width || task.imgsz || 0);
  const height = Number(task.input_height || task.imgsz || 0);
  const kind = task.kind === 'detect' ? '目标检测' : '画面分类';
  return `${kind} · ${width} × ${height}`;
}

function trainingDatasetDelta(task) {
  const delta = task.dataset_delta;
  const latestRunId = String(task.latest_successful_run_id || '');
  const baselineStale = Boolean(
    delta && latestRunId && String(delta.run_id || '') !== latestRunId);
  if (task.stats_refreshing && (!delta || baselineStale)) {
    return '<div class="training-dataset-delta empty">' +
      '<span>训练数据变化</span><b>正在更新</b>' +
      '<small>正在按最新成功模型重新计算基线</small></div>';
  }
  if (!delta) {
    return '<div class="training-dataset-delta empty">' +
      '<span>训练数据变化</span><b>尚无成功版本</b>' +
      '<small>首次训练后开始显示新增数据</small></div>';
  }
  const corrections = Number(delta.changed || 0);
  const removed = Number(delta.removed || 0);
  const newCount = Number(delta.new || 0);
  const details = [
    `上次 ${trainingNumber(delta.baseline_total)}`,
    `当前 ${trainingNumber(delta.current_total)}`,
    `新视频 ${trainingNumber(delta.new_videos)}`,
  ];
  if (corrections) details.push(`改标 ${trainingNumber(corrections)}`);
  if (removed) details.push(`移除 ${trainingNumber(removed)}`);
  if (task.stats_refreshing) details.push('新标注统计中');
  const byLabel = Object.entries(delta.new_by_label || {})
    .map(([label, count]) =>
      `${modelLabel(task.id, label)} ${trainingNumber(count)}`)
    .join(' · ');
  return `<div class="training-dataset-delta ${newCount ? 'has-new' : ''}"` +
    `${byLabel ? ` title="${esc(byLabel)}"` : ''}>` +
    `<span>距上次训练</span><b>${newCount
      ? `新增 ${trainingNumber(newCount)}` : '暂无新增'}</b>` +
    `<small>${esc(details.join(' · '))}</small></div>`;
}

function trainingSnapshotNote(taskId) {
  if (taskId === 'screen_state') {
    return '训练快照会自动限制大量普通对局帧，保留稀有状态和人工确认候选。';
  }
  if (taskId === 'key_screen_review') {
    return '训练快照会优先保留易混淆画面，并自动平衡大量普通画面。';
  }
  if (taskId === 'result_detector') {
    return '训练快照最多取 1500 张负样本，优先保留计分板 hard negative。';
  }
  return '';
}

function renderTrainingOverview(tasks, runs) {
  const root = $('#training-overview');
  const ready = tasks.filter((task) => task.ready).length;
  const heroTasks = tasks.filter((task) =>
    MODEL_PACKAGE_HERO_TASKS.includes(task.id)).length;
  const coreTasks = tasks.filter((task) =>
    MODEL_PACKAGE_CORE_TASKS.includes(task.id)).length;
  const sourceVideos = Math.max(
    0, ...tasks.map((task) => Number((task.counts || {}).videos || 0)));
  const active = (runs.runs || []).find((run) => run.id === runs.active_run_id);
  const activeTask = active ? modelTask(active.task_id).name : '没有排队任务';
  const activeDetail = active
    ? `${Math.round(Number(active.progress || 0) * 100)}% · ` +
      `${active.current_epoch || 0}/${active.epochs} epochs`
    : '可以选择任一已就绪模型加入 Worker 队列';
  root.innerHTML = `
    <div><span>训练模型</span><b>${tasks.length}</b><small>${coreTasks} 个核心 + ${heroTasks} 个英雄增强</small></div>
    <div><span>可以训练</span><b>${ready}/${tasks.length}</b><small>${ready === tasks.length ? '全部达到最低结构要求' : '仍有任务缺少数据'}</small></div>
    <div><span>数据来源</span><b>${trainingNumber(sourceVideos)}</b><small>个不同视频，按视频隔离切分</small></div>
    <div><span>当前任务</span><b>${esc(activeTask)}</b><small>${esc(activeDetail)}</small></div>`;
}

function trainingTaskCard(task) {
  const reasons = task.blocking_reasons || [];
  const warnings = task.quality_warnings || [];
  const metrics = trainingCountMetrics(task);
  const supplemental = trainingSupplementalText(task);
  const waitingForStats = task.stats_refreshing &&
    !Object.keys(task.counts || {}).length;
  const readinessLabel = waitingForStats
    ? '统计中' : (task.ready ? '可训练' : '数据不足');
  return `
    <article class="training-task-card ${task.ready ? 'ready' : ''}"
      data-task-id="${esc(task.id)}">
      <div class="training-task-heading">
        <div><h3>${esc(task.name)}</h3><span>${esc(trainingInputText(task))}</span></div>
        <span class="training-readiness ${task.ready ? 'ready' : 'blocked'}">${readinessLabel}</span>
      </div>
      <p class="training-task-description">${esc(task.description)}</p>
      <div class="training-data-grid">${metrics.map(([label, value]) => `
        <div><span>${esc(label)}</span><b>${trainingNumber(value)}</b></div>`).join('')}</div>
      ${supplemental ? `<p class="training-supplemental">${esc(supplemental)}</p>` : ''}
      ${trainingDatasetDelta(task)}
      <div class="training-recommendation"><span>建议</span>${esc(task.recommended)}</div>
      ${reasons.length
        ? `<div class="blocking">暂不能训练：${esc(reasons.join('；'))}</div>`
        : warnings.length
          ? `<details class="training-quality"${openTrainingQualityTasks.has(task.id) ? ' open' : ''}><summary>数据提醒（${warnings.length}）</summary><p>${esc(warnings.join('；'))}</p></details>`
          : '<div class="small status-done">数据结构达到当前建议量</div>'}
      <button class="primary training-start" data-task-id="${esc(task.id)}"
        ${!task.ready ? 'disabled' : ''}>
        用当前数据加入训练队列（${task.epochs} epochs）
      </button>
    </article>`;
}

function renderTrainingTasks(tasks) {
  const grid = $('#training-task-grid');
  $$('.training-task-card[data-task-id]').forEach((card) => {
    const details = card.querySelector('.training-quality');
    if (!details) return;
    if (details.open) openTrainingQualityTasks.add(card.dataset.taskId);
    else openTrainingQualityTasks.delete(card.dataset.taskId);
  });
  grid.innerHTML = '';
  const rendered = new Set();
  TRAINING_TASK_GROUPS.forEach((group) => {
    const groupTasks = group.taskIds
      .map((taskId) => tasks.find((task) => task.id === taskId))
      .filter(Boolean);
    if (!groupTasks.length) return;
    groupTasks.forEach((task) => rendered.add(task.id));
    grid.insertAdjacentHTML('beforeend', `
      <section class="training-task-group" data-group="${esc(group.id)}">
        <div class="training-group-heading"><div><h3>${esc(group.title)}</h3>
          <p>${esc(group.description)}</p></div><span>${groupTasks.length} 个模型</span></div>
        <div class="training-task-cards">${groupTasks.map((task) =>
          trainingTaskCard(task)).join('')}</div>
      </section>`);
  });
  const remaining = tasks.filter((task) => !rendered.has(task.id));
  if (remaining.length) {
    grid.insertAdjacentHTML('beforeend', `<section class="training-task-group">
      <div class="training-group-heading"><div><h3>其他训练任务</h3></div></div>
      <div class="training-task-cards">${remaining.map((task) =>
        trainingTaskCard(task)).join('')}</div></section>`);
  }
  $$('.training-start').forEach((button) => {
    button.onclick = () => startTraining(button.dataset.taskId);
  });
}

function trainingMetricItems(metrics) {
  const names = {
    'metrics/precision(B)': '精确率',
    'metrics/recall(B)': '召回率',
    'metrics/mAP50(B)': 'mAP50',
    'metrics/mAP50-95(B)': 'mAP50–95',
    'metrics/accuracy_top1': 'Top-1',
    'metrics/accuracy_top5': 'Top-5',
  };
  return Object.entries(metrics || {}).slice(0, 4).map(([key, value]) => ({
    label: names[key] || key.replace(/^metrics\//, ''),
    value: Number(value),
  })).filter((item) => Number.isFinite(item.value));
}

function trainingMetricsHtml(metrics) {
  const items = trainingMetricItems(metrics);
  if (!items.length) return '<span class="muted">--</span>';
  return `<div class="training-metrics">${items.map((item) =>
    `<span><small>${esc(item.label)}</small><b>${item.value.toFixed(3)}</b></span>`
  ).join('')}</div>`;
}

function trainingArtifactHtml(run) {
  const path = String(run.artifact_path || '');
  if (!path) return '<span class="muted">--</span>';
  const filename = path.split('/').filter(Boolean).pop() || path;
  const published = run.published_path
    ? `<small class="status-done">已有本机测试版本</small>` : '';
  return `<div class="training-artifact" title="${esc(path)}">` +
    `<code>${esc(filename)}</code>${published}</div>`;
}

function renderTrainingRuns(data) {
  const activeRunId = data.active_run_id;
  const tbody = $('#training-runs-table tbody');
  tbody.innerHTML = '';
  (data.runs || []).forEach((run) => {
    const percent = Math.round(Number(run.progress || 0) * 100);
    const statusLabel = TRAINING_STATUS_LABELS[run.status] || run.status;
    const canCancel = ['queued', 'running'].includes(run.status);
    const canTest = run.status === 'succeeded' && run.artifact_path;
    const taskName = modelTask(run.task_id).name;
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td><div class="training-model-cell"><b>${esc(taskName)}</b>
          <code title="${esc(run.id)}">${esc(run.id)}</code></div></td>
        <td><code class="training-snapshot" title="${esc(run.dataset_version_id)}">${esc(run.dataset_version_id)}</code></td>
        <td><span class="training-status training-status-${esc(run.status)}">${esc(statusLabel)}</span>
          ${run.error ? `<br><span class="small">${esc(run.error)}</span>` : ''}</td>
        <td><div class="training-progress-row"><span class="training-progress"><div style="width:${percent}%"></div></span>
          <b>${percent}%</b></div><small class="muted">${run.current_epoch || 0}/${run.epochs} epochs</small></td>
        <td>${trainingMetricsHtml(run.metrics_json)}</td>
        <td>${trainingArtifactHtml(run)}</td>
        <td>
          <div class="training-row-actions"><button class="training-log-open" data-run-id="${esc(run.id)}">日志</button>
          ${canCancel
            ? `<button class="training-cancel" data-run-id="${esc(run.id)}">取消</button>`
            : ''}
          ${canTest
            ? `<button class="training-test-run" data-run-id="${esc(run.id)}">进入模型验收</button>`
            : ''}</div>
        </td>
      </tr>`);
  });
  if (!(data.runs || []).length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">尚无训练记录</td></tr>';
  }
  $('#training-runs-count').textContent = `${(data.runs || []).length} 条记录`;
  $$('.training-log-open').forEach((button) => {
    button.onclick = () => loadTrainingLog(button.dataset.runId);
  });
  $$('.training-cancel').forEach((button) => {
    button.onclick = () => cancelTraining(button.dataset.runId);
  });
  $$('.training-test-run').forEach((button) => {
    button.onclick = async () => {
      const nav = $('.nav-item[data-view="model-tests"]');
      activateNav(nav);
      await loadModelTesting(button.dataset.runId);
    };
  });
  $('#training-global-state').textContent = activeRunId
    ? `队列正在处理：${activeRunId}` : '当前没有排队或运行中的训练任务';
}

function renderVisionWorkers(data) {
  const workers = data.workers || [];
  const jobs = data.jobs || [];
  $('#vision-worker-count').textContent = `${workers.length} 台`;
  const root = $('#vision-worker-list');
  if (!workers.length) {
    root.innerHTML = '<div class="empty-state compact">还没有 Vision Worker 连接。NAS 页面不会自行执行训练。</div>';
    return;
  }
  root.innerHTML = workers.map((worker) => {
    const job = jobs.find((item) => item.id === worker.active_job_id);
    const progress = job ? Math.round(Number(job.progress || 0) * 100) : 0;
    const capabilities = (worker.capabilities || []).map((kind) => ({
      train_model: '训练', model_prefill: '模型预填',
      candidate_metadata: '素材整理', validate_model: '批量验收',
      package_models: '模型打包',
    }[kind] || kind)).join(' · ');
    return `<article class="vision-worker-card ${worker.enabled ? '' : 'paused'}">
      <div><b>${esc(worker.display_name)}</b><span>${worker.enabled ? (worker.state === 'busy' ? '工作中' : '可领取任务') : '已暂停'}</span></div>
      <small>${esc(capabilities || '未声明能力')}</small>
      <small>最后心跳 ${esc(worker.last_seen_at || '--')}</small>
      ${job ? `<div class="vision-worker-job"><span>${esc(job.stage || job.kind)}</span><b>${progress}%</b></div><small>${esc(job.detail || '')}</small>` : ''}
      <button class="vision-worker-toggle" data-worker-id="${esc(worker.id)}" data-enabled="${worker.enabled ? '1' : '0'}">${worker.enabled ? '暂停领取任务' : '恢复领取任务'}</button>
    </article>`;
  }).join('');
  $$('.vision-worker-toggle').forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        await api(`/api/vision-workers/${encodeURIComponent(button.dataset.workerId)}`, {
          method: 'PATCH',
          body: JSON.stringify({ enabled: button.dataset.enabled !== '1' }),
        });
        loadTrainingDashboard();
      } catch (error) {
        $('#training-global-state').textContent = 'Worker 状态更新失败：' + error.message;
        button.disabled = false;
      }
    };
  });
}

async function loadTrainingDashboard() {
  try {
    const [tasks, runs, workers] = await Promise.all([
      api('/api/training/tasks'),
      api('/api/training/runs'),
      api('/api/vision-workers'),
    ]);
    const latestSuccessfulRuns = new Map();
    (runs.runs || []).forEach((run) => {
      if (run.status === 'succeeded' && !latestSuccessfulRuns.has(run.task_id)) {
        latestSuccessfulRuns.set(run.task_id, run.id);
      }
    });
    tasks.forEach((task) => {
      task.latest_successful_run_id = latestSuccessfulRuns.get(task.id) || '';
    });
    renderTrainingOverview(tasks, runs);
    renderVisionWorkers(workers);
    renderTrainingTasks(tasks);
    renderTrainingRuns(runs);
    const statsRefreshing = tasks.some((task) => task.stats_refreshing);
    const statsError = tasks.find((task) => task.stats_error)?.stats_error;
    if (!runs.active_run_id && statsRefreshing) {
      $('#training-global-state').textContent = '正在后台更新训练数据统计…';
    } else if (!runs.active_run_id && statsError) {
      $('#training-global-state').textContent = '训练数据统计失败：' + statsError;
    }
    if ((runs.active_run_id || statsRefreshing) && !trainingPollTimer) {
      trainingPollTimer = setInterval(loadTrainingDashboard, 2000);
    } else if (!runs.active_run_id && !statsRefreshing && trainingPollTimer) {
      clearInterval(trainingPollTimer);
      trainingPollTimer = null;
    }
  } catch (error) {
    $('#training-global-state').textContent = '加载失败：' + error.message;
  }
}

async function startTraining(taskId) {
  const taskCard = $(`.training-start[data-task-id="${taskId}"]`);
  if (!taskCard) return;
  const confirmed = window.confirm(
    '现在会冻结当前标注数据并加入 Vision Worker 队列。之后新增的标注不会混进这一轮，' +
    '需要再点一次训练才会生成新版本。继续吗？');
  if (!confirmed) return;
  $('#training-global-state').textContent = '正在冻结数据集快照…';
  $$('.training-start').forEach((button) => { button.disabled = true; });
  try {
    const run = await api('/api/training/start', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
    });
    $('#training-global-state').textContent =
      `已排队 ${run.id}，数据快照 ${run.dataset_version_id}`;
    if (!trainingPollTimer) {
      trainingPollTimer = setInterval(loadTrainingDashboard, 2000);
    }
    loadTrainingDashboard();
  } catch (error) {
    $('#training-global-state').textContent = '无法训练：' + error.message;
    loadTrainingDashboard();
  }
}

async function cancelTraining(runId) {
  if (!window.confirm('确定停止这一轮训练吗？数据快照和训练记录会保留。')) return;
  try {
    await api(`/api/training/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST', body: '{}',
    });
    $('#training-global-state').textContent = '已请求停止训练…';
    loadTrainingDashboard();
  } catch (error) {
    $('#training-global-state').textContent = '取消失败：' + error.message;
  }
}

async function loadTrainingLog(runId) {
  trainingLogRunId = runId;
  $('#training-log-run').textContent = `${runId} · 最近 200 行`;
  $('#training-log').textContent = '正在读取日志…';
  const dialog = $('#training-log-dialog');
  if (!dialog.open) dialog.showModal();
  try {
    const result = await api(
      `/api/training/runs/${encodeURIComponent(runId)}/log?tail=200`);
    $('#training-log').textContent = result.log || '暂无日志';
  } catch (error) {
    $('#training-log').textContent = '日志加载失败：' + error.message;
  }
}

$('#btn-training-refresh').onclick = loadTrainingDashboard;
$('#btn-training-log-refresh').onclick = () => {
  if (trainingLogRunId) loadTrainingLog(trainingLogRunId);
};
$('#btn-training-log-close').onclick = () => {
  $('#training-log-dialog').close();
};

// ---------- 工具 ----------
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
