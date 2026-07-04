/**
 * mimi3 Operator Dashboard — app.js
 */

// ── Constants ─────────────────────────────────────────────

const STATE_LABELS = {
    active:         { label: '活跃',    cls: 'badge-active' },
    idle:           { label: '空闲',    cls: 'badge-idle' },
    needs_deploy:   { label: '待部署',  cls: 'badge-needs_deploy' },
    deploying:      { label: '部署中',  cls: 'badge-deploying' },
    cooldown:       { label: '冷却',    cls: 'badge-cooldown' },
    relogin_needed: { label: '需补号',  cls: 'badge-relogin_needed' },
    disabled:       { label: '已禁用',  cls: 'badge-disabled' },
};
const POLL_INTERVAL = 5000;  // ms
const STATE_KEYS = Object.keys(STATE_LABELS);

// ── DOM refs ──────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
    overviewCards: $('#overview-cards'),
    schedulerContent: $('#scheduler-content'),
    tableBody: $('#table-body'),
    tableEmpty: $('#table-empty'),
    accountCount: $('#account-count'),
    refreshIndicator: $('#refresh-indicator'),
    btnRefresh: $('#btn-refresh'),
    btnAutoRefresh: $('#btn-auto-refresh'),
    btnReloadConfig: $('#btn-reload-config'),
    toastContainer: $('#toast-container'),
};

// ── Toast system ──────────────────────────────────────────

function toast(msg, type = 'info', duration = 4000) {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    dom.toastContainer.appendChild(el);
    setTimeout(() => {
        el.classList.add('removing');
        setTimeout(() => el.remove(), 200);
    }, duration);
}

// ── Confirm dialog ────────────────────────────────────────

function confirmDialog(title, msg) {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.id = 'confirm-overlay';
        overlay.innerHTML = `
            <div id="confirm-box">
                <h3>${escapeHtml(title)}</h3>
                <p>${escapeHtml(msg)}</p>
                <div class="actions">
                    <button class="action-btn action-btn-ghost" data-action="cancel">取消</button>
                    <button class="action-btn action-btn-danger" data-action="confirm">确认</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        overlay.addEventListener('click', (e) => {
            const action = e.target.dataset.action;
            if (action === 'confirm') { overlay.remove(); resolve(true); }
            if (action === 'cancel')  { overlay.remove(); resolve(false); }
        });
    });
}

// ── Helpers ───────────────────────────────────────────────

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatDuration(sec) {
    if (sec == null || sec <= 0) return '—';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (h > 0) return `${h}h${m}m`;
    return `${m}m`;
}

function formatStagger(sec) {
    if (sec == null) return '—';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (h > 0) return `${h}h${m}m`;
    return `${m}m`;
}

function timeAgo(ts) {
    if (!ts) return '—';
    const sec = Math.floor((Date.now() / 1000) - ts);
    if (sec < 5) return '刚刚';
    if (sec < 60) return `${sec}秒前`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}分钟前`;
    return `${Math.floor(m / 60)}小时前`;
}

// ── SVG icons (inline, since no icon library on CDN) ──────

function svgDeploy() {
    return '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>';
}
function svgSpinner() {
    return '<span class="spinner"></span>';
}

// ── Fetch API ─────────────────────────────────────────────

async function fetchJSON(url, options = {}) {
    const resp = await fetch(url, {
        headers: { 'Accept': 'application/json', ...options.headers },
        ...options,
    });
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`${resp.status} ${resp.statusText}${text ? ': ' + text.slice(0, 100) : ''}`);
    }
    if (resp.headers.get('content-type')?.includes('application/json')) {
        return resp.json();
    }
    return {};
}

// ── Render: Overview Cards ────────────────────────────────

function renderCards(statusData) {
    const byState = statusData.by_state || {};
    const snap = statusData.snapshot || [];

    const active = byState.active || 0;
    const cooldown = (byState.cooldown || 0);
    const attention = (byState.relogin_needed || 0) + (byState.disabled || 0);
    const reserve = snap.filter(r => r.eligible === true).length;

    setStatValue('active', active);
    setStatValue('cooldown', cooldown);
    setStatValue('attention', attention);
    setStatValue('reserve', reserve);
}

