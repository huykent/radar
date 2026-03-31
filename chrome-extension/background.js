// ═══════════════════════════════════════════════════════════════
// 📡 Livestream Radar — Background Service Worker
// Handles lifecycle events and settings sync between popup ↔ backend
// ═══════════════════════════════════════════════════════════════

// Note: popup.html handles the icon click now (default_popup in manifest).
// No onClicked listener needed.

// Log installation
chrome.runtime.onInstalled.addListener((details) => {
    console.log('[Radar BG] Extension installed:', details.reason);

    // Set default settings on first install
    if (details.reason === 'install') {
        chrome.storage.local.set({
            enabled: true,
            showBadges: true,
            autoLoad: false,
            junkFilter: true,
            serverUrl: 'http://localhost:8000',
            radarApiKey: '',
            posShopId: '',
            posApiKey: '',
            chatPageId: '',
            chatToken: '',
        });
    }
});
