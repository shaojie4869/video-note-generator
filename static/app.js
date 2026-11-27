'use strict';

const ACTIVE_STATUSES = new Set(['queued', 'running', 'waiting_confirmation']);
const STATUS_LABELS = {
  queued: '等待中',
  running: '进行中',
  waiting_confirmation: '待确认',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消'
};
const PURPOSE_LABELS = {
  summary: '文本生成',
  vision: '画面识别'
};

const PROVIDER_LABELS = {
  api: 'API'
};

const state = {
  sourceType: 'local',
  // 本地支持一次选多个文件；每一项 {path, title}
  local: { files: [] },
  // selected 存分P 的 index（0 基），批量生成时每一项各建一个任务
  bilibili: { info: null, infoUrl: '', selected: new Set() },
  system: null,
  jobs: [],
  courses: [],
  // 右侧当前显示哪个视图：home / course / job / records
  view: 'home',
  currentCourseId: '',
  currentCourse: null,
  courseNotes: [],
  miscId: 'misc',
  courseSelectSignature: '',
  coursesRefreshing: false,
  noteMenuJobId: '',
  selectedId: null,
  currentJob: null,
  config: null,
  refreshing: false,
  detailRequest: 0,
  pollErrorNotified: false,
  renderedSummaryJob: '',
  renderedSummary: '',
  renderedVisualJob: '',
  renderedVisual: '',
  // 划重点：按笔记缓存，渲染后按「原文 + 起始位置」贴回去
  highlights: { jobId: '', summary: [], visual: [] },
  highlightPositions: { summary: [], visual: [] },
  // 笔记搜索
  search: { query: '', results: [], request: 0 },
  // 从搜索结果跳进笔记后，需要在正文里定位并高亮
  pendingLocate: null,
  models: [],
  visionModels: [],
  // 当前停留在哪个模块：'' 表示只看模块入口
  currentModule: '',
  moduleJobId: '',
  renderedQuizKey: '',
  quizSortByLevel: false,
  quizBusy: false,
  // null 表示还没探测到；数组表示后端声明的能力
  backendFeatures: null
};

let elements = {};

// 文本模型 / 视觉模型两个下拉共用的注册表
const modelPickers = [];

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function init() {
  const ids = [
    'settings-open', 'settings-dialog', 'settings-close',
    'tab-local', 'tab-bilibili', 'panel-local', 'panel-bilibili',
    'source-card', 'source-toggle',
    'browse-button', 'local-selection', 'local-count', 'local-list', 'local-clear',
    'bilibili-url', 'bilibili-info-button', 'bilibili-result', 'bilibili-title', 'bilibili-count',
    'bilibili-entries', 'bilibili-select-all', 'bilibili-clear',
    'source-hint', 'start-button', 'course-count', 'course-refresh-status', 'course-empty', 'course-list',
    'course-select', 'course-new', 'course-hint', 'records-open', 'records-entry-meta',
    'course-view', 'course-title', 'course-meta', 'course-more', 'course-active', 'course-notes', 'course-notes-empty',
    'records-view', 'records-meta', 'records-list', 'usage-badge', 'card-menu',
    'note-search', 'note-search-clear', 'search-view', 'search-title', 'search-meta', 'search-results', 'hl-palette',
    'job-back', 'prev-note', 'next-note',
    'job-empty', 'job-detail', 'job-title', 'job-status', 'job-provider', 'job-created', 'job-usage', 'job-stage',
    'job-progress-section', 'job-progress', 'job-progress-text', 'cancel-job', 'confirmation-box', 'transcript-count', 'confirm-job',
    'error-box', 'job-error', 'result-section', 'copy-summary', 'export-summary', 'summary-content',
    'visual-details', 'visual-content', 'transcript-details', 'transcript-content', 'export-transcript', 'regenerate-job', 'delete-job', 'rename-job',
    'module-nav', 'module-notes', 'module-quiz', 'nav-notes-meta', 'nav-quiz-meta',
    'quiz-list', 'quiz-empty', 'quiz-empty-text', 'quiz-generate', 'quiz-regenerate', 'quiz-sort-by-level',
    'backend-stale', 'text-menu',
    'general-form', 'frames-enabled', 'frame-interval', 'max-frames', 'deep-refine', 'exam-mode', 'whisper-model',
    'whisper-device', 'whisper-compute-type', 'chunk-chars', 'save-general', 'general-status',
    'api-form', 'api-configured', 'api-base-url', 'api-model', 'api-vision-model', 'api-reasoning-effort', 'api-key', 'fetch-models', 'models-status', 'model-toggle', 'model-menu', 'vision-toggle', 'vision-menu', 'save-api', 'test-api', 'api-status',
    'system-badge', 'system-summary', 'system-slots',
    'toast-region'
  ];
  ids.forEach((id) => {
    elements[toCamelCase(id)] = document.getElementById(id);
  });

  bindEvents();
  switchSource('local');
  setView('home');
  refreshCourses({ initial: true });
  refreshJobs({ initial: true });
  checkBackendFeatures();
  loadSystemProfile();
  window.setInterval(() => {
    refreshJobs();
    refreshCourses();
  }, 2000);
}

function toCamelCase(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

/**
 * 探测后端能力。
 * 前端文件每次刷新都从磁盘重读，但 Python 进程只有重启才加载新路由，
 * 两者错配时用户只会看到“Not Found”。这里提前把原因摆出来。
 */
async function checkBackendFeatures() {
  try {
    const response = await fetch('/api/health', { headers: { 'X-Study-Buddy': '1' } });
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    state.backendFeatures = Array.isArray(data.features) ? data.features : [];
    renderCurrentJob();
  } catch (_error) {
    // 网络失败时保持未知，避免误报“后端过旧”。
  }
}

function backendSupports(feature) {
  return Array.isArray(state.backendFeatures) && state.backendFeatures.includes(feature);
}

function bindEvents() {
  elements.tabLocal.addEventListener('click', () => switchSource('local'));
  elements.tabBilibili.addEventListener('click', () => switchSource('bilibili'));
  [elements.tabLocal, elements.tabBilibili].forEach((tab) => {
    tab.addEventListener('keydown', handleTabKeydown);
  });

  elements.browseButton.addEventListener('click', browseLocalVideo);
  elements.sourceToggle.addEventListener('click', () => {
    setSourceCollapsed(!elements.sourceCard.classList.contains('is-collapsed'));
  });
  elements.localClear.addEventListener('click', clearLocalFiles);
  setupVideoDrop();
  elements.bilibiliInfoButton.addEventListener('click', readBilibiliInfo);
  elements.bilibiliUrl.addEventListener('input', handleBilibiliUrlChange);
  elements.bilibiliUrl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      readBilibiliInfo();
    }
  });
  elements.bilibiliSelectAll.addEventListener('click', () => setAllBilibiliEntries(true));
  elements.bilibiliClear.addEventListener('click', () => setAllBilibiliEntries(false));
  elements.bilibiliEntries.addEventListener('change', handleBilibiliEntryChange);
  elements.startButton.addEventListener('click', startNewJob);
  elements.courseNew.addEventListener('click', beginNewCourse);
  elements.courseSelect.addEventListener('change', () => {
    // 手动选过课程就不再显示「自动：xx」；切回自动时立即重新算一次归属
    if (!elements.courseSelect.value) {
      suggestCourse();
    }
  });
  elements.recordsOpen.addEventListener('click', openRecords);
  setupSearch();
  setupHighlightPalette();
  elements.courseMore.addEventListener('click', (event) => {
    event.stopPropagation();
    openCourseMenu(elements.courseMore);
  });
  elements.jobBack.addEventListener('click', goBackFromJob);
  elements.prevNote.addEventListener('click', () => stepNote(-1));
  elements.nextNote.addEventListener('click', () => stepNote(1));

  elements.cancelJob.addEventListener('click', cancelSelectedJob);
  elements.confirmJob.addEventListener('click', confirmSelectedJob);
  elements.regenerateJob.addEventListener('click', regenerateSelectedJob);
  elements.renameJob.addEventListener('click', startRenameJob);
  elements.deleteJob.addEventListener('click', () => {
    if (state.currentJob) {
      deleteJob(state.currentJob.id, elements.deleteJob);
    }
  });
  elements.copySummary.addEventListener('click', copySelectedSummary);
  elements.exportSummary.addEventListener('click', () => downloadSelectedJob('summary'));
  elements.exportTranscript.addEventListener('click', () => downloadSelectedJob('transcript'));

  elements.moduleNav.addEventListener('click', (event) => {
    const card = event.target.closest('[data-module]');
    if (card) {
      switchModule(card.dataset.module);
    }
  });
  document.querySelectorAll('.module-back').forEach((button) => {
    button.addEventListener('click', () => switchModule(''));
  });
  elements.quizSortByLevel.addEventListener('change', () => {
    state.quizSortByLevel = elements.quizSortByLevel.checked;
    renderQuiz();
  });
  elements.quizList.addEventListener('click', handleQuizClick);
  elements.quizGenerate.addEventListener('click', () => requestQuestions(elements.quizGenerate));
  elements.quizRegenerate.addEventListener('click', () => requestQuestions(elements.quizRegenerate));

  elements.settingsOpen.addEventListener('click', openSettings);
  elements.settingsClose.addEventListener('click', () => elements.settingsDialog.close());
  elements.settingsDialog.addEventListener('close', clearSensitiveInputs);
  elements.generalForm.addEventListener('submit', saveGeneralSettings);
  elements.apiForm.addEventListener('submit', saveApiSettings);
  elements.testApi.addEventListener('click', testApiConnection);
  elements.fetchModels.addEventListener('click', () => {
    withBusy(elements.fetchModels, async () => {
      await loadModelOptions({ announce: true });
      openPicker(modelPickers[0]);
    });
  });
  setupModelPickers();
  setupTextMenu();
}

function handleTabKeydown(event) {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    return;
  }
  event.preventDefault();
  const nextType = state.sourceType === 'local' ? 'bilibili' : 'local';
  switchSource(nextType);
  elements[nextType === 'local' ? 'tabLocal' : 'tabBilibili'].focus();
}

function switchSource(type) {
  state.sourceType = type;
  const isLocal = type === 'local';
  elements.tabLocal.classList.toggle('is-active', isLocal);
  elements.tabBilibili.classList.toggle('is-active', !isLocal);
  elements.tabLocal.setAttribute('aria-selected', String(isLocal));
  elements.tabBilibili.setAttribute('aria-selected', String(!isLocal));
  elements.tabLocal.tabIndex = isLocal ? 0 : -1;
  elements.tabBilibili.tabIndex = isLocal ? -1 : 0;
  elements.panelLocal.hidden = !isLocal;
  elements.panelBilibili.hidden = isLocal;
  updateStartState();
}

// 一次最多排这么多，和后端 BatchJobCreate 的上限保持一致
const MAX_BATCH = 30;

function browseLocalVideo() {
  withBusy(elements.browseButton, async () => {
    const result = await api('/api/videos/browse?multiple=1', { method: 'POST' });
    if (result.cancelled) {
      showToast('已取消选择视频。', 'info');
      return;
    }
    const files = Array.isArray(result.files) && result.files.length
      ? result.files
      : (result.path ? [{ path: result.path, title: result.title }] : []);
    if (!files.length) {
      throw new ApiError('未获取到有效的视频路径。');
    }
    addLocalFiles(files);
  });
}

function addLocalFiles(files) {
  const known = new Set(state.local.files.map((item) => item.path));
  const added = [];
  files.forEach((file) => {
    if (!file || !file.path || known.has(file.path)) {
      return;
    }
    known.add(file.path);
    added.push({ path: file.path, title: file.title || fileBaseName(file.path) });
  });
  if (!added.length) {
    showToast('这些视频已经在待生成列表里了。', 'info');
    return;
  }
  state.local.files = state.local.files.concat(added);
  if (state.local.files.length > MAX_BATCH) {
    const dropped = state.local.files.length - MAX_BATCH;
    state.local.files = state.local.files.slice(0, MAX_BATCH);
    showToast(`一次最多排 ${MAX_BATCH} 个，已忽略多出来的 ${dropped} 个。`, 'info');
  }
  renderLocalSelection();
  const total = state.local.files.length;
  showToast(
    total > 1
      ? `已加入 ${added.length} 个视频，共 ${total} 个待生成；点「批量生成」后才会开始处理。`
      : '视频已选择，点击“开始生成总结”后才会开始处理。',
    'success'
  );
}

function clearLocalFiles() {
  state.local.files = [];
  renderLocalSelection();
}

function setupVideoDrop() {
  const zone = elements.browseButton;
  ['dragover', 'drop'].forEach((type) => {
    window.addEventListener(type, (event) => event.preventDefault());
  });
  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    zone.classList.add('is-dragging');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('is-dragging'));
  zone.addEventListener('drop', handleVideoDrop);
}

function handleVideoDrop(event) {
  event.preventDefault();
  elements.browseButton.classList.remove('is-dragging');
  if (state.sourceType !== 'local' || elements.browseButton.disabled) {
    return;
  }
  const paths = droppedFilePaths(event);
  if (!paths.length) {
    showToast('无法读取拖入文件的路径，请点击此处选择视频。', 'info');
    return;
  }
  addLocalFiles(paths.map((path) => ({ path, title: fileBaseName(path) })));
}

