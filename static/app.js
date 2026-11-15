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
  local: { path: '', title: '' },
  bilibili: { info: null, infoUrl: '', entryIndex: 0 },
  jobs: [],
  selectedId: null,
  currentJob: null,
  config: null,
  refreshing: false,
  detailRequest: 0,
  pollErrorNotified: false,
  renderedSummaryJob: '',
  renderedSummary: '',
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
    'browse-button', 'local-selection', 'local-title', 'local-path',
    'bilibili-url', 'bilibili-info-button', 'bilibili-result', 'bilibili-title', 'bilibili-entry',
    'source-hint', 'start-button', 'history-count', 'usage-badge', 'history-refresh-status', 'history-empty', 'history-list',
    'job-empty', 'job-detail', 'job-title', 'job-status', 'job-provider', 'job-created', 'job-usage', 'job-stage',
    'job-progress-section', 'job-progress', 'job-progress-text', 'cancel-job', 'confirmation-box', 'transcript-count', 'confirm-job',
    'error-box', 'job-error', 'result-section', 'copy-summary', 'export-summary', 'summary-content',
    'visual-details', 'visual-content', 'transcript-details', 'transcript-content', 'export-transcript', 'regenerate-job', 'delete-job',
    'module-nav', 'module-notes', 'module-quiz', 'nav-notes-meta', 'nav-quiz-meta',
    'quiz-list', 'quiz-empty', 'quiz-empty-text', 'quiz-generate', 'quiz-regenerate', 'quiz-sort-by-level',
    'backend-stale', 'text-menu',
    'general-form', 'frames-enabled', 'frame-interval', 'max-frames', 'deep-refine', 'exam-mode', 'whisper-model',
    'whisper-device', 'whisper-compute-type', 'chunk-chars', 'save-general', 'general-status',
    'api-form', 'api-configured', 'api-base-url', 'api-model', 'api-vision-model', 'api-reasoning-effort', 'api-key', 'fetch-models', 'models-status', 'model-toggle', 'model-menu', 'vision-toggle', 'vision-menu', 'save-api', 'test-api', 'api-status',
    'toast-region'
  ];
  ids.forEach((id) => {
    elements[toCamelCase(id)] = document.getElementById(id);
  });

  bindEvents();
  switchSource('local');
  renderHistory();
  renderCurrentJob();
  refreshJobs({ initial: true });
  checkBackendFeatures();
  window.setInterval(() => refreshJobs(), 2000);
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
  setupVideoDrop();
  elements.bilibiliInfoButton.addEventListener('click', readBilibiliInfo);
  elements.bilibiliUrl.addEventListener('input', handleBilibiliUrlChange);
  elements.bilibiliUrl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      readBilibiliInfo();
    }
  });
  elements.bilibiliEntry.addEventListener('change', () => {
    state.bilibili.entryIndex = Number(elements.bilibiliEntry.value);
    updateStartState();
  });
  elements.startButton.addEventListener('click', startNewJob);

  elements.cancelJob.addEventListener('click', cancelSelectedJob);
  elements.confirmJob.addEventListener('click', confirmSelectedJob);
  elements.regenerateJob.addEventListener('click', regenerateSelectedJob);
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

function browseLocalVideo() {
  withBusy(elements.browseButton, async () => {
    const result = await api('/api/videos/browse', { method: 'POST' });
    if (result.cancelled) {
      showToast('已取消选择视频。', 'info');
      return;
    }
    if (!result.path) {
      throw new ApiError('未获取到有效的视频路径。');
    }
    state.local.path = result.path;
    state.local.title = result.title || '本地视频';
    renderLocalSelection();
    showToast('视频已选择，点击“开始生成总结”后才会开始处理。', 'success');
  });
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
  const path = droppedFilePath(event);
  if (!path) {
    showToast('无法读取拖入文件的路径，请点击此处选择视频。', 'info');
    return;
  }
  state.local.path = path;
  state.local.title = fileBaseName(path);
  renderLocalSelection();
  showToast('视频已选择，点击“开始生成总结”后才会开始处理。', 'success');
}

