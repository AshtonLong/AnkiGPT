/* AnkiGPT — client behaviours
   Progressive enhancement only: every page works without this file; it adds motion,
   toasts, HTMX wiring, and the small interactions (selection tray, counters, tilt).  */
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
    if (t.id === 'status-root') {
      swapMemo.status = { p: parseFloat(t.getAttribute('data-pct')) || 0, done: parseInt(t.getAttribute('data-done'), 10) || 0, state: t.getAttribute('data-state') };
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
    if (el.id === 'status-root') hydrateStatus(swapMemo.status);
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

  /* --------------------------------------------------------- status page */
  function hydrateStatus(prev) {
    var root = doc.getElementById('status-root');
    if (!root) return;
    var state = root.getAttribute('data-state');
    var ring = $('[data-progress]', root);
    var target = parseFloat(root.getAttribute('data-pct')) || 0;
    var from = prev && typeof prev.p === 'number' ? prev.p : 0;
    if (ring) {
      ring.style.setProperty('--p', from);
      // A timer, not rAF: rAF is paused in background tabs, and the ring should still
      // land on the right value even if the page finishes loading while hidden.
      setTimeout(function () { ring.style.setProperty('--p', target); }, 40);
      var num = $('[data-progress-num]', root);
      if (num) tween(num, from, target, 900);
    }
    var prevDone = prev ? prev.done : 0, k = 0;
    $$('.deal-row .mini', root).forEach(function (m, i) {
      if (i >= prevDone) { m.style.setProperty('--k', k++); m.classList.add('new'); }
    });
    if (state === 'processing') doc.title = Math.round(target) + '% · Generating · AnkiGPT';
    else if (state === 'ready') { doc.title = 'Deck ready · AnkiGPT'; if (prev && prev.state === 'processing') toast('Your deck is ready to review.', 'success'); }
    else if (state === 'failed') { doc.title = 'Generation failed · AnkiGPT'; }
    observeReveals(root);
  }

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
    var est = $('[data-estimate]');
    var chars = parseInt((range.closest('form') || {}).getAttribute && range.closest('form').getAttribute('data-source-chars'), 10) || 0;
    function update() {
      var v = parseInt(range.value, 10), min = parseInt(range.min, 10), max = parseInt(range.max, 10);
      range.style.setProperty('--fill', ((v - min) / (max - min) * 100) + '%');
      if (out) out.textContent = fmtInt(v);
      if (est && chars) est.textContent = Math.max(1, Math.ceil(chars / v));
    }
    range.addEventListener('input', update); update();
  })();

  /* --------------------------------------------------------------------- boot */
  observeReveals();
  initCounters();
  initAutogrow();
  syncSelection();
  hydrateStatus(null);
})();
