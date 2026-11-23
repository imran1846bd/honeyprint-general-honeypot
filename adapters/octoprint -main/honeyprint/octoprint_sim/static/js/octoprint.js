/**
 * OctoPrint Honeypot - Frontend JavaScript
 * Simulates OctoPrint web interface behavior for realistic interaction.
 */

// State
let apiKey = '';
let sessionToken = '';
let isLoggedIn = false;
let ws = null;
let connected = true;
let tempHistory = { hotend: [], bed: [], timestamps: [] };
let lastJobState = 'idle';
let lastLoggedPrintLine = -1;
let lastLoggedPrintFile = '';
let lastDisplayedLine = '';
let lastDisplayedCommand = '';
let viewedGcodeFile = '';
let currentPrinterState = 'operational';
const MAX_HISTORY = 120;
const ALLOWED_UPLOAD_EXTENSIONS = ['.gcode', '.gco', '.g', '.bgcode'];

// === Initialization ===
document.addEventListener('DOMContentLoaded', () => {
    setupTabs();
    setupButtonFeedback();
    checkAuth();
});

// Pulse any clicked .btn for tactile feedback
function setupButtonFeedback() {
    document.body.addEventListener('click', (e) => {
        const btn = e.target.closest('.btn');
        if (!btn) return;
        btn.classList.remove('btn-pulse');
        // force reflow so the animation can replay
        void btn.offsetWidth;
        btn.classList.add('btn-pulse');
    });
}

// === Toast notifications ===
function toast(message, type = 'info', durationMs = 2200) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const icons = { success: '✓', info: 'ℹ', warning: '⚠', error: '✕' };
    const el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.innerHTML =
        `<span class="toast-icon">${icons[type] || 'ℹ'}</span>` +
        `<span class="toast-msg"></span>`;
    el.querySelector('.toast-msg').textContent = message;
    container.appendChild(el);
    setTimeout(() => {
        el.classList.add('toast-out');
        setTimeout(() => el.remove(), 220);
    }, durationMs);
}

function setupTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            const target = tab.getAttribute('data-tab');
            document.getElementById('tab-' + target).classList.add('active');
        });
    });
}

// === Authentication ===
async function checkAuth() {
    try {
        const resp = await fetch('/api/login');
        if (resp.ok) {
            const data = await resp.json();
            if (data.name) {
                loginSuccess(data);
                return;
            }
        }
    } catch(e) {}
    showLogin();
}

function showLogin() {
    document.getElementById('login-modal').style.display = 'flex';
    document.getElementById('main-content').style.filter = 'blur(5px)';
}

function hideLogin() {
    document.getElementById('login-modal').style.display = 'none';
    document.getElementById('main-content').style.filter = 'none';
}

async function doLogin(e) {
    e.preventDefault();
    const user = document.getElementById('login-user').value;
    const pass = document.getElementById('login-pass').value;

    try {
        const resp = await fetch('/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({user: user, pass: pass})
        });
        if (resp.ok) {
            const data = await resp.json();
            loginSuccess(data);
        } else {
            document.getElementById('login-error').style.display = 'block';
            document.getElementById('login-error').textContent = 'Invalid username or password.';
        }
    } catch(err) {
        document.getElementById('login-error').style.display = 'block';
        document.getElementById('login-error').textContent = 'Connection error.';
    }
}

function loginSuccess(data) {
    const wasLoggedIn = isLoggedIn;
    isLoggedIn = true;
    apiKey = data.apikey || '';
    sessionToken = data.session || '';
    hideLogin();
    document.getElementById('user-display').textContent = data.name || 'admin';
    document.getElementById('btn-logout').style.display = 'inline-block';
    initDashboard();
    if (!wasLoggedIn) {
        toast('Welcome, ' + (data.name || 'admin'), 'success', 1800);
    }
}

function doLogout() {
    isLoggedIn = false;
    apiKey = '';
    document.cookie = 'octoprint_session=; Max-Age=0';
    showLogin();
}

// === Dashboard ===
function initDashboard() {
    refreshFiles();
    loadPrinterState();
    connectWebSocket();
    startPolling();
}

