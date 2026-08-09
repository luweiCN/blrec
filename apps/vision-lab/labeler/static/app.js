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
let candidateQueue = [];
let candidateIndex = 0;
let candidateDraft = null;
let candidateBoxes = [];
let candidateDrawStart = null;
let candidateSyncTimer = null;
let candidateHeroCatalog = [];
let candidateHeroCatalogPromise = null;
let candidateHeroLineup = null;
let candidateHeroDraft = new Map();
let candidateHeroDirty = false;
let candidateHeroLoading = false;
let candidateHeroLoadToken = 0;
let candidateHeroPickerSlot = null;
let candidateHeroPlayerSlot = null;
let candidateHeroTeamSizeExplicit = false;
let candidateHeroTeamSizeOverride = null;
let candidateHeroDrawMode = false;
let candidateHeroEdit = null;
let modelTestRuns = [];
let modelTestSamples = [];
let modelTestIndex = 0;
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

// ---------- 初始化 ----------
async function init() {
  CFG = await api('/api/config');
  buildSelects();
  buildBoxToolbar();
  buildStrategySelect();
  loadVideos();
  loadStats();
  loadDatasets();
  loadPairs();
  bindNav();
  bindShortcuts();
  bindBpReview();
  bindKeyScreenReview();
  bindCandidateReview();
  bindModelTesting();
  setTask(state.task, false);
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
      if (btn.dataset.view === 'candidates') loadCandidateReview();
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
    labels: { '3v3': '3V3', aram: '大乱斗', '5v5': '5V5', unreadable: '看不出模式' },
  },
  {
    key: 'hero_select_label', suggestion: 'hero_select', title: '3. 是否是英雄选择界面',
    help: '匹配接受／拒绝不是英雄选择。能看出英雄选择时，同时标出模式。',
    labels: {
      not_select: '不是英雄选择', select_3v3: '3V3 英雄选择',
      select_aram: '大乱斗英雄选择', select_5v5: '5V5 英雄选择',
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

const CANDIDATE_SUGGESTION_TITLES = {
  match_flow: '对局流程',
  match_mode: '对局模式',
  hero_select: '英雄选择',
  result_panel: '结算面板',
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

function currentCandidate() {
  return candidateQueue[candidateIndex] || null;
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
    return Object.fromEntries(TRAINING_REVIEW_FIELDS.flatMap((field) => {
      const value = stored[field.key];
      return Object.prototype.hasOwnProperty.call(field.labels, value)
        ? [[field.key, value]] : [];
    }));
  } catch (_error) {
    return {};
  }
}

function cacheCandidateReviewLabels(draft) {
  const values = Object.fromEntries(TRAINING_REVIEW_FIELDS.flatMap((field) => {
    const value = draft[field.key];
    return Object.prototype.hasOwnProperty.call(field.labels, value)
      ? [[field.key, value]] : [];
  }));
  try {
    window.localStorage.setItem(
      CANDIDATE_REVIEW_DEFAULTS_STORAGE_KEY, JSON.stringify(values));
  } catch (_error) {
    // 浏览器禁止本地存储时仍可继续打标，只是不跨图片沿用。
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

function candidateDefaultDraft(item) {
  const hasHumanLabels = TRAINING_REVIEW_FIELDS.some(
    (field) => Boolean(item[field.key]));
  const cached = hasHumanLabels ? {} : candidateCachedReviewLabels();
  const resultHeroCountMode = hasHumanLabels
    ? null : candidateResultHeroCountMode(item);
  const draft = {};
  TRAINING_REVIEW_FIELDS.forEach((field) => {
    const resultMode = field.key === 'match_mode_label'
      ? resultHeroCountMode : null;
    draft[field.key] = item[field.key] || resultMode || cached[field.key] ||
      candidateSuggestedValue(item, field);
  });
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
  draft.hero_layout_label = item.hero_layout_label || null;
  if (!draft.hero_layout_label) {
    if (draft.result_panel_label === 'result_panel') {
      draft.hero_layout_label = 'result_page';
    } else {
      for (const source of item.sources || []) {
        const metadata = source.metadata || {};
        const screen = metadata.screen_type || metadata.stage_class || '';
        if (['scoreboard', 'death_scoreboard'].includes(screen)) {
          draft.hero_layout_label = 'scoreboard';
          break;
        }
        if (['gameplay', 'gameplay_hud', 'in_match'].includes(screen)) {
          draft.hero_layout_label = 'gameplay_hud';
          break;
        }
      }
    }
  }
  draft.hero_layout_label ||= 'none';
  draft.ocr_usable = item.ocr_usable || 'yes';
  draft.result_occlusion = item.result_occlusion || 'none';
  draft.occluder_types = Array.isArray(item.occluder_types)
    ? [...item.occluder_types] : [];
  return draft;
}

function candidateSuggestedResultBox(item) {
  if (item.boxes && item.boxes.result_panel) {
    return {...item.boxes.result_panel, type: 'result_panel'};
  }
  for (const source of item.sources || []) {
    const boxes = source.metadata && source.metadata.suggested_boxes || [];
    const box = boxes.find((value) =>
      ['result_panel', ''].includes(value.type || value.box_type || ''));
    if (box) return {...box, type: 'result_panel'};
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
  if (event.button !== 0 || candidateHeroLoading) return;
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
  edit.moved ||= Math.hypot(dx, dy) >= 3;
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

async function finishCandidateHeroEdit(event, node) {
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
  const saveTemplate = candidateHeroLayoutComplete();
  const saved = await persistCandidateHeroLayout(candidateHeroLineup.slots, {
    saveTemplate: saveTemplate,
  });
  if (!saved) {
    candidateHeroLineup.slots = edit.originalSlots;
    renderCandidateHeroLineup();
    return;
  }
  $('#candidate-save-state').textContent = saveTemplate
    ? '英雄圆框的位置和大小已更新，并同步到该主播的布局缓存'
    : '英雄圆框的位置和大小已更新';
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
    const isPlayer = candidateHeroLineup.screen_type === 'result_page' &&
      candidateHeroPlayerSlot === key;
    node.classList.toggle('player', isPlayer);
    applyCandidateHeroCrop(node, box);
    const label = candidateHeroDraft.get(key) || '';
    const hero = candidateHeroDisplay(label);
    node.title = hero
      ? `${slot.side === 'left' ? '左队' : '右队'} ${slot.slot}：${hero.name}`
      : `${slot.side === 'left' ? '左队' : '右队'} ${slot.slot}：点击选择英雄`;
    if (isPlayer) node.title += '（主播本人）';
    const tag = document.createElement('span');
    tag.className = 'candidate-hero-circle-label';
    tag.textContent = `${slot.side === 'left' ? '左' : '右'}${slot.slot}` +
      (isPlayer ? ' · 本人' : '');
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
  if (!suggestions.childElementCount) suggestions.textContent = '没有模型建议';
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
}

function candidateHeroKey(side, slot) {
  return `${side}:${slot}`;
}

function candidateHeroPlayerKey(lineup) {
  if (!lineup || !lineup.player_side || !lineup.player_slot) return null;
  return candidateHeroKey(lineup.player_side, lineup.player_slot);
}

function candidateHeroPlayerPosition() {
  if (!candidateHeroPlayerSlot) return null;
  const slot = candidateHeroSlot(candidateHeroPlayerSlot);
  return slot ? {side: slot.side, slot: slot.slot} : null;
}

function candidateHeroByLabel(label) {
  return candidateHeroCatalog.find((hero) => hero.label === label) || null;
}

function candidateHeroContext(item) {
  if (!item || !candidateDraft) return null;
  const screenType = candidateDraft.hero_layout_label;
  if (!CANDIDATE_HERO_SCREEN_TYPES.has(screenType)) return null;
  const selectedMode = candidateDraft.match_mode_label || '';
  const teamSize = candidateHeroTeamSizeExplicit
    ? candidateHeroTeamSizeOverride
    : selectedMode === '5v5' ? 5
      : ['3v3', 'aram'].includes(selectedMode) ? 3
        : candidateHeroLineup && candidateHeroLineup.screen_type === screenType
          ? candidateHeroLineup.team_size : null;
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
  candidateHeroLineup = null;
  candidateHeroDraft = new Map();
  candidateHeroPlayerSlot = null;
  candidateHeroDirty = false;
  candidateHeroLoading = false;
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

function candidateHeroLayoutComplete() {
  return Boolean(
    candidateHeroLineup &&
    candidateHeroLineup.team_size &&
    candidateHeroLineup.slots.length === candidateHeroLineup.team_size * 2
  );
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

  const originalCenter = candidateHeroCropCenter(selected.crop);
  const editedCenter = candidateHeroCropCenter(editedCrop);
  if (['result_page', 'scoreboard'].includes(screenType)) {
    result.forEach((slot) => {
      const center = candidateHeroCropCenter(slot.crop);
      if (slot.side === selected.side) center.x = editedCenter.x;
      if (slot.slot === selected.slot) center.y = editedCenter.y;
      slot.crop = candidateHeroCropAtCenter(center, slot.crop);
    });
  } else if (screenType === 'gameplay_hud') {
    const delta = {
      x: editedCenter.x - originalCenter.x,
      y: editedCenter.y - originalCenter.y,
    };
    result.forEach((slot) => {
      if (slot.side !== selected.side) return;
      const center = candidateHeroCropCenter(slot.crop);
      slot.crop = candidateHeroCropAtCenter({
        x: center.x + delta.x,
        y: center.y + delta.y,
      }, slot.crop);
    });
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
    const left1 = findSlot('left', 1);
    const left2 = findSlot('left', 2);
    if (!left1 || !left2) return result;
    const firstCenter = candidateHeroCropCenter(left1.crop);
    const secondCenter = candidateHeroCropCenter(left2.crop);
    const step = {
      x: secondCenter.x - firstCenter.x,
      y: secondCenter.y - firstCenter.y,
    };
    for (let slot = 3; slot <= teamSize; slot += 1) {
      addSlot('left', slot, {
        x: firstCenter.x + step.x * (slot - 1),
        y: firstCenter.y + step.y * (slot - 1),
      });
    }
    const right1 = findSlot('right', 1);
    if (!right1) return result;
    const rightCenter = candidateHeroCropCenter(right1.crop);
    for (let slot = 2; slot <= teamSize; slot += 1) {
      addSlot('right', slot, {
        x: rightCenter.x + step.x * (slot - 1),
        y: rightCenter.y + step.y * (slot - 1),
      });
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

function renderCandidateHeroLineup() {
  const review = $('#candidate-hero-review');
  const teams = $('#candidate-hero-teams');
  const tools = $('.candidate-hero-tools');
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
  const drawButton = $('#btn-candidate-hero-draw');
  const saveTemplate = $('#btn-candidate-hero-save-template');
  const clearButton = $('#btn-candidate-hero-clear');
  if (!candidateHeroLineup) {
    teams.classList.add('hidden');
    drawButton.classList.remove('hidden');
    drawButton.disabled = true;
    saveTemplate.disabled = true;
    clearButton.disabled = true;
    $('#candidate-hero-status').textContent = candidateHeroLoading
      ? '正在读取本图或该主播缓存的英雄框…'
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
  const marksPlayer = ['scoreboard', 'result_page'].includes(
    candidateHeroLineup.screen_type);
  const playerPosition = candidateHeroPlayerPosition();
  const next = candidateNextHeroPosition();
  const status = candidateHeroLineup.review_status === 'confirmed'
    ? '阵容已经人工确认；修改任意下拉框后会更新。'
    : complete
      ? `算法预填 ${recognized}/${candidateHeroLineup.slots.length} 个；` +
        '正确的不用改，只修改错误或空白的位置。'
      : next
        ? `已画 ${candidateHeroLineup.slots.length}/` +
          `${candidateHeroLineup.team_size * 2} 个；下一框是` +
          `${next.side === 'left' ? '左队' : '右队'}第 ${next.slot} 个。`
        : '还没有英雄圆框。';
  const drawingHint = !complete && candidateHeroLineup.slots.length
    ? ' 后续圆框沿用第一个大小，可直接点头像中心。' : '';
  const playerHint = !marksPlayer ? '' : playerPosition
    ? ` 主播本人：${playerPosition.side === 'left' ? '左' : '右'}队第 ` +
      `${playerPosition.slot} 个。`
    : ' 请点击“设为本人”，标出画面中高亮的主播英雄。';
  $('#candidate-hero-status').textContent =
    `${screenName} · ${candidateHeroLineup.team_size}V${candidateHeroLineup.team_size} · ` +
    `${status}${drawingHint}${playerHint} 拖圆框移动，拖黄点缩放。`;
  drawButton.disabled = candidateHeroLoading || complete;
  drawButton.classList.toggle('hidden', complete);
  drawButton.classList.toggle('selected', candidateHeroDrawMode);
  drawButton.textContent = candidateHeroDrawMode && next
      ? `正在画${next.side === 'left' ? '左' : '右'}${next.slot}`
      : '补画头像';
  saveTemplate.disabled = candidateHeroLoading || !complete;
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
      const isPlayer = marksPlayer && candidateHeroPlayerSlot === key;
      const card = document.createElement('article');
      card.className = 'candidate-hero-slot';
      card.dataset.heroSlot = key;
      card.classList.toggle('player', isPlayer);
      const index = document.createElement('span');
      index.className = 'candidate-hero-slot-index';
      index.textContent = String(slot.slot);
      card.appendChild(index);
      const comparison = document.createElement('div');
      comparison.className = 'candidate-hero-comparison';
      comparison.setAttribute('aria-label', '截图头像与当前标注头像对照');
      const crop = document.createElement('img');
      crop.className = 'candidate-hero-crop';
      crop.src = `${slot.crop_url}?t=${encodeURIComponent(slot.updated_at || '')}`;
      crop.alt = `${side === 'left' ? '左队' : '右队'}第 ${slot.slot} 个截图头像`;
      crop.title = '截图中圈出的原始头像';
      crop.draggable = false;
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
      details.classList.toggle('with-player-action', marksPlayer);
      const select = document.createElement('button');
      select.type = 'button';
      select.className = 'candidate-hero-select';
      select.dataset.heroSlot = key;
      select.classList.toggle('missing', !hero);
      const name = document.createElement('span');
      name.className = 'candidate-hero-selected-name';
      const confidence = hero && selected === slot.suggested_label
        ? ` · ${(Number(slot.suggestion_confidence || 0) * 100).toFixed(1)}%`
        : '';
      name.textContent = hero
        ? `${hero.name}${hero.label === 'unreadable' ? '' : ` · ${hero.label}`}`
        : '请选择英雄';
      name.textContent += confidence;
      select.title = name.textContent;
      select.appendChild(name);
      select.onclick = () => openCandidateHeroPicker(select, key);
      details.appendChild(select);
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
          candidateHeroPlayerSlot = key;
          candidateHeroDirty = true;
          $('#candidate-save-state').classList.remove('error');
          $('#candidate-save-state').textContent = '';
          renderCandidateHeroLineup();
        };
        details.appendChild(playerButton);
      }
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
  showCandidateSaveError('请在英雄阵容中标出画面高亮的主播本人英雄');
  const buttons = $$('.candidate-hero-player');
  buttons.forEach((button) => button.classList.add('needs-attention'));
  if (buttons[0]) buttons[0].scrollIntoView({block: 'center', inline: 'nearest'});
}

async function loadCandidateHeroLineup(item) {
  const context = candidateHeroContext(item);
  if (!context) {
    resetCandidateHeroReview();
    renderCandidateHeroContextControls();
    renderCandidateHeroLineup();
    $('#btn-candidate-save').disabled = false;
    return;
  }
  const token = ++candidateHeroLoadToken;
  candidateHeroLoading = true;
  candidateHeroLineup = null;
  candidateHeroDraft = new Map();
  candidateHeroPlayerSlot = null;
  candidateHeroDirty = false;
  candidateHeroDrawMode = false;
  closeCandidateHeroPicker();
  $('#candidate-hero-review').classList.remove('hidden');
  $('#candidate-hero-teams').innerHTML = '';
  $('#candidate-hero-status').textContent = '正在读取本图或该主播缓存的英雄框…';
  $('#btn-candidate-save').disabled = true;
  const query = new URLSearchParams({screen_type: context.screenType});
  if (context.teamSize) query.set('team_size', String(context.teamSize));
  try {
    const [, lineup] = await Promise.all([
      ensureCandidateHeroCatalog(),
      api(`/api/training-review/items/${item.frame_id}/hero-lineup?${query}`),
    ]);
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
    lineup.slots ||= [];
    candidateHeroLineup = lineup;
    candidateHeroPlayerSlot = candidateHeroPlayerKey(lineup);
    if (!context.teamSize && lineup.team_size) {
      candidateHeroTeamSizeExplicit = true;
      candidateHeroTeamSizeOverride = lineup.team_size;
      renderCandidateChoices();
    }
    candidateHeroDraft = new Map(
      lineup.slots.map((slot) => [
        candidateHeroKey(slot.side, slot.slot),
        slot.confirmed_label || slot.suggested_label || '',
      ])
    );
    renderCandidateHeroLineup();
  } catch (error) {
    if (token !== candidateHeroLoadToken || currentCandidate() !== item) return;
    candidateHeroLineup = null;
    $('#candidate-hero-review').classList.remove('hidden');
    $('#candidate-hero-status').textContent = '英雄预填失败：' + error.message;
  } finally {
    if (token === candidateHeroLoadToken) {
      candidateHeroLoading = false;
      $('#btn-candidate-save').disabled = false;
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

async function persistCandidateHeroLayout(
  slots, {recognize = false, saveTemplate = false} = {}) {
  const item = currentCandidate();
  const context = candidateHeroContext(item);
  if (!item || !context || !context.teamSize || candidateHeroLoading) return false;
  const previousDraft = new Map(candidateHeroDraft);
  const previousPlayerSlot = candidateHeroPlayerSlot;
  candidateHeroLoading = true;
  $('#btn-candidate-save').disabled = true;
  renderCandidateHeroLineup();
  try {
    const lineup = await api(
      `/api/training-review/items/${item.frame_id}/hero-layout`, {
        method: 'PUT',
        body: JSON.stringify({
          screen_type: context.screenType,
          team_size: context.teamSize,
          slots: candidateHeroSlotsPayload(slots),
          recognize: recognize,
          save_template: saveTemplate,
        }),
      });
    candidateHeroLineup = lineup;
    const persistedPlayerSlot = candidateHeroPlayerKey(lineup);
    const previousPlayerStillExists = previousPlayerSlot && lineup.slots.some(
      (slot) => candidateHeroKey(slot.side, slot.slot) === previousPlayerSlot
    );
    candidateHeroPlayerSlot = persistedPlayerSlot ||
      (previousPlayerStillExists ? previousPlayerSlot : null);
    candidateHeroDraft = new Map(
      lineup.slots.map((slot) => {
        const key = candidateHeroKey(slot.side, slot.slot);
        return [
          key,
          previousDraft.get(key) || slot.confirmed_label ||
            slot.suggested_label || '',
        ];
      })
    );
    candidateHeroDirty = true;
    if (lineup.template_saved) {
      $('#candidate-save-state').textContent =
        '英雄圆框已识别，并缓存为该主播的同类画面布局';
    }
    return true;
  } catch (error) {
    $('#candidate-save-state').textContent = '英雄圆框保存失败：' + error.message;
    return false;
  } finally {
    candidateHeroLoading = false;
    $('#btn-candidate-save').disabled = false;
    renderCandidateHeroLineup();
    renderCandidateChoices();
  }
}

async function addCandidateHeroCircle(crop) {
  const next = candidateNextHeroPosition();
  if (!next || !candidateHeroLineup) return;
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
  const saved = await persistCandidateHeroLayout(slots, {
    recognize: complete,
    saveTemplate: complete,
  });
  if (saved && complete) {
    candidateHeroDrawMode = false;
    const automaticallyAdded = slots.length - manuallyAdded.length;
    if (automaticallyAdded > 0) {
      $('#candidate-save-state').textContent =
        `已自动补齐 ${automaticallyAdded} 个英雄圆框，并完成识别`;
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
  if (candidateHeroPlayerSlot === key) candidateHeroPlayerSlot = null;
  closeCandidateHeroPicker();
  const saved = await persistCandidateHeroLayout(slots);
  if (saved) candidateHeroDrawMode = true;
  renderCandidateHeroLineup();
}

async function clearCandidateHeroLayout() {
  if (!candidateHeroLineup || !candidateHeroLineup.slots.length) return;
  candidateHeroDraft = new Map();
  candidateHeroPlayerSlot = null;
  closeCandidateHeroPicker();
  const saved = await persistCandidateHeroLayout([]);
  if (saved) candidateHeroDrawMode = true;
  renderCandidateHeroLineup();
}

async function saveCandidateHeroTemplate() {
  if (!candidateHeroLayoutComplete()) return;
  await persistCandidateHeroLayout(candidateHeroLineup.slots, {
    recognize: true,
    saveTemplate: true,
  });
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
  sizeGroup.classList.toggle('hidden', !hasHeroLayout);
  if (hasHeroLayout) {
    const context = candidateHeroContext(item);
    [3, 5].forEach((teamSize) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `${teamSize} 人`;
      button.classList.toggle(
        'selected', context && context.teamSize === teamSize);
      button.onclick = () => {
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

function candidateResultQualitySummary() {
  if (!candidateDraft) return '异常：无';
  const parts = [];
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
  return parts.length ? parts.join(' · ') : '异常：无';
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
  summary.title = '展开填写结算遮挡和 OCR 可用性';
  details.appendChild(summary);
  const panel = document.createElement('div');
  panel.className = 'candidate-result-quality-panel';
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
  details.appendChild(panel);
}

function createCandidateResultQualityDetails() {
  const details = document.createElement('details');
  details.className = 'candidate-result-quality';
  details.classList.toggle(
    'hidden', candidateDraft.result_panel_label !== 'result_panel');
  renderCandidateResultQualityDetails(details);
  return details;
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
    const help = document.createElement('p');
    help.className = 'hint small';
    help.textContent = field.help;
    group.appendChild(help);
    actions.appendChild(group);
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
  candidateDraft.hero_layout_label = value;
  candidateHeroDrawMode = false;
  if (CANDIDATE_HERO_SCREEN_TYPES.has(value)) {
    candidateDraft.match_flow_label = 'match_flow';
    candidateDraft.match_mode_label ||= 'unreadable';
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
  renderCandidateBoxes();
  renderCandidateChoices();
  refreshCandidateHeroReview();
}

function selectCandidateReviewLabel(field, value) {
  if (!candidateDraft) return;
  candidateDraft[field] = value;
  if (field === 'match_flow_label') {
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
    if (value === 'match_flow') {
      candidateDraft.match_mode_label ||= 'unreadable';
      candidateDraft.hero_select_label = 'not_select';
    } else {
      candidateDraft.match_mode_label = null;
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
    candidateHeroTeamSizeExplicit = true;
    candidateHeroTeamSizeOverride = value === '5v5' ? 5
      : ['3v3', 'aram'].includes(value) ? 3 : null;
  } else if (field === 'hero_select_label' && value.startsWith('select_')) {
    candidateDraft.match_flow_label = 'not_match_flow';
    candidateDraft.match_mode_label = null;
    candidateHeroTeamSizeExplicit = false;
    candidateHeroTeamSizeOverride = null;
    candidateDraft.result_panel_label = 'no_result_panel';
    candidateDraft.hero_layout_label = 'none';
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
  renderCandidateChoices();
  refreshCandidateHeroReview();
}

function renderCandidateItem() {
  const item = currentCandidate();
  const image = $('#candidate-image');
  const empty = $('#candidate-empty');
  $('#candidate-progress').textContent = candidateQueue.length
    ? `${candidateIndex + 1}/${candidateQueue.length}` : '0/0';
  $('#btn-candidate-prev').disabled = !item || candidateIndex <= 0;
  $('#btn-candidate-next').disabled = !item || candidateIndex >= candidateQueue.length - 1;
  if (!item) {
    image.onload = null;
    image.removeAttribute('src');
    $('#candidate-image-wrap').classList.add('hidden');
    empty.classList.remove('hidden');
    $('#candidate-meta').textContent = '';
    $('#candidate-label-actions').innerHTML = '';
    $('#candidate-suggestion').textContent = '--';
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
  image.onload = () => {
    if (currentCandidate() && currentCandidate().frame_id === frameId) {
      renderCandidateBoxes();
    }
  };
  image.src = `/api/frames/${item.frame_id}/image?t=${encodeURIComponent(item.updated_at)}`;
  candidateHeroTeamSizeExplicit = false;
  candidateHeroTeamSizeOverride = null;
  candidateHeroDrawMode = false;
  candidateDraft = candidateDefaultDraft(item);
  const suggestedBox = candidateSuggestedResultBox(item);
  candidateBoxes = suggestedBox ? [suggestedBox] : [];
  $('#candidate-notes').value = item.notes || '';
  $('#candidate-meta').textContent =
    `${item.streamer || '未知主播'} / ${item.filename || ''} · ` +
    `${(item.timestamp_ms / 1000).toFixed(1)}s · frame #${item.frame_id} · ` +
    `${item.source_count || 0} 个来源`;
  renderCandidateSuggestions(item);
  renderCandidateChoices();
  $('#btn-candidate-save').disabled = false;
  $('#candidate-save-state').classList.remove('error');
  $('#candidate-save-state').textContent = item.needs_player_hero_review
    ? '原标注已保留，请补齐英雄阵容并标出主播本人'
    : item.review_status === 'confirmed'
      ? '这张图已经人工确认' : item.review_status === 'partial'
      ? '历史数据只覆盖了部分标签，请补齐后确认'
      : item.review_status === 'skipped' ? '已跳过' : '';
  $('#btn-candidate-skip').disabled = item.review_status === 'confirmed';
  renderCandidateBoxes();
  loadCandidateHeroLineup(item);
}

async function loadCandidateReview() {
  const status = $('#candidate-status-filter').value;
  try {
    const data = await api(
      `/api/training-review/items?status=${encodeURIComponent(status)}&limit=2000`);
    candidateQueue = data.items || [];
    candidateIndex = 0;
    renderCandidateItem();
    const stats = data.stats || {};
    const missingPlayer = stats.missing_player_hero || 0;
    $('#candidate-sync-state').textContent =
      `本地 ${stats.total || 0} · 待补齐 ${((stats.statuses || {}).pending || 0) +
        ((stats.statuses || {}).partial || 0) + missingPlayer}（待补本人 ${missingPlayer}） · ` +
      `已确认 ${(stats.statuses || {}).confirmed || 0} · ` +
      `待回传 ${stats.dirty || 0}`;
  } catch (error) {
    $('#candidate-save-state').textContent = '加载失败：' + error.message;
  }
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

async function saveCandidateReview(skip = false) {
  const item = currentCandidate();
  if (!item || !candidateDraft) return;
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
  const heroContext = candidateHeroContext(item);
  if (!skip && heroContext && (
    !heroContext.teamSize ||
    !candidateHeroLineup ||
    candidateHeroLineup.screen_type !== heroContext.screenType ||
    candidateHeroLineup.team_size !== heroContext.teamSize ||
    !candidateHeroLayoutComplete()
  )) {
    showCandidateSaveError('请先选择每队人数，并按顺序画满全部英雄圆框');
    return;
  }
  const heroLabels = !skip && heroContext && candidateHeroLineup
    ? candidateHeroLineup.slots.map((slot) => ({
      side: slot.side,
      slot: slot.slot,
      hero_label: candidateHeroDraft.get(
        candidateHeroKey(slot.side, slot.slot)) || '',
    })) : null;
  const missingHero = heroLabels && heroLabels.find(
    (value) => !value.hero_label);
  if (missingHero) {
    showCandidateMissingHero(missingHero);
    return;
  }
  const playerPosition = candidateHeroPlayerPosition();
  if (!skip && heroContext &&
      ['scoreboard', 'result_page'].includes(heroContext.screenType) &&
      !playerPosition) {
    showCandidateMissingPlayerHero();
    return;
  }
  $('#candidate-save-state').textContent = '正在保存…';
  try {
    if (heroLabels && (
      candidateHeroDirty || candidateHeroLineup.review_status !== 'confirmed'
    )) {
      candidateHeroLineup = await api(
        `/api/training-review/items/${item.frame_id}/hero-lineup`, {
          method: 'PUT',
          body: JSON.stringify({
            heroes: heroLabels,
            player_side: playerPosition && playerPosition.side,
            player_slot: playerPosition && playerPosition.slot,
          }),
        });
      candidateHeroPlayerSlot = candidateHeroPlayerKey(candidateHeroLineup);
      candidateHeroDirty = false;
    }
    const labels = skip ? {
      match_flow_label: null, match_mode_label: null,
      hero_select_label: null, result_panel_label: null,
      hero_layout_label: null,
    } : candidateDraft;
    const updated = await api(`/api/training-review/items/${item.frame_id}`, {
      method: 'PUT',
      body: JSON.stringify({
        ...labels,
        review_status: skip ? 'skipped' : 'confirmed',
        result_box: !skip && candidateDraft.result_panel_label === 'result_panel'
          ? candidateBoxes[0] : null,
        notes: $('#candidate-notes').value,
      }),
    });
    if (!skip) cacheCandidateReviewLabels(candidateDraft);
    candidateQueue[candidateIndex] = updated;
    if (candidateIndex < candidateQueue.length - 1) {
      moveCandidate(1);
      return;
    }
    renderCandidateItem();
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
  return crop.w * width;
}

async function refreshCandidateSync() {
  try {
    const value = await api('/api/worker-candidates/state');
    const sync = value.sync || {};
    const review = value.review || {};
    $('#candidate-sync-state').textContent = sync.running
      ? `同步中：Worker ${sync.processed || 0}/${sync.total || 0}，` +
        `历史结算 ${sync.archive_processed || 0}/${sync.archive_total || 0}`
      : sync.error ? `同步失败：${sync.error}`
      : sync.archive_failed ?
        `历史结算图失败 ${sync.archive_failed} 张：${sync.archive_last_error || '未知错误'}`
      : `本地 ${review.total || 0} · 待补齐 ${((review.statuses || {}).pending || 0) +
          ((review.statuses || {}).partial || 0) +
          (review.missing_player_hero || 0)}（待补本人 ${review.missing_player_hero || 0}） · ` +
        `待回传 ${review.dirty || 0} · ` +
        `历史结算新增 ${sync.archive_inserted || 0}／预填框 ${sync.archive_box_suggested || 0} · ` +
        `本次拉取 ${sync.reviews_pulled || 0}／回传 ${sync.reviews_pushed || 0}`;
    if (!sync.running && candidateSyncTimer) {
      clearInterval(candidateSyncTimer);
      candidateSyncTimer = null;
      await loadCandidateReview();
    }
  } catch (error) {
    $('#candidate-sync-state').textContent = '同步状态读取失败：' + error.message;
  }
}

async function syncCandidates() {
  $('#candidate-sync-state').textContent = '正在启动同步…';
  try {
    await api('/api/worker-candidates/sync', {
      method: 'POST', body: JSON.stringify({maximum: 20000}),
    });
    if (!candidateSyncTimer) {
      candidateSyncTimer = setInterval(refreshCandidateSync, 1000);
    }
    await refreshCandidateSync();
  } catch (error) {
    $('#candidate-sync-state').textContent = '同步失败：' + error.message;
  }
}

function bindCandidateReview() {
  $('#candidate-status-filter').onchange = loadCandidateReview;
  $('#btn-candidate-refresh').onclick = loadCandidateReview;
  $('#btn-candidate-sync').onclick = syncCandidates;
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
  $('#btn-candidate-hero-draw').onclick = () => {
    if (!candidateHeroLineup || candidateHeroLayoutComplete()) return;
    candidateHeroDrawMode = !candidateHeroDrawMode;
    renderCandidateHeroLineup();
    renderCandidateChoices();
  };
  $('#btn-candidate-hero-save-template').onclick = saveCandidateHeroTemplate;
  $('#btn-candidate-hero-clear').onclick = clearCandidateHeroLayout;
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
  layer.onpointerdown = (event) => {
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
      await addCandidateHeroCircle({
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
      `已处理 ${sync.processed} 张，新增 ${sync.inserted}，更新 ${sync.updated}，` +
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

// ---------- 模型测试与 Worker 模型包 ----------
const MODEL_TASK_NAMES = {
  screen_state: '画面状态',
  bp_review: 'BP 模式',
  key_screen_review: '结算／计分板',
  result_detector: '结算面板',
  mode_gate: '大乱斗光栅',
};

function currentModelTestSample() {
  return modelTestSamples[modelTestIndex] || null;
}

function renderModelTestSample() {
  const sample = currentModelTestSample();
  $('#model-test-progress').textContent = modelTestSamples.length
    ? `${modelTestIndex + 1}/${modelTestSamples.length}` : '0/0';
  $('#btn-model-test-prev').disabled = !sample || modelTestIndex <= 0;
  $('#btn-model-test-next').disabled =
    !sample || modelTestIndex >= modelTestSamples.length - 1;
  $('#model-test-output').textContent = '--';
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
  $('#model-test-expected').textContent = JSON.stringify(sample.expected, null, 2);
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
  if (!runId) {
    modelTestSamples = [];
    renderModelTestSample();
    return;
  }
  $('#model-test-state').textContent = '正在读取冻结快照…';
  try {
    const split = $('#model-test-split').value;
    const data = await api(
      `/api/model-tests/runs/${encodeURIComponent(runId)}/samples` +
      `?split=${encodeURIComponent(split)}&limit=1000`);
    modelTestSamples = data.items || [];
    modelTestIndex = 0;
    const run = modelTestRuns.find((item) => item.id === runId);
    $('#model-test-notes').value = (run && run.validation_notes) || '';
    $('#model-test-state').textContent =
      `${split} 共 ${data.total || 0} 张 · 当前验收：` +
      `${(run && run.validation_status) || 'pending'}`;
    renderModelTestSample();
  } catch (error) {
    $('#model-test-state').textContent = '载入失败：' + error.message;
  }
}

async function predictModelTestSample() {
  const sample = currentModelTestSample();
  const runId = $('#model-test-run').value;
  if (!sample || !sample.has_snapshot_image || !runId) return;
  $('#model-test-output').textContent = '推理中…';
  try {
    const result = await api(
      `/api/model-tests/runs/${encodeURIComponent(runId)}/predict`, {
        method: 'POST',
        body: JSON.stringify({
          sample_id: sample.sample_id,
          split: sample.split,
          conf_thr: Number($('#model-test-conf').value || 0.25),
        }),
      });
    $('#model-test-output').textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    $('#model-test-output').textContent = '推理失败：' + error.message;
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
  Object.entries(MODEL_TASK_NAMES).forEach(([taskId, name]) => {
    const choices = modelTestRuns.filter(
      (run) => run.task_id === taskId && run.validation_status === 'passed');
    const row = document.createElement('label');
    row.className = 'model-package-choice';
    const title = document.createElement('span');
    title.textContent = name;
    const select = document.createElement('select');
    select.dataset.packageTask = taskId;
    select.innerHTML = '<option value="">尚未选择</option>' + choices.map((run) =>
      `<option value="${esc(run.id)}">${esc(run.id)} · ${esc(run.dataset_version_id)}</option>`
    ).join('');
    row.append(title, select);
    root.appendChild(row);
  });
}

function renderModelPackages(packages) {
  $('#model-package-list').innerHTML = (packages || []).map((item) => {
    const missing = ((item.manifest_json || {}).missing_roles || []).join('、');
    return `<div class="small"><b>${esc(item.id)}</b> · ${esc(item.status)} · ` +
      `${esc(item.path)}${missing ? ` · 缺少 ${esc(missing)}` : ''} · ` +
      `<a href="/api/model-packages/${encodeURIComponent(item.id)}/archive">下载 ZIP</a></div>`;
  }).join('') || '<div class="muted small">还没有模型包</div>';
}

async function buildModelPackage() {
  const runIds = $$('[data-package-task]').map((select) => select.value).filter(Boolean);
  if (!runIds.length) {
    $('#model-package-state').textContent = '请至少选择一个验收通过的 run';
    return;
  }
  $('#model-package-state').textContent = '正在校验哈希并组装…';
  try {
    const result = await api('/api/model-packages', {
      method: 'POST', body: JSON.stringify({run_ids: runIds}),
    });
    $('#model-package-state').textContent = result.status === 'ready'
      ? `已生成 ready 模型包：${result.id}`
      : `已生成 incomplete 研究包：${result.id}；` +
        `${result.missing_tasks.length ? `还缺任务 ${result.missing_tasks.join('、')}；` : ''}` +
        `${Object.keys(result.evaluation_gaps || {}).length ? '固定测试集覆盖仍不足' : ''}`;
    const packages = await api('/api/model-packages');
    renderModelPackages(packages.packages);
  } catch (error) {
    $('#model-package-state').textContent = '组包失败：' + error.message;
  }
}

async function loadModelTesting(preferredRunId = '', loadSamples = true) {
  try {
    const [runsData, packagesData] = await Promise.all([
      api('/api/model-tests/runs'), api('/api/model-packages'),
    ]);
    modelTestRuns = runsData.runs || [];
    const select = $('#model-test-run');
    const previous = preferredRunId || select.value;
    select.innerHTML = '<option value="">请选择训练结果</option>' +
      modelTestRuns.map((run) =>
        `<option value="${esc(run.id)}">${esc(MODEL_TASK_NAMES[run.task_id] || run.task_id)} · ` +
        `${esc(run.id)} · ${esc(run.validation_status)}</option>`
      ).join('');
    if (modelTestRuns.some((run) => run.id === previous)) select.value = previous;
    else if (modelTestRuns.length) select.value = modelTestRuns[0].id;
    renderModelPackageChoices();
    renderModelPackages(packagesData.packages);
    if (loadSamples) await loadModelTestSamples();
  } catch (error) {
    $('#model-test-state').textContent = '模型测试页加载失败：' + error.message;
  }
}

function bindModelTesting() {
  $('#model-test-run').onchange = loadModelTestSamples;
  $('#model-test-split').onchange = loadModelTestSamples;
  $('#btn-model-test-load').onclick = loadModelTestSamples;
  $('#btn-model-test-prev').onclick = () => moveModelTestSample(-1);
  $('#btn-model-test-next').onclick = () => moveModelTestSample(1);
  $('#btn-model-test-predict').onclick = predictModelTestSample;
  $('#btn-model-test-pass').onclick = () => saveModelValidation('passed');
  $('#btn-model-test-fail').onclick = () => saveModelValidation('failed');
  $('#btn-build-model-package').onclick = buildModelPackage;
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

function trainingCountsText(task) {
  const counts = task.counts || {};
  if (task.id === 'screen_state') {
    const labels = counts.by_label || {};
    return `非虚荣 ${labels.not_vainglory || 0} · 游戏外 ${labels.out_of_match || 0} · ` +
      `对局前 ${labels.pre_match || 0} · 对局中 ${labels.in_match || 0} · ` +
      `天赋 ${labels.talent_select || 0} · 赛后 ${labels.post_match || 0} · ` +
      `转场 ${labels.transition || 0} · ${counts.videos || 0} 个视频`;
  }
  if (task.id === 'bp_review') {
    const labels = counts.by_label || {};
    return `3V3 ${labels.bp_3v3 || 0} · 大乱斗 ${labels.bp_aram || 0} · ` +
      `5V5 ${labels.bp_5v5 || 0} · 非BP ${labels.not_bp || 0} · ` +
      `${counts.videos || 0} 个视频`;
  }
  if (task.id === 'key_screen_review') {
    const labels = counts.by_label || {};
    return `结算 ${labels.result_page || 0} · 计分板 ${labels.scoreboard || 0} · ` +
      `其他 ${labels.other || 0} · ${counts.videos || 0} 个视频`;
  }
  return `正样本 ${counts.positive || 0} · 负样本 ${counts.negative || 0}` +
    `${counts.hard_negative == null ? '' : `（计分板 hard negative ${counts.hard_negative}）`} · ` +
    `${counts.videos || 0} 个视频`;
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

function renderTrainingTasks(tasks, activeRunId) {
  const grid = $('#training-task-grid');
  grid.innerHTML = '';
  tasks.forEach((task) => {
    const reasons = task.blocking_reasons || [];
    const warnings = task.quality_warnings || [];
    grid.insertAdjacentHTML('beforeend', `
      <article class="training-task-card ${task.ready ? 'ready' : ''}">
        <h3>${esc(task.name)}</h3>
        <div class="muted small">${esc(task.description)}</div>
        <div class="task-counts">${esc(trainingCountsText(task))}</div>
        <div class="muted small">建议量：${esc(task.recommended)}</div>
        ${trainingSnapshotNote(task.id)
          ? `<div class="muted small">${esc(trainingSnapshotNote(task.id))}</div>`
          : ''}
        ${reasons.length
          ? `<div class="blocking">暂不能训练：${esc(reasons.join('；'))}</div>`
          : '<div class="small status-done">已达到最低结构要求，可以训练</div>'}
        ${!reasons.length && warnings.length
          ? `<div class="blocking">可以试训，但距离建议量还差：${esc(warnings.join('；'))}</div>`
          : ''}
        <button class="primary training-start" data-task-id="${esc(task.id)}"
          ${!task.ready || activeRunId ? 'disabled' : ''}>
          用当前数据开始训练（${task.epochs} epochs）
        </button>
      </article>`);
  });
  $$('.training-start').forEach((button) => {
    button.onclick = () => startTraining(button.dataset.taskId);
  });
}

function metricSummary(metrics) {
  const entries = Object.entries(metrics || {}).slice(0, 3);
  return entries.length
    ? entries.map(([key, value]) => `${key}=${Number(value).toFixed(3)}`).join(' · ')
    : '--';
}

function renderTrainingRuns(data) {
  const activeRunId = data.active_run_id;
  const tbody = $('#training-runs-table tbody');
  tbody.innerHTML = '';
  (data.runs || []).forEach((run) => {
    const percent = Math.round(Number(run.progress || 0) * 100);
    const statusLabel = TRAINING_STATUS_LABELS[run.status] || run.status;
    const canCancel = ['queued', 'running'].includes(run.status) &&
      run.id === activeRunId;
    const canTest = run.status === 'succeeded' && run.artifact_path;
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td>${esc(run.task_id)}<br><span class="muted">${esc(run.id)}</span></td>
        <td>${esc(run.dataset_version_id)}</td>
        <td class="training-status-${esc(run.status)}">${esc(statusLabel)}
          ${run.error ? `<br><span class="small">${esc(run.error)}</span>` : ''}</td>
        <td><span class="training-progress"><div style="width:${percent}%"></div></span>
          ${percent}% · ${run.current_epoch || 0}/${run.epochs}</td>
        <td>${esc(metricSummary(run.metrics_json))}</td>
        <td>${run.artifact_path ? esc(run.artifact_path) : '--'}
          ${run.published_path
            ? `<br><span class="status-done">本机测试：${esc(run.published_path)}</span>`
            : ''}</td>
        <td>
          <button class="training-log-open" data-run-id="${esc(run.id)}">日志</button>
          ${canCancel
            ? `<button class="training-cancel" data-run-id="${esc(run.id)}">取消</button>`
            : ''}
          ${canTest
            ? `<button class="training-test-run" data-run-id="${esc(run.id)}">进入模型验收</button>`
            : ''}
        </td>
      </tr>`);
  });
  if (!(data.runs || []).length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">尚无训练记录</td></tr>';
  }
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
    ? `正在训练：${activeRunId}` : '当前没有训练任务占用本机算力';
}

async function loadTrainingDashboard() {
  try {
    const [tasks, runs] = await Promise.all([
      api('/api/training/tasks'),
      api('/api/training/runs'),
    ]);
    renderTrainingTasks(tasks, runs.active_run_id);
    renderTrainingRuns(runs);
    if (runs.active_run_id && !trainingPollTimer) {
      trainingPollTimer = setInterval(loadTrainingDashboard, 2000);
    } else if (!runs.active_run_id && trainingPollTimer) {
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
    '现在会冻结当前标注数据并开始本机训练。之后新增的标注不会混进这一轮，' +
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
      `已启动 ${run.id}，数据快照 ${run.dataset_version_id}`;
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
  try {
    const result = await api(
      `/api/training/runs/${encodeURIComponent(runId)}/log?tail=200`);
    $('#training-log').textContent = result.log || '暂无日志';
    $('#training-log-box').classList.remove('hidden');
    $('#training-log-box').open = true;
  } catch (error) {
    $('#training-log').textContent = '日志加载失败：' + error.message;
    $('#training-log-box').classList.remove('hidden');
  }
}

$('#btn-training-refresh').onclick = loadTrainingDashboard;

// ---------- 工具 ----------
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
