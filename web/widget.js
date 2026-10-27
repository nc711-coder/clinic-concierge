/*
 * Clinic Concierge chat widget. One file, no libraries, no build step.
 *
 * Add it to any web page:
 *
 *   <script src="https://YOUR-API-HOST/widget.js" defer
 *           data-api="https://YOUR-API-HOST"            (optional: defaults to where this file came from)
 *           data-title="Quillmere PT assistant"         (optional)
 *           data-accent="#2F5BEA"></script>             (optional: button and bubble colour)
 *
 * The page can also open it from its own buttons:
 *   ClinicConcierge.open()                                  -> just open the chat
 *   ClinicConcierge.open("I'd like to book an evaluation")  -> open and send this message
 *
 * Everything lives inside a Shadow DOM, so the host site's CSS can't break the widget and the widget's
 * CSS can't break the host site.
 */
(function () {
  "use strict";
  if (window.ClinicConcierge) return;                       // loaded twice: keep the first one

  var script = document.currentScript;
  var cfg = {
    api: ((script && script.dataset.api) || (script ? new URL(script.src).origin : "")).replace(/\/$/, ""),
    title: (script && script.dataset.title) || "Clinic assistant",
    accent: (script && script.dataset.accent) || "#2F5BEA",
    phone: (script && script.dataset.phone) || "(703) 555-0142",
    greeting: (script && script.dataset.greeting) ||
      "Hi! I can answer questions about our services, prices, insurance and policies, and I can book " +
      "an appointment for you. I can't give medical advice.",
  };
  var STORE_KEY = "clinic-concierge-session";
  var SLOW_MS = 8000;                                       // local models can take a while

  // ------------------------------------------------------------------ small helpers
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === "text") n.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function load() { try { return localStorage.getItem(STORE_KEY); } catch (e) { return null; } }
  function save(v) { try { v ? localStorage.setItem(STORE_KEY, v) : localStorage.removeItem(STORE_KEY); } catch (e) {} }

  // Reply text -> DOM: keeps line breaks, turns "[2]" into a small clickable citation marker. Never uses innerHTML,
  // so nothing the model writes can run as code on the page.
  // Some models write **bold**: shown as bold, still built from text nodes (never HTML).
  function renderText(text, target) {
    text.split("\n").forEach(function (line, i) {
      if (i) target.appendChild(document.createElement("br"));
      line.split(/(\*\*[^*]+\*\*)/).forEach(function (chunk) {
        var bold = chunk.match(/^\*\*([^*]+)\*\*$/);
        var into = bold ? target.appendChild(el("strong")) : target;
        (bold ? bold[1] : chunk).split(/(\[\d+\])/).forEach(function (part) {
          var m = part.match(/^\[(\d+)\]$/);
          into.appendChild(m ? el("sup", {}, [el("button", { type: "button", class: "cite", "data-n": m[1],
                                                          "aria-label": "Show source " + m[1], text: m[1] })])
                             : document.createTextNode(part));
        });
      });
    });
  }

  // ------------------------------------------------------------------ styles (scoped to the shadow root)
  var CSS = [
    ":host{all:initial}",
    "*{box-sizing:border-box}",
    ".wrap{--accent:" + cfg.accent + ";--ink:#0F2E26;--muted:#53675F;--line:#D3DDD8;--bg:#FFFFFF;--soft:#EEF3F0;",
    "  font:15px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:var(--ink)}",
    ".launch{position:fixed;right:20px;bottom:20px;z-index:2147483000;display:flex;align-items:center;gap:8px;",
    "  border:0;border-radius:999px;padding:14px 20px;background:var(--accent);color:#fff;font:inherit;font-weight:600;",
    "  cursor:pointer;box-shadow:0 6px 20px rgba(15,46,38,.28)}",
    ".launch svg{width:20px;height:20px}",
    ".launch:focus-visible,button:focus-visible{outline:3px solid #FFB020;outline-offset:2px}",
    "textarea:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}",
    ".panel{position:fixed;right:20px;bottom:20px;z-index:2147483001;width:380px;height:min(620px,calc(100vh - 40px));",
    "  display:flex;flex-direction:column;background:var(--bg);border-radius:16px;overflow:hidden;",
    "  box-shadow:0 18px 50px rgba(15,46,38,.30);transform-origin:bottom right;animation:pop .18s ease-out}",
    ".panel[hidden],.launch[hidden]{display:none}",
    "@keyframes pop{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}",
    "@media (prefers-reduced-motion:reduce){.panel{animation:none}.dots i{animation:none!important}}",
    "@media (max-width:480px){.panel{right:0;bottom:0;width:100vw;height:100%;height:100dvh;border-radius:0}}",
    "header{display:flex;align-items:center;gap:8px;padding:14px 12px 14px 16px;background:var(--ink);color:#fff}",
    "header h2{margin:0;flex:1;font-size:16px;font-weight:650}",
    "header button{border:0;background:transparent;color:#fff;font:inherit;font-size:13px;padding:6px 8px;",
    "  border-radius:6px;cursor:pointer;opacity:.85}",
    "header button:hover{opacity:1;background:rgba(255,255,255,.12)}",
    ".log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;background:var(--soft)}",
    ".msg{max-width:88%;padding:10px 13px;border-radius:14px;white-space:normal;overflow-wrap:anywhere}",
    ".bot{align-self:flex-start;background:var(--bg);border:1px solid var(--line);border-bottom-left-radius:4px}",
    ".me{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}",
    ".err{align-self:flex-start;background:#FFF4E5;border:1px solid #F3C98B}",
    ".cite{border:0;background:none;padding:0 2px;margin-left:1px;font:inherit;font-size:11px;font-weight:700;",
    "  color:var(--accent);cursor:pointer;border-radius:4px}",
    ".cite:hover{background:var(--soft);text-decoration:underline}",
    ".sources{margin:8px 0 0;padding:6px 0 0;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted);list-style:none}",
    ".src{display:flex;align-items:baseline;gap:5px;width:100%;border:0;background:none;padding:4px 0;margin:0;",
    "  font:inherit;color:inherit;text-align:left;cursor:pointer}",
    ".src:hover .t{color:var(--ink);text-decoration:underline}",
    ".src b{color:var(--accent);flex:none}",
    ".src .t{flex:1}",
    ".src .chev{flex:none;transition:transform .15s}",
    ".src[aria-expanded=true] .chev{transform:rotate(180deg)}",
    ".excerpt{margin:2px 0 8px;padding:8px 10px;background:var(--soft);border-left:3px solid var(--accent);",
    "  border-radius:6px;color:var(--ink);white-space:pre-line;max-height:220px;overflow-y:auto}",
    ".excerpt[hidden]{display:none}",
    ".excerpt a{display:inline-block;margin-top:6px;color:var(--accent);font-weight:600}",
    ".flash{animation:flash 1s ease-out}",
    "@keyframes flash{from{background:#FFF1C2}to{background:var(--soft)}}",
    ".booked{margin-top:8px;padding:10px 12px;border-radius:10px;background:#E6F4EA;border:1px solid #A8D5B5;font-size:13.5px}",
    ".note{margin-top:6px;font-size:12.5px;color:var(--muted)}",
    ".chips{display:flex;flex-wrap:wrap;gap:6px;align-self:flex-start;max-width:100%}",
    ".chips button{border:1px solid var(--accent);background:var(--bg);color:var(--accent);border-radius:999px;",
    "  padding:7px 12px;font:inherit;font-size:13.5px;cursor:pointer}",
    ".chips button:hover{background:var(--accent);color:#fff}",
    ".typing{align-self:flex-start;color:var(--muted);font-size:13px;display:flex;align-items:center;gap:8px}",
    ".dots i{display:inline-block;width:6px;height:6px;margin-right:3px;border-radius:50%;background:var(--muted);",
    "  animation:blink 1.2s infinite}",
    ".dots i:nth-child(2){animation-delay:.2s}.dots i:nth-child(3){animation-delay:.4s}",
    "@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}",
    "form{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line);background:var(--bg)}",
    "textarea{flex:1;resize:none;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:inherit;font-size:16px;",
    "  color:var(--ink);max-height:120px;min-height:42px}",
    "form button{border:0;border-radius:10px;padding:0 16px;background:var(--accent);color:#fff;font:inherit;",
    "  font-weight:600;cursor:pointer}",
    "form button:disabled{opacity:.5;cursor:default}",
    ".foot{padding:0 12px 10px;background:var(--bg);font-size:11.5px;color:var(--muted);text-align:center}",
  ].join("\n");

  // ------------------------------------------------------------------ build the UI
  var host = el("div", { id: "clinic-concierge" });
  var root = host.attachShadow({ mode: "open" });
  root.appendChild(el("style", { text: CSS }));

  var icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
             '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"/></svg>';
  var launch = el("button", { class: "launch", type: "button", "aria-label": "Open chat: " + cfg.title });
  launch.innerHTML = icon + "<span>Ask us</span>";         // static markup only

  var log = el("div", { class: "log", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  var input = el("textarea", { rows: "1", placeholder: "Type a question or ask to book…", "aria-label": "Your message",
                               maxlength: "2000" });
  var send = el("button", { type: "submit", text: "Send" });
  var form = el("form", {}, [input, send]);
  var panel = el("section", { class: "panel", role: "dialog", "aria-label": cfg.title, hidden: "" }, [
    el("header", {}, [
      el("h2", { text: cfg.title }),
      el("button", { type: "button", text: "Start over", title: "Start a new conversation", onclick: reset }),
      el("button", { type: "button", text: "Close", "aria-label": "Close chat", onclick: close }),
    ]),
    log,
    form,
    el("div", { class: "foot", text: "Answers come from the clinic's documents. For emergencies call 911." }),
  ]);
  root.appendChild(el("div", { class: "wrap" }, [launch, panel]));

  // ------------------------------------------------------------------ conversation
  var sessionId = load();
  var busy = false;
  var started = false;

  function bubble(cls, text) {
    var b = el("div", { class: "msg " + cls });
    renderText(text, b);
    log.appendChild(b);
    log.scrollTop = log.scrollHeight;
    return b;
  }

  function chips(labels, onPick) {
    var row = el("div", { class: "chips" });
    labels.forEach(function (c) {
      row.appendChild(el("button", { type: "button", text: c.label, onclick: function () {
        row.remove();
        onPick(c.send || c.label);
      } }));
    });
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  function clearChips() { log.querySelectorAll(".chips").forEach(function (c) { c.remove(); }); }

  function greet() {
    bubble("bot", cfg.greeting);
    chips([{ label: "Do you take my insurance?" }, { label: "How much is a first visit?" },
           { label: "Book an appointment", send: "I'd like to book an appointment" }], ask);
  }

  // "Which time works best?\n1. Thu, Oct 22 at 10:00 AM with Dr. Bell" -> buttons that send "1", "2", ...
  function numberedOptions(reply) {
    var out = [];
    reply.split("\n").forEach(function (line) {
      var m = line.match(/^\s*(\d)\.\s+(.+)$/);
      if (m) out.push({ label: m[2].replace(/\*\*/g, ""), send: m[1] });
    });
    return out.length >= 2 ? out : [];
  }

  function showReply(r) {
    var b = bubble("bot", r.reply);
    if (r.citations && r.citations.length) b.appendChild(sourceList(r.citations, b));
    if (r.appointment) {
      b.appendChild(el("div", { class: "booked", text: "Booked: " + r.appointment.when + " with " +
                                r.appointment.provider + ". Confirmation #" + r.appointment.appointment_id + "." }));
    }
    if (r.handoff_id) {
      b.appendChild(el("div", { class: "note", text: "The front desk has your request (#" + r.handoff_id + ")." }));
    }
    if (r.awaiting_confirmation) {
      chips([{ label: "Yes, book it", send: "yes" }, { label: "No, change something", send: "no, I'd like to change something" }], ask);
    } else {
      var opts = numberedOptions(r.reply);
      if (opts.length) chips(opts, ask);
    }
    log.scrollTop = log.scrollHeight;                     // show the sources / booking card too
  }

  // Sources under an answer: click one (or a [n] marker in the text) to see the passage the answer came from,
  // with a link to the whole document.
  function sourceList(citations, bubbleEl) {
    var list = el("ul", { class: "sources", "aria-label": "Sources" });
    var toggles = {};
    citations.forEach(function (c) {
      var id = "src-" + Math.random().toString(36).slice(2);
      var box = el("div", { class: "excerpt", id: id, hidden: "" });
      if (c.excerpt) box.appendChild(document.createTextNode("“" + c.excerpt + "”"));
      if (c.url) {
        box.appendChild(document.createElement("br"));
        box.appendChild(el("a", { href: cfg.api + c.url, target: "_blank", rel: "noopener",
                                  text: "Open the full document ↗" }));
      }
      var btn = el("button", { type: "button", class: "src", "aria-expanded": "false", "aria-controls": id }, [
        el("b", { text: String(c.n) }),
        el("span", { class: "t", text: c.title + (c.section ? " — " + c.section : "") }),
        el("span", { class: "chev", "aria-hidden": "true", text: "▾" }),
      ]);
      function toggle(show) {
        show = show === undefined ? box.hidden : show;
        box.hidden = !show;
        btn.setAttribute("aria-expanded", String(show));
        return box;
      }
      btn.addEventListener("click", function () { toggle(); });
      toggles[c.n] = toggle;
      list.appendChild(el("li", {}, [btn, box]));
    });
    bubbleEl.addEventListener("click", function (e) {      // [n] markers in the answer open that source
      var cite = e.target.closest && e.target.closest(".cite");
      if (!cite || !toggles[cite.dataset.n]) return;
      var box = toggles[cite.dataset.n](true);
      box.classList.remove("flash");
      void box.offsetWidth;                                // restart the highlight
      box.classList.add("flash");
      box.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    return list;
  }

  function typing() {
    var t = el("div", { class: "typing" }, [el("span", { class: "dots", "aria-hidden": "true" },
                                                [el("i"), el("i"), el("i")]),
                                            el("span", { text: "Typing…" })]);
    log.appendChild(t);
    log.scrollTop = log.scrollHeight;
    var slow = setTimeout(function () { t.lastChild.textContent = "Still working on it…"; }, SLOW_MS);
    return function () { clearTimeout(slow); t.remove(); };
  }

  function ask(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    clearChips();
    bubble("me", text);
    busy = true;
    send.disabled = true;
    var done = typing();
    fetch(cfg.api + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          if (!res.ok) {
            var err = new Error(typeof body.detail === "string" ? body.detail
              : res.status >= 500 ? "Something went wrong on our side. Please try again in a few minutes."
              : "HTTP " + res.status);
            err.friendly = res.status === 429 || res.status >= 500;    // limits / model busy / our error: plain English
            throw err;
          }
          return body;
        });
      })
      .then(function (r) {
        done();
        sessionId = r.session_id;
        save(sessionId);
        showReply(r);
      })
      .catch(function (e) {
        done();
        var msg = e instanceof TypeError
          ? "I can't reach the assistant right now. Please try again, or call us at " + cfg.phone + "."
          : e.friendly ? e.message.replace(/\s*\[.*\]$/, "") + " You can also call us at " + cfg.phone + "."
          : "Something went wrong: " + e.message + " You can also call us at " + cfg.phone + ".";
        bubble("err", msg);
      })
      .finally(function () {
        busy = false;
        send.disabled = false;
        input.focus();
      });
  }

  function open(message) {
    panel.hidden = false;
    launch.hidden = true;
    if (!started) {
      started = true;
      if (sessionId) bubble("bot", "Welcome back! We can pick up where we left off.");
      else greet();
    }
    if (message) ask(message);
    else input.focus();
  }

  function close() {
    panel.hidden = true;
    launch.hidden = false;
    launch.focus();
  }

  function reset() {
    sessionId = null;
    save(null);
    log.textContent = "";
    greet();
    input.focus();
  }

  launch.addEventListener("click", function () { open(); });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var t = input.value;
    input.value = "";
    input.style.height = "";
    ask(t);
  });
  input.addEventListener("keydown", function (e) {               // Enter sends, Shift+Enter is a new line
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("input", function () {                   // grow with the text
    input.style.height = "";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });
  root.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) close(); });

  function mount() { document.body.appendChild(host); }
  document.body ? mount() : document.addEventListener("DOMContentLoaded", mount);

  window.ClinicConcierge = { open: open, close: close, reset: reset };
})();
