#!/usr/bin/env node
/**
 * 恒源云实例一键关机小程序
 *
 * 用法:
 *   node shutdown_gpushare.js --login                          # 首次:自动填账号密码,人工输验证码,保存登录态
 *   node shutdown_gpushare.js --shutdown                       # 复用登录态,定位实例并在网页点击关机
 *   node shutdown_gpushare.js --shutdown --headless            # 无头模式(深夜无人值守可用)
 *   node shutdown_gpushare.js --shutdown --instance-name <名称>  # 指定实例名称
 *   node shutdown_gpushare.js --shutdown --console-url <URL>   # 指定控制台实例列表地址
 *
 * 说明:
 *   - 登录态(cookie/localStorage)显式保存到 ~/.gpushare-auto/storage.json,不依赖浏览器 profile 写盘
 *   - 账号/密码从 .env 读取(GPUSHARE_USERNAME / GPUSHARE_PASSWORD),不写入脚本;密码留空则手动输入
 *   - 实例定位优先级:--instance-name > .env 的 GPUSHARE_INSTANCE_NAME > 主机名 i-2.gpushare.com
 *   - 每个关键步骤截图保存到 ~/.gpushare-auto/shots/ 供人工确认
 *   - 退出码:0=成功 1=参数/运行错误 2=未登录 3=实例定位/关机失败
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// ---------- 配置 ----------
const HOME = os.homedir();
const APP_DIR = path.join(HOME, '.gpushare-auto');
const STATE_PATH = path.join(APP_DIR, 'storage.json');
const SHOTS_DIR = path.join(APP_DIR, 'shots');
const ENV_PATH = path.resolve(__dirname, '..', '.env');
const FALLBACK_HOST = 'i-2.gpushare.com';
const BASE_URL = 'https://gpushare.com';
const LOGIN_URL = 'https://gpushare.com/auth/login';

// ---------- 极简 .env 解析 ----------
function loadEnv(file) {
  const out = {};
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch { return out; }
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    let value = m[2].replace(/^["']|["']$/g, '').trim();
    if (value.startsWith('#')) continue;
    out[m[1]] = value;
  }
  return out;
}
const ENV = loadEnv(ENV_PATH);

// ---------- 加载 playwright(优先 Codex bundled runtime) ----------
function loadPlaywright() {
  try { return require('playwright'); } catch (e) { /* 继续尝试 bundled */ }
  const cacheRoot = path.join(HOME, '.cache', 'codex-runtimes');
  if (fs.existsSync(cacheRoot)) {
    for (const d of fs.readdirSync(cacheRoot)) {
      const p = path.join(cacheRoot, d, 'dependencies', 'node', 'node_modules', 'playwright');
      if (fs.existsSync(p)) return require(p);
    }
  }
  throw new Error('未找到 playwright:请使用 Codex bundled node 运行,或先 npm install playwright');
}
const { chromium } = loadPlaywright();

// ---------- 工具 ----------
function log(...args) { console.log('[gpushare-shutdown]', ...args); }
function errExit(code, msg) { if (msg) console.error('[gpushare-shutdown]', msg); process.exit(code); }
function waitMs(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function shot(page, name) {
  try {
    fs.mkdirSync(SHOTS_DIR, { recursive: true });
    const p = path.join(SHOTS_DIR, `${Date.now()}_${name}.png`);
    await page.screenshot({ path: p, fullPage: true });
    log('截图已保存:', p);
  } catch (e) { log('截图失败:', e.message); }
}
function parseArgs(argv) {
  const a = { headless: false, consoleUrl: '', instanceName: '' };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--login') a.login = true;
    else if (arg === '--shutdown') a.shutdown = true;
    else if (arg === '--start') a.start = true;
    else if (arg === '--headless') a.headless = true;
    else if (arg === '--instance-name' && argv[i + 1]) a.instanceName = argv[++i];
    else if (arg === '--console-url' && argv[i + 1]) a.consoleUrl = argv[++i];
    else if (arg === '-h' || arg === '--help') a.help = true;
  }
  return a;
}
function storageStateExists() { return fs.existsSync(STATE_PATH); }

