/* AnkiGPT — client behaviours
   Progressive enhancement only: every page works without this file; it adds motion,
   toasts, HTMX wiring, the live run trace, and the small interactions (selection tray,
   counters, tilt).  */
(function () {
  'use strict';

  var doc = document;
  var body = doc.body;
  doc.documentElement.classList.add('js');
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var canHover = window.matchMedia('(hover: hover)').matches;

  /* ------------------------------------------------------------------ utils */
  function $(sel, root) { return (root || doc).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function easeOutExpo(t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); }
  function tween(el, from, to, ms, fmt) {
    if (reduced || from === to) { el.textContent = fmt ? fmt(to) : Math.round(to); return; }
    var start = null;
    function frame(ts) {
      if (!start) start = ts;
      var p = Math.min(1, (ts - start) / ms);
      var v = from + (to - from) * easeOutExpo(p);
      el.textContent = fmt ? fmt(v) : Math.round(v);
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }
  function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }
  function fmtInt(n) { return Math.round(n).toLocaleString(); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function fmtMoney(n) { return (Math.round((n || 0) * 1000) / 1000).toFixed(3); }

  /* ---------------------------------------------------------------- top bar */
  var topbar = $('.topbar');
  function onScroll() { if (topbar) topbar.classList.toggle('is-scrolled', window.scrollY > 12); }
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });

  /* ----------------------------------------------------------- progress bar */
  var pbar = $('.pbar'), pTimer = null, pending = 0;
  function pStart() {
    if (!pbar) return;
    pending++;
    clearTimeout(pTimer);
    pbar.classList.add('is-active');
    if (pbar.style.width === '' || pbar.style.width === '0px') {
      pbar.style.width = '0';
      requestAnimationFrame(function () { pbar.style.width = '72%'; });
    }
  }
  function pDone() {
    if (!pbar) return;
    pending = Math.max(0, pending - 1);
    if (pending) return;
    pbar.style.width = '100%';
    pTimer = setTimeout(function () { pbar.classList.remove('is-active'); pbar.style.width = '0'; }, 320);
  }

  /* ----------------------------------------------------------------- toasts */
  var ICON = {
    info: '<svg class="t-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
    success: '<svg class="t-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m8.5 12.5 2.5 2.5 5-5"/></svg>',
    error: '<svg class="t-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>'
  };
  var X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  function dismissToast(t) {
    if (!t || t.classList.contains('leaving')) return;
    t.classList.add('leaving');
    setTimeout(function () { t.remove(); }, 260);
  }
  function toast(message, kind) {
    var region = $('#toasts');
    if (!region) return;
    kind = kind || 'info';
    var el = doc.createElement('div');
    el.className = 'toast ' + kind;
    el.setAttribute('role', 'status');
    el.innerHTML = ICON[kind] + '<div></div><button class="t-x" type="button" aria-label="Dismiss" data-dismiss>' + X + '</button><span class="t-bar"></span>';
    el.children[1].textContent = message;
    region.appendChild(el);
    return el;
  }
  window.AnkiToast = toast;
  body.addEventListener('click', function (e) {
    var x = e.target.closest('[data-dismiss]');
    if (x) dismissToast(x.closest('.toast'));
  });
  body.addEventListener('animationend', function (e) {
    if (e.target.classList && e.target.classList.contains('t-bar')) dismissToast(e.target.closest('.toast'));
  });

  /* -------------------------------------------------------- reveal on view */
  var io = 'IntersectionObserver' in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
    });
  }, { threshold: 0.08, rootMargin: '0px 0px -6% 0px' }) : null;

  function observeReveals(root) {
    root = root || doc;
    $$('[data-stagger]', root).forEach(function (group) {
      var step = parseInt(group.getAttribute('data-stagger'), 10) || 70;
      Array.prototype.forEach.call(group.children, function (child, i) {
        if (child.hasAttribute('data-reveal') && !child.style.getPropertyValue('--d')) {
          child.style.setProperty('--d', Math.min(i, 14) * step);
        }
      });
    });
    $$('[data-reveal]:not(.in)', root).forEach(function (el) {
      if (io) io.observe(el); else el.classList.add('in');
    });
  }
  // Safety net: if the observer hasn't fired (background tab, throttled renderer),
  // reveal anything already inside the viewport so content is never stuck hidden.
  setTimeout(function () {
    $$('[data-reveal]:not(.in)').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.top < window.innerHeight && r.bottom > 0) el.classList.add('in');
    });
  }, 1400);

  /* --------------------------------------------------------------- count-up */
  function initCounters(root) {
    $$('[data-count]', root || doc).forEach(function (el) {
      var end = parseFloat(el.getAttribute('data-count')) || 0;
      var run = function () { tween(el, 0, end, 1100, fmtInt); };
      if (!io) return run();
      var o = new IntersectionObserver(function (en) { if (en[0].isIntersecting) { run(); o.disconnect(); } }, { threshold: 0.4 });
      o.observe(el);
    });
  }

  /* ---------------------------------------------------------- relative time */
  function relTime(date) {
    var s = Math.round((Date.now() - date.getTime()) / 1000);
    if (s < 45) return 'just now';
    var m = Math.round(s / 60); if (m < 60) return m + ' min ago';
    var h = Math.round(m / 60); if (h < 24) return h + (h === 1 ? ' hour ago' : ' hours ago');
    var d = Math.round(h / 24); if (d < 7) return d + (d === 1 ? ' day ago' : ' days ago');
    return null;
  }
  $$('time[data-relative]').forEach(function (t) {
    var d = new Date(t.getAttribute('datetime'));
    if (isNaN(d)) return;
    var r = relTime(d);
    if (r) { t.setAttribute('title', t.textContent); t.textContent = r; }
  });

  /* ------------------------------------------------------- cursor spotlight */
  if (canHover) {
    $$('.feat').forEach(function (card) {
      card.addEventListener('pointermove', function (e) {
        var r = card.getBoundingClientRect();
        card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        card.style.setProperty('--my', (e.clientY - r.top) + 'px');
      });
    });
  }

  /* ----------------------------------------------------------------- tilt */
  if (canHover && !reduced) {
    $$('[data-tilt]').forEach(function (zone) {
      var inner = $('.stack-inner', zone);
      if (!inner) return;
      zone.addEventListener('pointermove', function (e) {
        var r = zone.getBoundingClientRect();
        var x = (e.clientX - r.left) / r.width - 0.5;
        var y = (e.clientY - r.top) / r.height - 0.5;
        inner.style.setProperty('--ry', (x * 16).toFixed(2) + 'deg');
        inner.style.setProperty('--rx', (-y * 12).toFixed(2) + 'deg');
      });
      zone.addEventListener('pointerleave', function () {
        inner.style.setProperty('--rx', '0deg');
        inner.style.setProperty('--ry', '0deg');
      });
    });
  }

  /* ------------------------------------------------------------ flip cards */
  $$('.flip').forEach(function (f) {
    f.setAttribute('tabindex', '0');
    f.setAttribute('role', 'button');
    f.setAttribute('aria-label', 'Flip card');
    var flip = function () { f.classList.toggle('is-flipped'); };
    f.addEventListener('click', flip);
    f.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } });
  });

  /* -------------------------------------------------------- autogrow areas */
  function autogrow(t) { t.style.height = 'auto'; t.style.height = (t.scrollHeight + 2) + 'px'; }
  function initAutogrow(root) {
    $$('textarea[data-autogrow]', root || doc).forEach(function (t) {
      autogrow(t);
      t.addEventListener('input', function () { autogrow(t); });
    });
  }

  /* -------------------------------------------------------- password eye */
  body.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-eye]');
    if (!btn) return;
    var inp = doc.getElementById(btn.getAttribute('data-eye'));
    if (!inp) return;
    var show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.querySelectorAll('svg').forEach(function (s) { s.hidden = !s.hidden; });
    inp.focus();
  });

  /* ------------------------------------------- classic form submit feedback */
  body.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.hasAttribute('hx-post') || form.hasAttribute('hx-get')) return;
    pStart();
    var btn = e.submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (!btn) return;
    // Defer disabling so the submit value is still included in the request.
    setTimeout(function () { btn.classList.add('is-loading'); btn.disabled = true; }, 0);
  });
  window.addEventListener('pageshow', function () {
    $$('button.is-loading').forEach(function (b) { b.classList.remove('is-loading'); b.disabled = false; });
    pending = 0; pDone();
  });

  /* ------------------------------------------------------------- HTMX wiring */
  var csrf = $('meta[name="csrf-token"]');
  csrf = csrf ? csrf.getAttribute('content') : '';
  var swapMemo = {};
  var improveFailed = false;

  body.addEventListener('htmx:configRequest', function (e) { e.detail.headers['X-CSRFToken'] = csrf; });
  body.addEventListener('htmx:beforeRequest', function (e) {
    pStart();
    var elt = e.detail.elt;
    if (elt && elt.classList) elt.classList.add('is-busy');
  });
  body.addEventListener('htmx:afterRequest', function (e) {
    pDone();
    var elt = e.detail.elt;
    if (elt && elt.classList) elt.classList.remove('is-busy');
  });
  body.addEventListener('improveError', function () { improveFailed = true; toast('AI improve failed — the card was left unchanged.', 'error'); });
  body.addEventListener('htmx:responseError', function () { toast('Something went wrong. Please retry.', 'error'); });
  body.addEventListener('htmx:sendError', function () { toast('Network error. Check your connection and retry.', 'error'); });

  body.addEventListener('htmx:beforeSwap', function (e) {
    var t = e.detail.target;
    if (!t) return;
    if (t.classList.contains('card-row')) {
      var cb = $('input[name="card_ids"]', t);
      swapMemo[t.id] = { checked: !!(cb && cb.checked) };
    }
  });
  // afterSettle, not afterSwap: htmx re-applies server attributes (class, style) to
  // id-matched elements after the settle delay, which would wipe anything set earlier.
  body.addEventListener('htmx:afterSettle', function (e) {
    var t = e.detail.target;
    var el = t && t.id ? doc.getElementById(t.id) : null;
    if (!el) return;
    if (el.classList.contains('card-row')) {
      initAutogrow(el);
      var memo = swapMemo[el.id];
      if (memo && memo.checked) { var cb = $('input[name="card_ids"]', el); if (cb) cb.checked = true; }
      syncSelection();
      if (!improveFailed) {
        el.classList.add('just-saved');
        setTimeout(function () { el.classList.remove('just-saved'); }, 1800);
      }
      improveFailed = false;
    }
  });

  /* --------------------------------------------------- editor: selection */
  var tray = $('.tray');
  function syncSelection() {
    var boxes = $$('input[name="card_ids"]');
    var n = 0;
    boxes.forEach(function (b) {
      var row = b.closest('.card-row');
      if (row) row.classList.toggle('is-selected', b.checked);
      if (b.checked) n++;
    });
    if (tray) {
      tray.classList.toggle('is-open', n > 0);
      var c = $('[data-sel-count]', tray);
      if (c) c.textContent = n;
    }
    var all = doc.getElementById('select-all');
    if (all) { all.checked = n > 0 && n === boxes.length; all.indeterminate = n > 0 && n < boxes.length; }
  }
  body.addEventListener('change', function (e) {
    var t = e.target;
    if (t.name === 'card_ids') syncSelection();
    if (t.id === 'select-all') { $$('input[name="card_ids"]').forEach(function (b) { b.checked = t.checked; }); syncSelection(); }
    if (t.hasAttribute('data-autosubmit')) { var f = t.closest('form'); if (f) { pStart(); f.submit(); } }
  });
  body.addEventListener('click', function (e) {
    if (e.target.closest('[data-clear-sel]')) {
      $$('input[name="card_ids"]').forEach(function (b) { b.checked = false; });
      syncSelection();
    }
    var improve = e.target.closest('[data-improve]');
    if (improve) {
      var row = improve.closest('.card-row');
      if (row && row.classList.contains('is-dirty') && !window.confirm('You have unsaved edits on this card. AI improve works from the saved version — discard your edits?')) {
        e.preventDefault(); e.stopPropagation();
      }
    }
  }, true);
  body.addEventListener('input', function (e) {
    var row = e.target.closest('.card-row');
    if (row && e.target.closest('.card-form')) row.classList.add('is-dirty');
  });
  if (tray) {
    var tagIn = $('.tag-in', tray), tagBtn = $('[data-needs-tag]', tray);
    if (tagIn && tagBtn) {
      tagIn.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); tagBtn.click(); } });
      tagBtn.addEventListener('click', function (e) {
        if (!tagIn.value.trim()) { e.preventDefault(); tagIn.focus(); tagIn.style.borderColor = 'var(--rose)'; setTimeout(function () { tagIn.style.borderColor = ''; }, 900); }
      });
    }
  }

  /* --------------------------------------------------- status page: trace */
  var PHASE_VERBS = {
    map: ['Mapping the', 'document'], plan: ['Planning the', 'work order'], figures: ['Reading', 'figures'],
    write: ['Writing', 'cards'], critique: ['Critiquing', 'every card'], reconcile: ['Resolving', 'duplicates'],
    coverage: ['Auditing', 'coverage'], finish: ['Finishing', 'up'], planned: ['Plan ready for', 'review']
  };
  var LOADER = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>';

  function renderTrace(root, data) {
    // Phase strip
    var strip = $('[data-phase-strip]', root);
    if (strip) {
      data.phases.forEach(function (p) {
        var el = strip.querySelector('[data-phase="' + p.key + '"]');
        if (!el) return;
        el.className = 'phase ' + p.status;
        var small = el.querySelector('small');
        if (p.total) {
          if (!small) { small = doc.createElement('small'); el.appendChild(small); }
          small.textContent = p.done + '/' + p.total;
        } else if (small) { small.remove(); }
      });
    }
    // Headline
    var head = $('[data-headline]', root);
    var running = data.tasks.filter(function (t) { return t.status === 'running'; }).length;
    if (head) {
      var verb = PHASE_VERBS[data.phase] || ['Starting the', 'agent'];
      var extra = (running && (data.phase === 'write' || data.phase === 'coverage')) ? ' <small>' + running + ' workers in flight</small>' : '';
      head.innerHTML = LOADER + ' ' + esc(verb[0]) + ' <span class="grad">' + esc(verb[1]) + '</span>' + extra;
    }
    // Live stats
    var t = data.totals || {};
    var set = function (sel, v) { var el = $(sel, root); if (el) el.textContent = v; };
    set('[data-live-calls]', t.calls || 0);
    set('[data-live-cost]', fmtMoney(t.cost));
    set('[data-live-cached]', t.cached || 0);
    set('[data-live-running]', running);
    var ok = $('[data-cards-ok]', root);
    if (ok) { var prevOk = parseInt(ok.textContent, 10) || 0; if (prevOk !== data.cards_ok) tween(ok, prevOk, data.cards_ok, 700); }
    var note = $('[data-planner-note]', root);
    if (note && data.summary) { note.hidden = false; $('[data-planner-summary]', note).textContent = data.summary; }

    // Task tree: keyed rows grouped by phase; the running phase stays open.
    var tbody = $('[data-trace-body]', root);
    if (!tbody) return;
    var byPhase = {};
    data.tasks.forEach(function (task) { (byPhase[task.phase] = byPhase[task.phase] || []).push(task); });
    data.phases.forEach(function (p) {
      if (p.status === 'pending') return;
      var group = tbody.querySelector('[data-phase-group="' + p.key + '"]');
      if (!group) {
        group = doc.createElement('details');
        group.setAttribute('data-phase-group', p.key);
        group.innerHTML = '<summary><i class="ph-dot"></i><b>' + esc(p.label) + '</b><span class="count"></span></summary><div class="trace-rows"></div>';
        group.addEventListener('toggle', function () { group.setAttribute('data-user-toggled', '1'); });
        tbody.appendChild(group);
      }
      group.className = 'trace-phase ' + p.status;
      if (!group.hasAttribute('data-user-toggled')) {
        group.open = p.status === 'running' || (byPhase[p.key] || []).length <= 3;
        group.removeAttribute('data-user-toggled');
      }
      group.querySelector('.count').textContent = p.done + '/' + p.total;
      var rows = group.querySelector('.trace-rows');
      (byPhase[p.key] || []).forEach(function (task) {
        var row = rows.querySelector('[data-task="' + task.id + '"]');
        var meta = task.cards_made ? (task.cards_kept + '/' + task.cards_made + ' kept') : (task.status === 'running' ? 'working…' : (task.status === 'cached' ? 'from cache' : ''));
        if (task.error) meta = task.error.slice(0, 110);
        var sig = task.status + '|' + meta;
        var html = '<i class="ph-dot"></i><span class="tr-label">' + esc(task.label) + '</span>'
          + (task.strategy ? '<span class="chip">' + esc(task.strategy) + '</span>' : '')
          + '<span class="tr-meta' + (task.error ? ' err' : '') + '">' + esc(meta) + '</span>';
        if (!row) {
          row = doc.createElement('div');
          row.setAttribute('data-task', task.id);
          row.innerHTML = html;
          row.className = 'trace-row ' + task.status + ' new';
          rows.appendChild(row);
          setTimeout(function () { row.classList.remove('new'); }, 900);
        } else if (row.getAttribute('data-sig') !== sig) {
          row.innerHTML = html;
          row.className = 'trace-row ' + task.status + ((task.status === 'done' || task.status === 'cached') ? ' flash' : '');
          setTimeout(function () { row.classList.remove('flash'); }, 1200);
        }
        row.setAttribute('data-sig', sig);
      });
    });
  }

  function hydrateStatus() {
    var root = doc.getElementById('status-root');
    if (!root) return;
    var state = root.getAttribute('data-state');
    var ring = $('[data-progress]', root);
    var target = parseFloat(root.getAttribute('data-pct')) || 0;
    if (ring) {
      ring.style.setProperty('--p', 0);
      // A timer, not rAF: rAF is paused in background tabs, and the ring should still
      // land on the right value even if the page finishes loading while hidden.
      setTimeout(function () { ring.style.setProperty('--p', target); }, 40);
      var num = $('[data-progress-num]', root);
      if (num) tween(num, 0, target, 900);
    }
    if (state === 'ready') doc.title = 'Deck ready · AnkiGPT';
    else if (state === 'failed') doc.title = 'Generation failed · AnkiGPT';
    if (state !== 'processing') return;

    var url = root.getAttribute('data-progress-url');
    var lastPct = target, failures = 0;
    function poll() {
      fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      }).then(function (data) {
        failures = 0;
        if (data.status === 'ready' || data.status === 'failed' || data.status === 'planned') {
          var msg = data.status === 'ready' ? 'Your deck is ready to review.' : (data.status === 'planned' ? 'The plan is ready for review.' : 'Generation failed.');
          toast(msg, data.status === 'failed' ? 'error' : 'success');
          setTimeout(function () { window.location.reload(); }, 500);
          return;
        }
        if (ring) {
          ring.style.setProperty('--p', data.pct);
          var num = $('[data-progress-num]', root);
          if (num) tween(num, lastPct, data.pct, 700);
          lastPct = data.pct;
        }
        doc.title = Math.round(data.pct) + '% · Generating · AnkiGPT';
        renderTrace(root, data);
        setTimeout(poll, 1500);
      }).catch(function () {
        failures++;
        setTimeout(poll, Math.min(8000, 1500 * failures));
      });
    }
    setTimeout(poll, 500);
  }

  /* ------------------------------------------------------------ plan page */
  (function () {
    var tasks = $$('[data-plan-task]');
    if (!tasks.length) return;
    var totalEl = $('[data-plan-total]'), countEl = $('[data-plan-count]');
    function recount() {
      var total = 0, count = 0;
      tasks.forEach(function (t) {
        var skip = $('[data-skip]', t), target = $('[data-target]', t);
        var off = skip && skip.checked;
        t.classList.toggle('is-skipped', !!off);
        if (!off) { total += parseInt(target.value, 10) || 0; count++; }
      });
      if (totalEl) totalEl.textContent = total;
      if (countEl) countEl.textContent = count;
    }
    body.addEventListener('change', function (e) { if (e.target.closest('[data-plan-task]')) recount(); });
    body.addEventListener('input', function (e) { if (e.target.hasAttribute('data-target')) recount(); });
    recount();
  })();

  /* ---------------------------------------------------------- new deck page */
  (function () {
    var form = doc.getElementById('new-deck-form');
    if (!form) return;
    var srcRadios = $$('input[name="source_type"]', form);
    var fileInput = doc.getElementById('pdf_file');
    var drop = doc.getElementById('drop');
    function showSource(kind) {
      $$('.src-section', form).forEach(function (s) { s.hidden = s.id !== 'source-' + kind; });
      if (fileInput) fileInput.required = kind === 'pdf';
    }
    srcRadios.forEach(function (r) { r.addEventListener('change', function () { if (r.checked) showSource(r.value); }); });
    var checked = srcRadios.filter(function (r) { return r.checked; })[0];
    showSource(checked ? checked.value : 'text');

    var ta = $('textarea[data-counter]', form), cc = doc.getElementById('cc'), wc = doc.getElementById('wc');
    if (ta && cc && wc) {
      var update = function () {
        cc.textContent = fmtInt(ta.value.length);
        wc.textContent = fmtInt(ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0);
      };
      ta.addEventListener('input', update); update();
    }

    if (drop && fileInput) {
      var nameEl = doc.getElementById('file-name'), sizeEl = doc.getElementById('file-size'), clear = doc.getElementById('file-clear');
      function reflect() {
        var f = fileInput.files && fileInput.files[0];
        drop.classList.toggle('has-file', !!f);
        if (f) { nameEl.textContent = f.name; sizeEl.textContent = fmtBytes(f.size) + ' · PDF'; }
      }
      fileInput.addEventListener('change', reflect);
      ['dragenter', 'dragover'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('is-over'); }); });
      ['dragleave', 'drop'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('is-over'); }); });
      drop.addEventListener('drop', function (e) {
        var files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        if (files[0].type !== 'application/pdf' && !/\.pdf$/i.test(files[0].name)) { toast('Only PDF files are supported.', 'error'); return; }
        try { fileInput.files = files; } catch (err) { /* older browsers */ }
        reflect();
      });
      if (clear) clear.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        fileInput.value = ''; reflect();
      });
      reflect();
    }
  })();

  /* ------------------------------------------------------------ preview page */
  (function () {
    var range = $('input[type="range"][data-range]');
    if (!range) return;
    var out = doc.getElementById(range.getAttribute('data-range'));
    var hidden = doc.getElementById('target_cards_value');
    var est = $('[data-estimate]');
    var form = range.closest('form');
    var chars = parseInt(form ? form.getAttribute('data-source-chars') : 0, 10) || 0;
    var autoLabel = range.getAttribute('data-auto-label') || 'Auto';
    // Mirrors planner.heuristic_target for an average-density document (~3 cards / 1k chars).
    if (est && chars) est.textContent = fmtInt(Math.max(5, Math.round(chars / 1000 * 3)));
    function update() {
      var v = parseInt(range.value, 10), min = parseInt(range.min, 10), max = parseInt(range.max, 10);
      range.style.setProperty('--fill', ((v - min) / (max - min) * 100) + '%');
      if (out) out.textContent = v === 0 ? autoLabel : fmtInt(v);
      if (hidden) hidden.value = v === 0 ? 'auto' : String(v);
    }
    range.addEventListener('input', update); update();
  })();

  /* --------------------------------------------------------------------- boot */
  observeReveals();
  initCounters();
  initAutogrow();
  syncSelection();
  hydrateStatus();
})();