function droppedFilePaths(event) {
  const transfer = event.dataTransfer;
  if (!transfer) {
    return [];
  }
  const paths = [];
  const push = (value) => {
    if (value && !paths.includes(value)) {
      paths.push(value);
    }
  };
  const listed = transfer.getData('text/uri-list') || transfer.getData('text/plain') || '';
  listed.split(/\r?\n/).forEach((line) => {
    if (!line || line.startsWith('#') || !line.startsWith('file://')) {
      return;
    }
    try {
      const path = decodeURIComponent(new URL(line).pathname).replace(/^\//, '');
      push(path.replace(/\//g, '\\'));
    } catch (error) {
      // 解析失败的这一行直接跳过，其他文件仍然可以加入
    }
  });
  if (transfer.files) {
    Array.from(transfer.files).forEach((file) => {
      if (typeof file.path === 'string' && file.path) {
        push(file.path);
      }
    });
  }
  return paths;
}

function fileBaseName(path) {
  const name = path.split(/[\\/]/).pop() || '';
  return name.replace(/\.[^.\\/]+$/, '') || '本地视频';
}

function renderLocalSelection() {
  const files = state.local.files;
  elements.localSelection.hidden = files.length === 0;
  elements.localCount.textContent = files.length ? `已选择 ${files.length} 个视频` : '';
  const fragment = document.createDocumentFragment();
  files.forEach((file, index) => {
    const item = document.createElement('li');
    item.className = 'pick-item';
    const text = document.createElement('div');
    text.className = 'pick-text';
    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = file.title;
    const path = document.createElement('span');
    path.className = 'pick-path';
    path.textContent = file.path;
    path.title = file.path;
    text.append(title, path);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'pick-remove';
    remove.textContent = '×';
    remove.title = '从这个视频移除';
    remove.setAttribute('aria-label', `移除 ${file.title}`);
    remove.addEventListener('click', () => {
      state.local.files.splice(index, 1);
      renderLocalSelection();
    });
    item.append(text, remove);
    fragment.appendChild(item);
  });
  elements.localList.replaceChildren(fragment);
  updateStartState();
  suggestCourse();
}

function handleBilibiliUrlChange() {
  if (state.bilibili.info && elements.bilibiliUrl.value.trim() !== state.bilibili.infoUrl) {
    state.bilibili.info = null;
    state.bilibili.infoUrl = '';
    renderBilibiliInfo();
  }
  updateStartState();
}

function readBilibiliInfo() {
  const url = elements.bilibiliUrl.value.trim();
  if (!url) {
    showToast('请先输入 B站视频或课程链接。', 'error');
    elements.bilibiliUrl.focus();
    return;
  }
  withBusy(elements.bilibiliInfoButton, async () => {
    const info = await api('/api/bilibili/info', {
      method: 'POST',
      body: JSON.stringify({ url })
    });
    if (!Array.isArray(info.entries) || info.entries.length === 0) {
      throw new ApiError('未读取到可选择的视频分P。');
    }
    state.bilibili.info = info;
    state.bilibili.infoUrl = url;
    // 默认只勾第一集：整门课几十集一次全排上，多数人并不想要。
    state.bilibili.selected = new Set([bilibiliEntryIndex(info.entries[0], 0)]);
    renderBilibiliInfo();
    showToast('视频信息读取成功，尚未创建任务。', 'success');
  });
}

function bilibiliEntryIndex(entry, position) {
  return Number.isFinite(Number(entry && entry.index)) ? Number(entry.index) : position;
}

function selectedBilibiliEntries() {
  const info = state.bilibili.info;
  if (!info || !Array.isArray(info.entries)) {
    return [];
  }
  return info.entries.filter((entry, position) => (
    state.bilibili.selected.has(bilibiliEntryIndex(entry, position))
  ));
}

function renderBilibiliInfo() {
  const info = state.bilibili.info;
  const hasInfo = Boolean(info && Array.isArray(info.entries) && info.entries.length);
  elements.bilibiliResult.hidden = !hasInfo;
  elements.bilibiliEntries.replaceChildren();
  if (!hasInfo) {
    state.bilibili.selected = new Set();
    updateBilibiliCount();
    updateStartState();
    return;
  }

  elements.bilibiliTitle.textContent = info.title || 'B站课程';
  const fragment = document.createDocumentFragment();
  info.entries.forEach((entry, position) => {
    const index = bilibiliEntryIndex(entry, position);
    const item = document.createElement('label');
    item.className = 'pick-item pick-check';

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.value = String(index);
    box.checked = state.bilibili.selected.has(index);

    const text = document.createElement('span');
    text.className = 'pick-text';
    const title = document.createElement('span');
    title.className = 'pick-title';
    title.textContent = `P${position + 1} · ${entry.title || `分P ${position + 1}`}`;
    text.appendChild(title);
    const duration = formatDuration(entry.duration);
    if (duration) {
      const meta = document.createElement('span');
      meta.className = 'pick-path';
      meta.textContent = duration;
      text.appendChild(meta);
    }

    item.append(box, text);
    fragment.appendChild(item);
  });
  elements.bilibiliEntries.replaceChildren(fragment);
  updateBilibiliCount();
  updateStartState();
  suggestCourse();
}

function handleBilibiliEntryChange(event) {
  const box = event.target.closest('input[type="checkbox"]');
  if (!box) {
    return;
  }
  const index = Number(box.value);
  if (box.checked) {
    if (state.bilibili.selected.size >= MAX_BATCH) {
      box.checked = false;
      showToast(`一次最多排 ${MAX_BATCH} 集，可以先取消几集再选。`, 'info');
      return;
    }
    state.bilibili.selected.add(index);
  } else {
    state.bilibili.selected.delete(index);
  }
  updateBilibiliCount();
  updateStartState();
  suggestCourse();
}

function setAllBilibiliEntries(select) {
  const info = state.bilibili.info;
  if (!info || !Array.isArray(info.entries)) {
    return;
  }
  if (!select) {
    state.bilibili.selected = new Set();
  } else {
    const all = info.entries.map((entry, position) => bilibiliEntryIndex(entry, position));
    state.bilibili.selected = new Set(all.slice(0, MAX_BATCH));
    if (all.length > MAX_BATCH) {
      showToast(`这门课有 ${all.length} 集，一次最多排 ${MAX_BATCH} 集，已选前 ${MAX_BATCH} 集。`, 'info');
    }
  }
  elements.bilibiliEntries.querySelectorAll('input[type="checkbox"]').forEach((box) => {
    box.checked = state.bilibili.selected.has(Number(box.value));
  });
  updateBilibiliCount();
  updateStartState();
  suggestCourse();
}

function updateBilibiliCount() {
  const info = state.bilibili.info;
  const total = info && Array.isArray(info.entries) ? info.entries.length : 0;
  elements.bilibiliCount.textContent = total
    ? `（已选 ${state.bilibili.selected.size} / ${total}）`
    : '';
}

function buildPayloads() {
  const courseId = elements.courseSelect.value || '';
  if (state.sourceType === 'local') {
    return state.local.files.map((file) => ({
      source_type: 'local',
      source_ref: file.path,
      title: file.title,
      entry_index: 0,
      force: false,
      course_id: courseId
    }));
  }
  const info = state.bilibili.info;
  if (!info) {
    return [];
  }
  return selectedBilibiliEntries().map((entry) => ({
    source_type: 'bilibili',
    source_ref: state.bilibili.infoUrl,
    title: entry.title || info.title,
    entry_index: bilibiliEntryIndex(entry, 0),
    force: false,
    course_id: courseId
  }));
}

// 这个视频是否已有任务在跑（按来源与分P 精确匹配）
function activeJobFor(payload) {
  return state.jobs.find((job) => {
    if (!ACTIVE_STATUSES.has(job.status) || job.source_type !== payload.source_type) {
      return false;
    }
    if (payload.source_type === 'local') {
      return job.source_ref === payload.source_ref;
    }
    if (Number(job.entry_index || 0) !== Number(payload.entry_index || 0)) {
      return false;
    }
    const bvid = (String(payload.source_ref).match(/BV[0-9A-Za-z]{10}/) || [])[0] || '';
    return bvid ? String(job.source_ref).includes(bvid) : job.source_ref === payload.source_ref;
  }) || null;
}

function updateStartState() {
  const payloads = buildPayloads();
  const fresh = payloads.filter((payload) => !activeJobFor(payload));
  const skipped = payloads.length - fresh.length;
  const busy = elements.browseButton.dataset.busy === 'true'
    || elements.bilibiliInfoButton.dataset.busy === 'true';

  let hint = '';
  let canStart = fresh.length > 0;
  if (state.sourceType === 'local') {
    if (!state.local.files.length) {
      hint = '请先选择本地视频文件，可一次选多个。';
    } else if (!fresh.length) {
      hint = '已选视频都在生成中，可在左侧课程或生成记录里查看进度。';
    } else {
      hint = `已选 ${payloads.length} 个视频，本次生成 ${fresh.length} 个`;
    }
  } else {
    const info = state.bilibili.info;
    if (!info) {
      hint = '请先输入链接并读取视频信息。';
    } else if (!payloads.length) {
      hint = '请至少勾选一集分P。';
    } else if (!fresh.length) {
      hint = '已勾选的分P都在生成中，可在左侧课程或生成记录里查看进度。';
    } else {
      hint = `已勾选 ${payloads.length} 集，本次生成 ${fresh.length} 集`;
    }
  }
  if (canStart && skipped > 0) {
    hint += `（${skipped} 个已在生成中，将跳过）`;
  }
  if (canStart && fresh.length > 1) {
    const slots = state.system && state.system.slots;
    hint += slots
      ? `。后台按本机能力排队：转写 ${slots.transcribe} 路、画面 ${slots.prepare} 路、笔记 ${slots.summarize} 路`
      : '。后台会按本机能力自动排队。';
  } else if (canStart) {
    hint += '，点击按钮后才会开始处理。';
  }
  elements.sourceHint.textContent = hint;
  elements.startButton.textContent = fresh.length > 1
    ? `批量生成 ${fresh.length} 个笔记`
    : '开始生成总结';
  setButtonDisabled(elements.startButton, !canStart || busy);
  setButtonDisabled(elements.bilibiliInfoButton, !elements.bilibiliUrl.value.trim());
}

function startNewJob() {
  const payloads = buildPayloads().filter((payload) => !activeJobFor(payload));
  if (!payloads.length) {
    showToast('没有可生成的视频：请先选择或勾选，且它们当前不在生成中。', 'error');
    return;
  }
  if (payloads.length === 1) {
    createJob(payloads[0], elements.startButton);
    return;
  }
  createBatch(payloads, elements.startButton);
}

function createBatch(payloads, button) {
  withBusy(button, async () => {
    const items = payloads.slice(0, MAX_BATCH);
    const result = await api('/api/jobs/batch', {
      method: 'POST',
      body: JSON.stringify({ items })
    });
    const jobs = Array.isArray(result.jobs) ? result.jobs : [];
    const errors = Array.isArray(result.errors) ? result.errors : [];
    if (!jobs.length) {
      throw new ApiError(errors.length ? errors[0].message : '服务未返回有效的任务信息。');
    }
    jobs.forEach(upsertJob);
    state.selectedId = null;
    state.currentJob = null;
    state.detailRequest += 1;
    clearSourceSelection();
    await refreshJobs();
    openRecords();
    const parts = [`已创建 ${result.created || 0} 个任务`];
    if (result.reused) {
      parts.push(`${result.reused} 个已有记录直接复用`);
    }
    if (errors.length) {
      parts.push(`${errors.length} 个失败：${errors[0].message}`);
    }
    showToast(parts.join('，'), errors.length ? 'info' : 'success');
  });
}

function regenerateSelectedJob() {
  const job = state.currentJob;
  if (!job || ACTIVE_STATUSES.has(job.status)) {
    return;
  }
  createJob({
    source_type: job.source_type,
    source_ref: job.source_ref,
    title: job.title,
    entry_index: Number(job.entry_index) || 0,
    force: true,
  }, elements.regenerateJob);
}

// 标题内联编辑：点笔图标把标题换成输入框，回车保存、Esc 取消、失焦保存。
// 不用 window.prompt，WebView 里对原生对话框的支持不确定。
function startRenameJob() {
  const job = state.currentJob;
  if (!job || !job.id || elements.jobTitle.querySelector('input')) {
    return;
  }
  const original = elements.jobTitle.textContent;
  const input = document.createElement('input');
  input.className = 'input job-title-input';
  input.type = 'text';
  input.maxLength = 120;
  input.value = job.title || '';
  elements.jobTitle.textContent = '';
  elements.jobTitle.appendChild(input);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) {
      return;
    }
    settled = true;
    const value = input.value.trim();
    input.remove();
    elements.jobTitle.textContent = original;
    if (!save || !value || value === job.title) {
      return;
    }
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(job.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: value })
      });
      if (result && result.job) {
        Object.assign(job, result.job);
        upsertJob(result.job);
        elements.jobTitle.textContent = result.job.title || '未命名任务';
        const note = state.courseNotes.find((item) => item.id === job.id);
        if (note) {
          note.title = result.job.title;
        }
        refreshLists();
        showToast('标题已更新。', 'success');
      }
    } catch (error) {
      showToast(error && error.message ? error.message : String(error), 'error');
    }
  };

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}

function createJob(payload, button) {
  withBusy(button, async () => {
    let result;
    try {
      result = await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    } catch (error) {
      // 重做笔记要重新读原视频：文件被挪走/删掉时，顺手把「重出题目」这条替代路径说清楚
      if (payload.force && /本地文件不存在|请重新选择/.test(error.message || '')) {
        throw new ApiError(
          '原视频不在原位置，没法重做笔记；如果只是想换练习题，点练习题区的「重出题目」。'
        );
      }
      throw error;
    }
    if (!result.job || !result.job.id) {
      throw new ApiError('服务未返回有效的任务信息。');
    }
    state.selectedId = result.job.id;
    state.currentJob = result.job;
    state.detailRequest += 1;
    if (result.job.course_id) {
      state.currentCourseId = result.job.course_id;
    }
    upsertJob(result.job);
    updatePager();
    setView('job');
    renderCurrentJob();
    refreshLists();
    // 提交成功后立刻清空选择：表单留着上一次的视频，很容易被再点一次
    clearSourceSelection();
    if (result.reused) {
      showToast(
        ACTIVE_STATUSES.has(result.job.status)
          ? '这个视频正在生成中，已为你打开它的进度。'
          : '已读取历史总结，没有重复调用模型',
        'success'
      );
    } else {
      showToast(payload.force ? '已开始重新生成总结。' : '任务已创建，开始处理视频。', 'success');
    }
    await refreshJobs();
  });
}

function clearSourceSelection() {
  state.local = { files: [] };
  state.bilibili.info = null;
  state.bilibili.infoUrl = '';
  state.bilibili.selected = new Set();
  elements.bilibiliUrl.value = '';
  renderLocalSelection();
  renderBilibiliInfo();
  updateStartState();
  suggestCourse();
}

function upsertJob(job) {
  const index = state.jobs.findIndex((item) => item.id === job.id);
  if (index === -1) {
    state.jobs.unshift(job);
  } else {
    state.jobs[index] = { ...state.jobs[index], ...job };
  }
}

async function refreshJobs({ initial = false } = {}) {
  if (state.refreshing) {
    return;
  }
  state.refreshing = true;
  try {
    const previous = state.jobs;
    const result = await api('/api/jobs');
    state.jobs = Array.isArray(result.jobs) ? result.jobs : [];
    // 任务状态变化会影响「开始生成」能不能点，跟着刷新一次
    updateStartState();
    elements.recordsEntryMeta.textContent = state.jobs.length
      ? `共 ${state.jobs.length} 条`
      : '按时间查看';
    if (state.view === 'records') {
      renderRecords();
    }
    notifyCompleted(previous, state.jobs);
    // 停在课程页时，进度条要跟着轮询走，否则切回来只剩一个静止的数字
    if (state.view === 'course' && state.currentCourseId) {
      renderCourseActive();
    }

    if (state.selectedId) {
      const listed = state.jobs.find((job) => job.id === state.selectedId);
      if (!listed) {
        state.selectedId = null;
        state.currentJob = null;
        state.detailRequest += 1;
        if (state.view === 'job') {
          goBackFromJob();
        }
      } else {
        const current = state.currentJob;
        const needsDetail = Boolean(
          initial || !current || current.id !== state.selectedId ||
          current.transcript === undefined || current.summary === undefined ||
          ACTIVE_STATUSES.has(listed.status) || ACTIVE_STATUSES.has(current.status) ||
          listed.status !== current.status ||
          // 列表行比手里的详情新（改名、移课、重出题目）就重新拉详情，
          // 否则另一个窗口里改过的题目，这边永远看不到
          (listed.updated_at && current.updated_at && listed.updated_at !== current.updated_at)
        );
        if (needsDetail) {
          await loadJob(state.selectedId, { silent: !initial });
        } else {
          state.currentJob = { ...current, ...listed };
          renderCurrentJob();
        }
      }
    }
    state.pollErrorNotified = false;
  } catch (error) {
    if (initial || !state.pollErrorNotified) {
      showToast(error.message, 'error');
      state.pollErrorNotified = true;
    }
  } finally {
    state.refreshing = false;
  }
}

