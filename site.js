/* MoteDeck 官网交互：窗口装配、首屏镜头、各区块演示 */
(() => {
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const reduce = matchMedia('(prefers-reduced-motion: reduce)');
const hasGsap = typeof gsap !== 'undefined';
const calm = () => reduce.matches || !hasGsap;
const isMono = t => /[A-Za-z]/.test(t) && !/[\u4e00-\u9fa5]/.test(t);
const onceSeen = (el, fn, threshold = .4) => {
  const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { io.disconnect(); fn(); } }, { threshold });
  io.observe(el);
};

/* 1. 软件窗口：按键页克隆到两个演示位，再给每个窗口补标题栏和导航 */
const keywin = $('#keywin');
$$('.kc .c span', keywin).forEach(s => { s.dataset.o = s.textContent; s.dataset.c = s.className; });
const cloneKeys = host => {
  const w = keywin.cloneNode(true);
  w.removeAttribute('id');
  host.appendChild(w);
  return w;
};
const appsWin = cloneKeys($('[data-keys=apps]'));
$('.toast', appsWin).remove();
const dropWin = cloneKeys($('[data-keys=drop]'));
dropWin.insertAdjacentHTML('beforeend',
  '<div class="scrim"></div>' +
  '<div class="hintc"><b>松手导入按键配置</b><span>将覆盖「全局」当前的按键映射</span></div>' +
  '<div class="dlg"><b>导入按键配置</b><p>导入会用文件里的按键映射覆盖「全局」当前的配置，原有按键映射将被替换，其它布局不受影响。</p>' +
  '<div class="act"><button class="btn-t">取消</button><button class="btn1">覆盖导入</button></div></div>');

const NAV = [['device', 'link', '设备'], ['keys', 'keys', '按键'], ['voice', 'mic', '语音']];
$$('.win[data-page]').forEach(w => {
  const p = w.dataset.page;
  w.insertAdjacentHTML('afterbegin',
    '<div class="titlebar"><span class="wm"><svg viewBox="0 0 784 130"><use href="#i-wordmark"/></svg></span>' +
    ['min', 'max', 'close'].map(i => `<button class="tb-btn"><svg class="ic"><use href="#i-${i}"/></svg></button>`).join('') +
    '</div><nav class="nav"><i class="app-ic"><svg viewBox="0 0 64 64"><use href="#i-logo"/></svg></i>' +
    NAV.map(([k, ic, t]) => `<button class="nav-item${k === p ? ' on' : ''}"><svg class="ic"><use href="#i-${ic}"/></svg><span>${t}</span></button>`).join('') +
    '<div class="battery" style="--v:60"><span class="pct mono">60<small>%</small></span></div></nav>');
  w.inert = true;
});

/* 2. 窗口等比缩放：首屏镜头模式固定 1，其余按容器宽度 */
const fit = f => f.style.setProperty('--s', f.closest('.kv.pin') ? 1 : Math.min(1, f.clientWidth / 880));
const ro = new ResizeObserver(es => es.forEach(e => fit(e.target)));
$$('.frame').forEach(f => ro.observe(f));
const refit = () => $$('.frame').forEach(fit);

/* 3. 按键格写值：map[key] = [单击, 双击, 长按]，null 表示沿用全局 */
const paint = (win, map = {}, fade = true) => {
  const changed = [];
  $$('.kc', win).forEach(kc => {
    const m = map[kc.dataset.key] || [];
    $$('.c span', kc).forEach((s, i) => {
      const v = m[i] ?? null;
      const text = v ?? s.dataset.o;
      const cls = v ? 'ov' + (isMono(v) ? ' mono' : '') : s.dataset.c;
      if (s.textContent === text && s.className.replace(/ ?hot/, '') === cls) return;
      s.textContent = text; s.className = cls; changed.push(s);
    });
  });
  if (fade && !calm() && changed.length) gsap.fromTo(changed, { opacity: 0, y: 4 }, { opacity: 1, y: 0, duration: .32, stagger: .03, ease: 'power2.out', overwrite: true });
  return changed.length;
};

