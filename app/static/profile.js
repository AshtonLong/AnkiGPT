/* Profile editing: live identity preview and honest unsaved-change feedback. */
(function () {
  'use strict';
  var forms = Array.from(document.querySelectorAll('[data-profile-form]'));
  var editor = document.querySelector('[data-profile-editor]');
  if (!editor) return;
  var avatar = document.querySelector('[data-profile-avatar]');
  var name = document.querySelector('[data-profile-name]');
  var bio = document.querySelector('[data-profile-bio]');
  var originalName = name.textContent;
  var originalBio = bio.textContent;
  var originalAvatar = avatar.className;
  var originalInitials = avatar.textContent;
  var submitting = false;
  function snapshot(form) { return JSON.stringify(Array.from(new FormData(form).entries())); }
  function preview() {
    var displayName = editor.elements.display_name.value.trim();
    var previewName = displayName || name.dataset.profileFallback;
    var words = previewName.split(/\s+/);
    name.textContent = previewName;
    bio.textContent = editor.elements.bio.value.trim() || 'A curious mind. A fresh stack of cards.';
    avatar.textContent = (words.length > 1 ? words[0][0] + words[words.length - 1][0] : previewName.slice(0, 2)).toUpperCase();
    avatar.className = 'profile-avatar profile-avatar-large avatar-' + (editor.elements.avatar_color.value || 'terracotta');
    document.querySelector('[data-bio-count]').textContent = editor.elements.bio.value.length;
  }
  forms.forEach(function (form) {
    var initial = snapshot(form);
    var state = form.querySelector('[data-save-state]');
    function update() {
      var changed = snapshot(form) !== initial;
      form.dataset.dirty = changed ? 'true' : 'false';
      state.textContent = changed ? 'Unsaved changes' : (form.dataset.saveFailed ? 'Changes not saved' : (form === editor ? 'Up to date' : ''));
      state.classList.toggle('is-dirty', changed);
      if (form === editor) preview();
    }
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    form.addEventListener('reset', function () {
      setTimeout(function () {
        update();
        if (form === editor) {
          name.textContent = originalName;
          bio.textContent = originalBio;
          avatar.className = originalAvatar;
          avatar.textContent = originalInitials;
        }
      }, 0);
    });
    form.addEventListener('submit', function () { submitting = true; });
  });
  document.querySelectorAll('form[action$="/logout"]').forEach(function (form) {
    form.addEventListener('submit', function () { submitting = true; });
  });
  window.addEventListener('beforeunload', function (event) {
    if (!submitting && forms.some(function (form) { return form.dataset.dirty === 'true'; })) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  var errors = document.querySelector('[data-profile-errors]');
  if (errors) {
    var invalid = document.querySelector('[aria-invalid="true"]');
    (invalid || errors).focus();
    if (invalid) invalid.scrollIntoView({ block: 'center' });
  }
}());
