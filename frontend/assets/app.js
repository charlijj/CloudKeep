// app.js — CloudKeep client: login → dashboard (fleet + builder) → viewer.
//
// One page, three views toggled in place. The JWT lives only in module memory
// (never localStorage/sessionStorage/URL), so a navigation or tab close ends
// the session client-side. All dynamic DOM is built with createElement +
// textContent — server data never meets innerHTML, so there is no XSS sink
// even if a VM label or error message contains markup.
//
// Organisation: tiny helpers → api → views → login → dashboard → builder →
// viewer. Each section only talks to the ones above it.

'use strict';

// ---------- state ----------
let jwt = null;        // bearer token, memory only
let rfb = null;        // active noVNC connection
let pollTimer = null;  // dashboard refresh while any VM is building

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);

// h('div', 'card', child1, child2) — element builder; strings become text
// nodes (safe by construction).
function h(tag, className, ...children) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.append(...children);
    return el;
}

function showError(id, msg) {
    const el = $(id);
    el.textContent = msg;
    el.hidden = false;
}

function clearError(id) { $(id).hidden = true; }

const GB = (mb) => Math.round(mb / 1024);

// ---------- api ----------
// Controld is reached through the edge's /ck/ proxy. Every authed call goes
// through this wrapper so 401 (expired JWT) is handled in exactly one place.
async function api(path, opts = {}) {
    const res = await fetch(`/ck${path}`, {
        ...opts,
        headers: { ...opts.headers, 'Authorization': `Bearer ${jwt}` },
    });
    if (res.status === 401) { sessionExpired(); throw new Error('unauthorized'); }
    return res;
}

async function detail(res, fallback) {
    return (await res.json().catch(() => ({}))).detail || fallback;
}

// ---------- views ----------
const views = { login: $('view-login'), dashboard: $('view-dashboard'),
                viewer: $('view-viewer') };

function showView(name) {
    for (const [key, el] of Object.entries(views)) el.hidden = key !== name;
    document.body.dataset.view = name;
    $('logout').hidden = name === 'login';
    // Pre-warm the noVNC bundle so Connect feels instant.
    if (name === 'dashboard') import('/lib/novnc.bundle.js?v=1.5.0').catch(() => {});
}

function sessionExpired() {
    jwt = null;
    stopPolling();
    showView('login');
    showError('login-error', 'Session expired — please sign in again.');
}

// ---------- login ----------
$('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    clearError('login-error');
    const form = event.target;
    try {
        const res = await fetch('/ck/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: form.username.value,
                password: form.password.value,
            }),
        });
        if (!res.ok) throw new Error();
        jwt = (await res.json()).access_token;
        form.reset();
        showView('dashboard');
        refresh();
    } catch {
        showError('login-error', 'Invalid credentials');  // generic on purpose
    }
});

$('logout').addEventListener('click', () => {
    jwt = null;
    stopPolling();
    showView('login');
});

// ---------- dashboard ----------
function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// One refresh = VMs + resources in parallel; reused by polling, lifecycle
// actions, and viewer-exit so the dashboard is never stale.
async function refresh() {
    clearError('dashboard-error');
    try {
        const res = await api('/dashboard');           // VMs + resources, one round-trip
        const { vms, resources } = await res.json();
        renderVMs(vms);
        renderResources(resources);
        // Poll while anything is mid-build so cards flip to ready by themselves.
        const busy = vms.some(v => v.state === 'PROVISIONING' || v.state === 'REQUESTED' || v.state === 'DELETING');
        if (busy && !pollTimer) pollTimer = setInterval(refresh, 3000);
        if (!busy) stopPolling();
    } catch (e) {
        if (e.message !== 'unauthorized') showError('dashboard-error', 'Could not reach the server.');
    }
}