// === WebSocket ===
function connectWebSocket() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = proto + '//' + window.location.host + '/ws';
    try {
        ws = new WebSocket(wsUrl);
        ws.onopen = () => { termLog('WebSocket connected', 'resp'); };
        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleWsMessage(msg);
            } catch(e) {}
        };
        ws.onclose = () => {
            setTimeout(connectWebSocket, 5000);
        };
    } catch(e) {
        setTimeout(connectWebSocket, 5000);
    }
}

let _wsTickCount = 0;
function handleWsMessage(msg) {
    if (msg.current) {
        updateFromCurrent(msg.current);
        // Throttle: only print a temperature line every 5 seconds
        const t = (msg.current.temps || [])[0];
        _wsTickCount++;
        if (t && t.tool0 && (_wsTickCount % 5 === 0)) {
            const line = `Recv: T:${t.tool0.actual.toFixed(2)} /${t.tool0.target.toFixed(2)} ` +
                         `B:${t.bed.actual.toFixed(2)} /${t.bed.target.toFixed(2)} @:0 B@:0`;
            termLog(line, 'resp');
        }
    }
    if (msg.event) {
        termLog('Event: ' + (msg.event.type || JSON.stringify(msg.event)), 'resp');
    }
}

function updateFromCurrent(data) {
    // Temperature
    if (data.temps && data.temps.length > 0) {
        const t = data.temps[0];
        if (t.tool0) {
            updateTempDisplay('hotend', t.tool0.actual, t.tool0.target);
        }
        if (t.bed) {
            updateTempDisplay('bed', t.bed.actual, t.bed.target);
        }
    }
    // State
    if (data.state) {
        updateState(data.state);
    }
    // Job
    if (data.job) {
        updateJob(data.job, data.progress);
    }
}

// === Polling Fallback ===
let pollInterval = null;

function startPolling() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(async () => {
        if (!isLoggedIn) return;
        try {
            const headers = apiKey ? {'X-Api-Key': apiKey} : {};

            // Get printer state
            const pResp = await fetch('/api/printer', {headers});
            if (pResp.ok) {
                const pData = await pResp.json();
                if (pData.temperature) {
                    if (pData.temperature.tool0) {
                        updateTempDisplay('hotend',
                            pData.temperature.tool0.actual,
                            pData.temperature.tool0.target);
                    }
                    if (pData.temperature.bed) {
                        updateTempDisplay('bed',
                            pData.temperature.bed.actual,
                            pData.temperature.bed.target);
                    }
                }
                if (pData.state) {
                    updateState(pData.state);
                }
            }

            // Get job state
            const jResp = await fetch('/api/job', {headers});
            if (jResp.ok) {
                const jData = await jResp.json();
                updateJob(jData.job, jData.progress);
            }
        } catch(e) {}
    }, 2000);
}

// === UI Updates ===
function updateTempDisplay(heater, actual, target) {
    const actualEl = document.getElementById(heater + '-actual');
    const targetEl = document.getElementById(heater + '-target');
    if (actualEl) actualEl.textContent = (actual || 0).toFixed(1) + '\u00B0C';
    if (targetEl) targetEl.textContent = (target || 0).toFixed(1) + '\u00B0C';

    // Update chart history
    tempHistory[heater].push(actual || 0);
    if (tempHistory[heater].length > MAX_HISTORY) tempHistory[heater].shift();
    if (heater === 'hotend') {
        tempHistory.timestamps.push(new Date());
        if (tempHistory.timestamps.length > MAX_HISTORY) tempHistory.timestamps.shift();
        drawTempChart();
    }
}

function updateState(state) {
    const stateEl = document.getElementById('state-text');
    const badgeEl = document.getElementById('printer-state');
    const text = state.text || 'Operational';
    currentPrinterState = text.toLowerCase();
    if (stateEl) stateEl.textContent = text;
    if (badgeEl) {
        badgeEl.textContent = text;
        badgeEl.className = 'state-badge state-' + text.toLowerCase();
    }
    syncViewerPrintControls();
}

