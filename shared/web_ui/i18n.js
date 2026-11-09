/* Explicit UI bindings only. Never translate task data or inspect all page text. */
(() => {
  'use strict';
  const key = 'geoworld.ui.language';
  let language = 'en', dictionary = {}, patterns = [], ready = false;
  try {
    const saved = localStorage.getItem(key);
    if (saved === 'en' || saved === 'zh-CN') language = saved;
  } catch (_) {}
  const originals = new WeakMap();
  const selector = '[data-ui-text],[data-ui-output],[data-ui-title],[data-ui-placeholder],[data-ui-aria-label]';
  const missing = new Set();
  function t(source, params = {}) {
    let value = source;
    if (language === 'en') {
      value = dictionary[source] || source;
      if (value === source) {
        for (const pattern of patterns) {
          const match = source.match(pattern.re);
          if (!match) continue;
          value = pattern.target.replace(/\{(\w+)\}/g, (raw, name) => {
            const captured = match[pattern.names.indexOf(name) + 1];
            return captured == null ? raw : (dictionary[captured] || captured);
          });
          break;
        }
      }
      if (ready && !dictionary[source] && /[\u4e00-\u9fff]/.test(source)) missing.add(source);
    }
    return String(value).replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match);
  }
  function translateNode(node) {
    const previous = originals.get(node);
    const source = previous && node.nodeValue === previous.shown ? previous.source : node.nodeValue;
    const trimmed = source.trim();
    const english = node.parentElement?.getAttribute('data-ui-en-text');
    const shown = source.replace(trimmed, () => language === 'en' && english != null
      ? english : t(trimmed));
    originals.set(node, {source, shown});
    if (shown !== node.nodeValue) node.nodeValue = shown;
  }
  function render(element) {
    if (element.closest('[translate="no"],pre,code,[contenteditable="true"]')) return;
    if (element.tagName !== 'TEXTAREA' && (element.hasAttribute('data-ui-text') || element.hasAttribute('data-ui-output'))) {
      for (const node of element.childNodes) if (node.nodeType === 3 && node.nodeValue.trim()) translateNode(node);
    }
    for (const name of ['title', 'placeholder', 'aria-label']) {
      if (!element.hasAttribute('data-ui-' + name)) continue;
      let values = originals.get(element);
      if (!values) { values = {}; originals.set(element, values); }
      const current = element.getAttribute(name) || '';
      const old = values[name];
      const source = old && current === old.shown ? old.source : current;
      const shown = t(source); values[name] = {source, shown};
      if (current !== shown) element.setAttribute(name, shown);
    }
  }
  function scan(root = document) {
    if (root.nodeType === 1 && root.matches(selector)) render(root);
    root.querySelectorAll?.(selector).forEach(render);
  }
  function setLanguage(value, persist = true) {
    language = value === 'en' ? 'en' : 'zh-CN';
    if (persist) { try { localStorage.setItem(key, language); } catch (_) {} }
    document.documentElement.lang = language;
    document.querySelectorAll('[data-ui-language]').forEach(select => { select.value = language; });
    scan();
  }
  function start() {
    const host = document.querySelector('.header,#topbar,header') || document.body;
    const select = document.createElement('select');
    select.className = 'ui-language'; select.setAttribute('data-ui-language', '');
    select.setAttribute('aria-label', '界面语言 / Interface language');
    select.innerHTML = '<option value="zh-CN">中文</option><option value="en">English</option>';
    select.addEventListener('change', () => setLanguage(select.value));
    (host.querySelector('.ui-brand-group') || host).append(select);
    setLanguage(language, false);
    const observer = new MutationObserver(records => {
      const roots = new Set();
      for (const record of records) {
        if (record.type === 'characterData') {
          if (record.target.parentElement?.matches(selector)) roots.add(record.target.parentElement);
        } else {
          roots.add(record.target);
          for (const node of record.addedNodes || []) if (node.nodeType === 1) roots.add(node);
        }
      }
      roots.forEach(scan);
    });
    observer.observe(document.body, {subtree:true, childList:true, characterData:true,
      attributes:true, attributeFilter:['data-ui-text','data-ui-output','data-ui-title','data-ui-placeholder','data-ui-aria-label','title','placeholder','aria-label']});
    fetch('/ui/en.json').then(r => { if (!r.ok) throw Error('UI dictionary unavailable'); return r.json(); })
      .then(data => {
        dictionary = data;
        patterns = Object.entries(data).filter(([source]) => /\{\w+\}/.test(source)).map(([source, target]) => {
          const names = [];
          const parts = source.split(/(\{\w+\})/).map(part => {
            if (/^\{\w+\}$/.test(part)) { names.push(part.slice(1,-1)); return '([\\s\\S]*?)'; }
            return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          });
          return {names, target, re: new RegExp('^' + parts.join('') + '$')};
        });
        ready = true; setLanguage(language, false);
      })
      .catch(error => { console.warn(error); /* Original Chinese remains usable. */ });
    window.addEventListener('storage', event => { if (event.key === key) setLanguage(event.newValue, false); });
  }
  window.GeoUI = {t, setLanguage, scan, get language() {return language;}, missing};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
