// ==UserScript==
// @name         Gemini Multi-Agent & Terminal Bridge
// @namespace    https://github.com/Shamane2000
// @version      2.1
// @description  Dual-Tab ReAct Debatte mit Linux-Terminal-Bridge und Mission-Control Dashboard für Google Gemini & Claude
// @author       Remote-Host & User
// @match        https://gemini.google.com/*
// @match        https://claude.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=gemini.google.com
// @grant        GM_addStyle
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @downloadURL  http://127.0.0.1:8765/userscript/gemini-dual-agent.user.js
// @updateURL    http://127.0.0.1:8765/userscript/gemini-dual-agent.user.js
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// @run-at       document-idle
// ==/UserScript==

(function() {
    'use strict';

    const ACTIVE_PROVIDER = location.hostname.includes('claude.ai') ? 'claude' : 'gemini';

    // Sichere DOM-Element-Erstellung (100% immun gegen Trusted Types CSP-Blocker)
    function el(tag, attrs = {}, children = []) {
        const elem = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === 'style') {
                elem.style.cssText = v;
            } else if (k === 'textContent') {
                elem.textContent = v;
            } else if (k.startsWith('on') && typeof v === 'function') {
                elem.addEventListener(k.substring(2).toLowerCase(), v);
            } else if (typeof v === 'boolean') {
                if (v) elem.setAttribute(k, '');
                elem[k] = v;
            } else {
                elem.setAttribute(k, v);
                elem[k] = v;
            }
        }
        const childList = Array.isArray(children) ? children : [children];
        for (const child of childList) {
            if (!child) continue;
            if (typeof child === 'string' || typeof child === 'number') {
                elem.appendChild(document.createTextNode(String(child)));
            } else if (child instanceof Node) {
                elem.appendChild(child);
            }
        }
        return elem;
    }

    const BRIDGE_URL = 'http://127.0.0.1:8765';
    const AGENT_BUS_CHANNEL = 'gemini_multi_agent_bus';

    let agentBus = null;
    try {
        agentBus = new BroadcastChannel(AGENT_BUS_CHANNEL);
    } catch (e) {
        console.warn('BroadcastChannel nicht verfügbar:', e);
    }

    let bridgeOnline = false;

    function checkBridgeHealth(callback) {
        if (typeof GM_xmlhttpRequest !== 'function') {
            bridgeOnline = false;
            if (callback) callback(false);
            updateBridgeIndicator();
            return;
        }

        GM_xmlhttpRequest({
            method: 'GET',
            url: `${BRIDGE_URL}/health`,
            timeout: 2000,
            onload: function(res) {
                bridgeOnline = (res.status === 200);
                if (callback) callback(bridgeOnline);
                updateBridgeIndicator();
            },
            onerror: function() {
                bridgeOnline = false;
                if (callback) callback(false);
                updateBridgeIndicator();
            },
            ontimeout: function() {
                bridgeOnline = false;
                if (callback) callback(false);
                updateBridgeIndicator();
            }
        });
    }

    function testCodeOnTerminal(language, code, callback) {
        if (!bridgeOnline) {
            callback(null, 'Bridge offline');
            return;
        }

        GM_xmlhttpRequest({
            method: 'POST',
            url: `${BRIDGE_URL}/test_code`,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ language: language, code: code, round: debateState.round }),
            timeout: 15000,
            onload: function(res) {
                try {
                    const data = JSON.parse(res.responseText);
                    callback(data, null);
                } catch (e) {
                    callback(null, 'Ungültige Server-Antwort');
                }
            },
            onerror: function() {
                callback(null, 'Verbindungsfehler zur Bridge');
            }
        });
    }

    function logEventToBridge(role, round, title, content, task = '') {
        if (!bridgeOnline && typeof GM_xmlhttpRequest !== 'function') return;
        try {
            GM_xmlhttpRequest({
                method: 'POST',
                url: `${BRIDGE_URL}/log_event`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify({
                    role: role,
                    round: round,
                    title: title,
                    content: content,
                    task: task
                })
            });
        } catch (e) {}
    }

    function finishMissionOnServer(result) {
        if (!bridgeOnline && typeof GM_xmlhttpRequest !== 'function') return;
        try {
            GM_xmlhttpRequest({
                method: 'POST',
                url: `${BRIDGE_URL}/finish_mission`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify({ result: result, provider: ACTIVE_PROVIDER })
            });
        } catch (e) {}
    }

    function failMissionOnServer(errMsg) {
        if (!bridgeOnline && typeof GM_xmlhttpRequest !== 'function') return;
        try {
            GM_xmlhttpRequest({
                method: 'POST',
                url: `${BRIDGE_URL}/fail_mission`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify({ error: String(errMsg || 'Browser error'), provider: ACTIVE_PROVIDER })
            });
        } catch (e) {}
    }

    // =========================================================================
    // MODELL-ERKENNUNG, LIMIT-ÜBERWACHUNG & AUTO-FALLBACK IM BROWSER
    // =========================================================================
    let lastReportedModel = '';

    function getModelSwitcherButton() {
        const selectors = ACTIVE_PROVIDER === 'claude'
            ? [
                'button[aria-label^="Modell:"]',
                'button[aria-label^="Model:"]',
                '[data-testid="model-selector-dropdown"]'
              ]
            : [
                '[data-test-id="bard-mode-menu-button"]',
                'bard-mode-switcher button',
                '.gds-mode-switch-button',
                'button[aria-label*="Modell" i]',
                'button[aria-label*="Model" i]',
                'button[class*="mode-switch"]',
                'model-picker button'
              ];
        for (const sel of selectors) {
            const btn = document.querySelector(sel);
            if (btn && btn.offsetParent !== null) return btn;
        }
        // Fallback: Suche sichtbaren Button mit Modellnamen + Dropdown-Icon
        const allBtns = document.querySelectorAll('button');
        for (const b of allBtns) {
            if (b.offsetParent === null) continue;
            const t = (b.innerText || '').trim();
            if (t.length > 0 && t.length < 35 && /\b(2\.0|2\.5|3\.0|Flash|Pro|Thinking|Schnell|Advanced)\b/i.test(t) && !t.includes('Dual-Agent')) {
                if (b.querySelector('mat-icon, svg') || b.getAttribute('aria-haspopup')) {
                    return b;
                }
            }
        }
        return null;
    }

    function detectCurrentBrowserModel() {
        const btn = getModelSwitcherButton();
        if (!btn) return '';
        let txt = (ACTIVE_PROVIDER === 'claude'
            ? (btn.getAttribute('aria-label') || btn.innerText || btn.textContent || '')
            : (btn.innerText || btn.textContent || '')
        ).replace(/\s+/g, ' ').trim();
        txt = txt.replace(/^(?:Modell|Model):\s*/i, '');
        txt = txt.replace(/arrow_drop_down|expand_more|keyboard_arrow_down/gi, '').trim();
        return txt;
    }

    function reportModelStatus(limitWarning = null, cooldownInfo = null) {
        const current = detectCurrentBrowserModel();
        const mb = document.getElementById('gda-model-badge');
        if (mb && current) mb.textContent = `🤖 ${current}`;
        if (!bridgeOnline) return;
        if (!current && limitWarning === null && !cooldownInfo) return;
        if (current) lastReportedModel = current;

        const payload = {
            provider: ACTIVE_PROVIDER,
            capability: getTabCapability(),
            current_model: current || lastReportedModel
        };
        if (limitWarning !== null) payload.limit_warning = limitWarning;
        if (cooldownInfo) payload.cooldown_info = cooldownInfo;
        if (Array.isArray(window.__gdaAvailableModels) && window.__gdaAvailableModels.length > 0) {
            payload.available_models = window.__gdaAvailableModels;
        }

        try {
            GM_xmlhttpRequest({
                method: 'POST',
                url: `${BRIDGE_URL}/model_status`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(payload)
            });
        } catch (e) {}
    }

    let isSwitchingModel = false;
    function switchGeminiModelInDOM(targetName, callback) {
        if (isSwitchingModel) {
            if (callback) callback(false, 'Modellwechsel läuft bereits');
            return;
        }
        const btn = getModelSwitcherButton();
        if (!btn) {
            if (callback) callback(false, 'Modell-Button nicht gefunden');
            return;
        }
        isSwitchingModel = true;

        btn.click();
        setTimeout(() => {
            const items = document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], .mat-mdc-menu-item, bard-mode-switcher-item, [data-test-id*="mode-option"]');
            const available = [];
            let matchedItem = null;

            for (const item of items) {
                const rawText = (item.innerText || item.textContent || '').trim();
                const rawLines = rawText.split('\n').map(s => s.trim()).filter(Boolean);
                const shortName = rawLines[0] || '';
                const label = rawText.replace(/\s+/g, ' ').trim();
                if (shortName) {
                    available.push(shortName);
                    if (!matchedItem && label.toLowerCase().includes(targetName.toLowerCase())) {
                        matchedItem = item;
                    }
                }
            }
            if (available.length > 0) window.__gdaAvailableModels = available;

            if (matchedItem) {
                const isDisabled = matchedItem.getAttribute('aria-disabled') === 'true' ||
                                   matchedItem.disabled ||
                                   matchedItem.classList.contains('mat-mdc-menu-item-disabled');
                if (isDisabled) {
                    const cd = parseCooldownFromText(matchedItem.innerText || matchedItem.textContent || '');
                    reportModelStatus(`Modell ${targetName} aktuell gesperrt (bis ${cd.retry_after_str})`, {
                        limited_model: targetName,
                        retry_after_ts: cd.retry_after_ts,
                        retry_after_str: cd.retry_after_str,
                        reason: 'Menü-Option im Browser deaktiviert'
                    });
                    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
                    const bd = document.querySelector('.cdk-overlay-backdrop');
                    if (bd) bd.click();
                    isSwitchingModel = false;
                    if (callback) callback(false, `${targetName} noch gesperrt bis ${cd.retry_after_str}`);
                    return;
                }
                matchedItem.click();
                setTimeout(() => {
                    isSwitchingModel = false;
                    const newModel = detectCurrentBrowserModel();
                    try {
                        GM_xmlhttpRequest({
                            method: 'POST',
                            url: `${BRIDGE_URL}/model_status`,
                            headers: { 'Content-Type': 'application/json' },
                            data: JSON.stringify({
                                current_model: newModel || targetName,
                                available_models: available,
                                limit_warning: '',
                                clear_desired: true
                            })
                        });
                    } catch (e) {}
                    if (callback) callback(true, newModel || targetName);
                }, 600);
            } else {
                document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
                const bd = document.querySelector('.cdk-overlay-backdrop');
                if (bd) bd.click();
                isSwitchingModel = false;
                try {
                    GM_xmlhttpRequest({
                        method: 'POST',
                        url: `${BRIDGE_URL}/model_status`,
                        headers: { 'Content-Type': 'application/json' },
                        data: JSON.stringify({ clear_desired: true })
                    });
                } catch (e) {}
                if (callback) callback(false, `Modell "${targetName}" nicht im Menü gefunden`);
            }
        }, 450);
    }

    let __gdaScannedOnce = false;
    function scanGeminiModelsInDOM() {
        if (ACTIVE_PROVIDER !== 'gemini' || __gdaScannedOnce || debateState.active) return;
        const btn = getModelSwitcherButton();
        if (!btn) return;
        __gdaScannedOnce = true;

        btn.click();
        setTimeout(() => {
            const items = document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], .mat-mdc-menu-item, bard-mode-switcher-item, [data-test-id*="mode-option"]');
            const available = [];
            let firstCooldown = null;

            for (const item of items) {
                const rawText = (item.innerText || item.textContent || '').trim();
                const rawLines = rawText.split('\n').map(s => s.trim()).filter(Boolean);
                const shortName = rawLines[0] || '';
                if (!shortName) continue;
                available.push(shortName);

                const isDisabled = item.getAttribute('aria-disabled') === 'true' ||
                                   item.disabled ||
                                   item.classList.contains('mat-mdc-menu-item-disabled');
                if (isDisabled || checkRateLimitInText(rawText)) {
                    const cd = parseCooldownFromText(rawText);
                    firstCooldown = {
                        limited_model: shortName,
                        retry_after_ts: cd.retry_after_ts,
                        retry_after_str: cd.retry_after_str,
                        reason: rawText.replace(/\s+/g, ' ').slice(0, 120)
                    };
                }
            }

            // Menü sauber wieder schließen
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
            const backdrop = document.querySelector('.cdk-overlay-backdrop');
            if (backdrop) backdrop.click();

            if (available.length > 0) {
                window.__gdaAvailableModels = available;
                reportModelStatus(
                    firstCooldown ? `${firstCooldown.limited_model} im Menü gesperrt (bis ${firstCooldown.retry_after_str})` : null,
                    firstCooldown
                );
            }
        }, 400);
    }

    function parseCooldownFromText(text) {
        const now = Date.now();
        let targetMs = now + 60 * 60 * 1000; // Fallback: +60 Min
        if (text) {
            // 1. Explizite Uhrzeit: "18:30", "um 18:30 Uhr", "6:30 PM"
            const timeMatch = text.match(/\b(\d{1,2}):(\d{2})\s*(am|pm)?\b/i);
            // 2. Relative Stunden/Minuten: "in 2 Stunden", "in 45 Minuten", "in 2 hours"
            const relHours = text.match(/in\s+(\d+)\s*(stunde|stunden|hour|hours|std|h)\b/i);
            const relMins = text.match(/in\s+(\d+)\s*(minute|minuten|min|minutes|m)\b/i);

            if (timeMatch) {
                let h = parseInt(timeMatch[1], 10);
                const m = parseInt(timeMatch[2], 10);
                const ampm = (timeMatch[3] || '').toLowerCase();
                if (ampm === 'pm' && h < 12) h += 12;
                if (ampm === 'am' && h === 12) h = 0;
                if (h >= 0 && h <= 23 && m >= 0 && m <= 59) {
                    const dt = new Date();
                    dt.setHours(h, m, 0, 0);
                    if (dt.getTime() <= now) {
                        dt.setDate(dt.getDate() + 1);
                    }
                    targetMs = dt.getTime();
                }
            } else if (relHours || relMins) {
                let addMs = 0;
                if (relHours) addMs += parseInt(relHours[1], 10) * 3600 * 1000;
                if (relMins) addMs += parseInt(relMins[1], 10) * 60 * 1000;
                if (addMs > 0) targetMs = now + addMs;
            } else if (/morgen|tomorrow/i.test(text)) {
                const dt = new Date();
                dt.setDate(dt.getDate() + 1);
                dt.setHours(8, 0, 0, 0);
                targetMs = dt.getTime();
            }
        }
        const d = new Date(targetMs);
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        return {
            retry_after_ts: Math.floor(targetMs / 1000),
            retry_after_str: `${hh}:${mm}`
        };
    }

    function checkRateLimitInText(text) {
        if (!text) return false;
        const limitPatterns = [
            /limit.*erreicht/i,
            /nutzungslimit/i,
            /reached your limit/i,
            /usage limit/i,
            /rate limit/i,
            /zu viele anfragen/i,
            /später erneut versuchen/i,
            /try again later/i,
            /kapazität.*erschöpft/i
        ];
        return limitPatterns.some(rx => rx.test(text));
    }

    function getStopButton() {
        return document.querySelector(
            'button[aria-label*="Stopp" i], button[aria-label*="Stop" i], ' +
            'button[aria-label*="anhalten" i], button[aria-label*="abbrechen" i], ' +
            'button[aria-label*="unterbrechen" i], button[data-testid="stop-button"], .stop-icon'
        ) || (ACTIVE_PROVIDER === 'claude' ? document.querySelector('[data-is-streaming="true"]') : null);
    }

    function isTabCurrentlyGenerating() {
        return Boolean(getStopButton());
    }

    function handleBridgeCommands(commands) {
        if (!Array.isArray(commands) || commands.length === 0) return;
        for (const cmd of commands) {
            const action = (cmd && cmd.action) || '';
            if (action === 'reload') {
                debateState.active = false;
                try { sessionStorage.removeItem('gda_pending_init'); } catch (e) {}
                updateStatus('🔄 Remote-Reload...', 'running');
                logEventToBridge(getTabRole(), 1, '🔄 Remote-Reload ausgeführt', `provider=${ACTIVE_PROVIDER} role=${getTabRole()} tab=${getTabId()}`);
                setTimeout(() => window.location.reload(), 250);
                return;
            }
            if (action === 'new_chat') {
                debateState.active = false;
                try { sessionStorage.removeItem('gda_pending_init'); } catch (e) {}
                updateStatus('🆕 Starte neuen Chat...', 'running');
                logEventToBridge(getTabRole(), 1, '🆕 Remote New-Chat ausgeführt', `provider=${ACTIVE_PROVIDER} role=${getTabRole()}`);
                setTimeout(() => {
                    window.location.href = (ACTIVE_PROVIDER === 'claude' ? '/new' : '/app');
                }, 150);
                return;
            }
            if (action === 'set_role' && cmd.value) {
                setTabRole(cmd.value);
                const sel = document.getElementById('gda-role-select');
                if (sel) sel.value = cmd.value;
                updateStatus(`Rolle: ${cmd.value}`, 'success');
                logEventToBridge(cmd.value, 1, '🎭 Remote-Rollenwechsel', `provider=${ACTIVE_PROVIDER} new_role=${cmd.value}`);
            }
            if (action === 'ping') {
                const elIn = getInputElement();
                const inLen = (elIn?.innerText || elIn?.value || '').trim().length;
                let hasPm = false;
                try {
                    const pw = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
                    const pe = pw.document.querySelector('div.tiptap.ProseMirror[contenteditable="true"], .ProseMirror[contenteditable="true"]');
                    hasPm = Boolean(pe && (pe.pmViewDesc?.view || pe.parentElement?.pmViewDesc?.view));
                } catch (e) {}
                let domDiag = '';
                if (ACTIVE_PROVIDER === 'claude') {
                    const aMsg = document.querySelectorAll('[data-testid="assistant-message"]');
                    const uMsg = document.querySelectorAll('[data-testid="user-message"]');
                    const fcr = document.querySelectorAll('.font-claude-response');
                    const fcrb = document.querySelectorAll('.font-claude-response-body');
                    const sm = document.querySelectorAll('.standard-markdown');
                    const stream = document.querySelectorAll('[data-is-streaming]');
                    const testIds = Array.from(new Set(Array.from(document.querySelectorAll('[data-testid]')).map(e => e.getAttribute('data-testid')))).slice(0, 20);
                    const lastFcr = fcr.length ? (fcr[fcr.length - 1].innerText || '').trim().slice(0, 60).replace(/\s+/g, ' ') : '';
                    const lastAMsg = aMsg.length ? (aMsg[aMsg.length - 1].innerText || '').trim().slice(0, 60).replace(/\s+/g, ' ') : '';
                    domDiag = ` url=${location.pathname} aMsg=${aMsg.length} uMsg=${uMsg.length} fcr=${fcr.length} fcrb=${fcrb.length} sm=${sm.length} stream=${stream.length} testIds=[${testIds.join(',')}] lastFcr="${lastFcr}" lastAMsg="${lastAMsg}"`;
                }
                logEventToBridge(getTabRole(), 1, '🏓 Remote-Ping', `provider=${ACTIVE_PROVIDER} role=${getTabRole()} focus=${document.hasFocus()} hasEl=${Boolean(elIn)} pmView=${hasPm} inLen=${inLen} method=${lastInputMethod}${domDiag}`);
            }
        }
    }

    let lastHeartbeatTs = 0;
    function sendTabHeartbeat(tabId, role, generating, debate) {
        const now = Date.now();
        if (now - lastHeartbeatTs < 3500) return;
        lastHeartbeatTs = now;
        try {
            GM_xmlhttpRequest({
                method: 'GET',
                url: `${BRIDGE_URL}/heartbeat?tab_id=${encodeURIComponent(tabId)}&role=${encodeURIComponent(role)}&provider=${encodeURIComponent(ACTIVE_PROVIDER)}&generating=${generating ? 1 : 0}&debate=${debate ? 1 : 0}`,
                timeout: 3000,
                onload: function(res) {
                    try {
                        const d = JSON.parse(res.responseText);
                        if (d && Array.isArray(d.commands) && d.commands.length > 0) {
                            handleBridgeCommands(d.commands);
                        }
                    } catch (e) {}
                }
            });
        } catch (e) {}
    }

    let isCheckingMission = false;
    let hasActiveReviewer = false;
    function checkPendingMissions() {
        if (!bridgeOnline) return;
        const role = getTabRole();
        const tabId = getTabId();
        const gen = isTabCurrentlyGenerating();
        if (debateState.active) {
            sendTabHeartbeat(tabId, role, gen, true);
            return;
        }
        if (isCheckingMission) return;

        isCheckingMission = true;
        GM_xmlhttpRequest({
            method: 'GET',
            url: `${BRIDGE_URL}/pending_mission?tab_id=${encodeURIComponent(tabId)}&role=${encodeURIComponent(role)}&provider=${encodeURIComponent(ACTIVE_PROVIDER)}&generating=${gen ? 1 : 0}&debate=0`,
            timeout: 5000,
            onload: function(res) {
                isCheckingMission = false;
                try {
                    const data = JSON.parse(res.responseText);
                    if (data && Array.isArray(data.commands) && data.commands.length > 0) {
                        handleBridgeCommands(data.commands);
                        return;
                    }
                    hasActiveReviewer = Boolean(data.has_reviewer);

                    // 1. Browser-Modell regelmäßig an Bridge melden
                    const curMod = detectCurrentBrowserModel();
                    if (curMod && curMod !== lastReportedModel) {
                        reportModelStatus();
                    }

                    // 2. Manueller Modellwechsel vom Dashboard ausführen
                    if (data.desired_model && !isSwitchingModel && (!curMod || !curMod.toLowerCase().includes(data.desired_model.toLowerCase()))) {
                        updateStatus(`🔄 Wechsle Browser-Modell zu ${data.desired_model}...`, 'running');
                        switchGeminiModelInDOM(data.desired_model, (ok, newMod) => {
                            if (ok) updateStatus(`✅ Modell gewechselt auf: ${newMod}`, 'success');
                        });
                        return;
                    }

                    if (!isWorkerRole(role)) return;

                    const mission = data.mission;
                    if (mission && mission.status === 'pending' && mission.task) {
                        // Claim mission
                        GM_xmlhttpRequest({
                            method: 'POST',
                            url: `${BRIDGE_URL}/claim_mission`,
                            headers: { 'Content-Type': 'application/json' },
                            data: JSON.stringify({ id: mission.id })
                        });
                        console.log('Übernehme Mission vom Dashboard:', mission.task);
                        if (mission.type === 'init') {
                            runInitMission(mission.task);
                        } else if (mission.capability === 'image') {
                            runImageMission(mission.task);
                        } else {
                            startDebate(mission.task, mission.rounds || 2, mission.execute !== false);
                        }
                    }
                } catch (e) {}
            },
            onerror: function() { isCheckingMission = false; },
            ontimeout: function() { isCheckingMission = false; }
        });
    }

    function executeInitBootstrap(soulPrompt) {
        debateState.active = true;
        updateStatus('🔄 Sende API-Harness-Soul...', 'running');
        logEventToBridge('coder', 1, '🔄 /init Soul-Bootstrap gesendet', soulPrompt);
        sendPromptAndAwaitResponse(soulPrompt, (resText, err) => {
            debateState.active = false;
            if (err) {
                updateStatus('❌ Init-Fehler: ' + err, 'error');
                failMissionOnServer('ERROR: ' + err);
                return;
            }
            updateStatus('✅ Session initialisiert (API-Harness)', 'success');
            logEventToBridge('coder', 1, '✅ /init bestätigt', resText || 'READY');
            finishMissionOnServer(resText || 'READY');
        });
    }

    function runInitMission(soulPrompt) {
        debateState.active = true;
        updateStatus('🔄 Starte neuen Chat...', 'running');
        if (agentBus) {
            try { agentBus.postMessage({ type: 'NEW_REVIEWER_CHAT', provider: ACTIVE_PROVIDER }); } catch (e) {}
        }
        try {
            sessionStorage.setItem('gda_pending_init', JSON.stringify({
                prompt: soulPrompt,
                provider: ACTIVE_PROVIDER,
                ts: Date.now()
            }));
        } catch (e) {}

        const newChatBtn = ACTIVE_PROVIDER === 'claude'
            ? document.querySelector('a[href="/new"], button[aria-label*="Neuer Chat" i], button[aria-label*="New chat" i]')
            : document.querySelector('a[href="/app"], button[aria-label*="Neuer Chat" i], button[aria-label*="New chat" i]');

        if (newChatBtn) {
            newChatBtn.click();
        } else {
            window.location.href = (ACTIVE_PROVIDER === 'claude' ? '/new' : '/app');
            return;
        }

        // Falls SPA ohne Seiten-Reload wechselt: Init nach kurzem UI-Settle direkt ausführen
        setTimeout(() => {
            try {
                const raw = sessionStorage.getItem('gda_pending_init');
                if (!raw) return;
                sessionStorage.removeItem('gda_pending_init');
                const obj = JSON.parse(raw);
                if (obj && obj.prompt) executeInitBootstrap(obj.prompt);
            } catch (e) {}
        }, 1600);
    }

    function resumePendingInitIfAny() {
        try {
            const raw = sessionStorage.getItem('gda_pending_init');
            if (!raw) return;
            const obj = JSON.parse(raw);
            if (!obj || obj.provider !== ACTIVE_PROVIDER || (Date.now() - (obj.ts || 0)) > 30000) {
                sessionStorage.removeItem('gda_pending_init');
                return;
            }
            sessionStorage.removeItem('gda_pending_init');
            executeInitBootstrap(obj.prompt);
        } catch (e) {}
    }

    function extractLatestGeminiImage(callback) {
        const responses = document.querySelectorAll('model-response');
        const lastResp = responses.length > 0 ? responses[responses.length - 1] : document;
        const imgs = Array.from(lastResp.querySelectorAll('img'));
        const candidate = imgs.reverse().find(img => {
            const src = img.getAttribute('src') || '';
            if (!src) return false;
            const big = (img.naturalWidth >= 120 && img.naturalHeight >= 120) || (img.clientWidth >= 120 && img.clientHeight >= 120);
            const isGen = /googleusercontent\.com|ggpht\.com|^blob:|^data:image\//i.test(src);
            return (big || isGen) && !/avatar|profile|icon|favicon/i.test(src);
        });

        if (!candidate) {
            callback(null, 'Kein generiertes Bild im Chat gefunden');
            return;
        }

        const src = candidate.src;
        if (src.startsWith('data:image/')) {
            callback({ dataUrl: src, mime: src.split(';')[0].replace('data:', '') }, null);
            return;
        }

        // Über GM_xmlhttpRequest laden (umgeht CORS und nutzt Browser-Cookies)
        GM_xmlhttpRequest({
            method: 'GET',
            url: src,
            responseType: 'blob',
            timeout: 20000,
            onload: function(res) {
                try {
                    const blob = res.response;
                    const reader = new FileReader();
                    reader.onloadend = () => {
                        callback({ dataUrl: reader.result, mime: blob.type || 'image/png' }, null);
                    };
                    reader.readAsDataURL(blob);
                } catch (e) {
                    callback(null, String(e));
                }
            },
            onerror: function() { callback(null, 'Bild-Download fehlgeschlagen'); }
        });
    }

    function runImageMission(promptText) {
        debateState.active = true;
        updateStatus('🎨 Generiere Bild...', 'running');
        const finalPrompt = /^(erstelle|generiere|zeichne|male|create|generate|draw)\b/i.test(promptText)
            ? promptText
            : `Erstelle ein Bild: ${promptText}`;

        sendPromptAndAwaitResponse(finalPrompt, (text, err) => {
            if (err) {
                debateState.active = false;
                updateStatus('❌ Bild-Fehler', 'error');
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: `${BRIDGE_URL}/finish_mission`,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify({ result: `Fehler: ${err}` })
                });
                return;
            }
            // Kurz warten bis Bild vollständig im DOM gerendert ist
            setTimeout(() => {
                extractLatestGeminiImage((imgData, imgErr) => {
                    debateState.active = false;
                    if (imgData && imgData.dataUrl) {
                        updateStatus('✅ Bild an Bridge gesendet!', 'success');
                        GM_xmlhttpRequest({
                            method: 'POST',
                            url: `${BRIDGE_URL}/finish_mission`,
                            headers: { 'Content-Type': 'application/json' },
                            data: JSON.stringify({
                                result: text || 'Bild erfolgreich generiert.',
                                image_base64: imgData.dataUrl,
                                image_mime: imgData.mime || 'image/png'
                            })
                        });
                    } else {
                        updateStatus('⚠️ Kein Bild gefunden', 'error');
                        GM_xmlhttpRequest({
                            method: 'POST',
                            url: `${BRIDGE_URL}/finish_mission`,
                            headers: { 'Content-Type': 'application/json' },
                            data: JSON.stringify({ result: text || imgErr || 'Kein Bild gefunden' })
                        });
                    }
                });
            }, 1500);
        });
    }

    function getTabId() {
        let tid = sessionStorage.getItem('gda_tab_id');
        if (!tid) {
            tid = 'tab_' + Math.random().toString(36).slice(2, 10) + '_' + Date.now().toString(36);
            sessionStorage.setItem('gda_tab_id', tid);
        }
        return tid;
    }
    function getTabRole() {
        return sessionStorage.getItem('gemini_dual_agent_role') || 'solo';
    }
    function setTabRole(role) {
        sessionStorage.setItem('gemini_dual_agent_role', role);
    }
    let __gdaRoleAutoAssigned = false;
    function autoAssignTabRoleIfUnset() {
        if (__gdaRoleAutoAssigned || sessionStorage.getItem('gemini_dual_agent_role')) return;
        __gdaRoleAutoAssigned = true;
        const tabId = getTabId();
        try {
            GM_xmlhttpRequest({
                method: 'GET',
                url: `${BRIDGE_URL}/register_tab?tab_id=${encodeURIComponent(tabId)}&provider=${encodeURIComponent(ACTIVE_PROVIDER)}`,
                timeout: 4000,
                onload: function(res) {
                    try {
                        const data = JSON.parse(res.responseText);
                        if (data.assigned_role) {
                            setTabRole(data.assigned_role);
                            const sel = document.getElementById('gda-role-select');
                            if (sel) sel.value = data.assigned_role;
                            updateStatus(isWorkerRole(data.assigned_role) ? 'Bereit' : 'Einzel-Chat', 'normal');
                            reportModelStatus();
                        }
                    } catch (e) {}
                }
            });
        } catch (e) {}
    }
    function isWorkerRole(role) {
        return ['coder', 'worker', 'image', 'canvas', 'research'].includes(role);
    }
    function getTabCapability() {
        const r = getTabRole();
        if (r === 'image' || r === 'canvas' || r === 'research') return r;
        if (r === 'reviewer') return 'review';
        return 'chat';
    }

    function getInputElement() {
        if (ACTIVE_PROVIDER === 'claude') {
            return document.querySelector('div.tiptap.ProseMirror[contenteditable="true"]') ||
                   document.querySelector('.ProseMirror[contenteditable="true"]') ||
                   document.querySelector('[contenteditable="true"]') ||
                   document.querySelector('textarea');
        }
        return document.querySelector('rich-textarea div[contenteditable="true"]') ||
               document.querySelector('rich-textarea textarea') ||
               document.querySelector('.input-area-container textarea') ||
               document.querySelector('textarea') ||
               document.querySelector('[contenteditable="true"]');
    }

    let lastInputMethod = 'none';
    let lastSendDiag = 'none';

    function getInputTextLength(elInput) {
        if (!elInput) return 0;
        if (elInput.tagName && elInput.tagName.toLowerCase() === 'textarea') {
            return (elInput.value || '').trim().length;
        }
        return (elInput.innerText || elInput.textContent || '').trim().length;
    }

    function placeSelectionInElement(elInput) {
        try {
            const targetNode = elInput.querySelector('p') || elInput;
            const sel = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(targetNode);
            sel.removeAllRanges();
            sel.addRange(range);
        } catch (e) {}
    }

    function setClaudeProseMirrorDirect(text) {
        try {
            const pageWin = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;
            const pageDoc = pageWin.document || document;
            const candidates = [
                pageDoc.querySelector('div.tiptap.ProseMirror[contenteditable="true"]'),
                pageDoc.querySelector('.ProseMirror[contenteditable="true"]'),
                pageDoc.querySelector('[contenteditable="true"]')
            ].filter(Boolean);

            for (let node of candidates) {
                let cur = node;
                for (let depth = 0; depth < 4 && cur; depth++, cur = cur.parentElement) {
                    const view = cur.pmViewDesc?.view || cur.pmViewDesc?.editorView;
                    if (view && view.state && typeof view.dispatch === 'function') {
                        const state = view.state;
                        const pType = state.schema?.nodes?.paragraph;
                        let tr;
                        if (pType && typeof state.schema.text === 'function') {
                            const lines = text.split('\n');
                            const nodes = lines.map(l => pType.create(null, l ? state.schema.text(l) : null));
                            tr = state.tr.replaceWith(0, state.doc.content.size, nodes);
                        } else {
                            tr = state.tr.insertText(text, 0, state.doc.content.size);
                        }
                        view.dispatch(tr);
                        if ((node.innerText || node.textContent || '').trim().length > 0) {
                            lastInputMethod = 'pmViewDesc';
                            return true;
                        }
                    }
                    // Fallback: React Fiber / Tiptap Editor instance
                    const rKey = Object.keys(cur).find(k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'));
                    if (rKey && cur[rKey]) {
                        const props = rKey.startsWith('__reactProps$') ? cur[rKey] : cur[rKey].memoizedProps;
                        const editor = props?.editor || props?.tiptapEditor;
                        if (editor && editor.commands && typeof editor.commands.setContent === 'function') {
                            editor.commands.setContent(text.split('\n').map(l => `<p>${l.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</p>`).join(''));
                            if ((node.innerText || node.textContent || '').trim().length > 0) {
                                lastInputMethod = 'tiptapEditor';
                                return true;
                            }
                        }
                    }
                }
            }
        } catch (e) {}
        return false;
    }

    function setGeminiInput(text, preferredMethod = 'auto') {
        const elInput = getInputElement();
        if (!elInput) {
            lastInputMethod = 'no_element';
            return false;
        }
        try { elInput.focus({ preventScroll: true }); } catch (e) { try { elInput.focus(); } catch (e2) {} }

        if (elInput.tagName.toLowerCase() === 'textarea') {
            elInput.value = text;
            elInput.dispatchEvent(new Event('input', { bubbles: true }));
            elInput.dispatchEvent(new Event('change', { bubbles: true }));
            lastInputMethod = 'textarea';
            return true;
        }

        // Für Tiptap/ProseMirror (claude.ai): Direkte Transaction, Selection+execCommand (nur bei Fokus), beforeinput/paste, Composition-Flush
        if (ACTIVE_PROVIDER === 'claude') {
            if (preferredMethod === 'auto' && setClaudeProseMirrorDirect(text)) {
                return true;
            }

            if (preferredMethod === 'auto' && document.hasFocus()) {
                placeSelectionInElement(elInput);
                try {
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                    if (document.execCommand('insertText', false, text) && getInputTextLength(elInput) > 0) {
                        elInput.dispatchEvent(new Event('input', { bubbles: true }));
                        lastInputMethod = 'execCommand';
                        return true;
                    }
                } catch (e) {}
            }

            if (preferredMethod !== 'composition' && document.hasFocus()) {
                try {
                    placeSelectionInElement(elInput);
                    const dt = new DataTransfer();
                    dt.setData('text/plain', text);
                    elInput.dispatchEvent(new InputEvent('beforeinput', {
                        bubbles: true,
                        cancelable: true,
                        inputType: 'insertFromPaste',
                        data: text,
                        dataTransfer: dt
                    }));
                    elInput.dispatchEvent(new ClipboardEvent('paste', {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: dt
                    }));
                    if (getInputTextLength(elInput) > 0) {
                        elInput.dispatchEvent(new Event('input', { bubbles: true }));
                        lastInputMethod = 'beforeinput_paste';
                        return true;
                    }
                } catch (e) {}
            }

            // Letzter ProseMirror-Fallback ohne Fokus: Bestehendes <p> wiederverwenden + CompositionEvent-Flush
            try {
                elInput.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
                const lines = text.split('\n');
                let firstP = elInput.querySelector('p');
                if (!firstP) {
                    firstP = document.createElement('p');
                    elInput.appendChild(firstP);
                }
                while (elInput.children.length > 1) {
                    elInput.removeChild(elInput.lastChild);
                }
                firstP.classList.remove('is-empty', 'is-editor-empty');
                firstP.textContent = lines[0] || '';
                for (let i = 1; i < lines.length; i++) {
                    const p = document.createElement('p');
                    if (lines[i].length === 0) p.appendChild(document.createElement('br'));
                    else p.textContent = lines[i];
                    elInput.appendChild(p);
                }
                placeSelectionInElement(elInput);
                elInput.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: text }));
                elInput.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertCompositionText', data: text }));
                elInput.dispatchEvent(new Event('change', { bubbles: true }));
                lastInputMethod = 'composition_dom';
                return getInputTextLength(elInput) > 0;
            } catch (e) {}
        }

        // Für rich-textarea (Google Gemini): Sicher ohne innerHTML und ohne Enter-Trigger
        while (elInput.firstChild) {
            elInput.removeChild(elInput.firstChild);
        }

        const lines = text.split('\n');
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            const p = document.createElement('p');
            if (line.trim().length === 0) {
                p.appendChild(document.createElement('br'));
            } else {
                p.textContent = line;
            }
            elInput.appendChild(p);
        }

        // Cursor ans Ende
        try {
            const range = document.createRange();
            const sel = window.getSelection();
            range.selectNodeContents(elInput);
            range.collapse(false);
            sel.removeAllRanges();
            sel.addRange(range);
        } catch (e) {}

        // Events feuern für Google Gemini Angular/Wiz Framework
        elInput.dispatchEvent(new Event('input', { bubbles: true }));
        elInput.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste', data: text }));
        elInput.dispatchEvent(new Event('change', { bubbles: true }));
        lastInputMethod = 'gemini_dom';

        return true;
    }

    function clickSendButton() {
        // Sicherheits-Check: Falls Provider bereits antwortet (Stopp-Button sichtbar), NIEMALS klicken!
        if (getStopButton()) {
            return true;
        }
        const elInput = getInputElement();
        if (getInputTextLength(elInput) === 0) {
            return false;
        }
        const selectors = ACTIVE_PROVIDER === 'claude'
            ? [
                'button[aria-label="Nachricht senden"]',
                'button[aria-label="Send message"]',
                'button[data-testid="send-button"]'
              ]
            : [
                'button[aria-label="Nachricht senden"]',
                'button[aria-label="Send message"]',
                'button[data-testid="send-button"]',
                'button[aria-label*="Senden" i]',
                'button[aria-label*="Send" i]',
                'button.send-button',
                '.send-button-container button',
                'button:has(.send-icon)',
                '.send-icon'
              ];
        let sawDisabledSendBtn = false;
        let clicked = false;
        for (const sel of selectors) {
            const btn = document.querySelector(sel);
            if (btn) {
                const clickable = btn.tagName.toLowerCase() === 'button' ? btn : (btn.closest('button') || btn);
                if (clickable) {
                    const lbl = (clickable.getAttribute('aria-label') || '').toLowerCase();
                    if (lbl.includes('stopp') || lbl.includes('stop') || lbl.includes('anhalten') || clickable.querySelector('.stop-icon')) {
                        lastSendDiag = 'already_stop:' + lbl;
                        return true;
                    }
                    if (ACTIVE_PROVIDER === 'claude' && (clickable.disabled || clickable.getAttribute('aria-disabled') === 'true')) {
                        sawDisabledSendBtn = true;
                        lastSendDiag = 'disabled:' + sel;
                        continue;
                    }
                    clickable.removeAttribute('disabled');
                    clickable.disabled = false;
                    clickable.click();
                    lastSendDiag = 'clicked:' + sel + '|lbl=' + (clickable.getAttribute('aria-label') || '');
                    clicked = true;
                    break;
                }
            }
        }

        // Enter nur als Fallback (bei Gemini, oder wenn bei Claude gar kein Sende-Button existiert)
        if (!clicked && elInput && !(ACTIVE_PROVIDER === 'claude' && sawDisabledSendBtn)) {
            if (ACTIVE_PROVIDER !== 'claude') {
                try {
                    elInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
                    elInput.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                    lastSendDiag = 'enter_fallback';
                    clicked = true;
                } catch (e) {}
            } else if (!sawDisabledSendBtn) {
                // Bei Claude Enter-Fallback mit echtem KeyboardEvent + Check aller Send-Buttons im Composer
                const composerBtns = Array.from(elInput.closest('form, fieldset, [class*="composer" i]')?.querySelectorAll('button') || []);
                lastSendDiag = 'no_send_btn_found;btns=' + composerBtns.map(b => (b.getAttribute('aria-label') || b.getAttribute('data-testid') || 'unlabeled') + (b.disabled ? '[dis]' : '')).join(',');
            }
        }
        return clicked;
    }

    function domToMarkdown(root) {
        if (!root) return '';

        function walk(node, listDepth = 0) {
            if (!node) return '';
            if (node.nodeType === Node.TEXT_NODE) {
                return node.nodeValue || '';
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return '';

            const tag = node.tagName.toLowerCase();
            if (['script', 'style', 'mat-icon', 'svg', 'button'].includes(tag)) return '';
            if (node.classList && (node.classList.contains('code-block-decoration') || node.classList.contains('table-footer'))) {
                return '';
            }

            // Code-Blöcke
            if (tag === 'pre' || tag === 'code-block') {
                const codeEl = node.querySelector('code') || node;
                const langEl = node.querySelector('.code-block-decoration span, [class*="language-"]');
                let lang = langEl ? (langEl.textContent || '').trim().toLowerCase() : '';
                if (!lang && codeEl.className) {
                    const m = codeEl.className.match(/language-([a-z0-9_+-]+)/i);
                    if (m) lang = m[1].toLowerCase();
                }
                const codeText = (codeEl.innerText || codeEl.textContent || '').replace(/^\s+|\s+$/g, '');
                return `\n\`\`\`${lang}\n${codeText}\n\`\`\`\n\n`;
            }

            // Tabellen -> echtes Markdown
            if (tag === 'table') {
                const rows = Array.from(node.querySelectorAll('tr'));
                if (rows.length === 0) return '';
                const mdRows = [];
                rows.forEach((tr, idx) => {
                    const cells = Array.from(tr.querySelectorAll('th, td')).map(c =>
                        (c.innerText || c.textContent || '').replace(/\s+/g, ' ').trim().replace(/\|/g, '\\|')
                    );
                    if (cells.length === 0) return;
                    mdRows.push(`| ${cells.join(' | ')} |`);
                    if (idx === 0) {
                        mdRows.push(`| ${cells.map(() => '---').join(' | ')} |`);
                    }
                });
                return `\n${mdRows.join('\n')}\n\n`;
            }

            // Listen
            if (tag === 'ul' || tag === 'ol') {
                let idx = 1;
                const items = [];
                for (const child of node.children) {
                    if (child.tagName && child.tagName.toLowerCase() === 'li') {
                        const prefix = tag === 'ol' ? `${idx++}. ` : '- ';
                        const indent = '  '.repeat(listDepth);
                        let inner = '';
                        for (const gc of child.childNodes) {
                            inner += walk(gc, listDepth + 1);
                        }
                        items.push(`${indent}${prefix}${inner.trim()}`);
                    }
                }
                return (listDepth === 0 ? '\n' : '') + items.join('\n') + (listDepth === 0 ? '\n\n' : '\n');
            }

            let inner = '';
            for (const child of node.childNodes) {
                inner += walk(child, listDepth);
            }

            if (tag === 'br') return '\n';
            if (tag === 'hr') return '\n---\n\n';
            if (/^h[1-6]$/.test(tag)) {
                const lvl = parseInt(tag[1], 10);
                return `\n${'#'.repeat(lvl)} ${inner.trim()}\n\n`;
            }
            if (tag === 'strong' || tag === 'b') {
                const t = inner.trim();
                return t ? `**${t}**` : '';
            }
            if (tag === 'em' || tag === 'i') {
                const t = inner.trim();
                return t ? `*${t}*` : '';
            }
            if (tag === 'code') {
                const t = inner.trim();
                return t ? `\`${t}\`` : '';
            }
            if (tag === 'a') {
                const href = node.getAttribute('href') || '';
                const t = inner.trim() || href;
                return href ? `[${t}](${href})` : t;
            }
            if (tag === 'blockquote') {
                return '\n' + inner.trim().split('\n').map(l => `> ${l}`).join('\n') + '\n\n';
            }
            if (tag === 'p' || tag === 'div') {
                const t = inner.trim();
                return t ? `${t}\n\n` : '';
            }
            return inner;
        }

        // Falls ein eigener Markdown-Content-Container in model-response existiert, bevorzugt nutzen
        const contentRoot = root.querySelector('.markdown, message-content, .model-response-text') || root;
        let md = walk(contentRoot, 0);
        md = md.replace(/\n{3,}/g, '\n\n').trim();
        return md || (root.innerText || '').trim();
    }

    function getResponseNodes() {
        if (ACTIVE_PROVIDER === 'claude') {
            return Array.from(document.querySelectorAll('[data-testid="assistant-message"]'));
        }
        return document.querySelectorAll('model-response');
    }

    function getUserMessageCount() {
        if (ACTIVE_PROVIDER === 'claude') {
            return document.querySelectorAll('[data-testid="user-message"]').length;
        }
        return 0;
    }

    function waitForGeminiFinished(initialCount, onDone, maxWaitSeconds = 300, onStuckRetry = null, initialUserCount = 0, initialLastNode = null) {
        const startTime = Date.now();
        let lastLength = 0;
        let stableTicks = 0;
        let loggedWaitDebug = false;
        let stuckRetryFired = false;

        const timer = setInterval(() => {
            const elapsed = (Date.now() - startTime) / 1000;
            if (elapsed > maxWaitSeconds) {
                clearInterval(timer);
                onDone(null, 'Zeitüberschreitung');
                return;
            }

            const responses = getResponseNodes();
            const curUserCount = getUserMessageCount();
            const claudeSent = ACTIVE_PROVIDER !== 'claude' ? false : (curUserCount > initialUserCount);
            const hasNewResp = responses.length > initialCount || (responses.length > 0 && initialLastNode && responses[responses.length - 1] !== initialLastNode);
            if (!stuckRetryFired && elapsed >= 15 && !hasNewResp && !claudeSent && !getStopButton() && getInputTextLength(getInputElement()) === 0) {
                stuckRetryFired = true;
                if (typeof onStuckRetry === 'function') {
                    logEventToBridge(getTabRole(), 1, '🔄 Auto-Retry nach stummem Sende-Drop (>15s)', `provider=${ACTIVE_PROVIDER} role=${getTabRole()} focus=${document.hasFocus()} aMsg=${responses.length}/${initialCount} uMsg=${curUserCount}/${initialUserCount}`);
                    onStuckRetry();
                }
            }
            if (elapsed >= 32 && !hasNewResp && !claudeSent && !getStopButton() && getInputTextLength(getInputElement()) === 0) {
                clearInterval(timer);
                const tailTxt = (document.querySelector('main')?.innerText || '').trim().slice(-140).replace(/\s+/g, ' ');
                logEventToBridge(getTabRole(), 1, '❌ Keine Antwort nach Auto-Retry (32s)', `provider=${ACTIVE_PROVIDER} role=${getTabRole()} focus=${document.hasFocus()} aMsg=${responses.length}/${initialCount} uMsg=${curUserCount}/${initialUserCount} method=${lastInputMethod} send=${lastSendDiag} tail="${tailTxt}"`);
                onDone(null, `Keine Antwort nach Auto-Retry (focus=${document.hasFocus()}, method=${lastInputMethod})`);
                return;
            }
            if (!loggedWaitDebug && elapsed >= 15) {
                loggedWaitDebug = true;
                const stopNow = Boolean(getStopButton());
                const inLen = (getInputElement()?.innerText || getInputElement()?.value || '').trim().length;
                const tailTxt = (document.querySelector('main')?.innerText || '').trim().slice(-140).replace(/\s+/g, ' ');
                logEventToBridge(getTabRole(), 1, 'Warte-Diagnose (>15s)', `provider=${ACTIVE_PROVIDER} role=${getTabRole()} focus=${document.hasFocus()} method=${lastInputMethod} send=${lastSendDiag} responses=${responses.length}/${initialCount} uMsg=${curUserCount}/${initialUserCount} stop=${stopNow} lastLen=${lastLength} inputLen=${inLen} tail="${tailTxt}"`);
            }
            if (hasNewResp) {
                const latest = responses[responses.length - 1];
                const stopBtn = getStopButton();
                const isThinking = ACTIVE_PROVIDER === 'claude'
                    ? Boolean(latest.closest('[data-is-streaming="true"]'))
                    : Boolean(latest.querySelector('thinking-dots-animation, [class*="animating"]'));
                const contentEl = ACTIVE_PROVIDER === 'claude'
                    ? (latest.querySelector('.standard-markdown, .progressive-markdown, .font-claude-response-body') || latest)
                    : latest;
                let text = domToMarkdown(contentEl);
                text = text
                    .replace(/^(?:###?\s*)?(?:Gemini|Claude)\s+(?:hat\s+gesagt|said):?[^\n]*\n+/i, '')
                    .replace(/\n+(?:jetzt|just now|vor\s+\d+\s+\w+)\s*$/i, '')
                    .trim();

                if (!stopBtn && !isThinking && text.trim().length > 0) {
                    if (text.length === lastLength) {
                        stableTicks++;
                        if (stableTicks >= 2) {
                            clearInterval(timer);
                            onDone(text, null);
                        }
                    } else {
                        lastLength = text.length;
                        stableTicks = 0;
                    }
                } else {
                    lastLength = text.length;
                    stableTicks = 0;
                }
            }
        }, 500);
    }

    function sendPromptAndAwaitResponse(promptText, callback) {
        const wrappedCallback = (text, err) => {
            if (text && checkRateLimitInText(text)) {
                const oldModel = detectCurrentBrowserModel() || 'Aktuelles Modell';
                const cd = parseCooldownFromText(text);
                updateStatus(`⚠️ Limit auf ${oldModel} (bis ${cd.retry_after_str})! Wechsle auf Flash...`, 'error');
                reportModelStatus(
                    `Limit auf ${oldModel} (bis ${cd.retry_after_str}) – Auto-Fallback auf Flash`,
                    {
                        limited_model: oldModel,
                        retry_after_ts: cd.retry_after_ts,
                        retry_after_str: cd.retry_after_str,
                        reason: text.slice(0, 120)
                    }
                );
                logEventToBridge('reviewer', debateState.round || 1, `⚠️ Nutzungslimit auf ${oldModel} erkannt (bis ${cd.retry_after_str})`, text);

                switchGeminiModelInDOM('Flash', (ok, newMod) => {
                    if (ok) {
                        updateStatus(`🔄 Umgestellt auf ${newMod}. Sende Aufgabe erneut...`, 'running');
                        sendPromptAndAwaitResponse(promptText, callback);
                    } else {
                        callback(text, err);
                    }
                });
                return;
            }
            callback(text, err);
        };

        const triggerStuckRetry = () => {
            setGeminiInput(promptText);
            setTimeout(() => { clickSendButton(); }, 450);
        };

        setTimeout(() => {
            const initNodes = getResponseNodes();
            const initialCount = initNodes.length;
            const initialLastNode = initialCount > 0 ? initNodes[initialCount - 1] : null;
            const initialUserCount = getUserMessageCount();
            setGeminiInput(promptText);

            let attempts = 0;
            let clickTriggered = false;
            let postSubmitTicks = 0;
            const retryTimer = setInterval(() => {
                attempts++;
                const curNodes = getResponseNodes();
                const newCount = curNodes.length;
                const hasNewNode = newCount > initialCount || (newCount > 0 && initialLastNode && curNodes[newCount - 1] !== initialLastNode);
                const hasStop = getStopButton();
                const curLen = getInputTextLength(getInputElement());

                if (hasNewNode || (hasStop && clickTriggered && curLen === 0)) {
                    clearInterval(retryTimer);
                    waitForGeminiFinished(initialCount, wrappedCallback, 180, triggerStuckRetry, initialUserCount, initialLastNode);
                    return;
                }

                if (curLen === 0 && !clickTriggered && (attempts === 1 || attempts === 2 || attempts === 4 || attempts === 8)) {
                    setGeminiInput(promptText, attempts >= 6 ? 'composition' : 'auto');
                }

                if (getInputTextLength(getInputElement()) > 0 && !clickTriggered) {
                    if (clickSendButton()) {
                        clickTriggered = true;
                    }
                }

                if (ACTIVE_PROVIDER === 'claude' && !clickTriggered && attempts >= 2 && lastSendDiag.startsWith('disabled:')) {
                    const bodyTxt = (document.body?.innerText || '');
                    const limMatch = bodyTxt.match(/(?:Nutzungsbegrenzung|mach jetzt mit Pro weiter|später wieder verfügbar|usage limit|limit reached|Chat[-\s]?Limit)/i);
                    if (limMatch) {
                        clearInterval(retryTimer);
                        const mod = detectCurrentBrowserModel() || 'Claude';
                        const limMsg = `Claude Free-Plan Nutzungslimit erreicht (${limMatch[0]})`;
                        reportModelStatus(limMsg, {
                            limited_model: mod,
                            retry_after_ts: Math.floor(Date.now() / 1000) + 1800,
                            retry_after_str: new Date(Date.now() + 1800000).toTimeString().slice(0, 5),
                            reason: limMsg
                        });
                        logEventToBridge(getTabRole(), 1, '⚠️ ' + limMsg, limMsg);
                        wrappedCallback(null, limMsg);
                        return;
                    }
                }

                const afterNodes = getResponseNodes();
                const afterCount = afterNodes.length;
                const afterNew = afterCount > initialCount || (afterCount > 0 && initialLastNode && afterNodes[afterCount - 1] !== initialLastNode);
                const afterStop = getStopButton();
                const hasTextStill = getInputTextLength(getInputElement()) > 0;
                if (afterNew || (afterStop && clickTriggered && !hasTextStill)) {
                    clearInterval(retryTimer);
                    waitForGeminiFinished(initialCount, wrappedCallback, 180, triggerStuckRetry, initialUserCount, initialLastNode);
                } else if (clickTriggered && !hasTextStill) {
                    postSubmitTicks++;
                    if (postSubmitTicks >= 2) {
                        clearInterval(retryTimer);
                        waitForGeminiFinished(initialCount, wrappedCallback, 180, triggerStuckRetry, initialUserCount, initialLastNode);
                    }
                } else if (attempts >= 18) {
                    clearInterval(retryTimer);
                    const finalInLen = getInputTextLength(getInputElement());
                    const msg = `Senden fehlgeschlagen (provider=${ACTIVE_PROVIDER}, role=${getTabRole()}, focus=${document.hasFocus()}, method=${lastInputMethod}, send=${lastSendDiag}, inputLen=${finalInLen}, clicked=${clickTriggered})`;
                    logEventToBridge(getTabRole(), 1, '❌ Senden fehlgeschlagen', msg);
                    wrappedCallback(null, msg);
                }
            }, 500);
        }, 550);
    }

    function extractCodeBlocks(markdown) {
        const blocks = [];
        // 1. Zuerst aus echtem DOM extrahieren (<pre><code>)
        const responses = getResponseNodes();
        if (responses.length > 0) {
            const latest = responses[responses.length - 1];
            const preElems = latest.querySelectorAll('pre');
            for (const pre of preElems) {
                const codeElem = pre.querySelector('code') || pre;
                const codeText = (codeElem.innerText || codeElem.textContent || '').trim();
                if (codeText.length > 15) {
                    let lang = 'python';
                    const cls = (codeElem.className || '') + ' ' + (pre.className || '');
                    if (cls.includes('bash') || cls.includes('sh')) lang = 'bash';
                    else if (cls.includes('js') || cls.includes('node')) lang = 'javascript';
                    blocks.push({ lang: lang, code: codeText });
                }
            }
        }

        // 2. Fallback: Markdown-Regex
        if (blocks.length === 0) {
            const regex = /```([a-zA-Z0-9_\-]+)?\n([\s\S]*?)```/g;
            let match;
            while ((match = regex.exec(markdown)) !== null) {
                blocks.push({
                    lang: (match[1] || 'python').toLowerCase(),
                    code: match[2].trim()
                });
            }
        }
        return blocks;
    }

    let debateState = {
        active: false,
        task: '',
        round: 1,
        maxRounds: 2,
        checkTerminal: true
    };

    function updateStatus(text, type = 'normal') {
        const statusEl = document.getElementById('gda-status');
        if (!statusEl) return;
        statusEl.textContent = text;
        if (type === 'running') {
            statusEl.style.color = '#8ab4f8';
            statusEl.style.background = 'rgba(74, 136, 232, 0.15)';
        } else if (type === 'success') {
            statusEl.style.color = '#81c995';
            statusEl.style.background = 'rgba(52, 168, 83, 0.15)';
        } else if (type === 'error') {
            statusEl.style.color = '#f28b82';
            statusEl.style.background = 'rgba(234, 67, 53, 0.15)';
        } else {
            statusEl.style.color = '#94a3b8';
            statusEl.style.background = 'rgba(255, 255, 255, 0.05)';
        }
    }

    function updateBridgeIndicator() {
        const ind = document.getElementById('gda-bridge-ind');
        if (!ind) return;
        if (bridgeOnline) {
            ind.textContent = '🟢 Bridge aktiv';
            ind.style.borderColor = '#1e8e3e';
            ind.style.color = '#81c995';
        } else {
            ind.textContent = '⚪ Bridge offline';
            ind.style.borderColor = '#5f6368';
            ind.style.color = '#9aa0a6';
        }
    }

    let pendingWorkerReviewCb = null;
    if (agentBus) {
        agentBus.onmessage = (event) => {
            const data = event.data;
            if (!data) return;

            const myRole = getTabRole();

            if (myRole === 'reviewer' && data.type === 'NEW_REVIEWER_CHAT' && (!data.provider || data.provider === ACTIVE_PROVIDER)) {
                updateStatus('🆕 Starte neuen Reviewer-Chat...', 'running');
                setTimeout(() => {
                    window.location.href = (ACTIVE_PROVIDER === 'claude' ? '/new' : '/app');
                }, 100);
                return;
            }

            if (myRole === 'reviewer' && data.type === 'WORKER_REVIEW_REQ') {
                updateStatus('🧐 Prüfe Worker-Antwort...', 'running');
                logEventToBridge('reviewer', 1, 'Anti-Halluzinations-Check gestartet', data.draft.slice(0, 400));
                const checkPrompt =
                    `[Rolle: Pragmatischer Anti-Halluzinations-Reviewer]\n` +
                    `Prüfe diese Antwort des Workers ausschließlich auf echte Halluzinationen:\n\n` +
                    `--- ANFRAGE ---\n${data.task}\n\n` +
                    `--- WORKER-ANTWORT ---\n${data.draft}\n\n` +
                    `Prüfregeln:\n` +
                    `1. Als BELEGT gilt alles, was in einem [Tool-Ergebnis] ODER bereits in der --- ANFRAGE --- steht (inkl. genannter Zahlen, Pfade und Zusammenfassungen).\n` +
                    `2. Blockiere NUR, wenn der Worker konkrete Messwerte, Dateipfade, Dienstnamen, Exit-Codes oder Ergebnisse frei erfunden hat ODER bei fehlenden Live-Systemdaten kein Tool aufgerufen hat (z. B. wenn er fälschlich behauptet, keine Verbindung/nur eine Sandbox zu haben, statt {"tool_call": ...} auszugeben).\n` +
                    `3. VERBOTEN: Stilkritik, Erbsenzählerei zu Formulierungen/Shell-Flags (z. B. ob grep/cat genutzt wurde) oder Rückfragen zu Details aus der Anfrage.\n` +
                    `Wenn keine grobe Halluzination vorliegt: Antworte AUSSCHLIESSLICH mit genau diesem Wort: VERIFIED_OK\n` +
                    `Nur bei echter Halluzination: Nenne in maximal 2 knappen Sätzen direkt den konkreten Fehler (ohne "Weise den Worker an...").`;

                sendPromptAndAwaitResponse(checkPrompt, (reviewRes, err) => {
                    const out = err ? 'VERIFIED_OK' : (reviewRes || 'VERIFIED_OK');
                    updateStatus(out.includes('VERIFIED_OK') ? '✅ Freigegeben' : '⚠️ Kritik an Worker gesendet', out.includes('VERIFIED_OK') ? 'success' : 'error');
                    logEventToBridge('reviewer', 1, 'Worker-Check Ergebnis', out);
                    agentBus.postMessage({ type: 'WORKER_REVIEW_RES', review: out });
                });
                return;
            }

            if (isWorkerRole(myRole) && data.type === 'WORKER_REVIEW_RES' && pendingWorkerReviewCb) {
                const cb = pendingWorkerReviewCb;
                pendingWorkerReviewCb = null;
                cb(data.review || 'VERIFIED_OK');
                return;
            }

            if (myRole === 'reviewer' && data.type === 'CODE_FOR_REVIEW') {
                updateStatus(`Runde ${data.round}: Prüfe Entwurf von Agent 1...`, 'running');
                logEventToBridge('reviewer', data.round, `Review gestartet für Entwurf Runde ${data.round}`, 'Prüfe Code auf Fehler und Optimierungspotenziale...');

                const reviewPrompt = `[Rolle: Senior Staff Reviewer & QA Auditor (Multi-Agent Tandem)]\n` +
                    `Dein Entwickler-Partner hat folgenden Entwurf für eine Mission vorgelegt:\n\n` +
                    `${data.content}\n\n` +
                    `Deine Prüfaufgabe:\n` +
                    `1. Analysiere den Entwurf akribisch auf Syntaxfehler, Logikmängel, Edge Cases, Performance und Clean Code.\n` +
                    `2. WICHTIGE REGEL FÜR DIE ENTSCHEIDUNG:\n` +
                    `- Falls der Entwurf absolut fehlerfrei, robust und einsatzbereit ist, antworte an ALLERERSTER STELLE GENAU mit dem Codewort: VERIFIED_PERFECT\n` +
                    `- Falls Mängel, Lücken oder Verbesserungen nötig sind: Liste alle Kritikpunkte präzise und nummeriert auf, damit dein Partner den Code direkt nachbessern kann.`;

                sendPromptAndAwaitResponse(reviewPrompt, (reviewText, err) => {
                    if (err) {
                        updateStatus('Fehler beim Review: ' + err, 'error');
                        return;
                    }

                    const isPerfect = reviewText.includes('VERIFIED_PERFECT');
                    updateStatus(isPerfect ? '🎉 Review bestanden! Sende Freigabe...' : `Runde ${data.round}: Kritik gesendet...`, isPerfect ? 'success' : 'normal');

                    logEventToBridge('reviewer', data.round, isPerfect ? '✅ Freigabe erteilt (VERIFIED_PERFECT)' : `Kritikpunkte Runde ${data.round}`, reviewText);

                    agentBus.postMessage({
                        type: 'REVIEW_COMPLETED',
                        round: data.round,
                        isPerfect: isPerfect,
                        critique: reviewText
                    });
                });
            }

            if (myRole === 'coder' && data.type === 'REVIEW_COMPLETED' && debateState.active) {
                if (data.isPerfect) {
                    debateState.active = false;
                    updateStatus('🎉 Reviewer gibt grünes Licht! Debatte erfolgreich.', 'success');
                    logEventToBridge('final', debateState.round, '🎉 Debatte erfolgreich abgeschlossen', 'Der Reviewer hat die Freigabe (VERIFIED_PERFECT) erteilt. Der Code ist einsatzbereit!');
                    const startBtn = document.getElementById('gda-start-btn');
                    if (startBtn) startBtn.disabled = false;
                    return;
                }

                if (debateState.round >= debateState.maxRounds) {
                    updateStatus(`Runde ${debateState.round} (Final): Erstelle finale bereinigte Version...`, 'running');
                    const finalPrompt = `[Rolle: Leitender Entwickler (Finale Abschluss-Version)]\n` +
                        `Hier ist das abschließende Review deines Prüfers:\n\n` +
                        `${data.critique}\n\n` +
                        `Erstelle nun bitte die endgültige, vollständige und fehlerfreie Finalversion ohne weitere Fragen.`;

                    sendPromptAndAwaitResponse(finalPrompt, (finalResult) => {
                        debateState.active = false;
                        updateStatus('✅ Finale Version erstellt! Debatte beendet.', 'success');
                        logEventToBridge('final', debateState.round, '🏆 Finale Version fertiggestellt', finalResult || 'Debatte beendet.');
                        const startBtn = document.getElementById('gda-start-btn');
                        if (startBtn) startBtn.disabled = false;
                    });
                } else {
                    debateState.round++;
                    updateStatus(`Runde ${debateState.round}/${debateState.maxRounds}: Coder behebt Kritik...`, 'running');

                    const fixPrompt = `[Rolle: Leitender Entwickler (Nachbesserungs-Runde ${debateState.round})]\n` +
                        `Dein Reviewer hat folgende Kritikpunkte an deinem vorherigen Entwurf geäußert:\n\n` +
                        `${data.critique}\n\n` +
                        `Anweisung:\n` +
                        `Behebe alle genannten Kritikpunkte vollständig und gib den überarbeiteten, vollständigen Code aus.`;

                    sendPromptAndAwaitResponse(fixPrompt, (newCode, err) => {
                        if (err) {
                            updateStatus('Fehler: ' + err, 'error');
                            debateState.active = false;
                            return;
                        }

                        logEventToBridge('coder', debateState.round, `Code nachgebessert (Runde ${debateState.round})`, newCode);
                        proceedWithCode(newCode, debateState.round);
                    });
                }
            }
        };
    }

    function proceedWithCode(codeText, round) {
        const blocks = extractCodeBlocks(codeText);

        if (debateState.checkTerminal && bridgeOnline && blocks.length > 0) {
            updateStatus(`Runde ${round}: Führe Code auf Linux-Terminal aus...`, 'running');

            const primaryBlock = blocks[0];
            testCodeOnTerminal(primaryBlock.lang, primaryBlock.code, (res) => {
                if (res && !res.valid && res.stderr) {
                    updateStatus(`⚠️ Fehler beim Ausführen! Lasse Coder korrigieren...`, 'error');

                    const terminalFixPrompt = `[Rolle: Leitender Entwickler (Terminal-Fehlerbehebung)]\n` +
                        `Beim Ausführen deines Skripts auf the user's Linux-System ist folgender Fehler aufgetreten:\n\n` +
                        `${res.stderr}\n\n` +
                        `Bitte korrigiere den Fehler sofort und gib den vollständigen, bereinigten Code aus.`;

                    sendPromptAndAwaitResponse(terminalFixPrompt, (fixedCode) => {
                        proceedWithCode(fixedCode, round);
                    });
                    return;
                }

                // Ausführung war erfolgreich! Echte Systemdaten sind da!
                const output = (res && res.stdout) ? res.stdout.trim() : 'Code erfolgreich ausgeführt.';
                logEventToBridge('terminal', round, `Echte Systemausgabe erhalten (${res.duration_ms}ms)`, output);

                // ReAct-Loop: Terminal-Daten interpretieren und finale Antwort formulieren!
                updateStatus('Formuliere finale Antwort aus echten Systemdaten...', 'running');

                const interpretPrompt = `[Rolle: Leitender Entwickler (Missions-Abschluss)]\n` +
                    `Dein Skript wurde erfolgreich auf the user's Linux-System ausgeführt!\n` +
                    `Hier ist die reale Terminal-Ausgabe deines Skripts:\n\n` +
                    `\`\`\`\n${output}\n\`\`\`\n\n` +
                    `Mission von User:\n"${debateState.task}"\n\n` +
                    `Deine Aufgabe:\n` +
                    `Analysiere diese echten Messwerte und beantworte the user's Mission nun direkt, übersichtlich und verständlich. Präsentiere ihm die fertige Zusammenfassung seiner Komponenten und Daten (kein weiterer Code erforderlich).`;

                sendPromptAndAwaitResponse(interpretPrompt, (finalAnswer) => {
                    logEventToBridge('final', round, '🏆 Finale Antwort an User', finalAnswer);
                    finishMissionOnServer(finalAnswer);
                    updateStatus('✅ Mission erfolgreich abgeschlossen!', 'success');
                    debateState.active = false;
                    const startBtn = document.getElementById('gda-start-btn');
                    if (startBtn) startBtn.disabled = false;
                });
            });
        } else {
            sendToReviewer(codeText, round);
        }
    }

    function sendToReviewer(codeText, round) {
        updateStatus(`Runde ${round}: Entwurf an Tab 2 (Reviewer) gesendet...`, 'running');
        if (agentBus) {
            agentBus.postMessage({
                type: 'CODE_FOR_REVIEW',
                round: round,
                content: codeText
            });
        }
    }

    function startDebate(taskText, rounds, checkTerminal) {
        if (!taskText.trim()) return;
        debateState.active = true;
        debateState.task = taskText;
        debateState.round = 1;
        debateState.maxRounds = rounds;
        debateState.checkTerminal = checkTerminal;

        const startBtn = document.getElementById('gda-start-btn');
        if (startBtn) startBtn.disabled = true;

        // Direkter Worker-Modus (mit optionalem Anti-Halluzinations-Check durch Reviewer-Tab)
        if (getTabRole() === 'worker' || rounds === 1) {
            const finishWorker = (finalTxt) => {
                debateState.active = false;
                if (startBtn) startBtn.disabled = false;
                updateStatus('✅ Antwort an Bridge gesendet', 'success');
                logEventToBridge('coder', 1, 'Worker-Antwort fertiggestellt', finalTxt);
                finishMissionOnServer(finalTxt);
            };

            updateStatus('🤖 Worker bearbeitet Anfrage...', 'running');
            logEventToBridge('coder', 1, 'Worker-Anfrage gestartet', taskText, taskText);

            sendPromptAndAwaitResponse(taskText, (resText, err) => {
                if (err) {
                    debateState.active = false;
                    if (startBtn) startBtn.disabled = false;
                    updateStatus('Fehler: ' + err, 'error');
                    logEventToBridge('coder', 1, 'Fehler bei Worker-Anfrage', err);
                    failMissionOnServer(err);
                    return;
                }

                // Wenn bereits ein tool_call ausgegeben wurde oder kein Reviewer aktiv ist: sofort fertig!
                if (resText.includes('"tool_call"') || !hasActiveReviewer || !agentBus) {
                    finishWorker(resText);
                    return;
                }

                updateStatus('🧐 Warte auf Reviewer-Freigabe...', 'running');
                logEventToBridge('coder', 1, 'Entwurf an Reviewer zur Prüfung', resText);
                let reviewHandled = false;
                const reviewTimeoutId = setTimeout(() => {
                    if (!reviewHandled && pendingWorkerReviewCb) {
                        reviewHandled = true;
                        pendingWorkerReviewCb = null;
                        logEventToBridge('reviewer', 1, '⚠️ Reviewer-Timeout (45s) – Fallback VERIFIED_OK', 'Keine rechtzeitige Rückmeldung vom Reviewer-Tab');
                        finishWorker(resText);
                    }
                }, 45000);
                pendingWorkerReviewCb = (reviewFeedback) => {
                    if (reviewHandled) return;
                    reviewHandled = true;
                    clearTimeout(reviewTimeoutId);
                    if (/VERIFIED_OK/i.test(reviewFeedback)) {
                        finishWorker(resText);
                    } else {
                        updateStatus('🔧 Worker bessert nach...', 'running');
                        logEventToBridge('coder', 1, '🔧 Worker bessert nach', reviewFeedback);
                        const fixPrompt =
                            `Dein Entwurf:\n${resText}\n\n` +
                            `Korrektur-Anforderung:\n${reviewFeedback}\n\n` +
                            `Gib jetzt direkt die korrigierte, saubere finale Antwort für den Nutzer aus (oder rufe bei fehlenden Daten ein Tool per {"tool_call": ...} auf). Keine Meta-Kommentare.`;
                        sendPromptAndAwaitResponse(fixPrompt, (fixedText, fixErr) => {
                            finishWorker(fixErr ? resText : (fixedText || resText));
                        });
                    }
                };
                agentBus.postMessage({ type: 'WORKER_REVIEW_REQ', task: taskText, draft: resText });
            });
            return;
        }

        updateStatus('Runde 1: Agent 1 (Coder) entwirft Lösung...', 'running');

        const initialPrompt = `[Rolle: Leitender Entwickler & System-Architekt (Multi-Agent Tandem)]\n` +
            `Mission: ${taskText}\n\n` +
            `Anweisungen:\n` +
            `- Erstelle hierfür eine vollständige, sofort lauffähige und professionelle Lösung.\n` +
            `- Platziere allen ausführbaren Code in sauber formatierte Markdown-Codeblöcke.\n` +
            `- Keine Dummy-Platzhalter wie 'TODO' oder unvollständige Abschnitte.\n` +
            `- Dein Entwurf wird im nächsten Schritt automatisch von deinem Partner-Agenten (Senior QA/Reviewer) geprüft und optimiert.`;

        logEventToBridge('coder', 1, 'Missionsstart: Entwurf wird erstellt', initialPrompt, taskText);

        sendPromptAndAwaitResponse(initialPrompt, (codeResult, err) => {
            if (err) {
                updateStatus('Fehler beim Entwurf: ' + err, 'error');
                logEventToBridge('coder', 1, 'Fehler beim Entwurf', err);
                debateState.active = false;
                if (startBtn) startBtn.disabled = false;
                return;
            }

            logEventToBridge('coder', 1, 'Code-Entwurf (Runde 1) fertiggestellt', codeResult);
            proceedWithCode(codeResult, 1);
        });
    }

    /* ========================================================
       7. UI STATUSLEISTE & KONTROLLEN (OHNE INNERHTML / 100% SICHER)
       ======================================================== */
    function injectBar() {
        try {
            if (!document.body) return;

            if (document.body.contains(document.getElementById('gda-bar'))) {
                return;
            }

            const oldBar = document.getElementById('gda-bar');
            if (oldBar) oldBar.remove();

            const currentRole = getTabRole();
            const isMinimized = sessionStorage.getItem('gda_minimized') === '1';

            const bridgeInd = el('div', {
                id: 'gda-bridge-ind',
                textContent: 'Prüfe...',
                title: 'Bridge-Status (klicken zum Prüfen)',
                style: 'font-size: 11px; padding: 3px 7px; border-radius: 10px; border: 1px solid #3d4d63; background: rgba(255,255,255,0.04); cursor: pointer; white-space: nowrap; flex-shrink: 0;'
            });

            const roleSelect = el('select', {
                id: 'gda-role-select',
                title: 'Rolle / Fähigkeit dieses Browser-Tabs',
                style: 'background: #1a202c; border: 1px solid #3d4d63; color: #f1f5f9; border-radius: 10px; padding: 3px 7px; font-size: 11px; outline: none; cursor: pointer; flex-shrink: 0;'
            }, [
                el('option', { value: 'solo', textContent: '⚪ Solo', selected: currentRole === 'solo' }),
                el('option', { value: 'worker', textContent: '🤖 Worker', selected: currentRole === 'worker' }),
                el('option', { value: 'coder', textContent: '💻 Coder', selected: currentRole === 'coder' }),
                el('option', { value: 'image', textContent: '🎨 Bild', selected: currentRole === 'image' }),
                el('option', { value: 'canvas', textContent: '📝 Canvas', selected: currentRole === 'canvas' }),
                el('option', { value: 'research', textContent: '🔍 Research', selected: currentRole === 'research' }),
                el('option', { value: 'reviewer', textContent: '🧐 Reviewer', selected: currentRole === 'reviewer' })
            ]);

            const detectedInit = detectCurrentBrowserModel();
            const modelBadge = el('div', {
                id: 'gda-model-badge',
                textContent: detectedInit ? `🤖 ${detectedInit}` : '🤖 Modell?',
                title: 'Erkanntes Gemini-Modell in diesem Tab (klicken zum Aktualisieren)',
                style: 'font-size: 11px; padding: 3px 7px; border-radius: 10px; border: 1px solid #3d4d63; background: #1a2230; color: #93c5fd; cursor: pointer; white-space: nowrap; max-width: 130px; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0;'
            });
            modelBadge.addEventListener('click', () => {
                const m = detectCurrentBrowserModel();
                modelBadge.textContent = m ? `🤖 ${m}` : '🤖 nicht erkannt';
                reportModelStatus();
            });

            const statusDiv = el('div', {
                id: 'gda-status',
                textContent: currentRole === 'reviewer' ? '👂 Wartet...' : 'Bereit',
                style: 'font-size: 11px; padding: 3px 7px; border-radius: 10px; background: rgba(255,255,255,0.05); color: #94a3b8; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;'
            });

            const dashBtn = el('button', {
                id: 'gda-dash-btn',
                textContent: '📊',
                title: 'Python-Dashboard öffnen (Aufgaben, Runden, Modellwechsel)',
                style: 'background: #1f293d; color: #8ab4f8; border: 1px solid #3d4d63; border-radius: 9px; padding: 3px 7px; font-size: 11px; font-weight: 600; cursor: pointer; white-space: nowrap; flex-shrink: 0;'
            });
            dashBtn.addEventListener('click', () => {
                window.open('http://127.0.0.1:8765/dashboard', '_blank');
            });

            const minBtn = el('button', {
                id: 'gda-min-btn',
                textContent: isMinimized ? '➕' : '➖',
                title: isMinimized ? 'Leiste aufklappen' : 'Leiste minimieren',
                style: 'background: rgba(255,255,255,0.06); color: #cbd5e1; border: 1px solid #3d4d63; border-radius: 8px; padding: 2px 6px; font-size: 10px; cursor: pointer; flex-shrink: 0; line-height: 1.2;'
            });

            const applyMinState = (min) => {
                modelBadge.style.display = min ? 'none' : '';
                statusDiv.style.display = min ? 'none' : '';
                dashBtn.style.display = min ? 'none' : '';
                minBtn.textContent = min ? '➕' : '➖';
                minBtn.title = min ? 'Leiste aufklappen' : 'Leiste minimieren';
            };
            applyMinState(isMinimized);

            minBtn.addEventListener('click', () => {
                const next = sessionStorage.getItem('gda_minimized') !== '1';
                sessionStorage.setItem('gda_minimized', next ? '1' : '0');
                applyMinState(next);
            });

            const bar = el('div', {
                id: 'gda-bar',
                style: `
                    position: fixed !important;
                    top: 6px !important;
                    left: 50% !important;
                    transform: translateX(-50%) !important;
                    z-index: 2147483645 !important;
                    background: rgba(17, 20, 26, 0.94) !important;
                    backdrop-filter: blur(8px) !important;
                    border: 1px solid #2d3748 !important;
                    border-radius: 14px !important;
                    padding: 4px 8px !important;
                    display: flex !important;
                    flex-wrap: wrap !important;
                    justify-content: center !important;
                    align-items: center !important;
                    gap: 5px !important;
                    width: max-content !important;
                    max-width: calc(100vw - 140px) !important;
                    box-sizing: border-box !important;
                    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.65) !important;
                    font-family: system-ui, sans-serif !important;
                    font-size: 11.5px !important;
                    color: #e2e8f0 !important;
                    user-select: none !important;
                `
            }, [bridgeInd, roleSelect, modelBadge, statusDiv, dashBtn, minBtn]);

            document.body.appendChild(bar);

            bridgeInd.addEventListener('click', () => {
                bridgeInd.textContent = 'Prüfe...';
                checkBridgeHealth();
            });

            roleSelect.addEventListener('change', (e) => {
                const role = e.target.value;
                setTabRole(role);
                reportModelStatus();
                if (role === 'reviewer') {
                    updateStatus('👂 Wartet...', 'normal');
                } else if (isWorkerRole(role)) {
                    updateStatus('Bereit', 'normal');
                } else {
                    updateStatus('Einzel-Chat', 'normal');
                }
            });

            checkBridgeHealth();
        } catch (e) {
            console.error('Gemini Dual Agent: Fehler beim Injizieren der Leiste:', e);
        }
    }

    try {
        injectBar();
        setInterval(injectBar, 1500);
        setInterval(checkBridgeHealth, 15000);
        setTimeout(autoAssignTabRoleIfUnset, 1500);
        setTimeout(resumePendingInitIfAny, 2500);
        setTimeout(scanGeminiModelsInDOM, 3500);
        setInterval(checkPendingMissions, 2500);
    } catch (e) {
        console.error('Gemini Dual Agent Init-Fehler:', e);
    }
})();
