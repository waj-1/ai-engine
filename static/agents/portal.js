/* ===========================================================================
   AI agents demo portal — picker + workspace (story card | call panel).

   Data comes from the portal API (/agents/api/catalog, /agents/api/<agent>/cases)
   and is untrusted by convention: every value goes through textContent and is
   never parsed as HTML. Voice uses the shared VoiceLiveClient (static/shared/voice-live.js)
   against the duplex WS; chat streams POST /agents/chat/stream (SSE) and
   round-trips the signed `state` token between turns.
   =========================================================================== */
(function () {
  "use strict";

  // ------------------------------------------------------------------ copy
  var T = {
    en: {
      appName: "AI Agents",
      brandSub: "for {brand}",
      fictionPill: "Fictional company · test data",
      heroTitle: "Meet the agents. Play the customer.",
      heroSub: "Four AI agents working for {brand}, a made-up home-internet and mobile company. Pick one, pick a story, and talk to it — by voice or chat, in English or Arabic.",
      pickerNote: "{brand} is fictional. Every person, account and amount is test data — nothing real is charged, sent or booked.",
      allAgents: "All agents", prevCase: "Previous story", nextCase: "Next story",
      yourStory: "Your story", yourGoal: "Your goal on this call",
      keyFacts: "Your details", saySomething: "Say something like…", plotTwist: "Plot twist to try",
      greatAgent: "What a great agent does",
      fineprint: "Every person, account, number and amount here is fictional test data.",
      sayHintChat: "tap to send", sayHintVoice: "say it out loud",
      modeVoice: "Voice", modeChat: "Chat", accent: "Language & accent", mute: "Mute", unmute: "Unmute", end: "End",
      send: "Send", newChat: "New conversation", inputPlaceholder: "Type as the customer…",
      inbound: "You call", outbound: "Calls you",
      callIn: "Start call", callOut: "Let it call you", hangUp: "Hang up",
      emptyVoiceIn: "Press the call button and start talking. Your lines are on the left.",
      emptyVoiceOut: "Press the button — the agent rings you and opens the call. You play the customer.",
      emptyChatIn: "Type as the customer, or tap a line on the left to send it.",
      emptyChatOut: "The agent is calling. Answer to start the conversation.",
      pickUp: "Answer the call",
      step1: "Pick an agent", step2: "Pick a story — you play the customer", step3: "Call or chat — try the plot twist",
      ready: "Ready", ringing: "Ringing…", inCall: "In call", ended: "Call ended", connecting: "Connecting…",
      agentTyping: "Typing…", agentLabel: "Agent",
      voiceOff: "Voice is not available for this agent right now — chat works.",
      busyNotice: "Hang up first — the story, mode and language can't change during a call.",
      errGeneric: "Something went wrong. Please try again.",
      rateLimited: "Slow down a little — too many messages in a minute.",
      micDenied: "Microphone blocked. Allow it in the browser to talk.",
      you: "You", toolsUsed: "Tools",
      dirIn: "Inbound", dirOut: "Outbound",
      stories: "{n} stories", voiceChat: "Voice + chat", chatOnly: "Chat",
      langs: { en: "English", ar: "Arabic" },
      accents: {
        "en-US": "English · American", "en-GB": "English · British", "en-AU": "English · Australian", "en-IN": "English · Indian",
        "ar-SA": "Arabic · Saudi", "ar-AE": "Arabic · Emirati", "ar-EG": "Arabic · Egyptian", "ar-JO": "Arabic · Levantine",
      },
    },
    ar: {
      appName: "وكلاء الذكاء الاصطناعي",
      brandSub: "لـ {brand}",
      fictionPill: "شركة وهمية · بيانات تجريبية",
      heroTitle: "تعرّف على الوكلاء. وتقمّص دور العميل.",
      heroSub: "أربعة وكلاء ذكاء اصطناعي يشتغلون لـ {brand}، شركة إنترنت منزلي وجوال وهمية. اختر وكيل، واختر قصة، وكلّمه — صوت أو محادثة، بالعربي أو بالإنجليزي.",
      pickerNote: "{brand} شركة وهمية. كل الأشخاص والحسابات والمبالغ بيانات تجريبية — ما فيه شي حقيقي ينخصم أو ينرسل أو ينحجز.",
      allAgents: "كل الوكلاء", prevCase: "القصة السابقة", nextCase: "القصة التالية",
      yourStory: "قصتك", yourGoal: "هدفك من المكالمة",
      keyFacts: "بياناتك", saySomething: "قول شي مثل…", plotTwist: "حركة مفاجئة جرّبها",
      greatAgent: "وش يسوي الوكيل الممتاز",
      fineprint: "كل الأشخاص والحسابات والأرقام والمبالغ هنا بيانات تجريبية وهمية.",
      sayHintChat: "اضغط للإرسال", sayHintVoice: "قلها بصوتك",
      modeVoice: "صوت", modeChat: "محادثة", accent: "اللغة واللهجة", mute: "كتم", unmute: "إلغاء الكتم", end: "إنهاء",
      send: "إرسال", newChat: "محادثة جديدة", inputPlaceholder: "اكتب كأنك العميل…",
      inbound: "أنت تتصل", outbound: "يتصل فيك", outbound_f: "تتصل فيك",
      callIn: "ابدأ المكالمة", callOut: "خلّ الوكيل يتصل فيك", hangUp: "إنهاء المكالمة",
      emptyVoiceIn: "اضغط زر الاتصال وابدأ الكلام. جملك موجودة على الجنب.",
      emptyVoiceOut: "اضغط الزر — الوكيل يتصل فيك ويبدأ المكالمة. وأنت تقمّص دور العميل.",
      emptyChatIn: "اكتب كأنك العميل، أو اضغط جملة من الجنب ترسلها.",
      emptyChatOut: "الوكيل يتصل فيك. ردّ عشان تبدأ المحادثة.",
      pickUp: "رد على المكالمة",
      step1: "اختر وكيل", step2: "اختر قصة — وأنت تقمّص دور العميل", step3: "اتصل أو حادث — وجرّب الحركة المفاجئة",
      ready: "جاهز", ringing: "يرن…", inCall: "في مكالمة", ended: "انتهت المكالمة", connecting: "جاري الاتصال…",
      agentTyping: "يكتب…", agentLabel: "الوكيل",
      voiceOff: "الصوت غير متاح لهذا الوكيل حالياً — المحادثة شغالة.",
      busyNotice: "أنهِ المكالمة أول — ما تتغير القصة ولا الوضع ولا اللغة أثناء المكالمة.",
      errGeneric: "صار خطأ. حاول مرة ثانية.",
      rateLimited: "على راحتك — رسائل كثيرة خلال دقيقة.",
      micDenied: "المايكروفون مقفل. اسمح له من المتصفح عشان تتكلم.",
      you: "أنت", toolsUsed: "الأدوات",
      dirIn: "وارد", dirOut: "صادر",
      stories: "{n} قصص", voiceChat: "صوت + محادثة", chatOnly: "محادثة",
      langs: { en: "الإنجليزية", ar: "العربية" },
      accents: {
        "en-US": "إنجليزي · أمريكي", "en-GB": "إنجليزي · بريطاني", "en-AU": "إنجليزي · أسترالي", "en-IN": "إنجليزي · هندي",
        "ar-SA": "عربي · سعودي", "ar-AE": "عربي · إماراتي", "ar-EG": "عربي · مصري", "ar-JO": "عربي · شامي",
      },
    },
  };

  var AGENT_COPY = {
    support: {
      en: { role: "Customer support", line: "Fixes the Wi-Fi before the second half — and knows when to send a human." },
      ar: { role: "خدمة العملاء", line: "يصلّح الواي فاي قبل الشوط الثاني — ويعرف متى يحوّلك لموظف." },
    },
    inquiries: {
      en: { role: "Plans & business inquiries", line: "Turns “which plan?” into the right plan — prices straight from the catalog." },
      ar: { role: "الباقات واستفسارات الأعمال", line: "يحوّل سؤال «أي باقة؟» للباقة الصح — والأسعار من الكتالوج بالضبط." },
    },
    collection: {
      en: { role: "Payment reminders", line: "Calls about overdue bills — kind, firm, and never says a number before checking it's you." },
      ar: { role: "تذكير بالسداد", line: "تتصل بخصوص الفواتير المتأخرة — لطيفة وحازمة، وما تقول أي رقم قبل ما تتأكد إنه أنت." },
    },
    sales: {
      en: { role: "Offers & upgrades", line: "Calls with one offer that actually fits — and takes “no” for an answer." },
      ar: { role: "العروض والترقيات", line: "يتصل بعرض واحد يناسبك فعلاً — ويقبل «لا» بصدر رحب." },
    },
  };

  // The agent's language & accent (a BCP-47 tag the engine turns into the
  // spoken language and the prompts' dialect). It starts on the page's
  // language — the first tag of that group — and a pick is remembered per
  // page language. Independent of LOCALE: an English page can call in Arabic.
  var SPEECH = { en: ["en-US", "en-GB", "en-AU", "en-IN"], ar: ["ar-SA", "ar-AE", "ar-EG", "ar-JO"] };

  // ----------------------------------------------------------------- state
  var LOCALE = document.body.getAttribute("data-locale") === "ar" ? "ar" : "en";
  var S = {
    catalog: null, agent: null, cases: [], caseIdx: 0, mode: "voice",
    voice: null, voiceLive: false, muted: false, callStarted: 0, timer: null,
    chat: { session: null, token: null, busy: false, started: false },
  };

  function store(key, value) { try { localStorage.setItem("agents_" + key, value); } catch (_) { /* private mode */ } }
  function recall(key) { try { return localStorage.getItem("agents_" + key); } catch (_) { return null; } }
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 3 | 8)).toString(16);
    });
  }
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function brand() {
    var b = S.catalog && S.catalog.brand;
    return b ? (LOCALE === "ar" ? b.name_ar : b.name_en) : "";
  }
  function t(key, vars) {
    var s = (T[LOCALE] && T[LOCALE][key]) || T.en[key] || key;
    vars = vars || {};
    if (typeof s !== "string") return s;
    return s.replace(/\{(\w+)\}/g, function (_, k) {
      if (k === "brand") return brand();
      return vars[k] !== undefined ? String(vars[k]) : "{" + k + "}";
    });
  }
  // Arabic verbs agree with the agent: a `<key>_f` variant wins for a
  // female agent when the current language has one.
  function tg(key, meta, vars) {
    var fem = meta && meta.gender === "f" && T[LOCALE] && T[LOCALE][key + "_f"];
    return t(fem ? key + "_f" : key, vars);
  }
  function callTag() {
    var saved = recall("accent_" + LOCALE);
    var all = SPEECH.en.concat(SPEECH.ar);
    return all.indexOf(saved) >= 0 ? saved : SPEECH[LOCALE][0];
  }
  function callLang() { return callTag().slice(0, 2) === "ar" ? "ar" : "en"; }
  // The lines to say follow the CALL's language (the agent mirrors the
  // caller, so English lines on an Arabic call would flip it to English).
  function sayLines(c) { return (c && (c["say_" + callLang()] || c.say_en)) || []; }
  function loc(obj, base) { return obj ? (obj[base + "_" + LOCALE] || obj[base + "_en"] || "") : ""; }
  function agentMeta(key) {
    var list = (S.catalog && S.catalog.agents) || [];
    for (var i = 0; i < list.length; i++) if (list[i].key === key) return list[i];
    return null;
  }
  // Agents are labelled by role, never by persona name (the catalog still
  // carries persona_* for older clients).
  function roleOf(key) { var c = AGENT_COPY[key] || {}; return ((c[LOCALE] || c.en || {}).role) || ""; }
  function persona(meta) { return meta ? roleOf(meta.key) : ""; }

  // ------------------------------------------------------------- i18n/chrome
  function applyStatic() {
    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute("data-i18n"));
    $("ap-brand-sub").textContent = brand() ? t("brandSub") : "";
    $("ap-input").setAttribute("placeholder", t("inputPlaceholder"));
    document.title = (S.agent ? persona(agentMeta(S.agent)) + " · " : "") + t("appName");
  }

  function setLocale(next) {
    if (next === LOCALE) return;
    if (S.voiceLive) { notice(t("busyNotice")); return; }
    LOCALE = next;
    document.documentElement.lang = next;
    document.documentElement.dir = next === "ar" ? "rtl" : "ltr";
    document.body.setAttribute("data-locale", next);
    store("locale", next);
    var url = new URL(location.href);
    url.searchParams.set("lang", next);
    history.replaceState(history.state, "", url.pathname + url.search);
    applyStatic();
    if (S.agent) { renderWorkspaceChrome(); renderCase(); resetChat(); } else { renderPicker(); }
  }

  // ------------------------------------------------------------------ picker
  function renderPicker() {
    var wrap = $("ap-cards");
    wrap.textContent = "";
    ((S.catalog && S.catalog.agents) || []).forEach(function (a) {
      var copy = AGENT_COPY[a.key] || {};
      var c = copy[LOCALE] || copy.en || {};
      var card = el("a", "ap-card ap-t-" + a.key);
      card.href = "/agents/" + a.key + (LOCALE === "ar" ? "?lang=ar" : "");
      card.addEventListener("click", function (e) { e.preventDefault(); openAgent(a.key, true); });
      var top = el("div", "ap-card-top");
      top.appendChild(el("span", "ap-dir", a.direction === "outbound" ? tg("outbound", a) : t("inbound")));
      card.appendChild(top);
      card.appendChild(el("h2", null, persona(a)));
      card.appendChild(el("p", "ap-card-line", c.line));
      var meta = el("div", "ap-card-meta");
      meta.appendChild(el("span", null, t("stories", { n: a.cases })));
      meta.appendChild(el("span", null, a.voice ? t("voiceChat") : t("chatOnly")));
      card.appendChild(meta);
      var go = el("span", "ap-card-go");
      go.appendChild(el("span", null, LOCALE === "ar" ? "ابدأ" : "Start"));
      go.appendChild(el("span", "ap-flip", "→"));
      card.appendChild(go);
      wrap.appendChild(card);
    });
    $("ap-picker-note").textContent = t("pickerNote");
    var steps = $("ap-steps");
    steps.textContent = "";
    [["1", "step1"], ["2", "step2"], ["3", "step3"]].forEach(function (st) {
      var li = el("li", "ap-howto-step");
      li.appendChild(el("span", "ap-howto-n", st[0]));
      li.appendChild(el("span", null, t(st[1])));
      steps.appendChild(li);
    });
  }

  function showPicker(push) {
    if (S.voiceLive) { notice(t("busyNotice")); return; }
    S.agent = null;
    document.querySelector(".ap-app").classList.remove("is-workspace");
    $("ap-workspace").hidden = true;
    $("ap-picker").hidden = false;
    if (push) history.pushState({}, "", "/agents" + (LOCALE === "ar" ? "?lang=ar" : ""));
    applyStatic();
    renderPicker();
  }

  // --------------------------------------------------------------- workspace
  function openAgent(key, push, caseId) {
    var meta = agentMeta(key);
    if (!meta) { showPicker(false); return; }
    S.agent = key;
    S.cases = [];
    document.querySelector(".ap-app").classList.add("is-workspace");
    $("ap-picker").hidden = true;
    $("ap-workspace").hidden = false;
    if (push) history.pushState({}, "", "/agents/" + key + (LOCALE === "ar" ? "?lang=ar" : ""));
    var mode = recall("mode_" + key) || "voice";
    S.mode = meta.voice ? (mode === "chat" ? "chat" : "voice") : "chat";
    renderWorkspaceChrome();
    applyStatic();
    fetch("/agents/api/" + encodeURIComponent(key) + "/cases", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        if (S.agent !== key) return;
        S.cases = data.cases || [];
        var want = caseId || recall("case_" + key);
        var idx = 0;
        for (var i = 0; i < S.cases.length; i++) if (S.cases[i].id === want) idx = i;
        S.caseIdx = idx;
        renderCaseOptions();
        renderCase();
        resetChat();
      })
      .catch(function () { notice(t("errGeneric")); });
  }

  function renderWorkspaceChrome() {
    var meta = agentMeta(S.agent);
    if (!meta) return;
    var ws = $("ap-workspace");
    ws.className = "ap-workspace ap-t-" + S.agent;
    $("ap-agent-name").textContent = persona(meta);
    $("ap-agent-dir").textContent = meta.direction === "outbound" ? tg("outbound", meta) : t("inbound");
    $("ap-mode-voice").disabled = !meta.voice;
    renderModes();
    renderAccent();
    renderCaseOptions();
  }

  function renderCaseOptions() {
    var sel = $("ap-case");
    sel.textContent = "";
    S.cases.forEach(function (c, i) {
      var o = el("option", null, loc(c, "title"));
      o.value = String(i);
      if (i === S.caseIdx) o.selected = true;
      sel.appendChild(o);
    });
    $("ap-prev").disabled = S.caseIdx <= 0;
    $("ap-next").disabled = S.caseIdx >= S.cases.length - 1;
  }

  function renderCase() {
    var c = S.cases[S.caseIdx];
    if (!c) return;
    $("ap-story-headline").textContent = loc(c, "headline");
    $("ap-summary").textContent = loc(c, "summary") || [loc(c, "who"), loc(c, "happened")].filter(Boolean).join(" ");
    $("ap-goal").textContent = loc(c, "goal");
    $("ap-twist").textContent = loc(c, "twist");
    $("ap-great").textContent = loc(c, "great");
    var facts = $("ap-facts");
    facts.textContent = "";
    (c.facts || []).forEach(function (f) {
      var li = el("li", "ap-chip");
      li.appendChild(el("span", "ap-chip-k", loc(f, "label")));
      var v = el("span", "ap-chip-v", LOCALE === "ar" && f.value_ar ? f.value_ar : f.value);
      if (!(LOCALE === "ar" && f.value_ar)) v.setAttribute("dir", "ltr");
      li.appendChild(v);
      facts.appendChild(li);
    });
    var say = $("ap-say");
    say.textContent = "";
    sayLines(c).forEach(function (line) {
      var b = el("button", "ap-say-btn");
      b.type = "button";
      b.dir = "auto";
      b.appendChild(el("span", null, line));
      b.addEventListener("click", function () { onSayTap(b, line); });
      say.appendChild(b);
    });
    $("ap-say-hint").textContent = "· " + (S.mode === "chat" ? t("sayHintChat") : t("sayHintVoice"));
    renderEmpty();
    store("case_" + S.agent, c.id);
    var url = new URL(location.href);
    url.searchParams.set("case", c.id);
    history.replaceState(history.state, "", url.pathname + url.search);
  }

  function selectCase(idx) {
    if (idx < 0 || idx >= S.cases.length || idx === S.caseIdx) return;
    if (S.voiceLive) { $("ap-case").value = String(S.caseIdx); notice(t("busyNotice")); return; }
    S.caseIdx = idx;
    renderCaseOptions();
    renderCase();
    resetChat();
  }

  // ------------------------------------------------------------------ modes
  function renderModes() {
    ["voice", "chat"].forEach(function (m) {
      $("ap-mode-" + m).setAttribute("aria-selected", S.mode === m ? "true" : "false");
    });
    $("ap-voice-controls").hidden = S.mode !== "voice";
    $("ap-composer").hidden = S.mode !== "chat";
    var c = S.cases[S.caseIdx];
    if (c) $("ap-say-hint").textContent = "· " + (S.mode === "chat" ? t("sayHintChat") : t("sayHintVoice"));
    renderCallButton();
    renderEmpty();
  }

  function setMode(m) {
    var meta = agentMeta(S.agent);
    if (m === S.mode) return;
    if (m === "voice" && !(meta && meta.voice)) { notice(t("voiceOff")); return; }
    if (S.voiceLive) { notice(t("busyNotice")); return; }
    S.mode = m;
    store("mode_" + S.agent, m);
    renderModes();
    resetChat();
  }

  function renderAccent() {
    var sel = $("ap-accent");
    sel.textContent = "";
    var current = callTag();
    // The page's language first.
    [LOCALE, LOCALE === "ar" ? "en" : "ar"].forEach(function (lang) {
      var group = el("optgroup");
      group.label = t("langs")[lang];
      SPEECH[lang].forEach(function (code) {
        var o = el("option", null, t("accents")[code]);
        o.value = code;
        o.lang = lang;
        if (code === current) o.selected = true;
        group.appendChild(o);
      });
      sel.appendChild(group);
    });
  }

  function setAccent(code) {
    if (S.voiceLive) { $("ap-accent").value = callTag(); notice(t("busyNotice")); return; }
    store("accent_" + LOCALE, code);
    // A new language means new lines to say and a fresh conversation.
    if (S.agent) { renderCase(); resetChat(); }
  }

  // ---------------------------------------------------------------- the log
  function logEl() { return $("ap-log"); }
  function scrollLog() { var l = logEl(); l.scrollTop = l.scrollHeight; }
  function clearLog() {
    var l = logEl();
    var keep = $("ap-empty");
    l.textContent = "";
    l.appendChild(keep);
  }
  function renderEmpty() {
    var meta = agentMeta(S.agent);
    if (!meta) return;
    var name = persona(meta);
    var out = meta.direction === "outbound";
    var key = S.mode === "voice" ? (out ? "emptyVoiceOut" : "emptyVoiceIn") : (out ? "emptyChatOut" : "emptyChatIn");
    var p = $("ap-empty-text");
    p.textContent = tg(key, meta, { name: name });
    var existing = $("ap-pickup");
    if (existing) existing.remove();
    if (S.mode === "chat" && out && !S.chat.started) {
      var b = el("button", "ap-callbtn");
      b.id = "ap-pickup";
      b.type = "button";
      b.style.marginBlockStart = "14px";
      b.appendChild(el("span", null, t("pickUp")));
      b.addEventListener("click", function () {
        var c = S.cases[S.caseIdx];
        var first = sayLines(c)[0] || (callLang() === "ar" ? "ألو؟" : "Hello?");
        sendChat(first);
      });
      $("ap-empty").appendChild(b);
    }
    $("ap-empty").hidden = logEl().children.length > 1;
  }

  // Minimal rich text: **bold** and line breaks (white-space: pre-wrap), built
  // from text nodes — never parsed as HTML.
  function renderRich(node, text) {
    node.textContent = "";
    String(text || "").split(/(\*\*[^*\n]+\*\*)/).forEach(function (part) {
      if (/^\*\*[^*\n]+\*\*$/.test(part)) node.appendChild(el("strong", null, part.slice(2, -2)));
      else if (part) node.appendChild(document.createTextNode(part));
    });
  }

  function addMsg(role, text) {
    $("ap-empty").hidden = true;
    var meta = agentMeta(S.agent);
    var cls = role === "user" ? "ap-msg ap-msg-user" : role === "agent" ? "ap-msg ap-msg-agent" : "ap-msg ap-msg-system";
    var row = el("div", cls);
    if (role === "agent") row.appendChild(el("span", "ap-msg-who", t("agentLabel")));
    var body = el("span", "ap-msg-body");
    body.dir = "auto";
    if (role === "agent") renderRich(body, text); else body.textContent = text || "";
    row.appendChild(body);
    logEl().appendChild(row);
    scrollLog();
    return body;
  }

  function addTools(names) {
    if (!names || !names.length) return;
    var wrap = el("div", "ap-tools");
    names.forEach(function (n) { wrap.appendChild(el("span", "ap-tool", String(n).replace(/_/g, " "))); });
    logEl().appendChild(wrap);
    scrollLog();
  }

  function notice(text) { addMsg("system", text); }

  // On a phone the story folds away once the conversation starts, so the
  // transcript and the pinned call bar share the screen; one tap reopens it.
  function foldStoryOnPhone() {
    if (!window.matchMedia("(max-width: 899px)").matches) return;
    $("ap-story").open = false;
  }

  // ------------------------------------------------------------------- chat
  function resetChat() {
    S.chat = { session: uuid(), token: null, busy: false, started: false, pending: null };
    clearLog();
    renderEmpty();
    setStatus(S.mode === "voice" ? t("ready") : "", "");
    $("ap-send").disabled = false;
  }

  function onSayTap(btn, line) {
    btn.classList.add("is-used");
    if (S.mode === "chat") { sendChat(line); return; }
    if (!S.voiceLive) {
      var input = $("ap-input");
      input.value = line;
    }
  }

  function sendChat(text) {
    text = String(text || "").trim();
    if (!text || !S.agent) return;
    // A line tapped while the agent is still answering is queued, not lost.
    if (S.chat.busy) { S.chat.pending = text; return; }
    var c = S.cases[S.caseIdx];
    var meta = agentMeta(S.agent);
    S.chat.busy = true;
    if (!S.chat.started) foldStoryOnPhone();
    S.chat.started = true;
    var pick = $("ap-pickup");
    if (pick) pick.remove();
    $("ap-send").disabled = true;
    addMsg("user", text);
    var row = el("div", "ap-msg ap-msg-agent");
    row.appendChild(el("span", "ap-msg-who", t("agentLabel")));
    var body = el("span", "ap-msg-body");
    body.dir = "auto";
    var typing = el("span", "ap-typing");
    typing.appendChild(el("i")); typing.appendChild(el("i")); typing.appendChild(el("i"));
    body.appendChild(typing);
    row.appendChild(body);
    logEl().appendChild(row);
    scrollLog();
    var acc = "";
    var tools = [];
    var payload = {
      agent: S.agent, message: text, session_id: S.chat.session, locale: callTag(),
      case_id: c ? c.id : null, state: S.chat.token,
    };
    function finish() {
      S.chat.busy = false;
      $("ap-send").disabled = false;
      if (!acc) row.remove();
      var next = S.chat.pending;
      S.chat.pending = null;
      if (next && S.chat.session === session) { sendChat(next); return; }
      $("ap-input").focus();
    }
    var session = S.chat.session;
    fetch("/agents/chat/stream", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }).then(function (resp) {
      if (resp.status === 429) { notice(t("rateLimited")); return null; }
      if (!resp.ok || !resp.body) { notice(t("errGeneric")); return null; }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) return;
          buf += decoder.decode(res.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(function (chunk) {
            chunk.split("\n").forEach(function (line) {
              if (line.indexOf("data: ") !== 0) return;
              var evt;
              try { evt = JSON.parse(line.slice(6)); } catch (_) { return; }
              if (evt.type === "answer") {
                acc += evt.content || "";
                renderRich(body, acc);
                scrollLog();
              } else if (evt.type === "tool_activity") {
                (evt.tools || []).forEach(function (n) { if (tools.indexOf(n) < 0) tools.push(n); });
              } else if (evt.type === "state") {
                S.chat.token = evt.token || S.chat.token;
              } else if (evt.type === "error") {
                notice(evt.message || t("errGeneric"));
              }
            });
          });
          return pump();
        });
      }
      return pump();
    }).catch(function () { notice(t("errGeneric")); })
      .then(function () {
        if (tools.length && acc) logEl().insertBefore(toolRow(tools), row);
        finish();
      });
  }

  function toolRow(names) {
    var wrap = el("div", "ap-tools");
    wrap.setAttribute("title", t("toolsUsed"));
    names.forEach(function (n) { wrap.appendChild(el("span", "ap-tool", String(n).replace(/_/g, " "))); });
    return wrap;
  }

  // ------------------------------------------------------------------ voice
  function setStatus(text, flag) {
    var s = $("ap-status");
    s.textContent = text || "";
    s.removeAttribute("data-live");
    s.removeAttribute("data-ring");
    if (flag) s.setAttribute(flag, "1");
  }

  function renderCallButton() {
    var meta = agentMeta(S.agent);
    var btn = $("ap-callbtn");
    var label = $("ap-callbtn-label");
    if (!meta) return;
    if (S.voiceLive) {
      label.textContent = t("hangUp");
      btn.setAttribute("data-state", btn.getAttribute("data-state") === "ringing" ? "ringing" : "live");
    } else {
      label.textContent = t(meta.direction === "outbound" ? "callOut" : "callIn");
      btn.removeAttribute("data-state");
    }
    btn.disabled = !meta.voice;
    $("ap-mute").disabled = !S.voiceLive;
    $("ap-end").disabled = !S.voiceLive;
  }

  function voiceUrl() {
    var meta = agentMeta(S.agent);
    var c = S.cases[S.caseIdx];
    var q = new URLSearchParams({ agentKind: meta.kind, locale: callTag(), sessionId: uuid() });
    if (c) q.set("callCase", c.id);
    var scheme = location.protocol === "https:" ? "wss" : "ws";
    return scheme + "://" + location.host + (S.catalog.voice_path || "/voice/live") + "?" + q.toString();
  }

  function tick() {
    if (!S.callStarted) return;
    var s = Math.floor((Date.now() - S.callStarted) / 1000);
    setStatus(t("inCall") + " · " + Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2), "data-live");
  }

  function endCall() {
    if (S.voice) { try { S.voice.disconnect(); } catch (_) { /* already closed */ } }
  }

  function startCall() {
    if (S.voiceLive) { endCall(); return; }
    if (typeof VoiceLiveClient === "undefined") { notice(t("errGeneric")); return; }
    clearLog();
    $("ap-empty").hidden = true;
    foldStoryOnPhone();
    S.voiceLive = true;
    S.muted = false;
    $("ap-mute").setAttribute("aria-pressed", "false");
    var userRow = null, userText = "", botRow = null, botText = "", botBreak = false;
    function closeBot() { botRow = null; botText = ""; botBreak = false; }
    var client = new VoiceLiveClient({
      url: voiceUrl(),
      onState: function (state) {
        if (client !== S.voice) return;
        var btn = $("ap-callbtn");
        if (state === "connecting") {
          btn.setAttribute("data-state", "ringing");
          setStatus(t("ringing"), "data-ring");
        } else if (state === "listening") {
          btn.setAttribute("data-state", "live");
          S.callStarted = Date.now();
          clearInterval(S.timer);
          S.timer = setInterval(tick, 1000);
          tick();
        } else if (state === "closed") {
          S.voiceLive = false;
          S.voice = null;
          S.callStarted = 0;
          clearInterval(S.timer);
          setStatus(t("ended"), "");
          closeBot();
        }
        renderCallButton();
      },
      onEvent: function (frame) {
        if (client !== S.voice || !frame) return;
        if (frame.type === "transcript") {
          if (frame.role === "user") {
            if (!userRow) { closeBot(); userRow = addMsg("user", ""); userText = ""; }
            userText += frame.text || "";
            userRow.textContent = userText;
            scrollLog();
            if (frame.final) { userRow = null; userText = ""; }
          } else if (frame.role === "assistant") {
            if (!botRow) { botRow = addMsg("agent", ""); botText = ""; }
            if (botBreak && botText) botText += "\n";
            botBreak = false;
            botText += frame.text || "";
            renderRich(botRow, botText);
            scrollLog();
          }
        } else if (frame.type === "turn_end") {
          userRow = null; userText = "";
          if (botRow) botBreak = true;
        } else if (frame.type === "error") {
          notice(frame.message || t("errGeneric"));
        }
      },
    });
    S.voice = client;
    renderCallButton();
    try { client.connect(); } catch (err) { S.voiceLive = false; S.voice = null; renderCallButton(); notice(t("errGeneric")); }
  }

  function toggleMute() {
    if (!S.voice || !S.voiceLive) return;
    S.muted = !S.muted;
    S.voice.setMuted(S.muted);
    var b = $("ap-mute");
    b.setAttribute("aria-pressed", S.muted ? "true" : "false");
    b.lastChild.textContent = S.muted ? t("unmute") : t("mute");
  }

  // ---------------------------------------------------------------- routing
  function route(push) {
    var parts = location.pathname.replace(/\/+$/, "").split("/");
    var key = parts[2] || document.body.getAttribute("data-agent") || "";
    var caseId = new URLSearchParams(location.search).get("case");
    if (key && agentMeta(key)) openAgent(key, push, caseId); else showPicker(false);
  }

  function wire() {
    $("ap-lang").addEventListener("click", function () { setLocale(LOCALE === "ar" ? "en" : "ar"); });
    $("ap-back").addEventListener("click", function () { showPicker(true); });
    document.querySelector(".ap-brand").addEventListener("click", function (e) { e.preventDefault(); showPicker(true); });
    $("ap-prev").addEventListener("click", function () { selectCase(S.caseIdx - 1); });
    $("ap-next").addEventListener("click", function () { selectCase(S.caseIdx + 1); });
    $("ap-case").addEventListener("change", function () { selectCase(parseInt(this.value, 10)); });
    $("ap-mode-voice").addEventListener("click", function () { setMode("voice"); });
    $("ap-mode-chat").addEventListener("click", function () { setMode("chat"); });
    $("ap-accent").addEventListener("change", function () { setAccent(this.value); });
    $("ap-callbtn").addEventListener("click", function () {
      try { startCall(); } catch (err) { notice(t("errGeneric")); }
    });
    $("ap-end").addEventListener("click", endCall);
    $("ap-mute").addEventListener("click", toggleMute);
    $("ap-newchat").addEventListener("click", function () { if (!S.chat.busy) resetChat(); });
    $("ap-composer").addEventListener("submit", function (e) {
      e.preventDefault();
      var input = $("ap-input");
      var v = input.value;
      input.value = "";
      sendChat(v);
    });
    window.addEventListener("popstate", function () { route(false); });
    // Desktop keeps the story card open; on a phone it can fold away.
    var story = $("ap-story");
    story.querySelector("summary").addEventListener("click", function (e) {
      if (window.matchMedia("(min-width: 900px)").matches) e.preventDefault();
    });
  }

  function boot() {
    wire();
    fetch("/agents/api/catalog", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        S.catalog = data;
        applyStatic();
        route(false);
      })
      .catch(function () {
        $("ap-picker").hidden = false;
        $("ap-picker-note").textContent = t("errGeneric");
      });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