// ---------- 登录态检测(文本证据,不导航) ----------
async function detectLoggedIn(page) {
  try {
    await page.waitForTimeout(800);
    if (/\/auth\/login|\/login/.test(page.url())) return false;
    const st = await page.evaluate(() => {
      const body = document.body.innerText || '';
      if (!body.trim()) return 'unknown';
      const header = document.querySelector('header, .ant-layout-header, [class*=header], nav');
      const scope = header || document.body;
      const t = scope.innerText || '';
      const hasRegEntry = t.includes('免费注册') || /登录\s*免费注册/.test(t);
      const hasUserMenu = /退出|个人中心|我的控制台|控制台|我的订单|我的账户/.test(t);
      if (hasRegEntry && !hasUserMenu) return 'logged_out';
      if (!hasRegEntry && hasUserMenu) return 'logged_in';
      if (body.includes('免费注册') && !body.includes('退出') && !body.includes('我的控制台')) return 'logged_out';
      if (!body.includes('免费注册') && (body.includes('退出') || body.includes('我的控制台'))) return 'logged_in';
      return 'unknown';
    }).catch(() => 'unknown');
    return st === 'logged_in' ? true : st === 'logged_out' ? false : null;
  } catch { return null; }
}

// ---------- 自动填登录表单(账号密码;验证码留人工) ----------
async function fillLoginForm(page) {
  const username = ENV.GPUSHARE_USERNAME || '';
  const password = ENV.GPUSHARE_PASSWORD || '';
  if (!username && !password) return false;
  try {
    // 登录页/弹窗中的输入框:手机号、密码(按 placeholder 定位)
    const phone = page.locator('input[placeholder*="手机号"], input[type="text"]').first();
    const pwd = page.locator('input[placeholder*="密码"], input[type="password"]').first();
    if (username && await phone.count() > 0) await phone.fill(username);
    if (password && await pwd.count() > 0) await pwd.fill(password);
    log(username && password ? '已自动填入账号密码(验证码请人工输入)' : '已自动填入账号(密码/验证码请人工输入)');
    return true;
  } catch (e) { log('自动填表失败,请手动输入:', e.message.slice(0, 100)); return false; }
}