/**
 * 任务完成时告知归档位置。
 * 生成要几分钟，人通常不会盯着进度条，所以提示必须带「去哪看」。
 */
function notifyCompleted(previous, jobs) {
  if (!previous || !previous.length) {
    return;
  }
  const finished = jobs.find((job) => {
    if (job.status !== 'completed') {
      return false;
    }
    const before = previous.find((item) => item.id === job.id);
    return before && before.status !== 'completed';
  });
  if (!finished) {
    return;
  }
  const course = state.courses.find((item) => item.id === finished.course_id);
  const where = course ? `「${course.name}」` : '「杂项」';
  showToast(`已生成：${finished.title || '未命名笔记'}，归档到${where}。`, 'success');
  refreshCourses();
  if (state.view === 'course' && state.currentCourseId === finished.course_id) {
    openCourse(state.currentCourseId);
  }
}

async function loadJob(id, { silent = false } = {}) {
  const request = ++state.detailRequest;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (request !== state.detailRequest || state.selectedId !== id) {
      return null;
    }
    state.currentJob = job;
    renderCurrentJob();
    // 划重点单独取一次：笔记正文已经出来，标记可以晚一点贴上去
    loadHighlights(id);
    return job;
  } catch (error) {
    if (!silent && request === state.detailRequest) {
      showToast(error.message, 'error');
    }
    return null;
  }
}

function selectJob(id) {
  if (!id) {
    return;
  }
  const listed = state.jobs.find((job) => job.id === id);
  if (listed && listed.course_id) {
    state.currentCourseId = listed.course_id;
  }
  openNote(id);
}

// 任务状态变化后统一刷新：课程计数、生成记录、以及当前打开的课程索引
function refreshLists() {
  refreshCourses();
  if (state.view === 'records') {
    renderRecords();
  }
  if (state.view === 'course' && state.currentCourseId) {
    openCourse(state.currentCourseId);
  }
}

/* ---------- 课程导航 ---------- */

function setView(view) {
  state.view = view;
  // 首页和生成记录都不属于任何课程，清掉选中课程，
  // 否则左栏会残留一门高亮的课程，看起来像还停在它上面。
  if (view === 'home' || view === 'records') {
    state.currentCourseId = '';
  }
  // 读笔记或看记录时把「新建任务」收成一行：半屏高度下，展开的表单会把正文挤到折叠线以下
  setSourceCollapsed(view !== 'home');
  elements.jobEmpty.hidden = view !== 'home';
  elements.courseView.hidden = view !== 'course';
  elements.recordsView.hidden = view !== 'records';
  elements.searchView.hidden = view !== 'search';
  elements.jobDetail.hidden = !(view === 'job' && state.selectedId && state.currentJob);
  elements.recordsOpen.classList.toggle('is-active', view === 'records');
  renderCourses();
}

function setSourceCollapsed(collapsed) {
  elements.sourceCard.classList.toggle('is-collapsed', Boolean(collapsed));
  elements.sourceToggle.textContent = collapsed ? '展开' : '收起';
  elements.sourceToggle.setAttribute('aria-expanded', String(!collapsed));
  elements.sourceToggle.title = collapsed
    ? '展开新建任务面板'
    : '收起新建任务面板，给笔记腾出空间';
}

async function refreshCourses({ initial = false } = {}) {
  if (state.coursesRefreshing) {
    return;
  }
  state.coursesRefreshing = true;
  try {
    const result = await api('/api/courses');
    state.courses = Array.isArray(result.courses) ? result.courses : [];
    state.miscId = result.misc_id || 'misc';
    renderCourses();
    renderCourseSelect();
    elements.courseRefreshStatus.textContent = '每 2 秒自动刷新';
    elements.courseRefreshStatus.classList.remove('is-error');
  } catch (error) {
    elements.courseRefreshStatus.textContent = '更新失败，将自动重试';
    elements.courseRefreshStatus.classList.add('is-error');
    if (initial) {
      showToast(error.message, 'error');
    }
  } finally {
    state.coursesRefreshing = false;
  }
}

function renderCourses() {
  const courses = state.courses;
  elements.courseCount.textContent = String(courses.length);
  elements.courseEmpty.hidden = courses.length > 0;
  elements.courseList.hidden = courses.length === 0;
  const fragment = document.createDocumentFragment();
  courses.forEach((course) => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'course-item';
    button.classList.toggle('is-active', course.id === state.currentCourseId);
    button.setAttribute('aria-label', `查看课程：${course.name}`);

    const name = document.createElement('div');
    name.className = 'course-name';
    const label = document.createElement('span');
    label.textContent = course.name;
    name.appendChild(label);
    if (course.unread > 0) {
      const dot = document.createElement('span');
      dot.className = 'course-unread';
      dot.title = `${course.unread} 条新笔记`;
      name.appendChild(dot);
    }

    const meta = document.createElement('div');
    meta.className = 'course-meta';
    const running = courseActiveJobs(course.id).length;
    const bits = [course.note_count > 0 ? `${course.note_count} 条笔记` : '暂无笔记'];
    if (running) {
      bits.push(`${running} 个生成中`);
      meta.classList.add('is-running');
    }
    meta.textContent = bits.join(' · ');

    button.append(name, meta);
    button.addEventListener('click', () => openCourse(course.id));
    item.appendChild(button);
    fragment.appendChild(item);
  });
  elements.courseList.replaceChildren(fragment);
  elements.recordsEntryMeta.textContent = state.jobs.length
    ? `共 ${state.jobs.length} 条`
    : '按时间查看';
}

async function openCourse(courseId) {
  state.currentCourseId = courseId;
  state.selectedId = null;
  state.currentJob = null;
  state.currentModule = '';
  renderCourses();
  setView('course');
  elements.courseTitle.textContent = '读取中…';
  elements.courseMeta.textContent = '';
  elements.courseNotes.replaceChildren();
  elements.courseActive.replaceChildren();
  elements.courseNotesEmpty.hidden = true;
  try {
    const result = await api(`/api/courses/${encodeURIComponent(courseId)}`);
    state.currentCourse = result.course;
    state.courseNotes = Array.isArray(result.notes) ? result.notes : [];
    renderCourseView();
    refreshCourses();
    loadUsageSummary();
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function renderCourseView() {
  const course = state.currentCourse;
  if (!course) {
    return;
  }
  elements.courseTitle.textContent = course.name;
  elements.courseMore.hidden = course.kind === 'misc';
  renderCourseActive();
  renderCourseNotes();
}

/**
 * 正在生成的任务：课程页顶部单独一栏。
 * 进度直接取 state.jobs（每 2 秒轮询），所以切换课程再回来仍能看到，
 * 不会因为「课程笔记接口只返回已完成」而变成空状态。
 */
function courseActiveJobs(courseId = state.currentCourseId) {
  if (!courseId) {
    return [];
  }
  const seen = new Set();
  return state.jobs.filter((job) => {
    if (job.course_id !== courseId || !ACTIVE_STATUSES.has(job.status)) {
      return false;
    }
    const key = job.cache_key || job.id;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function renderCourseActive() {
  const course = state.currentCourse;
  if (!course) {
    return;
  }
  const active = courseActiveJobs();
  const total = state.courseNotes.length;
  const order = course.kind === 'misc' ? '按时间排序' : '按章节排序';
  const parts = [];
  if (total) {
    parts.push(`${total} 条笔记`);
  }
  if (active.length) {
    parts.push(`${active.length} 个生成中`);
  }
  elements.courseMeta.textContent = parts.length ? `${parts.join(' · ')} · ${order}` : '暂无笔记';
  // 有任务在跑时不能显示「还没有笔记」，否则会把生成中的状态盖掉
  elements.courseNotesEmpty.hidden = total > 0 || active.length > 0;

  const fragment = document.createDocumentFragment();
  active.forEach((job) => fragment.appendChild(activeJobRow(job)));
  elements.courseActive.replaceChildren(fragment);
}

function activeJobRow(job) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'active-job';
  row.title = '打开生成详情';
  row.addEventListener('click', () => openNote(job.id));

  const head = document.createElement('div');
  head.className = 'active-job-head';
  if (job.seq) {
    const seq = document.createElement('span');
    seq.className = 'active-job-seq';
    seq.textContent = job.seq;
    head.appendChild(seq);
  }
  const title = document.createElement('span');
  title.className = 'active-job-title';
  // 和笔记卡片一样去掉标题里重复的章节号（「2-1」+「2-1 进程」）
  title.textContent = noteTitle(job);
  head.appendChild(title);
  const percent = document.createElement('span');
  percent.className = 'active-job-pct';
  percent.textContent = `${normalizeProgress(job.progress)}%`;
  head.appendChild(percent);

  const track = document.createElement('div');
  track.className = 'active-job-track';
  const bar = document.createElement('span');
  bar.style.width = `${normalizeProgress(job.progress)}%`;
  track.appendChild(bar);

  const stage = document.createElement('div');
  stage.className = 'active-job-stage';
  stage.textContent = job.stage || STATUS_LABELS[job.status] || '处理中';

  row.append(head, track, stage);
  return row;
}

function renderCourseNotes() {
  const fragment = document.createDocumentFragment();
  let lastChapter = null;
  state.courseNotes.forEach((note) => {
    const chapter = note.seq ? String(note.seq).split('-')[0] : '';
    if (chapter && chapter !== lastChapter) {
      const heading = document.createElement('p');
      heading.className = 'course-chapter';
      heading.textContent = `第 ${chapter} 章`;
      fragment.appendChild(heading);
      lastChapter = chapter;
    }
    fragment.appendChild(noteCard(note));
  });
  elements.courseNotes.replaceChildren(fragment);
}

// 卡片标题去掉重复的章节号：`1-2 操作系统的发展历程` + `1-2` 会读成两遍
function noteTitle(note) {
  const title = note.title || '未命名笔记';
  if (!note.seq) {
    return title;
  }
  const [major, minor] = String(note.seq).split('-');
  const pattern = new RegExp(`^\\s*p?${major}\\s*[-–—．.]\\s*${minor}\\s*`);
  return title.replace(pattern, '').trim() || title;
}

function noteCard(note) {
  const card = document.createElement('div');
  card.className = 'note-card';
  card.dataset.jobId = note.id;
  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.setAttribute('aria-label', `打开笔记：${noteTitle(note)}`);
  card.addEventListener('click', (event) => {
    if (event.target.closest('.note-more')) {
      return;
    }
    openNote(note.id);
  });
  card.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openNote(note.id);
    }
  });

  const seq = document.createElement('span');
  seq.className = 'note-seq';
  seq.textContent = note.seq || '';

  const title = document.createElement('span');
  title.className = 'note-title';
  title.textContent = noteTitle(note);

  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'note-more';
  more.textContent = '⋯';
  more.title = '重命名 / 移到其他课程';
  more.setAttribute('aria-label', '更多操作');
  more.addEventListener('click', (event) => {
    event.stopPropagation();
    openNoteMenu(note.id, more);
  });

  card.append(seq, title);
  if (note.wrong_total > 0) {
    const badge = document.createElement('span');
    badge.className = 'note-badge';
    badge.textContent = `错题 ×${note.wrong_total}`;
    card.appendChild(badge);
  }
  card.appendChild(more);
  return card;
}

function openNote(jobId) {
  state.selectedId = jobId;
  state.currentModule = '';
  state.renderedSummaryJob = '';
  state.renderedSummary = '';
  state.renderedVisualJob = '';
  state.renderedVisual = '';
  if (state.highlights.jobId !== jobId) {
    state.highlights = { jobId: '', summary: [], visual: [] };
    state.highlightPositions = { summary: [], visual: [] };
  }
  updatePager();
  setView('job');
  renderCurrentJob();
  return loadJob(jobId);
}

function updatePager() {
  const index = state.courseNotes.findIndex((note) => note.id === state.selectedId);
  const inCourse = index >= 0;
  elements.prevNote.disabled = !inCourse || index <= 0;
  elements.nextNote.disabled = !inCourse || index >= state.courseNotes.length - 1;
  elements.jobBack.hidden = !state.currentCourseId;
}

function goBackFromJob() {
  state.selectedId = null;
  state.currentJob = null;
  if (state.currentCourseId) {
    openCourse(state.currentCourseId);
  } else {
    setView('home');
    renderCourses();
  }
}

function stepNote(delta) {
  const index = state.courseNotes.findIndex((note) => note.id === state.selectedId);
  const target = state.courseNotes[index + delta];
  if (target) {
    openNote(target.id);
  }
}

/* ---------- 生成记录 ---------- */

function openRecords() {
  setView('records');
  renderRecords();
  loadUsageSummary(true);
}

function renderRecords() {
  const jobs = state.jobs;
  const active = jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).length;
  elements.recordsMeta.textContent = jobs.length
    ? `共 ${jobs.length} 次生成${active ? ` · ${active} 个进行中` : ''}，按时间倒序`
    : '还没有生成记录';
  const fragment = document.createDocumentFragment();
  if (jobs.length) {
    const head = document.createElement('div');
    head.className = 'records-row is-head';
    ['时间', '标题', '耗时', '用量 / 状态'].forEach((text) => {
      const cell = document.createElement('span');
      cell.textContent = text;
      head.appendChild(cell);
    });
    fragment.appendChild(head);
  }
  jobs.forEach((job) => {
    const row = document.createElement('div');
    row.className = 'records-row';
    row.classList.toggle('is-failed', job.status === 'failed');

    const time = document.createElement('span');
    time.className = 'records-num';
    time.style.textAlign = 'left';
    time.textContent = formatDate(job.created_at, true);

    const title = document.createElement('span');
    title.className = 'records-title';
    title.textContent = job.title || '未命名任务';
    // 批量生成时这一列能看出每个任务卡在哪一步（排队 / 转写 / 出笔记）
    if (ACTIVE_STATUSES.has(job.status) && job.stage) {
      const stage = document.createElement('span');
      stage.className = 'records-stage';
      stage.textContent = job.stage;
      title.appendChild(stage);
    }

    const cost = document.createElement('span');
    cost.className = 'records-num';
    cost.textContent = jobDuration(job) || '—';

    const tail = document.createElement('span');
    tail.className = 'records-tail';
    const tailText = document.createElement('span');
    tailText.className = 'records-num';
    tailText.textContent = `${formatTokens(job.tokens || 0)} · ${STATUS_LABELS[job.status] || job.status}`;
    tail.appendChild(tailText);
    if (ACTIVE_STATUSES.has(job.status)) {
      // 误触重复生成时，这里能直接掐掉，不用再点进详情
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'records-cancel';
      cancel.textContent = '取消';
      cancel.title = '停止这个任务，已完成的转写会保留';
      cancel.addEventListener('click', (event) => {
        event.stopPropagation();
        cancelJobById(job.id, cancel);
      });
      tail.appendChild(cancel);
    }

    row.append(time, title, cost, tail);
    if (job.summary_chars > 0) {
      row.style.cursor = 'pointer';
      row.addEventListener('click', () => openRecord(job));
    }
    fragment.appendChild(row);
  });
  elements.recordsList.replaceChildren(fragment);
}

