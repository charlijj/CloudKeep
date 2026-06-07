// app.js — CloudKeep single-page gateway client (login + dashboard + viewer).
//
// One page, three views toggled in place. Because nothing navigates, the JWT
// and session token never leave module memory: no localStorage, no
// sessionStorage, no URL fragment. Module scope keeps them off `window`.

let _jwt = null;   // set on login, held in memory only
let _rfb = null;   // active RFB connection, if any

// Centralised error display: shows message in the nearest .form-error element.
function _error(elId, msg) {
    console.error('[cloudkeep]', elId, msg);
    const el = document.getElementById(elId);
    if (el) { el.textContent = msg; el.hidden = false; }
}

const views = {
    login: document.getElementById('view-login'),
    dashboard: document.getElementById('view-dashboard'),
    viewer: document.getElementById('view-viewer'),
};

function showView(name) {
    for (const [key, el] of Object.entries(views)) el.hidden = key !== name;
    document.body.dataset.view = name;
    // Warm up RFB.js the moment the dashboard becomes visible so the module
    // is cached by the time the user clicks Connect. Single pre-bundled file
    // (esbuild) — one request instead of noVNC's ~40-module graph.
    if (name === 'dashboard') import('/lib/novnc.bundle.js?v=1.5.0').catch(() => {});
}

// ---------- login ----------
const form = document.getElementById('login-form');
const loginErr = document.getElementById('login-error');
const submitBtn = document.getElementById('submit');

form.addEventListener('submit', async (event) => {
    event.preventDefault();
    loginErr.hidden = true;
    submitBtn.disabled = true;
    try {
        const res = await fetch('/ck/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: form.username.value,
                password: form.password.value,
            }),
        });
        if (!res.ok) throw new Error('login failed');
        _jwt = (await res.json()).access_token;  // stays in memory only
        form.reset();
        showView('dashboard');
    } catch {
        // Generic message — no hint whether user or password was wrong.
        _error('login-error', 'Invalid credentials');
    } finally {
        submitBtn.disabled = false;
    }
});

// ---------- dashboard ----------
const dashErr = document.getElementById('dashboard-error');

async function startSession(button) {
    button.disabled = true;
    dashErr.hidden = true;
    try {
        const res = await fetch('/ck/auth/session', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${_jwt}` },
        });
        if (res.status === 401) {  // JWT expired/invalid -> re-auth
            _jwt = null; _error('dashboard-error', 'Session expired — please sign in again.'); showView('login'); return;
        }
        if (!res.ok) throw new Error('session failed');
        const { session_token } = await res.json();
        await connectViewer(session_token);  // passed directly, never in URL
    } catch {
        _error('dashboard-error', `Session request failed (${res?.status ?? 'network error'})`);
    } finally {
        button.disabled = false;
    }
}

for (const button of document.querySelectorAll('[data-vm]')) {
    button.addEventListener('click', () => startSession(button));
}

// ---------- viewer ----------
const statusEl = document.getElementById('status');
const screenEl = document.getElementById('screen');

async function connectViewer(sessionToken) {
    const { default: RFB } = await import('/lib/novnc.bundle.js?v=1.5.0');
    showView('viewer');
    statusEl.textContent = 'Connecting\u2026';
    // Yield one frame so layout paints and #screen has measurable height.
    await new Promise(r => requestAnimationFrame(r));

    const wsUrl = `wss://${window.location.host}/ck/ws?session_token=${sessionToken}`;
    _rfb = new RFB(screenEl, wsUrl, {});
    _rfb.scaleViewport = true;   // fit framebuffer to canvas
    _rfb.resizeSession = false;  // do not ask the server to resize
    _rfb.viewOnly = false;       // forward mouse + keyboard

    // --- Latency tuning: the bytes-vs-quality/CPU dials for Tight+JPEG ---
    // These are sent to the server (Tight pseudo-encodings) and override its
    // defaults, so the server config no longer has to guess. Tuned for WAN
    // responsiveness: readable text with fewer bytes per frame. Adjust after
    // measuring a real session — lower quality on slow links, lower
    // compression if the VM (software-rendered) becomes encoder-CPU bound.
    _rfb.qualityLevel = 6;       // JPEG quality, 0-9 (default 6)
    _rfb.compressionLevel = 6;   // zlib effort, 0-9; higher = fewer bytes, more VM CPU

    _rfb.addEventListener('connect', () => { statusEl.textContent = 'Connected'; });
    _rfb.addEventListener('disconnect', () => {
        _rfb = null;
        // JWT is still valid (1hr) -> return to dashboard, reconnect needs a
        // fresh single-use session token, which the dashboard requests again.
        setTimeout(() => showView('dashboard'), 600);
    });
    _rfb.addEventListener('credentialsrequired', () => {
        // Should never fire: VNC runs SecurityTypes=None behind the gateway.
        console.error('VNC requested credentials — config mismatch');
    });
}

document.getElementById('disconnect')
    .addEventListener('click', () => { if (_rfb) _rfb.disconnect(); });

// ---------- start ----------
showView('login');
