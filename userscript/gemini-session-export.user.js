// ==UserScript==
// @name         Gemini Session Export (HttpOnly)
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  Exports HttpOnly Gemini cookies to local server via GM_cookie
// @match        https://gemini.google.com/*
// @grant        GM_cookie
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      192.168.1.154
// @run-at       document-idle
// ==/UserScript==

(function() {
    "use strict";

    // Konfiguration: Server-Endpunkt anpassen falls nötig
    const SERVER_URL = "http://192.168.1.154:8766/session/sync";

    function log(msg) {
        console.log("[GeminiSessionExport] " + msg);
    }

    function exportSession() {
        if (typeof GM_cookie === "undefined" || !GM_cookie.list) {
            log("GM_cookie nicht verfügbar. Bitte Tampermonkey Beta oder Violentmonkey nutzen.");
            return;
        }

        log("Lese HttpOnly-Cookies...");
        GM_cookie.list({ url: "https://gemini.google.com" }, function(cookies, error) {
            if (error) {
                log("Fehler beim Lesen der Cookies: " + error);
                return;
            }

            const required = ["__Secure-1PSID", "__Secure-1PSIDTS"];
            const cookieMap = {};
            let snlm0e = null;

            cookies.forEach(c => {
                if (required.includes(c.name)) {
                    cookieMap[c.name] = c.value;
                }
                // SNlM0e ist oft im HTML oder in anderen Cookies/LocalStorage,
                // aber wir versuchen es auch aus Cookies zu holen falls vorhanden
                if (c.name === "SNlM0e") {
                    snlm0e = c.value;
                }
            });

            // Fallback: SNlM0e aus dem DOM extrahieren falls nicht in Cookies
            if (!snlm0e) {
                try {
                    const scripts = document.querySelectorAll("script");
                    for (const s of scripts) {
                        const m = s.textContent.match(/"SNlM0e":"([^"]+)"/);
                        if (m) {
                            snlm0e = m[1];
                            break;
                        }
                    }
                } catch(e) { /* ignore */ }
            }

            const missing = required.filter(k => !cookieMap[k]);
            if (missing.length > 0) {
                log("Fehlende Pflicht-Cookies: " + missing.join(", ") + ". Bitte zuerst bei Google anmelden.");
                return;
            }

            const payload = {
                cookies: cookieMap,
                snlm0e: snlm0e
            };

            log("Sende Session an " + SERVER_URL + " (PSID=***" + cookieMap["__Secure-1PSID"].slice(-4) + ")");

            GM_xmlhttpRequest({
                method: "POST",
                url: SERVER_URL,
                headers: { "Content-Type": "application/json" },
                data: JSON.stringify(payload),
                onload: function(resp) {
                    if (resp.status >= 200 && resp.status < 300) {
                        log("✅ Session erfolgreich synchronisiert! Status: " + resp.status);
                        alert("Gemini Session erfolgreich an Server übertragen!");
                    } else {
                        log("❌ Server-Fehler: " + resp.status + " - " + resp.responseText);
                        alert("Fehler bei Session-Übertragung: " + resp.status);
                    }
                },
                onerror: function(err) {
                    log("❌ Netzwerkfehler: " + JSON.stringify(err));
                    alert("Netzwerkfehler beim Session-Export. Läuft der Server?");
                }
            });
        });
    }

    // Kleinen Button oben rechts einblenden
    function createUI() {
        const btn = document.createElement("button");
        btn.textContent = "🔄 Sync Session";
        btn.style.cssText = "position:fixed;top:10px;right:10px;z-index:99999;padding:8px 16px;background:#1a73e8;color:white;border:none;border-radius:4px;cursor:pointer;font-size:14px;box-shadow:0 2px 4px rgba(0,0,0,0.3);";
        btn.onclick = exportSession;
        btn.onmouseover = () => btn.style.background = "#1557b0";
        btn.onmouseout = () => btn.style.background = "#1a73e8";
        document.body.appendChild(btn);
        log("UI bereit. Klicke \"Sync Session\" nach dem Login.");
    }

    // Warten bis Seite geladen
    if (document.readyState === "complete") {
        createUI();
    } else {
        window.addEventListener("load", createUI);
    }
})();