function setStatValue(key, val) {
    const el = document.querySelector(`[data-stat="${key}"]`);
    if (el) el.textContent = val;
}

// ── Render: Scheduler Panel ───────────────────────────────

function renderScheduler(planData) {
    const plan = planData || {};
    const parts = [];

    parts.push(`<span class="text-[#94A3B8]">错峰间隔</span> <span class="font-mono text-[#22C55E]">${formatStagger(plan.stagger_interval)}</span>`);
    parts.push(`<span class="text-[#64748B]">·</span>`);
    parts.push(`<span class="text-[#94A3B8]">活跃</span> <span class="font-mono text-[#22C55E]">${plan.active_count ?? '?'}</span>`);
    parts.push(`<span class="text-[#64748B]">·</span>`);
    parts.push(`<span class="text-[#94A3B8]">储备</span> <span class="font-mono text-[#EAB308]">${plan.reserve_size ?? '?'}</span>`);
    parts.push(`<span class="text-[#64748B]">·</span>`);
    parts.push(`<span class="text-[#94A3B8]">可部署</span> <span class="font-mono">${plan.eligible_count ?? '?'}</span>`);

    // Warning badges
    if (plan.coverage_gap) {
        parts.push(`<span class="ml-2 px-2 py-0.5 rounded-full text-xs bg-[#EF4444]/15 text-[#EF4444] border border-[#EF4444]/30">覆盖缺口</span>`);
    }
    if (plan.coverage_risk) {
        parts.push(`<span class="ml-2 px-2 py-0.5 rounded-full text-xs bg-[#EAB308]/15 text-[#EAB308] border border-[#EAB308]/30">覆盖风险</span>`);
    }

    let html = `<div class="flex flex-wrap items-center gap-1">${parts.join(' ')}</div>`;

    // Due deploys
    const deploys = plan.due_deploys || [];
    if (deploys.length > 0) {
        html += '<div class="mt-3 pt-3 border-t border-[#1E293B]">';
        html += '<span class="text-xs text-[#64748B] uppercase tracking-wider">待执行部署</span>';
        html += '<div class="mt-1 space-y-1">';
        for (const d of deploys) {
            const uid = d.uid || '(无 reserve)';
            const reason = d.reason || '?';
            const handoff = d.handoff_from ? ` ← ${d.handoff_from}` : '';
            html += `<div class="flex items-center gap-2 text-xs"><span class="font-mono text-[#CBD5E1]">${escapeHtml(uid)}</span><span class="text-[#64748B]">${escapeHtml(reason)}${handoff}</span></div>`;
        }
        html += '</div></div>';
    }

    dom.schedulerContent.innerHTML = html;
}

// ── Render: Account Table ─────────────────────────────────

function renderTable(statusData) {
    const snap = statusData.snapshot || [];

    if (snap.length === 0) {
        dom.tableBody.innerHTML = '';
        dom.tableEmpty.classList.remove('hidden');
        dom.accountCount.textContent = '0 个账号';
        return;
    }
    dom.tableEmpty.classList.add('hidden');
    dom.accountCount.textContent = `${snap.length} 个账号`;

    const rows = snap.map(r => renderRow(r)).join('');
    dom.tableBody.innerHTML = rows;

    // Bind action buttons (use event delegation for table, but individual buttons need binds)
    dom.tableBody.querySelectorAll('[data-action="deploy"]').forEach(btn => {
        btn.addEventListener('click', () => handleDeploy(btn.dataset.uid));
    });
    dom.tableBody.querySelectorAll('[data-action="toggle-disable"]').forEach(btn => {
        btn.addEventListener('click', () => handleToggleDisable(btn.dataset.uid, btn.dataset.state));
    });
    dom.tableBody.querySelectorAll('[data-action="reload-creds"]').forEach(btn => {
        btn.addEventListener('click', () => handleReloadCreds(btn.dataset.uid));
    });
}

