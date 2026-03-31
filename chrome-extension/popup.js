// ═══════════════════════════════════════════════════════════════
// 📡 Livestream Radar — Popup Script
// Settings are loaded FROM and saved TO the backend server.
// Only toggle state + local prefs are stored in chrome.storage.
// ═══════════════════════════════════════════════════════════════

const LOCAL_DEFAULTS = {
    enabled: true,
    showBadges: true,
    autoLoad: false,
    junkFilter: true,
    serverUrl: 'http://localhost:8000',
    radarApiKey: '',
};

// DOM elements
const $toggle = document.getElementById('toggle-enabled');
const $toggleLabel = document.getElementById('toggle-label');
const $statusDot = document.getElementById('status-dot');
const $statusText = document.getElementById('status-text');
const $optBadges = document.getElementById('opt-badges');
const $optAutoload = document.getElementById('opt-autoload');
const $optJunkfilter = document.getElementById('opt-junkfilter');
const $serverUrl = document.getElementById('cfg-server-url');
const $radarApiKey = document.getElementById('cfg-radar-api-key');
const $posShopId = document.getElementById('cfg-pos-shop-id');
const $posApiKey = document.getElementById('cfg-pos-api-key');
const $chatPageId = document.getElementById('cfg-chat-page-id');
const $chatToken = document.getElementById('cfg-chat-token');
const $btnSave = document.getElementById('btn-save');
const $btnSync = document.getElementById('btn-sync');
const $toast = document.getElementById('toast');
const $footerPostId = document.getElementById('footer-post-id');
const $dashLink = document.getElementById('footer-dashboard-link');

// ── Load settings ───────────────────────────────────────────
async function loadSettings() {
    // 1) Load local prefs from chrome.storage
    chrome.storage.local.get(LOCAL_DEFAULTS, async (local) => {
        $toggle.checked = local.enabled;
        updateToggleLabel(local.enabled);
        $optBadges.checked = local.showBadges;
        $optAutoload.checked = local.autoLoad;
        $optJunkfilter.checked = local.junkFilter;
        $serverUrl.value = local.serverUrl;
        $radarApiKey.value = local.radarApiKey;

        // Update dashboard link
        $dashLink.href = local.serverUrl || LOCAL_DEFAULTS.serverUrl;

        // 2) Load API settings from the backend server
        await loadServerSettings(local.serverUrl, local.radarApiKey);
    });
}

async function loadServerSettings(baseUrl, apiKey) {
    try {
        const base = (baseUrl || LOCAL_DEFAULTS.serverUrl).replace(/\/+$/, '');
        const headers = {};
        if (apiKey) headers['X-API-Key'] = apiKey;

        const resp = await fetch(`${base}/api/settings`, { headers });
        if (resp.status === 401) {
            $statusText.textContent = '🔑 API Key sai — kiểm tra lại';
            $statusDot.className = 'status-dot disconnected';
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        $posShopId.value = data.pancake_shop_id || '';
        $chatPageId.value = data.pancake_chat_page_id || '';

        // Show masked values as placeholders
        if (data.pancake_api_key_set) {
            $posApiKey.placeholder = data.pancake_api_key_masked || '••••••••';
            $posApiKey.value = '';
        }
        if (data.pancake_chat_token_set) {
            $chatToken.placeholder = data.pancake_chat_token_masked || '••••••••';
            $chatToken.value = '';
        }

        $statusText.textContent = '✅ Đã tải settings từ server';
        $statusDot.className = 'status-dot connected';
    } catch (e) {
        console.warn('[Popup] Cannot load from server:', e.message);
        $statusText.textContent = '⚠️ Không kết nối được server';
    }
}

function updateToggleLabel(on) {
    $toggleLabel.textContent = on ? 'BẬT' : 'TẮT';
    $toggleLabel.className = 'toggle-label ' + (on ? 'on' : 'off');
}

// ── Save settings ───────────────────────────────────────────
async function saveSettings() {
    // 1) Save local prefs to chrome.storage
    const localPrefs = {
        enabled: $toggle.checked,
        showBadges: $optBadges.checked,
        autoLoad: $optAutoload.checked,
        junkFilter: $optJunkfilter.checked,
        serverUrl: $serverUrl.value.trim().replace(/\/+$/, '') || LOCAL_DEFAULTS.serverUrl,
        radarApiKey: $radarApiKey.value.trim(),
    };
    chrome.storage.local.set(localPrefs);

    // Update dashboard link
    $dashLink.href = localPrefs.serverUrl;

    // 2) Push API settings to backend server
    const base = localPrefs.serverUrl;
    const payload = {};
    if ($posShopId.value.trim()) payload.pancake_shop_id = $posShopId.value.trim();
    if ($posApiKey.value.trim()) payload.pancake_api_key = $posApiKey.value.trim();
    if ($chatPageId.value.trim()) payload.pancake_chat_page_id = $chatPageId.value.trim();
    if ($chatToken.value.trim()) payload.pancake_chat_token = $chatToken.value.trim();

    const headers = { 'Content-Type': 'application/json' };
    if (localPrefs.radarApiKey) headers['X-API-Key'] = localPrefs.radarApiKey;

    try {
        const resp = await fetch(`${base}/api/settings`, {
            method: 'POST',
            headers,
            body: JSON.stringify(payload),
        });
        if (resp.status === 401) {
            showToast('🔑 API Key sai!');
            return;
        }
        if (resp.ok) {
            const result = await resp.json();
            showToast(`✅ Đã lưu: ${(result.saved_keys || []).join(', ')}`);
        } else {
            showToast('⚠️ Server lỗi ' + resp.status);
        }
    } catch (e) {
        showToast('❌ Không kết nối được server');
    }

    // 3) Notify content script
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0]) {
            chrome.tabs.sendMessage(tabs[0].id, {
                action: 'settings_updated',
                settings: localPrefs,
            });
        }
    });
}

