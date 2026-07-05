/**
 * mimi3 运维控制台。
 */

const ST = {
    running:             { label: '运行中',      cls: 'badge-running' },
    cooldown:            { label: '冷却中',      cls: 'badge-cooldown' },
    idle:                { label: '空闲中',      cls: 'badge-idle' },
    token_invalid:       { label: 'token 失效', cls: 'badge-token_invalid' },
    active:              { label: '活跃',        cls: 'badge-active' },
    needs_deploy:        { label: '待部署',      cls: 'badge-needs_deploy' },
    deploying:           { label: '部署中',      cls: 'badge-deploying' },
    relogin_needed:      { label: '凭据失效',    cls: 'badge-relogin_needed' },
    disabled:            { label: '已禁用',      cls: 'badge-disabled' },
};

const FILTERS = {
    all: () => true,
    running: r => rowWorkbenchState(r) === 'running',
    cooldown: r => rowWorkbenchState(r) === 'cooldown',
    idle: r => rowWorkbenchState(r) === 'idle',
    token_invalid: r => rowWorkbenchState(r) === 'token_invalid',
};

const WORKBENCH_ORDER = {
    running: 1,
    cooldown: 2,
    idle: 3,
    token_invalid: 4,
};

const POLL = 5000;
const HANDOFF_WARN_SEC = 1800;

const Icon = {
    check:   '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    x:       '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    info:    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    deploy:  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>',
    spinner:'<span class="spinner"></span>',
};

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const D = {
    navTabs: $$('.nav-tab[data-page]'),
    pageHome: $('#page-home'),
    pageDetail: $('#page-detail'),
    pageScheduler: $('#page-scheduler'),
    pageHistory: $('#page-history'),
    pageConfig: $('#page-config'),
    homeSchedulerControls: $('#home-scheduler-controls'),
    homeActive: $('#home-active'),
    homeTarget: $('#home-target'),
    homeCoverage: $('#home-coverage'),
    homeActiveBar: $('#home-active-bar'),
    homeNextAction: $('#home-next-action'),
    homeNextDetail: $('#home-next-detail'),
    homeReserve: $('#home-reserve'),
    homeReserveNote: $('#home-reserve-note'),
    homeDue: $('#home-due'),
    homeDueNote: $('#home-due-note'),
    homeRisk: $('#home-risk'),
    homeRiskNote: $('#home-risk-note'),
    homeCooldown: $('#home-cooldown'),
    homeCooldownNote: $('#home-cooldown-note'),
    homeRunningCount: $('#home-running-count'),
    homeRunningList: $('#home-running-list'),
    homeActionCount: $('#home-action-count'),
    homeActionList: $('#home-action-list'),
    btnOpenDetail: $('#btn-open-detail'),
    runningCount: $('#running-count'),
    targetCount: $('#target-count'),
    coverage: $('#coverage-status'),
    activeBar: $('#active-target-bar'),
    activeMarker: $('#active-target-marker'),
    runningSummary: $('#running-summary'),
    runningStrip: $('#running-strip'),
    criticalCount: $('#critical-count'),
    criticalQueue: $('#critical-queue'),
    cards: $('#overview-cards'),
    poolTotal: $('#pool-total'),
    poolSegment: $('#pool-segment'),
    poolLegend: $('#pool-legend'),
    sched: $('#scheduler-content'),
    schedulerStatusStrip: $('#scheduler-status-strip'),
    schedulerActions: $('#scheduler-actions'),
    schedulerOperationBadge: $('#scheduler-operation-badge'),
    schedulerQueue: $('#scheduler-queue'),
    schedulerDueCount: $('#scheduler-due-count'),
    schedulerLastTime: $('#scheduler-last-time'),
    schedulerLastOperation: $('#scheduler-last-operation'),
    configStatus: $('#config-status'),
    configUpdated: $('#config-updated'),
    tbody: $('#table-body'),
    empty: $('#table-empty'),
    count: $('#account-count'),
    search: $('#account-search'),
    sort: $('#account-sort'),
    btnImportAccounts: $('#btn-import-accounts'),
    side: $('#side-panel'),
    sideContent: $('#side-content'),
    sideClose: $('#btn-close-side'),
    ts: $('#refresh-indicator'),
    btnR: $('#btn-refresh'),
    btnAuto: $('#btn-auto-refresh'),
    btnCfg: $('#btn-reload-config'),
    btnLogout: $('#btn-logout'),
    historyTotal: $('#history-total'),
    historySuccess: $('#history-success'),
    historyFailed: $('#history-failed'),
    historyCooldown: $('#history-cooldown'),
    historyAction: $('#history-action'),
    historyList: $('#history-list'),
    historyRefresh: $('#btn-history-refresh'),
    historyWorkbench: $('#btn-history-workbench'),
    historyFilters: $$('.history-filter-btn'),
    historyLimitForm: $('#history-limit-form'),
    historyLimitInput: $('#history-limit-input'),
    historyLimitApply: $('#btn-history-limit-apply'),
    historyLimitSave: $('#btn-history-limit-save'),
    historyLimitNote: $('#history-limit-note'),
    historyShowing: $('#history-showing'),
    configPageContent: $('#config-page-content'),
    configPageReload: $('#btn-config-page-reload'),
    toast: $('#toast-container'),
};

const state = {
    snapshot: [],
    plan: null,
    config: null,
    history: { events: [], summary: {} },
    auth: { required: false, authenticated: true },
    page: 'home',
    filter: 'all',
    query: '',
    sort: 'priority',
    historyFilter: 'all',
    historyLimit: null,
    historyLimitSaving: false,
    selectedUid: null,
    pending: {},
    lastAction: null,
    configLoadedAt: null,
    configFormDirty: false,
    promptTemplates: null,
    promptTemplatesLoadedAt: null,
    scheduler: {
        status: null,
        lastOperation: null,
        pendingAction: null,
    },
};

// Toast
const ICO = { success: Icon.check, error: Icon.x, info: Icon.info };
function toast(m, t = 'info', d = 4000) {
    const e = document.createElement('div');
    e.className = `toast toast-${t}`;
    e.innerHTML = `<span class="toast-icon">${ICO[t] || ICO.info}</span><span>${esc(m)}</span>`;
    D.toast.appendChild(e);
    setTimeout(() => { e.classList.add('removing'); setTimeout(() => e.remove(), 180); }, d);
}

function confirm(title, msg) {
    return new Promise(r => {
        const o = document.createElement('div');
        o.id = 'confirm-overlay';
        o.innerHTML = `<div id="confirm-box"><h3>${esc(title)}</h3><p>${esc(msg)}</p><div class="actions"><button class="action-btn action-btn-ghost" data-a="cancel">取消</button><button class="action-btn action-btn-danger" data-a="confirm">确认</button></div></div>`;
        document.body.appendChild(o);
        o.addEventListener('click', e => {
            const a = e.target.dataset.a;
            if (a === 'confirm') { o.remove(); r(true); }
            if (a === 'cancel')  { o.remove(); r(false); }
        });
    });
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}

function fd(sec) {
    if (sec == null || sec <= 0) return '—';
    if (sec < 60) return `${Math.floor(sec)}s`;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h${m}m` : `${m}m`;
}

function fs(sec) {
    if (sec == null) return '—';
    if (sec < 60) return `${Math.floor(sec)}s`;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h${m}m` : `${m}m`;
}

