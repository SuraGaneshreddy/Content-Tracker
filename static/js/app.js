/* =========================================================================
   Personal Content Tracker — front-end behaviour
   Vanilla JS, no build step, no external dependencies.
   ========================================================================= */
(function () {
  "use strict";

  var csrfToken = document.querySelector('meta[name="csrf-token"]');
  var CSRF = csrfToken ? csrfToken.content : "";

  /* ------------------------------------------------------------- helpers */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function toast(message, kind) {
    var wrap = $("#toasts");
    if (!wrap) return;
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = message;
    wrap.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .25s, transform .25s";
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      setTimeout(function () { el.remove(); }, 260);
    }, 2600);
  }

  /**
   * Every state-changing request must carry the CSRF token, whether it sends a
   * JSON body, a form body or nothing at all (query-string-only endpoints such
   * as /api/theme?theme=light).
   */
  function apiFetch(url, options) {
    options = options || {};
    var headers = {};
    Object.keys(options.headers || {}).forEach(function (key) { headers[key] = options.headers[key]; });
    headers["X-CSRF-Token"] = CSRF;
    return fetch(url, {
      method: options.method || "POST",
      headers: headers,
      body: options.body,
      credentials: "same-origin",
      redirect: "follow"
    });
  }

  function api(url, options) {
    options = options || {};
    if (options.body && !(options.body instanceof FormData)) {
      options.headers = Object.assign({}, options.headers, { "Content-Type": "application/json" });
      options.body = JSON.stringify(options.body);
    }
    return apiFetch(url, options).then(function (response) {
      return response.json().catch(function () { return { ok: false, detail: "Unexpected response." }; })
        .then(function (data) {
          if (!response.ok) {
            var message = data.detail || data.error || ("Request failed (" + response.status + ")");
            throw new Error(message);
          }
          return data;
        });
    });
  }

  /* -------------------------------------------------------------- theme */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("pct-theme", theme); } catch (e) {}
    var btn = $("#theme-toggle");
    if (btn) btn.textContent = theme === "dark" ? "☀" : "🌙";
  }

  var themeToggle = $("#theme-toggle");
  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      // Persist server-side too, so a fresh page load matches.
      api("/api/theme", { body: { theme: next } }).catch(function () {});
    });
  }

  /* ----------------------------------------------------------- sidebar */
  var sidebar = $("#sidebar");
  var scrim = $("#scrim");
  function toggleSidebar(force) {
    if (!sidebar) return;
    var open = typeof force === "boolean" ? force : !sidebar.classList.contains("open");
    sidebar.classList.toggle("open", open);
    if (scrim) scrim.classList.toggle("show", open);
    document.body.style.overflow = open ? "hidden" : "";
  }
  var menuBtn = $("#menu-toggle");
  if (menuBtn) menuBtn.addEventListener("click", function () { toggleSidebar(); });
  if (scrim) scrim.addEventListener("click", function () { toggleSidebar(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { toggleSidebar(false); closeAllMenus(); closeModal(); }
  });

  /* --------------------------------------------------------- dropdowns */
  function closeAllMenus() { $$(".dropdown-menu.open").forEach(function (m) { m.classList.remove("open"); }); }
  $$("[data-dropdown]").forEach(function (trigger) {
    trigger.addEventListener("click", function (event) {
      event.stopPropagation();
      var menu = document.getElementById(trigger.getAttribute("data-dropdown"));
      if (!menu) return;
      var wasOpen = menu.classList.contains("open");
      closeAllMenus();
      menu.classList.toggle("open", !wasOpen);
    });
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".dropdown")) closeAllMenus();
  });

  /* ------------------------------------------------------------ search */
  var searchInput = $("#global-search");
  var searchResults = $("#search-results");
  var searchTimer = null;

  function renderSearch(items) {
    if (!searchResults) return;
    searchResults.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "search-result";
      empty.innerHTML = '<div class="meta"><div class="s">No matches in your library.</div></div>';
      searchResults.appendChild(empty);
    }
    items.forEach(function (item) {
      var row = document.createElement("a");
      row.className = "search-result";
      row.href = "/content/" + item.id;
      var img = document.createElement("img");
      img.className = "thumb";
      img.alt = "";
      img.loading = "lazy";
      if (item.cover) { img.src = item.cover; img.onerror = function () { img.style.visibility = "hidden"; }; }
      else { img.style.visibility = "hidden"; }
      var meta = document.createElement("div");
      meta.className = "meta";
      var title = document.createElement("div");
      title.className = "t";
      title.textContent = item.title;
      var sub = document.createElement("div");
      sub.className = "s";
      sub.textContent = [item.icon, item.category, item.status_label].filter(Boolean).join(" · ");
      meta.appendChild(title);
      meta.appendChild(sub);
      row.appendChild(img);
      row.appendChild(meta);
      searchResults.appendChild(row);
    });
    searchResults.classList.add("open");
  }

  if (searchInput) {
    searchInput.addEventListener("input", function () {
      var term = searchInput.value.trim();
      clearTimeout(searchTimer);
      if (term.length < 2) { if (searchResults) searchResults.classList.remove("open"); return; }
      searchTimer = setTimeout(function () {
        fetch("/api/search?q=" + encodeURIComponent(term), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (data) { renderSearch(data.results || []); })
          .catch(function () {});
      }, 180);
    });
    searchInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        window.location.href = "/search?q=" + encodeURIComponent(searchInput.value.trim());
      }
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".search-wrap") && searchResults) searchResults.classList.remove("open");
    });
  }

  /* --------------------------------------------------------- favourites */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-favorite]");
    if (!btn) return;
    event.preventDefault();
    event.stopPropagation();
    var id = btn.getAttribute("data-favorite");
    var next = btn.getAttribute("aria-pressed") !== "true";
    btn.disabled = true;
    api("/api/content/" + id + "/favorite", { body: { favorite: next } })
      .then(function (data) {
        btn.setAttribute("aria-pressed", data.favorite ? "true" : "false");
        btn.classList.toggle("on", !!data.favorite);
        btn.textContent = data.favorite ? "★" : "☆";
        toast(data.favorite ? "Added to favorites" : "Removed from favorites");
      })
      .catch(function (err) { toast(err.message, "error"); })
      .finally(function () { btn.disabled = false; });
  });

  /* ------------------------------------------------------- open tracking */
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-open-track]");
    if (!link) return;
    var id = link.getAttribute("data-open-track");
    // Fire and forget: the browser opens the link in a new tab meanwhile.
    api("/api/content/" + id + "/open").catch(function () {});
  });

  /* ------------------------------------------------------ confirm modal */
  var modal = $("#confirm-modal");
  var pendingAction = null;

  function openModal(title, message, confirmLabel, onConfirm) {
    if (!modal) { if (window.confirm(message)) onConfirm(); return; }
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    $("#confirm-ok").textContent = confirmLabel;
    pendingAction = onConfirm;
    modal.classList.add("open");
    var ok = $("#confirm-ok");
    if (ok) ok.focus();
  }

  function closeModal() {
    if (modal) modal.classList.remove("open");
    pendingAction = null;
  }

  var cancelBtn = $("#confirm-cancel");
  if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
  var okBtn = $("#confirm-ok");
  if (okBtn) okBtn.addEventListener("click", function () {
    var action = pendingAction;
    closeModal();
    if (action) action();
  });
  if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

  /* --------------------------------------------------------- delete flow */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-delete]");
    if (!btn) return;
    event.preventDefault();
    var id = btn.getAttribute("data-delete");
    var title = btn.getAttribute("data-title") || "this item";
    var hard = btn.getAttribute("data-hard") === "1";
    openModal(
      hard ? "Delete permanently?" : "Are you sure you want to delete this item?",
      hard
        ? title + " and its history will be removed for good. This cannot be undone."
        : title + " will be moved to Trash, where you can restore it later.",
      hard ? "Delete forever" : "Delete",
      function () {
        api("/api/content/" + id + "/delete", { body: { hard: hard } })
          .then(function () {
            toast(hard ? "Deleted permanently" : "Moved to Trash", "success");
            var card = btn.closest(".card, tr, .row-item");
            if (card) {
              card.style.transition = "opacity .22s, transform .22s";
              card.style.opacity = "0";
              card.style.transform = "scale(.97)";
              setTimeout(function () { card.remove(); }, 230);
            } else {
              setTimeout(function () { window.location.href = "/library"; }, 400);
            }
          })
          .catch(function (err) { toast(err.message, "error"); });
      }
    );
  });

  /* ----------------------------------------------------------- restore */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-restore]");
    if (!btn) return;
    event.preventDefault();
    api("/api/content/" + btn.getAttribute("data-restore") + "/restore")
      .then(function () { toast("Restored", "success"); setTimeout(function () { location.reload(); }, 500); })
      .catch(function (err) { toast(err.message, "error"); });
  });

  /* ------------------------------------------------------- check URL now */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-check-url]");
    if (!btn) return;
    event.preventDefault();
    var id = btn.getAttribute("data-check-url");
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "Checking…";
    api("/api/content/" + id + "/check-url")
      .then(function (data) {
        var labels = { ok: "🟢 Working", degraded: "🟡 Temporarily unavailable", broken: "🔴 Broken", unknown: "⚪ Not checked" };
        toast(labels[data.status] || "Checked");
        var badge = document.querySelector('[data-url-badge="' + id + '"]');
        if (badge) badge.textContent = labels[data.status] || data.status;
      })
      .catch(function (err) { toast(err.message, "error"); })
      .finally(function () { btn.disabled = false; btn.textContent = original; });
  });

  /* --------------------------------------------------- check update now */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-check-update]");
    if (!btn) return;
    event.preventDefault();
    var id = btn.getAttribute("data-check-update");
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "Checking…";
    api("/api/content/" + id + "/check-update")
      .then(function (data) {
        if (data.found) toast("🔔 " + (data.label || "New update available"), "success");
        else if (data.error) toast(data.error, "error");
        else toast("No new updates found.");
      })
      .catch(function (err) { toast(err.message, "error"); })
      .finally(function () { btn.disabled = false; btn.textContent = original; });
  });

  /* -------------------------------------------------------- update state */
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-update-state]");
    if (!btn) return;
    event.preventDefault();
    var id = btn.getAttribute("data-content-id");
    var state = btn.getAttribute("data-update-state");
    api("/api/content/" + id + "/update-state", { body: { state: state } })
      .then(function () {
        toast(state === "ignored" ? "Update ignored" : "Marked as read", "success");
        var card = btn.closest(".card, .update-row");
        if (card) { card.style.opacity = ".55"; }
      })
      .catch(function (err) { toast(err.message, "error"); });
  });

  /* --------------------------------------------------- quick progress UI */
  var quickForm = $("#quick-progress-form");
  if (quickForm) {
    quickForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var id = quickForm.getAttribute("data-id");
      var body = {};
      $$("[name]", quickForm).forEach(function (input) {
        if (input.value !== "") body[input.name] = isNaN(Number(input.value)) ? input.value : Number(input.value);
      });
      api("/api/content/" + id + "/progress", { body: body })
        .then(function (data) {
          toast("Progress saved — " + (data.progress || data.status_label), "success");
          var label = $("[data-progress-label]");
          if (label) label.textContent = data.progress || "—";
          setTimeout(function () { location.reload(); }, 900);
        })
        .catch(function (err) { toast(err.message, "error"); });
    });
  }

  /* ------------------------------------------------------- add/edit form */
  var form = $("#content-form");
  if (form) {
    var categoryInputs = $$('input[name="category"]', form);
    var progressFields = {
      chapter: $("#field-chapter"),
      episode: $("#field-episode"),
      season: $("#field-season"),
      volume: $("#field-volume"),
      progress_text: $("#field-progress_text"),
      progress_percent: $("#field-progress_percent")
    };

    function selectedCategory() {
      var checked = $('input[name="category"]:checked', form);
      return checked ? checked.value : "";
    }

    function applyProgressVisibility() {
      var slug = selectedCategory();
      var el = document.querySelector('[data-fields-for="' + slug + '"]');
      var allowed = el ? (el.getAttribute("data-fields-for-value") || "").split(",") : ["custom"];
      Object.keys(progressFields).forEach(function (key) {
        var node = progressFields[key];
        if (!node) return;
        var show = allowed.indexOf(key) !== -1;
        node.style.display = show ? "" : "none";
        if (!show) {
          var input = $("input, textarea", node);
          if (input) input.value = "";
        }
      });
      var heading = $("#progress-heading");
      if (heading) heading.textContent = progressHeadingFor(slug);
    }

    function progressHeadingFor(slug) {
      return {
        manga: "Reading progress", manhwa: "Reading progress", manhua: "Reading progress",
        anime: "Watching progress", movie: "Watch status", sports: "Follow details",
        news: "Follow details", coding: "Course progress"
      }[slug] || "Progress";
    }

    categoryInputs.forEach(function (input) {
      input.addEventListener("change", function () {
        applyProgressVisibility();
        syncStatusOptions();
      });
    });

    function syncStatusOptions() {
      var slug = selectedCategory();
      var holder = document.querySelector('[data-statuses-for="' + slug + '"]');
      if (!holder) return;
      var allowed = (holder.getAttribute("data-statuses-value") || "").split(",");
      $$(".status-option", form).forEach(function (opt) {
        var value = $("input", opt).value;
        var show = allowed.length === 0 || allowed.indexOf(value) !== -1;
        opt.style.display = show ? "" : "none";
        if (!show && $("input", opt).checked) {
          var first = $('.status-option input[value="following"]', form) || $(".status-option input", form);
          if (first) first.checked = true;
        }
      });
    }

    // Rating slider
    var rating = $('input[name="rating"]', form);
    var ratingOut = $("#rating-value");
    function paintRating() {
      if (!rating || !ratingOut) return;
      var value = Number(rating.value || 0);
      ratingOut.textContent = value ? value + "/10" : "—";
    }
    if (rating) { rating.addEventListener("input", paintRating); paintRating(); }

    // Tag input -> chips
    var tagInput = $('input[name="tags"]', form);
    var tagPreview = $("#tag-preview");
    function paintTags() {
      if (!tagInput || !tagPreview) return;
      tagPreview.innerHTML = "";
      tagInput.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean).forEach(function (name) {
        var chip = document.createElement("span");
        chip.className = "tag";
        chip.textContent = "#" + name;
        tagPreview.appendChild(chip);
      });
    }
    if (tagInput) { tagInput.addEventListener("input", paintTags); paintTags(); }

    // Metadata lookup
    var lookupBtn = $("#lookup-btn");
    var lookupBox = $("#lookup-box");
    if (lookupBtn) {
      lookupBtn.addEventListener("click", function () {
        var url = ($('input[name="url"]', form) || {}).value || "";
        var title = ($('input[name="title"]', form) || {}).value || "";
        if (!url && !title) { toast("Add a URL or title first."); return; }
        lookupBtn.disabled = true;
        lookupBtn.textContent = "Looking up…";
        api("/api/lookup", { body: { url: url, title: title, category: selectedCategory() } })
          .then(function (data) {
            if (($('input[name="url"]', form).value || "") !== url) return;
            if (data.meta && data.meta.title && !$('input[name="title"]', form).value) {
              $('input[name="title"]', form).value = data.meta.title;
            }
            if (data.meta && data.meta.image_url && !$('input[name="cover_image_url"]', form).value) {
              $('input[name="cover_image_url"]', form).value = data.meta.image_url;
              var preview = $("#cover-preview");
              if (preview) { preview.src = data.meta.image_url; preview.style.display = "block"; }
            }
            if (data.meta && data.meta.description && !$('textarea[name="description"]', form).value) {
              $('textarea[name="description"]', form).value = data.meta.description;
            }
            // Never copy available chapter numbers into the user's read progress.
            renderMatches(data.matches || []);
            if (lookupBox) lookupBox.style.display = "block";
            var status = $("#lookup-status");
            if (status) {
              status.textContent = data.meta && data.meta.title
                ? "Found metadata from " + (data.meta.site || data.meta.host || "the page") + "."
                : (data.error || "No page metadata found — choose a catalog match below or fill fields manually.");
              if (data.meta && data.meta.chapter != null) status.textContent += " Highest chapter found on page: " + data.meta.chapter + " (not your progress or a total count).";
              if (data.meta && data.meta.episode != null) status.textContent += " Highest episode found on page: " + data.meta.episode + " (not your progress).";
            }
          })
          .catch(function (err) { toast(err.message, "error"); })
          .finally(function () { lookupBtn.disabled = false; lookupBtn.textContent = "✨ Find metadata"; });
      });
    }

    var urlForLookup = $('input[name="url"]', form);
    if (urlForLookup && lookupBtn) {
      urlForLookup.addEventListener("change", function () {
        if (/^https?:\/\//i.test(urlForLookup.value.trim()) && !lookupBtn.disabled) lookupBtn.click();
      });
    }

    function renderMatches(matches) {
      var list = $("#match-list");
      if (!list) return;
      list.innerHTML = "";
      matches.forEach(function (match) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "match";
        var img = document.createElement("img");
        img.alt = "";
        img.loading = "lazy";
        if (match.image_url) { img.src = match.image_url; img.onerror = function () { img.style.visibility = "hidden"; }; }
        else img.style.visibility = "hidden";
        var body = document.createElement("div");
        var t = document.createElement("div");
        t.className = "t";
        t.textContent = match.title;
        var s = document.createElement("div");
        s.className = "s";
        s.textContent = [match.source, match.note].filter(Boolean).join(" · ");
        body.appendChild(t);
        body.appendChild(s);
        btn.appendChild(img);
        btn.appendChild(body);
        btn.addEventListener("click", function () {
          $('input[name="title"]', form).value = match.title;
          if (match.image_url) {
            $('input[name="cover_image_url"]', form).value = match.image_url;
            var preview = $("#cover-preview");
            if (preview) { preview.src = match.image_url; preview.style.display = "block"; }
          }
          if (match.description) $('textarea[name="description"]', form).value = match.description;
          if (selectedCategory() === "anime" || selectedCategory() === "movie") $('input[name="external_id"]', form).value = match.id || "";
          toast("Applied “" + match.title + "”");
        });
        list.appendChild(btn);
      });
    }

    // Client-side validation mirrors the server rules (server is authoritative).
    form.addEventListener("submit", function (event) {
      var title = $('input[name="title"]', form);
      var url = $('input[name="url"]', form);
      if (title && !title.value.trim()) {
        event.preventDefault();
        title.focus();
        toast("Please enter a title.", "error");
        return;
      }
      if (url && !url.value.trim()) {
        event.preventDefault();
        url.focus();
        toast("Please enter a URL.", "error");
        return;
      }
      var submit = $('button[type="submit"]', form);
      if (submit) { submit.disabled = true; submit.textContent = "Saving…"; }
    });

    applyProgressVisibility();
    syncStatusOptions();
  }

  /* ------------------------------------------------------- notifications */
  document.addEventListener("click", function (event) {
    var readBtn = event.target.closest("[data-notification-read]");
    if (readBtn) {
      event.preventDefault();
      var id = readBtn.getAttribute("data-notification-read");
      api("/api/notifications/" + id + "/read", { body: { is_read: true } })
        .then(function (data) {
          var row = readBtn.closest(".notification-row");
          if (row) row.classList.add("read");
          var badge = $("#nav-unread");
          if (badge) { badge.textContent = data.unread; badge.style.display = data.unread ? "grid" : "none"; }
          readBtn.remove();
        })
        .catch(function (err) { toast(err.message, "error"); });
      return;
    }

    var delBtn = event.target.closest("[data-notification-delete]");
    if (delBtn) {
      event.preventDefault();
      var nid = delBtn.getAttribute("data-notification-delete");
      api("/api/notifications/" + nid, { method: "DELETE" })
        .then(function (data) {
          var row = delBtn.closest(".notification-row");
          if (row) row.remove();
          var badge = $("#nav-unread");
          if (badge) { badge.textContent = data.unread; badge.style.display = data.unread ? "grid" : "none"; }
        })
        .catch(function (err) { toast(err.message, "error"); });
    }
  });

  var markAll = $("#mark-all-read");
  if (markAll) {
    markAll.addEventListener("click", function () {
      api("/api/notifications/read-all")
        .then(function () { toast("All notifications marked as read", "success"); setTimeout(function () { location.reload(); }, 600); })
        .catch(function (err) { toast(err.message, "error"); });
    });
  }

  /* ------------------------------------------------- filters (auto-submit) */
  $$("[data-auto-submit]").forEach(function (el) {
    el.addEventListener("change", function () {
      var formEl = el.closest("form");
      if (formEl) formEl.submit();
    });
  });

  $$("[data-filter-link]").forEach(function (link) {
    link.addEventListener("click", function () {
      $$(".chip").forEach(function (c) { c.classList.remove("active"); });
      link.classList.add("active");
    });
  });

  /* --------------------------------------------------------- view toggle */
  var viewToggle = $("#view-toggle");
  if (viewToggle) {
    viewToggle.addEventListener("click", function () {
      var grid = $("#content-grid");
      if (!grid) return;
      var isList = grid.classList.toggle("list-view");
      viewToggle.textContent = isList ? "▦ Grid" : "☰ List";
      api("/api/settings/view", { body: { view: isList ? "list" : "grid" } }).catch(function () {});
      try { localStorage.setItem("pct-view", isList ? "list" : "grid"); } catch (e) {}
    });
  }
  try {
    var savedView = localStorage.getItem("pct-view");
    var grid = $("#content-grid");
    if (savedView === "list" && grid && !grid.classList.contains("list-view")) {
      grid.classList.add("list-view");
      if (viewToggle) viewToggle.textContent = "▦ Grid";
    }
  } catch (e) {}

  /* ------------------------------------------------- run checks (updates) */
  var runChecks = $("#run-checks");
  if (runChecks) {
    runChecks.addEventListener("click", function () {
      runChecks.disabled = true;
      runChecks.textContent = "Checking…";
      api("/api/run-checks")
        .then(function (data) {
          var u = data.updates || {};
          var l = data.urls || {};
          toast("Checked " + (u.checked || 0) + " item(s) for updates and " + (l.checked || 0) + " link(s).", "success");
          setTimeout(function () { location.reload(); }, 900);
        })
        .catch(function (err) { toast(err.message, "error"); runChecks.disabled = false; runChecks.textContent = "Check now"; });
    });
  }

  /* ------------------------------------------------------- lazy images */
  if ("IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var img = entry.target;
        if (img.dataset.src) { img.src = img.dataset.src; img.removeAttribute("data-src"); }
        observer.unobserve(img);
      });
    }, { rootMargin: "300px" });
    $$("img[data-src]").forEach(function (img) { observer.observe(img); });
  }

  /* Keyboard shortcut: press "/" to focus search, "n" to add. */
  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "/" && searchInput) { e.preventDefault(); searchInput.focus(); }
    if (e.key.toLowerCase() === "n" && !e.metaKey && !e.ctrlKey) { window.location.href = "/add"; }
  });

  /* Auto-dismiss flash messages */
  $$(".alert[data-autohide]").forEach(function (el) {
    setTimeout(function () {
      el.style.transition = "opacity .4s";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 420);
    }, 6000);
  });
})();