function updateJob(job, progress) {
    const jobInfo = document.getElementById('job-info');
    if (!job || !job.file || !job.file.name) {
        if (jobInfo) jobInfo.style.display = 'none';
        return;
    }
    if (jobInfo) jobInfo.style.display = 'block';

    const fileEl = document.getElementById('job-file');
    const progText = document.getElementById('job-progress-text');
    const progBar = document.getElementById('job-progress-bar');
    const timeEl = document.getElementById('job-time');
    const remEl = document.getElementById('job-remaining');
    const lineEl = document.getElementById('job-line');
    const commandEl = document.getElementById('job-command');

    const fileName = job.file.name || '-';
    if (fileName !== lastLoggedPrintFile) {
        lastLoggedPrintFile = fileName;
        lastLoggedPrintLine = -1;
        lastDisplayedLine = '';
        lastDisplayedCommand = '';
    }

    if (fileEl) fileEl.textContent = fileName;
    if (progress) {
        const comp = progress.completion || 0;
        const currentLine = progress.currentLine || 0;
        const totalLines = progress.totalLines || 0;
        const currentCommand = progress.currentCommand || '';
        if (progText) progText.textContent = comp.toFixed(1) + '%';
        if (progBar) progBar.style.width = comp + '%';
        if (timeEl) timeEl.textContent = formatTime(progress.printTime);
        if (remEl) remEl.textContent = formatTime(progress.printTimeLeft);
        if (currentLine && totalLines) {
            lastDisplayedLine = `${currentLine}/${totalLines}`;
        }
        if (lineEl) lineEl.textContent = lastDisplayedLine || '-';
        highlightExecutingGcodeLine(currentLine);
        if (currentCommand) {
            lastDisplayedCommand = currentCommand;
        }
        if (commandEl) commandEl.textContent = lastDisplayedCommand || '-';
        if (currentLine && currentLine !== lastLoggedPrintLine && currentCommand) {
            lastLoggedPrintLine = currentLine;
            termLog(
                `Print line ${currentLine}/${totalLines}: ${currentCommand}`,
                'resp'
            );
        }
    }

    // Update pause button label based on the printer state badge
    const stateText = (document.getElementById('printer-state').textContent || '').toLowerCase();
    const pauseBtn = document.getElementById('btn-pause');
    if (pauseBtn) {
        pauseBtn.textContent = stateText === 'paused' ? 'Resume' : 'Pause';
    }
    syncViewerPrintControls();
}

// === Temperature Chart ===
function drawTempChart() {
    const canvas = document.getElementById('temp-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;

    ctx.clearRect(0, 0, w, h);

    // Background
    ctx.fillStyle = '#2b2b2b';
    ctx.fillRect(0, 0, w, h);

    // Grid
    ctx.strokeStyle = '#444';
    ctx.lineWidth = 0.5;
    for (let y = 0; y <= h; y += h/5) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
    }

    // Y-axis labels
    ctx.fillStyle = '#888';
    ctx.font = '10px sans-serif';
    const maxTemp = 300;
    for (let i = 0; i <= 5; i++) {
        const temp = maxTemp - (maxTemp * i / 5);
        ctx.fillText(temp + '\u00B0', 2, i * h/5 + 12);
    }

    // Draw hotend line
    drawLine(ctx, tempHistory.hotend, w, h, maxTemp, '#4CAF50');
    // Draw bed line
    drawLine(ctx, tempHistory.bed, w, h, maxTemp, '#2196F3');

    // Legend
    ctx.fillStyle = '#4CAF50';
    ctx.fillRect(w - 120, 10, 10, 10);
    ctx.fillStyle = '#888';
    ctx.fillText('Hotend', w - 105, 19);
    ctx.fillStyle = '#2196F3';
    ctx.fillRect(w - 120, 25, 10, 10);
    ctx.fillStyle = '#888';
    ctx.fillText('Bed', w - 105, 34);
}

