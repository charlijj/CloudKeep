// console.js — the pop-up boot-log window (opened by the dashboard).
//
// Runs in its own browser window so the user can watch a VM boot while using
// its desktop in the main window. It deliberately holds NO credentials: the
// JWT lives only in the opener (dashboard) window's memory. To (re)connect we
// ask the opener to mint a single-use, console-bound token on our behalf via
// `window.opener.__ckMintConsole(vmId)`. The bare token rides the WS URL only;
// it never touches storage or history.
//
// This mirrors the dashboard's old in-page console logic (status badge,
// bounded buffer, auto-reconnect) — moved here so the console is a real second
// window that works across browsers.

'use strict';

const $ = (id) => document.getElementById(id);

// vmId/label arrive via the URL hash (#vm=…&label=…) — not secret, and the
// hash never reaches the server in a request.
const params = new URLSearchParams(location.hash.slice(1));
const vmId = params.get('vm');
const label = params.get('label') || 'machine';
const MAX_RETRIES = 5;

let ws = null;
let attempts = 0;
let closing = false;        // set on unload so a deliberate close doesn't retry

document.title = `boot log · ${label}`;
$('console-title').textContent = `Boot log — ${label}`;

function setStatus(text, cls) {
    $('console-status').className = `badge ${cls || ''}`.trim();
    $('console-status').textContent = text;
}

// Mint through the opener so the JWT stays in the dashboard window. Returns the
// token string, or null if the opener is gone / the call failed (e.g. expired
// session, in which case the opener handles its own 401 and returns null).
async function mintToken() {
    const opener = window.opener;
    if (!opener || opener.closed || typeof opener.__ckMintConsole !== 'function') return null;
    try { return await opener.__ckMintConsole(vmId); }
    catch { return null; }
}

async function openStream() {
    if (closing) return;
    setStatus('Connecting…', 'badge--pending');
    $('console-reconnect').hidden = true;

    const token = await mintToken();
    if (closing) return;
    if (!token) {
        onDrop('Dashboard window unavailable — reopen the boot log from there.');
        return;
    }

    const log = $('console-log');
    ws = new WebSocket(`wss://${location.host}/ck/console?session_token=${token}`);
    ws.addEventListener('open', () => { attempts = 0; setStatus('Streaming', 'badge--active'); });
    ws.addEventListener('message', (ev) => {
        // Stay pinned to the bottom only if the user is already there, so a
        // manual scroll-back to read earlier output isn't yanked away.
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
        log.textContent += ev.data;
        // Cap the buffer so a long stream can't grow the DOM node unboundedly.
        if (log.textContent.length > 200000) log.textContent = log.textContent.slice(-160000);
        if (atBottom) log.scrollTop = log.scrollHeight;
    });
    ws.addEventListener('close', () => { ws = null; onDrop(); });
    // 'error' is always followed by 'close', which drives reconnect — no-op.
}

// A drop auto-reconnects a few times: covers the brief window right after a
// build where the domain isn't attachable yet, plus transient blips. After the
// cap, stop and offer a manual Reconnect.
function onDrop(msg) {
    if (closing) return;
    if (attempts < MAX_RETRIES) {
        attempts += 1;
        setStatus(`Reconnecting… (${attempts}/${MAX_RETRIES})`, 'badge--pending');
        setTimeout(openStream, 1500);
    } else {
        setStatus(msg || 'Disconnected', 'badge--down');
        $('console-reconnect').hidden = false;
    }
}

$('console-close').addEventListener('click', () => window.close());
$('console-reconnect').addEventListener('click', () => { attempts = 0; openStream(); });
window.addEventListener('beforeunload', () => { closing = true; if (ws) ws.close(); });

if (!vmId) setStatus('No machine specified', 'badge--down');
else openStream();
