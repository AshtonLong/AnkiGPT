/* Source to Recall · navigation, dialogs, and the interactive sample.
   Real mutations stay on the existing authenticated, CSRF-protected routes. */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.from((r || document).querySelectorAll(s)); };
  var body = document.body;
  var navToggle = $('[data-nav-toggle]'), sidebar = $('.sidebar');
  var mobile = window.matchMedia('(max-width: 760px)');
  function closeNav() {
    body.classList.remove('nav-open');
    if (navToggle) navToggle.setAttribute('aria-expanded', 'false');
    var scrim = $('[data-close-nav]'); if (scrim) scrim.hidden = true;
    if (sidebar) sidebar.inert = mobile.matches;
  }
  if (sidebar) {
    closeNav();
    mobile.addEventListener('change', closeNav);
    navToggle.addEventListener('click', function () {
      var open = !body.classList.contains('nav-open');
      body.classList.toggle('nav-open', open);
      navToggle.setAttribute('aria-expanded', String(open));
      $('[data-close-nav]').hidden = !open;
      sidebar.inert = !open && mobile.matches;
      if (open) $('a', sidebar).focus();
    });
    $('[data-close-nav]').addEventListener('click', function () { closeNav(); navToggle.focus(); });
    document.addEventListener('keydown', function (e) {
      if (!body.classList.contains('nav-open')) return;
      if (e.key === 'Escape') { closeNav(); navToggle.focus(); }
      if (e.key === 'Tab') {
        var links = $$('a,button', sidebar), first = links[0], last = links[links.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
  }
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    clearTimeout(dialog.closeTimer);
    dialog.classList.remove('is-closing');
    body.classList.add('dialog-open'); dialog.showModal();
  }
  function closeDialog(dialog) {
    if (!dialog || !dialog.open || dialog.classList.contains('is-closing')) return;
    if (motion.matches) { dialog.close(); return; }
    dialog.classList.add('is-closing');
    dialog.closeTimer = setTimeout(function () { dialog.close(); }, 160);
  }
  $$('[data-open-dialog]').forEach(function (button) {
    button.addEventListener('click', function () { openDialog(document.getElementById(button.dataset.openDialog)); });
  });
  $$('[data-close-dialog]').forEach(function (button) {
    button.addEventListener('click', function () { closeDialog(button.closest('dialog')); });
  });
  $$('dialog').forEach(function (dialog) {
    dialog.addEventListener('close', function () {
      clearTimeout(dialog.closeTimer); dialog.classList.remove('is-closing');
      if (!$('dialog[open]')) body.classList.remove('dialog-open');
    });
    dialog.addEventListener('cancel', function (e) { e.preventDefault(); closeDialog(dialog); });
    dialog.addEventListener('click', function (e) {
      if (e.target !== dialog) return;
      var r = dialog.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeDialog(dialog);
    });
  });
  var confirmForm = null;
  $$('[data-confirm-deck]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (form.dataset.confirmed) return;
      e.preventDefault(); e.stopImmediatePropagation();
      confirmForm = form;
      $('[data-confirm-copy]').textContent = '“' + form.dataset.confirmDeck + '”';
      openDialog($('#confirm-dialog'));
      $('[data-close-dialog]', $('#confirm-dialog')).focus();
    });
  });
  $('[data-confirm-accept]').addEventListener('click', function () {
    if (!confirmForm) return;
    confirmForm.dataset.confirmed = 'true';
    $('#confirm-dialog').close();
    confirmForm.requestSubmit();
  });
  var filters = $('[data-toggle-filters]');
  if (filters) filters.addEventListener('click', function () {
    var open = filters.closest('form').classList.toggle('filters-open');
    filters.setAttribute('aria-expanded', String(open));
  });

  var importForm = $('[data-import-form]');
  if (importForm) {
    var packageInput = $('input[type=file]', importForm), importResult = $('[data-import-result]', importForm);
    packageInput.addEventListener('change', function () {
      var file = packageInput.files[0];
      $('[data-import-file]').textContent = file ? file.name + ' · ' + (file.size / 1024).toFixed(0) + ' KB' : 'No file selected · .apkg or .colpkg';
      packageInput.setCustomValidity(file && !/\.(apkg|colpkg)$/i.test(file.name) ? 'Choose an .apkg or .colpkg file.' : '');
      importResult.hidden = true;
    });
    importForm.addEventListener('submit', async function (e) {
      e.preventDefault(); e.stopPropagation();
      var button = $('button[type=submit]', importForm);
      button.disabled = true; button.classList.add('is-loading');
      importResult.className = 'hint'; importResult.hidden = false;
      importResult.textContent = 'Reading review history and matching your cards…';
      try {
        var response = await fetch(importForm.action, { method:'POST', body:new FormData(importForm), headers:{Accept:'application/json'} });
        var result;
        try { result = await response.json(); } catch (_) { throw new Error('The upload could not be completed. Check the file size and your connection, then retry.'); }
        if (!response.ok || !result.ok) throw new Error(result.message || 'Could not import this package.');
        importResult.className = 'download-state'; importResult.textContent = result.message;
        var link = document.createElement('a'); link.className = 'btn block'; link.href = result.url; link.textContent = 'Review matched cards →';
        importResult.appendChild(document.createElement('br')); importResult.appendChild(link);
      } catch (error) {
        importResult.className = 'form-error'; importResult.textContent = error.message || 'Could not connect. Please retry.';
      } finally { button.disabled = false; button.classList.remove('is-loading'); }
    });
  }
  var exportForm = $('[data-export-form]');
  if (exportForm) exportForm.addEventListener('submit', async function (e) {
    e.preventDefault(); e.stopPropagation();
    var button = $('button[type=submit]', exportForm), result = $('[data-export-result]', exportForm);
    button.disabled = true; button.classList.add('is-loading'); result.hidden = false; result.className = 'hint'; result.textContent = 'Packaging your cards and images…';
    try {
      var response = await fetch(exportForm.action, {method:'POST', body:new FormData(exportForm), headers:{Accept:'application/octet-stream'}});
      if (!response.ok || response.redirected || (response.headers.get('Content-Type') || '').includes('text/html')) throw new Error('The deck could not be exported. Return to your cards and try again.');
      var blob = await response.blob(), url = URL.createObjectURL(blob), a = document.createElement('a');
      a.href = url; a.download = exportForm.dataset.filename.replace(/[<>:"/\\|?*]/g, '_'); document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
      result.className = 'download-state'; result.textContent = 'Your package is ready. Check your downloads, then import it into Anki.';
    } catch (error) { result.className = 'form-error'; result.textContent = error.message; }
    finally { button.disabled = false; button.classList.remove('is-loading'); }
  });

  var newForm = $('#new-deck-form');
  if (newForm) {
    var textInput = $('#text-input'), start = $('#page-start'), end = $('#page-end');
    function validateSource() {
      var pdf = $('#src-pdf').checked;
      textInput.required = !pdf;
      end.setCustomValidity(pdf && start.value && end.value && Number(end.value) < Number(start.value) ? 'End page must be the same as or after the start page.' : '');
    }
    newForm.addEventListener('input', validateSource); newForm.addEventListener('change', validateSource); validateSource();
  }

  var demo = $('[data-demo]');
  if (demo) {
    var samples = {
      data: {title:'Stacks & queues', file:'DATA_STRUCTURES.PDF / SAMPLE', exportName:'Data structures.apkg', cards:[
        {type:'Basic', question:'Which order does a stack follow?', answer:'Last in, first out (LIFO).', passage:'A stack follows a last-in, first-out (LIFO) order. The most recently added item is the first to be removed.'},
        {type:'Cloze', question:'A queue follows a […] order.', answer:'first-in, first-out (FIFO)', passage:'A queue follows a first-in, first-out (FIFO) order. The earliest added item is removed first.'}]},
      french: {title:'Words for every day', file:'FRENCH_VOCABULARY.TXT / SAMPLE', exportName:'French vocabulary.apkg', cards:[
        {type:'Basic', question:'What does “apprendre” mean in English?', answer:'To learn.', passage:'Apprendre means “to learn”. Je veux apprendre le français means “I want to learn French”.'},
        {type:'Cloze', question:'“Se souvenir” means […].', answer:'to remember', passage:'Se souvenir means “to remember”. Je me souviens de cette histoire means “I remember this story”.'}]}
    };
    var topic = 'data', selected = 0, revealed = false;
    function renderCard() {
      var data = samples[topic], card = data.cards[selected];
      $('[data-demo-filename]').textContent = data.file; $('[data-demo-title]').textContent = data.title;
      $('[data-demo-export-name]').textContent = data.exportName;
      $$('[data-demo-card]').forEach(function (button, index) {
        button.textContent = data.cards[index].passage;
        button.classList.toggle('selected', index === selected); button.setAttribute('aria-pressed', String(index === selected));
      });
      $('[data-demo-question]').textContent = card.question; $('[data-demo-answer]').textContent = card.answer;
      $('[data-demo-type]').textContent = card.type; $('[data-demo-edit-type]').textContent = card.type;
      $('[data-demo-number]').textContent = '0' + (selected + 1) + ' / 02';
      $('[data-demo-answer]').hidden = !revealed; $('[data-demo-reveal]').textContent = revealed ? 'Hide answer' : 'Reveal answer';
      $('#demo-front').value = card.question; $('#demo-back').value = card.answer; $('[data-demo-saved]').textContent = '';
    }
    function showTab(name, focus) {
      $$('[data-demo-tab]').forEach(function (button) {
        var active = button.dataset.demoTab === name;
        button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
        document.getElementById('demo-' + button.dataset.demoTab).hidden = !active;
        if (active && focus) button.focus();
      });
    }
    $$('[data-demo-tab]').forEach(function (button, index, buttons) {
      button.addEventListener('click', function () { showTab(button.dataset.demoTab); });
      button.addEventListener('keydown', function (e) {
        var next = e.key === 'ArrowRight' ? (index + 1) % 3 : e.key === 'ArrowLeft' ? (index + 2) % 3 : e.key === 'Home' ? 0 : e.key === 'End' ? 2 : -1;
        if (next >= 0) { e.preventDefault(); showTab(buttons[next].dataset.demoTab, true); }
      });
    });
    $$('[data-demo-card]').forEach(function (button) { button.addEventListener('click', function () { selected = Number(button.dataset.demoCard); revealed = false; renderCard(); }); });
    $('[data-demo-reveal]').addEventListener('click', function () { revealed = !revealed; $('[data-demo-answer]').hidden = !revealed; this.textContent = revealed ? 'Hide answer' : 'Reveal answer'; });
    $('[data-demo-next]').addEventListener('click', function () { showTab('review', true); });
    $('[data-demo-topic]').addEventListener('change', function () { topic = this.value; selected = 0; revealed = false; renderCard(); });
    $('[data-demo-form]').addEventListener('submit', function (e) {
      e.preventDefault(); e.stopPropagation(); var card = samples[topic].cards[selected];
      card.question = $('#demo-front').value.trim(); card.answer = $('#demo-back').value.trim(); renderCard();
      $('[data-demo-saved]').textContent = 'Saved in this sample';
    });
  }
  $$('[data-coach-example]').forEach(function (button) {
    button.addEventListener('click', function () {
      var after = button.dataset.coachExample === 'after';
      $('[data-coach-before]').hidden = after; $('[data-coach-after]').hidden = !after;
      $$('[data-coach-example]').forEach(function (b) { b.setAttribute('aria-pressed', String(b === button)); });
    });
  });
})();