function drawLine(ctx, data, w, h, maxTemp, color) {
    if (data.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const step = w / MAX_HISTORY;
    for (let i = 0; i < data.length; i++) {
        const x = i * step;
        const y = h - (data[i] / maxTemp * h);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.stroke();
}

// === API Calls ===
function getHeaders() {
    const h = {'Content-Type': 'application/json'};
    if (apiKey) h['X-Api-Key'] = apiKey;
    return h;
}

async function loadPrinterState() {
    try {
        const resp = await fetch('/api/printer', {headers: getHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            if (data.temperature) {
                if (data.temperature.tool0) {
                    updateTempDisplay('hotend',
                        data.temperature.tool0.actual,
                        data.temperature.tool0.target);
                }
                if (data.temperature.bed) {
                    updateTempDisplay('bed',
                        data.temperature.bed.actual,
                        data.temperature.bed.target);
                }
            }
        }
    } catch(e) {}
}

async function setTemp(heater) {
    const inputId = heater === 'tool0' ? 'hotend-set' : 'bed-set';
    const temp = parseInt(document.getElementById(inputId).value) || 0;
    const endpoint = heater === 'tool0' ? '/api/printer/tool' : '/api/printer/bed';
    const body = heater === 'tool0'
        ? {command: 'target', targets: {tool0: temp}}
        : {command: 'target', target: temp};

    await fetch(endpoint, {method: 'POST', headers: getHeaders(), body: JSON.stringify(body)});
    toast((heater === 'tool0' ? 'Hotend' : 'Bed') + ' target: ' + temp + '°C', 'success', 1600);
}

async function offTemp(heater) {
    const endpoint = heater === 'tool0' ? '/api/printer/tool' : '/api/printer/bed';
    const body = heater === 'tool0'
        ? {command: 'target', targets: {tool0: 0}}
        : {command: 'target', target: 0};

    await fetch(endpoint, {method: 'POST', headers: getHeaders(), body: JSON.stringify(body)});
    document.getElementById(heater === 'tool0' ? 'hotend-set' : 'bed-set').value = '0';
    toast((heater === 'tool0' ? 'Hotend' : 'Bed') + ' off', 'info', 1400);
}

function presetTemp(heater, temp) {
    const inputId = heater === 'tool0' ? 'hotend-set' : 'bed-set';
    document.getElementById(inputId).value = temp;
    setTemp(heater);
}

async function jog(axis, amount) {
    const dist = parseFloat(document.getElementById('jog-distance').value) || 10;
    const actual = amount > 0 ? dist : -dist;
    await sendCmd(`G91\nG1 ${axis}${actual} F3000\nG90`);
    toast(`Jog ${axis}${actual >= 0 ? '+' : ''}${actual} mm`, 'info', 1400);
}

async function homeAxes() {
    await sendCmd('G28');
    toast('Homing all axes...', 'info', 1800);
}

async function extrude(direction) {
    const amount = parseInt(document.getElementById('extrude-amount').value) || 5;
    await sendCmd(`G91\nG1 E${amount * direction} F300\nG90`);
    toast(direction > 0 ? `Extruding ${amount} mm` : `Retracting ${amount} mm`, 'info', 1400);
}

function setFan(value) {
    const pct = value > 0 ? Math.round(value/255*100) + '%' : 'Off';
    document.getElementById('fan-display').textContent = pct;
    sendCmd('M106 S' + value);
}

// === Extra control handlers ===

async function runCmd(gcode, message) {
    await sendCmd(gcode);
    if (message) toast(message, 'success', 1600);
    termLog('>>> ' + gcode, 'cmd');
}

async function emergencyStop() {
    if (!confirm('EMERGENCY STOP — halt the printer immediately?')) return;
    await sendCmd('M112');
    toast('EMERGENCY STOP issued', 'error', 3500);
    termLog('!!! M112 EMERGENCY STOP', 'err');
}

let _speedTimer = null;
function setSpeedFactor(value) {
    document.getElementById('speed-display').textContent = value + '%';
    clearTimeout(_speedTimer);
    _speedTimer = setTimeout(() => {
        sendCmd('M220 S' + value);
        toast('Print speed: ' + value + '%', 'info', 1200);
    }, 250);
}

let _flowTimer = null;
function setFlowFactor(value) {
    document.getElementById('flow-display').textContent = value + '%';
    clearTimeout(_flowTimer);
    _flowTimer = setTimeout(() => {
        sendCmd('M221 S' + value);
        toast('Flow rate: ' + value + '%', 'info', 1200);
    }, 250);
}

let _babystepZ = 0;
async function babystepZ(delta) {
    _babystepZ += delta;
    document.getElementById('babystep-display').textContent =
        (_babystepZ >= 0 ? '+' : '') + _babystepZ.toFixed(3) + ' mm';
    await sendCmd('M290 Z' + delta.toFixed(3));
    toast(`Babystep Z ${delta >= 0 ? '+' : ''}${delta.toFixed(3)} mm`, 'info', 1100);
}

async function sendCmd(gcode) {
    const commands = gcode.split('\n').filter(c => c.trim());
    await fetch('/api/printer/command', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({commands: commands})
    });
}

function toggleConnection() {
    // The printer is always physically "connected" in the honeypot,
    // but we toggle the UI state so the button feels responsive.
    connected = !connected;
    const btn = document.getElementById('btn-connect');
    const stateText = document.getElementById('state-text');
    const stateBadge = document.getElementById('printer-state');
    if (connected) {
        btn.textContent = 'Disconnect';
        btn.className = 'btn btn-primary btn-block';
        if (stateText) stateText.textContent = 'Operational';
        if (stateBadge) {
            stateBadge.textContent = 'Operational';
            stateBadge.className = 'state-badge state-operational';
        }
        startPolling();
        connectWebSocket();
        termLog('Connected to printer', 'resp');
        toast('Connected to printer', 'success', 1600);
    } else {
        btn.textContent = 'Connect';
        btn.className = 'btn btn-block';
        if (stateText) stateText.textContent = 'Closed';
        if (stateBadge) {
            stateBadge.textContent = 'Closed';
            stateBadge.className = 'state-badge state-error';
        }
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
        if (ws) { try { ws.close(); } catch(e) {} ws = null; }
        termLog('Disconnected from printer', 'err');
        toast('Disconnected from printer', 'warning', 1800);
    }
}

async function pauseJob() {
    const stateText = (document.getElementById('printer-state').textContent || '').toLowerCase();
    const action = stateText === 'paused' ? 'resume' : 'pause';
    await fetch('/api/job', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({command: 'pause', action: action})
    });
    termLog('Job ' + action + ' requested', 'cmd');
    toast(action === 'pause' ? 'Print paused' : 'Print resumed',
          action === 'pause' ? 'warning' : 'success', 1800);
}

async function cancelJob() {
    if (!confirm('Cancel the current print job?')) return;
    await fetch('/api/job', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({command: 'cancel'})
    });
    lastLoggedPrintLine = -1;
    lastDisplayedLine = '';
    lastDisplayedCommand = '';
    termLog('Job cancel requested', 'cmd');
    toast('Print cancelled', 'error', 1800);
}