function jobDuration(job) {
  const start = Date.parse(job.created_at || '');
  const end = Date.parse(job.finished_at || job.updated_at || '');
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return '';
  }
  return formatDuration((end - start) / 1000);
}

async function openRecord(job) {
  if (job.course_id && job.course_id !== state.currentCourseId) {
    await openCourse(job.course_id);
  }
  openNote(job.id);
}

/* ---------- 笔记卡片菜单 ---------- */

function closeNoteMenu() {
  elements.cardMenu.hidden = true;
  state.noteMenuJobId = '';
}

function menuItem(text, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'text-menu-item';
  button.textContent = text;
  button.addEventListener('click', () => {
    closeNoteMenu();
    action();
  });
  return button;
}

function openNoteMenu(jobId, anchor) {
  state.noteMenuJobId = jobId;
  const menu = elements.cardMenu;
  const fragment = document.createDocumentFragment();
  fragment.appendChild(menuItem('重命名标题', () => startRenameNote(jobId)));

  const label = document.createElement('div');
  label.className = 'card-menu-sep';
  fragment.appendChild(label);
  const hint = document.createElement('div');
  hint.className = 'card-menu-hint';
  hint.textContent = '移到课程';
  fragment.appendChild(hint);

  state.courses.forEach((course) => {
    if (course.id === state.currentCourseId) {
      return;
    }
    fragment.appendChild(menuItem(course.name, () => moveNote(jobId, course.id, course.name)));
  });

  menu.replaceChildren(fragment);
  menu.hidden = false;
  positionCardMenu(anchor);
}

// 菜单贴着按钮弹出，靠近窗口下缘时向上翻，避免被裁掉
function positionCardMenu(anchor) {
  const menu = elements.cardMenu;
  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.min(rect.right - width + 4, window.innerWidth - width - 12);
  const top = rect.bottom + 6 + height > window.innerHeight - 12
    ? Math.max(12, rect.top - height - 6)
    : rect.bottom + 6;
  menu.style.left = `${Math.max(12, left)}px`;
  menu.style.top = `${top}px`;
}

function openCourseMenu(anchor) {
  const course = state.currentCourse;
  if (!course) {
    return;
  }
  const fragment = document.createDocumentFragment();
  fragment.appendChild(menuItem('重命名课程', startRenameCourse));

  const sep = document.createElement('div');
  sep.className = 'card-menu-sep';
  fragment.appendChild(sep);

  if (course.kind !== 'misc') {
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'text-menu-item is-danger';
    remove.textContent = '删除课程';
    fragment.appendChild(remove);

    // 提示跟在「删除课程」下面，否则夹在两个菜单项中间会被读成重命名的说明
    const notes = state.courseNotes.length;
    const hint = document.createElement('div');
    hint.className = 'card-menu-hint';
    hint.textContent = notes ? `删除后 ${notes} 条笔记会移到「杂项」` : '这门课没有笔记';
    fragment.appendChild(hint);

    // 二次确认：菜单原地换成确认项，避免手滑直接删掉
    remove.addEventListener('click', () => {
      const confirmFragment = document.createDocumentFragment();
      const tip = document.createElement('div');
      tip.className = 'card-menu-hint';
      tip.textContent = `确认删除「${course.name}」？`;
      confirmFragment.appendChild(tip);
      confirmFragment.appendChild(menuItem('确认删除', () => removeCourse(course)));
      confirmFragment.appendChild(menuItem('取消', () => {}));
      elements.cardMenu.replaceChildren(confirmFragment);
    });
  }

  elements.cardMenu.replaceChildren(fragment);
  elements.cardMenu.hidden = false;
  positionCardMenu(anchor);
}

async function removeCourse(course) {
  try {
    const result = await api(`/api/courses/${encodeURIComponent(course.id)}`, {
      method: 'DELETE'
    });
    showToast(
      result.moved ? `课程已删除，${result.moved} 条笔记移到「杂项」。` : '课程已删除。',
      'success'
    );
    state.currentCourseId = '';
    state.currentCourse = null;
    state.courseNotes = [];
    state.selectedId = null;
    state.currentJob = null;
    await refreshCourses();
    setView('home');
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function moveNote(jobId, courseId, courseName) {
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ course_id: courseId })
    });
    showToast(`已移到「${courseName}」。`, 'success');
    await refreshCourses();
    if (state.currentCourseId) {
      openCourse(state.currentCourseId);
    }
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function startRenameNote(jobId) {
  const card = elements.courseNotes.querySelector(`[data-job-id="${jobId}"]`);
  const titleNode = card && card.querySelector('.note-title');
  if (!titleNode || titleNode.querySelector('input')) {
    return;
  }
  const note = state.courseNotes.find((item) => item.id === jobId);
  const original = titleNode.textContent;
  const input = document.createElement('input');
  input.className = 'input note-title-input';
  input.type = 'text';
  input.maxLength = 120;
  input.value = note ? note.title : original;
  titleNode.textContent = '';
  titleNode.appendChild(input);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) {
      return;
    }
    settled = true;
    const value = input.value.trim();
    input.remove();
    titleNode.textContent = original;
    if (!save || !value || !note || value === note.title) {
      return;
    }
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: value })
      });
      if (result.job) {
        note.title = result.job.title;
        note.seq = result.job.seq || note.seq;
        renderCourseView();
        showToast('标题已更新。', 'success');
      }
    } catch (error) {
      showToast(error.message, 'error');
    }
  };

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}

/* ---------- 课程重命名 / 课前选择 ---------- */

