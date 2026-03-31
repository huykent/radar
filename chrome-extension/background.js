// ═══════════════════════════════════════════════════════════════
// 📡 Livestream Radar — Background Service Worker
// WebSocket connection runs HERE to bypass mixed content.
// Content script sends comments via chrome.runtime.sendMessage.
// ═══════════════════════════════════════════════════════════════

const DEFAULTS = {
    enabled: true,
    showBadges: true,
    autoLoad: false,
    junkFilter: true,
    serverUrl: 'http://localhost:8000',
    radarApiKey: '',
};

let ws = null;
let wsConnected = false;
let reconnectDelay = 2000;
let reconnectTimer = null;
let serverUrl = DEFAULTS.serverUrl;
let radarApiKey = '';
let radarEnabled = true;
let sentCount = 0;
const commentQueue = [];

// ── Load settings and connect ──────────────────────────────
function init() {
    chrome.storage.local.get(DEFAULTS, (data) => {
        serverUrl = data.serverUrl || DEFAULTS.serverUrl;
        radarApiKey = data.radarApiKey || '';
        radarEnabled = data.enabled;
        if (radarEnabled) {
            connectWS();
        }
        console.log('[Radar BG] Initialized, server:', serverUrl);
    });
}

// ── WebSocket ──────────────────────────────────────────────
function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    const wsBase = serverUrl.replace(/^http/, 'ws').replace(/\/+$/, '');
    const wsUrl = `${wsBase}/ws/radar${radarApiKey ? '?api_key=' + encodeURIComponent(radarApiKey) : ''}`;

    console.log('[Radar BG] Connecting to', wsUrl);

    try {
        ws = new WebSocket(wsUrl);
    } catch (e) {
        console.warn('[Radar BG] WS create failed:', e);
        scheduleReconnect();
        return;
    }

    ws.onopen = () => {
        console.log('[Radar BG] ✅ Connected');
        wsConnected = true;
        reconnectDelay = 2000;

        // Flush queued comments
        while (commentQueue.length > 0) {
            ws.send(JSON.stringify(commentQueue.shift()));
        }

        // Notify all tabs
        broadcastToTabs({ action: 'ws_status', connected: true });
    };

    ws.onmessage = (evt) => {
        try {
            const msg = JSON.parse(evt.data);
            // Forward augmented comments to all Facebook tabs
            broadcastToTabs(msg);
        } catch (e) {
            console.warn('[Radar BG] Parse error:', e);
        }
    };

    ws.onclose = () => {
        console.warn('[Radar BG] WebSocket closed');
        wsConnected = false;
        ws = null;
        broadcastToTabs({ action: 'ws_status', connected: false });
        if (radarEnabled) scheduleReconnect();
    };

    ws.onerror = () => {
        ws.close();
    };
}

function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWS();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
}

function disconnectWS() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    if (ws) {
        ws.close();
        ws = null;
    }
    wsConnected = false;
}

// ── Broadcast to all Facebook tabs ──────────────────────────
function broadcastToTabs(msg) {
    chrome.tabs.query({ url: ['https://www.facebook.com/*', 'https://web.facebook.com/*'] }, (tabs) => {
        for (const tab of tabs) {
            chrome.tabs.sendMessage(tab.id, msg).catch(() => { });
        }
    });
}

// ── Message handler ─────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    // Content script sends a comment
    if (msg.action === 'send_comment') {
        const payload = {
            action: 'new_comment',
            fb_name: msg.fb_name,
            text: msg.text,
            fb_uid: msg.fb_uid || null,
            post_id: msg.post_id || null,
        };
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(payload));
        } else {
            if (commentQueue.length < 500) {
                commentQueue.push(payload);
            }
        }
        sentCount++;
        sendResponse({ ok: true, sentCount, wsConnected });
        return true;
    }

    // Popup or content script asks for status
    if (msg.action === 'get_ws_status') {
        sendResponse({ wsConnected, sentCount, enabled: radarEnabled, serverUrl });
        return true;
    }

    // Toggle on/off
    if (msg.action === 'toggle') {
        radarEnabled = !!msg.enabled;
        chrome.storage.local.set({ enabled: radarEnabled });
        if (radarEnabled) {
            connectWS();
        } else {
            disconnectWS();
        }
        sendResponse({ ok: true, enabled: radarEnabled });
        return true;
    }

    // Settings updated from popup
    if (msg.action === 'settings_updated') {
        const s = msg.settings || {};
        const urlChanged = s.serverUrl && s.serverUrl !== serverUrl;
        const keyChanged = s.radarApiKey !== undefined && s.radarApiKey !== radarApiKey;

        if (s.serverUrl) serverUrl = s.serverUrl;
        if (s.radarApiKey !== undefined) radarApiKey = s.radarApiKey;
        if (typeof s.enabled === 'boolean') radarEnabled = s.enabled;

        // Reconnect if URL or key changed
        if (urlChanged || keyChanged) {
            disconnectWS();
            if (radarEnabled) connectWS();
        }

        // Forward local prefs to content scripts
        broadcastToTabs({ action: 'prefs_updated', settings: s });
        sendResponse({ ok: true });
        return true;
    }

    // Ping
    if (msg.action === 'ping') {
        sendResponse({ status: 'active', wsConnected, sentCount });
        return true;
    }
});

// ── Storage change listener ──────────────────────────────────
chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.serverUrl) serverUrl = changes.serverUrl.newValue || DEFAULTS.serverUrl;
    if (changes.radarApiKey) radarApiKey = changes.radarApiKey.newValue || '';
    if (changes.enabled) {
        radarEnabled = changes.enabled.newValue;
        if (radarEnabled && !wsConnected) connectWS();
        if (!radarEnabled) disconnectWS();
    }
});

// ── Lifecycle ────────────────────────────────────────────────
chrome.runtime.onInstalled.addListener((details) => {
    console.log('[Radar BG] Extension installed:', details.reason);
    if (details.reason === 'install') {
        chrome.storage.local.set(DEFAULTS);
    }
});

// Init on service worker start
init();
