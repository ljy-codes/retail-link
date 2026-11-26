/* Observable behavior checks for the offline appearance controller.
 * No browser settings, source workbook, business preferences or production data
 * are written. Every controller load uses an isolated VM and in-memory storage.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const sourcePath = path.join(root, 'web', 'appearance.js');
const expectedPalettes = {
  focus: ['#0B4F4A', '#DEFFF2'],
  violet: ['#A99AFF', '#7054DE'],
};
const originalBusinessValue = JSON.stringify([{ name: '业务配置', config: { targetRate: 0.85 } }]);
const businessKey = 'operator-console-profiles-v1';

function environment(options = {}) {
  const storageMap = new Map(Object.entries(options.seed || {}));
  if (!storageMap.has(businessKey)) storageMap.set(businessKey, originalBusinessValue);
  const writes = [];
  const domListeners = new Map();
  const windowListeners = new Map();
  const mediaListeners = new Set();
  const styles = new Map();
  const attributes = new Map();
  const media = {
    matches: Boolean(options.dark),
    media: '(prefers-color-scheme: dark)',
    addEventListener(type, callback) { if (type === 'change') mediaListeners.add(callback); },
    removeEventListener(type, callback) { if (type === 'change') mediaListeners.delete(callback); },
    addListener(callback) { mediaListeners.add(callback); },
    removeListener(callback) { mediaListeners.delete(callback); },
  };
  const style = {
    setProperty(key, value) { styles.set(key, String(value)); },
    getPropertyValue(key) { return styles.get(key) || ''; },
    removeProperty(key) { styles.delete(key); },
    colorScheme: '',
  };
  const html = {
    dataset: {}, style,
    setAttribute(key, value) {
      attributes.set(key, String(value));
      if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = String(value);
    },
    getAttribute(key) {
      if (key.startsWith('data-')) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] || null;
      return attributes.get(key) || null;
    },
  };
  const meta = { setAttribute(key, value) { attributes.set('meta:' + key, String(value)); }, getAttribute(key) { return attributes.get('meta:' + key) || null; } };
  const document = {
    documentElement: html, readyState: 'loading', body: null,
    querySelector(selector) { return /meta/.test(selector) ? meta : selector === 'html' || selector === ':root' ? html : null; },
    getElementById() { return null; },
    addEventListener(type, callback) { domListeners.set(type, callback); },
    removeEventListener(type) { domListeners.delete(type); },
  };
  const storage = {
    getItem(key) {
      if (options.storageFailure === 'all' || options.storageFailure === 'read') throw new Error('storage read denied');
      return storageMap.has(key) ? storageMap.get(key) : null;
    },
    setItem(key, value) {
      if (options.storageFailure === 'all' || options.storageFailure === 'write') throw new Error('storage write denied');
      writes.push([key, String(value)]);
      storageMap.set(key, String(value));
    },
    removeItem(key) { storageMap.delete(key); },
  };
  const window = {
    document, localStorage: storage,
    matchMedia() { return media; },
    addEventListener(type, callback) { windowListeners.set(type, callback); },
    removeEventListener(type) { windowListeners.delete(type); },
    dispatchEvent() {},
  };
  const context = vm.createContext({
    window, document, localStorage: storage, matchMedia: () => media,
    console: { log() {}, warn() {}, error() {} },
    setTimeout() { return 1; }, clearTimeout() {},
    requestAnimationFrame(callback) { callback(); return 1; },
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  });
  vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), context, { filename: sourcePath, timeout: 2000 });
  const api = window.OperatorAppearance;
  assert.ok(api, '控制器应提供独立的外观接口');
  assert.equal(typeof api.getPreferences, 'function');
  assert.equal(typeof api.applyPreferences, 'function');
  return {
    api, html, styles, storageMap, writes, domListeners, windowListeners,
    changeSystem(dark) {
      media.matches = dark;
      for (const callback of mediaListeners) callback({ matches: dark, media: media.media });
    },
    snapshot() { return Object.fromEntries(storageMap); },
  };
}

function prefs(env) {
  const saved = env.api.getPreferences();
  return { mode: saved.mode, palette: saved.palette, fontSize: saved.fontSize };
}
function apply(env, mode, palette = 'violet', fontSize = 'standard') {
  env.api.applyPreferences({ mode, palette, fontSize });
}

function rgb(value, env, base = '#ffffff') {
  let text = String(value || '').trim();
  if (text.startsWith('var(')) return rgb(env.styles.get(text.slice(4, -1).trim()), env, base);
  if (/^#[0-9a-f]{3}$/i.test(text)) text = '#' + [...text.slice(1)].map(ch => ch + ch).join('');
  if (/^#[0-9a-f]{6}$/i.test(text)) return [1, 3, 5].map(i => parseInt(text.slice(i, i + 2), 16));
  const match = text.match(/^rgba?\(\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)(?:\s*[,/]\s*([\d.]+))?\s*\)$/);
  if (match) {
    const color = match.slice(1, 4).map(Number);
    if (match[4] !== undefined) {
      const under = rgb(base, env);
      return color.map((component, i) => component * Number(match[4]) + under[i] * (1 - Number(match[4])));
    }
    return color;
  }
  if (text.toLowerCase() === 'white') return [255, 255, 255];
  if (text.toLowerCase() === 'black') return [0, 0, 0];
  throw new Error('无法解析主题色值：' + text);
}
function luminance(color) {
  const linear = color.map(c => c / 255).map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}
function contrast(foreground, background, env) {
  const one = luminance(rgb(foreground, env, background));
  const two = luminance(rgb(background, env));
  return (Math.max(one, two) + 0.05) / (Math.min(one, two) + 0.05);
}

const tests = [];
function test(name, callback) { tests.push([name, callback]); }

test('首次启动默认深色通路青绿工作台、标准字号', () => {
  const env = environment();
  assert.deepEqual(prefs(env), { mode: 'dark', palette: 'focus', fontSize: 'standard' });
  assert.equal(env.html.dataset.colorMode, 'dark');
  assert.equal(env.html.dataset.theme, 'dark');
  assert.equal(env.html.dataset.palette, 'focus');
  assert.equal(env.html.dataset.fontSize, 'standard');
});
test('默认深色不依赖系统模式且DOM就绪前已应用', () => {
  const env = environment({ dark: true });
  assert.equal(env.html.dataset.theme, 'dark');
  assert.equal(env.html.dataset.colorMode, 'dark');
  env.changeSystem(false);
  assert.equal(env.html.dataset.theme, 'dark');
  assert.ok(env.styles.get('--paper'), 'DOM尚未就绪时已经应用主题变量');
});
test('跟随系统模式响应系统变化但保留配色与字号', () => {
  const env = environment();
  apply(env, 'system', 'focus', 'large');
  env.changeSystem(true);
  assert.equal(env.html.dataset.theme, 'dark');
  assert.equal(env.html.dataset.palette, 'focus');
  assert.equal(env.html.dataset.fontSize, 'large');
  env.changeSystem(false);
  assert.equal(env.html.dataset.theme, 'light');
  assert.equal(prefs(env).mode, 'system');
  const restored = environment({ seed: env.snapshot(), dark: true });
  assert.equal(prefs(restored).mode, 'system');
  assert.equal(restored.html.dataset.theme, 'dark');
  restored.changeSystem(false);
  assert.equal(restored.html.dataset.theme, 'light');
});
test('明确选择浅色时不被深色系统覆盖', () => {
  const env = environment({ dark: true });
  apply(env, 'light', 'peach', 'comfortable');
  env.changeSystem(false); env.changeSystem(true);
  assert.equal(env.html.dataset.theme, 'light');
  assert.equal(env.html.dataset.colorMode, 'light');
  const restored = environment({ seed: env.snapshot(), dark: true });
  assert.equal(prefs(restored).mode, 'light');
  assert.equal(restored.html.dataset.theme, 'light');
});
test('明确选择深色时不被浅色系统覆盖', () => {
  const env = environment();
  apply(env, 'dark', 'sky', 'standard');
  env.changeSystem(true); env.changeSystem(false);
  assert.equal(env.html.dataset.theme, 'dark');
  assert.equal(env.html.dataset.colorMode, 'dark');
});
test('保存后刷新可恢复深色、配色及大字号', () => {
  const one = environment();
  apply(one, 'dark', 'focus', 'large');
  const two = environment({ seed: one.snapshot() });
  assert.deepEqual(prefs(two), { mode: 'dark', palette: 'focus', fontSize: 'large' });
  assert.equal(two.html.dataset.theme, 'dark');
});
test('外观保存不改写业务配置', () => {
  const env = environment();
  for (const palette of Object.keys(expectedPalettes)) apply(env, 'dark', palette, 'comfortable');
  assert.equal(env.storageMap.get(businessKey), originalBusinessValue);
  assert.ok(env.writes.length > 0);
  assert.ok(env.writes.every(([key]) => key !== businessKey));
});
test('存储读取失败时仍可选择主题', () => {
  const env = environment({ storageFailure: 'read' });
  apply(env, 'dark', 'focus', 'large');
  assert.equal(env.html.dataset.theme, 'dark');
  assert.equal(env.html.dataset.palette, 'focus');
});
test('存储写入失败时当前页面外观仍生效', () => {
  const env = environment({ storageFailure: 'all' });
  apply(env, 'light', 'focus', 'comfortable');
  assert.equal(env.html.dataset.theme, 'light');
  assert.equal(env.html.dataset.palette, 'focus');
  assert.equal(env.html.dataset.fontSize, 'comfortable');
});
test('损坏JSON与非法枚举回落到有效默认配置', () => {
  const baseline = environment();
  apply(baseline, 'dark');
  const appearanceKey = baseline.writes.find(([key]) => key !== businessKey)?.[0];
  assert.ok(appearanceKey, '外观使用独立持久化键');
  const broken = environment({ seed: { [appearanceKey]: '{oops' } });
  assert.equal(broken.html.dataset.colorMode, 'dark');
  const invalid = environment({ seed: { [appearanceKey]: JSON.stringify({ mode: 'sepia', palette: 'missing', fontSize: 'tiny' }) } });
  assert.deepEqual(prefs(invalid), { mode: 'dark', palette: 'focus', fontSize: 'standard' });
});
test('2组配色保留品牌参考色，不按用户身份限制选择', () => {
  const env = environment();
  assert.equal(env.api.palettes.length, 2);
  for (const [id, colors] of Object.entries(expectedPalettes)) {
    const palette = env.api.palettes.find(item => item.id === id);
    assert.ok(palette, '缺少配色 ' + id);
    const encoded = JSON.stringify(palette).toUpperCase();
    for (const color of colors) assert.ok(encoded.includes(color), `${id} 应保留参考色 ${color}`);
    apply(env, 'light', id);
    assert.equal(env.html.dataset.palette, id);
  }
});
test('标准、舒适、大号分别应用预期比例', () => {
  const env = environment();
  for (const [fontSize, scale] of [['standard', 1], ['comfortable', 1.12], ['large', 1.25]]) {
    apply(env, 'light', 'violet', fontSize);
    assert.equal(Number(env.styles.get('--ui-scale')), scale);
    assert.equal(env.html.dataset.fontSize, fontSize);
  }
});
test('4种配色明暗组合的正文、次级、弱文本及按钮对比达标', () => {
  const env = environment();
  const comparisons = [
    ['--ink', '--paper'], ['--text-secondary', '--paper'], ['--muted', '--paper'],
    ['--ink', '--canvas'], ['--accent-contrast', '--purple'],
    ['--accent-contrast', '--purple-dark'], ['--purple', '--purple-soft'],
    ['--nav-strong', '--nav'], ['--nav-text', '--nav'], ['--nav-muted', '--nav'],
    ['--muted', '--surface-subtle'], ['--text-secondary', '--surface-raised'],
  ];
  for (const mode of ['light', 'dark']) {
    for (const palette of Object.keys(expectedPalettes)) {
      apply(env, mode, palette);
      for (const [front, back] of comparisons) {
        const ratio = contrast(env.styles.get(front), env.styles.get(back), env);
        assert.ok(ratio >= 4.5, `${mode}/${palette} ${front} on ${back}: ${ratio.toFixed(2)} < 4.5`);
      }
    }
  }
});
test('其他窗口的外观变更可同步，业务配置事件被忽略', () => {
  const env = environment();
  apply(env, 'light', 'violet');
  const ownKey = env.writes.find(([key]) => key !== businessKey)[0];
  const handler = env.windowListeners.get('storage');
  assert.equal(typeof handler, 'function');
  const count = env.writes.length;
  handler({ key: ownKey, newValue: JSON.stringify({ mode: 'dark', palette: 'focus', fontSize: 'comfortable' }) });
  assert.equal(env.html.dataset.theme, 'dark');
  assert.equal(env.html.dataset.palette, 'focus');
  assert.equal(env.html.dataset.fontSize, 'comfortable');
  assert.equal(env.writes.length, count, '接收同步事件不能回写产生循环');
  handler({ key: businessKey, newValue: JSON.stringify({ mode: 'light' }) });
  assert.equal(env.html.dataset.theme, 'dark');
});
test('改变外观无需联网、无需触碰业务数据', () => {
  const env = environment();
  for (const mode of ['light', 'dark', 'system']) {
    for (const palette of Object.keys(expectedPalettes)) {
      apply(env, mode, palette, 'large');
      assert.ok(['light', 'dark'].includes(env.html.dataset.theme));
    }
  }
  assert.equal(env.storageMap.get(businessKey), originalBusinessValue);
});
test('动态玻璃效果默认关闭且可独立切换，不影响主题模式', () => {
  const env = environment({ dark: true });
  assert.equal(prefs(env).mode, 'dark');
  assert.equal(env.html.dataset.glass, 'off');
  env.api.applyPreferences({ glass: true });
  assert.equal(env.html.dataset.glass, 'on');
  assert.equal(env.html.dataset.theme, 'dark');
  env.api.applyPreferences({ glass: false });
  assert.equal(env.html.dataset.glass, 'off');
});

if (!fs.existsSync(sourcePath)) {
  console.error('SKIP: 外观控制器尚未落盘：' + sourcePath);
  process.exitCode = 2;
} else {
  let failures = 0;
  for (const [name, callback] of tests) {
    try { callback(); console.log('PASS ' + name); }
    catch (error) { failures++; console.error('FAIL ' + name + '\n' + (error.stack || error)); }
  }
  console.log(`\n${tests.length} tests, ${tests.length - failures} passed, ${failures} failed`);
  process.exitCode = failures ? 1 : 0;
}