function startRenameCourse() {
  const course = state.currentCourse;
  const row = elements.courseTitle.parentElement;
  if (!course || row.querySelector('input')) {
    return;
  }
  const original = course.name;
  const input = document.createElement('input');
  input.className = 'input note-title-input';
  input.type = 'text';
  input.maxLength = 120;
  input.value = original;
  elements.courseTitle.hidden = true;
  row.insertBefore(input, elements.courseTitle);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) {
      return;
    }
    settled = true;
    const value = input.value.trim();
    input.remove();
    elements.courseTitle.hidden = false;
    if (!save || !value || value === original) {
      return;
    }
    try {
      const result = await api(`/api/courses/${encodeURIComponent(course.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ name: value })
      });
      if (result.course) {
        state.currentCourse = { ...course, ...result.course };
        elements.courseTitle.textContent = result.course.name;
        await refreshCourses();
        showToast('课程已重命名。', 'success');
      }
    } catch (error) {
      showToast(error.message, 'error');
    }
  };

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}

function renderCourseSelect() {
  const select = elements.courseSelect;
  if (!select) {
    return;
  }
  const signature = state.courses.map((course) => `${course.id}:${course.name}`).join('|');
  if (signature === state.courseSelectSignature) {
    return;
  }
  state.courseSelectSignature = signature;
  const previous = select.value;
  const previousAuto = select.querySelector('option[data-auto="1"]');
  const autoLabel = previousAuto ? previousAuto.textContent : '自动识别';
  const fragment = document.createDocumentFragment();
  const auto = document.createElement('option');
  auto.value = '';
  auto.dataset.auto = '1';
  // 轮询会每 2 秒重建一次，自动项的文字要保留，否则「自动：某课程」会被刷回默认
  auto.textContent = autoLabel;
  fragment.appendChild(auto);
  state.courses.forEach((course) => {
    const option = document.createElement('option');
    option.value = course.id;
    option.textContent = course.name;
    fragment.appendChild(option);
  });
  select.replaceChildren(fragment);
  if (previous && Array.from(select.options).some((option) => option.value === previous)) {
    select.value = previous;
  }
}

async function suggestCourse() {
  const auto = elements.courseSelect.querySelector('option[data-auto="1"]');
  if (!auto) {
    return;
  }
  const payloads = buildPayloads();
  const payload = payloads[0];
  if (!payload || !payload.source_ref) {
    auto.textContent = '自动识别';
    return;
  }
  try {
    const query = new URLSearchParams({
      source_type: payload.source_type,
      source_ref: payload.source_ref,
      entry_index: String(payload.entry_index || 0),
      title: payload.title || ''
    });
    const result = await api(`/api/courses/suggest?${query.toString()}`);
    const suffix = payloads.length > 1 ? ` 等 ${payloads.length} 个` : '';
    if (elements.courseSelect.value !== '') {
      // 用户手动选过课程就尊重它，不要被自动建议覆盖
      return;
    }
    // 只改「自动」那一项的文字，不把下拉框钉到某门课上：
    // 一旦 value 被写成具体课程，它就不再是自动模式，下一批别的视频会跟着归错课程。
    auto.textContent = result.name ? `自动：${result.name}${suffix}` : '自动：杂项';
  } catch (error) {
    auto.textContent = '自动识别';
  }
}

function endNewCourse(input) {
  if (input && input.isConnected) {
    input.remove();
  }
  elements.courseSelect.hidden = false;
  elements.courseNew.textContent = '新建';
}

// 不用 window.prompt：WebView 里对原生弹窗的支持不确定，内联输入更稳。
function beginNewCourse() {
  const row = elements.courseSelect.parentElement;
  const existing = row.querySelector('.course-new-input');
  if (existing) {
    endNewCourse(existing);
    return;
  }
  const input = document.createElement('input');
  input.className = 'input course-new-input';
  input.type = 'text';
  input.maxLength = 120;
  input.placeholder = '课程名，回车创建';
  elements.courseSelect.hidden = true;
  row.insertBefore(input, elements.courseSelect);
  elements.courseNew.textContent = '取消';
  input.focus();

  let settled = false;
  const finish = async (save) => {
    if (settled) {
      return;
    }
    const name = input.value.trim();
    if (!save || !name) {
      settled = true;
      endNewCourse(input);
      return;
    }
    settled = true;
    try {
      const result = await api('/api/courses', {
        method: 'POST',
        body: JSON.stringify({ name })
      });
      endNewCourse(input);
      await refreshCourses();
      if (result.course) {
        elements.courseSelect.value = result.course.id;
      }
      showToast(`已创建课程「${name}」。`, 'success');
    } catch (error) {
      settled = false;
      showToast(error.message, 'error');
    }
  };

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}

function createStatusBadge(status) {
  const badge = document.createElement('span');
  badge.className = `status-badge ${statusClass(status)}`;
  badge.textContent = STATUS_LABELS[status] || status || '未知';
  return badge;
}

function statusClass(status) {
  return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) ? `status-${status}` : 'status-queued';
}

function renderCurrentJob() {
  const job = state.currentJob;
  const hasJob = Boolean(state.selectedId && job);
  // 主区域显示哪一块由 setView 决定；这里只更新内容。
  // 否则轮询每 2 秒跑一次，会把笔记面板重新显示到课程页/生成记录页上。
  if (state.view === 'home' || state.view === 'job') {
    elements.jobEmpty.hidden = state.view !== 'home';
    elements.jobDetail.hidden = !(state.view === 'job' && hasJob);
  }
  if (!hasJob) {
    return;
  }

  // 正在重命名时不要覆盖：轮询每 2 秒跑一次，直接写 textContent 会把输入框冲掉
  if (!elements.jobTitle.querySelector('input')) {
    elements.jobTitle.textContent = job.title || '未命名任务';
  }
  elements.jobCreated.textContent = `创建于 ${formatDate(job.created_at, true)}`;
  renderJobUsage(job.usage);
  elements.jobStatus.className = `status-badge ${statusClass(job.status)}`;
  elements.jobStatus.textContent = STATUS_LABELS[job.status] || job.status || '未知';
  elements.jobProvider.hidden = !job.provider;
  elements.jobProvider.textContent = PROVIDER_LABELS[job.provider] || job.provider || '';
  elements.jobStage.textContent = job.stage || STATUS_LABELS[job.status] || '等待处理';

  const progress = normalizeProgress(job.progress);
  elements.jobProgress.value = progress;
  elements.jobProgress.textContent = `${progress}%`;
  elements.jobProgressText.textContent = `${progress}%`;
  elements.jobProgressSection.classList.toggle('is-done', job.status === 'completed');
  elements.cancelJob.hidden = !ACTIVE_STATUSES.has(job.status);

  const waiting = job.status === 'waiting_confirmation';
  elements.confirmationBox.hidden = !waiting;
  const transcriptChars = job.transcript ? job.transcript.length : Number(job.transcript_chars) || 0;
  elements.transcriptCount.textContent = transcriptChars.toLocaleString('zh-CN');

  elements.errorBox.hidden = !(job.status === 'failed' && job.error);
  elements.jobError.textContent = job.error || '';

  const summary = typeof job.summary === 'string' ? job.summary : '';
  const completed = job.status === 'completed' && Boolean(summary);
  elements.resultSection.hidden = !completed;
  if (summary && (state.renderedSummaryJob !== job.id || state.renderedSummary !== summary)) {
    renderNoteHtml('summary', summary, job.id);
  }
  if (completed) {
    syncModuleState(job);
    handlePendingLocate(job);
  }
  const staleBackend = completed && state.backendFeatures !== null && !backendSupports('questions');
  elements.backendStale.hidden = !staleBackend;

  const visualNotes = typeof job.visual_notes === 'string' ? job.visual_notes : '';
  elements.visualDetails.hidden = !visualNotes;
  if (!visualNotes) {
    if (state.renderedVisual) {
      elements.visualContent.innerHTML = '';
    }
    state.renderedVisualJob = '';
    state.renderedVisual = '';
  } else if (state.renderedVisualJob !== job.id || state.renderedVisual !== visualNotes) {
    renderNoteHtml('visual', visualNotes, job.id);
  }

  const transcript = typeof job.transcript === 'string' ? job.transcript : '';
  elements.transcriptDetails.hidden = !transcript;
  elements.transcriptContent.textContent = transcript;
  setButtonDisabled(elements.exportTranscript, !transcript);
  elements.regenerateJob.hidden = ACTIVE_STATUSES.has(job.status);
  setButtonDisabled(elements.regenerateJob, ACTIVE_STATUSES.has(job.status));
  setButtonDisabled(elements.deleteJob, ACTIVE_STATUSES.has(job.status));
  elements.deleteJob.title = ACTIVE_STATUSES.has(job.status) ? '请先取消任务，再删除历史。' : '';
}

function normalizeProgress(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, Math.round(number))) : 0;
}

// ---------- 学习成果：模块导航与练习题 ----------

const QUESTION_TYPE_LABELS = { single: '单选', judge: '判断', short: '简答', blank: '填空' };
const DIFFICULTY_LABELS = { easy: '基础', medium: '理解', hard: '辨析' };

// 0=未标记  1=注意  2=易错  3=重点复习
const MASTERY_TIERS = [
  { level: 0, label: '', tone: 'none' },
  { level: 1, label: '注意', tone: 'low' },
  { level: 2, label: '易错', tone: 'mid' },
  { level: 3, label: '重点复习', tone: 'high' }
];

function masteryTier(count) {
  const value = Math.max(0, Math.floor(Number(count) || 0));
  return MASTERY_TIERS[Math.min(value, 3)];
}

function parseJobQuestions(job) {
  if (!job || typeof job.questions !== 'string' || !job.questions) {
    return [];
  }
  try {
    const parsed = JSON.parse(job.questions);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_error) {
    return [];
  }
}

function wrongCountOf(job, index) {
  const stats = job && job.question_stats;
  if (!stats || typeof stats !== 'object') {
    return 0;
  }
  return Number(stats[String(index)]) || 0;
}

function quizRenderKey(job) {
  return [
    job.id,
    // 题目被重出后题量可能不变，只看数量会让页面一直显示旧题
    job.updated_at || '',
    parseJobQuestions(job).length,
    state.quizSortByLevel ? 'sorted' : 'plain'
  ].join(':');
}

function applyModuleVisibility() {
  const job = state.currentJob;
  const hasNotes = Boolean(job && typeof job.summary === 'string' && job.summary);
  const module = state.currentModule;
  elements.moduleNav.hidden = Boolean(module);
  elements.moduleNotes.hidden = module !== 'notes' || !hasNotes;
  elements.moduleQuiz.hidden = module !== 'quiz';
}

function syncModuleState(job) {
  if (state.moduleJobId !== job.id) {
    state.moduleJobId = job.id;
    state.currentModule = '';
    state.renderedQuizKey = '';
    applyModuleVisibility();
  }
  updateModuleNav(job);
  if (state.currentModule === 'quiz') {
    const key = quizRenderKey(job);
    if (state.renderedQuizKey !== key) {
      renderQuiz();
      state.renderedQuizKey = key;
    }
  }
}

function switchModule(name) {
  state.currentModule = name || '';
  applyModuleVisibility();
  if (state.currentModule === 'notes') {
    const job = state.currentJob;
    if (job && typeof job.summary === 'string' && job.summary) {
      renderNoteHtml('summary', job.summary, job.id);
    }
  }
  if (state.currentModule === 'quiz') {
    renderQuiz();
    state.renderedQuizKey = state.currentJob ? quizRenderKey(state.currentJob) : '';
  }
  if (state.currentModule && elements.resultSection.scrollIntoView) {
    elements.resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function updateModuleNav(job = state.currentJob) {
  if (!job) {
    return;
  }
  const summary = typeof job.summary === 'string' ? job.summary : '';
  elements.navNotesMeta.textContent = summary
    ? `${summary.length.toLocaleString('zh-CN')} 字`
    : '暂无内容';

  const questions = parseJobQuestions(job);
  if (!questions.length) {
    elements.navQuizMeta.textContent = '尚未生成';
    return;
  }
  const wrongs = questions.map((_, index) => wrongCountOf(job, index));
  const weak = wrongs.filter((value) => value >= 2).length;
  const tried = wrongs.filter((value) => value > 0).length;
  if (weak) {
    elements.navQuizMeta.textContent = `${questions.length} 道题 · ${weak} 道需重点复习`;
  } else if (tried) {
    elements.navQuizMeta.textContent = `${questions.length} 道题 · ${tried} 道有错题记录`;
  } else {
    elements.navQuizMeta.textContent = `${questions.length} 道题 · 尚未作答`;
  }
}

function renderQuiz() {
  const job = state.currentJob;
  const questions = parseJobQuestions(job);
  elements.quizList.innerHTML = '';
  elements.quizEmpty.hidden = questions.length > 0;
  elements.quizRegenerate.hidden = questions.length === 0;
  elements.quizSortByLevel.checked = state.quizSortByLevel;

  if (!questions.length) {
    elements.quizEmptyText.textContent = job && job.summary
      ? '这条笔记生成时还没有练习题，可以现在补一份。'
      : '这条笔记还没有练习题。';
    return;
  }

  const order = questions.map((question, index) => ({ question, index }));
  if (state.quizSortByLevel) {
    order.sort((left, right) => {
      const diff = wrongCountOf(job, right.index) - wrongCountOf(job, left.index);
      return diff !== 0 ? diff : left.index - right.index;
    });
  }
  elements.quizList.innerHTML = order
    .map(({ question, index }) => quizCardHtml(question, index, wrongCountOf(job, index)))
    .join('');
}

function quizCardHtml(question, index, wrongCount) {
  const tier = masteryTier(wrongCount);
  const type = QUESTION_TYPE_LABELS[question.type] || '简答';
  const difficulty = DIFFICULTY_LABELS[question.difficulty] || '理解';
  const options = Array.isArray(question.options) && question.options.length
    ? `<ul class="quiz-options">${question.options.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
    : '';
  const badge = tier.label
    ? `<span class="quiz-badge quiz-badge-${tier.tone}">${tier.label} · ${wrongCount} 次</span>`
    : '<span class="quiz-badge quiz-badge-none">未标记</span>';
  const topic = question.topic ? `<span class="quiz-topic">${escapeHtml(question.topic)}</span>` : '';
  const explanation = question.explanation
    ? `<p class="quiz-answer-line"><span class="quiz-answer-label">解析</span><span class="quiz-answer-text">${escapeHtml(question.explanation)}</span></p>`
    : '';
  return `
    <article class="quiz-card quiz-level-${tier.level}" data-index="${index}">
      <header class="quiz-card-head">
        <span class="quiz-no">${String(index + 1).padStart(2, '0')}</span>
        ${badge}
        <span class="quiz-tag">${type}</span>
        <span class="quiz-tag quiz-tag-${question.difficulty}">${difficulty}</span>
        ${topic}
      </header>
      <p class="quiz-question">${escapeHtml(question.question)}</p>
      ${options}
      <div class="quiz-card-actions">
        <button class="button button-secondary button-small quiz-toggle" type="button" data-index="${index}">查看答案</button>
        <div class="quiz-wrong-group">
          <button class="button button-small quiz-wrong" type="button" data-index="${index}" title="答错了就点一次，累计记录">答错 +1</button>
          <button class="button button-small quiz-wrong-undo" type="button" data-index="${index}" title="撤销一次误点" aria-label="撤销一次误点">−1</button>
        </div>
      </div>
      <div class="quiz-answer" hidden>
        <p class="quiz-answer-line"><span class="quiz-answer-label">答案</span><span class="quiz-answer-text">${escapeHtml(question.answer)}</span></p>
        ${explanation}
      </div>
    </article>`;
}

function handleQuizClick(event) {
  const toggle = event.target.closest('.quiz-toggle');
  if (toggle) {
    const card = toggle.closest('.quiz-card');
    const answer = card.querySelector('.quiz-answer');
    const expanded = !answer.hidden;
    answer.hidden = expanded;
    toggle.textContent = expanded ? '查看答案' : '收起答案';
    card.classList.toggle('is-open', !expanded);
    return;
  }
  const wrong = event.target.closest('.quiz-wrong');
  if (wrong) {
    bumpQuestionWrong(Number(wrong.dataset.index), 1);
    return;
  }
  const undo = event.target.closest('.quiz-wrong-undo');
  if (undo) {
    bumpQuestionWrong(Number(undo.dataset.index), -1);
  }
}

function handleApiFailure(error) {
  if (error && error.status === 404) {
    // 路由不存在 = 后端进程还没加载新代码。直接把结论告诉用户，别只丢一句 Not Found。
    state.backendFeatures = [];
    elements.backendStale.hidden = false;
    showToast('后端还没有这个接口，请完全退出应用（含托盘图标）后重新打开。', 'error');
    return;
  }
  showToast(error && error.message ? error.message : String(error), 'error');
}

async function bumpQuestionWrong(index, delta) {
  const job = state.currentJob;
  if (!job || !Number.isInteger(index)) {
    return;
  }
  try {
    const result = await api(
      `/api/jobs/${encodeURIComponent(job.id)}/questions/${index}/wrong?delta=${delta}`,
      { method: 'POST' }
    );
    const count = Number(result.wrong_count) || 0;
    if (!job.question_stats || typeof job.question_stats !== 'object') {
      job.question_stats = {};
    }
    job.question_stats[String(index)] = count;
    refreshQuizCard(index, count);
    updateModuleNav(job);
  } catch (error) {
    handleApiFailure(error);
  }
}

function refreshQuizCard(index, wrongCount) {
  const card = elements.quizList.querySelector(`.quiz-card[data-index="${index}"]`);
  if (!card) {
    return;
  }
  const tier = masteryTier(wrongCount);
  card.classList.remove('quiz-level-0', 'quiz-level-1', 'quiz-level-2', 'quiz-level-3');
  card.classList.add(`quiz-level-${tier.level}`);
  const head = card.querySelector('.quiz-card-head');
  const existing = head.querySelector('.quiz-badge');
  const badge = document.createElement('span');
  badge.className = tier.label ? `quiz-badge quiz-badge-${tier.tone}` : 'quiz-badge quiz-badge-none';
  badge.textContent = tier.label ? `${tier.label} · ${wrongCount} 次` : '未标记';
  if (existing) {
    existing.replaceWith(badge);
  } else {
    const anchor = head.querySelector('.quiz-no');
    if (anchor) {
      anchor.insertAdjacentElement('afterend', badge);
    }
  }
  // 计数变化时让徽章跳一下，点击有明确反馈。
  badge.classList.add('is-bump');
  window.setTimeout(() => badge.classList.remove('is-bump'), 420);
}

async function requestQuestions(button) {
  const job = state.currentJob;
  if (!job || state.quizBusy) {
    return;
  }
  state.quizBusy = true;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '正在出题…';
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(job.id)}/questions`, { method: 'POST' });
    const questions = Array.isArray(result.questions) ? result.questions : [];
    job.questions = JSON.stringify(questions);
    job.question_stats = {};
    renderQuiz();
    state.renderedQuizKey = quizRenderKey(job);
    updateModuleNav(job);
    showToast(`已生成 ${questions.length} 道练习题。`, 'success');
  } catch (error) {
    handleApiFailure(error);
  } finally {
    state.quizBusy = false;
    button.disabled = false;
    button.textContent = label;
  }
}

async function cancelJobById(jobId, button) {
  await withBusy(button, async () => {
    const updated = await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
    upsertJob(updated);
    showToast('已请求取消任务。', 'success');
    await refreshJobs();
  });
}

function cancelSelectedJob() {
  const job = state.currentJob;
  if (!job || !ACTIVE_STATUSES.has(job.status)) {
    return;
  }
  withBusy(elements.cancelJob, async () => {
    const updated = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' });
    state.currentJob = updated;
    upsertJob(updated);
    refreshLists();
    renderCurrentJob();
    showToast('已请求取消任务。', 'success');
    await refreshJobs();
  });
}

function confirmSelectedJob() {
  const job = state.currentJob;
  if (!job || job.status !== 'waiting_confirmation') {
    return;
  }
  withBusy(elements.confirmJob, async () => {
    const updated = await api(`/api/jobs/${encodeURIComponent(job.id)}/confirm`, { method: 'POST' });
    state.currentJob = updated;
    upsertJob(updated);
    refreshLists();
    renderCurrentJob();
    showToast('已确认，开始生成学习笔记。', 'success');
    await refreshJobs();
  });
}

function deleteJob(id, button) {
  const job = state.jobs.find((item) => item.id === id) || state.currentJob;
  if (!job || ACTIVE_STATUSES.has(job.status)) {
    showToast('任务仍在进行，请先取消后再删除。', 'info');
    return;
  }
  if (!window.confirm(`确定删除历史任务“${job.title || '未命名任务'}”吗？此操作不可撤销。`)) {
    return;
  }
  withBusy(button, async () => {
    await api(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
    state.jobs = state.jobs.filter((item) => item.id !== id);
    if (state.selectedId === id) {
      state.selectedId = null;
      state.currentJob = null;
      state.detailRequest += 1;
    }
    if (state.view === 'job') {
      goBackFromJob();
    }
    refreshLists();
    renderCurrentJob();
    showToast('已删除这条记录。', 'success');
    await refreshJobs();
  });
}

function copySelectedSummary() {
  const summary = state.currentJob && state.currentJob.summary;
  if (!summary) {
    return;
  }
  withBusy(elements.copySummary, async () => {
    await copyText(summary);
    showToast('学习笔记已复制到剪贴板。', 'success');
  });
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) {
    throw new ApiError('复制失败，请手动选择笔记内容复制。');
  }
}

// ---------- 笔记 / 转写的右键菜单 ----------
// pywebview 的 EdgeChromium 后端默认关闭 WebView2 的系统右键菜单
// （edgechromium.py: AreDefaultContextMenusEnabled = _state['debug']），
// 所以必须自己提供一份，否则用户只能整篇复制。

let textMenuTarget = null;

function setupTextMenu() {
  document.addEventListener('contextmenu', (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }
    // 按钮、输入框上不接管右键，避免和点击操作抢事件。
    if (event.target.closest('button, a, input, textarea, label, summary')) {
      return;
    }
    const target = event.target.closest('.markdown-body, #transcript-content, .quiz-card');
    if (!target) {
      return;
    }
    event.preventDefault();
    openTextMenu(target, event.clientX, event.clientY);
  });
  document.addEventListener('click', (event) => {
    if (!elements.textMenu.hidden && !elements.textMenu.contains(event.target)) {
      closeTextMenu();
    }
    if (!elements.cardMenu.hidden && !elements.cardMenu.contains(event.target)) {
      closeNoteMenu();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeTextMenu();
      closeNoteMenu();
    }
  });
  window.addEventListener('scroll', closeTextMenu, true);
  window.addEventListener('scroll', closeNoteMenu, true);
  window.addEventListener('resize', () => {
    closeTextMenu();
    closeNoteMenu();
  });
  elements.textMenu.addEventListener('click', handleTextMenuClick);
}

function openTextMenu(target, x, y) {
  textMenuTarget = target;
  const isQuiz = target.classList.contains('quiz-card');
  const isTranscript = target.id === 'transcript-content';
  const copyItem = elements.textMenu.querySelector('[data-action="copy-selection"]');
  const allItem = elements.textMenu.querySelector('[data-action="copy-all"]');
  const selected = String(window.getSelection() || '').trim();
  copyItem.disabled = !selected;
  if (isQuiz) {
    allItem.textContent = '复制本题（含答案）';
  } else if (isTranscript) {
    allItem.textContent = '复制全部转写';
  } else {
    allItem.textContent = '复制整篇笔记';
  }

  elements.textMenu.hidden = false;
  // 先显示再量尺寸，隐藏元素的宽高是 0，会把菜单位置算错。
  const rect = elements.textMenu.getBoundingClientRect();
  const left = Math.max(12, Math.min(x, window.innerWidth - rect.width - 12));
  const top = Math.max(12, Math.min(y, window.innerHeight - rect.height - 12));
  elements.textMenu.style.left = `${left}px`;
  elements.textMenu.style.top = `${top}px`;
}

function closeTextMenu() {
  elements.textMenu.hidden = true;
  textMenuTarget = null;
}

async function handleTextMenuClick(event) {
  const item = event.target.closest('[data-action]');
  if (!item || item.disabled) {
    return;
  }
  const action = item.dataset.action;
  // closeTextMenu 会清空目标，先留一份引用。
  const target = textMenuTarget;
  closeTextMenu();

  if (action === 'copy-selection') {
    const text = String(window.getSelection() || '').trim();
    if (!text) {
      return;
    }
    try {
      await copyText(text);
      showToast(`已复制选中的 ${text.length} 个字符。`, 'success');
    } catch (error) {
      showToast(error && error.message ? error.message : String(error), 'error');
    }
    return;
  }

  if (action === 'select-all') {
    if (target && target.classList.contains('quiz-card')) {
      selectQuizCard(target);
      return;
    }
    const node = target || elements.summaryContent;
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return;
  }

  if (action === 'copy-all') {
    if (target && target.classList.contains('quiz-card')) {
      const text = quizQuestionText(Number(target.dataset.index));
      if (!text) {
        return;
      }
      try {
        await copyText(text);
        showToast('本题已复制到剪贴板。', 'success');
      } catch (error) {
        showToast(error && error.message ? error.message : String(error), 'error');
      }
      return;
    }
    const job = state.currentJob;
    if (!job) {
      return;
    }
    const isTranscript = Boolean(target && target.id === 'transcript-content');
    const text = isTranscript ? job.transcript : job.summary;
    if (!text) {
      return;
    }
    try {
      await copyText(text);
      showToast(isTranscript ? '已复制全部转写。' : '整篇笔记已复制到剪贴板。', 'success');
    } catch (error) {
      showToast(error && error.message ? error.message : String(error), 'error');
    }
  }
}

/** 只选中题面到答案，避免把「查看答案 / 答错 +1」按钮文字一起选进去。 */
function selectQuizCard(card) {
  const start = card.querySelector('.quiz-question');
  if (!start) {
    return;
  }
  const lastBlock =
    card.querySelector('.quiz-answer') ||
    card.querySelector('.quiz-options') ||
    start;
  const range = document.createRange();
  range.setStartBefore(start);
  range.setEndAfter(lastBlock);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

/** 把一道题还原成可粘贴的纯文本。 */
function quizQuestionText(index) {
  const questions = parseJobQuestions(state.currentJob);
  const item = questions[index];
  if (!item) {
    return '';
  }
  const type = QUESTION_TYPE_LABELS[item.type] || '简答';
  const difficulty = DIFFICULTY_LABELS[item.difficulty] || '理解';
  const lines = [`【${String(index + 1).padStart(2, '0')}】${type} · ${difficulty}`];
  if (item.topic) {
    lines.push(`知识点：${item.topic}`);
  }
  lines.push(item.question);
  if (Array.isArray(item.options) && item.options.length) {
    item.options.forEach((option) => lines.push(option));
  }
  lines.push(`答案：${item.answer}`);
  if (item.explanation) {
    lines.push(`解析：${item.explanation}`);
  }
  return lines.join('\n');
}

function downloadSelectedJob(kind) {
  const job = state.currentJob;
  if (!job) {
    return;
  }
  const isSummary = kind === 'summary';
  const button = isSummary ? elements.exportSummary : elements.exportTranscript;
  const endpoint = isSummary ? 'export' : 'transcript';
  const extension = isSummary ? 'md' : 'txt';
  const suffix = isSummary ? '学习笔记' : '转写';
  withBusy(button, async () => {
    await downloadFile(
      `/api/jobs/${encodeURIComponent(job.id)}/${endpoint}`,
      `${safeFilename(job.title || '学习任务')}-${suffix}.${extension}`
    );
    showToast('文件下载已开始。', 'success');
  });
}

async function downloadFile(path, fallbackFilename) {
  let response;
  try {
    response = await fetch(path, { headers: { 'X-Study-Buddy': '1' } });
  } catch (_error) {
    throw new ApiError('无法连接本地服务，请确认应用仍在运行。');
  }
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(extractErrorMessage(parseJson(text), response.status, text));
  }
  const blob = await response.blob();
  const filename = filenameFromDisposition(response.headers.get('Content-Disposition')) || fallbackFilename;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function filenameFromDisposition(disposition) {
  if (!disposition) {
    return '';
  }
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, ''));
    } catch (_error) {
      return '';
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1].trim() : '';
}

function safeFilename(value) {
  return String(value).replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 100) || '学习任务';
}

async function openSettings() {
  if (elements.settingsDialog.open) {
    return;
  }
  state.config = null;
  clearSensitiveInputs();
  updateSettingsActionStates();
  renderSystemProfile();
  elements.settingsDialog.showModal();

  await withBusy(elements.settingsOpen, async () => {
    const config = await api('/api/config');
    state.config = config;
    populateConfig(config);
    updateSettingsActionStates();
  });
  loadSystemProfile(true);
}

/**
 * 批量生成的并发策略完全由后端按机器能力决定，界面只负责把结论讲清楚，
 * 免得用户以为是「卡住了」，其实是排在队里。
 */
async function loadSystemProfile(force = false) {
  if (state.system && !force) {
    return state.system;
  }
  try {
    const result = await api('/api/system');
    state.system = result;
    renderSystemProfile();
    updateStartState();
    return result;
  } catch (_error) {
    // 后端还没更新时接口不存在，页面按「未探测」处理即可
    return null;
  }
}

function renderSystemProfile() {
  const system = state.system;
  if (!elements.systemSummary) {
    return;
  }
  if (!system || !system.hardware) {
    elements.systemSummary.textContent = '正在检测 CPU、内存和显卡…';
    elements.systemSlots.replaceChildren();
    setConfigBadge(elements.systemBadge, false, '检测中');
    return;
  }
  const hardware = system.hardware;
  elements.systemSummary.textContent = hardware.summary || '';
  setConfigBadge(
    elements.systemBadge,
    Boolean(hardware.has_gpu),
    hardware.has_gpu ? 'GPU 加速可用' : '纯 CPU 运行'
  );
  const slots = system.slots || {};
  const whisper = system.whisper || {};
  const rows = [
    ['语音转写', `${slots.transcribe || 1} 路（串行占用显卡/CPU 核心）`],
    ['下载与画面提取', `${slots.prepare || 1} 路（网络与 ffmpeg）`],
    ['笔记与出题', `${slots.summarize || 1} 路（等模型返回，不占算力）`],
    ['单次批量上限', `${system.max_batch || 30} 个视频`],
    [
      '当前转写配置',
      `${whisper.model || 'small'} · ${(whisper.device || 'cpu').toUpperCase()}` +
        ` · ${whisper.compute_type || 'int8'}` +
        (whisper.cuda_available ? '（CUDA 已就绪）' : '（未启用 CUDA）')
    ]
  ];
  elements.systemSlots.replaceChildren(...rows.map(([name, value]) => {
    const item = document.createElement('li');
    const label = document.createElement('span');
    label.className = 'system-slot-name';
    label.textContent = name;
    const detail = document.createElement('span');
    detail.className = 'system-slot-value';
    detail.textContent = value;
    item.append(label, detail);
    return item;
  }));
}

function populateConfig(config) {
  elements.framesEnabled.checked = config.frames_enabled !== false;
  elements.frameInterval.value = config.frame_interval || 60;
  elements.maxFrames.value = config.max_frames || 24;
  elements.deepRefine.checked = config.deep_refine !== false;
  elements.examMode.checked = config.exam_mode !== false;
  setSelectValue(elements.whisperModel, config.whisper_model || 'small');
  setSelectValue(elements.whisperDevice, config.whisper_device || 'cpu');
  setSelectValue(elements.whisperComputeType, config.whisper_compute_type || 'int8');
  elements.chunkChars.value = config.chunk_chars || 14000;

  const apiConfig = config.api || {};
  elements.apiBaseUrl.value = apiConfig.base_url || '';
  elements.apiModel.value = apiConfig.model || '';
  elements.apiVisionModel.value = apiConfig.vision_model || 'glm-4v-flash';
  setSelectValue(elements.apiReasoningEffort, apiConfig.reasoning_effort || 'low');
  elements.apiKey.value = '';
  elements.generalStatus.textContent = '';
  updateConfigIndicators();
  if (apiConfig.api_configured) {
    loadModelOptions({ announce: false }).catch(() => {});
  }
}

function setSelectValue(select, value) {
  const exists = Array.from(select.options).some((option) => option.value === value);
  select.value = exists ? value : select.options[0].value;
}

function updateConfigIndicators() {
  const config = state.config;
  const apiConfigured = Boolean(config && config.api && config.api.api_configured);
  setConfigBadge(elements.apiConfigured, apiConfigured, apiConfigured ? '已配置' : '未配置');
  updateSettingsActionStates();
}

function setConfigBadge(badge, ready, text) {
  badge.textContent = text;
  badge.classList.toggle('is-ready', ready);
}

function updateSettingsActionStates() {
  if (!elements.saveGeneral) {
    return;
  }
  const loaded = Boolean(state.config);
  setButtonDisabled(elements.saveGeneral, !loaded);
  setButtonDisabled(elements.saveApi, !loaded);
  setButtonDisabled(elements.testApi, !loaded);
  const apiReady = Boolean(loaded && state.config.api && state.config.api.api_configured);
  setButtonDisabled(elements.fetchModels, !apiReady);
}

function saveGeneralSettings(event) {
  event.preventDefault();
  if (!elements.generalForm.reportValidity()) {
    return;
  }
  const payload = {
    whisper_model: elements.whisperModel.value,
    whisper_device: elements.whisperDevice.value,
    whisper_compute_type: elements.whisperComputeType.value,
    chunk_chars: Number(elements.chunkChars.value),
    frames_enabled: elements.framesEnabled.checked,
    frame_interval: Number(elements.frameInterval.value),
    max_frames: Number(elements.maxFrames.value),
    deep_refine: elements.deepRefine.checked,
    exam_mode: elements.examMode.checked
  };
  withBusy(elements.saveGeneral, async () => {
    const config = await api('/api/config', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    state.config = config;
    updateConfigIndicators();
    setFormStatus(elements.generalStatus, '通用设置已保存。', 'success');
    showToast('通用设置已保存。', 'success');
  });
}

function saveApiSettings(event) {
  event.preventDefault();
  if (!elements.apiForm.reportValidity()) {
    return;
  }
  const apiKey = elements.apiKey.value;
  withBusy(elements.saveApi, async () => {
    const config = await api('/api/config/api', {
      method: 'POST',
      body: JSON.stringify({
        base_url: elements.apiBaseUrl.value.trim(),
        model: elements.apiModel.value.trim(),
        vision_model: elements.apiVisionModel.value.trim(),
        reasoning_effort: elements.apiReasoningEffort.value,
        api_key: apiKey.trim() ? apiKey : null
      })
    });
    state.config = config;
    elements.apiKey.value = '';
    elements.apiBaseUrl.value = config.api.base_url || '';
    elements.apiModel.value = config.api.model || '';
    elements.apiVisionModel.value = config.api.vision_model || '';
    setSelectValue(elements.apiReasoningEffort, config.api.reasoning_effort || 'low');
    updateConfigIndicators();
    setFormStatus(elements.apiStatus, 'API 配置已保存；测试连接会产生极少量 API 用量。', 'success');
    showToast('API 配置已保存，密钥未在页面回显。', 'success');
  });
}

function testApiConnection() {
  if (!state.config) {
    return;
  }
  const saved = state.config.api || {};
  const dirty = elements.apiBaseUrl.value.trim() !== (saved.base_url || '') ||
    elements.apiModel.value.trim() !== (saved.model || '') ||
    elements.apiVisionModel.value.trim() !== (saved.vision_model || '') ||
    elements.apiReasoningEffort.value !== (saved.reasoning_effort || 'low') ||
    Boolean(elements.apiKey.value);
  if (dirty) {
    setFormStatus(elements.apiStatus, '有尚未保存的修改，请先保存 API 配置再测试。', 'error');
    showToast('请先保存 API 配置，再测试连接。', 'info');
    return;
  }
  if (!window.confirm('测试连接将实际调用模型，会产生极少量 API 用量。是否继续？')) {
    return;
  }
  withBusy(elements.testApi, async () => {
    const result = await api('/api/config/api/test', { method: 'POST' });
    const message = result.message ? `API 连接成功：${result.message}` : 'API 连接成功。';
    setFormStatus(elements.apiStatus, message, 'success');
    showToast(message, 'success');
  });
}

function clearSensitiveInputs() {
  elements.apiKey.value = '';
}

function setFormStatus(element, message, type) {
  element.textContent = message;
  element.classList.toggle('is-success', type === 'success');
  element.classList.toggle('is-error', type === 'error');
}

async function withBusy(button, action) {
  if (!button || button.disabled || button.dataset.busy === 'true') {
    return undefined;
  }
  button.dataset.busy = 'true';
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    return await action();
  } catch (error) {
    showToast(error && error.message ? error.message : String(error), 'error');
    return undefined;
  } finally {
    delete button.dataset.busy;
    button.removeAttribute('aria-busy');
    button.disabled = false;
    updateStartState();
    renderCurrentJob();
    updateSettingsActionStates();
  }
}

function setButtonDisabled(button, disabled) {
  if (button) {
    button.disabled = Boolean(disabled) || button.dataset.busy === 'true';
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('X-Study-Buddy', '1');
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (_error) {
    throw new ApiError('无法连接本地服务，请确认应用仍在运行。');
  }
  const text = await response.text();
  const data = parseJson(text);
  if (!response.ok) {
    throw new ApiError(extractErrorMessage(data, response.status, text), response.status);
  }
  if (!text) {
    return null;
  }
  return data === undefined ? text : data;
}

function parseJson(text) {
  if (!text) {
    return undefined;
  }
  try {
    return JSON.parse(text);
  } catch (_error) {
    return undefined;
  }
}

function extractErrorMessage(data, status, fallback = '') {
  if (data && data.detail !== undefined) {
    const detail = data.detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => item && item.msg ? item.msg : String(item)).join('；');
    }
    if (typeof detail === 'object') {
      return JSON.stringify(detail);
    }
    return String(detail);
  }
  if (data && data.message) {
    return String(data.message);
  }
  return fallback || `请求失败（HTTP ${status}）。`;
}

function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  const text = document.createElement('p');
  text.textContent = String(message || '操作未完成。');
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', '关闭提示');
  close.textContent = '×';
  close.addEventListener('click', () => toast.remove());
  toast.appendChild(text);
  toast.appendChild(close);
  elements.toastRegion.appendChild(toast);
  while (elements.toastRegion.children.length > 4) {
    elements.toastRegion.firstElementChild.remove();
  }
  window.setTimeout(() => toast.remove(), type === 'error' ? 7000 : 4500);
}

function formatDate(value, full = false) {
  if (!value) {
    return '时间未知';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '时间未知';
  }
  const options = full
    ? { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }
    : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false };
  return date.toLocaleString('zh-CN', options);
}

function formatDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return '';
  }
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`
    : `${minutes}:${String(remaining).padStart(2, '0')}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[character]);
}

function renderInline(value) {
  let text = escapeHtml(value);
  const codeTokens = [];
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    const index = codeTokens.push(`<code>${code}</code>`) - 1;
    return `\u0001${index}\u0001`;
  });
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  return text.replace(/\u0001(\d+)\u0001/g, (_, index) => codeTokens[Number(index)] || '');
}

/* ---------- 划重点 ---------- */

const HIGHLIGHT_LABELS = { yellow: '黄色', green: '绿色', blue: '蓝色', pink: '粉色' };

function highlightContainer(field) {
  return field === 'visual' ? elements.visualContent : elements.summaryContent;
}

/** 把 markdown 渲染进正文，并立刻把已存的划重点贴回去。 */
function renderNoteHtml(field, markdown, jobId) {
  const container = highlightContainer(field);
  if (!container) {
    return;
  }
  container.innerHTML = renderMarkdown(markdown);
  if (field === 'visual') {
    state.renderedVisualJob = jobId;
    state.renderedVisual = markdown;
  } else {
    state.renderedSummaryJob = jobId;
    state.renderedSummary = markdown;
  }
  applyHighlights(field);
}

async function loadHighlights(jobId) {
  if (!jobId || state.highlights.jobId === jobId) {
    return;
  }
  state.highlights = { jobId, summary: [], visual: [] };
  state.highlightPositions = { summary: [], visual: [] };
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/highlights`);
    if (state.selectedId !== jobId || state.highlights.jobId !== jobId) {
      return;
    }
    const list = Array.isArray(result.highlights) ? result.highlights : [];
    state.highlights.summary = list.filter((item) => item.field === 'summary');
    state.highlights.visual = list.filter((item) => item.field === 'visual');
    applyHighlights('summary');
    applyHighlights('visual');
  } catch (_error) {
    // 划重点是附加功能，接口失败就当作还没有标记，不打断阅读
  }
}