function droppedFilePath(event) {
  const transfer = event.dataTransfer;
  if (!transfer) {
    return '';
  }
  const files = transfer.files;
  if (files && files.length) {
    const direct = files[0].path;
    if (typeof direct === 'string' && direct) {
      return direct;
    }
  }
  const listed = transfer.getData('text/uri-list') || transfer.getData('text/plain') || '';
  const first = listed.split(/\r?\n/).find((line) => line && !line.startsWith('#'));
  if (first && first.startsWith('file://')) {
    try {
      const path = decodeURIComponent(new URL(first).pathname).replace(/^\//, '');
      return path.replace(/\//g, '\\');
    } catch (error) {
      return '';
    }
  }
  return '';
}

function fileBaseName(path) {
  const name = path.split(/[\\/]/).pop() || '';
  return name.replace(/\.[^.\\/]+$/, '') || '本地视频';
}

function renderLocalSelection() {
  const hasSelection = Boolean(state.local.path);
  elements.localSelection.hidden = !hasSelection;
  elements.localTitle.textContent = state.local.title;
  elements.localPath.textContent = state.local.path;
  elements.localPath.title = state.local.path;
  updateStartState();
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
    state.bilibili.entryIndex = Number(info.entries[0].index) || 0;
    renderBilibiliInfo();
    showToast('视频信息读取成功，尚未创建任务。', 'success');
  });
}

function renderBilibiliInfo() {
  const info = state.bilibili.info;
  const hasInfo = Boolean(info && Array.isArray(info.entries) && info.entries.length);
  elements.bilibiliResult.hidden = !hasInfo;
  elements.bilibiliEntry.replaceChildren();
  if (!hasInfo) {
    updateStartState();
    return;
  }

  elements.bilibiliTitle.textContent = info.title || 'B站课程';
  info.entries.forEach((entry, position) => {
    const option = document.createElement('option');
    const entryIndex = Number.isFinite(Number(entry.index)) ? Number(entry.index) : position;
    option.value = String(entryIndex);
    const duration = formatDuration(entry.duration);
    option.textContent = `P${position + 1} · ${entry.title || `分P ${position + 1}`}${duration ? ` · ${duration}` : ''}`;
    elements.bilibiliEntry.appendChild(option);
  });
  const available = info.entries.some((entry, position) => {
    const entryIndex = Number.isFinite(Number(entry.index)) ? Number(entry.index) : position;
    return entryIndex === state.bilibili.entryIndex;
  });
  if (!available) {
    state.bilibili.entryIndex = Number(info.entries[0].value || info.entries[0].index) || 0;
  }
  elements.bilibiliEntry.value = String(state.bilibili.entryIndex);
  updateStartState();
}

function updateStartState() {
  let canStart = false;
  if (state.sourceType === 'local') {
    canStart = Boolean(state.local.path);
    elements.sourceHint.textContent = canStart
      ? `已选择《${state.local.title}》，点击按钮开始转写和总结。`
      : '请先选择一个本地视频文件。';
  } else {
    const info = state.bilibili.info;
    canStart = Boolean(
      info && info.entries && info.entries.length &&
      elements.bilibiliUrl.value.trim() === state.bilibili.infoUrl
    );
    if (canStart) {
      const entry = info.entries.find((item, position) => {
        const index = Number.isFinite(Number(item.index)) ? Number(item.index) : position;
        return index === state.bilibili.entryIndex;
      });
      elements.sourceHint.textContent = `将总结《${entry && entry.title ? entry.title : info.title}》，点击按钮后才会开始处理。`;
    } else {
      elements.sourceHint.textContent = '请先输入链接并读取视频信息。';
    }
  }
  const sourceBusy = elements.browseButton.dataset.busy === 'true' || elements.bilibiliInfoButton.dataset.busy === 'true';
  setButtonDisabled(elements.startButton, !canStart || sourceBusy);
  setButtonDisabled(elements.bilibiliInfoButton, !elements.bilibiliUrl.value.trim());
}

function startNewJob() {
  let payload;
  if (state.sourceType === 'local') {
    if (!state.local.path) {
      showToast('请先选择本地视频。', 'error');
      return;
    }
    payload = {
      source_type: 'local',
      source_ref: state.local.path,
      title: state.local.title,
      entry_index: 0,
      force: false,
      confirm_long: false
    };
  } else {
    if (!state.bilibili.info || elements.bilibiliUrl.value.trim() !== state.bilibili.infoUrl) {
      showToast('请先读取 B站视频信息。', 'error');
      return;
    }
    const selected = state.bilibili.info.entries.find((entry, position) => {
      const index = Number.isFinite(Number(entry.index)) ? Number(entry.index) : position;
      return index === state.bilibili.entryIndex;
    });
    payload = {
      source_type: 'bilibili',
      source_ref: state.bilibili.infoUrl,
      title: selected && selected.title ? selected.title : state.bilibili.info.title,
      entry_index: state.bilibili.entryIndex,
      force: false,
      confirm_long: false
    };
  }
  createJob(payload, elements.startButton);
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
    confirm_long: false
  }, elements.regenerateJob);
}

