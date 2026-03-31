// ═══════════════════════════════════════════════════════════════
// 📡 Livestream Radar — Chrome Extension Content Script
// Captures Facebook Live comments → sends to Radar backend
// via WebSocket → injects tier badges + quick-reply buttons.
// ═══════════════════════════════════════════════════════════════

(function () {
    'use strict';

    // ═══════════════════════════════════════════════════════════════
    // CONFIG (overridden by chrome.storage)
    // ═══════════════════════════════════════════════════════════════
    let WS_URL = 'ws://localhost:8000/ws/radar';
    const RECONNECT_BASE_MS = 2000;
    const RECONNECT_MAX_MS = 30000;
    const PROCESSED_ATTR = 'data-radar-processed';
    const DEBUG = true;

    // ── Runtime state ──────────────────────────────────────────
    let radarEnabled = true;
    let junkFilterEnabled = true;
    let showBadgesEnabled = true;
    let serverUrl = 'http://localhost:8000';
    let radarApiKey = '';

    // ── Junk comment filter ─────────────────────────────────
    const JUNK_NAMES = new Set([
        'quảng cáo', 'điều khoản', 'quyền riêng tư', 'trung tâm quảng cáo',
        'trình quản lý quảng cáo', 'công cụ chuyên nghiệp', 'được tài trợ',
        'facebook', 'meta ai', 'manus ai', 'bảng feed', 'nhóm',
        'marketplace', 'watch', 'gaming', 'messenger', 'instagram',
        'threads', 'thước phim', 'kỷ niệm', 'trang', 'sự kiện',
        'video trên watch', 'reels', 'fundraisers', 'bay thẳng tới',
        'bestseelling', 'bestselling', 'see more', 'xem thêm',
    ]);
    const JUNK_PATTERNS = [
        /^.{0,2}$/, // too short
        /^(like|thích|trả lời|reply|share|chia sẻ|see more|xem thêm)$/i,
        /quảng cáo/i, /điều khoản/i, /quyền riêng tư/i,
        /trung tâm/i, /trình quản lý/i, /công cụ chuyên nghiệp/i,
        /được tài trợ/i, /^meta ai$/i, /^manus ai$/i,
        /^facebook$/i, /^bảng feed$/i, /bay thẳng tới/i,
    ];

    function isJunkComment(name, text) {
        if (!junkFilterEnabled) return false;
        const ln = (name || '').toLowerCase().trim();
        if (JUNK_NAMES.has(ln)) return true;
        for (const re of JUNK_PATTERNS) {
            if (re.test(ln)) return true;
        }
        if (text && name && text.trim().toLowerCase() === ln) return true;
        return false;
    }

    function log(...args) { if (DEBUG) console.log('[Radar]', ...args); }
    function warn(...args) { if (DEBUG) console.warn('[Radar]', ...args); }

    // ── Extract Facebook Post ID from URL ────────────────────
    function extractPostId() {
        const url = window.location.href;
        // Patterns: /videos/123456, /posts/123456, /permalink/123456, ?v=123456
        const patterns = [
            /\/videos\/(\d+)/,
            /\/posts\/(\d+)/,
            /\/permalink\/(\d+)/,
            /[?&]v=(\d+)/,
            /story_fbid=(\d+)/,
            /\/watch\/.*?(\d{10,})/,
        ];
        for (const re of patterns) {
            const m = url.match(re);
            if (m) return m[1];
        }
        // Fallback: use URL path hash as session ID
        return null;
    }

    const currentPostId = extractPostId();
    log('📌 Post ID:', currentPostId || 'unknown');

    // ═══════════════════════════════════════════════════════════════
    // STATUS BAR
    // ═══════════════════════════════════════════════════════════════
    const statusBar = document.createElement('div');
    statusBar.className = 'radar-status-bar';
    statusBar.innerHTML = `<span class="radar-status-dot radar-status-dot--off"></span> 📡 Radar: connecting…`;
    document.body.appendChild(statusBar);

    function updateStatus(connected, commentCount) {
        const dot = statusBar.querySelector('.radar-status-dot');
        if (!dot) return;
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
    const commentQueue = [];
    let pendingProfiles = new Map();
    let sentCount = 0;
    let wsConnected = false;

    // ═══════════════════════════════════════════════════════════════
    // FB UID EXTRACTION
    // ═══════════════════════════════════════════════════════════════
    /**
     * Extract Facebook User ID from a profile link element.
     * Patterns:
     *   - facebook.com/profile.php?id=100012345678
     *   - facebook.com/100012345678
     *   - facebook.com/user/100012345678
     *   - data-hovercard attribute with id=XXXX
     */
    function extractFbUid(linkEl) {
        if (!linkEl) return null;

        // Try href
        const href = linkEl.getAttribute('href') || linkEl.href || '';

        // Pattern 1: profile.php?id=XXXXX
        const profileMatch = href.match(/profile\.php\?id=(\d+)/);
        if (profileMatch) return profileMatch[1];

        // Pattern 2: /user/XXXXX/ or /XXXXX (numeric only)
        const userMatch = href.match(/facebook\.com\/(?:user\/)?(\d{5,})/);
        if (userMatch) return userMatch[1];

        // Pattern 3: data-hovercard or data-id
        const hovercard = linkEl.getAttribute('data-hovercard') || '';
        const hcMatch = hovercard.match(/id=(\d+)/);
        if (hcMatch) return hcMatch[1];

        const dataId = linkEl.getAttribute('data-id');
        if (dataId && /^\d{5,}$/.test(dataId)) return dataId;

        // Pattern 4: Walk up to find any parent with user ID in data attributes
        const parent = linkEl.closest('[data-actor-id]') || linkEl.closest('[data-uid]');
        if (parent) {
            return parent.getAttribute('data-actor-id') || parent.getAttribute('data-uid');
        }

        return null;
    }

    // ═══════════════════════════════════════════════════════════════
    // WEBSOCKET
    // ═══════════════════════════════════════════════════════════════
    function connectWS() {
        // Derive WS URL from serverUrl
        const wsBase = serverUrl.replace(/^http/, 'ws').replace(/\/+$/, '');
        WS_URL = `${wsBase}/ws/radar${radarApiKey ? '?api_key=' + encodeURIComponent(radarApiKey) : ''}`;

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

    function sendComment(fbName, text, fbUid) {
        if (!radarEnabled) return;
        const payload = {
            action: 'new_comment',
            fb_name: fbName,
            text: text,
            fb_uid: fbUid || null,
            post_id: currentPostId || null,
        };
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(payload));
        } else {
            if (commentQueue.length < 200) {
                commentQueue.push(payload);
            }
        }
        sentCount++;
        if (wsConnected) updateStatus(true, sentCount);
        log(`💬 Comment: [${fbName}] ${text.substring(0, 60)}… (uid: ${fbUid || 'N/A'})`);
    }

    // ═══════════════════════════════════════════════════════════════
    // PROFILE INJECTION
    // ═══════════════════════════════════════════════════════════════
    function handleProfile(msg) {
        if (msg.fb_name) {
            pendingProfiles.set(msg.fb_name, msg);
        }
        if (msg.fb_uid) {
            pendingProfiles.set('uid:' + msg.fb_uid, msg);
        }
        injectAllPending();
    }

    function getTierBadgeClass(tierTag) {
        if (!tierTag) return 'radar-badge--moi';
        if (tierTag.includes('VIP')) return 'radar-badge--vip';
        if (tierTag.includes('QUEN')) return 'radar-badge--quen';
        if (tierTag.includes('DẠO')) return 'radar-badge--dao';
        if (tierTag.includes('BOM')) return 'radar-badge--bom';
        if (tierTag.includes('KHÔNG CỌC')) return 'radar-badge--dao'; // orange warning
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

        const badge = document.createElement('span');
        badge.className = `radar-badge ${getTierBadgeClass(profile.tier_tag)}`;
        badge.textContent = profile.tier_tag || '⚪ KHÁCH MỚI';

        const insertTarget = nameEl.parentElement || nameEl;
        insertTarget.appendChild(badge);

        // Show Pancake tags if available
        if (profile.pancake_tags && profile.pancake_tags.length > 0) {
            profile.pancake_tags.forEach(tag => {
                const tagEl = document.createElement('span');
                tagEl.className = 'radar-badge radar-badge--tag';
                tagEl.textContent = `🏷️ ${tag}`;
                tagEl.style.fontSize = '9px';
                tagEl.style.background = 'rgba(139,92,246,0.15)';
                tagEl.style.color = '#a78bfa';
                insertTarget.appendChild(tagEl);
            });
        }

        if (profile.total_spent && profile.total_spent > 0) {
            const sp = document.createElement('span');
            sp.className = 'radar-spent';
            sp.textContent = formatSpent(profile.total_spent);
            insertTarget.appendChild(sp);
        }

        // Quick-reply buttons
        const btnWrap = document.createElement('span');
        btnWrap.style.marginLeft = '8px';

        const btnChot = document.createElement('button');
        btnChot.className = 'radar-btn radar-btn--chot';
        btnChot.innerHTML = '⚡ Chốt';
        btnChot.addEventListener('click', (e) => {
            e.stopPropagation(); e.preventDefault();
            log('Chốt đơn for', profile.fb_name);
            btnChot.textContent = '✅ Đã chốt';
            btnChot.style.opacity = '0.6';
            btnChot.disabled = true;
        });

        const btnHet = document.createElement('button');
        btnHet.className = 'radar-btn radar-btn--het';
        btnHet.innerHTML = '❌ Hết';
        btnHet.addEventListener('click', (e) => {
            e.stopPropagation(); e.preventDefault();
            log('Hết hàng for', profile.fb_name);
            btnHet.textContent = '🚫 Đã báo';
            btnHet.style.opacity = '0.6';
            btnHet.disabled = true;
        });

        btnWrap.appendChild(btnChot);
        btnWrap.appendChild(btnHet);
        insertTarget.appendChild(btnWrap);

        if (profile.tier_tag && profile.tier_tag.includes('BOM')) {
            commentContainer.classList.add('radar-dimmed');
        }

        log(`🏷️ Injected: [${profile.fb_name}] ${profile.tier_tag} (match: ${profile.match_method || '?'})`);
    }

    function injectAllPending() {
        if (pendingProfiles.size === 0) return;

        const nameSelectors = [
            'a[role="link"] span',
            '[role="article"] a[role="link"] span',
            'a[href*="/user/"] span',
            'a[href*="facebook.com/"] span',
        ];

        const allNameEls = document.querySelectorAll(nameSelectors.join(', '));

        allNameEls.forEach((el) => {
            const name = el.textContent?.trim();
            if (!name || name.length < 2 || name.length > 50) return;

            // Try match by name or by uid
            let profile = pendingProfiles.get(name);

            if (!profile) {
                // Try to extract UID from the link and match by uid
                const link = el.closest('a[role="link"]') || el.closest('a');
                if (link) {
                    const uid = extractFbUid(link);
                    if (uid) {
                        profile = pendingProfiles.get('uid:' + uid);
                    }
                }
            }

            if (!profile) return;

            const container =
                el.closest('[role="article"]') ||
                el.closest('[data-testid]') ||
                el.closest('li') ||
                el.closest('[class*="x1n2onr6"]') ||
                el.parentElement?.parentElement?.parentElement;

            if (!container || container.hasAttribute(PROCESSED_ATTR)) return;

            injectBadge(el, container, profile);
        });
    }

    // ═══════════════════════════════════════════════════════════════
    // COMMENT DETECTION
    // ═══════════════════════════════════════════════════════════════
    function tryExtractComment(node) {
        if (node.nodeType !== Node.ELEMENT_NODE) return null;
        if (node.hasAttribute(PROCESSED_ATTR)) return null;

        const articles = node.matches?.('[role="article"]')
            ? [node]
            : Array.from(node.querySelectorAll?.('[role="article"]') || []);

        for (const article of articles) {
            const result = extractFromContainer(article);
            if (result) return result;
        }

        const listItems = node.matches?.('li') ? [node] : Array.from(node.querySelectorAll?.('li') || []);
        for (const li of listItems) {
            const result = extractFromContainer(li);
            if (result) return result;
        }

        const divs = node.matches?.('div') ? [node] : [];
        if (divs.length === 0 && node.querySelectorAll) {
            node.querySelectorAll(':scope > div > div, :scope > div').forEach(d => divs.push(d));
        }

        for (const div of divs) {
            if (div.hasAttribute(PROCESSED_ATTR)) continue;
            const result = extractFromContainer(div);
            if (result) return result;
        }

        return null;
    }

    function extractFromContainer(container) {
        if (!container || container.hasAttribute(PROCESSED_ATTR)) return null;

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

        // Extract FB UID from the author link
        const authorLink =
            container.querySelector('a[role="link"]') ||
            container.querySelector('a[href*="/user/"]') ||
            container.querySelector('a[href*="facebook.com/profile"]') ||
            container.querySelector('h3 a');
        const fbUid = extractFbUid(authorLink);

        const textEl =
            container.querySelector('[dir="auto"]:not(a [dir="auto"])') ||
            container.querySelector('[data-ad-preview="message"]') ||
            container.querySelector('span[dir="auto"]');

        let text = '';
        if (textEl) {
            text = textEl.textContent?.trim() || '';
        } else {
            const fullText = container.textContent || '';
            text = fullText.replace(fbName, '').trim();
            text = text.replace(/Thích|Trả lời|Xem thêm|Gởi tin nhắn|Ẩn|See more|Like|Reply/g, '').trim();
        }

        if (!text || text.length < 1) return null;

        // Filter out junk (Facebook UI elements)
        if (isJunkComment(fbName, text)) return null;

        return { fbName, text, container, nameEl, fbUid };
    }

    // ═══════════════════════════════════════════════════════════════
    // INITIAL SCAN
    // ═══════════════════════════════════════════════════════════════
    function scanExistingComments() {
        log('🔍 Scanning existing comments on page…');
        let found = 0;

        const containers = document.querySelectorAll(
            '[role="article"], ul > li, [data-testid*="UFI2Comment"]'
        );

        containers.forEach(container => {
            if (container.hasAttribute(PROCESSED_ATTR)) return;
            const result = extractFromContainer(container);
            if (result) {
                container.setAttribute(PROCESSED_ATTR, '1');
                sendComment(result.fbName, result.text, result.fbUid);
                found++;
            }
        });

        log(`🔍 Found ${found} existing comments`);
    }

    // ═══════════════════════════════════════════════════════════════
    // MUTATION OBSERVER
    // ═══════════════════════════════════════════════════════════════
    function startObserver() {
        const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                for (const node of mutation.addedNodes) {
                    if (node.nodeType !== Node.ELEMENT_NODE) continue;

                    const result = tryExtractComment(node);
                    if (result) {
                        result.container.setAttribute(PROCESSED_ATTR, '1');
                        sendComment(result.fbName, result.text, result.fbUid);
                    }
                }
            }

            if (pendingProfiles.size > 0) {
                injectAllPending();
            }
        });

        observer.observe(document.body, { childList: true, subtree: true });
        log('👀 MutationObserver started — watching for new comments');
    }

    // ═══════════════════════════════════════════════════════════════
    // PERIODIC RE-SCAN
    // ═══════════════════════════════════════════════════════════════
    function startPeriodicScan() {
        setInterval(() => {
            scanExistingComments();
            if (pendingProfiles.size > 0) {
                injectAllPending();
            }
        }, 3000);
    }

    // ═══════════════════════════════════════════════════════════════
    // AUTO-LOAD MORE COMMENTS (REPLAY MODE)
    // ═══════════════════════════════════════════════════════════════
    const LOAD_MORE_INTERVAL_MS = 2500;
    const LOAD_MORE_MAX_IDLE = 5; // stop after this many consecutive fails

    // Patterns for "load more comments" buttons (Vietnamese + English)
    const LOAD_MORE_PATTERNS = [
        'Xem thêm bình luận',
        'Xem các bình luận trước',
        'View more comments',
        'View previous comments',
        'View more replies',
        'Xem thêm phản hồi',
    ];

    // Comment filter options
    const FILTER_DROPDOWN_PATTERNS = ['Phù hợp nhất', 'Most relevant', 'Mới nhất', 'Newest'];
    const ALL_COMMENTS_PATTERNS = ['Tất cả bình luận', 'All comments'];

    // State: 'IDLE' | 'SELECT_FILTER' | 'WAIT_DROPDOWN' | 'LOADING'
    let autoLoadActive = false;
    let autoLoadTimer = null;
    let autoLoadIdleCount = 0;
    let autoLoadClicks = 0;
    let autoLoadPhase = 'IDLE';
    let filterSelected = false;

    /**
     * Step 1: Click the comment filter dropdown ("Phù hợp nhất ▼")
     * Returns true if dropdown was found and clicked.
     */
    function clickFilterDropdown() {
        // Look for the filter dropdown — it's usually a span with "Phù hợp nhất" text
        // inside a clickable container
        const allEls = document.querySelectorAll(
            'span, div[role="button"], [role="button"] span, [role="listbox"]'
        );

        for (const el of allEls) {
            const text = el.textContent?.trim();
            if (!text) continue;

            for (const pattern of FILTER_DROPDOWN_PATTERNS) {
                if (text === pattern || text.startsWith(pattern)) {
                    const clickTarget =
                        el.closest('[role="button"]') ||
                        el.closest('[role="listbox"]') ||
                        el.closest('div[tabindex]') ||
                        el.closest('[aria-haspopup]') ||
                        el;

                    if (!clickTarget) continue;
                    const rect = clickTarget.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;

                    clickTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    setTimeout(() => {
                        clickTarget.click();
                        log(`📋 Clicked filter dropdown: "${text}"`);
                    }, 200);
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * Step 2: Select "Tất cả bình luận" from the opened dropdown menu.
     * Returns true if the option was found and clicked.
     */
    function selectAllComments() {
        // Look for menu items / options in the dropdown
        const allEls = document.querySelectorAll(
            '[role="menuitem"], [role="option"], [role="menuitemradio"], div[role="menu"] span, ' +
            '[role="listbox"] [role="option"], [role="dialog"] span, [data-visualcompletion] span'
        );

        for (const el of allEls) {
            const text = el.textContent?.trim();
            if (!text) continue;

            for (const pattern of ALL_COMMENTS_PATTERNS) {
                if (text.includes(pattern)) {
                    const clickTarget =
                        el.closest('[role="menuitem"]') ||
                        el.closest('[role="option"]') ||
                        el.closest('[role="menuitemradio"]') ||
                        el;

                    if (!clickTarget) continue;
                    const rect = clickTarget.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;

                    clickTarget.click();
                    log(`✅ Selected: "${text}"`);
                    return true;
                }
            }
        }

        // Fallback: try finding by text content in any visible span
        const spans = document.querySelectorAll('span');
        for (const span of spans) {
            const text = span.textContent?.trim();
            if (!text) continue;
            for (const pattern of ALL_COMMENTS_PATTERNS) {
                if (text === pattern) {
                    const rect = span.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    span.click();
                    // Also try clicking parent
                    if (span.parentElement) span.parentElement.click();
                    log(`✅ Selected (fallback): "${text}"`);
                    return true;
                }
            }
        }

        return false;
    }

    /**
     * Step 3: Click "Xem thêm bình luận" / "View more comments".
     * Returns true if a button was found and clicked.
     */
    function clickLoadMore() {
        const allSpans = document.querySelectorAll(
            'span, div[role="button"], [role="button"] span'
        );

        for (const el of allSpans) {
            const text = el.textContent?.trim();
            if (!text || text.length > 80) continue;

            for (const pattern of LOAD_MORE_PATTERNS) {
                if (text.includes(pattern)) {
                    const clickTarget = el.closest('[role="button"]') || el.closest('div[tabindex]') || el;
                    if (!clickTarget) continue;

                    const rect = clickTarget.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;

                    clickTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    setTimeout(() => {
                        clickTarget.click();
                        log(`🔽 Clicked: "${text.substring(0, 40)}"`);
                    }, 300);

                    autoLoadClicks++;
                    autoLoadIdleCount = 0;
                    return true;
                }
            }
        }

        // Strategy 2: aria-label
        const btns = document.querySelectorAll(
            '[aria-label*="thêm bình luận"], [aria-label*="more comments"], [aria-label*="previous comments"]'
        );
        for (const btn of btns) {
            const rect = btn.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
            setTimeout(() => btn.click(), 300);
            autoLoadClicks++;
            autoLoadIdleCount = 0;
            log(`🔽 Clicked load-more (aria-label: ${btn.getAttribute('aria-label')?.substring(0, 30)})`);
            return true;
        }

        return false;
    }

    function autoLoadLoop() {
        if (!autoLoadActive) return;

        // ── Phase: SELECT FILTER ────────────────────────
        if (autoLoadPhase === 'SELECT_FILTER') {
            log('📋 Phase: Opening comment filter dropdown…');
            const clicked = clickFilterDropdown();
            if (clicked) {
                autoLoadPhase = 'WAIT_DROPDOWN';
                updateAutoLoadUI();
                autoLoadTimer = setTimeout(autoLoadLoop, 1500); // wait for dropdown to open
            } else {
                log('⚠️ Could not find filter dropdown — skipping to load-more');
                autoLoadPhase = 'LOADING';
                filterSelected = true;
                updateAutoLoadUI();
                autoLoadTimer = setTimeout(autoLoadLoop, 500);
            }
            return;
        }

        // ── Phase: WAIT FOR DROPDOWN → SELECT "Tất cả bình luận" ───
        if (autoLoadPhase === 'WAIT_DROPDOWN') {
            log('📋 Phase: Selecting "Tất cả bình luận"…');
            const selected = selectAllComments();
            if (selected) {
                filterSelected = true;
                autoLoadPhase = 'LOADING';
                updateAutoLoadUI();
                // Wait for comments to reload after filter change
                autoLoadTimer = setTimeout(autoLoadLoop, 3000);
            } else {
                // Dropdown might not be open yet, or "Tất cả bình luận" not found
                autoLoadIdleCount++;
                if (autoLoadIdleCount >= 3) {
                    log('⚠️ Could not select "Tất cả bình luận" — proceeding with current filter');
                    autoLoadPhase = 'LOADING';
                    autoLoadIdleCount = 0;
                }
                updateAutoLoadUI();
                autoLoadTimer = setTimeout(autoLoadLoop, 1000);
            }
            return;
        }

        // ── Phase: LOADING — click "Xem thêm bình luận" ───
        const found = clickLoadMore();

        if (!found) {
            autoLoadIdleCount++;
            if (autoLoadIdleCount >= LOAD_MORE_MAX_IDLE) {
                log(`✅ Auto-load finished — no more buttons found (${autoLoadClicks} clicks, ${sentCount} comments)`);
                stopAutoLoad();
                return;
            }
        }

        // After clicking, also run a scan for new comments
        setTimeout(() => {
            scanExistingComments();
        }, 800);

        updateAutoLoadUI();
        autoLoadTimer = setTimeout(autoLoadLoop, LOAD_MORE_INTERVAL_MS);
    }

    function startAutoLoad() {
        if (autoLoadActive) return;
        autoLoadActive = true;
        autoLoadIdleCount = 0;
        autoLoadClicks = 0;
        // Start with filter selection if not already done
        autoLoadPhase = filterSelected ? 'LOADING' : 'SELECT_FILTER';
        log('▶️ Auto-load started — phase:', autoLoadPhase);
        updateAutoLoadUI();
        autoLoadLoop();
    }

    function stopAutoLoad() {
        autoLoadActive = false;
        autoLoadPhase = 'IDLE';
        if (autoLoadTimer) {
            clearTimeout(autoLoadTimer);
            autoLoadTimer = null;
        }
        log('⏹️ Auto-load stopped');
        updateAutoLoadUI();
    }

    // ═══════════════════════════════════════════════════════════════
    // AUTO-LOAD CONTROL UI
    // ═══════════════════════════════════════════════════════════════
    const controlBar = document.createElement('div');
    controlBar.className = 'radar-control-bar';

    const btnAutoLoad = document.createElement('button');
    btnAutoLoad.className = 'radar-autoload-btn';
    btnAutoLoad.innerHTML = '▶️ Tải hết';
    btnAutoLoad.title = 'Auto-load tất cả comment từ replay livestream';
    btnAutoLoad.addEventListener('click', () => {
        if (autoLoadActive) {
            stopAutoLoad();
        } else {
            startAutoLoad();
        }
    });

    const autoLoadStatus = document.createElement('span');
    autoLoadStatus.className = 'radar-autoload-status';
    autoLoadStatus.textContent = '';

    controlBar.appendChild(btnAutoLoad);
    controlBar.appendChild(autoLoadStatus);
    document.body.appendChild(controlBar);

    function updateAutoLoadUI() {
        if (autoLoadActive) {
            btnAutoLoad.innerHTML = '⏹️ Dừng';
            btnAutoLoad.classList.add('radar-autoload-btn--active');
            if (autoLoadPhase === 'SELECT_FILTER') {
                autoLoadStatus.textContent = '📋 Đang chọn "Tất cả bình luận"…';
            } else if (autoLoadPhase === 'WAIT_DROPDOWN') {
                autoLoadStatus.textContent = '📋 Chọn bộ lọc…';
            } else {
                autoLoadStatus.textContent = `🔄 ${autoLoadClicks} clicks · ${sentCount} comments`;
            }
        } else {
            btnAutoLoad.innerHTML = '▶️ Tải hết';
            btnAutoLoad.classList.remove('radar-autoload-btn--active');
            if (autoLoadClicks > 0) {
                autoLoadStatus.textContent = `✅ Xong — ${autoLoadClicks} clicks · ${sentCount} comments`;
            } else {
                autoLoadStatus.textContent = '';
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════
    // MESSAGE LISTENER (for popup + background)
    // ═══════════════════════════════════════════════════════════════
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
        if (msg.action === 'ping') {
            sendResponse({ status: 'active', sentCount, wsConnected, autoLoadActive });
            return true;
        }
        if (msg.action === 'get_status') {
            sendResponse({
                wsConnected,
                commentCount: sentCount,
                enabled: radarEnabled,
                postId: currentPostId,
                autoLoadActive,
            });
            return true;
        }
        if (msg.action === 'toggle') {
            radarEnabled = !!msg.enabled;
            if (radarEnabled && !wsConnected) {
                connectWS();
            } else if (!radarEnabled && ws) {
                ws.close();
            }
            updateStatus(wsConnected, sentCount);
            sendResponse({ ok: true, enabled: radarEnabled });
            return true;
        }
        if (msg.action === 'settings_updated') {
            const s = msg.settings || {};
            if (s.serverUrl) {
                serverUrl = s.serverUrl;
                if (ws) ws.close(); // reconnect with new URL
            }
            if (s.radarApiKey !== undefined) radarApiKey = s.radarApiKey;
            if (typeof s.junkFilter === 'boolean') junkFilterEnabled = s.junkFilter;
            if (typeof s.showBadges === 'boolean') showBadgesEnabled = s.showBadges;
            if (typeof s.enabled === 'boolean') radarEnabled = s.enabled;
            sendResponse({ ok: true });
            return true;
        }
    });

    // ═══════════════════════════════════════════════════════════════
    // INIT — Load settings from chrome.storage then start
    // ═══════════════════════════════════════════════════════════════
    function startRadar() {
        log('📡 Livestream Radar v3.1 (Chrome Extension + Popup Settings) loaded');
        connectWS();
        setTimeout(() => {
            scanExistingComments();
            startObserver();
            startPeriodicScan();
        }, 3000);
    }

    // Load settings from chrome.storage then init
    if (typeof chrome !== 'undefined' && chrome.storage) {
        chrome.storage.local.get({
            enabled: true,
            serverUrl: 'http://localhost:8000',
            radarApiKey: '',
            junkFilter: true,
            showBadges: true,
        }, (data) => {
            radarEnabled = data.enabled;
            serverUrl = data.serverUrl || serverUrl;
            radarApiKey = data.radarApiKey || '';
            junkFilterEnabled = data.junkFilter;
            showBadgesEnabled = data.showBadges;
            if (radarEnabled) {
                startRadar();
            } else {
                log('📡 Radar is disabled via settings');
                updateStatus(false, 0);
            }
        });
    } else {
        startRadar();
    }

})();