function ago(ts) {
    if (!ts) return '—';
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 5) return '刚刚';
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}min` : `${Math.floor(m / 60)}h`;
}

async function json(url, opts = {}) {
    const r = await fetch(url, { headers: { Accept: 'application/json', ...opts.headers }, ...opts });
    if (!r.ok) {
        const t = await r.text().catch(() => '');
        if (r.status === 401 && !url.startsWith('/api/auth/')) showAuthOverlay('请先登录工作台');
        throw new Error(`${r.status}${t ? ': ' + t.slice(0, 100) : ''}`);
    }
    return r.json();
}

function showAuthOverlay(message = '') {
    let overlay = $('#auth-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'auth-overlay';
        overlay.innerHTML = `<form id="auth-box">
            <span class="logo">mimi3</span>
            <h2>工作台登录</h2>
            <p>请输入工作台密码后继续操作。</p>
            <input id="auth-password" type="password" autocomplete="current-password" placeholder="工作台密码">
            <button class="action-btn action-btn-primary" type="submit">登录</button>
            <span id="auth-message"></span>
        </form>`;
        document.body.appendChild(overlay);
        overlay.querySelector('#auth-box').addEventListener('submit', handleLogin);
    }
    overlay.classList.remove('hidden');
    overlay.querySelector('#auth-message').textContent = message;
    setTimeout(() => overlay.querySelector('#auth-password')?.focus(), 40);
}

function hideAuthOverlay() {
    $('#auth-overlay')?.classList.add('hidden');
}

function renderAuthControls() {
    if (!D.btnLogout) return;
    const showLogout = Boolean(state.auth.required);
    D.btnLogout.hidden = !showLogout;
    D.btnLogout.style.display = showLogout ? '' : 'none';
    D.btnLogout.disabled = showLogout && !state.auth.authenticated;
}

async function ensureAuth() {
    const auth = await json('/api/auth/status');
    state.auth = auth;
    renderAuthControls();
    if (auth.required && !auth.authenticated) {
        showAuthOverlay();
        return false;
    }
    hideAuthOverlay();
    return true;
}

async function handleLogin(e) {
    e.preventDefault();
    const input = $('#auth-password');
    const message = $('#auth-message');
    try {
        await json('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: input.value }),
        });
        input.value = '';
        hideAuthOverlay();
        await refresh();
        start();
    } catch (err) {
        message.textContent = `登录失败: ${err.message}`;
    }
}

async function handleLogout() {
    if (!state.auth.required) return;
    try {
        await json('/api/auth/logout', { method: 'POST' });
        stop();
        state.auth = { required: true, authenticated: false };
        renderAuthControls();
        showAuthOverlay('已退出登录');
        toast('已退出登录', 'success');
    } catch (err) {
        toast(`退出失败：${err.message}`, 'error');
    }
}

function byState(rows) {
    return rows.reduce((acc, r) => ({ ...acc, [r.deploy_state]: (acc[r.deploy_state] || 0) + 1 }), {});
}

function byWorkbenchState(rows) {
    return rows.reduce((acc, r) => {
        const key = rowWorkbenchState(r);
        return { ...acc, [key]: (acc[key] || 0) + 1 };
    }, {});
}

function rowWorkbenchState(r) {
    if (r?.workbench_state === 'rate_limited_retry') return 'cooldown';
    if (r?.workbench_state) return r.workbench_state;
    if (r?.deploy_state === 'active') return 'running';
    if (r?.deploy_state === 'relogin_needed' || (r?.deploy_state === 'disabled' && r?.disabled_reason === 'auth_expired')) return 'token_invalid';
    if (r?.last_result === 'create_rate_limited') return 'cooldown';
    if (r?.deploy_state === 'cooldown' || (r?.cooldown_remaining_sec || 0) > 0) return 'cooldown';
    return 'idle';
}

function getTarget(config) {
    return config?.pool?.min_accounts || 8;
}

function isRisk(r) {
    const ws = rowWorkbenchState(r);
    return ws === 'token_invalid'
        || (ws === 'running' && !connectorValue(r))
        || (r.last_result === 'fail' && (r.consecutive_failures || 0) > 2);
}

function healthInfo(r) {
    const ws = rowWorkbenchState(r);
    if (ws === 'running' && connectorValue(r)) {
        return { label: 'L3 推断正常', cls: 'ok', dot: 'health-dot-ok', title: '运行中，连接器正常' };
    }
    if (ws === 'running') {
        return { label: '退化', cls: 'degraded', dot: 'health-dot-degraded', title: '运行中，但缺少连接器' };
    }
    if (ws === 'token_invalid' || (r.last_result === 'fail' && (r.consecutive_failures || 0) > 2)) {
        return { label: '异常', cls: 'fail', dot: 'health-dot-fail', title: '需要导入新 cookie 或删除账号' };
    }
    if (ws === 'cooldown') {
        const prefix = r.last_result === 'create_rate_limited' ? '7001限流，' : '';
        return { label: '冷却', cls: 'unknown', dot: 'health-dot-unknown', title: `${prefix}冷却剩余 ${fd(r.cooldown_remaining_sec)}` };
    }
    return { label: '待机', cls: 'unknown', dot: 'health-dot-unknown', title: stateInfo(r).label };
}

function stateInfo(r) {
    const ws = rowWorkbenchState(r);
    const fallback = ST[ws] || ST[r?.deploy_state] || { label: ws || r?.deploy_state || '未知', cls: 'badge-idle' };
    return {
        label: r?.workbench_state_label || fallback.label,
        cls: fallback.cls,
    };
}

function connectorValue(r) {
    return r.connector_display || (r.connector_live ? r.connector_id : '');
}

function shortConnector(r) {
    const connector = connectorValue(r);
    return connector ? `${connector.slice(0, 8)}...` : '—';
}

function historicalConnector(r) {
    if (connectorValue(r)) return shortConnector(r);
    return r.connector_id ? `历史 ${r.connector_id.slice(0, 8)}...` : '—';
}

function workbenchRemaining(r) {
    const ws = rowWorkbenchState(r);
    if (ws === 'running') return fd(r.remain_sec);
    if (ws === 'cooldown') return fd(r.cooldown_remaining_sec);
    return '—';
}

function displayReason(value) {
    const map = {
        bootstrap: '初始化部署',
        scheduled: '计划调度',
        handoff: '接力部署',
        needs_deploy: '实例待部署',
        manual: '手动部署',
    };
    return map[value] || value || '-';
}

function displaySchedulerMode(value) {
    const map = {
        idle: '空闲',
        loop: '循环运行',
        planning: '生成计划',
        executing: '执行中',
        stopping: '停止中',
        failed: '异常',
        unknown: '未知',
    };
    return map[value] || value || '未知';
}

function schedulerStatusTone(status) {
    if (!status) return 'info';
    if (status.mode === 'failed' || status.last_error) return 'danger';
    if (status.active_operation || status.mode === 'executing' || status.mode === 'stopping') return 'warning';
    if (status.running) return 'ok';
    return 'info';
}

function displayResult(value) {
    const map = {
        success: '成功',
        fail: '失败',
        create_failed: '创建失败',
        create_rate_limited: '7001限流',
        create_peak_rate_limited: '高峰限流',
        deploy_failed: '部署失败',
        relogin_needed: '凭据失效',
    };
    return map[value] || value || '—';
}

function clampHistoryLimit(value) {
    const n = Number.parseInt(value, 10);
    if (!Number.isFinite(n)) return 10;
    return Math.max(1, Math.min(n, 200));
}

function configuredHistoryLimit(config = state.config) {
    return clampHistoryLimit(config?.webui?.history_limit ?? 10);
}

function currentHistoryLimit() {
    return state.historyLimit == null ? configuredHistoryLimit() : clampHistoryLimit(state.historyLimit);
}

function historyRequestUrl() {
    return state.historyLimit == null ? '/api/history' : `/api/history?limit=${currentHistoryLimit()}`;
}

function syncHistoryLimitFromConfig(config) {
    if (state.historyLimit == null && config) state.historyLimit = configuredHistoryLimit(config);
}

function syncHistoryLimitControls() {
    if (!D.historyLimitInput) return;
    const limit = currentHistoryLimit();
    if (document.activeElement !== D.historyLimitInput) D.historyLimitInput.value = String(limit);
    if (D.historyLimitNote) {
        const saved = configuredHistoryLimit();
        D.historyLimitNote.textContent = `当前显示最近 ${limit} 条，保存默认后新打开也按 ${limit} 条加载。已保存默认：${saved} 条。`;
    }
    if (D.historyLimitApply) D.historyLimitApply.disabled = state.historyLimitSaving;
    if (D.historyLimitSave) D.historyLimitSave.disabled = state.historyLimitSaving;
}

function pendingKey(uid, action) {
    return `${uid}:${action}`;
}

function isPending(uid, action) {
    return Boolean(state.pending[pendingKey(uid, action)]);
}

function hasPending(uid) {
    return Object.keys(state.pending).some(k => k.startsWith(`${uid}:`));
}

function setPending(uid, action, value) {
    state.pending = value
        ? { ...state.pending, [pendingKey(uid, action)]: true }
        : Object.fromEntries(Object.entries(state.pending).filter(([k]) => k !== pendingKey(uid, action)));
    renderTable();
    syncSidePanel();
}

function setConfigPending(value) {
    state.pending = value
        ? { ...state.pending, config: true }
        : Object.fromEntries(Object.entries(state.pending).filter(([k]) => k !== 'config'));
    renderConfigStatus();
}

function setSchedulerPending(action, value) {
    state.scheduler.pendingAction = value ? action : null;
    renderSchedulerControls();
    renderSchedulerPage();
}

function isConfigFormActive() {
    const forms = ['#project-config-form', '#prompt-template-form']
        .map(selector => $(selector))
        .filter(Boolean);
    return forms.some(form => state.configFormDirty || form.contains(document.activeElement));
}

function currentPromptId() {
    return String(state.config?.deploy?.prompt_id || state.promptTemplates?.current_prompt_id || '');
}

function currentPromptTemplate() {
    const promptId = currentPromptId();
    return (state.promptTemplates?.templates || []).find(t => t.prompt_id === promptId) || null;
}

async function loadPromptTemplates(force = false) {
    if (!force && state.promptTemplatesLoadedAt) return state.promptTemplates;
    state.promptTemplates = await json('/api/prompt-templates');
    state.promptTemplatesLoadedAt = Date.now();
    return state.promptTemplates;
}

function actionLabel(action, running = false) {
    const labels = {
        deploy: running ? '部署中...' : '部署',
        enable: running ? '启用中...' : '启用',
        disable: running ? '禁用中...' : '禁用',
        delete: running ? '删除中...' : '删除',
    };
    return labels[action] || action;
}

function actionButton(r, action, opts = {}) {
    const uid = esc(r.uid);
    const pending = isPending(r.uid, action);
    const disabled = opts.disabled || pending || hasPending(r.uid);
    const cls = opts.cls || 'action-btn-ghost';
    const icon = pending ? Icon.spinner : (opts.icon || '');
    const title = opts.title ? ` title="${esc(opts.title)}"` : '';
    const stateAttr = opts.state ? ` data-state="${esc(opts.state)}"` : '';
    return `<button class="action-btn ${cls}" data-a="${esc(action)}" data-uid="${uid}"${stateAttr}${title}${disabled ? ' disabled' : ''}>${icon}${icon ? ' ' : ''}${actionLabel(action, pending)}</button>`;
}

function renderAll(status, plan, config, historyData, schedulerStatus, opts = {}) {
    state.snapshot = status.snapshot || [];
    state.plan = plan || null;
    state.history = historyData || { events: [], summary: {} };
    state.scheduler.status = schedulerStatus || null;
    if (config) {
        state.config = config;
        syncHistoryLimitFromConfig(config);
        state.configLoadedAt = Date.now();
    }
    renderHome();
    renderSchedulerControls();
    renderSchedulerPage();
    renderCommandCenter();
    renderCards(status);
    renderPool();
    renderSched(plan);
    renderConfigStatus();
    renderHistory();
    if (!(opts.preserveConfigPage && state.page === 'config') && (!opts.preserveConfigForm || !isConfigFormActive())) {
        renderConfigPage();
    }
    renderTable();
    syncSidePanel();
}

function renderHome() {
    const rows = state.snapshot;
    const plan = state.plan || {};
    const target = getTarget(state.config);
    const bs = byWorkbenchState(rows);
    const active = rows.filter(r => rowWorkbenchState(r) === 'running');
    const reserve = rows.filter(r => r.eligible === true);
    const risks = riskQueue(rows, plan);
    const due = plan.due_deploys || [];
    const pct = Math.max(0, Math.min(100, target ? (active.length / target) * 100 : 0));
    const coverageCls = plan.coverage_gap ? 'danger' : plan.coverage_risk ? 'warning' : active.length >= target ? 'ok' : 'warning';
    const coverageText = plan.coverage_gap ? '覆盖缺口' : plan.coverage_risk ? '覆盖风险' : active.length >= target ? '覆盖正常' : '低于目标';

    D.homeActive.textContent = active.length;
    D.homeTarget.textContent = target;
    D.homeCoverage.className = `coverage-pill coverage-${coverageCls}`;
    D.homeCoverage.textContent = coverageText;
    D.homeActiveBar.style.width = `${pct}%`;

    const next = due[0];
    D.homeNextAction.textContent = next ? `部署 ${next.uid}` : active.length >= target ? '保持轮询' : '等待调度';
    D.homeNextDetail.textContent = next ? displayReason(next.reason || 'scheduled') : `错峰 ${fs(plan.stagger_interval)}`;

    D.homeReserve.textContent = reserve.length;
    D.homeReserveNote.textContent = `${plan.eligible_count ?? reserve.length} 个可部署`;
    D.homeDue.textContent = due.length;
    D.homeDueNote.textContent = due[0] ? `${due[0].uid} · ${displayReason(due[0].reason || 'scheduled')}` : '暂无待部署';
    D.homeRisk.textContent = risks.filter(x => x.type === 'risk').length;
    D.homeRiskNote.textContent = risks.some(x => x.type === 'risk') ? '需要处理' : '状态平稳';
    D.homeCooldown.textContent = bs.cooldown || 0;
    D.homeCooldownNote.textContent = (bs.cooldown || 0) ? '等待释放' : '无冷却账号';

    D.homeRunningCount.textContent = `${active.length} 个`;
    D.homeRunningList.innerHTML = active.length
        ? active.sort((a, b) => (a.remain_sec ?? 999999) - (b.remain_sec ?? 999999)).slice(0, 5).map(renderHomeAccount).join('')
        : '<div class="home-empty"><strong>当前无运行账号</strong><span>运行详情仅展示状态，总表在账号工作台。</span></div>';

    D.homeActionCount.textContent = `${risks.length} 项`;
    D.homeActionList.innerHTML = risks.length
        ? risks.slice(0, 5).map(renderHomeAction).join('')
        : '<div class="home-empty"><strong>暂无待处理事项</strong><span>继续自动刷新即可。</span></div>';

    attachCardEvents(D.homeRunningList);
    attachCardEvents(D.homeActionList);
}

function renderHomeAccount(r) {
    const h = healthInfo(r);
    return `<button class="home-row" data-uid="${esc(r.uid)}">
        <span class="mono">${esc(r.uid)}</span>
        <span class="health-label health-${h.cls}"><span class="health-dot ${h.dot}"></span>${h.label}</span>
        <strong>${fd(r.remain_sec)}</strong>
    </button>`;
}

function renderHomeAction(item) {
    return `<button class="home-row home-row-action" data-uid="${esc(item.uid)}">
        <span>${esc(item.title)}</span>
        <span class="mono">${esc(item.uid)}</span>
        <strong>${esc(item.detail)}</strong>
    </button>`;
}

function renderCommandCenter() {
    const rows = state.snapshot;
    const plan = state.plan || {};
    const target = getTarget(state.config);
    const active = rows.filter(r => rowWorkbenchState(r) === 'running');
    const pct = Math.max(0, Math.min(100, target ? (active.length / target) * 100 : 0));
    const targetPct = Math.max(0, Math.min(100, target ? 100 : 0));
    const risks = riskQueue(rows, plan);

    D.runningCount.textContent = active.length;
    D.targetCount.textContent = target;
    D.runningSummary.textContent = `${active.length} 个运行`;
    D.criticalCount.textContent = `${risks.length} 项`;
    D.activeBar.style.width = `${pct}%`;
    D.activeMarker.style.left = `${targetPct}%`;

    const coverageCls = plan.coverage_gap ? 'danger' : plan.coverage_risk ? 'warning' : active.length >= target ? 'ok' : 'warning';
    const coverageText = plan.coverage_gap ? '覆盖缺口' : plan.coverage_risk ? '覆盖风险' : active.length >= target ? '覆盖正常' : '低于目标';
    D.coverage.className = `coverage-pill coverage-${coverageCls}`;
    D.coverage.textContent = coverageText;

    D.runningStrip.innerHTML = active.length
        ? active.sort((a, b) => (a.remain_sec ?? 999999) - (b.remain_sec ?? 999999)).map(renderRunningCard).join('')
        : renderEmptyRunning(plan);

    D.criticalQueue.innerHTML = risks.length
        ? risks.slice(0, 6).map(renderCriticalItem).join('')
        : '<div class="quiet-ok">暂无紧急事项</div>';

    attachCardEvents(D.runningStrip);
    attachCardEvents(D.criticalQueue);
}

function renderRunningCard(r) {
    const h = healthInfo(r);
    const warn = r.remain_sec != null && r.remain_sec <= HANDOFF_WARN_SEC;
    return `<button class="running-card ${warn ? 'running-card-warn' : ''}" data-uid="${esc(r.uid)}">
        <span class="running-card-top"><span class="mono">${esc(r.uid)}</span><span class="health-label health-${h.cls}"><span class="health-dot ${h.dot}"></span>${h.label}</span></span>
        <span class="running-remain display">${fd(r.remain_sec)}</span>
        <span class="running-meta"><span>${esc(shortConnector(r))}</span><span>${esc(displayResult(r.last_result))}</span></span>
    </button>`;
}

function renderEmptyRunning(plan) {
    const due = (plan.due_deploys || [])[0];
    return `<div class="running-empty">
        <strong>当前无运行账号</strong>
        <span>${due ? `下一次待部署 ${esc(due.uid)} · ${esc(displayReason(due.reason || 'scheduled'))}` : '暂无待执行部署'}</span>
    </div>`;
}

function riskQueue(rows, plan) {
    const due = (plan.due_deploys || []).map(d => ({
        type: 'deploy',
        uid: d.uid,
        title: '待部署',
        detail: d.handoff_from ? `${displayReason(d.reason || 'scheduled')} ← ${d.handoff_from}` : displayReason(d.reason || 'scheduled'),
    }));
    const risky = rows.filter(isRisk).map(r => ({
        type: 'risk',
        uid: r.uid,
        title: stateInfo(r).label,
        detail: healthInfo(r).title,
    }));
    return [...due, ...risky];
}

function renderCriticalItem(item) {
    const cls = item.type === 'deploy' ? 'critical-deploy' : 'critical-risk';
    return `<button class="critical-item ${cls}" data-uid="${esc(item.uid)}">
        <span class="critical-title">${esc(item.title)}</span>
        <span class="critical-uid mono">${esc(item.uid)}</span>
        <span class="critical-detail">${esc(item.detail)}</span>
    </button>`;
}

function renderCards(d) {
    const rows = d.snapshot || [];
    const bs = d.by_workbench_state || byWorkbenchState(rows);
    const target = getTarget(state.config);
    const active = bs.running || 0;
    const cooldown = bs.cooldown || 0;
    const attention = rows.filter(isRisk).length;
    const reserve = rows.filter(r => r.eligible === true).length;
    setText('[data-stat="active"]', active);
    setText('[data-stat="cooldown"]', cooldown);
    setText('[data-stat="attention"]', attention);
    setText('[data-stat="reserve"]', reserve);
    setText('[data-stat-note="active"]', `${Math.max(0, target - active)} 个目标缺口`);
    setText('[data-stat-note="cooldown"]', cooldown ? '等待冷却释放' : '无冷却账号');
    setText('[data-stat-note="attention"]', attention ? '需要人工查看' : '状态平稳');
    setText('[data-stat-note="reserve"]', `${rows.filter(r => r.eligible).length} 个可部署`);
}

function renderPool() {
    const rows = state.snapshot;
    const total = rows.length || 0;
    const bs = byWorkbenchState(rows);
    const segments = [
        ['running', '运行中', bs.running || 0],
        ['cooldown', '冷却中', bs.cooldown || 0],
        ['idle', '空闲中', bs.idle || 0],
        ['token_invalid', 'token 失效', bs.token_invalid || 0],
    ].filter(([, , v]) => v > 0);

    D.poolTotal.textContent = `共 ${total} 个`;
    D.poolSegment.innerHTML = segments.length
        ? segments.map(([key, label, value]) => `<span class="pool-segment-part pool-${key}" style="width:${Math.max((value / total) * 100, 4)}%" title="${esc(label)} ${value}"></span>`).join('')
        : '<span class="pool-segment-part pool-empty" style="width:100%"></span>';
    D.poolLegend.innerHTML = segments.length
        ? segments.map(([key, label, value]) => `<span class="pool-legend-item"><span class="pool-dot pool-${key}"></span>${esc(label)} <strong>${value}</strong></span>`).join('')
        : '<span class="ph">暂无账号数据</span>';
}

function renderSched(p) {
    if (!p) { D.sched.innerHTML = '<div class="ph">无调度数据</div>'; return; }
    const parts = [
        `<span class="sched-tag"><span class="sched-label">错峰</span><span class="sched-value sched-value-active">${fs(p.stagger_interval)}</span></span>`,
        `<span class="sched-tag"><span class="sched-label">活跃</span><span class="sched-value sched-value-active">${p.active_count ?? '?'}</span></span>`,
        `<span class="sched-tag"><span class="sched-label">储备</span><span class="sched-value sched-value-reserve">${p.reserve_size ?? '?'}</span></span>`,
        `<span class="sched-tag"><span class="sched-label">可部署</span><span class="sched-value sched-value-normal">${p.eligible_count ?? '?'}</span></span>`,
    ];
    let h = `<div class="sched-row">${parts.join('')}</div>`;
    const w = [];
    if (p.coverage_gap)  w.push('<span class="warn-badge warn-badge-gap">覆盖缺口</span>');
    if (p.coverage_risk) w.push('<span class="warn-badge warn-badge-risk">覆盖风险</span>');
    if (w.length) h += `<div class="sched-row sched-warnings">${w.join('')}</div>`;

    const int = p.stagger_interval || 86400, pct = Math.min(100, (int / 86400) * 100);
    h += `<div class="sched-bar-container"><div class="sched-bar"><div class="sched-bar-fill sched-bar-active" style="width:${Math.max(pct, 5)}%"></div><div class="sched-bar-fill sched-bar-idle" style="width:${Math.max(100 - pct, 2)}%"></div></div><div class="sched-bar-labels"><span>0h</span><span>${fs(int)} / 24h</span><span>24h</span></div></div>`;

    const dd = p.due_deploys || [];
    if (dd.length) {
        h += '<div class="due-list"><div class="due-title">待执行部署</div>';
        for (const d of dd) {
            const uid = d.uid || '—', reason = displayReason(d.reason || '?'), hf = d.handoff_from ? ` ← ${d.handoff_from}` : '';
            h += `<button class="due-item" data-uid="${esc(uid)}"><span class="due-uid">${esc(uid)}</span><span class="due-reason">${esc(reason)}${esc(hf)}</span></button>`;
        }
        h += '</div>';
    }
    D.sched.innerHTML = h;
    attachCardEvents(D.sched);
}

function schedulerActionButton(action, label, cls = 'action-btn-ghost', disabled = false) {
    const pending = state.scheduler.pendingAction === action;
    return `<button class="action-btn ${cls}" data-scheduler-action="${esc(action)}" ${pending || disabled ? 'disabled' : ''}>${pending ? Icon.spinner + ' ' : ''}${esc(label)}</button>`;
}

function renderSchedulerControls() {
    const status = state.scheduler.status;
    const tone = schedulerStatusTone(status);
    const mode = displaySchedulerMode(status?.mode);
    const running = Boolean(status?.running);
    const busy = Boolean(state.scheduler.pendingAction || status?.active_operation);
    const due = state.plan?.due_deploys?.length ?? status?.due_count ?? 0;
    const html = `<div class="scheduler-mini scheduler-mini-${tone}">
        <div>
            <span class="scheduler-mini-label">调度循环</span>
            <strong>${esc(mode)}</strong>
            <small>${running ? '后台循环运行中' : '当前未运行'} · 待部署 ${due}</small>
        </div>
        <div class="scheduler-mini-actions">
            ${schedulerActionButton(running ? 'stop' : 'start', running ? '停止调度循环' : '启动调度循环', running ? 'action-btn-danger' : 'action-btn-primary', busy)}
            ${schedulerActionButton('tick', '执行一次调度', 'action-btn-ghost', busy || running)}
        </div>
    </div>`;
    if (D.homeSchedulerControls) {
        D.homeSchedulerControls.innerHTML = html;
        bindSchedulerButtons(D.homeSchedulerControls);
    }
}

function renderSchedulerPage() {
    if (!D.pageScheduler) return;
    const status = state.scheduler.status;
    const plan = state.plan || {};
    const due = plan.due_deploys || [];
    const running = Boolean(status?.running);
    const busy = Boolean(state.scheduler.pendingAction || status?.active_operation);
    const tone = schedulerStatusTone(status);

    if (D.schedulerStatusStrip) {
        D.schedulerStatusStrip.innerHTML = `<div class="scheduler-status-card scheduler-status-${tone}">
            <span>状态</span><strong>${esc(displaySchedulerMode(status?.mode))}</strong>
            <small>${status?.last_error ? esc(status.last_error) : `上次 tick：${esc(ago(status?.last_tick_at))}`}</small>
        </div>
        <div class="scheduler-status-card">
            <span>活跃账号</span><strong>${esc(status?.active_count ?? plan.active_count ?? '-')}</strong>
            <small>并发上限 ${esc(status?.max_concurrent_deploys ?? '-')}</small>
        </div>
        <div class="scheduler-status-card">
            <span>待部署</span><strong>${esc(due.length)}</strong>
            <small>${plan.coverage_gap ? '存在覆盖缺口' : plan.coverage_risk ? '存在覆盖风险' : '计划正常'}</small>
        </div>`;
    }

    if (D.schedulerOperationBadge) {
        D.schedulerOperationBadge.textContent = status?.active_operation ? '执行中' : (running ? '循环中' : '空闲');
    }
    if (D.schedulerActions) {
        D.schedulerActions.innerHTML = `<div class="scheduler-action-grid">
            ${schedulerActionButton('start', '启动调度循环', 'action-btn-primary', busy || running)}
            ${schedulerActionButton('stop', '停止调度循环', 'action-btn-danger', busy || !running)}
            ${schedulerActionButton('tick', '执行一次调度', 'action-btn-ghost', busy || running)}
            ${schedulerActionButton('deploy-due', '执行待部署队列', 'action-btn-primary', busy || running || !due.length)}
            ${schedulerActionButton('refresh-plan', '刷新计划', 'action-btn-ghost', Boolean(state.scheduler.pendingAction))}
        </div>`;
        bindSchedulerButtons(D.schedulerActions);
    }

    if (D.schedulerDueCount) D.schedulerDueCount.textContent = `${due.length} 项`;
    if (D.schedulerQueue) {
        D.schedulerQueue.innerHTML = due.length
            ? due.map(renderSchedulerQueueItem).join('')
            : '<div class="home-empty"><strong>暂无待部署任务</strong><span>调度计划没有发现需要立即部署的账号。</span></div>';
        attachCardEvents(D.schedulerQueue);
    }

    const last = state.scheduler.lastOperation;
    if (D.schedulerLastTime) D.schedulerLastTime.textContent = last ? ago(last.finished_at || last.started_at) : '-';
    if (D.schedulerLastOperation) {
        D.schedulerLastOperation.innerHTML = last
            ? `<div class="scheduler-result ${last.success ? 'scheduler-result-ok' : 'scheduler-result-fail'}">
                <strong>${esc(last.title || '调度操作')}</strong>
                <span>${last.success ? '执行成功' : '执行失败'} · ${esc(last.operation_id || '-')}</span>
                <small>${esc(last.error || last.detail || '无错误')}</small>
            </div>`
            : '<div class="home-empty"><strong>暂无调度操作</strong><span>执行调度后会在这里显示结果。</span></div>';
    }
}

function renderSchedulerQueueItem(item) {
    const uid = item.uid || '-';
    return `<button class="scheduler-queue-item" data-uid="${esc(uid)}" ${item.uid ? '' : 'disabled'}>
        <span class="mono">${esc(uid)}</span>
        <strong>${esc(displayReason(item.reason))}</strong>
        <small>${item.handoff_from ? `接力来源 ${esc(item.handoff_from)}` : '由当前调度计划生成'}</small>
        <span class="result-badge">待执行</span>
    </button>`;
}

function bindSchedulerButtons(root) {
    root.querySelectorAll('[data-scheduler-action]').forEach(btn => {
        btn.addEventListener('click', () => {
            const action = btn.dataset.schedulerAction;
            if (action === 'start') handleSchedulerStart();
            if (action === 'stop') handleSchedulerStop();
            if (action === 'tick') handleSchedulerTick();
            if (action === 'deploy-due') handleDeployDue();
            if (action === 'refresh-plan') refresh({ preserveConfigForm: true, preserveConfigPage: true });
        });
    });
}

function eventTime(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function historyEvents() {
    const events = state.history?.events || [];
    if (state.historyFilter === 'all') return events;
    if (state.historyFilter === 'danger') return events.filter(e => e.severity === 'danger');
    return events.filter(e => e.kind === state.historyFilter);
}

function renderHistory() {
    if (!D.historyList) return;
    const summary = state.history?.summary || {};
    const loadedEvents = state.history?.events || [];
    const events = historyEvents();
    syncHistoryLimitControls();
    D.historyTotal.textContent = summary.total ?? 0;
    D.historySuccess.textContent = summary.success ?? 0;
    D.historyFailed.textContent = summary.failed ?? 0;
    D.historyCooldown.textContent = summary.cooldown ?? 0;
    D.historyAction.textContent = summary.needs_action ?? 0;
    if (D.historyShowing) {
        const filter = state.historyFilter === 'all' ? '全部事件' : D.historyFilters.find(btn => btn.dataset.historyFilter === state.historyFilter)?.textContent || '当前筛选';
        D.historyShowing.textContent = `${filter} ${events.length} 条，已载入最近 ${loadedEvents.length} 条，上限 ${currentHistoryLimit()} 条。点击账号可跳转到账号工作台详情。`;
    }

    if (!events.length) {
        D.historyList.innerHTML = '<div class="home-empty"><strong>暂无历史事件</strong><span>刷新后会显示最近部署、冷却和账号处理记录。</span></div>';
        return;
    }

    D.historyList.innerHTML = events.map(renderHistoryItem).join('');
    D.historyList.querySelectorAll('[data-uid]').forEach(item => {
        item.addEventListener('click', () => {
            setPage('detail');
            openSide(item.dataset.uid);
        });
    });
}

function renderHistoryItem(event) {
    const st = stateInfo({ deploy_state: event.state });
    const result = displayResult(event.result);
    const connector = event.connector_id ? `<span class="mono">${esc(String(event.connector_id).slice(0, 8))}...</span>` : '';
    return `<button class="history-item history-${esc(event.severity || 'info')}" data-uid="${esc(event.uid)}">
        <span class="history-time">${esc(eventTime(event.occurred_at))}</span>
        <span class="history-dot" aria-hidden="true"></span>
        <span class="history-main">
            <span class="history-title-row">
                <strong>${esc(event.title)}</strong>
                <span class="badge ${st.cls}">${esc(st.label)}</span>
                <span class="result-badge">${esc(result)}</span>
            </span>
            <span class="history-detail">${esc(event.detail || '—')}</span>
        </span>
        <span class="history-account">
            <span class="mono">${esc(event.uid)}</span>
            ${connector}
        </span>
    </button>`;
}

function setHistoryLimitPending(pending) {
    state.historyLimitSaving = pending;
    syncHistoryLimitControls();
}

async function handleHistoryLimitApply(e) {
    e?.preventDefault();
    const limit = clampHistoryLimit(D.historyLimitInput?.value ?? 10);
    state.historyLimit = limit;
    syncHistoryLimitControls();
    await refresh({ preserveConfigForm: true, preserveConfigPage: true });
    toast(`部署历史已切换为最近 ${limit} 条`, 'success');
}

async function handleHistoryLimitSave() {
    const limit = clampHistoryLimit(D.historyLimitInput?.value ?? 10);
    state.historyLimit = limit;
    setHistoryLimitPending(true);
    try {
        await json('/api/config/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: { history_limit: limit } }),
        });
        recordAction('config', '保存部署历史条数', '成功', true, `默认显示 ${limit} 条`);
        toast(`已保存部署历史默认显示 ${limit} 条`, 'success');
        await refresh({ preserveConfigForm: true, preserveConfigPage: true });
    } catch (err) {
        recordAction('config', '保存部署历史条数', '失败', false, err.message);
        toast(`保存失败: ${err.message}`, 'error');
    } finally {
        setHistoryLimitPending(false);
    }
}

function renderConfigPage() {
    if (!D.configPageContent) return;
    const c = state.config;
    if (!c) {
        D.configPageContent.innerHTML = '<div class="ph">暂无配置数据</div>';
        return;
    }
    const readiness = [
        ['隧道令牌', c.tunnel_token_configured],
        ['代理密钥', c.proxy_api_key_configured],
        ['Cloudflare API 令牌', c.cf_api_token_configured],
        ['Cloudflare 账户', c.cf_account_id_configured],
    ];
    const blocks = [
        ['目标池', `${c.pool?.min_accounts ?? '—'} / ${c.pool?.max_accounts ?? '—'}`, '账号池上下限'],
        ['调度周期', fs(c.scheduler?.tick_seconds), '后台轮询节奏'],
        ['冷却窗口', fs(c.scheduler?.daily_cooldown_seconds), '单账号部署冷却'],
        ['部署并发', c.scheduler?.max_concurrent_deploys ?? '—', '当前只启用一个服务测试'],
        ['健康检查', fs(c.health?.interval_seconds), '活跃账号检查间隔'],
        ['发送超时', fs(c.deploy?.send_timeout), '单次部署等待上限'],
        ['历史条数', c.webui?.history_limit ?? 10, '部署历史默认显示'],
    ];
    const promptId = currentPromptId();
    const promptTemplate = currentPromptTemplate();
    const promptText = promptTemplate?.text || '';
    D.configPageContent.innerHTML = `
        <div class="config-edit-main">
            <section class="panel config-page-card config-primary-card">
                <div class="panel-header"><h2 class="panel-title">项目参数</h2><span class="count-badge">主要配置</span></div>
                <form id="project-config-form" class="project-config-form">
                    <label><span>工作台登录密码</span><input name="WEBUI_PASSWORD" type="password" autocomplete="new-password" placeholder="${c.webui_password_configured ? '已配置，留空不变' : '未配置'}"></label>
                    <label><span>隧道令牌</span><input name="TUNNEL_TOKEN" type="password" autocomplete="off" placeholder="${c.tunnel_token_configured ? '已配置，留空不变' : '未配置'}"></label>
                    <label><span>隧道域名</span><input name="public_hostname" type="text" value="${esc(c.tunnel?.public_hostname || '')}" placeholder="mimo.example.com"></label>
                    <label><span>mimo-claw 监听端口</span><input name="local_port" type="number" min="1" max="65535" value="${esc(c.tunnel?.local_port ?? '')}"></label>
                    <label><span>CF 令牌（可选）</span><input name="CF_API_TOKEN" type="password" autocomplete="off" placeholder="${c.cf_api_token_configured ? '已配置，留空不变' : '未配置'}"></label>
                    <label><span>CF 账户 ID（可选）</span><input name="CF_ACCOUNT_ID" type="password" autocomplete="off" placeholder="${c.cf_account_id_configured ? '已配置，留空不变' : '未配置'}"></label>
                    <label><span>代理密钥</span><input name="PROXY_API_KEY" type="password" autocomplete="off" placeholder="${c.proxy_api_key_configured ? '已配置，留空不变' : '未配置'}"></label>
                    <label><span>号池最低阈值</span><input name="min_accounts" type="number" min="1" max="500" value="${esc(c.pool?.min_accounts ?? '')}"><em>建议大于 8，降低覆盖断档风险。</em></label>
                    <label><span>部署历史显示条数</span><input name="history_limit" type="number" min="1" max="200" value="${esc(c.webui?.history_limit ?? 10)}"><em>默认 10 条，可临时通过历史接口 limit 参数覆盖。</em></label>
                    <div class="project-config-options">
                        <label class="inline-check"><input name="clear_cf_api_token" type="checkbox"> 清空 CF 令牌</label>
                        <label class="inline-check"><input name="clear_cf_account_id" type="checkbox"> 清空 CF 账户 ID</label>
                    </div>
                    <div class="project-config-actions">
                        <span>敏感值只写入 .env，不在前端回显。</span>
                        <button class="action-btn action-btn-primary" type="submit">保存项目参数</button>
                    </div>
                </form>
            </section>
            <section class="panel config-page-card">
                <div class="panel-header"><h2 class="panel-title">提示词模板</h2><span class="count-badge">发给 MiMo Claw</span></div>
                <form id="prompt-template-form" class="config-template-form" data-prompt-id="${esc(promptId)}">
                    <div class="config-template-id">
                        <span>当前生效模板</span>
                        <strong class="mono">${esc(promptId || '—')}</strong>
                    </div>
                    <label class="prompt-template-editor">
                        <span>提示词正文</span>
                        <textarea name="text" ${promptTemplate ? '' : 'disabled'} spellcheck="false" placeholder="${promptTemplate ? '' : '正在加载提示词模板...'}">${esc(promptText)}</textarea>
                    </label>
                    <div class="config-template-actions">
                        <span>${promptTemplate ? `正文 ${esc(promptTemplate.text_length)} 字符` : '模板正文加载中'}</span>
                        <button class="action-btn action-btn-primary" type="submit" ${promptTemplate ? '' : 'disabled'}>保存提示词模板</button>
                    </div>
                </form>
            </section>
        </div>
        <aside class="config-edit-side">
            <section class="panel config-page-card config-summary-card">
                <div class="panel-header"><h2 class="panel-title">运行摘要</h2><span class="count-badge">${esc(ago(state.configLoadedAt / 1000))}</span></div>
                <div class="config-page-metrics">
                    ${blocks.map(([label, value, note]) => `<div class="config-page-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><em>${esc(note)}</em></div>`).join('')}
                </div>
            </section>
            <section class="panel config-page-card config-summary-card">
                <div class="panel-header"><h2 class="panel-title">令牌就绪</h2><span class="count-badge">${readiness.filter(([, ok]) => ok).length} / ${readiness.length}</span></div>
                <div class="config-page-secrets">
                    ${readiness.map(([label, ok]) => `<span class="config-secret ${ok ? 'config-secret-ok' : 'config-secret-miss'}"><span class="health-dot ${ok ? 'health-dot-ok' : 'health-dot-fail'}"></span>${esc(label)} · ${ok ? '已配置' : '缺失'}</span>`).join('')}
                </div>
            </section>
        </aside>`;
    const form = D.configPageContent.querySelector('#project-config-form');
    const promptForm = D.configPageContent.querySelector('#prompt-template-form');
    promptForm?.addEventListener('submit', handleSavePromptTemplate);
    promptForm?.addEventListener('input', () => { state.configFormDirty = true; });
    promptForm?.addEventListener('change', () => { state.configFormDirty = true; });
    form?.addEventListener('submit', handleSaveProjectConfig);
    form?.addEventListener('input', () => { state.configFormDirty = true; });
    form?.addEventListener('change', () => { state.configFormDirty = true; });
    if (!state.promptTemplatesLoadedAt) {
        loadPromptTemplates().then(() => {
            if (state.page === 'config' && !isConfigFormActive()) renderConfigPage();
        }).catch(err => toast(`提示词模板加载失败: ${err.message}`, 'error'));
    }
}

function renderConfigStatus() {
    if (!D.configStatus || !D.configUpdated) return;
    const c = state.config;
    const pending = Boolean(state.pending.config);
    D.configUpdated.textContent = pending ? '重载中' : state.configLoadedAt ? ago(state.configLoadedAt / 1000) : '-';
    if (!c) {
        D.configStatus.innerHTML = '<div class="ph">暂无配置数据</div>';
        return;
    }
    const secrets = [
        ['隧道令牌', c.tunnel_token_configured],
        ['代理密钥', c.proxy_api_key_configured],
        ['Cloudflare API 令牌', c.cf_api_token_configured],
        ['Cloudflare 账户', c.cf_account_id_configured],
    ];
    D.configStatus.innerHTML = `<div class="config-grid">
        <div class="config-metric"><span>目标池</span><strong>${esc(c.pool?.min_accounts ?? '—')} / ${esc(c.pool?.max_accounts ?? '—')}</strong></div>
        <div class="config-metric"><span>调度周期</span><strong>${fs(c.scheduler?.tick_seconds)}</strong></div>
        <div class="config-metric"><span>部署并发</span><strong>${esc(c.scheduler?.max_concurrent_deploys ?? '—')}</strong></div>
        <div class="config-metric"><span>健康检查</span><strong>${fs(c.health?.interval_seconds)}</strong></div>
    </div>
    <div class="config-secret-list">
        ${secrets.map(([label, ok]) => `<span class="config-secret ${ok ? 'config-secret-ok' : 'config-secret-miss'}"><span class="health-dot ${ok ? 'health-dot-ok' : 'health-dot-fail'}"></span>${esc(label)}</span>`).join('')}
    </div>
    <div class="config-foot">
        <span>提示模板: <strong class="mono">${esc(c.deploy?.prompt_id || '—')}</strong></span>
        <button class="action-btn action-btn-ghost" data-a="reload-config" ${pending ? 'disabled' : ''}>${pending ? Icon.spinner + ' 重载中...' : '重载配置'}</button>
    </div>`;
    D.configStatus.querySelector('[data-a="reload-config"]')?.addEventListener('click', handleReloadCfg);
}

function renderTable() {
    const rows = visibleRows();
    if (!state.snapshot.length) {
        D.tbody.innerHTML = '';
        D.empty.classList.remove('hidden');
        D.count.textContent = '0 个账号';
        return;
    }
    if (!rows.length) {
        D.tbody.innerHTML = '';
        D.empty.classList.remove('hidden');
        D.count.textContent = `0 / ${state.snapshot.length}`;
        return;
    }
    D.empty.classList.add('hidden');
    D.count.textContent = `${rows.length} / ${state.snapshot.length}`;
    D.tbody.innerHTML = rows.map(renderRow).join('');
    D.tbody.querySelectorAll('[data-a="deploy"]').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); handleDeploy(b.dataset.uid); }));
    D.tbody.querySelectorAll('[data-a="enable"]').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); handleEnable(b.dataset.uid); }));
    D.tbody.querySelectorAll('[data-a="disable"]').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); handleDisable(b.dataset.uid); }));
    D.tbody.querySelectorAll('[data-a="delete"]').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); handleDeleteAccount(b.dataset.uid); }));
    D.tbody.querySelectorAll('tr[data-uid]').forEach(row => row.addEventListener('click', () => openSide(row.dataset.uid)));
}

function visibleRows() {
    const q = state.query.trim().toLowerCase();
    const filter = FILTERS[state.filter] || FILTERS.all;
    return state.snapshot
        .filter(filter)
        .filter(r => !q || [r.uid, r.name, r.connector_display, r.connector_id, r.last_result, r.workbench_state_label].some(v => String(v || '').toLowerCase().includes(q)))
        .sort(compareRows);
}

function compareRows(a, b) {
    if (state.sort === 'remain') return remainingValue(a) - remainingValue(b) || String(a.uid).localeCompare(String(b.uid));
    if (state.sort === 'state') return priority(a) - priority(b) || String(a.uid).localeCompare(String(b.uid));
    if (state.sort === 'uid') return String(a.uid).localeCompare(String(b.uid));
    return priority(a) - priority(b) || remainingValue(a) - remainingValue(b) || String(a.uid).localeCompare(String(b.uid));
}

function priority(r) {
    return WORKBENCH_ORDER[rowWorkbenchState(r)] || 9;
}

function remainingValue(r) {
    const ws = rowWorkbenchState(r);
    if (ws === 'running') return r.remain_sec ?? 999999999;
    if (ws === 'cooldown') return r.cooldown_remaining_sec ?? 999999999;
    return 999999999;
}

function renderRow(r) {
    const st = stateInfo(r);
    const h = healthInfo(r);
    const ws = rowWorkbenchState(r);
    const can = r.eligible && r.deploy_state !== 'deploying' && ws !== 'token_invalid';
    const dis = r.deploy_state === 'disabled';
    const tokenInvalid = ws === 'token_invalid';
    const fCls = r.consecutive_failures > 2 ? 'color:var(--danger)' : 'color:var(--text-muted)';
    const deploy = actionButton(r, 'deploy', { cls: can ? 'action-btn-primary' : 'action-btn-ghost', icon: Icon.deploy, disabled: !can, title: can ? '部署账号' : '当前状态不可部署' });
    const toggle = dis
        ? actionButton(r, 'enable', { cls: 'action-btn-primary', state: 'disabled', title: '启用账号' })
        : actionButton(r, 'disable', { cls: 'action-btn-danger', state: r.deploy_state, title: '禁用账号' });
    const del = tokenInvalid ? actionButton(r, 'delete', { cls: 'action-btn-danger', state: ws, title: '删除 token 失效账号' }) : '';
    return `<tr data-uid="${esc(r.uid)}">
        <td>${esc(r.uid)}</td>
        <td><span class="badge ${st.cls}">${st.label}</span></td>
        <td class="tar mono" style="color:var(--text-muted)">${workbenchRemaining(r)}</td>
        <td><span class="health-label health-${h.cls}" title="${esc(h.title)}"><span class="health-dot ${h.dot}"></span>${h.label}</span></td>
        <td class="hidden md mono" style="color:var(--text-muted)">${esc(shortConnector(r))}</td>
        <td class="tac hidden lg mono" style="${fCls}">${r.consecutive_failures ?? 0}</td>
        <td class="hidden lg"><span class="result-badge">${esc(displayResult(r.last_result))}</span></td>
        <td class="tar"><div class="row-actions">${deploy}${tokenInvalid ? del : toggle}</div></td>
    </tr>`;
}

function openSide(uid) {
    const row = state.snapshot.find(r => String(r.uid) === String(uid));
    if (!row) return;
    state.selectedUid = row.uid;
    D.side.classList.remove('hidden');
    renderSide(row);
}

function closeSide() {
    state.selectedUid = null;
    D.side.classList.add('hidden');
}

function syncSidePanel() {
    if (!state.selectedUid) return;
    const row = state.snapshot.find(r => String(r.uid) === String(state.selectedUid));
    if (row) renderSide(row);
    else closeSide();
}

function renderSide(r) {
    const st = stateInfo(r);
    const h = healthInfo(r);
    const ws = rowWorkbenchState(r);
    const canDeploy = r.eligible && r.deploy_state !== 'deploying' && ws !== 'token_invalid';
    const isDisabled = r.deploy_state === 'disabled';
    const tokenInvalid = ws === 'token_invalid';
    const last = state.lastAction && String(state.lastAction.uid) === String(r.uid) ? state.lastAction : null;
    const deployBtn = actionButton(r, 'deploy', { cls: canDeploy ? 'action-btn-primary' : 'action-btn-ghost', icon: Icon.deploy, disabled: !canDeploy, title: canDeploy ? '部署当前账号' : '当前状态不可部署' });
    const toggleBtn = isDisabled
        ? actionButton(r, 'enable', { cls: 'action-btn-primary', state: 'disabled', title: '启用当前账号' })
        : actionButton(r, 'disable', { cls: 'action-btn-danger', state: r.deploy_state, title: '禁用当前账号' });
    const deleteBtn = tokenInvalid ? actionButton(r, 'delete', { cls: 'action-btn-danger', state: ws, title: '删除 token 失效账号' }) : '';
    const items = [
        ['UID', r.uid],
        ['名称', r.name || '—'],
        ['状态', st.label],
        ['状态说明', r.state_detail || '—'],
        ['健康', h.title],
        ['剩余/等待', workbenchRemaining(r)],
        ['在线连接器', shortConnector(r)],
        ['历史连接器', historicalConnector(r)],
        ['最近结果', displayResult(r.last_result)],
        ['错误详情', r.last_error_detail || '—'],
        ['连续失败', r.consecutive_failures ?? 0],
        ['可调度', r.eligible ? '是' : '否'],
        ['禁用原因', r.disabled_reason || '—'],
    ];
    D.sideContent.innerHTML = `<div class="side-account-head">
        <span class="mono side-uid">${esc(r.uid)}</span>
        <span class="badge ${st.cls}">${st.label}</span>
    </div>
    <div class="side-health health-${h.cls}"><span class="health-dot ${h.dot}"></span>${h.label}<span>· ${esc(h.title)}</span></div>
    <div class="side-actions" aria-label="账号操作">
        <div class="side-actions-primary">${deployBtn}</div>
        <div class="side-actions-danger">${tokenInvalid ? deleteBtn : toggleBtn}</div>
    </div>
    <div class="last-action ${last ? (last.ok ? 'last-action-ok' : 'last-action-fail') : ''}">
        <span class="last-action-label">最近操作</span>
        ${last ? `<strong>${esc(last.action)} · ${esc(last.result)}</strong><span>${esc(last.detail || '—')}</span><time>${ago(last.at / 1000)}前</time>` : '<strong>暂无</strong><span>这里会显示部署和启停结果</span>'}
    </div>
    <dl class="detail-list">${items.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`;
    D.sideContent.querySelector('[data-a="deploy"]')?.addEventListener('click', e => { e.stopPropagation(); handleDeploy(r.uid); });
    D.sideContent.querySelector('[data-a="enable"]')?.addEventListener('click', e => { e.stopPropagation(); handleEnable(r.uid); });
    D.sideContent.querySelector('[data-a="disable"]')?.addEventListener('click', e => { e.stopPropagation(); handleDisable(r.uid); });
    D.sideContent.querySelector('[data-a="delete"]')?.addEventListener('click', e => { e.stopPropagation(); handleDeleteAccount(r.uid); });
}

function setText(selector, value) {
    const e = document.querySelector(selector);
    if (e) e.textContent = value;
}

function attachCardEvents(root) {
    root.querySelectorAll('[data-uid]').forEach(el => {
        el.addEventListener('click', () => {
            setPage('detail');
            openSide(el.dataset.uid);
        });
    });
}

function recordAction(uid, action, result, ok, detail = '') {
    state.lastAction = {
        uid,
        action,
        result,
        ok,
        detail,
        at: Date.now(),
    };
}

function deployFailureDetail(r) {
    const label = displayResult(r?.error_type);
    if (r?.error_type === 'create_peak_rate_limited') {
        return '高峰限流，已完成 3 次创建重试，将在下个调度周期再试';
    }
    if (r?.error_type === 'create_rate_limited') {
        return '7001限流，账号进入 24h 创建冷却';
    }
    return `${label} · ${r?.error_detail || '无详情'}`;
}

async function handleDeploy(uid) {
    if (!await confirm('部署账号', `确定部署 ${uid}？执行后将进入 24h 冷却。`)) return;
    setPending(uid, 'deploy', true);
    try {
        const r = await json(`/api/deploy/${uid}`, { method: 'POST' });
        const detail = r.success
            ? `${(r.elapsed_sec ?? 0).toFixed(1)}s · ${r.connector_id || '无连接器'}`
            : deployFailureDetail(r);
        recordAction(uid, '部署', r.success ? '成功' : '失败', Boolean(r.success), detail);
        if (r.success) {
            toast(`部署成功 · ${uid} (${(r.elapsed_sec ?? 0).toFixed(1)}s)`, 'success');
        } else {
            toast(`部署失败 · ${uid}: ${deployFailureDetail(r)}`, 'error');
            if (r.needs_relogin) toast(`凭据失效 · ${uid}`, 'error');
        }
    } catch (e) {
        recordAction(uid, '部署', '请求失败', false, e.message);
        toast(`请求失败: ${e.message}`, 'error');
    } finally {
        setPending(uid, 'deploy', false);
    }
    await refresh();
}

async function handleEnable(uid) {
    setPending(uid, 'enable', true);
    try {
        await json(`/api/account/${uid}/enable`, { method: 'POST' });
        recordAction(uid, '启用', '成功', true, '账号已回到可调度池');
        toast(`已启用 · ${uid}`, 'success');
    } catch (e) {
        recordAction(uid, '启用', '失败', false, e.message);
        toast(`启用失败: ${e.message}`, 'error');
    } finally {
        setPending(uid, 'enable', false);
    }
    await refresh();
}

async function handleDisable(uid) {
    if (!await confirm('禁用账号', `确定禁用 ${uid}？禁用后调度器将跳过该账号。`)) return;
    setPending(uid, 'disable', true);
    try {
        await json(`/api/account/${uid}/disable`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: '前端禁用' }),
        });
        recordAction(uid, '禁用', '成功', true, '调度器将跳过该账号');
        toast(`已禁用 · ${uid}`, 'info');
    } catch (e) {
        recordAction(uid, '禁用', '失败', false, e.message);
        toast(`禁用失败: ${e.message}`, 'error');
    } finally {
        setPending(uid, 'disable', false);
    }
    await refresh();
}

async function handleDeleteAccount(uid) {
    if (!await confirm('删除失效账号', `确定删除 ${uid}？只会删除本地凭据和状态文件，运行中账号会被后端拒绝。`)) return;
    setPending(uid, 'delete', true);
    try {
        await json(`/api/account/${uid}`, { method: 'DELETE' });
        recordAction(uid, '删除账号', '成功', true, '已删除 token 失效账号');
        toast(`已删除失效账号 · ${uid}`, 'success');
        if (String(state.selectedUid) === String(uid)) closeSide();
    } catch (e) {
        recordAction(uid, '删除账号', '失败', false, e.message);
        toast(`删除失败: ${e.message}`, 'error');
    } finally {
        setPending(uid, 'delete', false);
    }
    await refresh();
}

function ensureImportModal() {
    let overlay = $('#account-import-overlay');
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'account-import-overlay';
    overlay.className = 'account-import-overlay hidden';
    overlay.innerHTML = `<form id="account-import-dialog" class="account-import-dialog">
        <div class="account-import-head">
            <div>
                <h3>批量导入账号</h3>
                <p>粘贴 JSON 凭据、JSONL，或原始 cookie 文本。</p>
            </div>
            <button class="btn btn-ghost btn-icon" type="button" data-a="close-import" aria-label="关闭导入">
                ${Icon.x}
            </button>
        </div>
        <textarea id="account-import-text" name="text" spellcheck="false" placeholder="示例：cUserId=...; serviceToken=...; xiaomichatbot_ph=..."></textarea>
        <div class="account-import-actions">
            <span>不会在结果中回显 serviceToken 或 cookie。</span>
            <div>
                <button class="action-btn action-btn-ghost" type="button" data-a="close-import">取消</button>
                <button id="account-import-submit" class="action-btn action-btn-primary" type="submit">导入账号</button>
            </div>
        </div>
        <div id="account-import-result" class="account-import-result" aria-live="polite"></div>
    </form>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => {
        if (e.target === overlay || e.target.closest('[data-a="close-import"]')) closeImportModal();
    });
    overlay.querySelector('#account-import-dialog').addEventListener('submit', handleImportSubmit);
    return overlay;
}