function renderRow(r) {
    const st = STATE_LABELS[r.deploy_state] || { label: r.deploy_state, cls: 'badge-idle' };
    const healthDot = renderHealthDot(r);
    const cid = r.connector_id ? r.connector_id.slice(0, 8) + '…' : '—';
    const remain = formatDuration(r.remain_sec);
    const failures = r.consecutive_failures ?? 0;

    // Actions: deploy (only when eligible/not deploying), disable/enable toggle, reload creds
    const canDeploy = r.eligible && r.deploy_state !== 'deploying';
    const isDisabled = r.deploy_state === 'disabled';
    const deployBtn = canDeploy
        ? `<button class="action-btn action-btn-primary" data-action="deploy" data-uid="${r.uid}">${svgDeploy()} 部署</button>`
        : `<button class="action-btn action-btn-ghost" disabled>${svgDeploy()}</button>`;

    const toggleBtn = isDisabled
        ? `<button class="action-btn action-btn-primary" data-action="toggle-disable" data-uid="${r.uid}" data-state="disabled">启用</button>`
        : `<button class="action-btn action-btn-danger" data-action="toggle-disable" data-uid="${r.uid}" data-state="${r.deploy_state}">禁用</button>`;

    const reloadBtn = `<button class="action-btn action-btn-ghost" data-action="reload-creds" data-uid="${r.uid}" title="重载凭据">补号</button>`;

    return `<tr>
        <td class="px-4 py-3 font-mono text-xs text-[#CBD5E1]">${escapeHtml(r.uid)}</td>
        <td class="px-4 py-3"><span class="badge ${st.cls}">${st.label}</span></td>
        <td class="px-4 py-3 text-right font-mono text-xs text-[#94A3B8]">${remain}</td>
        <td class="px-4 py-3 font-mono text-xs text-[#64748B] hidden md:table-cell">${cid}</td>
        <td class="px-4 py-3 text-center text-xs font-mono hidden lg:table-cell ${failures > 2 ? 'text-[#EF4444]' : 'text-[#64748B]'}">${failures}</td>
        <td class="px-4 py-3 text-center">${healthDot}</td>
        <td class="px-4 py-3 text-right"><div class="flex items-center justify-end gap-1.5">${deployBtn}${toggleBtn}${reloadBtn}</div></td>
    </tr>`;
}

function renderHealthDot(r) {
    // Simplified health: if active and has connector_id → green
    // If active but no connector_id → yellow (degraded)
    // If relogin_needed or failed → red
    // else → gray
    if (r.deploy_state === 'active' && r.connector_id) {
        return `<span class="health-dot health-dot-ok" title="活跃 (${r.connector_id.slice(0, 8)}…)"></span>`;
    }
    if (r.deploy_state === 'active' && !r.connector_id) {
        return `<span class="health-dot health-dot-degraded" title="活跃但无 connector_id"></span>`;
    }
    if (r.deploy_state === 'relogin_needed' || (r.last_result === 'fail' && r.consecutive_failures > 2)) {
        return `<span class="health-dot health-dot-fail" title="失败"></span>`;
    }
    return `<span class="health-dot health-dot-unknown" title="${r.deploy_state}"></span>`;
}

// ── Action Handlers ───────────────────────────────────────

async function handleDeploy(uid) {
    const confirmed = await confirmDialog('部署账号', `确定部署账号 ${uid}？执行部署后此账号将进入 24h 冷却。`);
    if (!confirmed) return;

    // Find the button and show loading
    const btns = dom.tableBody.querySelectorAll(`[data-action="deploy"][data-uid="${uid}"]`);
    btns.forEach(b => { b.disabled = true; b.innerHTML = svgSpinner() + ' 部署中…'; });

    try {
        const result = await fetchJSON(`/api/deploy/${uid}`, { method: 'POST' });
        if (result.success) {
            toast(`✅ ${uid} 部署成功 (${result.elapsed_sec?.toFixed(1) ?? '?'}s)`, 'success');
        } else {
            toast(`❌ ${uid} 部署失败: ${result.error_type || '未知'}`, 'error');
            if (result.needs_relogin) {
                toast(`🔴 ${uid} 需要补号 (cookie 过期)`, 'error');
            }
        }
    } catch (err) {
        toast(`部署请求失败: ${err.message}`, 'error');
    }

    // Refresh data after deploy
    await refreshAll();
}

