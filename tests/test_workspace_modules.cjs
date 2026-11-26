/* Module integration contracts. All routes, forms and requests are fictitious. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const rootPath = path.resolve(__dirname, '..');
const specs = [
  {file: 'daily.js', key: 'daily', title: '每日价格与销量', rootId: 'daily-root', api: 'RetailLinkDaily', tab: 'manual'},
  {file: 'dataset.js', key: 'import', title: '数据导入', rootId: 'dataset-root', api: 'RetailLinkDataset', tab: 'configure'},
  {file: 'operations.js', key: 'operations', title: '经营核对', rootId: 'operations-root', api: 'RetailLinkOperations', tab: 'import'},
];

function environment(spec) {
  const registrations = new Map();
  const requests = [];
  const refreshes = [];
  const documentListeners = new Map();
  let current = 'dashboard';

  const document = {
    readyState: 'complete',
    activeElement: null,
    addEventListener(type, handler) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(handler);
    },
    querySelector(selector) {
      return selector === '.sidebar nav' || selector === 'nav[aria-label="主导航"]' ? navigation : null;
    },
    createElement(tag) { return node(tag); },
  };

  function node(tag) {
    return {
      tagName: tag.toUpperCase(),
      ownerDocument: document,
      parentNode: null,
      id: '',
      dataset: {},
      style: {},
      hidden: false,
      disabled: false,
      innerHTML: '',
      value: '',
      children: [],
      attributes: {},
      listeners: new Map(),
      selections: new Map(),
      classList: {add() {}, remove() {}},
      append(child) {
        if (child.parentNode) {
          const siblings = child.parentNode.children;
          const index = siblings.indexOf(child);
          if (index >= 0) siblings.splice(index, 1);
        }
        this.children.push(child);
        child.parentNode = this;
      },
      setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(type, handler) { this.listeners.set(type, handler); },
      querySelector(selector) { return this.selections.get(selector) || null; },
      querySelectorAll() { return []; },
      contains(child) { return this.children.includes(child); },
      focus() { this.ownerDocument.activeElement = this; },
      click() { return this.listeners.get('click')?.({target: this, stopPropagation() {}}); },
    };
  }

  const navigation = node('nav');
  document.body = node('body');
  document.body.style.overflow = 'auto';
  const workspace = {
    register(key, entry) { registrations.set(key, entry); },
    activate(key) {
      if (key !== current) registrations.get(current)?.beforeLeave?.();
      current = key;
      for (const [id, entry] of registrations) entry.root.hidden = id !== key;
      return registrations.get(key)?.open?.();
    },
    refresh(key) { refreshes.push(key); },
    onDocument(callback) { callback(document); },
  };
  const window = {RetailLinkWorkspace: workspace};
  const context = vm.createContext({
    document, window, console, Date, Blob, URLSearchParams, Uint8Array, atob,
    setTimeout() {},
    CustomEvent: class {},
    localStorage: {getItem() { return null; }, setItem() {}},
    fetch: async (url, options = {}) => {
      requests.push({url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null});
      return {
        ok: true,
        json: async () => url === '/api/writeback/settings'
          ? {enabled: false, templates: {}}
          : {items: [], total: 0, defaultDirectory: '/fictional-inputs'},
      };
    },
  });
  const sourcePath = path.join(rootPath, 'web', spec.file);
  const source = fs.readFileSync(sourcePath, 'utf8');
  const bootstrap = spec.file === 'dataset.js'
    ? "  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();"
    : "  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();";
  assert.ok(source.includes(bootstrap), spec.file + ': missing module bootstrap');
  const exposed = "  globalThis.moduleTest = {state, close, getRoot:()=>root};\n";
  vm.runInContext(source.replace(bootstrap, exposed + bootstrap), context, {filename: sourcePath});
  return {
    context, window, document, registrations, requests, refreshes, workspace, node,
    get current() { return current; },
    state: context.moduleTest.state,
    close: () => context.moduleTest.close(),
    get root() { return context.moduleTest.getRoot(); },
    get entry() { return registrations.get(spec.key); },
    get publicApi() { return window[spec.api]; },
  };
}

const settle = () => new Promise(resolve => setImmediate(resolve));
let passed = 0;
async function check(label, action) {
  await action();
  passed += 1;
  console.log('PASS ' + label);
}

(async () => {
  for (const spec of specs) {
    const env = environment(spec);
    await settle();
    await check(spec.file + ' 注册唯一工作台页面和公开入口', () => {
      assert.equal(env.registrations.size, 1);
      assert.equal(env.entry.root, env.root);
      assert.equal(env.root.id, spec.rootId);
      assert.equal(env.entry.title, spec.title);
      assert.equal(typeof env.entry.open, 'function');
      assert.equal(typeof env.publicApi.open, 'function');
      assert.ok(env.entry.nav, 'navigation button is registered');
      assert.equal(env.root.hidden, true, 'boot does not open a function automatically');
    });

    await check(spec.file + ' 左侧导航首次打开普通页面且不锁滚动', async () => {
      env.entry.nav.click();
      await settle();
      assert.equal(env.current, spec.key);
      assert.equal(env.root.hidden, false);
      assert.match(env.root.innerHTML, /role="region"/);
      assert.match(env.root.innerHTML, /aria-modal="false"/);
      assert.doesNotMatch(env.root.innerHTML, /role="dialog"|aria-modal="true"/);
      assert.equal(env.document.body.style.overflow, 'auto');
      assert.ok(env.refreshes.includes(spec.key));
    });

    const form = env.node('input');
    form.value = '用户尚未保存的输入';
    env.root.append(form);
    env.root.innerHTML += '<span data-draft-marker="retained">未保存草稿</span>';
    const html = env.root.innerHTML;
    const preview = {token: 'fictional-preview-' + spec.key, validCount: 1};
    env.state.preview = preview;
    if (spec.key === 'import') {
      env.state.mode = spec.tab;
      env.state.stage = {sheets: [], templates: []};
    } else env.state.tab = spec.tab;
    const requestCount = env.requests.length;

    await check(spec.file + ' 离开并从导航返回保留原DOM与输入', async () => {
      env.workspace.activate('dashboard');
      assert.equal(env.root.hidden, true);
      env.entry.nav.click();
      await settle();
      assert.equal(env.current, spec.key);
      assert.equal(env.root.hidden, false);
      assert.equal(env.root.innerHTML, html);
      assert.ok(env.root.children.includes(form), 'input keeps its node identity');
      assert.equal(form.value, '用户尚未保存的输入');
    });

    await check(spec.file + ' 重复进入保留标签及预览且不重复读写', async () => {
      for (let index = 0; index < 3; index += 1) {
        env.workspace.activate('dashboard');
        await env.publicApi.open();
        await settle();
      }
      assert.equal(env.state.preview, preview, 'server preview token stays attached to its draft');
      assert.equal(spec.key === 'import' ? env.state.mode : env.state.tab, spec.tab);
      assert.equal(env.root.innerHTML, html);
      assert.equal(form.value, '用户尚未保存的输入');
      assert.equal(env.requests.length, requestCount, 'returning to a route does not restart module I/O');
      assert.ok(env.requests.every(request => request.method === 'GET' && request.body === null), 'navigation never writes business data');
    });

    await check(spec.file + ' 页面返回动作不销毁草稿或锁定主窗口', async () => {
      env.close();
      await settle();
      assert.equal(env.current, 'dashboard');
      assert.equal(env.root.hidden, true);
      assert.equal(env.root.innerHTML, html);
      assert.equal(env.state.preview, preview);
      assert.equal(env.document.body.style.overflow, 'auto');
      assert.equal(env.requests.length, requestCount);
    });
  }
  console.log(`\n${passed} tests, ${passed} passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
