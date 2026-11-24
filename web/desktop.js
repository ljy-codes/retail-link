/* Native path selection for the desktop shell. The browser keeps its existing UI. */
/* Native hosts complete each request through RetailLinkDesktop.resolve(id, result). */
(() => {
  'use strict';
  const native = window.webkit?.messageHandlers?.retailLinkDesktop;
  if (typeof native?.postMessage !== 'function') return;

  const actions = new Set(['chooseDirectory', 'chooseFile', 'saveFile', 'showInFolder']);
  const pending = new Map();
  let sequence = 0;

  function request(action, args = {}) {
    if (!actions.has(action)) return Promise.reject(new Error('无法识别这个桌面操作。'));
    const id = `retail-link-${Date.now()}-${++sequence}`;
    const message = { id, action };
    const initialPath = args.initialPath ?? args.path;
    if (typeof initialPath === 'string' && initialPath.trim()) message.initialPath = initialPath.trim();
    if (typeof args.filename === 'string' && args.filename.trim()) message.filename = args.filename.trim();
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      try { native.postMessage(message); }
      catch (error) {
        pending.delete(id);
        reject(new Error(error?.message || '文件选择窗口未能打开，请重试。'));
      }
    });
  }

  function resolve(id, result = {}) {
    const operation = pending.get(String(id));
    if (!operation) return false;
    pending.delete(String(id));
    if (result?.error) operation.reject(new Error(result.error.message || String(result.error)));
    else operation.resolve(result || {});
    return true;
  }

  window.RetailLinkDesktop = Object.freeze({ enabled: true, request, resolve });

  function splitPath(value) {
    const path = String(value || '').trim();
    const index = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
    return index < 0 ? { directory: '', filename: path } : {
      directory: index === 0 ? path[0] : path.slice(0, index),
      filename: path.slice(index + 1),
    };
  }

  function fill(input, value) {
    if (!input?.isConnected || input.value === value) return;
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  const controls = [
    { id: 'ds-directory', action: 'chooseDirectory', label: '选择经营数据目录', kind: 'directory' },
    { id: 'export-target-dir', action: 'chooseDirectory', label: '选择看盘表保存目录', kind: 'directory' },
    { id: 'export-filename', action: 'saveFile', label: '选择看盘表保存位置和文件名', kind: 'export' },
    { id: 'bundle-output', action: 'saveFile', label: '选择迁移包保存位置', kind: 'zip-output' },
    { id: 'bundle-input', action: 'chooseFile', label: '选择已有 ZIP 迁移包', kind: 'zip-input' },
  ];

  function pickerArguments(input, control) {
    const value = input.value.trim();
    if (control.kind === 'directory') return { initialPath: value };
    if (control.kind === 'export') return {
      initialPath: document.getElementById('export-target-dir')?.value.trim() || '',
      filename: value || '零售通看盘.xlsx',
    };
    const parts = splitPath(value);
    return {
      initialPath: parts.directory,
      filename: parts.filename || '零售通数据.zip',
    };
  }

  function installControl(input, control) {
    if (input.dataset.retailDesktopPicker === 'ready') return;
    input.dataset.retailDesktopPicker = 'ready';
    const wrapper = document.createElement('div');
    wrapper.className = 'rl-desktop-path-control';
    input.parentNode.insertBefore(wrapper, input);
    wrapper.append(input);
    const button = document.createElement('button');
    button.type = 'button';
    button.id = `desktop-choose-${control.id}`;
    button.className = 'rl-desktop-path-button';
    button.textContent = '选择';
    button.setAttribute('aria-label', control.label);
    button.title = control.label;
    const status = document.createElement('small');
    status.className = 'rl-desktop-path-status';
    status.hidden = true;
    status.setAttribute('role', 'status');
    wrapper.append(button);
    wrapper.after(status);
    input.addEventListener('input', () => { status.hidden = true; });
    button.addEventListener('click', async event => {
      event.preventDefault();
      event.stopPropagation();
      if (button.disabled) return;
      status.hidden = true;
      button.disabled = true;
      button.textContent = '选择中…';
      button.setAttribute('aria-busy', 'true');
      try {
        const result = await request(control.action, pickerArguments(input, control));
        if (result.cancelled || !result.path || !input.isConnected) return;
        if (control.kind === 'export') {
          const path = splitPath(result.path);
          const directory = document.getElementById('export-target-dir');
          if (!path.directory || !path.filename || !directory) throw new Error('未获得完整的保存位置，请重新选择。');
          fill(directory, path.directory);
          fill(input, path.filename);
        } else fill(input, result.path);
      } catch (error) {
        if (status.isConnected) {
          status.textContent = error.message || '选择文件未完成，请重试。';
          status.hidden = false;
        }
      } finally {
        button.disabled = false;
        button.textContent = '选择';
        button.removeAttribute('aria-busy');
      }
    });
  }

  function installControls() {
    for (const control of controls) {
      const input = document.getElementById(control.id);
      if (input instanceof HTMLInputElement) installControl(input, control);
    }
  }

  function mount() {
    installControls();
    const observer = new MutationObserver(records => {
      if (records.some(record => record.addedNodes.length)) installControls();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('pagehide', () => {
      observer.disconnect();
      for (const operation of pending.values()) operation.reject(new Error('页面已关闭，请重新打开文件选择。'));
      pending.clear();
    }, { once: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, { once: true });
  else mount();
})();