async function handleToggleDisable(uid, currentState) {
    const isDisabled = currentState === 'disabled';
    if (isDisabled) {
        // Enable
        try {
            await fetchJSON(`/api/account/${uid}/enable`, { method: 'POST' });
            toast(`✅ ${uid} 已启用`, 'success');
        } catch (err) {
            toast(`启用失败: ${err.message}`, 'error');
        }
    } else {
        // Disable
        const confirmed = await confirmDialog('禁用账号', `确定禁用账号 ${uid}？禁用后不会被调度部署。`);
        if (!confirmed) return;
        try {
            await fetchJSON(`/api/account/${uid}/disable`, { method: 'POST' });
            toast(`⛔ ${uid} 已禁用`, 'info');
        } catch (err) {
            toast(`禁用失败: ${err.message}`, 'error');
        }
    }
    await refreshAll();
}

async function handleReloadCreds(uid) {
    try {
        const result = await fetchJSON(`/api/account/${uid}/reload-creds`, { method: 'POST' });
        if (result.found) {
            toast(`📄 ${uid} 凭据已重载`, 'success');
        } else {
            toast(`⚠️ ${uid} 凭据文件未找到`, 'error');
        }
    } catch (err) {
        toast(`重载失败: ${err.message}`, 'error');
    }
    await refreshAll();
}

async function handleReloadConfig() {
    try {
        await fetchJSON('/api/config/reload', { method: 'POST' });
        toast(`⚙️ 配置已重载`, 'success');
    } catch (err) {
        toast(`配置重载失败: ${err.message}`, 'error');
    }
}

// ── Refresh Loop ──────────────────────────────────────────

let lastRefresh = null;
let pollTimer = null;
let autoRefreshActive = true;

async function refreshAll() {
    try {
        const [statusData, planData] = await Promise.all([
            fetchJSON('/api/status'),
            fetchJSON('/api/plan'),
        ]);

        renderCards(statusData);
        renderScheduler(planData);
        renderTable(statusData);

        lastRefresh = Date.now();
        dom.refreshIndicator.textContent = timeAgo(statusData.timestamp);
        dom.refreshIndicator.title = new Date(statusData.timestamp * 1000).toLocaleString();
    } catch (err) {
        console.error('refresh failed:', err);
        dom.refreshIndicator.textContent = '❌ 连接失败';
        toast(`数据加载失败: ${err.message}`, 'error', 5000);
    }
}

function startPoll() {
    stopPoll();
    pollTimer = setInterval(() => {
        if (autoRefreshActive) refreshAll();
    }, POLL_INTERVAL);
}

function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function toggleAutoRefresh() {
    autoRefreshActive = !autoRefreshActive;
    const btn = dom.btnAutoRefresh;
    if (autoRefreshActive) {
        btn.dataset.active = 'true';
        btn.className = 'px-3 py-1.5 text-xs rounded bg-[#22C55E]/10 text-[#22C55E] border border-[#22C55E]/30 transition-colors duration-150';
        btn.textContent = '自动刷新 5s';
        startPoll();
    } else {
        btn.dataset.active = 'false';
        btn.className = 'px-3 py-1.5 text-xs rounded bg-[#1E293B] hover:bg-[#334155] border border-[#334155] transition-colors duration-150 text-[#64748B]';
        btn.textContent = '自动刷新 关';
        stopPoll();
    }
}

// ── Init ──────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Initial load
    refreshAll().then(startPoll);

    // Event binds
    dom.btnRefresh.addEventListener('click', () => refreshAll());
    dom.btnAutoRefresh.addEventListener('click', toggleAutoRefresh);
    dom.btnReloadConfig.addEventListener('click', handleReloadConfig);

    // Keyboard shortcut: 'r' to refresh
    document.addEventListener('keydown', (e) => {
        if (e.key === 'r' && !e.ctrlKey && !e.metaKey && !e.target.matches('input,textarea,button')) {
            refreshAll();
        }
    });
});