/* Shared, offline appearance preferences. Load in <head> before the app styles. */
(() => {
  'use strict';
  const STORAGE_KEY = 'operator-console-appearance-v1';
  const DEFAULTS = Object.freeze({mode: 'dark', palette: 'focus', fontSize: 'standard', glass: false});
  const PALETTES = Object.freeze([
    {id: 'focus', name: '通路青绿', description: 'A02 品牌 · 默认数据工作台', a: '#0B4F4A', b: '#DEFFF2', accent: '#0B6A62', darkAccent: '#76DAC0'},
    {id: 'violet', name: '经典紫', description: '清晰、简洁', a: '#A99AFF', b: '#7054DE', accent: '#6544C7'},
  ].map(Object.freeze));
  const MODES = Object.freeze({light: '浅色', dark: '深色', system: '跟随系统'});
  const FONT_SIZES = Object.freeze({standard: {name: '标准', scale: 1}, comfortable: {name: '舒适', scale: 1.12}, large: {name: '大号', scale: 1.25}});
  const html = document.documentElement;
  let media;
  try { media = window.matchMedia('(prefers-color-scheme: dark)'); }
  catch { media = {matches: false}; }
  let panelRoot = null, trigger = null, lastFocus = null, previousOverflow = '', opened = false, canPersist = true;

  function normalize(value) {
    const supplied = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    return {
      mode: Object.hasOwn(MODES, supplied.mode) ? supplied.mode : DEFAULTS.mode,
      palette: PALETTES.some(item => item.id === supplied.palette) ? supplied.palette : DEFAULTS.palette,
      fontSize: Object.hasOwn(FONT_SIZES, supplied.fontSize) ? supplied.fontSize : DEFAULTS.fontSize,
      glass: supplied.glass === true,
    };
  }
  function readPreferences(value) {
    try { return normalize(JSON.parse(value === undefined ? window.localStorage.getItem(STORAGE_KEY) : value)); }
    catch { return {...DEFAULTS}; }
  }
  let preferences = readPreferences();
  function rgb(hex) { return hex.replace('#', '').match(/../g).map(value => parseInt(value, 16)); }
  function mix(one, two, weight) {
    const a = rgb(one), b = rgb(two);
    return '#' + a.map((value, index) => Math.round(value * (1 - weight) + b[index] * weight).toString(16).padStart(2, '0')).join('');
  }
  function alpha(hex, opacity) { return `rgba(${rgb(hex).join(', ')}, ${opacity})`; }
  function luminance(hex) {
    const channels = rgb(hex).map(value => {value /= 255; return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;});
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  function contrast(one, two) { const a = luminance(one), b = luminance(two); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); }
  function effectiveMode() { return preferences.mode === 'system' ? (media.matches ? 'dark' : 'light') : preferences.mode; }
  function palette() { return PALETTES.find(item => item.id === preferences.palette); }

  function tokens(mode, colors) {
    const dark = mode === 'dark';
    const paper = dark ? '#151f2c' : '#ffffff';
    let accent = dark ? (colors.darkAccent || mix(colors.b, '#ffffff', 0.45)) : colors.accent;
    // Accent text must remain legible on its lightly tinted selected surface.
    for (let attempt = 0; attempt < 20; attempt++) {
      const soft = mix(paper, accent, dark ? 0.15 : 0.085);
      if (contrast(accent, soft) >= 4.8) break;
      accent = mix(accent, dark ? '#ffffff' : '#111827', 0.07);
    }
    const onAccent = contrast(accent, '#ffffff') >= contrast(accent, '#111827') ? '#ffffff' : '#111827';
    const canvas = dark ? '#0d141e' : mix('#f5f6fa', colors.a, 0.035);
    return {
      '--canvas': canvas,
      '--canvas-gradient': `linear-gradient(135deg, ${alpha(colors.a, dark ? 0.035 : 0.11)} 0%, transparent 46%, ${alpha(colors.b, dark ? 0.045 : 0.065)} 100%)`,
      '--paper': paper,
      '--surface-subtle': dark ? '#1b2838' : mix('#f5f6fa', colors.a, 0.025),
      '--surface-raised': dark ? '#121c29' : '#ffffff',
      '--ink': dark ? '#edf3fb' : '#252b3d',
      '--text-secondary': dark ? '#b4c0d0' : '#515c70',
      '--muted': dark ? '#94a3b8' : '#5e6a7e',
      '--line': dark ? '#283649' : '#e0e5ee',
      '--line-strong': dark ? '#45566b' : '#b9c3d3',
      '--purple': accent,
      '--purple-dark': dark ? mix(accent, '#ffffff', 0.1) : mix(accent, '#111827', 0.13),
      '--purple-soft': mix(paper, accent, dark ? 0.15 : 0.085),
      '--accent-contrast': onAccent,
      '--green': dark ? '#6bd0a7' : '#167558',
      '--green-soft': dark ? '#173c34' : '#e8f5ef',
      '--amber': dark ? '#f1c36a' : '#8d5b12',
      '--amber-soft': dark ? '#42351e' : '#fff3db',
      '--red': dark ? '#ff9aab' : '#b2364b',
      '--red-soft': dark ? '#472b36' : '#fdecef',
      '--nav': dark ? '#0f1823' : mix('#fcfdff', colors.a, 0.025),
      '--nav-text': dark ? '#b4c0d0' : '#515c70',
      '--nav-muted': dark ? '#94a3b8' : '#657187',
      '--nav-strong': dark ? '#edf3fb' : '#252b3d',
      '--nav-active': mix(paper, accent, dark ? 0.15 : 0.085),
      '--nav-active-text': accent,
      '--shadow': dark ? '0 8px 30px rgba(0, 0, 0, 0.17)' : '0 8px 30px rgba(32, 38, 67, 0.045)',
      '--overlay': dark ? 'rgba(4, 8, 16, 0.72)' : 'rgba(25, 33, 52, 0.35)',
      '--focus-ring': accent,
      '--ui-scale': String(FONT_SIZES[preferences.fontSize].scale),
      '--theme-a': colors.a,
      '--theme-b': colors.b,
    };
  }

  function applyState(persist = false) {
    const mode = effectiveMode(), colors = palette();
    const values = tokens(mode, colors);
    html.dataset.colorMode = preferences.mode;
    html.dataset.theme = mode;
    html.dataset.palette = preferences.palette;
    html.dataset.fontSize = preferences.fontSize;
    html.dataset.glass = preferences.glass ? 'on' : 'off';
    html.style.colorScheme = mode;
    Object.entries(values).forEach(([key, value]) => html.style.setProperty(key, value));
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    if (themeMeta) themeMeta.setAttribute('content', values['--canvas']);
    if (persist) {
      try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify({version: 1, ...preferences})); canPersist = true; }
      catch { canPersist = false; }
    }
    refreshControls();
    if (typeof window.CustomEvent === 'function') window.dispatchEvent(new window.CustomEvent('operator-appearance-change', {detail: {...preferences, resolvedMode: mode}}));
    return {...preferences, resolvedMode: mode};
  }
  function applyPreferences(next) {
    preferences = normalize({...preferences, ...(next && typeof next === 'object' ? next : {})});
    return applyState(true);
  }
  applyState();

  const systemChanged = () => { if (preferences.mode === 'system') applyState(); };
  if (media.addEventListener) media.addEventListener('change', systemChanged);
  else if (media.addListener) media.addListener(systemChanged);
  window.addEventListener('storage', event => {
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    preferences = event.newValue ? readPreferences(event.newValue) : {...DEFAULTS};
    applyState();
  });

  const svg = (name, className = '') => {
    const drawings = {
      sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
      moon: '<path d="M20.8 13.1A9 9 0 0 1 10.9 3.2 9 9 0 1 0 20.8 13.1Z"/>',
      system: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
      palette: '<path d="M12 3a9 9 0 1 0 0 18h1.5a2.2 2.2 0 0 0 1.3-4 1.2 1.2 0 0 1 .7-2.2H18a3 3 0 0 0 3-3c0-5-4-8.8-9-8.8Z"/><circle cx="7.5" cy="11" r=".8"/><circle cx="10" cy="7.5" r=".8"/><circle cx="14.5" cy="7.5" r=".8"/><circle cx="17" cy="11" r=".8"/>',
      check: '<path d="m5 12 4 4L19 6"/>',
      close: '<path d="m6 6 12 12M6 18 18 6"/>',
      reset: '<path d="M3 10V4m0 0h6M3 4l4 4a8 8 0 1 1-1.5 8"/>',
    };
    return `<svg class="appearance-icon ${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${drawings[name]}</svg>`;
  };

  function refreshControls() {
    if (trigger) {
      trigger.title = `外观：${MODES[preferences.mode]} · ${palette().name}`;
      trigger.setAttribute('aria-expanded', String(opened));
    }
    if (!panelRoot) return;
    panelRoot.querySelectorAll('input[data-appearance-pref]').forEach(input => {
      input.checked = preferences[input.dataset.appearancePref] === input.value;
      input.closest('label').classList.toggle('is-selected', input.checked);
    });
    const glass = panelRoot.querySelector('[data-appearance-glass]');
    if (glass) glass.checked = preferences.glass;
    const effective = panelRoot.querySelector('[data-appearance-effective]');
    if (effective) effective.textContent = preferences.mode === 'system' ? `当前跟随系统：${MODES[effectiveMode()]}` : `当前使用${MODES[effectiveMode()]}模式`;
    const label = panelRoot.querySelector('[data-appearance-current]');
    if (label) label.textContent = `${palette().name} · ${FONT_SIZES[preferences.fontSize].name}字号`;
    const saving = panelRoot.querySelector('[data-appearance-saving]');
    if (saving) saving.textContent = canPersist ? '更改即时生效，并自动保存在当前浏览器。' : '更改在当前窗口生效；浏览器未允许保存设置。';
  }
  function closePanel() {
    if (!opened) return;
    opened = false;
    panelRoot.hidden = true;
    document.body.style.overflow = previousOverflow;
    refreshControls();
    const destination = lastFocus && lastFocus.isConnected ? lastFocus : trigger;
    destination?.focus();
  }
  function openPanel() {
    if (opened || !panelRoot) return;
    lastFocus = document.activeElement;
    previousOverflow = document.body.style.overflow;
    opened = true;
    panelRoot.hidden = false;
    document.body.style.overflow = 'hidden';
    refreshControls();
    panelRoot.querySelector('[data-appearance-close]').focus();
  }
  function initialize() {
    if (document.getElementById('appearance-root')) return;
    const destination = document.querySelector('.topbar-right');
    if (!destination) return;
    trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'appearance-trigger';
    trigger.id = 'appearance-trigger';
    trigger.setAttribute('aria-label', '外观设置');
    trigger.setAttribute('aria-controls', 'appearance-panel');
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.innerHTML = `${svg('palette')}<span>外观</span>`;
    destination.prepend(trigger);
    panelRoot = document.createElement('div');
    panelRoot.id = 'appearance-root';
    panelRoot.className = 'appearance-backdrop';
    panelRoot.hidden = true;
    panelRoot.innerHTML = `<section class="appearance-panel" id="appearance-panel" role="dialog" aria-modal="true" aria-labelledby="appearance-title" aria-describedby="appearance-description">
      <header class="appearance-header"><div><span class="appearance-eyebrow">YOUR WORKSPACE</span><h2 id="appearance-title">让工作台适合你</h2><p id="appearance-description">选择喜欢的底色与字号，找到舒适的阅读方式。</p></div><button class="appearance-close" data-appearance-close type="button" aria-label="关闭外观设置">${svg('close')}</button></header>
      <div class="appearance-body">
        <fieldset class="appearance-section"><legend>显示模式</legend><div class="appearance-mode-grid">${Object.entries(MODES).map(([value, name]) => `<label class="appearance-mode-option"><input type="radio" name="appearance-mode" value="${value}" data-appearance-pref="mode">${svg(value === 'light' ? 'sun' : value === 'dark' ? 'moon' : 'system')}<span>${name}</span><i class="appearance-selection">${svg('check')}</i></label>`).join('')}</div><p class="appearance-helper" data-appearance-effective></p></fieldset>
        <fieldset class="appearance-section"><legend>工作台配色<span class="appearance-legend-note">所有模式均可使用</span></legend><div class="appearance-palette-grid">${PALETTES.map(item => `<label class="appearance-palette-option" style="--swatch-a:${item.a};--swatch-b:${item.b}"><input type="radio" name="appearance-palette" value="${item.id}" data-appearance-pref="palette"><span class="appearance-swatch"><i></i><i></i><i></i><span class="appearance-selection">${svg('check')}</span></span><span class="appearance-palette-name">${item.name}</span><span class="appearance-palette-description">${item.description}</span></label>`).join('')}</div></fieldset>
        <fieldset class="appearance-section"><legend>阅读字号</legend><div class="appearance-font-grid">${Object.entries(FONT_SIZES).map(([value, info]) => `<label class="appearance-font-option"><input type="radio" name="appearance-font" value="${value}" data-appearance-pref="fontSize"><strong class="appearance-font-example appearance-font-${value}">Aa</strong><span>${info.name}</span><i class="appearance-selection">${svg('check')}</i></label>`).join('')}</div></fieldset>
        <fieldset class="appearance-section appearance-performance"><legend>显示效果</legend><label class="appearance-switch"><input type="checkbox" data-appearance-glass><span class="appearance-switch-track"><i></i></span><span><strong>动态玻璃效果</strong><small>开启后卡片和弹窗增加半透明与模糊。默认关闭以降低 GPU 和内存占用。</small></span></label></fieldset>
        <div class="appearance-preview"><span class="appearance-preview-label">阅读预览</span><strong>经营数据，清晰易读。</strong><p>查看报表、核对问题，用适合自己的方式继续工作。</p><span data-appearance-current></span></div>
      </div>
      <footer class="appearance-footer"><p data-appearance-saving aria-live="polite"></p><div><button type="button" class="appearance-reset" data-appearance-reset>${svg('reset')}恢复默认</button><button type="button" class="appearance-done" data-appearance-close>完成</button></div></footer>
    </section>`;
    document.body.append(panelRoot);
    trigger.addEventListener('click', event => {event.stopPropagation(); openPanel();});
    panelRoot.addEventListener('click', event => {
      event.stopPropagation();
      if (event.target === panelRoot || event.target.closest('[data-appearance-close]')) closePanel();
      else if (event.target.closest('[data-appearance-reset]')) applyPreferences(DEFAULTS);
    });
    panelRoot.addEventListener('change', event => {
      const input = event.target;
      if (input.matches('[data-appearance-glass]')) { applyPreferences({glass: input.checked}); return; }
      if (!input.matches('input[data-appearance-pref]')) return;
      event.stopPropagation();
      applyPreferences({[input.dataset.appearancePref]: input.value});
    });
    document.addEventListener('keydown', event => {
      if (!opened) return;
      if (event.key === 'Escape') {event.preventDefault(); event.stopImmediatePropagation(); closePanel(); return;}
      if (event.key !== 'Tab') return;
      const controls = [...panelRoot.querySelectorAll('button:not([disabled]), input:checked')].filter(element => element.offsetParent !== null);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && (document.activeElement === first || !panelRoot.contains(document.activeElement))) {event.preventDefault(); last?.focus();}
      else if (!event.shiftKey && (document.activeElement === last || !panelRoot.contains(document.activeElement))) {event.preventDefault(); first?.focus();}
    }, true);
    refreshControls();
  }
  window.OperatorAppearance = Object.freeze({
    getPreferences: () => ({...preferences, resolvedMode: effectiveMode()}),
    applyPreferences,
    palettes: PALETTES,
    storageKey: STORAGE_KEY,
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