function openImportModal() {
    const overlay = ensureImportModal();
    overlay.classList.remove('hidden');
    overlay.querySelector('#account-import-result').innerHTML = '';
    setTimeout(() => overlay.querySelector('#account-import-text')?.focus(), 40);
}

function closeImportModal() {
    $('#account-import-overlay')?.classList.add('hidden');
}

function renderImportResults(response) {
    const rows = response.results || [];
    const summary = `<div class="import-summary">
        <span class="import-ok">导入 ${esc(response.imported ?? 0)}</span>
        <span>跳过 ${esc(response.skipped ?? 0)}</span>
        <span class="import-fail">失败 ${esc(response.failed ?? 0)}</span>
    </div>`;
    if (!rows.length) return `${summary}<div class="ph">没有返回导入明细</div>`;
    const list = rows.map(item => `<div class="import-row import-row-${esc(item.status || 'failed')}">
        <span>第 ${esc(item.row || '-')} 行</span>
        <strong>${esc(item.uid || '未识别账号')}</strong>
        <em>${esc(item.message || item.status || '—')}</em>
    </div>`).join('');
    return `${summary}<div class="import-list">${list}</div>`;
}

async function handleImportSubmit(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const textarea = form.querySelector('#account-import-text');
    const resultBox = form.querySelector('#account-import-result');
    const submit = form.querySelector('#account-import-submit');
    const text = textarea.value.trim();
    if (!text) {
        resultBox.innerHTML = '<div class="import-row import-row-failed"><strong>请输入账号 JSON 或原始 cookie 文本</strong></div>';
        return;
    }
    submit.disabled = true;
    submit.innerHTML = `${Icon.spinner} 导入中...`;
    resultBox.innerHTML = '<div class="ph">正在导入...</div>';
    try {
        const response = await json('/api/accounts/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ format: 'auto', text }),
        });
        resultBox.innerHTML = renderImportResults(response);
        recordAction('accounts', '批量导入', response.success ? '成功' : '有失败', response.success, `导入 ${response.imported || 0} 个，失败 ${response.failed || 0} 个`);
        toast(`导入完成：成功 ${response.imported || 0}，失败 ${response.failed || 0}`, response.failed ? 'info' : 'success');
        await refresh();
    } catch (err) {
        resultBox.innerHTML = `<div class="import-row import-row-failed"><strong>导入失败</strong><em>${esc(err.message)}</em></div>`;
        toast(`导入失败: ${err.message}`, 'error');
    } finally {
        submit.disabled = false;
        submit.textContent = '导入账号';
    }
}

