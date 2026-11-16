/* Comment mode for the admin pages, much like Figma's comments.
 *
 * A reviewer (switched on, per admin login, on the Feedback page) presses the
 * Comment toggle in the top bar, or C. While it is on, the element under the
 * pointer is outlined; a click opens a comment box on it instead of doing
 * what the element does. Each comment is a numbered pin on its element,
 * opening its thread: status (Pending, In progress, Done), replies, Copy (for
 * AI with the exact files and selector, or as text) and Delete.
 *
 * Esc closes a box, then leaves comment mode. A link to a comment
 * (#comment-<id>, from the Feedback page) opens it for anyone signed in.
 * The server side is admin/routes/feedback.py and services/page_comments.py.
 */
(function () {
  "use strict";

  var configNode = document.getElementById("page-comments-config");
  if (!configNode) return;
  var cfg = JSON.parse(configNode.textContent || "{}");
  var STORE_KEY = "shero.comment-mode";
  var STATUS = { pending: "Pending", in_progress: "In progress", done: "Done" };

  var state = {
    on: false,          // picking elements to comment on (reviewers)
    showing: false,     // pins on screen (comment mode, or a link to a comment)
    showDone: false,
    threads: [],
    loaded: false,
    hovered: null,
    popover: null,
  };

  // --- small helpers ---------------------------------------------------------------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key];
      else if (key === "class") node.className = attrs[key];
      else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), attrs[key]);
      else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) {
      if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function storeGet() {
    try { return window.localStorage.getItem(STORE_KEY) === "1"; } catch (e) { return false; }
  }
  function storeSet(on) {
    try { window.localStorage.setItem(STORE_KEY, on ? "1" : "0"); } catch (e) { /* private window */ }
  }

  function api(path, method, body) {
    var options = { method: method || "GET", credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(cfg.api + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || data.detail || "Could not save - try again.");
        return data;
      });
    });
  }

  function toast(message) {
    var box = el("div", { class: "pc-toast", role: "status", text: message });
    document.body.appendChild(box);
    setTimeout(function () { box.classList.add("pc-toast-out"); }, 1800);
    setTimeout(function () { box.remove(); }, 2300);
  }

  function copyText(text, what) {
    var done = function () { toast(what); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  }
  function fallbackCopy(text) {
    var area = el("textarea", { style: "position:fixed;top:-1000px;opacity:0" });
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (e) { /* nothing more to try */ }
    area.remove();
  }

  // --- styles (own classes: the page's Tailwind is built from its own markup) ------------
  var css = [
    "#pc-root{position:absolute;left:0;top:0;width:0;height:0;z-index:60}",
    ".pc-on, .pc-on *{cursor:crosshair !important}",
    ".pc-on #pc-root *, .pc-on [data-comment-toggle], .pc-on .pc-banner *{cursor:auto !important}",
    ".pc-on #pc-root button, .pc-on .pc-banner button, .pc-on .pc-banner a{cursor:pointer !important}",
    ".pc-hover{position:fixed;pointer-events:none;z-index:59;border:2px solid #0d9488;background:rgba(13,148,136,.07);border-radius:6px;transition:all .06s ease-out}",
    ".pc-hover span{position:absolute;left:-2px;top:-22px;max-width:320px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;background:#0d9488;color:#fff;font:600 11px/20px system-ui,sans-serif;padding:0 8px;border-radius:6px 6px 6px 0}",
    ".pc-banner{position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:61;display:flex;align-items:center;gap:10px;background:#134e4a;color:#fff;font:500 12px/1.2 system-ui,sans-serif;padding:8px 10px 8px 14px;border-radius:999px;box-shadow:0 10px 30px rgba(0,0,0,.25);white-space:nowrap;max-width:calc(100vw - 24px);overflow-x:auto}",
    ".pc-banner button,.pc-banner a{background:rgba(255,255,255,.14);color:#fff;border:0;border-radius:999px;padding:5px 10px;font:600 11px system-ui,sans-serif;text-decoration:none}",
    ".pc-banner button:hover,.pc-banner a:hover{background:rgba(255,255,255,.25)}",
    ".pc-pin{position:absolute;width:28px;height:28px;margin:-28px 0 0 0;border-radius:50% 50% 50% 0;background:#0d9488;color:#fff;border:2px solid #fff;box-shadow:0 4px 12px rgba(0,0,0,.25);font:700 12px/24px system-ui,sans-serif;text-align:center;padding:0;cursor:pointer}",
    ".pc-pin.pc-in_progress{background:#d97706}.pc-pin.pc-done{background:#78716c}.pc-pin.pc-lost{border-style:dashed}",
    ".pc-pin:hover,.pc-pin.pc-active{transform:scale(1.12)}",
    ".pc-pop{position:absolute;width:320px;max-width:calc(100vw - 24px);background:#fff;color:#1c1917;border-radius:14px;box-shadow:0 18px 48px rgba(0,0,0,.22);border:1px solid #e7e5e4;font:13px/1.45 system-ui,sans-serif}",
    ".pc-pop header{display:flex;align-items:center;gap:6px;padding:9px 10px 9px 12px;border-bottom:1px solid #f5f5f4;background:#fafaf9;border-radius:14px 14px 0 0}",
    ".pc-pop header b{flex:1;font-size:12px;color:#57534e;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}",
    ".pc-pop .pc-body{max-height:300px;overflow:auto;padding:10px 12px}",
    ".pc-pop .pc-msg{margin-bottom:10px}.pc-pop .pc-msg:last-child{margin-bottom:0}",
    ".pc-pop .pc-who{font-weight:600;font-size:12px}.pc-pop .pc-when{color:#a8a29e;font-size:11px;margin-left:6px}",
    ".pc-pop .pc-text{white-space:pre-wrap;word-break:break-word;margin-top:2px}",
    ".pc-pop .pc-on-el{font-size:11px;color:#78716c;margin-bottom:8px}",
    ".pc-pop textarea{display:block;width:100%;min-height:64px;resize:vertical;border:1px solid #d6d3d1;border-radius:10px;padding:8px 10px;font:13px/1.4 system-ui,sans-serif;outline:none}",
    ".pc-pop textarea:focus{border-color:#0d9488;box-shadow:0 0 0 3px rgba(13,148,136,.15)}",
    ".pc-pop footer{display:flex;align-items:center;gap:6px;padding:8px 10px;border-top:1px solid #f5f5f4}",
    ".pc-btn{border:1px solid #d6d3d1;background:#fff;color:#44403c;border-radius:8px;padding:5px 9px;font:600 12px system-ui,sans-serif;cursor:pointer}",
    ".pc-btn:hover{background:#f5f5f4}.pc-btn.pc-primary{background:#0d9488;border-color:#0d9488;color:#fff}.pc-btn.pc-primary:hover{background:#0f766e}",
    ".pc-btn.pc-danger{color:#b91c1c}.pc-btn[disabled]{opacity:.5;cursor:default}",
    ".pc-x{border:0;background:none;color:#78716c;font-size:18px;line-height:1;padding:2px 6px;cursor:pointer}",
    ".pc-pop select{border:1px solid #d6d3d1;border-radius:8px;padding:3px 6px;font:600 12px system-ui,sans-serif;background:#fff}",
    ".pc-menu{position:relative}.pc-menu-list{position:absolute;left:0;bottom:calc(100% + 6px);width:230px;background:#fff;border:1px solid #e7e5e4;border-radius:10px;box-shadow:0 12px 30px rgba(0,0,0,.18);padding:4px;display:none;z-index:2}",
    ".pc-menu.pc-open .pc-menu-list{display:block}",
    ".pc-menu-list button{display:block;width:100%;text-align:left;border:0;background:none;border-radius:7px;padding:7px 9px;font:13px system-ui,sans-serif;cursor:pointer}",
    ".pc-menu-list button:hover{background:#f0fdfa}.pc-menu-list small{display:block;color:#78716c;font-size:11px}",
    ".pc-error{color:#b91c1c;font-size:12px;padding:0 12px 8px}",
    ".pc-toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:70;background:#1c1917;color:#fff;font:600 13px system-ui,sans-serif;padding:9px 16px;border-radius:999px;box-shadow:0 10px 30px rgba(0,0,0,.25);transition:opacity .4s}",
    ".pc-toast-out{opacity:0}",
  ].join("\n");
  document.head.appendChild(el("style", { text: css }));

  var root = el("div", { id: "pc-root" });
  var hoverBox = el("div", { class: "pc-hover", hidden: "" }, [el("span")]);
  var banner = null;
  document.body.appendChild(root);
  document.body.appendChild(hoverBox);

  var toggles = Array.prototype.slice.call(document.querySelectorAll("[data-comment-toggle]"));

  function ours(node) {
    return !!(node && node.closest && (node.closest("#pc-root") || node.closest(".pc-banner")
      || node.closest(".pc-hover") || node.closest("[data-comment-toggle]") || node.closest(".pc-toast")));
  }

  // --- which element, and how to find it again ---------------------------------------
  function pick(target) {
    var node = target instanceof Element ? target : null;
    if (!node || ours(node)) return null;
    var svg = node.closest("svg");
    if (svg && svg.parentElement) node = svg.parentElement;
    while (node && node !== document.body) {
      var box = node.getBoundingClientRect();
      if (box.width >= 12 && box.height >= 12) break;
      node = node.parentElement;
    }
    if (!node || node === document.body || node === document.documentElement) return null;
    return node;
  }

  function cssPath(node) {
    var parts = [];
    for (var n = node; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
      if (n.id && !/^pc-/.test(n.id)) {
        parts.unshift("#" + CSS.escape(n.id));
        break;
      }
      var part = n.tagName.toLowerCase();
      for (var i = 0; i < n.attributes.length; i++) {
        var a = n.attributes[i];
        if (/^data-[a-z-]+$/.test(a.name) && a.value === "" && a.name.length < 40) {
          part += "[" + a.name + "]";
          break;
        }
      }
      var parent = n.parentElement;
      if (parent) {
        var same = Array.prototype.filter.call(parent.children, function (c) { return c.tagName === n.tagName; });
        if (same.length > 1) part += ":nth-of-type(" + (same.indexOf(n) + 1) + ")";
      }
      parts.unshift(part);
    }
    if (parts.length && parts[0].charAt(0) !== "#") parts.unshift("body");
    return parts.join(" > ");
  }

  var KIND = { div: "text", a: "link", button: "button", input: "field", select: "dropdown", textarea: "text box",
    img: "image", td: "table cell", th: "column heading", tr: "table row", table: "table",
    h1: "heading", h2: "heading", h3: "heading", h4: "heading", label: "label", nav: "navigation",
    form: "form", section: "section", header: "header", aside: "sidebar", ul: "list", li: "list item",
    p: "text", span: "text", main: "page body" };

  function describe(node) {
    var tag = node.tagName.toLowerCase();
    var name = node.getAttribute("aria-label") || node.getAttribute("title") || node.getAttribute("placeholder") || "";
    var text = (name || node.innerText || node.value || "").replace(/\s+/g, " ").trim();
    if (text.length > 80) text = text.slice(0, 77) + "…";
    var column = "";
    var cell = node.closest("td, th");
    if (cell && cell.parentElement) {
      var table = cell.closest("table");
      var index = Array.prototype.indexOf.call(cell.parentElement.children, cell);
      var head = table && table.querySelectorAll("thead th")[index];
      if (head && head !== node) column = head.textContent.replace(/\s+/g, " ").trim();
    }
    return (KIND[tag] || tag) + (column ? ' in the "' + column + '" column' : "") + (text ? ': "' + text + '"' : "");
  }

  function findElement(thread) {
    if (!thread.selector) return null;
    try { return document.querySelector(thread.selector); } catch (e) { return null; }
  }

  // --- pins --------------------------------------------------------------------------------
  function visibleThreads() {
    return state.threads.filter(function (t) { return state.showDone || t.status !== "done"; });
  }

  function numberOf(thread) {
    return state.threads.indexOf(thread) + 1;
  }

  function where(thread) {
    var node = findElement(thread);
    if (node) {
      var box = node.getBoundingClientRect();
      if (box.width || box.height) {
        return {
          x: box.left + window.scrollX + (thread.x_pct == null ? 0.5 : thread.x_pct) * box.width,
          y: box.top + window.scrollY + (thread.y_pct == null ? 0.5 : thread.y_pct) * box.height,
          found: true,
        };
      }
    }
    return { x: thread.page_x || 40, y: thread.page_y || 120, found: false };
  }

  function renderPins() {
    Array.prototype.slice.call(root.querySelectorAll(".pc-pin")).forEach(function (p) { p.remove(); });
    if (!state.showing) return;
    visibleThreads().forEach(function (thread) {
      var spot = where(thread);
      var pin = el("button", {
        type: "button", class: "pc-pin pc-" + thread.status + (spot.found ? "" : " pc-lost"),
        title: (thread.author.name || "") + ": " + thread.body.slice(0, 80) + (spot.found ? "" : " (its element has moved)"),
        "data-thread": thread.id, text: String(numberOf(thread)),
        style: "left:" + spot.x + "px;top:" + spot.y + "px",
        onclick: function (event) { event.stopPropagation(); openThread(thread); },
      });
      if (state.popover && state.popover.threadId === thread.id) pin.classList.add("pc-active");
      root.appendChild(pin);
    });
    updateCount();
  }

  var framePending = false;
  function reposition() {
    if (framePending || !state.showing) return;
    framePending = true;
    window.requestAnimationFrame(function () {
      framePending = false;
      renderPins();
      if (state.popover) placePopover(state.popover.node, state.popover.anchor());
    });
  }

  function updateCount() {
    var open = state.threads.filter(function (t) { return t.status !== "done"; }).length;
    toggles.forEach(function (button) {
      var badge = button.querySelector("[data-comment-count]");
      if (!badge) return;
      badge.textContent = String(open);
      badge.classList.toggle("hidden", !open);
    });
    if (banner) {
      var count = banner.querySelector("[data-pc-count]");
      if (count) count.textContent = open + " open here";
      var done = banner.querySelector("[data-pc-done]");
      if (done) done.textContent = state.showDone ? "Hide done" : "Show done";
    }
  }

  function load() {
    return api("?path=" + encodeURIComponent(window.location.pathname)).then(function (data) {
      state.threads = data.threads || [];
      state.loaded = true;
      renderPins();
      return state.threads;
    }).catch(function () { state.loaded = true; return []; });
  }

  function replaceThread(thread) {
    var index = state.threads.findIndex(function (t) { return t.id === thread.id; });
    if (index >= 0) state.threads[index] = thread;
    else state.threads.push(thread);
    renderPins();
  }

  // --- popovers ----------------------------------------------------------------------------
  function placePopover(node, spot) {
    var width = Math.min(320, window.innerWidth - 24);
    var left = Math.min(spot.x + 18, window.scrollX + window.innerWidth - width - 12);
    left = Math.max(window.scrollX + 12, left);
    var top = Math.max(window.scrollY + 12, spot.y - 20);
    node.style.left = left + "px";
    node.style.top = top + "px";
  }

  function closePopover() {
    if (state.popover) state.popover.node.remove();
    state.popover = null;
    renderPins();
  }

  function showPopover(node, anchor, threadId) {
    closePopover();
    hoverBox.hidden = true;
    root.appendChild(node);
    state.popover = { node: node, anchor: anchor, threadId: threadId || null };
    placePopover(node, anchor());
    renderPins();
  }

  function errorLine() { return el("div", { class: "pc-error", hidden: "" }); }
  function showError(line, error) { line.textContent = error.message; line.hidden = false; }

  function composer(target, event) {
    var box = target.getBoundingClientRect();
    var data = {
      path: window.location.pathname,
      query: window.location.search,
      page_title: document.title,
      template: cfg.template,
      route_file: cfg.route_file,
      selector: cssPath(target),
      element_label: describe(target),
      element_html: (target.outerHTML || "").replace(/\s+/g, " ").slice(0, 800),
      x_pct: box.width ? (event.clientX - box.left) / box.width : 0.5,
      y_pct: box.height ? (event.clientY - box.top) / box.height : 0.5,
      page_x: event.pageX,
      page_y: event.pageY,
      viewport_width: window.innerWidth,
    };
    var text = el("textarea", { placeholder: "Ask for a change or a feature here…", maxlength: "4000" });
    var error = errorLine();
    var post = el("button", { type: "button", class: "pc-btn pc-primary", text: "Post" });
    var node = el("div", { class: "pc-pop", role: "dialog", "aria-label": "New comment" }, [
      el("header", {}, [el("b", { text: "New comment" }),
        el("button", { type: "button", class: "pc-x", "aria-label": "Close", text: "×", onclick: closePopover })]),
      el("div", { class: "pc-body" }, [el("div", { class: "pc-on-el", text: "On " + data.element_label }), text]),
      error,
      el("footer", {}, [el("span", { style: "flex:1;font-size:11px;color:#a8a29e", text: "Ctrl+Enter to post" }),
        el("button", { type: "button", class: "pc-btn", text: "Cancel", onclick: closePopover }), post]),
    ]);
    function save() {
      data.body = text.value.trim();
      if (!data.body) { text.focus(); return; }
      post.disabled = true;
      api("", "POST", data).then(function (result) {
        replaceThread(result.thread);
        openThread(result.thread);
        toast("Comment added");
      }).catch(function (e) { post.disabled = false; showError(error, e); });
    }
    post.addEventListener("click", save);
    text.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) save();
    });
    var spot = { x: event.pageX, y: event.pageY };
    showPopover(node, function () { return spot; });
    text.focus();
  }

  function message(author, when, body) {
    return el("div", { class: "pc-msg" }, [
      el("div", {}, [el("span", { class: "pc-who", text: author }), el("span", { class: "pc-when", text: when })]),
      el("div", { class: "pc-text", text: body }),
    ]);
  }

  function openThread(thread) {
    var error = errorLine();
    var status = el("select", { "aria-label": "Status" });
    Object.keys(STATUS).forEach(function (key) {
      var option = el("option", { value: key, text: STATUS[key] });
      if (key === thread.status) option.selected = true;
      status.appendChild(option);
    });
    status.addEventListener("change", function () {
      api("/" + thread.id + "/status", "POST", { status: status.value }).then(function (result) {
        replaceThread(result.thread);
        toast("Marked " + STATUS[result.thread.status]);
        if (result.thread.status === "done" && !state.showDone) closePopover();
        else openThread(result.thread);
      }).catch(function (e) { showError(error, e); });
    });

    var menu = el("div", { class: "pc-menu" });
    var menuList = el("div", { class: "pc-menu-list", role: "menu" }, [
      el("button", { type: "button", role: "menuitem", onclick: function () {
        menu.classList.remove("pc-open"); copyText(thread.copy.ai, "Copied for AI, with file paths");
      } }, ["Comment for AI", el("small", { text: "Exact page, files, selector and the request" })]),
      el("button", { type: "button", role: "menuitem", onclick: function () {
        menu.classList.remove("pc-open"); copyText(thread.copy.text, "Copied");
      } }, ["Normal comment", el("small", { text: "The page, the element and what was asked" })]),
    ]);
    menu.appendChild(el("button", { type: "button", class: "pc-btn", "aria-haspopup": "menu", text: "Copy ▾",
      onclick: function (e) { e.stopPropagation(); menu.classList.toggle("pc-open"); } }));
    menu.appendChild(menuList);

    var replyBox = el("textarea", { placeholder: "Reply…", maxlength: "4000", style: "min-height:44px" });
    var replyButton = el("button", { type: "button", class: "pc-btn pc-primary", text: "Reply" });
    function sendReply() {
      var body = replyBox.value.trim();
      if (!body) { replyBox.focus(); return; }
      replyButton.disabled = true;
      api("/" + thread.id + "/replies", "POST", { body: body }).then(function (result) {
        replaceThread(result.thread);
        openThread(result.thread);
      }).catch(function (e) { replyButton.disabled = false; showError(error, e); });
    }
    replyButton.addEventListener("click", sendReply);
    replyBox.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendReply();
    });

    var body = el("div", { class: "pc-body" }, [
      thread.element_label ? el("div", { class: "pc-on-el", text: "On " + thread.element_label }) : null,
      message(thread.author.name, thread.created_at, thread.body),
    ]);
    thread.replies.forEach(function (r) { body.appendChild(message(r.author.name, r.created_at, r.body)); });
    body.appendChild(el("div", { style: "margin-top:8px" }, [replyBox]));

    var remove = el("button", { type: "button", class: "pc-btn pc-danger", text: "Delete", onclick: function () {
      if (!window.confirm("Delete this comment and its replies?")) return;
      api("/" + thread.id + "/delete", "POST", {}).then(function () {
        state.threads = state.threads.filter(function (t) { return t.id !== thread.id; });
        closePopover();
        toast("Comment deleted");
      }).catch(function (e) { showError(error, e); });
    } });

    var node = el("div", { class: "pc-pop", role: "dialog", "aria-label": "Comment " + numberOf(thread) }, [
      el("header", {}, [el("b", { text: "#" + numberOf(thread) + " · " + thread.author.name }), status,
        el("button", { type: "button", class: "pc-x", "aria-label": "Close", text: "×", onclick: closePopover })]),
      body, error,
      el("footer", {}, [menu, remove, el("span", { style: "flex:1" }), replyButton]),
    ]);
    showPopover(node, function () { return where(thread); }, thread.id);
  }

  // --- comment mode --------------------------------------------------------------------------
  function makeBanner() {
    var bits = [el("span", { text: cfg.can_comment ? "Comment mode · click anything to comment" : "Comments on this page" }),
      el("span", { "data-pc-count": "", style: "opacity:.75" }),
      el("button", { type: "button", "data-pc-done": "", onclick: function () {
        state.showDone = !state.showDone; renderPins();
      } }),
      el("a", { href: cfg.feedback_url, text: "All feedback" }),
      el("button", { type: "button", text: cfg.can_comment ? "Exit (Esc)" : "Close", onclick: function () { setMode(false); } })];
    return el("div", { class: "pc-banner", role: "status" }, bits);
  }

  function setMode(on, opts) {
    var picking = on && cfg.can_comment && !(opts && opts.viewOnly);
    state.on = picking;
    state.showing = on;
    document.documentElement.classList.toggle("pc-on", picking);
    toggles.forEach(function (b) { b.setAttribute("aria-pressed", picking ? "true" : "false"); });
    if (cfg.can_comment && !(opts && opts.keepStore)) storeSet(picking);
    hoverBox.hidden = true;
    if (on) {
      if (!banner) { banner = makeBanner(); document.body.appendChild(banner); }
      if (!state.loaded) load(); else renderPins();
      updateCount();
    } else {
      closePopover();
      if (banner) { banner.remove(); banner = null; }
      renderPins();
    }
  }

  function onMove(event) {
    if (!state.on) return;
    if (state.popover || ours(event.target)) { hoverBox.hidden = true; return; }
    var node = pick(event.target);
    if (!node) { hoverBox.hidden = true; return; }
    if (node !== state.hovered) {
      state.hovered = node;
      hoverBox.firstChild.textContent = describe(node);
    }
    var box = node.getBoundingClientRect();
    hoverBox.style.left = box.left - 2 + "px";
    hoverBox.style.top = box.top - 2 + "px";
    hoverBox.style.width = box.width + 4 + "px";
    hoverBox.style.height = box.height + 4 + "px";
    hoverBox.hidden = false;
  }

  function swallow(event) {
    if (!state.on || ours(event.target)) return false;
    event.preventDefault();
    event.stopPropagation();
    if (event.stopImmediatePropagation) event.stopImmediatePropagation();
    return true;
  }

  document.addEventListener("mousemove", onMove, true);
  ["mousedown", "mouseup", "pointerdown", "dblclick", "submit", "auxclick"].forEach(function (type) {
    document.addEventListener(type, swallow, true);
  });
  document.addEventListener("click", function (event) {
    // Close an open Copy menu on any outside click.
    Array.prototype.slice.call(document.querySelectorAll(".pc-menu.pc-open")).forEach(function (m) {
      if (!m.contains(event.target)) m.classList.remove("pc-open");
    });
    if (!swallow(event)) {
      if (state.popover && !ours(event.target)) closePopover();
      return;
    }
    if (state.popover) { closePopover(); return; }
    var node = pick(event.target);
    if (node) composer(node, event);
  }, true);

  document.addEventListener("keydown", function (event) {
    var typing = /^(input|textarea|select)$/i.test((event.target.tagName || "")) || event.target.isContentEditable;
    if (event.key === "Escape") {
      if (state.popover) { closePopover(); event.preventDefault(); return; }
      if (state.showing) { setMode(false); event.preventDefault(); }
      return;
    }
    if (!typing && cfg.can_comment && !event.ctrlKey && !event.metaKey && !event.altKey
        && (event.key === "c" || event.key === "C")) {
      setMode(!state.on);
    }
  });

  window.addEventListener("resize", reposition);
  window.addEventListener("scroll", reposition, { passive: true });
  if (window.ResizeObserver) new ResizeObserver(reposition).observe(document.body);

  toggles.forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      setMode(!state.on);
    });
  });

  // --- copy buttons on the Feedback page ------------------------------------------------------
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy-source]");
    if (!button) return;
    var source = document.getElementById(button.getAttribute("data-copy-source"));
    if (!source) return;
    var details = button.closest("details");
    if (details) details.open = false;
    copyText(source.value || source.textContent, button.getAttribute("data-copy-done") || "Copied");
  });

  // --- start ------------------------------------------------------------------------------------
  var linked = /^#comment-([0-9a-f-]{36})$/i.exec(window.location.hash || "");
  if (linked) {
    setMode(true, { viewOnly: !cfg.can_comment || !storeGet(), keepStore: true });
    load().then(function (threads) {
      var thread = threads.filter(function (t) { return t.id === linked[1]; })[0];
      if (!thread) { toast("That comment is not on this page any more"); return; }
      if (thread.status === "done") { state.showDone = true; renderPins(); }
      var spot = where(thread);
      window.scrollTo({ top: Math.max(0, spot.y - window.innerHeight / 3), behavior: "smooth" });
      setTimeout(function () { openThread(thread); }, 350);
    });
  } else if (cfg.can_comment) {
    // Comment mode carries over from page to page - except onto the Feedback
    // page, whose own buttons (Copy, status, Delete) it would catch.
    var onFeedback = window.location.pathname.replace(/\/$/, "") === (cfg.feedback_url || "").replace(/\/$/, "");
    if (storeGet() && !onFeedback) setMode(true, { keepStore: true });
    else load();
  }
})();