function unwrapHighlights(container) {
  container.querySelectorAll('mark.note-hl').forEach((mark) => {
    const parent = mark.parentNode;
    if (!parent) {
      return;
    }
    while (mark.firstChild) {
      parent.insertBefore(mark.firstChild, mark);
    }
    parent.removeChild(mark);
  });
  container.normalize();
}

/** 在纯文本里找片段：先信记录里的位置，找不到再全局找一次。 */
function locateQuote(text, quote, hint) {
  if (!quote) {
    return -1;
  }
  const start = Math.max(0, Number(hint) || 0);
  if (text.startsWith(quote, start)) {
    return start;
  }
  const near = text.indexOf(quote, Math.max(0, start - 24));
  if (near >= 0) {
    return near;
  }
  return text.indexOf(quote);
}

/**
 * 按「正文纯文本的第 start~end 个字符」把文字包上 mark，返回这几段 mark。
 *
 * 关键：必须**逐个文本节点**包，绝不能把整个 Range 一次性 extractContents。
 * 跨列表项 / 段落选中时，一个 Range 会捎带 <li>、<p> 这些块级元素，
 * 塞进 <mark> 后列表结构当场散架，文字也会被搬得七零八落。
 */
function wrapTextRange(container, start, end, className, dataset) {
  if (!(end > start)) {
    return [];
  }
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const segments = [];
  let offset = 0;
  let node = walker.nextNode();
  while (node) {
    const length = node.nodeValue ? node.nodeValue.length : 0;
    const nodeStart = offset;
    const nodeEnd = offset + length;
    const from = Math.max(start, nodeStart);
    const to = Math.min(end, nodeEnd);
    if (to > from) {
      segments.push({ node, from: from - nodeStart, to: to - nodeStart });
    }
    if (nodeEnd >= end) {
      break;
    }
    offset = nodeEnd;
    node = walker.nextNode();
  }
  const marks = [];
  segments.forEach((segment) => {
    const range = document.createRange();
    range.setStart(segment.node, segment.from);
    range.setEnd(segment.node, segment.to);
    const mark = document.createElement('mark');
    mark.className = className;
    if (dataset) {
      Object.entries(dataset).forEach(([key, value]) => {
        mark.dataset[key] = String(value === undefined || value === null ? '' : value);
      });
    }
    try {
      mark.appendChild(range.extractContents());
      range.insertNode(mark);
      marks.push(mark);
    } catch (_error) {
      // 这一段包不上就跳过，不能因为一处失败破坏整篇结构
    }
  });
  return marks;
}