function createJob(payload, button) {
  withBusy(button, async () => {
    const result = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    if (!result.job || !result.job.id) {
      throw new ApiError('服务未返回有效的任务信息。');
    }
    state.selectedId = result.job.id;
    state.currentJob = result.job;
    state.detailRequest += 1;
    upsertJob(result.job);
    renderHistory();
    renderCurrentJob();
    if (result.reused) {
      showToast('已读取历史总结，没有重复调用模型', 'success');
    } else {
      showToast(payload.force ? '已开始重新生成总结。' : '任务已创建，开始处理视频。', 'success');
    }
    await refreshJobs();
  });
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
    const result = await api('/api/jobs');
    state.jobs = Array.isArray(result.jobs) ? result.jobs : [];
    if (!state.selectedId && state.jobs.length) {
      state.selectedId = state.jobs[0].id;
    }
    if (state.selectedId && !state.jobs.some((job) => job.id === state.selectedId)) {
      state.selectedId = null;
      state.currentJob = null;
      state.detailRequest += 1;
    }
    renderHistory();

    if (state.selectedId) {
      const listed = state.jobs.find((job) => job.id === state.selectedId);
      const current = state.currentJob;
      const needsDetail = Boolean(
        initial || !current || current.id !== state.selectedId ||
        current.transcript === undefined || current.summary === undefined ||
        (listed && (ACTIVE_STATUSES.has(listed.status) || ACTIVE_STATUSES.has(current.status))) ||
        (listed && listed.status !== current.status)
      );
      if (needsDetail) {
        await loadJob(state.selectedId, { silent: !initial });
      } else {
        state.currentJob = { ...current, ...listed };
        renderCurrentJob();
      }
    } else {
      renderCurrentJob();
    }

    elements.historyRefreshStatus.textContent = '每 2 秒自动刷新';
    elements.historyRefreshStatus.classList.remove('is-error');
    state.pollErrorNotified = false;
  } catch (error) {
    elements.historyRefreshStatus.textContent = '更新失败，将自动重试';
    elements.historyRefreshStatus.classList.add('is-error');
    if (initial || !state.pollErrorNotified) {
      showToast(error.message, 'error');
      state.pollErrorNotified = true;
    }
  } finally {
    state.refreshing = false;
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
  state.selectedId = id;
  const listed = state.jobs.find((job) => job.id === id);
  if (!state.currentJob || state.currentJob.id !== id) {
    state.currentJob = listed || null;
  }
  renderHistory();
  renderCurrentJob();
  loadJob(id);
}

function historyGroupKey(job) {
  if (job.cache_key) {
    return String(job.cache_key);
  }
  return `${job.source_type || ''}|${job.source_ref || ''}|${job.entry_index || 0}`;
}

// 同一视频多次尝试只留最新一条；被选中的旧记录仍然展示，避免选中项凭空消失。
function buildHistoryEntries() {
  const groups = new Map();
  state.jobs.forEach((job) => {
    const key = historyGroupKey(job);
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(job);
  });
  const entries = [];
  groups.forEach((list) => {
    list.forEach((job, index) => {
      if (index === 0 || job.id === state.selectedId) {
        entries.push({ job, attempts: list.length });
      }
    });
  });
  return entries;
}

