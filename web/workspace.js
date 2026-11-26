/* A persistent page workspace. Detaching moves the same DOM and handlers. */
(() => {
  'use strict';
  const entries = new Map(), observers = new Set();
  let active = 'dashboard', lastDocument = document;
  let pages, toolbar, title, detachButton;

  function documents() {
    return [document, ...[...entries.values()].filter(e => e.popup && !e.popup.closed).map(e => e.popup.document)];
  }
  function watchDocument(doc) {
    doc.addEventListener('pointerdown', () => { lastDocument = doc; }, true);
    doc.addEventListener('click', () => { lastDocument = doc; }, true);
  }
  function querySelector(selector) {
    for (const doc of [...new Set([lastDocument, ...documents()])]) {
      try { const node = doc.querySelector(selector); if (node) return node; } catch {}
    }
    return null;
  }
  function querySelectorAll(selector) {
    return [...new Set(documents().flatMap(doc => [...doc.querySelectorAll(selector)]))];
  }
  function onDocument(callback) {
    observers.add(callback);
    for (const doc of documents().slice(1)) callback(doc);
  }
  function navigation(entry) {
    const result = entry.nav ? (Array.isArray(entry.nav) ? entry.nav : [entry.nav]) : [];
    return [...new Set([...result, ...document.querySelectorAll(`[data-workspace-nav="${entry.key}"]`)])];
  }
  function ensureShell() {
    if (pages) return;
    const app = document.getElementById('app');
    if (!app) return;
    pages = document.createElement('div'); pages.id = 'workspace-pages';
    app.after(pages);
    toolbar = document.createElement('div'); toolbar.className = 'workspace-toolbar';
    toolbar.innerHTML = '<div><span class="workspace-eyebrow">RETAIL LINK / WORKSPACE</span><strong id="workspace-page-title">电商看盘</strong></div><button type="button" class="btn btn-small" id="workspace-detach">在新窗口打开 ↗</button>';
    app.before(toolbar);
    title = toolbar.querySelector('strong'); detachButton = toolbar.querySelector('button');
    const tools = document.querySelector('.topbar-right');
    if (tools) { tools.prepend(detachButton); toolbar.hidden = true; }
    detachButton.addEventListener('click', () => detach(active));
    watchDocument(document);
  }
  function refresh(key) {
    const entry = entries.get(key); if (!entry) return;
    entry.root.classList.add('workspace-page');
    entry.root.setAttribute('data-workspace-page', key);
    entry.root.setAttribute('role', 'region');
    entry.root.setAttribute('aria-label', entry.title);
    for (const panel of entry.root.querySelectorAll('[aria-modal="true"]')) {
      panel.removeAttribute('aria-modal'); panel.setAttribute('role', 'region');
    }
    for (const button of navigation(entry)) button.setAttribute('data-workspace-nav', key);
    syncNavigation();
  }
  function register(key, options) {
    ensureShell();
    const previous = entries.get(key);
    if (previous) { Object.assign(previous, options); refresh(key); return previous; }
    let root = options.root;
    if (!root) { root = document.createElement('section'); root.id = `workspace-${key}`; }
    const entry = {key, ...options, root, opened: false, opening: false, popup: null, placeholder: null};
    entries.set(key, entry);
    if (key !== 'dashboard') pages.append(root);
    root.hidden = key !== active;
    refresh(key);
    return entry;
  }
  function syncNavigation() {
    for (const entry of entries.values()) {
      for (const button of navigation(entry)) {
        const selected = entry.key === active;
        button.classList.toggle('active', selected);
        if (selected) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      }
    }
    const entry = entries.get(active); if (!entry || !title) return;
    title.textContent = entry.title;
    const crumb = document.querySelector('.breadcrumb strong'); if (crumb) crumb.textContent = entry.title;
    detachButton.textContent = entry.popup ? '查看独立窗口 ↗' : '在新窗口打开 ↗';
  }
  function activate(key) {
    ensureShell();
    const entry = entries.get(key); if (!entry) return false;
    if (active !== key) entries.get(active)?.beforeLeave?.();
    active = key; lastDocument = document;
    document.body.dataset.workspaceRoute = key;
    for (const e of entries.values()) {
      if (!e.popup) e.root.hidden = e.key !== key;
      if (e.placeholder) e.placeholder.hidden = e.key !== key;
    }
    document.body.style.overflow = '';
    if (entry.popup && !entry.popup.closed) entry.popup.focus();
    const wasOpened = entry.opened;
    if (!entry.opened && !entry.opening) {
      entry.opening = true;
      try {
        const result = entry.open?.();
        entry.opened = true;
        if (result?.catch) result.catch(error => showMessage(error.message));
      } catch (error) { showMessage(error.message); }
      finally { entry.opening = false; }
    }
    if (wasOpened) entry.onActivate?.();
    syncNavigation();
    document.dispatchEvent(new CustomEvent('retail-workspace-change', {detail: {key}}));
    return true;
  }
  function present(key, html, options = {}) {
    const entry = entries.get(key) || register(key, options);
    entry.root.innerHTML = html; entry.opened = true;
    refresh(key); activate(key);
    return entry.root;
  }
  function copyTheme(doc) {
    for (const attr of document.documentElement.attributes) doc.documentElement.setAttribute(attr.name, attr.value);
    doc.documentElement.style.cssText = document.documentElement.style.cssText;
  }
  function showMessage(message) {
    const area = document.getElementById('toast-root');
    if (area) { const node = document.createElement('div'); node.className = 'toast error'; node.textContent = message; area.append(node); setTimeout(() => node.remove(), 7000); }
  }
  function detach(key = active) {
    const entry = entries.get(key); if (!entry) return false;
    if (entry.popup && !entry.popup.closed) { entry.popup.focus(); return true; }
    // A user click opens an inherited-origin blank window. No second app or
    // independent data state is created; the live feature nodes are adopted.
    const popup = window.open('about:blank', `retail-link-${key}`, 'popup=yes,width=1180,height=820');
    if (!popup) { showMessage('独立窗口未能打开，请允许当前应用打开窗口后重试。'); return false; }
    const doc = popup.document;
    doc.open(); doc.write('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body class="workspace-popout"><header class="workspace-popout-bar"><div><small>零售通 · 独立工作窗口</small><strong></strong></div><button type="button" class="btn" id="workspace-return">返回主窗口 ↙</button></header><div id="workspace-detached-content"></div><div id="modal-root"></div><div id="drawer-root"></div><div id="toast-root" aria-live="polite"></div></body></html>'); doc.close();
    doc.title = `${entry.title} · 零售通`;
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
      const copy = link.cloneNode(); copy.href = new URL(link.getAttribute('href'), window.location.href).href; doc.head.append(copy);
    }
    const sprite = document.querySelector('.svg-sprite'); if (sprite) doc.body.prepend(sprite.cloneNode(true));
    copyTheme(doc); watchDocument(doc);
    doc.querySelector('.workspace-popout-bar strong').textContent = entry.title;
    const placeholder = document.createElement('section'); placeholder.className = 'workspace-detached-notice card';
    const heading = document.createElement('h2'); heading.textContent = `${entry.title}已在独立窗口打开`;
    const text = document.createElement('p'); text.textContent = '当前输入、筛选和操作结果保留在同一个工作页面中。';
    const focus = document.createElement('button'); focus.className = 'btn btn-primary'; focus.textContent = '查看独立窗口'; focus.onclick = () => popup.focus();
    const back = document.createElement('button'); back.className = 'btn'; back.textContent = '返回右侧展示区'; back.onclick = () => restore(key);
    placeholder.append(heading, text, focus, back);
    entry.root.replaceWith(placeholder); entry.placeholder = placeholder; entry.popup = popup;
    entry.root.hidden = false; doc.getElementById('workspace-detached-content').append(entry.root);
    for (const callback of observers) callback(doc);
    doc.getElementById('workspace-return').onclick = () => restore(key);
    popup.addEventListener('beforeunload', () => restore(key, true));
    popup.addEventListener('pagehide', () => restore(key, true), {once: true});
    lastDocument = doc; popup.focus(); syncNavigation();
    return true;
  }
  function restore(key, closing = false) {
    const entry = entries.get(key); if (!entry?.popup) return;
    const popup = entry.popup;
    entry.popup = null;
    if (entry.placeholder?.isConnected) entry.placeholder.replaceWith(entry.root);
    else (key === 'dashboard' ? document.querySelector('.app-shell') : pages).append(entry.root);
    entry.placeholder = null; entry.root.hidden = active !== key;
    lastDocument = document;
    if (!closing && !popup.closed) popup.close();
    refresh(key);
    if (active === key) entry.root.querySelector('input,button,select')?.focus({preventScroll: true});
  }
  function restoreAll() { for (const key of entries.keys()) restore(key); }
  function invalidate(key) {
    const entry = entries.get(key); if (!entry) return;
    if (entry.popup) restore(key);
    entry.root.replaceChildren(); entry.opened = false;
    if (active === key) activate('dashboard');
  }
  window.addEventListener('operator-appearance-change', () => {
    for (const doc of documents().slice(1)) copyTheme(doc);
  });
  window.addEventListener('pagehide', restoreAll);
  window.RetailLinkWorkspace = Object.freeze({register, refresh, activate, present, detach, restore, restoreAll, invalidate,
    onDocument, documents, querySelector, querySelectorAll, get activeKey() {return active;},
    get activeDocument() {return lastDocument;}, has(key) {return entries.has(key);}});
})();