// ---------- 模式一:登录 ----------
async function runLogin(opts) {
  log('启动浏览器,准备登录...');
  const browser = await chromium.launch({ channel: 'chrome', headless: opts.headless });
  const ctx = await browser.newContext();
  try {
    const page = await ctx.newPage();
    await page.goto(BASE_URL, { timeout: 30000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);
    const st0 = await detectLoggedIn(page);
    if (st0 === true) {
      log('已登录(检测到登录态),保存会话...');
      await ctx.storageState({ path: STATE_PATH });
      log('登录态已保存:', STATE_PATH);
      return true;
    }
    // 打开登录页
    await page.goto(LOGIN_URL, { timeout: 30000, waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(1500);
    // 若登录页是弹窗形式(URL 未变),尝试点击右上角「登录」
    if (!page.url().includes('/auth/login')) {
      await page.getByText('登录', { exact: false }).first().click({ timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(1500);
    }
    await fillLoginForm(page);
    await shot(page, 'login_start');
    log('请在弹出的窗口中完成登录(验证码需人工输入),登录成功后脚本会自动保存会话...');
    const deadline = Date.now() + 5 * 60 * 1000;
    let trueCount = 0;
    while (Date.now() < deadline) {
      const url = page.url();
      if (!/\/auth\/login|\/login/.test(url)) {
        const st2 = await detectLoggedIn(page);
        if (st2 === true) {
          trueCount++;
          if (trueCount >= 2) {
            log('登录成功,保存会话...');
            await ctx.storageState({ path: STATE_PATH });
            log('登录态已保存:', STATE_PATH);
            await shot(page, 'login_done');
            return true;
          }
          log('检测到已登录(第 1 次确认),再确认一次...');
        } else {
          trueCount = 0;
          if (st2 === null) log('登录态不确定,当前 URL=' + url + ',继续等待...');
        }
      }
      await waitMs(3000);
    }
    log('等待登录超时(5 分钟),请重试 --login');
    return false;
  } finally { await ctx.close(); await browser.close(); }
}

// ---------- 控制台实例列表导航 ----------
function looksLikeInstancePage(page) {
  return page.evaluate(() => {
    const t = document.body.innerText || '';
    if (t.includes('未找到相关页面')) return false;
    return /关机|开机|实例|重启|GPU/.test(t);
  }).catch(() => false);
}

async function openInstanceList(page, consoleUrl) {
  // 恒源云已知实例列表地址(登录后可访问)
  const hireUrl = BASE_URL + '/center/hire';
  if (consoleUrl) {
    try {
      await page.goto(consoleUrl, { timeout: 20000, waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2000);
      if (await looksLikeInstancePage(page)) { log('实例列表:', consoleUrl); return true; }
    } catch (e) { log('打开指定 URL 失败:', e.message.slice(0, 120)); }
  }
  try {
    await page.goto(hireUrl, { timeout: 20000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    if (await looksLikeInstancePage(page)) { log('实例列表:', hireUrl); return true; }
  } catch (e) { log('打开 /center/hire 失败:', e.message.slice(0, 120)); }
  // 右上角用户菜单 → 控制台/我的控制台
  try {
    const header = page.locator('header, .ant-layout-header, [class*=header], nav').first();
    if (await header.count() > 0) {
      const menuItem = header.locator('text=控制台, text=我的控制台, text=个人中心').first();
      if (await menuItem.count() > 0 && await menuItem.isVisible().catch(() => false)) {
        log('点击右上角用户菜单入口');
        await menuItem.click({ timeout: 5000 }).catch(() => {});
        await page.waitForTimeout(2500);
        if (await looksLikeInstancePage(page)) { log('已通过用户菜单进入:', page.url()); return true; }
      }
    }
  } catch {}
  // 普通导航入口
  for (const label of ['控制台', '我的实例', '实例管理', '我的控制台']) {
    try {
      const el = page.getByText(label, { exact: false }).first();
      if (await el.count() > 0 && await el.isVisible()) {
        log('尝试点击入口:', label);
        await el.click({ timeout: 5000 });
        await page.waitForTimeout(2500);
        if (await looksLikeInstancePage(page)) { log('已通过入口进入:', page.url()); return true; }
      }
    } catch {}
  }
  // 候选路由
  const candidates = [
    BASE_URL + '/console',
    BASE_URL + '/console/instance',
    BASE_URL + '/console/instances',
    BASE_URL + '/user/instance',
    BASE_URL + '/user/instances',
    BASE_URL + '/instance',
  ];
  for (const url of candidates) {
    try {
      await page.goto(url, { timeout: 15000, waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1500);
      if (await looksLikeInstancePage(page)) { log('实例列表:', url); return true; }
    } catch {}
  }
  return false;
}

// ---------- 定位实例并点击关机 ----------
async function findShutdownRow(page, name) {
  // 遍历实例行 tr[data-row-key],按名称/主机名匹配
  return page.evaluate((nm) => {
    const rows = Array.from(document.querySelectorAll('tr[data-row-key]'));
    for (const tr of rows) {
      const t = (tr.innerText || '');
      if (t.includes(nm)) {
        return {
          found: true,
          rowKey: tr.getAttribute('data-row-key'),
          rowText: t.replace(/\s+/g, ' ').slice(0, 400),
          status: /已关机|已停止/.test(t) ? 'off' : (/运行中|开机中|启动中/.test(t) ? 'running' : 'other'),
        };
      }
    }
    return { found: false };
  }, name);
}

async function openInstanceMenu(page, rowKey) {
  // 「实例管理」为 hover 展开的 Ant Dropdown,用 mouseenter 事件触发
  return page.evaluate((rk) => {
    const tr = document.querySelector('tr[data-row-key="' + rk + '"]');
    if (!tr) return false;
    const a = tr.querySelector('a.ant-dropdown-trigger') || Array.from(tr.querySelectorAll('a')).find(x => /实例管理/.test(x.textContent || ''));
    if (!a) return false;
    ['mouseenter', 'mouseover', 'mousemove'].forEach((ev) => {
      a.dispatchEvent(new MouseEvent(ev, { bubbles: true, cancelable: true }));
    });
    return true;
  }, rowKey);
}

async function clickMenuItem(page, label) {
  await page.waitForTimeout(1200);
  const pos = await page.evaluate((txt) => {
    const items = Array.from(document.querySelectorAll('.ant-dropdown-menu-item, [class*=dropdown] li'));
    const t = items.find((li) => (li.textContent || '').trim() === txt);
    if (!t) return null;
    const r = t.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }, label);
  if (!pos) return false;
  await page.mouse.move(pos.x, pos.y);
  await page.waitForTimeout(300);
  await page.mouse.down();
  await page.waitForTimeout(150);
  await page.mouse.up();
  return true;
}

async function findAndOperate(page, instanceName, action) {
  const isStart = action === 'start';
  const menuLabel = isStart ? '启动' : '关机';
  const names = [instanceName, ENV.GPUSHARE_INSTANCE_NAME, FALLBACK_HOST].filter(Boolean);
  for (const name of names) {
    log('尝试按名称定位实例:', name);
    const row = await findShutdownRow(page, name);
    if (!row.found) continue;
    log('定位到实例行:', row.rowKey, '| 状态:', row.status);
    log('行内容片段:', row.rowText.slice(0, 200));
    // 幂等:目标状态已满足
    if (isStart && row.status === 'running') {
      log('实例已处于运行状态,无需操作(幂等完成)');
      await shot(page, 'already_running');
      return true;
    }
    if (!isStart && row.status === 'off') {
      log('实例已处于关机状态,无需操作(幂等完成)');
      await shot(page, 'already_off');
      return true;
    }
    // 展开「实例管理」下拉并点击目标菜单项
    await openInstanceMenu(page, row.rowKey);
    const clicked = await clickMenuItem(page, menuLabel);
    if (!clicked) {
      log('下拉菜单中未找到「' + menuLabel + '」菜单项,请检查截图');
      await shot(page, 'menu_no_item');
      return false;
    }
    log('已点击「' + menuLabel + '」菜单项');
    return true;
  }
  log('未找到匹配的实例(名称/主机名均无命中)');
  await shot(page, 'instance_not_found');
  return false;
}

async function confirmDialog(page, action) {
  await page.waitForTimeout(1800);
  const labels = action === 'start'
    ? ['确认启动', '立即启动', '确定', '确认', '是']
    : ['我已了解风险，立即关机', '我已了解风险', '立即关机', '确定', '确认', '是'];
  // 优先在可见弹窗内找按钮,避免点到页面其他同文本元素
  const modals = page.locator('.ant-modal');
  const n = await modals.count().catch(() => 0);
  for (let i = 0; i < n; i++) {
    const m = modals.nth(i);
    if (!(await m.isVisible().catch(() => false))) continue;
    for (const label of labels) {
      try {
        const el = m.getByText(label, { exact: true }).first();
        if (await el.count() > 0 && await el.isVisible()) {
          await el.click({ timeout: 3000 });
          log('已点击确认:', label);
          return true;
        }
      } catch {}
    }
  }
  // 兜底:全局查找
  for (const label of labels) {
    try {
      const el = page.getByText(label, { exact: true }).last();
      if (await el.count() > 0 && await el.isVisible()) {
        await el.click({ timeout: 3000 });
        log('已点击确认:', label);
        return true;
      }
    } catch {}
  }
  log('未找到确认按钮(可能无需确认或弹窗未出现)');
  return false;
}

async function waitInstanceState(page, action) {
  const isStart = action === 'start';
  const pat = isStart ? /运行中|开机中|启动中|开机成功/ : /已关机|已停止|关机成功|已释放/;
  const okMsg = isStart ? '实例已启动' : '实例已关机';
  const deadline = Date.now() + 90 * 1000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(4000);
    const t = await page.evaluate(() => document.body.innerText || '').catch(() => '');
    if (pat.test(t)) { log(okMsg); return true; }
  }
  log(isStart ? '未能自动确认启动状态,请到控制台人工核对' : '未能自动确认状态,请到控制台人工核对(可能已在关机流程中)');
  return false;
}

// ---------- 模式二:关机 ----------
async function runInstanceAction(action, opts) {
  if (!storageStateExists()) errExit(2, '未找到登录态,请先运行 --login');
  const verb = action === 'start' ? '启动' : '关机';
  log('加载登录态并启动浏览器(' + verb + '实例)...');
  const browser = await chromium.launch({ channel: 'chrome', headless: opts.headless });
  const ctx = await browser.newContext({ storageState: STATE_PATH });
  try {
    const page = await ctx.newPage();
    await page.goto(BASE_URL, { timeout: 30000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const st = await detectLoggedIn(page);
    if (st === false) {
      await shot(page, 'not_logged_in');
      errExit(2, '登录态已失效,请重新运行 --login');
    }
    log('已登录,进入控制台实例列表...');
    const ok = await openInstanceList(page, opts.consoleUrl);
    if (!ok) {
      await shot(page, 'instance_list_not_found');
      errExit(3, '未能打开实例列表页面,请用 --console-url 指定,或检查截图');
    }
    await shot(page, 'instance_list');
    if (!await findAndOperate(page, opts.instanceName, action)) {
      errExit(3, '实例定位或操作菜单查找失败,请检查截图与 --instance-name');
    }
    await shot(page, 'after_click');
    await confirmDialog(page, action);
    await shot(page, 'after_confirm');
    await waitInstanceState(page, action);
    log(verb + '流程完成');
    return true;
  } finally { await ctx.close(); await browser.close(); }
}

// ---------- 入口 ----------
function usage() {
  console.log(`用法:
  node shutdown_gpushare.js --login                # 首次:自动填账号密码,人工输验证码,保存登录态
  node shutdown_gpushare.js --shutdown [选项]      # 复用登录态,网页点击关机
  node shutdown_gpushare.js --start [选项]          # 复用登录态,网页点击启动
选项:
  --headless            无头模式(默认有头)
  --instance-name <名>  实例名称(优先于 .env)
  --console-url <URL>   控制台实例列表地址(可跳过自动探测)
环境变量(.env):
  GPUSHARE_USERNAME      恒源云账号(自动填入)
  GPUSHARE_PASSWORD      恒源云密码(自动填入;留空则手动输入)
  GPUSHARE_INSTANCE_NAME 控制台显示的实例名称(定位实例)
`);
}

(async () => {
  const a = parseArgs(process.argv.slice(2));
  if (a.help || (!a.login && !a.shutdown && !a.start)) { usage(); process.exit(0); }
  fs.mkdirSync(APP_DIR, { recursive: true });
  if (a.login) {
    const ok = await runLogin({ headless: a.headless });
    process.exit(ok ? 0 : 1);
  }
  if (a.shutdown) {
    await runInstanceAction('stop', { headless: a.headless, consoleUrl: a.consoleUrl, instanceName: a.instanceName });
    process.exit(0);
  }
  if (a.start) {
    await runInstanceAction('start', { headless: a.headless, consoleUrl: a.consoleUrl, instanceName: a.instanceName });
    process.exit(0);
  }
})();