// ── Trigger sync ────────────────────────────────────────────
async function triggerSync() {
    const base = ($serverUrl.value.trim() || LOCAL_DEFAULTS.serverUrl).replace(/\/+$/, '');
    const apiKey = $radarApiKey.value.trim();
    $btnSync.textContent = '⏳ Đang sync…';
    $btnSync.disabled = true;

    const headers = {};
    if (apiKey) headers['X-API-Key'] = apiKey;

    try {
        const resp = await fetch(`${base}/api/settings/sync-now`, {
            method: 'POST',
            headers,
        });
        if (resp.ok) {
            showToast('🔄 Đã kích hoạt sync!');
        } else {
            showToast('⚠️ Sync thất bại');
        }
    } catch (e) {
        showToast('❌ Không kết nối được server');
    } finally {
        $btnSync.textContent = '🔄 Sync ngay';
        $btnSync.disabled = false;
    }
}

// ── Check connection status ─────────────────────────────────
function checkStatus() {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0] && tabs[0].url && tabs[0].url.includes('facebook.com')) {
            chrome.tabs.sendMessage(tabs[0].id, { action: 'get_status' }, (response) => {
                if (chrome.runtime.lastError || !response) {
                    // Don't overwrite server status message
                } else {
                    if (response.wsConnected) {
                        $statusDot.className = 'status-dot connected';
                        $statusText.textContent = `Đang chạy — ${response.commentCount || 0} comments`;
                    } else {
                        $statusDot.className = 'status-dot disconnected';
                        $statusText.textContent = response.enabled === false ? 'Đã tắt' : 'Đang kết nối…';
                    }
                    if (response.postId) {
                        $footerPostId.textContent = response.postId;
                    }
                }
            });
        }
    });
}

// ── Toast ───────────────────────────────────────────────────
function showToast(msg) {
    $toast.textContent = msg;
    $toast.classList.add('show');
    setTimeout(() => $toast.classList.remove('show'), 2500);
}

// ── Event listeners ─────────────────────────────────────────
$toggle.addEventListener('change', () => {
    const on = $toggle.checked;
    updateToggleLabel(on);
    chrome.storage.local.set({ enabled: on });

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0]) {
            chrome.tabs.sendMessage(tabs[0].id, {
                action: 'toggle',
                enabled: on,
            });
        }
    });
});

$btnSave.addEventListener('click', saveSettings);
$btnSync.addEventListener('click', triggerSync);

// ── Init ────────────────────────────────────────────────────
loadSettings();
setTimeout(checkStatus, 1000);
setInterval(checkStatus, 5000);