async function handleReloadCfg() {
    setConfigPending(true);
    try {
        state.config = await json('/api/config/reload', { method: 'POST' }).then(() => json('/api/config'));
        state.historyLimit = null;
        syncHistoryLimitFromConfig(state.config);
        state.configLoadedAt = Date.now();
        recordAction('config', '重载配置', '成功', true, '配置已重新读取');
        toast('配置已重载', 'success');
        await refresh();
    } catch (e) {
        recordAction('config', '重载配置', '失败', false, e.message);
        toast(`失败: ${e.message}`, 'error');
    } finally {
        setConfigPending(false);
    }
}

async function handleSaveProjectConfig(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const fd = new FormData(form);
    const project = {
        min_accounts: fd.get('min_accounts'),
        public_hostname: fd.get('public_hostname'),
        local_port: fd.get('local_port'),
        history_limit: fd.get('history_limit'),
    };
    for (const key of ['WEBUI_PASSWORD', 'TUNNEL_TOKEN', 'CF_API_TOKEN', 'CF_ACCOUNT_ID', 'PROXY_API_KEY']) {
        const value = String(fd.get(key) || '').trim();
        if (value) project[key] = value;
    }
    if (fd.get('clear_cf_api_token')) project.clear_cf_api_token = true;
    if (fd.get('clear_cf_account_id')) project.clear_cf_account_id = true;

    setConfigPending(true);
    try {
        await json('/api/config/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project }),
        });
        recordAction('config', '保存项目参数', '成功', true, '配置已写入 config.json / .env');
        toast('项目参数已保存', 'success');
        state.configFormDirty = false;
        state.historyLimit = clampHistoryLimit(project.history_limit);
        await refresh({ preserveConfigForm: false });
    } catch (err) {
        recordAction('config', '保存项目参数', '失败', false, err.message);
        toast(`保存失败: ${err.message}`, 'error');
    } finally {
        setConfigPending(false);
    }
}