/* 4. 平滑滚动 */
let lenis = null;
if (hasGsap) gsap.registerPlugin(ScrollTrigger);
if (hasGsap && !reduce.matches && typeof Lenis !== 'undefined') {
  lenis = new Lenis({ lerp: .12 });
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add(t => lenis.raf(t * 1000));
  gsap.ticker.lagSmoothing(0);
}
const go = target => lenis ? lenis.scrollTo(target, { offset: target === '#kv' ? 0 : -24 }) : $(target)?.scrollIntoView({ behavior: reduce.matches ? 'auto' : 'smooth' });
$$('a[href^="#"]').forEach(a => a.addEventListener('click', e => {
  const h = a.getAttribute('href');
  if (h.length > 1 && $(h)) { e.preventDefault(); go(h); }
}));
const top = $('#top');
const solid = () => top.classList.toggle('solid', scrollY > 8);
addEventListener('scroll', solid, { passive: true }); solid();

/* 5. 首屏镜头：窗口上移居中 → 推近确定键 → 依次点亮单击 / 双击 / 长按 */
const kv = $('#kv'), stage = $('.kv-stage', kv), kframe = $('.frame', kv);
const okKc = $('.kc[data-key=ok]', keywin);
const okCells = $$('.c span', okKc);
const pressLis = $$('.press li', kv);
const HERO = [null, 'F11', '右键菜单'];
let heroN = -1;
const heroStep = (n, dim = true) => {
  if (n === heroN) return; heroN = n;
  okCells.forEach((s, i) => {
    const v = i < n && HERO[i] ? HERO[i] : null;
    s.textContent = v ?? s.dataset.o;
    s.className = (v ? 'ov' + (isMono(v) ? ' mono' : '') : s.dataset.c) + (i < n ? ' hot' : '');
  });
  $$('.kc', keywin).forEach(k => k.classList.toggle('dim', dim && n > 0 && k !== okKc));
  pressLis.forEach((li, i) => li.classList.toggle('on', i < n));
};
const within = (el, anc) => { let x = 0, y = 0; while (el && el !== anc) { x += el.offsetLeft; y += el.offsetTop; el = el.offsetParent; } return { x, y }; };
const stepThrough = () => {
  if (calm()) return heroStep(3, false);
  [1, 2, 3].forEach((n, i) => setTimeout(() => heroStep(n, false), 500 + i * 700));
};

const mm = hasGsap ? gsap.matchMedia() : null;
if (mm) {
  mm.add('(min-width:1100px) and (min-height:680px) and (prefers-reduced-motion:no-preference)', () => {
    kv.classList.add('pin'); refit(); heroN = -1; heroStep(0);
    const copy = $('.kv-copy', kv), veil = $('.kv-veil', kv), press = $('.press', kv), skip = $('.skip', kv);
    // 窗口起点：紧贴 CTA 下方；终点：确定键卡片放大后落在右侧 62% 处
    const y0 = () => copy.offsetTop + copy.offsetHeight + 48 - (innerHeight / 2 - 330);
    const Z = 1.35;
    const ok = () => { const p = within(okKc, keywin); return { x: p.x + okKc.offsetWidth / 2, y: p.y + okKc.offsetHeight / 2 }; };
    const zx = () => innerWidth * .64 - (innerWidth / 2 - 440) - Z * ok().x;
    const zy = () => innerHeight / 2 - (innerHeight / 2 - 330) - Z * ok().y;
    gsap.set(stage, { transformOrigin: '0 0', x: 0, y: y0, scale: 1 });
    const tl = gsap.timeline({
      defaults: { ease: 'none' },
      scrollTrigger: {
        trigger: kv, start: 'top top', end: '+=170%', pin: true, scrub: .6, invalidateOnRefresh: true,
        onUpdate: st => { const p = st.progress; heroStep(p < .52 ? 0 : p < .66 ? 1 : p < .8 ? 2 : 3); }
      }
    });
    tl.to(copy, { autoAlpha: 0, y: -60, duration: .22 }, 0)
      .to(stage, { y: 0, duration: .3, ease: 'power1.inOut' }, 0)
      .to(stage, { x: zx, y: zy, scale: Z, duration: .2, ease: 'power2.inOut' }, .3)
      .to(veil, { opacity: 1, duration: .14 }, .36)
      .fromTo(press, { autoAlpha: 0, x: -24 }, { autoAlpha: 1, x: 0, duration: .14 }, .4)
      .to({}, { duration: .5 }, .5)
      .to(skip, { autoAlpha: 0, duration: .08 }, .9);
    return () => {
      kv.classList.remove('pin'); refit();
      gsap.set([stage, copy, veil, press, skip, kv], { clearProps: 'all' });
      heroN = -1; heroStep(3, false);
    };
  });
  mm.add('not all and (min-width:1100px) and (min-height:680px) and (prefers-reduced-motion:no-preference)', () => { heroN = -1; heroStep(0, false); onceSeen(kframe, stepThrough); });
} else heroStep(3, false);