function applyHighlights(field) {
  const container = highlightContainer(field);
  if (!container) {
    return;
  }
  unwrapHighlights(container);
  const usable = state.highlights.jobId && state.highlights.jobId === state.selectedId;
  const records = usable ? (state.highlights[field] || []).slice() : [];
  const text = container.textContent || '';
  const positioned = [];
  let occupied = 0;
  records
    .sort((a, b) => (Number(a.start) || 0) - (Number(b.start) || 0) || Number(a.id) - Number(b.id))
    .forEach((record) => {
      const at = locateQuote(text, record.quote, record.start);
      if (at < 0) {
        return;
      }
      const finish = at + record.quote.length;
      // 历史数据可能重叠，重叠部分不嵌套第二层，免得清除时只掉了外壳
      if (at < occupied) {
        return;
      }
      positioned.push({ record, start: at, end: finish });
      occupied = finish;
    });
  state.highlightPositions[field] = positioned;
  positioned.forEach((item) => {
    wrapTextRange(
      container,
      item.start,
      item.end,
      `note-hl hl-${item.record.color || 'yellow'}`,
      { highlightId: item.record.id }
    );
  });
}

/** 当前选区落在哪块笔记里：返回纯文本偏移，供保存与重新贴回使用。 */
/** 选区端点 → 正文 textContent 的字符偏移（跨段落时 Range.toString 会多出换行，不能用）。 */
function textOffsetOfPosition(container, node, offset) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  if (node.nodeType === Node.TEXT_NODE) {
    let total = 0;
    let current = walker.nextNode();
    while (current) {
      if (current === node) {
        return total + Math.min(offset, current.nodeValue.length);
      }
      total += current.nodeValue.length;
      current = walker.nextNode();
    }
    return total;
  }
  // 端点落在元素上（选了整段、或从段首开始）：按「该位置的文字之前有多少字」算
  const probe = document.createRange();
  try {
    probe.setStart(node, offset);
    probe.collapse(true);
  } catch (_error) {
    return 0;
  }
  let total = 0;
  let current = walker.nextNode();
  while (current) {
    if (probe.comparePoint(current, 0) >= 0) {
      break;
    }
    total += current.nodeValue.length;
    current = walker.nextNode();
  }
  return total;
}

function currentSelectionContext() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    return null;
  }
  const range = selection.getRangeAt(0);
  let container = null;
  let field = '';
  if (elements.summaryContent.contains(range.commonAncestorContainer)) {
    container = elements.summaryContent;
    field = 'summary';
  } else if (elements.visualContent.contains(range.commonAncestorContainer)) {
    container = elements.visualContent;
    field = 'visual';
  }
  if (!container) {
    return null;
  }
  const text = container.textContent || '';
  let start = textOffsetOfPosition(container, range.startContainer, range.startOffset);
  // 用片段取原文：textContent 拼块级元素时不插换行，和 Range.toString() 的长度会差几个字符
  const quote = range.cloneContents().textContent || '';
  if (!quote.trim() || quote.length > 2000) {
    return null;
  }
  if (text.slice(start, start + quote.length) !== quote) {
    const corrected = text.indexOf(quote, Math.max(0, start - 80));
    if (corrected >= 0) {
      start = corrected;
    }
  }
  return { field, container, start, quote, end: start + quote.length, range };
}

function overlappingHighlights(context) {
  const positioned = state.highlightPositions[context.field] || [];
  return positioned.filter((item) => item.start < context.end && item.end > context.start);
}

function hideHighlightPalette() {
  if (elements.hlPalette) {
    elements.hlPalette.hidden = true;
  }
}

function showHighlightPalette() {
  const context = currentSelectionContext();
  if (!context) {
    hideHighlightPalette();
    return;
  }
  const rect = context.range.getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) {
    hideHighlightPalette();
    return;
  }
  elements.hlPalette.hidden = false;
  const palette = elements.hlPalette.getBoundingClientRect();
  let top = rect.top - palette.height - 10;
  if (top < 8) {
    top = rect.bottom + 10;
  }
  let left = rect.left + rect.width / 2 - palette.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - palette.width - 8));
  elements.hlPalette.style.top = `${Math.round(top)}px`;
  elements.hlPalette.style.left = `${Math.round(left)}px`;
}

function setupHighlightPalette() {
  document.addEventListener('mouseup', (event) => {
    if (elements.hlPalette.contains(event.target)) {
      return;
    }
    // 等浏览器把选区结算完再量位置
    window.setTimeout(showHighlightPalette, 10);
  });
  document.addEventListener('selectionchange', () => {
    if (elements.hlPalette.hidden) {
      return;
    }
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) {
      hideHighlightPalette();
    }
  });
  // 按住按钮时不取消选区，否则点颜色前选中文字就没了
  elements.hlPalette.addEventListener('mousedown', (event) => event.preventDefault());
  elements.hlPalette.addEventListener('click', handleHighlightPaletteClick);
  window.addEventListener('scroll', hideHighlightPalette, true);
  window.addEventListener('resize', hideHighlightPalette);
}

async function handleHighlightPaletteClick(event) {
  const button = event.target.closest('button');
  if (!button) {
    return;
  }
  const context = currentSelectionContext();
  hideHighlightPalette();
  if (!context) {
    showToast('选中的文字已经取消，请重新选择。', 'info');
    return;
  }
  const selection = window.getSelection();
  try {
    if (button.dataset.action === 'clear') {
      await clearHighlightsInContext(context);
    } else if (button.dataset.color) {
      await addHighlight(context, button.dataset.color);
    }
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (selection) {
      selection.removeAllRanges();
    }
  }
}

async function addHighlight(context, color) {
  const jobId = state.selectedId;
  if (!jobId) {
    return;
  }
  // 覆盖已有的同段标记：先删旧的，再建新的，避免两层 mark 叠在一起
  const overlapping = overlappingHighlights(context);
  for (const item of overlapping) {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/highlights/${item.record.id}`, {
      method: 'DELETE'
    });
    state.highlights[context.field] = state.highlights[context.field]
      .filter((record) => record.id !== item.record.id);
  }
  const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/highlights`, {
    method: 'POST',
    body: JSON.stringify({
      field: context.field,
      quote: context.quote,
      start: context.start,
      color
    })
  });
  state.highlights.jobId = jobId;
  state.highlights[context.field].push(result.highlight);
  applyHighlights(context.field);
  showToast(`已用${HIGHLIGHT_LABELS[color] || ''}标出重点。`, 'success');
}

