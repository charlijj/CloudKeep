// app.js — CloudKeep gateway client (login + fleet dashboard + builder + viewer).
//
// One page, three views toggled in place. The JWT lives only in module memory
// (no localStorage/sessionStorage/URL). Talks to controld via the /ck/ proxy.

let _jwt = null;        // set on login, memory only
let _rfb = null;        // active RFB connection
let _pollTimer = null;  // dashboard refresh while a VM is provisioning

const views = {
    login: document.getElementById('view-login'),
    dashboard: document.getElementById('view-dashboard'),
    viewer: document.getElementById('view-viewer'),
};

function showView(name) {
    for (const [key, el] of Object.entries(views)) el.hidden = key !== name;
    document.body.dataset.view = name;
    document.getElementById('logout').hidden = (name === 'login');
    if (name === 'dashboard') import('/lib/novnc.bundle.js?v=1.5.0').catch(() => {});
}

function _error(elId, msg) {
    console.error('[cloudkeep]', elId, msg);
    const el = document.getElementById(elId);
    if (el) { el.textContent = msg; el.hidden = false; }
}

// Authenticated fetch against the control API. Returns the Response.
async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers, { 'Authorization': `Bearer ${_jwt}` });
    return fetch(`/ck${path}`, Object.assign({}, opts, { headers }));
}

// ---------- login ----------
const form = document.getElementById('login-form');
const submitBtn = document.getElementById('submit');

form.addEventListener('submit', async (event) => {
    event.preventDefault();
    document.getElementById('login-error').hidden = true;
    submitBtn.disabled = true;
    try {
        const res = await fetch('/ck/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: form.username.value, password: form.password.value }),
        });
        if (!res.ok) throw new Error('login failed');
        _jwt = (await res.json()).access_token;
        form.reset();
        showView('dashboard');
        loadDashboard();
    } catch {
        _error('login-error', 'Invalid credentials');
    } finally {
        submitBtn.disabled = false;
    }
});

document.getElementById('logout').addEventListener('click', () => {
    _jwt = null;
    stopPolling();
    showView('login');
});

// ---------- dashboard ----------
const vmList = document.getElementById('vm-list');
const vmEmpty = document.getElementById('vm-empty');

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function loadDashboard() {
    try {
        const res = await api('/vms');
        if (res.status === 401) return sessionExpired();
        const { vms } = await res.json();
        renderVMs(vms);
        // Poll while anything is mid-provision so cards flip to "ready" live.
        const busy = vms.some(v => v.state === 'PROVISIONING' || v.state === 'REQUESTED');
        if (busy && !_pollTimer) _pollTimer = setInterval(loadDashboard, 3000);
        if (!busy) stopPolling();
    } catch {
        _error('dashboard-error', 'Could not load machines.');
    }
}

const STATE_BADGE = {
    RUNNING:      ['ready', 'badge--active'],
    PROVISIONING: ['building', 'badge--pending'],
    REQUESTED:    ['queued', 'badge--pending'],
    STOPPED:      ['stopped', 'badge--down'],
    ERROR:        ['error', 'badge--down'],
    DELETING:     ['deleting', 'badge--down'],
};

function renderVMs(vms) {
    vmList.innerHTML = '';
    vmEmpty.hidden = vms.length > 0;
    for (const vm of vms) {
        const [label, cls] = STATE_BADGE[vm.state] || [vm.state.toLowerCase(), 'badge--down'];
        const card = document.createElement('article');
        card.className = 'card';
        card.innerHTML = `
            <header class="card-header">
                <h3 class="card-title">${escapeHtml(vm.label)}</h3>
                <span class="badge ${cls}">${label}</span>
            </header>
            <p class="card-body">${vm.vcpus} vCPU &middot; ${Math.round(vm.mem_mb / 1024)} GB RAM &middot; ${vm.disk_gb} GB disk</p>
            ${vm.state === 'ERROR' && vm.error_msg ? `<p class="card-err">${escapeHtml(vm.error_msg)}</p>` : ''}
            <div class="card-actions"></div>`;
        const actions = card.querySelector('.card-actions');
        if (vm.state === 'RUNNING') {
            actions.append(action('Connect', 'btn--card', () => connect(vm.id)));
            actions.append(action('Stop', 'btn--ghost', () => lifecycle(vm.id, 'stop')));
        } else if (vm.state === 'STOPPED') {
            actions.append(action('Start', 'btn--card', () => lifecycle(vm.id, 'start')));
        }
        if (vm.state !== 'PROVISIONING' && vm.state !== 'REQUESTED' && vm.state !== 'DELETING') {
            actions.append(action('Delete', 'btn--ghost btn--danger', () => del(vm.id)));
        }
        vmList.append(card);
    }
}

function action(text, cls, fn) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `btn ${cls}`;
    b.textContent = text;
    b.addEventListener('click', async () => { b.disabled = true; await fn(); });
    return b;
}

async function lifecycle(id, verb) {
    document.getElementById('dashboard-error').hidden = true;
    const res = await api(`/vms/${id}/${verb}`, { method: 'POST' });
    if (res.status === 401) return sessionExpired();
    if (!res.ok) _error('dashboard-error', (await res.json().catch(() => ({}))).detail || `${verb} failed`);
    loadDashboard();
}

async function del(id) {
    if (!confirm('Delete this machine? This cannot be undone.')) { loadDashboard(); return; }
    const res = await api(`/vms/${id}`, { method: 'DELETE' });
    if (res.status === 401) return sessionExpired();
    loadDashboard();
}