/* 6. 出场：每个 [data-r] 只播一次 */
if (!calm()) {
  gsap.set('[data-r]', { autoAlpha: 0, y: 24 });
  ScrollTrigger.batch('[data-r]', {
    start: 'top 88%', once: true,
    onEnter: els => gsap.to(els, { autoAlpha: 1, y: 0, duration: .6, stagger: .06, ease: 'power3.out', overwrite: true })
  });
}

/* 7. 数字计数 */
$$('[data-to]').forEach(el => {
  const to = +el.dataset.to;
  if (calm()) return;
  el.textContent = '0';
  onceSeen(el, () => { const o = { v: 0 }; gsap.to(o, { v: to, duration: .9, ease: 'power2.out', onUpdate: () => el.textContent = Math.round(o.v) }); }, .8);
});

/* 8. 按应用换键位：档案只记改过的键 */
const PROFILES = {
  global: { name: '全局', map: {} },
  chrome: { name: 'Chrome', map: {
    left: ['Alt+←'], right: ['Alt+→'], menu: ['Ctrl+T'], back: ['Ctrl+W'], ok: [null, 'F11'], up: [null, null, 'Home']
  } },
  potplayer: { name: 'PotPlayer', map: {
    ok: ['Space', 'Enter'], left: ['←', null, '快退 30 秒'], right: ['→', null, '快进 30 秒'], up: ['音量 +'], down: ['音量 −'], menu: ['字幕切换']
  } },
  wechat: { name: '微信', map: {
    back: ['Esc'], menu: ['Ctrl+Alt+A'], home: ['Ctrl+Alt+W'], ok: [null, null, '发送语音']
  } }
};
const segs = $$('.seg');
const moveThumb = seg => {
  const b = $('[aria-pressed=true]', seg), th = $('.thumb', seg);
  if (!b || !th) return;
  th.style.width = b.offsetWidth + 'px';
  const x = b.offsetLeft;
  calm() ? (th.style.transform = `translateX(${x}px)`) : gsap.to(th, { x, duration: .35, ease: 'power3.out' });
};
const note = $('#keys .note');
const setApp = (v, fade = true) => {
  const p = PROFILES[v];
  $$('.seg[data-seg=apps] button').forEach(b => b.setAttribute('aria-pressed', b.dataset.v === v));
  moveThumb($('.seg[data-seg=apps]'));
  $$('.app', appsWin).forEach(a => { a.classList.toggle('on', a.dataset.app === v); a.classList.toggle('live', a.dataset.app === v && v !== 'global'); });
  paint(appsWin, p.map, fade);
  const n = Object.values(p.map).reduce((a, m) => a + m.filter(Boolean).length, 0);
  note.innerHTML = v === 'global'
    ? '当前档案：<b>全局</b>，所有应用默认用这套键位'
    : `当前档案：<b>${p.name}</b>，改了 ${n} 个动作位，其余沿用全局`;
};
$$('.seg[data-seg=apps] button').forEach(b => b.addEventListener('click', () => setApp(b.dataset.v)));
setApp('global', false);
addEventListener('resize', () => segs.forEach(moveThumb));
document.fonts?.ready.then(() => segs.forEach(moveThumb));