async function clearHighlightsInContext(context) {
  const jobId = state.selectedId;
  const overlapping = overlappingHighlights(context);
  if (!jobId || !overlapping.length) {
    showToast('这段文字还没有标记。', 'info');
    return;
  }
  for (const item of overlapping) {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/highlights/${item.record.id}`, {
      method: 'DELETE'
    });
    state.highlights[context.field] = state.highlights[context.field]
      .filter((record) => record.id !== item.record.id);
  }
  applyHighlights(context.field);
  showToast('已取消标记。', 'success');
}

/* ---------- 笔记搜索 ---------- */

function setupSearch() {
  let timer = 0;
  elements.noteSearch.addEventListener('input', () => {
    const query = elements.noteSearch.value.trim();
    elements.noteSearchClear.hidden = !query;
    window.clearTimeout(timer);
    if (!query) {
      if (state.view === 'search') {
        exitSearch();
      }
      return;
    }
    timer = window.setTimeout(() => runSearch(query), 280);
  });
  elements.noteSearch.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      window.clearTimeout(timer);
      runSearch(elements.noteSearch.value.trim());
    } else if (event.key === 'Escape') {
      event.preventDefault();
      clearSearchInput();
      exitSearch();
    }
  });
  elements.noteSearchClear.addEventListener('click', () => {
    clearSearchInput();
    exitSearch();
    elements.noteSearch.focus();
  });
  elements.searchResults.addEventListener('click', (event) => {
    const row = event.target.closest('[data-job-id]');
    if (row) {
      jumpToSearchResult(row.dataset.jobId, row.dataset.courseId);
    }
  });
}

function clearSearchInput() {
  elements.noteSearch.value = '';
  elements.noteSearchClear.hidden = true;
}

async function runSearch(query) {
  if (!query) {
    return;
  }
  if (state.view !== 'search') {
    state.search.returnView = {
      view: state.view,
      courseId: state.currentCourseId,
      jobId: state.selectedId || ''
    };
    setView('search');
  }
  const request = ++state.search.request;
  state.search.query = query;
  elements.searchTitle.textContent = `“${query}” 的搜索结果`;
  elements.searchMeta.textContent = '正在搜索…';
  elements.searchResults.replaceChildren();
  try {
    const result = await api(`/api/search?q=${encodeURIComponent(query)}&limit=60`);
    if (request !== state.search.request) {
      return;
    }
    state.search.results = Array.isArray(result.results) ? result.results : [];
    renderSearchResults();
  } catch (error) {
    if (request === state.search.request) {
      elements.searchMeta.textContent = error.message;
    }
  }
}

function renderSearchResults() {
  const results = state.search.results;
  const query = state.search.query;
  elements.searchMeta.textContent = results.length
    ? `找到 ${results.length} 条笔记，点一条即可跳转并定位到原文`
    : '没有找到包含这个知识点的笔记；换个更短的词试试';
  const fragment = document.createDocumentFragment();
  results.forEach((item) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'search-result';
    row.dataset.jobId = item.id;
    row.dataset.courseId = item.course_id || '';

    const head = document.createElement('span');
    head.className = 'search-result-head';
    const course = document.createElement('span');
    course.className = 'search-result-course';
    course.textContent = item.course_name || '杂项';
    const title = document.createElement('span');
    title.className = 'search-result-title';
    const seq = item.seq ? `${item.seq} ` : '';
    title.textContent = `${seq}${item.title || '未命名笔记'}`;
    head.append(course, title);
    if (Number(item.occurrences) > 1) {
      const count = document.createElement('span');
      count.className = 'search-result-count';
      count.textContent = `${item.occurrences} 处`;
      head.appendChild(count);
    }
    row.appendChild(head);

    const snippet = document.createElement('span');
    snippet.className = 'search-result-snippet';
    appendMarkedText(snippet, item.snippet, query);
    row.appendChild(snippet);

    if (item.field === 'visual') {
      const tag = document.createElement('span');
      tag.className = 'search-result-tag';
      tag.textContent = '课件画面';
      row.appendChild(tag);
    } else if (item.field === 'title') {
      const tag = document.createElement('span');
      tag.className = 'search-result-tag';
      tag.textContent = '标题命中';
      row.appendChild(tag);
    }
    fragment.appendChild(row);
  });
  elements.searchResults.replaceChildren(fragment);
}

function appendMarkedText(container, text, query) {
  const value = String(text || '');
  const needle = String(query || '').toLowerCase();
  if (!value) {
    return;
  }
  if (!needle) {
    container.textContent = value;
    return;
  }
  const lowered = value.toLowerCase();
  let index = lowered.indexOf(needle);
  if (index < 0) {
    container.textContent = value;
    return;
  }
  let cursor = 0;
  while (index >= 0) {
    if (index > cursor) {
      container.appendChild(document.createTextNode(value.slice(cursor, index)));
    }
    const mark = document.createElement('mark');
    mark.className = 'search-mark';
    mark.textContent = value.slice(index, index + needle.length);
    container.appendChild(mark);
    cursor = index + needle.length;
    index = lowered.indexOf(needle, cursor);
  }
  if (cursor < value.length) {
    container.appendChild(document.createTextNode(value.slice(cursor)));
  }
}

async function jumpToSearchResult(jobId, courseId) {
  if (!jobId) {
    return;
  }
  const query = state.search.query;
  state.pendingLocate = { jobId, query };
  state.search.returnView = null;
  if (courseId && courseId !== state.currentCourseId) {
    try {
      await openCourse(courseId);
    } catch (_error) {
      // 课程读取失败也照常打开笔记
    }
  }
  openNote(jobId);
}

function exitSearch() {
  // 只在搜索页生效：已经跳到某篇笔记后清空搜索框，不应该把人从笔记里带走
  if (state.view !== 'search') {
    return;
  }
  const previous = state.search.returnView;
  state.search.returnView = null;
  state.search.request += 1;
  state.search.results = [];
  if (previous && previous.view === 'job' && previous.jobId) {
    openNote(previous.jobId);
    return;
  }
  if (previous && previous.courseId) {
    openCourse(previous.courseId);
    return;
  }
  if (state.currentCourseId) {
    openCourse(state.currentCourseId);
    return;
  }
  setView('home');
}

/** 从搜索结果进来后，把命中词在正文里标出来并滚到视野中间。 */
function handlePendingLocate(job) {
  const pending = state.pendingLocate;
  if (!pending || pending.jobId !== job.id) {
    return;
  }
  state.pendingLocate = null;
  window.setTimeout(() => {
    if (state.selectedId !== job.id) {
      return;
    }
    if (state.currentModule !== 'notes') {
      switchModule('notes');
    }
    focusSearchTerm(pending.query);
  }, 60);
}

function focusSearchTerm(query) {
  const needle = String(query || '').trim();
  if (!needle) {
    return;
  }
  let container = elements.summaryContent;
  let text = container.textContent || '';
  let index = text.toLowerCase().indexOf(needle.toLowerCase());
  if (index < 0) {
    // 命中词只在课件画面里：展开那一栏再定位
    container = elements.visualContent;
    text = container.textContent || '';
    index = text.toLowerCase().indexOf(needle.toLowerCase());
    if (index >= 0) {
      elements.visualDetails.open = true;
    }
  }
  if (index < 0) {
    return;
  }
  const marks = wrapTextRange(container, index, index + needle.length, 'search-hit');
  if (!marks.length) {
    return;
  }
  marks[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
  window.setTimeout(() => {
    marks.forEach((mark) => {
      if (!mark.isConnected) {
        return;
      }
      const parent = mark.parentNode;
      while (mark.firstChild) {
        parent.insertBefore(mark.firstChild, mark);
      }
      parent.removeChild(mark);
      parent.normalize();
    });
  }, 6000);
}

/* ---------- Markdown 渲染 ---------- */

function splitTableRow(line) {
  let text = String(line || '').trim();
  if (text.startsWith('|')) {
    text = text.slice(1);
  }
  if (text.endsWith('|')) {
    text = text.slice(0, -1);
  }
  const cells = [];
  let buffer = '';
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '\\' && text[index + 1] === '|') {
      buffer += '|';
      index += 1;
      continue;
    }
    if (char === '|') {
      cells.push(buffer.trim());
      buffer = '';
      continue;
    }
    buffer += char;
  }
  cells.push(buffer.trim());
  return cells;
}

function isTableSeparator(line) {
  const cells = splitTableRow(line);
  if (!cells.length) {
    return false;
  }
  return cells.every((cell) => /^:?-{2,}:?$/.test(cell.replace(/\s+/g, '')));
}

function tableAlignments(line) {
  return splitTableRow(line).map((cell) => {
    const spec = cell.replace(/\s+/g, '');
    const left = spec.startsWith(':');
    const right = spec.endsWith(':');
    if (left && right) {
      return 'center';
    }
    if (right) {
      return 'right';
    }
    return left ? 'left' : '';
  });
}

function looksLikeTable(lines, index) {
  const line = lines[index];
  const next = lines[index + 1];
  if (next === undefined || !line || !line.includes('|')) {
    return false;
  }
  return isTableSeparator(next);
}

function renderTable(headerLine, separatorLine, rowLines) {
  const headers = splitTableRow(headerLine);
  const aligns = tableAlignments(separatorLine);
  const columns = Math.max(headers.length, aligns.length);
  while (headers.length < columns) {
    headers.push('');
  }
  const alignStyle = (index) => (aligns[index] ? ` style="text-align:${aligns[index]}"` : '');
  const head = headers
    .map((cell, index) => `<th${alignStyle(index)}>${renderInline(cell)}</th>`)
    .join('');
  const body = rowLines
    .map((line) => {
      const cells = splitTableRow(line);
      while (cells.length < columns) {
        cells.push('');
      }
      const tds = cells
        .slice(0, columns)
        .map((cell, index) => `<td${alignStyle(index)}>${renderInline(cell)}</td>`)
        .join('');
      return `<tr>${tds}</tr>`;
    })
    .join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderMarkdown(markdown) {
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  let paragraph = [];
  let listType = '';
  let quoteLines = [];
  let inCode = false;
  let codeLines = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      output.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (listType) {
      output.push(`</${listType}>`);
      listType = '';
    }
  };
  const flushQuote = () => {
    if (quoteLines.length) {
      output.push(`<blockquote><p>${quoteLines.map(renderInline).join('<br>')}</p></blockquote>`);
      quoteLines = [];
    }
  };
  const flushTextBlocks = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };
  const flushCode = () => {
    output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
    codeLines = [];
  };

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex];
    if (inCode) {
      if (/^\s*```/.test(line)) {
        flushCode();
        inCode = false;
      } else {
        codeLines.push(line);
      }
      continue;
    }
    if (/^\s*```/.test(line)) {
      flushTextBlocks();
      inCode = true;
      codeLines = [];
      continue;
    }
    if (!line.trim()) {
      flushTextBlocks();
      continue;
    }

    // 表格：表头下一行是 |---|:--:| 这类分隔行才认，
    // 否则笔记里随手写的「A | B」会被误判成表格。
    if (looksLikeTable(lines, lineIndex)) {
      flushTextBlocks();
      const headerLine = line;
      const separatorLine = lines[lineIndex + 1];
      const rowLines = [];
      lineIndex += 2;
      while (lineIndex < lines.length) {
        const rowLine = lines[lineIndex];
        if (!rowLine.trim() || !rowLine.includes('|') || /^\s*```/.test(rowLine)) {
          break;
        }
        rowLines.push(rowLine);
        lineIndex += 1;
      }
      // 让 for 的 += 1 落在表格后第一行，而不是跳过它
      lineIndex -= 1;
      output.push(renderTable(headerLine, separatorLine, rowLines));
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (heading) {
      flushTextBlocks();
      const level = heading[1].length;
      output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      flushQuote();
      const nextType = unordered ? 'ul' : 'ol';
      if (listType && listType !== nextType) {
        flushList();
      }
      if (!listType) {
        listType = nextType;
        output.push(`<${listType}>`);
      }
      output.push(`<li>${renderInline((unordered || ordered)[1])}</li>`);
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      quoteLines.push(quote[1]);
      continue;
    }

    flushList();
    flushQuote();
    paragraph.push(line.trim());
  }

  flushTextBlocks();
  if (inCode) {
    flushCode();
  }
  return output.join('');
}

document.addEventListener('DOMContentLoaded', init);

async function loadModelOptions({ announce = false } = {}) {
  const apiConfig = state.config ? state.config.api || {} : {};
  if (!apiConfig.api_configured) {
    if (announce) {
      showToast('请先在下方填写 API Key 并保存，再获取模型列表。', 'info');
    }
    return;
  }
  try {
    const result = await api('/api/models');
    const models = Array.isArray(result.models) ? result.models : [];
    state.models = models;
    elements.modelsStatus.textContent = models.length
      ? `已读取 ${models.length} 个可用文本模型，点右侧 ▾ 展开全部；视觉模型需手填。`
      : '服务端没有返回可用模型，请手动填写。';
    if (announce) {
      showToast(`已获取 ${models.length} 个可用模型。`, 'success');
    }
  } catch (error) {
    elements.modelsStatus.textContent = '读取失败，可手动填写模型名。';
    if (announce) {
      showToast(error.message, 'error');
    }
    throw error;
  }
}

function setupModelPickers() {
  modelPickers.length = 0;
  modelPickers.push({
    kind: 'text',
    input: elements.apiModel,
    toggle: elements.modelToggle,
    menu: elements.modelMenu
  });
  modelPickers.push({
    kind: 'vision',
    input: elements.apiVisionModel,
    toggle: elements.visionToggle,
    menu: elements.visionMenu
  });
  modelPickers.forEach((picker) => {
    if (!picker.toggle || !picker.menu) {
      return;
    }
    picker.toggle.addEventListener('click', (event) => {
      event.preventDefault();
      togglePicker(picker);
    });
    picker.menu.addEventListener('click', (event) => pickModel(picker, event));
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.combo')) {
      modelPickers.forEach(closePicker);
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      modelPickers.forEach(closePicker);
    }
  });
}

function pickerItems(picker) {
  return picker.kind === 'vision' ? state.visionModels : state.models;
}

function renderPickerMenu(picker) {
  const current = picker.input.value.trim();
  const names = pickerItems(picker).slice();
  if (current && !names.includes(current)) {
    names.unshift(current);
  }
  picker.menu.replaceChildren();
  if (!names.length) {
    const hint = document.createElement('li');
    hint.textContent = picker.kind === 'vision'
      ? '点此探测哪些模型能读图'
      : '先点「获取可用模型」读取列表';
    hint.style.cursor = 'default';
    picker.menu.appendChild(hint);
    return;
  }
  names.forEach((name) => {
    const item = document.createElement('li');
    item.textContent = name;
    item.dataset.model = name;
    item.setAttribute('role', 'option');
    if (name === current) {
      item.classList.add('is-active');
      item.setAttribute('aria-selected', 'true');
    }
    picker.menu.appendChild(item);
  });
}

function openPicker(picker) {
  if (!picker || !picker.menu || picker.menu.hidden === false) {
    return;
  }
  renderPickerMenu(picker);
  picker.menu.hidden = false;
  picker.toggle.setAttribute('aria-expanded', 'true');
}

function closePicker(picker) {
  if (!picker || !picker.menu || picker.menu.hidden) {
    return;
  }
  picker.menu.hidden = true;
  picker.toggle.setAttribute('aria-expanded', 'false');
}

async function togglePicker(picker) {
  if (!picker.menu.hidden) {
    closePicker(picker);
    return;
  }
  modelPickers.forEach((other) => {
    if (other !== picker) {
      closePicker(other);
    }
  });
  if (!pickerItems(picker).length) {
    try {
      if (picker.kind === 'vision') {
        await loadVisionOptions({ announce: true });
      } else {
        await loadModelOptions({ announce: false });
        openPicker(picker);
        return;
      }
    } catch (error) {
      showToast(error.message, 'error');
      return;
    }
  }
  openPicker(picker);
}

function pickModel(picker, event) {
  const item = event.target.closest('li[data-model]');
  if (!item) {
    return;
  }
  picker.input.value = item.dataset.model;
  closePicker(picker);
  picker.input.focus();
}

async function loadVisionOptions({ announce = false } = {}) {
  const apiConfig = state.config ? state.config.api || {} : {};
  if (!apiConfig.api_configured) {
    if (announce) {
      showToast('请先填写 API Key 并保存，再探测视觉模型。', 'info');
    }
    return;
  }
  if (announce) {
    showToast('正在逐个试探哪些模型能读图，约需 10-30 秒。', 'info');
  }
  try {
    const result = await api('/api/models?probe_vision=true');
    state.visionModels = Array.isArray(result.vision_models) ? result.vision_models : [];
    if (announce) {
      showToast(
        state.visionModels.length
          ? `有 ${state.visionModels.length} 个模型支持读图，已列在下拉里。`
          : '没有探测到支持读图的模型，请手动填写（如 glm-4v-flash）。',
        state.visionModels.length ? 'success' : 'info'
      );
    }
  } catch (error) {
    if (announce) {
      showToast(error.message, 'error');
    }
    throw error;
  }
}

function formatTokens(value) {
  const tokens = Number(value) || 0;
  if (tokens >= 100000000) {
    return `${(tokens / 100000000).toFixed(2)} 亿`;
  }
  if (tokens >= 10000) {
    return `${(tokens / 10000).toFixed(1)} 万`;
  }
  return tokens.toLocaleString('zh-CN');
}

function renderJobUsage(usage) {
  if (!elements.jobUsage) {
    return;
  }
  const total = usage ? Number(usage.total_tokens) || 0 : 0;
  if (total <= 0) {
    elements.jobUsage.hidden = true;
    return;
  }
  const parts = (usage.by_purpose || [])
    .filter((item) => Number(item.tokens) > 0)
    .map((item) => `${PURPOSE_LABELS[item.purpose] || item.purpose} ${item.calls} 次 ${formatTokens(item.tokens)}`);
  elements.jobUsage.textContent = `本次消耗 ${formatTokens(total)} tokens${parts.length ? `（${parts.join(' · ')}）` : ''}`;
  elements.jobUsage.hidden = false;
}

let lastUsageFetch = 0;

async function loadUsageSummary(force = false) {
  if (!elements.usageBadge) {
    return;
  }
  // 轮询会频繁触发，一分钟内不重复请求。
  if (!force && Date.now() - lastUsageFetch < 60000) {
    return;
  }
  lastUsageFetch = Date.now();
  try {
    const data = await api('/api/usage');
    const month = data.month || {};
    const tokens = Number(month.tokens) || 0;
    if (tokens <= 0) {
      elements.usageBadge.hidden = true;
      return;
    }
    const today = data.today || {};
    const purposeText = (data.by_purpose || [])
      .filter((item) => Number(item.tokens) > 0)
      .map((item) => `${PURPOSE_LABELS[item.purpose] || item.purpose} ${formatTokens(item.tokens)}（${item.calls} 次）`)
      .join('\n');
    elements.usageBadge.textContent = `本月 ${formatTokens(tokens)}`;
    elements.usageBadge.title = [
      `本月共 ${Number(month.tokens).toLocaleString('zh-CN')} tokens，${month.calls || 0} 次调用`,
      purposeText,
      `今日 ${Number(today.tokens || 0).toLocaleString('zh-CN')} tokens，${today.calls || 0} 次调用`
    ].filter(Boolean).join('\n');
    elements.usageBadge.hidden = false;
  } catch (error) {
    elements.usageBadge.hidden = true;
  }
}
