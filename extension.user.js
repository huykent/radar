// ==UserScript==
// @name         📡 Livestream Radar — FB Live Injector
// @namespace    https://livestream-radar.local
// @version      2.0.0
// @description  Captures Facebook Live comments, sends to Radar backend via WebSocket, and injects tier badges + quick-reply buttons.
// @author       Livestream Radar
// @match        https://www.facebook.com/*
// @match        https://web.facebook.com/*
// @connect      localhost
// @connect      127.0.0.1
// @connect      *
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // ═══════════════════════════════════════════════════════════════
    // CONFIG — Change LOCAL_IP to your server's LAN IP
    // ═══════════════════════════════════════════════════════════════
    const WS_URL = 'ws://localhost:8000/ws/radar';  // ← update if needed
    const RECONNECT_BASE_MS = 2000;
    const RECONNECT_MAX_MS = 30000;
    const PROCESSED_ATTR = 'data-radar-processed';
    const DEBUG = true; // Set false to silence console logs

    function log(...args) { if (DEBUG) console.log('[Radar]', ...args); }
    function warn(...args) { if (DEBUG) console.warn('[Radar]', ...args); }

    // ═══════════════════════════════════════════════════════════════
    // INJECT STYLESHEET
    // ═══════════════════════════════════════════════════════════════
    GM_addStyle(`
    /* ── Tier badges ─────────────────────────────────── */
    .radar-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 10px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
      margin-left: 6px;
      vertical-align: middle;
      line-height: 1;
      animation: radar-badge-in 0.35s ease-out;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    @keyframes radar-badge-in {
      from { opacity: 0; transform: scale(0.7) translateY(-2px); }
      to   { opacity: 1; transform: scale(1) translateY(0); }
    }

    .radar-badge--vip   { background: rgba(250,204,21,0.2); color: #facc15; box-shadow: 0 0 12px rgba(250,204,21,0.4); }
    .radar-badge--quen  { background: rgba(16,185,129,0.15); color: #34d399; }
    .radar-badge--moi   { background: rgba(148,163,184,0.12); color: #94a3b8; }
    .radar-badge--dao   { background: rgba(249,115,22,0.15); color: #fb923c; }
    .radar-badge--bom   { background: rgba(239,68,68,0.15); color: #ef4444; text-decoration: line-through; }

    /* ── Quick-reply buttons ─────────────────────────── */
    .radar-btn {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      font-size: 11px;
      font-weight: 600;
      padding: 3px 10px;
      border-radius: 6px;
      margin-left: 4px;
      cursor: pointer;
      border: none;
      transition: transform 0.15s ease, box-shadow 0.15s ease;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    .radar-btn:hover {
      transform: scale(1.05);
      box-shadow: 0 2px 12px rgba(0,0,0,0.3);
    }
    .radar-btn--chot  { background: #22c55e; color: #fff; }
    .radar-btn--het   { background: #ef4444; color: #fff; }

    /* ── Dimmed BOM comment ──────────────────────────── */
    .radar-dimmed {
      pointer-events: none !important;
      opacity: 0.35 !important;
      filter: grayscale(1) !important;
      transition: opacity 0.4s ease, filter 0.4s ease;
    }

    /* ── Spent info tooltip ──────────────────────────── */
    .radar-spent {
      display: inline-block;
      font-size: 10px;
      color: #94a3b8;
      margin-left: 6px;
      font-family: 'Courier New', monospace;
    }

    /* ── Radar controls bar ──────────────────────────── */
    .radar-status-bar {
      position: fixed;
      bottom: 8px;
      right: 8px;
      z-index: 99999;
      background: rgba(15, 23, 42, 0.9);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 8px;
      padding: 4px 10px;
      font-size: 11px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      color: #94a3b8;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .radar-status-dot {
      width: 6px; height: 6px;
      border-radius: 50%;
      display: inline-block;
    }
    .radar-status-dot--on  { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
    .radar-status-dot--off { background: #ef4444; }
  `);

    // ═══════════════════════════════════════════════════════════════
    // STATUS BAR (visible indicator on page)
    // ═══════════════════════════════════════════════════════════════
    const statusBar = document.createElement('div');
    statusBar.className = 'radar-status-bar';
    statusBar.innerHTML = `<span class="radar-status-dot radar-status-dot--off"></span> 📡 Radar: connecting…`;
    document.body.appendChild(statusBar);

    function updateStatus(connected, commentCount) {
        const dot = statusBar.querySelector('.radar-status-dot');
        dot.className = `radar-status-dot ${connected ? 'radar-status-dot--on' : 'radar-status-dot--off'}`;
        statusBar.innerHTML = '';
        statusBar.appendChild(dot);
        statusBar.append(connected
            ? ` 📡 Radar: connected (${commentCount} sent)`
            : ` 📡 Radar: disconnected`);
    }

    // ═══════════════════════════════════════════════════════════════
    // STATE
    // ═══════════════════════════════════════════════════════════════
    let ws = null;
    let reconnectDelay = RECONNECT_BASE_MS;
    const commentQueue = [];        // buffer outgoing comments (max 200)
    let pendingProfiles = new Map(); // fb_name → profile data from server
    let sentCount = 0;
    let wsConnected = false;

    // ═══════════════════════════════════════════════════════════════
    // WEBSOCKET
    // ═══════════════════════════════════════════════════════════════
    function connectWS() {
        try {
            ws = new WebSocket(WS_URL);
        } catch (e) {
            warn('WS create failed:', e);
            scheduleReconnect();
            return;
        }

        ws.onopen = () => {
            log('✅ Connected to', WS_URL);
            reconnectDelay = RECONNECT_BASE_MS;
            wsConnected = true;
            updateStatus(true, sentCount);
            // Flush queued comments
            while (commentQueue.length) {
                ws.send(JSON.stringify(commentQueue.shift()));
            }
        };

        ws.onmessage = (evt) => {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.action === 'comment_augmented' || msg.tier_tag) {
                    handleProfile(msg);
                }
            } catch (e) {
                warn('Parse error:', e);
            }
        };

        ws.onclose = () => {
            warn('WebSocket closed. Reconnecting…');
            wsConnected = false;
            updateStatus(false, sentCount);
            scheduleReconnect();
        };

        ws.onerror = () => ws.close();
    }

    function scheduleReconnect() {
        setTimeout(connectWS, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, RECONNECT_MAX_MS);
    }

    function sendComment(fbName, text) {
        const payload = { action: 'new_comment', fb_name: fbName, text: text };
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(payload));
        } else {
            // Cap queue at 200
            if (commentQueue.length < 200) {
                commentQueue.push(payload);
            }
        }
        sentCount++;
        if (wsConnected) updateStatus(true, sentCount);
        log(`💬 Comment captured: [${fbName}] ${text.substring(0, 60)}…`);
    }

    // ═══════════════════════════════════════════════════════════════
    // PROFILE INJECTION
    // ═══════════════════════════════════════════════════════════════
    function handleProfile(msg) {
        // Store for retroactive injection
        if (msg.fb_name) {
            pendingProfiles.set(msg.fb_name, msg);
        }
        // Try immediate injection
        injectAllPending();
    }

    function getTierBadgeClass(tierTag) {
        if (!tierTag) return 'radar-badge--moi';
        if (tierTag.includes('VIP')) return 'radar-badge--vip';
        if (tierTag.includes('QUEN')) return 'radar-badge--quen';
        if (tierTag.includes('DẠO')) return 'radar-badge--dao';
        if (tierTag.includes('BOM HÀNG')) return 'radar-badge--bom';
        return 'radar-badge--moi';
    }

    function formatSpent(v) {
        if (!v || v === 0) return '';
        return new Intl.NumberFormat('vi-VN').format(v) + ' ₫';
    }

    function injectBadge(nameEl, commentContainer, profile) {
        if (!nameEl || !commentContainer) return;
        if (commentContainer.hasAttribute(PROCESSED_ATTR)) return;
        commentContainer.setAttribute(PROCESSED_ATTR, '1');

        // --- Badge ---
        const badge = document.createElement('span');
        badge.className = `radar-badge ${getTierBadgeClass(profile.tier_tag)}`;
        badge.textContent = profile.tier_tag || '⚪ KHÁCH MỚI';

        // Insert badge after the name element
        const insertTarget = nameEl.parentElement || nameEl;
        insertTarget.appendChild(badge);

        // --- Spent info ---
        if (profile.total_spent && profile.total_spent > 0) {
            const sp = document.createElement('span');
            sp.className = 'radar-spent';
            sp.textContent = formatSpent(profile.total_spent);
            insertTarget.appendChild(sp);
        }

        // --- Quick-reply buttons ---
        const btnWrap = document.createElement('span');
        btnWrap.style.marginLeft = '8px';

        const btnChot = document.createElement('button');
        btnChot.className = 'radar-btn radar-btn--chot';
        btnChot.innerHTML = '⚡ Chốt';
        btnChot.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            log('Chốt đơn for', profile.fb_name);
            btnChot.textContent = '✅ Đã chốt';
            btnChot.style.opacity = '0.6';
            btnChot.disabled = true;
        });

        const btnHet = document.createElement('button');
        btnHet.className = 'radar-btn radar-btn--het';
        btnHet.innerHTML = '❌ Hết';
        btnHet.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            log('Hết hàng for', profile.fb_name);
            btnHet.textContent = '🚫 Đã báo';
            btnHet.style.opacity = '0.6';
            btnHet.disabled = true;
        });

        btnWrap.appendChild(btnChot);
        btnWrap.appendChild(btnHet);
        insertTarget.appendChild(btnWrap);

        // --- Auto-dim BOM HÀNG ---
        if (profile.tier_tag && profile.tier_tag.includes('BOM HÀNG')) {
            commentContainer.classList.add('radar-dimmed');
        }

        log(`🏷️ Injected badge for [${profile.fb_name}]: ${profile.tier_tag}`);
    }

    function injectAllPending() {
        if (pendingProfiles.size === 0) return;

        // Strategy: find all name-like links on the page and match against pending profiles
        // Facebook uses many different selectors, so cast a wide net
        const nameSelectors = [
            // Live chat / replay comments — name links
            'a[role="link"] span',
            // Article-based comments
            '[role="article"] a[role="link"] span',
            // Generic comment author links
            'a[href*="/user/"] span',
            'a[href*="facebook.com/"] span',
        ];

        const allNameEls = document.querySelectorAll(nameSelectors.join(', '));

        allNameEls.forEach((el) => {
            const name = el.textContent?.trim();
            if (!name || name.length < 2 || name.length > 50) return;

            const profile = pendingProfiles.get(name);
            if (!profile) return;

            // Walk up to find the comment container
            const container =
                el.closest('[role="article"]') ||
                el.closest('[data-testid]') ||
                el.closest('li') ||
                el.closest('[class*="x1n2onr6"]') || // common FB wrapper class
                el.parentElement?.parentElement?.parentElement;

            if (!container || container.hasAttribute(PROCESSED_ATTR)) return;

            injectBadge(el, container, profile);
        });
    }

    // ═══════════════════════════════════════════════════════════════
    // COMMENT DETECTION — Multiple strategies for FB Live
    // ═══════════════════════════════════════════════════════════════

    /**
     * Try to extract name + text from a DOM node that was just added.
     * Facebook uses MANY different DOM shapes depending on:
     *   - Live (theatre mode) vs replay vs regular post
     *   - Logged in vs logged out
     *   - Desktop vs mobile web
     * We use multiple strategies and take the first hit.
     */
    function tryExtractComment(node) {
        if (node.nodeType !== Node.ELEMENT_NODE) return null;
        if (node.hasAttribute(PROCESSED_ATTR)) return null;

        // ── Strategy 1: [role="article"] (classic FB comment) ──
        const articles = node.matches?.('[role="article"]')
            ? [node]
            : Array.from(node.querySelectorAll?.('[role="article"]') || []);

        for (const article of articles) {
            const result = extractFromContainer(article);
            if (result) return result;
        }

        // ── Strategy 2: List items containing links (Live replay) ──
        const listItems = node.matches?.('li') ? [node] : Array.from(node.querySelectorAll?.('li') || []);
        for (const li of listItems) {
            const result = extractFromContainer(li);
            if (result) return result;
        }

        // ── Strategy 3: Divs with comment-like structure ──
        // Look for any div that contains a user link + text with dir="auto"
        const divs = node.matches?.('div') ? [node] : [];
        if (divs.length === 0 && node.querySelectorAll) {
            // Only go 2 levels deep to avoid performance issues
            node.querySelectorAll(':scope > div > div, :scope > div').forEach(d => divs.push(d));
        }

        for (const div of divs) {
            if (div.hasAttribute(PROCESSED_ATTR)) continue;
            const result = extractFromContainer(div);
            if (result) return result;
        }

        return null;
    }

    /**
     * Given a container element, try to extract fbName and text.
     */
    function extractFromContainer(container) {
        if (!container || container.hasAttribute(PROCESSED_ATTR)) return null;

        // Find name: look for a link with text
        const nameEl =
            container.querySelector('a[role="link"] span span') ||
            container.querySelector('a[role="link"] > span') ||
            container.querySelector('a[role="link"]') ||
            container.querySelector('a[href*="/user/"]') ||
            container.querySelector('a[href*="facebook.com/profile"]') ||
            container.querySelector('h3 a') ||
            container.querySelector('a > strong') ||
            container.querySelector('a > span > strong');

        if (!nameEl) return null;

        const fbName = nameEl.textContent?.trim();
        if (!fbName || fbName.length < 2) return null;

        // Find text: look for content with dir="auto" or general text containers
        const textEl =
            container.querySelector('[dir="auto"]:not(a [dir="auto"])') ||
            container.querySelector('[data-ad-preview="message"]') ||
            container.querySelector('span[dir="auto"]');

        // Fallback: get all text that isn't the name
        let text = '';
        if (textEl) {
            text = textEl.textContent?.trim() || '';
        } else {
            // Get full text minus the name
            const fullText = container.textContent || '';
            text = fullText.replace(fbName, '').trim();
            // Clean up — remove common button text
            text = text.replace(/Thích|Trả lời|Xem thêm|Gởi tin nhắn|Ẩn|See more|Like|Reply/g, '').trim();
        }

        if (!text || text.length < 1) return null;

        return { fbName, text, container, nameEl };
    }

    // ═══════════════════════════════════════════════════════════════
    // INITIAL SCAN: Capture existing comments on page load
    // ═══════════════════════════════════════════════════════════════
    function scanExistingComments() {
        log('🔍 Scanning existing comments on page…');
        let found = 0;

        // Try all known comment container selectors
        const containers = document.querySelectorAll(
            '[role="article"], ' +
            'ul > li, ' +                    // list-based comments
            '[data-testid*="UFI2Comment"]'    // older FB comment testid
        );

        containers.forEach(container => {
            if (container.hasAttribute(PROCESSED_ATTR)) return;
            const result = extractFromContainer(container);
            if (result) {
                container.setAttribute(PROCESSED_ATTR, '1');
                sendComment(result.fbName, result.text);
                found++;
            }
        });

        log(`🔍 Found ${found} existing comments`);
    }

    // ═══════════════════════════════════════════════════════════════
    // MUTATION OBSERVER — Capture new comments
    // ═══════════════════════════════════════════════════════════════
    function startObserver() {
        const observer = new MutationObserver((mutations) => {
            let newCommentsFound = 0;

            for (const mutation of mutations) {
                for (const node of mutation.addedNodes) {
                    if (node.nodeType !== Node.ELEMENT_NODE) continue;

                    const result = tryExtractComment(node);
                    if (result) {
                        result.container.setAttribute(PROCESSED_ATTR, '1');
                        sendComment(result.fbName, result.text);
                        newCommentsFound++;
                    }
                }
            }

            // Also try inject pending profiles on any DOM change
            if (pendingProfiles.size > 0) {
                injectAllPending();
            }
        });

        // Observe the entire body for changes (FB is heavily SPA)
        observer.observe(document.body, { childList: true, subtree: true });
        log('👀 MutationObserver started — watching for new comments');
    }

    // ═══════════════════════════════════════════════════════════════
    // PERIODIC RE-SCAN (catches comments the observer might miss)
    // ═══════════════════════════════════════════════════════════════
    function startPeriodicScan() {
        setInterval(() => {
            // Re-inject pending profiles
            if (pendingProfiles.size > 0) {
                injectAllPending();
            }
        }, 3000); // Every 3 seconds
    }

    // ═══════════════════════════════════════════════════════════════
    // INIT
    // ═══════════════════════════════════════════════════════════════
    log('📡 Livestream Radar v2.0 loaded');
    connectWS();

    // Wait for FB to render initial content, then scan + observe
    setTimeout(() => {
        scanExistingComments();
        startObserver();
        startPeriodicScan();
    }, 3000);

})();