/* 9. 语音：像素电平 + 按住说话 */
const vu = (() => {
  const CELL = 4, GAP = 2, STEP = CELL + GAP;
  const el = $('#voice .vu'); if (!el) return { set() {} };
  const c = document.createElement('canvas'); el.appendChild(c);
  const ctx = c.getContext('2d');
  let cols = 0, rows = 0, ox = 0, oy = 0, gain = .15, target = .15, vis = false, raf = 0;
  const fitVu = () => {
    const r = devicePixelRatio || 1, w = el.clientWidth, h = el.clientHeight;
    if (!w) return;
    c.width = w * r; c.height = h * r; ctx.setTransform(r, 0, 0, r, 0, 0);
    cols = Math.floor((w + GAP) / STEP); rows = Math.max(1, Math.floor((h + GAP) / STEP));
    ox = (w - (cols * STEP - GAP)) / 2; oy = h - (rows * STEP - GAP);
  };
  const green = t => `hsl(${142 - t * 14} ${62 + t * 26}% ${28 + t * 34}%)`;
  const draw = now => {
    raf = 0;
    if (!cols) fitVu();
    const s = reduce.matches ? 0 : now / 1000;
    gain += (target - gain) * .12;
    ctx.clearRect(0, 0, c.width, c.height);
    const mid = (cols - 1) / 2;
    for (let i = 0; i < cols; i++) {
      const d = Math.abs(i - mid) / mid;
      const ripple = Math.sin(d * 14 - s * 5) * .5 + .5;
      const noise = Math.sin(i * 1.7 + s * 9) * .5 + .5;
      const env = Math.pow(1 - d, 1.4);
      const lvl = Math.round(rows * Math.min(1, gain * env * (.35 + ripple * .5 + noise * .25)));
      for (let j = 0; j < rows; j++) {
        const on = j < lvl, t = j / Math.max(1, rows - 1);
        ctx.fillStyle = on ? green(t) : 'rgba(0,0,0,.10)';
        ctx.globalAlpha = on ? .55 + t * .45 : Math.pow(1 - t, 1.2);
        ctx.fillRect(ox + i * STEP, oy + (rows - 1 - j) * STEP, CELL, CELL);
      }
    }
    ctx.globalAlpha = 1;
    if (vis && !reduce.matches) raf = requestAnimationFrame(draw);
  };
  new IntersectionObserver(([e]) => { vis = e.isIntersecting; if (vis && !raf) raf = requestAnimationFrame(draw); }).observe(el);
  new ResizeObserver(() => { cols = 0; if (!raf) raf = requestAnimationFrame(draw); }).observe(el);
  return { set(g) { target = g; if (reduce.matches) gain = g; if (!raf) raf = requestAnimationFrame(draw); } };
})();

const SAYS = ['把客厅灯调暗一点，然后播放上次没看完的那集', '明天早上八点提醒我给妈妈打电话', '帮我搜一下周末适合全家看的电影'];
let sayI = 0, typing = 0;
const out = $('.talk-out'), hold = $('.hold'), hl = $('.hl', hold), vstate = $('#voice .vstate');
const setV = (t, cls) => { vstate.textContent = t; vstate.className = 'pill vstate' + (cls ? ' ' + cls : ''); };
let holding = false;
const startTalk = () => {
  if (holding) return; holding = true;
  clearInterval(typing);
  hold.classList.add('on'); hl.textContent = '正在听…'; vu.set(1); setV('录音中', 'ok');
  out.innerHTML = '<span class="live">正在听</span><span class="caret"></span>';
};
const endTalk = () => {
  if (!holding) return; holding = false;
  hold.classList.remove('on'); hl.textContent = '按住说话'; vu.set(.15); setV('识别中');
  const s = SAYS[sayI++ % SAYS.length];
  let i = 0;
  out.innerHTML = '<span class="txt"></span><span class="caret"></span>';
  const t = $('.txt', out);
  const done = () => { t.textContent = s; setTimeout(() => setV('就绪'), 200); };
  if (calm()) return done();
  typing = setInterval(() => { t.textContent = s.slice(0, ++i); if (i >= s.length) { clearInterval(typing); done(); } }, 45);
};
hold.addEventListener('pointerdown', e => { hold.setPointerCapture(e.pointerId); startTalk(); });
hold.addEventListener('pointerup', endTalk);
hold.addEventListener('pointercancel', endTalk);
hold.addEventListener('keydown', e => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); startTalk(); } });
hold.addEventListener('keyup', e => { if (e.key === ' ' || e.key === 'Enter') endTalk(); });
// 语音区在视口内时，空格键也算按住
const voiceSec = $('#voice');
let voiceIn = false;
new IntersectionObserver(([e]) => voiceIn = e.isIntersecting, { threshold: .5 }).observe(voiceSec);
addEventListener('keydown', e => {
  if (e.key !== ' ' || e.repeat || !voiceIn || e.target !== document.body) return;
  e.preventDefault(); startTalk();
});
addEventListener('keyup', e => { if (e.key === ' ' && holding && e.target === document.body) endTalk(); });