function sessionExpired() {
    _jwt = null; stopPolling();
    _error('login-error', 'Session expired — please sign in again.');
    showView('login');
}

// ---------- builder ----------
const builder = document.getElementById('builder');
const builderForm = document.getElementById('builder-form');
const sliders = {
    vcpus: { in: document.getElementById('in-vcpus'), out: document.getElementById('out-vcpus'), scale: 1 },
    mem:   { in: document.getElementById('in-mem'),   out: document.getElementById('out-mem'),   scale: 1024 },
    disk:  { in: document.getElementById('in-disk'),  out: document.getElementById('out-disk'),  scale: 1 },
};

document.getElementById('build-toggle').addEventListener('click', async () => {
    builder.hidden = !builder.hidden;
    if (!builder.hidden) await configureBuilder();
});

for (const s of Object.values(sliders)) {
    s.in.addEventListener('input', () => { s.out.textContent = Math.round(s.in.value / s.scale); });
}

async function configureBuilder() {
    const res = await api('/resources');
    if (res.status === 401) return sessionExpired();
    const r = await res.json();
    const host = r.host, you = r.you, b = r.bounds;
    const cap = (boundMax, hostFree, quotaLeft) => Math.max(0, Math.min(boundMax, hostFree, quotaLeft));

    setSlider(sliders.vcpus, b.vcpus[0], cap(b.vcpus[1], host.vcpus.free, you.vcpus.quota - you.vcpus.used), 1);
    setSlider(sliders.mem, b.mem_mb[0], cap(b.mem_mb[1], host.mem_mb.free, you.mem_mb.quota - you.mem_mb.used), b.mem_mb[2]);
    setSlider(sliders.disk, b.disk_gb[0], cap(b.disk_gb[1], host.disk_gb.free, you.disk_gb.quota - you.disk_gb.used), 1);

    const vmsLeft = you.max_vms - you.used_vms;
    document.getElementById('builder-avail').textContent =
        `Host free: ${host.vcpus.free} vCPU · ${Math.round(host.mem_mb.free / 1024)} GB RAM · ${host.disk_gb.free} GB disk` +
        `  —  Your quota left: ${vmsLeft} VMs, ${you.vcpus.quota - you.vcpus.used} vCPU, ` +
        `${Math.round((you.mem_mb.quota - you.mem_mb.used) / 1024)} GB RAM, ${you.disk_gb.quota - you.disk_gb.used} GB disk`;

    const fits = sliders.vcpus.in.max >= sliders.vcpus.in.min &&
                 sliders.mem.in.max >= sliders.mem.in.min &&
                 sliders.disk.in.max >= sliders.disk.in.min;
    const blocked = vmsLeft <= 0 || !fits;
    document.getElementById('build-submit').disabled = blocked;
    if (blocked) _error('builder-error', vmsLeft <= 0 ? 'VM quota reached.' : 'Not enough free resources to build the minimum size.');
    else document.getElementById('builder-error').hidden = true;
}

function setSlider(s, min, max, step) {
    if (max < min) max = min;                 // degenerate: handled by `fits` check
    s.in.min = min; s.in.max = max; s.in.step = step;
    s.in.value = min;                          // default to the smallest size
    s.out.textContent = Math.round(min / s.scale);
}

builderForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    document.getElementById('builder-error').hidden = true;
    const btn = document.getElementById('build-submit');
    btn.disabled = true;
    try {
        const res = await api('/vms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                label: document.getElementById('vm-label').value,
                vcpus: Number(sliders.vcpus.in.value),
                mem_mb: Number(sliders.mem.in.value),
                disk_gb: Number(sliders.disk.in.value),
            }),
        });
        if (res.status === 401) return sessionExpired();
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'provision failed');
        builderForm.reset();
        builder.hidden = true;
        loadDashboard();
    } catch (e) {
        _error('builder-error', e.message);
    } finally {
        btn.disabled = false;
    }
});

// ---------- viewer ----------
const statusEl = document.getElementById('status');
const screenEl = document.getElementById('screen');

async function connect(vmId) {
    document.getElementById('dashboard-error').hidden = true;
    const res = await api(`/vms/${vmId}/session`, { method: 'POST' });
    if (res.status === 401) return sessionExpired();
    if (!res.ok) return _error('dashboard-error', (await res.json().catch(() => ({}))).detail || 'Could not start session');
    const { session_token } = await res.json();
    await connectViewer(session_token);
}

async function connectViewer(sessionToken) {
    const { default: RFB } = await import('/lib/novnc.bundle.js?v=1.5.0');
    showView('viewer');
    statusEl.textContent = 'Connecting…';
    await new Promise(r => requestAnimationFrame(r));

    const wsUrl = `wss://${window.location.host}/ck/ws?session_token=${sessionToken}`;
    _rfb = new RFB(screenEl, wsUrl, {});
    _rfb.scaleViewport = true;
    _rfb.resizeSession = false;
    _rfb.viewOnly = false;
    _rfb.qualityLevel = 6;
    _rfb.compressionLevel = 6;

    _rfb.addEventListener('connect', () => { statusEl.textContent = 'Connected'; });
    _rfb.addEventListener('disconnect', () => {
        _rfb = null;
        setTimeout(() => { showView('dashboard'); loadDashboard(); }, 600);
    });
    _rfb.addEventListener('credentialsrequired', () => {
        console.error('VNC requested credentials — config mismatch');
    });
}

document.getElementById('disconnect')
    .addEventListener('click', () => { if (_rfb) _rfb.disconnect(); });

// ---------- util ----------
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------- start ----------
showView('login');