// One meter = a label row (name + value) over a thin track holding one or more
// proportional segments. Built with createElement (no innerHTML), widths set
// via .style so server numbers never become markup.
function meter(name, valueText, segments) {
    const bar = h('div', 'meter');
    for (const s of segments) {
        const seg = h('span', s.cls);
        seg.style.width = `${Math.max(0, Math.min(100, s.pct))}%`;
        bar.append(seg);
    }
    return h('div', null, h('div', 'meter-head', h('span', null, name), h('b', null, valueText)), bar);
}

function renderResources(r) {
    latestResources = r;   // builder caps its sliders from the same snapshot
    const you = r.you, host = r.host;
    const pct = (used, total) => (total > 0 ? (used / total) * 100 : 0);

    // Your usage: a single fill of used/quota, turning danger-red near the cap.
    const mine = (name, used, quota, unit) => {
        const p = pct(used, quota);
        return meter(name, `${used} / ${quota} ${unit}`,
                     [{ pct: p, cls: p >= 90 ? 'meter-fill meter-fill--warn' : 'meter-fill' }]);
    };
    // Host: stacked allocated + reserved over total; the empty remainder is the
    // free pool. `c` converts MB->GB for the memory row, identity otherwise.
    const pool = (name, q, unit, c = (x) => x) =>
        meter(name, `${c(q.free)} / ${c(q.total)} ${unit} free`, [
            { pct: pct(q.allocated, q.total), cls: 'meter-seg--alloc' },
            { pct: pct(q.reserved, q.total), cls: 'meter-seg--reserve' },
        ]);

    const yours = h('div', 'res-group',
        h('div', 'res-group-title', `your allocation · ${you.used_vms}/${you.max_vms} machines`),
        mine('vCPU', you.vcpus.used, you.vcpus.quota, 'vCPU'),
        mine('memory', GB(you.mem_mb.used), GB(you.mem_mb.quota), 'GB'),
        mine('disk', you.disk_gb.used, you.disk_gb.quota, 'GB'));

    const server = h('div', 'res-group',
        h('div', 'res-group-title', 'host availability'),
        pool('vCPU', host.vcpus, 'vCPU'),
        pool('memory', host.mem_mb, 'GB', GB),
        pool('disk', host.disk_gb, 'GB'));

    $('resbar').replaceChildren(yours, server);
}

const BADGE = {
    RUNNING: ['ready', 'badge--active'],
    PROVISIONING: ['building…', 'badge--pending'],
    REQUESTED: ['queued', 'badge--pending'],
    STOPPED: ['stopped', 'badge--down'],
    ERROR: ['error', 'badge--down badge--error'],
    DELETING: ['deleting…', 'badge--down'],
};

function renderVMs(vms) {
    const list = $('vm-list');
    list.replaceChildren();
    $('vm-empty').hidden = vms.length > 0;

    for (const vm of vms) {
        const [text, cls] = BADGE[vm.state] || [vm.state.toLowerCase(), 'badge--down'];
        const card = h('article', 'card',
            h('header', 'card-header',
                h('h3', 'card-title', vm.label),
                h('span', `badge ${cls}`, text)),
            h('p', 'card-body', `${vm.vcpus} vCPU · ${GB(vm.mem_mb)} GB RAM · ${vm.disk_gb} GB disk`));
        // While a VM is queued/building, show the live provisioning stage so the
        // user sees what it's doing rather than guessing whether it's hung.
        if (['REQUESTED', 'PROVISIONING'].includes(vm.state) && vm.progress)
            card.append(h('p', 'card-progress', vm.progress));
        if (vm.state === 'ERROR' && vm.error_msg)
            card.append(h('p', 'card-err', vm.error_msg));

        const actions = h('div', 'card-actions');
        if (vm.state === 'RUNNING') {
            actions.append(button('Connect', 'btn--card', () => connect(vm.id)));
            actions.append(button('Stop', 'btn--ghost', () => lifecycle(vm.id, 'stop')));
        } else if (vm.state === 'STOPPED') {
            actions.append(button('Start', 'btn--card', () => lifecycle(vm.id, 'start')));
        }
        // Live boot log: available while booting or running, so users can watch
        // a build progress and diagnose a slow/stuck boot.
        if (['PROVISIONING', 'RUNNING'].includes(vm.state))
            actions.append(button('Logs', 'btn--ghost', () => showConsole(vm.id, vm.label)));
        if (!['PROVISIONING', 'REQUESTED', 'DELETING'].includes(vm.state))
            actions.append(button('Delete', 'btn--ghost btn--danger', () => destroy(vm.id, vm.label)));
        if (actions.childElementCount) card.append(actions);
        list.append(card);
    }
}