/* 10. JSON 拖入导入：文件飞进窗口 → 遮罩与提示 → 确认覆盖 → 键位更新 */
const jfile = $('.jfile'), drop = $('.drop');
const scrim = $('.scrim', dropWin), hintc = $('.hintc', dropWin), dlg = $('.dlg', dropWin);
const okBtn = $('.dlg .btn1', dropWin), dtoast = $('.toast', dropWin);
const IMPORTED = { ok: [null, 'F11', '右键菜单'], left: [null, null, '快退 30 秒'], right: [null, null, '快进 30 秒'], menu: [null, '任务视图'], tv: [null, 'Win+Tab'] };
let dropTl = null;
const resetDrop = () => {
  paint(dropWin, {}, false);
  dtoast.classList.add('off');
};
const playDrop = () => {
  dropTl?.kill(); resetDrop();
  if (calm()) { paint(dropWin, IMPORTED, false); dtoast.classList.remove('off'); return; }
  const fr = jfile.getBoundingClientRect(), wr = dropWin.getBoundingClientRect();
  const dx = wr.left + wr.width / 2 - (fr.left + fr.width / 2), dy = wr.top + wr.height * .45 - (fr.top + fr.height / 2);
  gsap.set(jfile, { x: 0, y: 0, scale: 1, rotate: 0, autoAlpha: 1 });
  gsap.set([scrim], { opacity: 0 }); gsap.set([hintc, dlg], { opacity: 0, scale: .95 });
  gsap.set(okBtn, { scale: 1 });
  dropTl = gsap.timeline({ delay: .3 })
    .to(jfile, { x: dx * .55, y: dy * .55, rotate: -3, scale: .82, duration: .7, ease: 'power2.inOut' })
    .to(scrim, { opacity: .42, duration: .14, ease: 'power3.out' }, '-=.25')
    .to(hintc, { opacity: 1, scale: 1, duration: .3, ease: 'back.out(1.3)' }, '<')
    .to(jfile, { x: dx, y: dy, scale: .4, autoAlpha: 0, duration: .35, ease: 'power2.in' }, '+=.5')
    .to(hintc, { opacity: 0, duration: .14 }, '<.2')
    .to(dlg, { opacity: 1, scale: 1, duration: .3, ease: 'back.out(1.3)' })
    .to(okBtn, { scale: .94, duration: .1, ease: 'power2.out' }, '+=.8')
    .to(okBtn, { scale: 1, duration: .14 })
    .to([dlg], { opacity: 0, scale: .97, duration: .18 }, '<')
    .to(scrim, { opacity: 0, duration: .2 }, '<')
    .add(() => { paint(dropWin, IMPORTED); dtoast.classList.remove('off'); })
    .add(() => dtoast.classList.add('off'), '+=2.4')
    .fromTo(jfile, { x: 0, y: 0, rotate: 0, scale: 1, autoAlpha: 0 }, { autoAlpha: 1, duration: .4 }, '+=.2');
};
onceSeen(drop, playDrop, .5);
$('.replay').addEventListener('click', playDrop);

/* 11. 状态灯：点哪个，设备页就切到哪种状态 */
const STATES = {
  r: { pill: ['启动失败', 'bad'], btn: '修复权限', recv: '未工作', svc: '缺少按键组件权限，修复后重试' },
  y: { pill: ['正在连接遥控器', 'warn'], btn: '重新连接', recv: '等待连接', svc: '蓝牙断开，正在自动重连' },
  g: { pill: ['已连接', 'ok'], btn: '重启服务', recv: '正常', svc: '连接遥控器，接收按键和语音' }
};
const lamps = $$('.lamps li');
const setLamp = c => {
  const s = STATES[c];
  lamps.forEach(li => { const on = li.dataset.c === c; li.classList.toggle('on', on); $('button', li).setAttribute('aria-pressed', on); });
  const pill = $('#dpill'); pill.textContent = s.pill[0]; pill.className = 'pill ' + s.pill[1];
  $('#dbtn').textContent = s.btn; $('#drecv').textContent = s.recv; $('#dsvc').textContent = s.svc;
};
let lampTouched = false;
lamps.forEach(li => $('button', li).addEventListener('click', () => { lampTouched = true; setLamp(li.dataset.c); }));
onceSeen($('#state'), () => {
  if (calm()) return;
  ['r', 'y', 'g'].forEach((c, i) => setTimeout(() => { if (!lampTouched) setLamp(c); }, 400 + i * 1100));
}, .5);

if (hasGsap) { document.fonts?.ready.then(() => ScrollTrigger.refresh()); addEventListener('load', () => ScrollTrigger.refresh()); }
})();