// === Terminal ===
async function sendTerminalCmd() {
    const input = document.getElementById('terminal-cmd');
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = '';

    termLog('>>> ' + cmd, 'cmd');

    try {
        // Send via command API and also via direct terminal endpoint
        const resp = await fetch('/api/terminal', {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify({command: cmd})
        });
        if (resp.ok) {
            const data = await resp.json();
            if (data.response) {
                termLog(data.response, 'resp');
            }
        } else {
            // Fallback to printer command API
            await fetch('/api/printer/command', {
                method: 'POST',
                headers: getHeaders(),
                body: JSON.stringify({commands: [cmd]})
            });
            termLog('ok', 'resp');
        }
    } catch(e) {
        termLog('Error: ' + e.message, 'err');
    }
}

function termLog(text, cls) {
    const output = document.getElementById('terminal-output');
    if (!output) return;
    const line = document.createElement('div');
    line.className = cls || 'resp';
    line.textContent = text;
    output.appendChild(line);
    output.scrollTop = output.scrollHeight;
}

// === Files ===
async function refreshFiles() {
    try {
        const resp = await fetch('/api/files', {headers: getHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            renderFiles(data.files || []);
        }
    } catch(e) {}
}

function renderFiles(files) {
    const list = document.getElementById('file-list');
    if (!list) return;
    list.innerHTML = '';
    files.forEach(f => {
        const name = f.name || '';
        const item = document.createElement('div');
        item.className = 'file-item';

        const fileName = document.createElement('span');
        fileName.className = 'file-name';
        fileName.textContent = f.display || name;
        fileName.addEventListener('click', () => selectFile(name));

        const fileSize = document.createElement('span');
        fileSize.className = 'file-size';
        fileSize.textContent = formatBytes(f.size);

        const actions = document.createElement('div');
        actions.className = 'file-actions';

        const printBtn = document.createElement('button');
        printBtn.className = 'btn btn-xs';
        printBtn.title = 'Print';
        printBtn.textContent = '▶';
        printBtn.addEventListener('click', () => printFile(name));

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'btn btn-xs btn-danger';
        deleteBtn.title = 'Delete';
        deleteBtn.textContent = '✕';
        deleteBtn.addEventListener('click', () => deleteFile(name));

        actions.appendChild(printBtn);
        actions.appendChild(deleteBtn);
        item.appendChild(fileName);
        item.appendChild(fileSize);
        item.appendChild(actions);
        list.appendChild(item);
    });
}

async function selectFile(name) {
    termLog('Loading file: ' + name, 'cmd');
    try {
        const resp = await fetch('/downloads/files/local/' + encodeURIComponent(name), {
            headers: getHeaders()
        });
        if (!resp.ok) {
            termLog('Failed to load file', 'err');
            return;
        }
        const text = await resp.text();
        renderGcode(name, text);
        // Switch to GCode Viewer tab
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        document.querySelector('.tab[data-tab="gcode"]').classList.add('active');
        document.getElementById('tab-gcode').classList.add('active');
    } catch (e) {
        termLog('Error: ' + e.message, 'err');
    }
}

function renderGcode(name, text) {
    document.getElementById('gcode-empty').style.display = 'none';
    document.getElementById('gcode-content-wrapper').style.display = 'block';
    document.getElementById('gcode-filename').textContent = name;
    viewedGcodeFile = name;
    syncViewerPrintControls();

    const lines = text.split('\n');
    let layers = 0;
    let moves = 0;
    let extrusions = 0;
    let maxZ = 0;
    let filamentMm = 0;

    // Syntax-highlight first 1000 lines (avoid massive DOM for huge files)
    const limit = Math.min(lines.length, 1000);
    const out = [];
    let executableLine = 0;
    for (let i = 0; i < lines.length; i++) {
        const raw = lines[i];
        const stripped = raw.trim();
        // stats over the whole file
        if (/;LAYER:/i.test(stripped) || /;LAYER_CHANGE/i.test(stripped)) layers++;
        if (/^G[01]\s/i.test(stripped)) {
            moves++;
            const zMatch = stripped.match(/Z([\-0-9.]+)/i);
            if (zMatch) { const z = parseFloat(zMatch[1]); if (z > maxZ) maxZ = z; }
            const eMatch = stripped.match(/E([\-0-9.]+)/i);
            if (eMatch) { extrusions++; filamentMm = Math.max(filamentMm, parseFloat(eMatch[1]) || 0); }
        }

        if (stripped && !stripped.startsWith(';')) executableLine++;
        if (i < limit) {
            const highlighted = highlightGcodeLine(raw);
            out.push(
                stripped && !stripped.startsWith(';')
                    ? `<span class="gcode-line" data-exec-line="${executableLine}">${highlighted}</span>`
                    : highlighted
            );
        }
    }

    const stats = document.getElementById('gcode-stats');
    stats.innerHTML =
        `<span><strong>${lines.length.toLocaleString()}</strong> lines</span>` +
        `<span><strong>${moves.toLocaleString()}</strong> moves</span>` +
        `<span><strong>${extrusions.toLocaleString()}</strong> extrusions</span>` +
        `<span><strong>${layers}</strong> layers</span>` +
        `<span>Max Z: <strong>${maxZ.toFixed(2)} mm</strong></span>` +
        `<span>Filament: <strong>${filamentMm.toFixed(1)} mm</strong></span>`;

    let html = out.join('\n');
    if (lines.length > limit) {
        html += `\n<span class="gcomment">; ... ${lines.length - limit} more lines truncated for display ...</span>`;
    }
    document.getElementById('gcode-content').innerHTML = html;
}

function syncViewerPrintControls() {
    const start = document.getElementById('gcode-start-print');
    const pause = document.getElementById('gcode-pause-print');
    const stop = document.getElementById('gcode-stop-print');
    if (!start || !pause || !stop) return;
    const active = currentPrinterState === 'printing' || currentPrinterState === 'paused';
    start.style.display = viewedGcodeFile && !active ? 'inline-block' : 'none';
    pause.style.display = active ? 'inline-block' : 'none';
    stop.style.display = active ? 'inline-block' : 'none';
    pause.textContent = currentPrinterState === 'paused' ? 'Resume' : 'Pause';
}

function highlightExecutingGcodeLine(currentLine) {
    if (!currentLine) return;
    const line = document.querySelector(`[data-exec-line="${currentLine}"]`);
    if (!line || line.classList.contains('gcode-line-active')) return;
    document.querySelectorAll('.gcode-line-active').forEach(
        el => el.classList.remove('gcode-line-active')
    );
    line.classList.add('gcode-line-active');
    line.scrollIntoView({block: 'center', behavior: 'smooth'});
}

function highlightGcodeLine(line) {
    const escaped = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    if (/^\s*;/.test(escaped)) return `<span class="gcomment">${escaped}</span>`;
    if (/^\s*M(104|109|140|190|105|106|107)\b/i.test(escaped)) return `<span class="gtemp">${escaped}</span>`;
    if (/^\s*G(0|1|2|3|28|29)\b/i.test(escaped)) {
        if (/;LAYER/i.test(escaped)) return `<span class="glayer">${escaped}</span>`;
        return `<span class="gmove">${escaped}</span>`;
    }
    return escaped;
}

async function printFile(name) {
    lastLoggedPrintLine = -1;
    lastLoggedPrintFile = name;
    lastDisplayedLine = '';
    lastDisplayedCommand = '';
    await fetch('/api/files/local/' + encodeURIComponent(name), {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({command: 'select', print: true})
    });
    termLog('Starting print: ' + name, 'cmd');
    toast('Starting print: ' + name, 'success', 2000);
}

async function printViewedFile() {
    if (!viewedGcodeFile) return;
    await printFile(viewedGcodeFile);
}

async function deleteFile(name) {
    if (!confirm(`Delete ${name}?`)) return;
    await fetch('/api/files/local/' + encodeURIComponent(name), {
        method: 'DELETE',
        headers: getHeaders()
    });
    refreshFiles();
    toast('Deleted ' + name, 'info', 1600);
}

async function uploadFile(input) {
    const file = input.files[0];
    if (!file) return;
    const lowerName = file.name.toLowerCase();
    const isAllowed = ALLOWED_UPLOAD_EXTENSIONS.some(ext => lowerName.endsWith(ext));
    if (!isAllowed) {
        const allowed = ALLOWED_UPLOAD_EXTENSIONS.join(', ');
        const message = `Unsupported file type. Upload ${allowed} files only.`;
        termLog('Upload rejected: ' + file.name + ' (' + allowed + ' only)', 'err');
        toast(message, 'warning', 3200);
        input.value = '';
        return;
    }

    const formData = new FormData();
    formData.append('file', file, file.name);
    formData.append('select', 'false');
    formData.append('print', 'false');

    toast('Uploading ' + file.name + '...', 'info', 1400);
    try {
        const resp = await fetch('/api/files/local', {
            method: 'POST',
            headers: apiKey ? {'X-Api-Key': apiKey} : {},
            body: formData
        });
        if (resp.ok) {
            termLog('Uploaded: ' + file.name, 'resp');
            refreshFiles();
            toast('Uploaded ' + file.name, 'success', 1800);
        } else {
            let message = 'Upload failed';
            try {
                const data = await resp.json();
                if (data.error) message = data.error;
            } catch(e) {}
            termLog(message, 'err');
            toast(message, 'error', 2600);
        }
    } catch(e) {
        termLog('Upload failed: ' + e.message, 'err');
        toast('Upload failed: ' + e.message, 'error', 2400);
    }
    input.value = '';
}

// === Helpers ===
function formatTime(seconds) {
    if (!seconds || seconds <= 0) return '-';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
}

function formatBytes(bytes) {
    if (!bytes) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}
