/* Pricing and billing: the monthly/yearly toggle, the plan finder and number motion. */
(function () {
  'use strict';
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // --- Monthly / yearly. Every toggle on the page stays in sync, and checkout forms
  // carry the chosen interval.
  function setInterval_(value) {
    $$('[data-interval-toggle] input').forEach(function (input) { input.checked = input.value === value; });
    $$('[data-interval-toggle]').forEach(function (toggle) { toggle.dataset.value = value; });
    $$('[data-interval-input]').forEach(function (input) { input.value = value; });
    $$('[data-price]').forEach(function (el) {
      var next = el.dataset[value];
      if (el.textContent === next) return;
      if (reduced) { el.textContent = next; return; }
      el.classList.add('is-swapping');
      window.setTimeout(function () { el.textContent = next; el.classList.remove('is-swapping'); }, 160);
    });
    $$('[data-billed]').forEach(function (el) { el.textContent = el.dataset[value]; });
  }
  $$('[data-interval-toggle] input').forEach(function (input) {
    input.addEventListener('change', function () { if (input.checked) setInterval_(input.value); });
  });

  // --- Plan finder: weekly pages -> monthly estimate -> the smallest plan that fits
  // with ~15% headroom, highlighted in the grid.
  var estimator = document.querySelector('[data-estimator]');
  if (estimator) {
    var range = estimator.querySelector('[data-est-input]');
    var weekly = estimator.querySelector('[data-est-weekly]');
    var monthly = estimator.querySelector('[data-est-monthly]');
    var verdict = estimator.querySelector('[data-est-plan]');
    var cards = $$('[data-plan-grid] [data-plan]');
    var touched = false;
    var update = function () {
      var perWeek = Number(range.value);
      var perMonth = Math.round(perWeek * 4.33);
      weekly.textContent = perWeek;
      monthly.textContent = perMonth;
      range.style.setProperty('--fill', ((perWeek - range.min) / (range.max - range.min) * 100) + '%');
      var fit = null;
      cards.forEach(function (card) {
        if (!fit && Number(card.dataset.pages) >= perMonth * 1.15) fit = card;
      });
      cards.forEach(function (card) {
        var on = touched && card === fit;
        card.classList.toggle('is-fit', on);
        card.querySelector('[data-fit-ribbon]').hidden = !on;
      });
      if (!fit) {
        verdict.textContent = 'That\'s beyond Max. Pick page ranges to focus on what matters.';
      } else {
        var name = fit.querySelector('h3').textContent;
        var spare = Number(fit.dataset.pages) - perMonth;
        verdict.textContent = name + ' fits, with ~' + spare + ' pages to spare.';
      }
    };
    range.addEventListener('input', function () { touched = true; update(); });
    update();
  }

  // --- Count-up for headline numbers once they scroll into view.
  var counters = $$('[data-count]');
  if (counters.length && !reduced && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        io.unobserve(entry.target);
        var el = entry.target, target = Number(el.dataset.count), start = null;
        var step = function (t) {
          if (start === null) start = t;
          var p = Math.min(1, (t - start) / 900);
          el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
          if (p < 1) window.requestAnimationFrame(step);
        };
        el.textContent = '0';
        window.requestAnimationFrame(step);
      });
    }, { threshold: 0.4 });
    counters.forEach(function (el) { io.observe(el); });
  }
})();
