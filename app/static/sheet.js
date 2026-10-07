/* Cheat sheet page: typeset the maths.
   The server marks each formula as <span class="tex" data-tex="…"> holding the formula
   as it was written (routes/main.py, sheet_inline). KaTeX replaces that text with the
   typeset formula. A formula it cannot read is left as it was written. */
(function () {
  'use strict';
  if (!window.katex) return;
  Array.from(document.querySelectorAll('.tex')).forEach(function (el) {
    try {
      // To a string first: rendering straight into the span empties it before it can fail.
      el.innerHTML = window.katex.renderToString(el.dataset.tex, {
        displayMode: el.hasAttribute('data-display'), throwOnError: true, strict: 'ignore'
      });
      el.classList.add('is-set');
    } catch (_) { /* the source stays */ }
  });
})();