function button(text, cls, onClick) {
    const b = h('button', `btn ${cls}`, text);
    b.type = 'button';
    // Disable for the duration of the action to prevent double-fire, then
    // re-enable. Actions that refresh()/navigate replace this node anyway; for
    // fire-and-forget ones (Logs just opens a pop-up, no refresh) this is what
    // lets the button work again without waiting for a re-render.
    b.addEventListener('click', async () => {
        b.disabled = true;
        try { await onClick(); } finally { b.disabled = false; }
    });
    return b;
}

async function lifecycle(id, verb) {
    const res = await api(`/vms/${id}/${verb}`, { method: 'POST' });
    if (!res.ok) showError('dashboard-error', await detail(res, `${verb} failed`));
    refresh();
}

async function destroy(id, label) {
    if (!confirm(`Delete "${label}"? This cannot be undone.`)) { refresh(); return; }
    const res = await api(`/vms/${id}`, { method: 'DELETE' });
    if (!res.ok && res.status !== 204) showError('dashboard-error', await detail(res, 'delete failed'));
    refresh();
}

// ---------- builder ----------
let latestResources = null;

const sliders = {
    vcpus: { input: $('in-vcpus'), output: $('out-vcpus'), toUnits: v => v },
    mem:   { input: $('in-mem'),   output: $('out-mem'),   toUnits: GB },
    disk:  { input: $('in-disk'),  output: $('out-disk'),  toUnits: v => v },
};
for (const s of Object.values(sliders))
    s.input.addEventListener('input', () => { s.output.textContent = s.toUnits(s.input.value); });

// -/+ steppers: nudge the matching slider by one step, clamped to its range.
// Gives precise control even though the slider track itself is short.
for (const btn of document.querySelectorAll('.step')) {
    btn.addEventListener('click', () => {
        const s = sliders[btn.dataset.slider];
        const step = Number(s.input.step) || 1;
        const next = Number(s.input.value) + Number(btn.dataset.dir) * step;
        s.input.value = Math.min(Number(s.input.max), Math.max(Number(s.input.min), next));
        s.output.textContent = s.toUnits(s.input.value);
    });
}

$('build-toggle').addEventListener('click', () => {
    const builder = $('builder');
    builder.hidden = !builder.hidden;
    if (!builder.hidden) configureBuilder();
});

// Cap every slider at min(static bound, host free, quota left). The server
// re-validates under its allocation lock — these caps are UX, not security.
function configureBuilder() {
    clearError('builder-error');
    const r = latestResources;
    if (!r) return;
    const b = r.bounds, host = r.host, you = r.you;
    const left = (q) => q.quota - q.used;

    setRange(sliders.vcpus, b.vcpus[0], Math.min(b.vcpus[1], host.vcpus.free, left(you.vcpus)), 1);
    setRange(sliders.mem, b.mem_mb[0], Math.min(b.mem_mb[1], host.mem_mb.free, left(you.mem_mb)), b.mem_mb[2]);
    setRange(sliders.disk, b.disk_gb[0], Math.min(b.disk_gb[1], host.disk_gb.free, left(you.disk_gb)), 1);

    const vmsLeft = you.max_vms - you.used_vms;
    const fits = Object.values(sliders).every(s => Number(s.input.max) >= Number(s.input.min));
    $('build-submit').disabled = vmsLeft <= 0 || !fits;
    if (vmsLeft <= 0) showError('builder-error', 'VM quota reached.');
    else if (!fits) showError('builder-error', 'Not enough free resources for the minimum size.');
}

