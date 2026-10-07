/* Cheat sheet page: typeset the maths.
   The server marks each formula as <span class="tex" data-tex="…"> holding the formula
   as it was written (routes/main.py, sheet_inline). KaTeX replaces that text with the
   typeset formula. A formula it cannot read is left as it was written. */
(function () {
  'use strict';
  if (!window.katex) return;
  var set = Array.from(document.querySelectorAll('.tex')).filter(function (el) {
    try {
      // To a string first: rendering straight into the span empties it before it can fail.
      el.innerHTML = window.katex.renderToString(el.dataset.tex, {
        displayMode: el.hasAttribute('data-display'), throwOnError: true, strict: 'ignore'
      });
      el.classList.add('is-set');
      return true;
    } catch (_) { return false; /* the source stays */ }
  });

  /* A formula breaks only after = and +, and paper cannot scroll. One with a piece wider
     than a printed column (about 30 em of the sheet's text on A4 or Letter) is given the
     scale that fits it, which the print styles apply. Measured once the fonts are in. */
  var COLUMN_EM = 30, SMALLEST = 0.5;
  var sheet = document.querySelector('.sheet');
  if (sheet) document.fonts.ready.then(function () {
    var column = COLUMN_EM * parseFloat(getComputedStyle(sheet).fontSize);
    set.forEach(function (el) {
      var widest = Math.max.apply(null, Array.from(el.querySelectorAll('.katex-html > .katex-base')).map(function (piece) {
        return piece.getBoundingClientRect().width;
      }));
      if (widest > column) el.style.setProperty('--fit', Math.max(SMALLEST, column / widest).toFixed(3));
    });
  });
})();
