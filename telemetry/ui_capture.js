/* Record every browser interaction without copying form contents or credentials.
 *
 * Captured: page views and in-app navigation, every click (any element), right-click,
 * double-click, setting changes, text-field edits (which field, never the text),
 * form submits, and selected or dropped files (name and size). Values typed into
 * fields are never read or sent. */
(() => {
  if (window.__honeyprintUiCapture) return;
  window.__honeyprintUiCapture = true;

  const LIMIT = 120;
  const clean = value => String(value == null ? '' : value).trim().replace(/\s+/g, ' ').slice(0, LIMIT);
  const where = () => ((location.pathname || '/') + (location.hash || '')).slice(0, 160);
  // Elements a person actually means to press; a click elsewhere is recorded on the area around it.
  const INTERACTIVE = 'button, a, input, select, textarea, label, summary, option, [onclick], ' +
    '[role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [role="checkbox"], ' +
    '[role="switch"], [role="radio"], [role="slider"], [role="treeitem"], [role="row"], [role="listitem"], ' +
    '.v-list-item, .v-tab, .v-chip, .v-btn, .v-expansion-panel-header, .v-slider, tr, li';
  const SECTION = '.v-dialog, .v-card, .v-navigation-drawer, .v-app-bar, header, nav, section, .panel, .modal, .tab-pane';
  const SECTION_TITLE = '.v-card__title, .v-toolbar__title, .v-subheader, .panel-heading, .modal-title, h1, h2, h3, h4, legend';

  function fingerprint(text) {  // stable short ID for an unnamed icon
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    return (hash >>> 0).toString(16).padStart(8, '0').slice(0, 6);
  }

  function section(element) {  // title of the panel, card or dialog the element sits in
    const box = element.closest && element.closest(SECTION);
    const title = box && box.querySelector(SECTION_TITLE);
    return title ? clean(title.textContent).slice(0, 40) : '';
  }

  function kind(element) {
    const role = element.getAttribute && element.getAttribute('role');
    const type = element.getAttribute && element.getAttribute('type');
    return clean((element.tagName || 'node').toLowerCase() + (role ? '[' + role + ']' : '') + (type ? '[' + type + ']' : '')).slice(0, 40);
  }

  function label(element) {
    if (!element || !element.getAttribute) return 'page';
    const vuetify = element.closest && element.closest('.v-input');
    const fieldLabel = (element.labels && element.labels[0] && element.labels[0].textContent) ||
      (vuetify && vuetify.querySelector('.v-label') && vuetify.querySelector('.v-label').textContent);
    const id = /^(input|v-[a-z-]+)-?\d+$/.test(element.id || '') ? '' : element.id;  // skip generated IDs
    const named = clean(element.getAttribute('data-test') || element.getAttribute('data-testid') ||
      element.getAttribute('aria-label') || element.getAttribute('title') || fieldLabel ||
      element.getAttribute('placeholder') || element.getAttribute('name') || id);
    if (named) return named;
    // Unlabelled field: use its descriptive class, e.g. Mainsail's "gcode-command-field".
    const hint = vuetify && [...vuetify.classList].find(name => !/^(v-|theme--|d-|m[trblxy]?-|p[trblxy]?-)/.test(name));
    if (hint && /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)) return clean(hint);
    const where = section(element);
    if (!element.matches(INTERACTIVE)) {
      // A click on a plain area: say where it was, never copy the page's text.
      return clean('area' + (where ? ' (' + where + ')' : ' ' + (element.tagName || '').toLowerCase()));
    }
    // Field values are never used: only names, labels and short visible button text.
    const text = /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName) ? '' : clean(element.textContent);
    if (text && text.length <= 60) return text;
    const icon = element.querySelector && element.querySelector('[class*="mdi-"], [class*="fa-"], svg path');
    const iconName = icon && icon.classList && [...icon.classList].find(name => /^(mdi-|fa-)/.test(name));
    const iconId = iconName || (icon && icon.getAttribute && icon.getAttribute('d') ? 'icon#' + fingerprint(icon.getAttribute('d')) : '');
    const base = iconId || (text ? text.slice(0, 60) : (element.tagName || 'element').toLowerCase());
    return clean(base + (where ? ' (' + where + ')' : ''));
  }

  function send(data) {
    if (window.OctoPrint && typeof OctoPrint.simpleApiCommand === 'function') {
      const request = OctoPrint.simpleApiCommand('honeyprint_audit', 'ui_interaction', data);
      if (request && typeof request.fail === 'function') request.fail(() => {});
      return;
    }
    fetch('/honeyprint/interactions', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    }).catch(() => {});
  }

  function record(action, element, extra = {}) {
    const data = {action, control: element ? label(element) : 'page', page: where(), ...extra};
    if (element && element.getAttribute) data.element = kind(element);
    send(data);
  }

  // Page views and in-app navigation (Mainsail and OctoPrint change pages without reloading).
  let current = where();
  const navigated = () => {
    const now = where();
    if (now === current) return;
    const name = (location.pathname.split('/').filter(Boolean).pop() || location.hash.replace(/^#\/?/, '') || 'home');
    send({action: 'navigate', control: clean(decodeURIComponent(name)) || 'home', page: now, from: current});
    current = now;
  };
  for (const method of ['pushState', 'replaceState']) {
    const original = history[method];
    history[method] = function () { const result = original.apply(this, arguments); setTimeout(navigated, 0); return result; };
  }
  window.addEventListener('popstate', navigated);
  window.addEventListener('hashchange', navigated);
  send({action: 'page_view', control: clean(document.title) || 'page', page: current});
  window.addEventListener('pagehide', () => send({action: 'page_leave', control: clean(document.title) || 'page', page: where()}));

  const target = event => {
    const node = event.target && event.target.nodeType === 1 ? event.target : event.target && event.target.parentElement;
    return node ? (node.closest(INTERACTIVE) || node) : null;
  };
  document.addEventListener('click', event => record('click', target(event)), true);
  document.addEventListener('dblclick', event => record('double_click', target(event)), true);
  document.addEventListener('contextmenu', event => record('context_menu', target(event)), true);
  document.addEventListener('submit', event => record('submit', event.target), true);

  document.addEventListener('change', event => {
    const element = event.target;
    if (!element || !element.matches) return;
    if (element.matches('input[type="file"]')) {
      for (const file of element.files || []) record('file_select', element, {filename: String(file.name).replace(/[\x00-\x1f]/g, '').slice(0, 240), size_bytes: file.size});
    } else if (element.matches('input:not([type]), input[type="text"], input[type="password"], input[type="email"], input[type="search"], input[type="url"], textarea, [contenteditable]')) {
      record('input', element);  // which field was edited, never its contents
    } else {
      record('change', element);
    }
  }, true);

  document.addEventListener('keydown', event => {
    const element = event.target;
    if (event.key === 'Enter' && element && element.matches && element.matches('input, textarea')) record('submit', element);
  }, true);

  document.addEventListener('drop', event => {
    for (const file of (event.dataTransfer && event.dataTransfer.files) || []) {
      record('file_drop', target(event), {filename: String(file.name).replace(/[\x00-\x1f]/g, '').slice(0, 240), size_bytes: file.size});
    }
  }, true);
})();