function renderHistory() {
  loadUsageSummary();
  const entries = buildHistoryEntries();
  elements.historyCount.textContent = String(entries.length);
  elements.historyEmpty.hidden = entries.length > 0;
  elements.historyList.hidden = entries.length === 0;
  const fragment = document.createDocumentFragment();

  entries.forEach(({ job, attempts }) => {
    const item = document.createElement('li');
    item.className = 'history-item';
    item.classList.toggle('is-selected', job.id === state.selectedId);

    const select = document.createElement('button');
    select.type = 'button';
    select.className = 'history-select';
    select.setAttribute('aria-label', `查看任务：${job.title || '未命名任务'}`);
    if (job.id === state.selectedId) {
      select.setAttribute('aria-current', 'true');
    }

    const title = document.createElement('div');
    title.className = 'history-item-title';
    title.textContent = job.title || '未命名任务';
    title.title = job.title || '未命名任务';
    select.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'history-item-meta';
    const metaLeft = document.createElement('span');
    metaLeft.className = 'history-meta-left';
    const date = document.createElement('span');
    date.className = 'history-date';
    date.textContent = formatDate(job.created_at);
    metaLeft.appendChild(date);
    if (attempts > 1) {
      const attemptsBadge = document.createElement('span');
      attemptsBadge.className = 'history-attempts';
      attemptsBadge.textContent = `共 ${attempts} 次`;
      attemptsBadge.title = `这条视频共处理过 ${attempts} 次，列表中只保留最近一次`;
      metaLeft.appendChild(attemptsBadge);
    }
    meta.appendChild(metaLeft);
    meta.appendChild(createStatusBadge(job.status));
    select.appendChild(meta);

    if (ACTIVE_STATUSES.has(job.status)) {
      const track = document.createElement('div');
      track.className = 'history-mini-progress';
      const bar = document.createElement('span');
      bar.style.width = `${normalizeProgress(job.progress)}%`;
      track.appendChild(bar);
      select.appendChild(track);
    }

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'history-delete';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `删除历史任务：${job.title || '未命名任务'}`);
    setButtonDisabled(remove, ACTIVE_STATUSES.has(job.status));
    remove.addEventListener('click', () => deleteJob(job.id, remove));

    item.appendChild(select);
    item.appendChild(remove);
    select.addEventListener('click', () => selectJob(job.id));
    fragment.appendChild(item);
  });
  elements.historyList.replaceChildren(fragment);
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
  elements.jobEmpty.hidden = hasJob;
  elements.jobDetail.hidden = !hasJob;
  if (!hasJob) {
    return;
  }

  elements.jobTitle.textContent = job.title || '未命名任务';
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
    elements.summaryContent.innerHTML = renderMarkdown(summary);
    state.renderedSummaryJob = job.id;
    state.renderedSummary = summary;
  }
  if (completed) {
    syncModuleState(job);
  }
  const staleBackend = completed && state.backendFeatures !== null && !backendSupports('questions');
  elements.backendStale.hidden = !staleBackend;

  const visualNotes = typeof job.visual_notes === 'string' ? job.visual_notes : '';
  elements.visualDetails.hidden = !visualNotes;
  elements.visualContent.innerHTML = visualNotes ? renderMarkdown(visualNotes) : '';

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
  return [job.id, parseJobQuestions(job).length, state.quizSortByLevel ? 'sorted' : 'plain'].join(':');
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
      elements.summaryContent.innerHTML = renderMarkdown(job.summary);
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

function cancelSelectedJob() {
  const job = state.currentJob;
  if (!job || !ACTIVE_STATUSES.has(job.status)) {
    return;
  }
  withBusy(elements.cancelJob, async () => {
    const updated = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' });
    state.currentJob = updated;
    upsertJob(updated);
    renderHistory();
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
    renderHistory();
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
    renderHistory();
    renderCurrentJob();
    showToast('历史任务已删除。', 'success');
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
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeTextMenu();
    }
  });
  window.addEventListener('scroll', closeTextMenu, true);
  window.addEventListener('resize', closeTextMenu);
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
  elements.settingsDialog.showModal();

  await withBusy(elements.settingsOpen, async () => {
    const config = await api('/api/config');
    state.config = config;
    populateConfig(config);
    updateSettingsActionStates();
  });
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

  lines.forEach((line) => {
    if (inCode) {
      if (/^\s*```/.test(line)) {
        flushCode();
        inCode = false;
      } else {
        codeLines.push(line);
      }
      return;
    }
    if (/^\s*```/.test(line)) {
      flushTextBlocks();
      inCode = true;
      codeLines = [];
      return;
    }
    if (!line.trim()) {
      flushTextBlocks();
      return;
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (heading) {
      flushTextBlocks();
      const level = heading[1].length;
      output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      return;
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
      return;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      quoteLines.push(quote[1]);
      return;
    }

    flushList();
    flushQuote();
    paragraph.push(line.trim());
  });

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