function setRange(s, min, max, step) {
    s.input.min = min;
    s.input.max = Math.max(min, max);
    s.input.step = step;
    s.input.value = min;                  // default to the smallest build
    s.output.textContent = s.toUnits(min);
}

$('builder').addEventListener('submit', async (event) => {
    event.preventDefault();
    clearError('builder-error');
    const btn = $('build-submit');
    btn.disabled = true;
    try {
        const res = await api('/vms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                label: $('vm-label').value,
                vcpus: Number(sliders.vcpus.input.value),
                mem_mb: Number(sliders.mem.input.value),
                disk_gb: Number(sliders.disk.input.value),
            }),
        });
        if (!res.ok) throw new Error(await detail(res, 'provision failed'));
        $('builder').reset();
        $('builder').hidden = true;
        refresh();                         // the new card appears as "queued"
    } catch (e) {
        if (e.message !== 'unauthorized') showError('builder-error', e.message);
    } finally {
        btn.disabled = false;
    }
});

// ---------- viewer ----------
async function connect(vmId) {
    // Mint a single-use, 30s token bound server-side to this VM, then hand it
    // straight to the WebSocket — it never touches storage or history.
    const res = await api(`/vms/${vmId}/session`, { method: 'POST' });
    if (!res.ok) { showError('dashboard-error', await detail(res, 'could not start session')); refresh(); return; }
    const { session_token } = await res.json();

    const { default: RFB } = await import('/lib/novnc.bundle.js?v=1.5.0');
    showView('viewer');
    $('status').textContent = 'Connecting…';
    await new Promise(r => requestAnimationFrame(r));  // let #screen get height

    rfb = new RFB($('screen'), `wss://${location.host}/ck/ws?session_token=${session_token}`, {});
    rfb.resizeSession = true;    // ask the guest to match the browser canvas -> crisp, native-res desktop
    rfb.scaleViewport = true;    // fallback: scale the framebuffer if the server can't resize
    rfb.qualityLevel = 6;        // Tight+JPEG bytes-vs-quality dial
    rfb.compressionLevel = 6;

    rfb.addEventListener('connect', () => { $('status').textContent = 'Connected'; });
    rfb.addEventListener('disconnect', () => {
        rfb = null;
        setTimeout(() => { showView('dashboard'); refresh(); }, 600);
    });
}

$('disconnect').addEventListener('click', () => { if (rfb) rfb.disconnect(); });

// ---------- console (live boot log) ----------
// The boot log opens in a SEPARATE browser window (app/console.html) so it can
// be watched alongside the VM's desktop. window.open is called synchronously
// from the Logs click so pop-up blockers allow it; the window name is keyed to
// the VM so re-clicking focuses the existing window instead of spawning more.
function showConsole(vmId, label) {
    const url = `/app/console.html#vm=${encodeURIComponent(vmId)}&label=${encodeURIComponent(label)}`;
    const features = 'popup=yes,width=940,height=620,menubar=no,toolbar=no,' +
                     'location=no,status=no,resizable=yes,scrollbars=yes';
    const win = window.open(url, `ckconsole_${vmId}`, features);
    if (!win) {
        showError('dashboard-error', 'Pop-up blocked — allow pop-ups for this site to open the boot log.');
        return;
    }
    try { win.focus(); } catch { /* cross-window focus can throw in some browsers */ }
}

// Called BY the console pop-up (same origin) to obtain a single-use,
// console-bound token. The pop-up never holds the JWT — it stays in this
// window's memory — so it delegates minting here. Returns the token string, or
// null if minting failed (api() centralises 401 -> session-expired handling).
window.__ckMintConsole = async (vmId) => {
    try {
        const res = await api(`/vms/${vmId}/console-session`, { method: 'POST' });
        if (!res.ok) return null;
        return (await res.json()).session_token;
    } catch {
        return null;   // unauthorized / network — the pop-up shows a retry state
    }
};

// ---------- start ----------
showView('login');