async function handleSavePromptTemplate(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const fd = new FormData(form);
    const promptId = String(form.dataset.promptId || '').trim();
    const text = String(fd.get('text') || '');
    if (!promptId) {
        toast('当前模板 ID 为空', 'error');
        return;
    }
    if (!text.trim()) {
        toast('提示词正文不能为空', 'error');
        return;
    }

    setConfigPending(true);
    try {
        const response = await json(`/api/prompt-templates/${encodeURIComponent(promptId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        if (state.promptTemplates?.templates && response.template) {
            state.promptTemplates = {
                ...state.promptTemplates,
                templates: state.promptTemplates.templates.map(t => t.prompt_id === promptId ? response.template : t),
            };
            state.promptTemplatesLoadedAt = Date.now();
        }
        recordAction('config', '保存提示词模板', '成功', true, `模板 ${promptId}`);
        toast('提示词模板已保存', 'success');
        state.configFormDirty = false;
        renderConfigPage();
    } catch (err) {
        recordAction('config', '保存提示词模板', '失败', false, err.message);
        toast(`保存失败: ${err.message}`, 'error');
    } finally {
        setConfigPending(false);
    }
}

async function runSchedulerAction(action, title, message, url, body = { confirm: true }) {
    if (!await confirm(title, message)) return;
    setSchedulerPending(action, true);
    try {
        const result = await json(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        state.scheduler.lastOperation = {
            ...result,
            title,
            success: result.success !== false,
            detail: result.message,
        };
        toast(result.message || `${title} 已执行`, result.success === false ? 'error' : 'success');
    } catch (err) {
        state.scheduler.lastOperation = {
            title,
            success: false,
            error: err.message,
            started_at: Date.now() / 1000,
        };
        toast(`${title} 失败: ${err.message}`, 'error');
    } finally {
        setSchedulerPending(action, false);
    }
    await refresh({ preserveConfigForm: true, preserveConfigPage: true });
}

async function handleSchedulerStart() {
    await runSchedulerAction(
        'start',
        '启动调度循环',
        '启动后后端会持续执行多账号调度，请确认当前只启用一个 WebUI 服务用于测试。',
        '/api/scheduler/start',
    );
}

async function handleSchedulerStop() {
    await runSchedulerAction(
        'stop',
        '停止调度循环',
        '停止 WebUI 管理的调度循环，正在执行中的 tick 会尽量优雅结束。',
        '/api/scheduler/stop',
    );
}

async function handleSchedulerTick() {
    await runSchedulerAction(
        'tick',
        '执行一次调度',
        '这会运行一次多账号调度 tick，可能触发真实部署。',
        '/api/scheduler/tick',
        { confirm: true, dry_run: false },
    );
}

async function handleDeployDue() {
    await runSchedulerAction(
        'deploy-due',
        '执行待部署队列',
        '这会按后端重新计算的 due_deploys 执行当前待部署账号。',
        '/api/scheduler/deploy-due',
    );
}

let timer = null, auto = true;

async function refresh(opts = {}) {
    try {
        const [s, p, c, h, schedulerStatus] = await Promise.all([
            json('/api/status'),
            json('/api/plan'),
            json('/api/config'),
            json(historyRequestUrl()),
            json('/api/scheduler/status'),
        ]);
        renderAll(s, p, c, h, schedulerStatus, opts);
        D.ts.textContent = ago(s.timestamp);
        D.ts.title = new Date(s.timestamp * 1000).toLocaleString();
    } catch (e) {
        console.error(e);
        D.ts.textContent = '失败';
        toast(`数据加载失败: ${e.message}`, 'error', 5000);
    }
}

function start() { stop(); timer = setInterval(() => { if (auto) refresh({ preserveConfigForm: true, preserveConfigPage: true }); }, POLL); }
function stop() { if (timer) { clearInterval(timer); timer = null; } }
function toggleAuto() {
    auto = !auto;
    D.btnAuto.dataset.active = auto ? 'true' : 'false';
    D.btnAuto.textContent = auto ? '自动 5s' : '自动 关';
    if (auto) start(); else stop();
}

function bindToolbar() {
    D.btnImportAccounts?.addEventListener('click', openImportModal);
    D.search.addEventListener('input', e => {
        state.query = e.target.value;
        renderTable();
    });
    D.sort.addEventListener('change', e => {
        state.sort = e.target.value;
        renderTable();
    });
    $$('.filter-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            state.filter = btn.dataset.filter || 'all';
            $$('.filter-tab').forEach(b => b.classList.toggle('active', b === btn));
            renderTable();
        });
    });
    D.historyFilters.forEach(btn => {
        btn.addEventListener('click', () => {
            state.historyFilter = btn.dataset.historyFilter || 'all';
            D.historyFilters.forEach(b => b.classList.toggle('active', b === btn));
            renderHistory();
        });
    });
    D.historyLimitForm?.addEventListener('submit', handleHistoryLimitApply);
    D.historyLimitSave?.addEventListener('click', handleHistoryLimitSave);
}

function setPage(page) {
    const pages = ['home', 'detail', 'scheduler', 'history', 'config'];
    if (!pages.includes(page)) page = 'home';
    state.page = page;
    if (location.hash !== `#${page}`) history.replaceState(null, '', `#${page}`);
    D.pageHome.classList.toggle('hidden', page !== 'home');
    D.pageDetail.classList.toggle('hidden', page !== 'detail');
    D.pageScheduler?.classList.toggle('hidden', page !== 'scheduler');
    D.pageHistory.classList.toggle('hidden', page !== 'history');
    D.pageConfig.classList.toggle('hidden', page !== 'config');
    D.navTabs.forEach(tab => {
        const active = tab.dataset.page === page;
        tab.classList.toggle('active', active);
        if (active) tab.setAttribute('aria-current', 'page');
        else tab.removeAttribute('aria-current');
    });
    if (page !== 'detail') closeSide();
}

function bindNavigation() {
    D.navTabs.forEach(tab => {
        tab.addEventListener('click', () => setPage(tab.dataset.page || 'home'));
    });
    window.addEventListener('hashchange', () => {
        const page = location.hash ? location.hash.slice(1) : 'home';
        setPage(page);
    });
    D.btnOpenDetail.addEventListener('click', () => setPage('detail'));
    D.historyRefresh?.addEventListener('click', refresh);
    D.historyWorkbench?.addEventListener('click', () => setPage('detail'));
    D.configPageReload?.addEventListener('click', handleReloadCfg);
}

document.addEventListener('DOMContentLoaded', async () => {
    bindNavigation();
    bindToolbar();
    const initialPage = ['#detail', '#scheduler', '#history', '#config'].includes(location.hash) ? location.hash.slice(1) : 'home';
    setPage(initialPage);
    try {
        if (await ensureAuth()) await refresh().then(start);
    } catch (err) {
        console.error(err);
        showAuthOverlay('认证状态检查失败');
    }
    D.btnR.addEventListener('click', refresh);
    D.btnAuto.addEventListener('click', toggleAuto);
    D.btnCfg.addEventListener('click', handleReloadCfg);
    D.btnLogout?.addEventListener('click', handleLogout);
    D.sideClose.addEventListener('click', closeSide);
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            closeImportModal();
            closeSide();
        }
        if (e.key === 'r' && !e.ctrlKey && !e.metaKey && !e.target.matches('input,textarea,button,select')) refresh();
    });
});
