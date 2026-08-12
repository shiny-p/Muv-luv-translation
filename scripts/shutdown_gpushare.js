#!/usr/bin/env node
/**
 * 恒源云实例一键关机小程序
 *
 * 用法:
 *   node shutdown_gpushare.js --login                          # 首次:打开浏览器手动登录,保存会话
 *   node shutdown_gpushare.js --shutdown                       # 复用登录态,定位实例并在网页点击关机
 *   node shutdown_gpushare.js --shutdown --headless            # 无头模式(深夜无人值守可用)
 *   node shutdown_gpushare.js --shutdown --instance-name <名称>  # 指定实例名称
 *   node shutdown_gpushare.js --shutdown --console-url <URL>   # 指定控制台实例列表地址
 *
 * 说明:
 *   - 登录会话持久化在 ~/.gpushare-auto/chrome-profile,密码不写入脚本
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
const USER_DATA_DIR = path.join(APP_DIR, 'chrome-profile');
const SHOTS_DIR = path.join(APP_DIR, 'shots');
const ENV_PATH = path.resolve(__dirname, '..', '.env');
const FALLBACK_HOST = 'i-2.gpushare.com';
const BASE_URL = 'https://gpushare.com';

// ---------- 极简 .env 解析(不依赖 dotenv) ----------
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
async function newContext(opts = {}) {
  const headless = !!opts.headless;
  fs.mkdirSync(APP_DIR, { recursive: true });
  const ctx = await chromium.launchPersistentContext(USER_DATA_DIR, {
    channel: 'chrome',
    headless,
    viewport: { width: 1440, height: 900 },
    args: ['--start-maximized'],
  });
  return ctx;
}

function parseArgs(argv) {
  const a = { headless: false, consoleUrl: '', instanceName: '' };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--login') a.login = true;
    else if (arg === '--shutdown') a.shutdown = true;
    else if (arg === '--headless') a.headless = true;
    else if (arg === '--instance-name' && argv[i + 1]) a.instanceName = argv[++i];
    else if (arg === '--console-url' && argv[i + 1]) a.consoleUrl = argv[++i];
    else if (arg === '-h' || arg === '--help') a.help = true;
  }
  return a;
}

// ---------- 登录态检测(不导航,只读当前页 DOM) ----------
async function detectLoggedIn(page) {
  try {
    await page.waitForTimeout(600);
    // 未登录页通常有「登录」「免费注册」可点链接;已登录导航出现用户名/「控制台」/「退出」
    const loginCount = await page.getByText('登录', { exact: false }).count().catch(() => 0);
    const userCount = await page.getByText(/退出|个人中心|我的控制台|控制台/).count().catch(() => 0);
    // 登录后「免费注册」通常消失
    const regCount = await page.getByText('免费注册', { exact: false }).count().catch(() => 0);
    if (userCount > 0 && loginCount === 0) return true;
    if (regCount > 0) return false;
    if (loginCount > 0) return false;
    return null; // 不确定
  } catch { return null; }
}

// ---------- 模式一:登录 ----------
async function runLogin() {
  log('启动浏览器(请在打开的窗口中完成登录)...');
  const ctx = await newContext();
  try {
    const page = ctx.pages()[0] || await ctx.newPage();
    await page.goto(BASE_URL, { timeout: 30000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);
    const st = await detectLoggedIn(page);
    if (st === true) { log('已登录,会话有效,无需重复登录'); await shot(page, 'login_already'); return true; }
    log('点击「登录」进入登录页...');
    await page.getByText('登录', { exact: false }).first().click({ timeout: 8000 }).catch((e) => {
      log('自动点击登录失败(请手动打开登录页):', e.message.slice(0, 120));
    });
    await shot(page, 'login_start');
    // 等待人工完成登录(URL 离开登录页且出现已登录标志),最多 5 分钟
    const deadline = Date.now() + 5 * 60 * 1000;
    while (Date.now() < deadline) {
      const url = page.url();
      if (!/\/auth\/login|\/login/.test(url)) {
        const st2 = await detectLoggedIn(page);
        if (st2 === true) { log('登录成功,会话已保存'); await shot(page, 'login_done'); return true; }
      }
      await waitMs(3000);
    }
    log('等待登录超时(5 分钟),请重试 --login');
    return false;
  } finally { await ctx.close(); }
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
  // 1) 显式 URL
  if (consoleUrl) {
    try {
      await page.goto(consoleUrl, { timeout: 20000, waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2000);
      if (await looksLikeInstancePage(page)) { log('实例列表:', consoleUrl); return true; }
    } catch (e) { log('打开指定 URL 失败:', e.message.slice(0, 120)); }
  }
  // 2) 尝试点击导航入口
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
  // 3) 候选路由
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
async function findAndShutdown(page, instanceName) {
  const names = [instanceName, ENV.GPUSHARE_INSTANCE_NAME, FALLBACK_HOST].filter(Boolean);
  for (const name of names) {
    log('尝试按名称定位实例:', name);
    const found = await page.evaluate((nm) => {
      const all = Array.from(document.querySelectorAll('body *'));
      let target = null;
      for (const el of all) {
        if (el.children.length === 0) {
          const t = (el.textContent || '').trim();
          if (t && t.includes(nm)) { target = el; break; }
        }
      }
      if (!target) return { found: false };
      // 向上找行容器
      let row = target;
      for (let i = 0; i < 6 && row; i++) {
        const tag = row.tagName.toLowerCase();
        const cls = String(row.className || '');
        if (tag === 'tr' || /(row|card|item|instance|list)/i.test(cls)) break;
        row = row.parentElement;
      }
      const rowEl = row || target.parentElement || target;
      const btns = Array.from(rowEl.querySelectorAll('button, a, span, div'));
      const offBtn = btns.find((b) => {
        const t = (b.textContent || '').trim();
        return /^关机/.test(t) && t.length <= 10;
      }) || null;
      return {
        found: true,
        rowText: (rowEl.innerText || '').slice(0, 400),
        hasOffBtn: !!offBtn,
        offBtnText: offBtn ? offBtn.textContent.trim() : null,
      };
    }, name);
    if (found.found) {
      log('定位到实例行,行内容片段:', found.rowText.replace(/\s+/g, ' ').slice(0, 200));
      if (found.hasOffBtn) {
        // 点击该行内的「关机」按钮
        const clicked = await page.evaluate((nm) => {
          const all = Array.from(document.querySelectorAll('body *'));
          let target = null;
          for (const el of all) {
            if (el.children.length === 0) {
              const t = (el.textContent || '').trim();
              if (t && t.includes(nm)) { target = el; break; }
            }
          }
          if (!target) return false;
          let row = target;
          for (let i = 0; i < 6 && row; i++) {
            const tag = row.tagName.toLowerCase();
            const cls = String(row.className || '');
            if (tag === 'tr' || /(row|card|item|instance|list)/i.test(cls)) break;
            row = row.parentElement;
          }
          const rowEl = row || target.parentElement || target;
          const btns = Array.from(rowEl.querySelectorAll('button, a, span, div'));
          const offBtn = btns.find((b) => {
            const t = (b.textContent || '').trim();
            return /^关机/.test(t) && t.length <= 10;
          });
          if (!offBtn) return false;
          (offBtn).click();
          return true;
        }, name);
        if (clicked) { log('已点击「关机」按钮'); return true; }
      }
      log('该实例行未找到「关机」按钮,请检查截图与行内容');
      await shot(page, 'instance_row_no_btn');
      return false;
    }
  }
  log('未找到匹配的实例(名称/主机名均无命中),可能实例已关机或被移除');
  await shot(page, 'instance_not_found');
  return false;
}

async function confirmDialog(page) {
  await page.waitForTimeout(1500);
  // 关机确认弹窗:尝试点击「确定/确认/是/关机」
  for (const label of ['确定', '确认', '是', '关机']) {
    try {
      const el = page.getByText(label, { exact: true }).last();
      if (await el.count() > 0 && await el.isVisible()) {
        await el.click({ timeout: 3000 });
        log('已点击确认:', label);
        return true;
      }
    } catch {}
  }
  return false;
}

async function waitShutdownState(page) {
  const deadline = Date.now() + 60 * 1000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(4000);
    const t = await page.evaluate(() => document.body.innerText || '').catch(() => '');
    if (/已关机|已停止|关机成功|已释放/.test(t)) { log('实例已关机'); return true; }
  }
  log('未能自动确认状态,请到控制台人工核对(可能已在关机流程中)');
  return false;
}

// ---------- 模式二:关机 ----------
async function runShutdown(opts) {
  log('启动浏览器(复用已保存的登录会话)...');
  const ctx = await newContext(opts);
  try {
    const page = ctx.pages()[0] || await ctx.newPage();
    await page.goto(BASE_URL, { timeout: 30000, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const st = await detectLoggedIn(page);
    if (st === false) {
      await shot(page, 'not_logged_in');
      errExit(2, '未登录:请先运行 --login 完成一次登录');
    }
    log('已登录,进入控制台实例列表...');
    const ok = await openInstanceList(page, opts.consoleUrl);
    if (!ok) {
      await shot(page, 'instance_list_not_found');
      errExit(3, '未能打开实例列表页面,请用 --console-url 指定,或检查截图');
    }
    await shot(page, 'instance_list');
    if (!await findAndShutdown(page, opts.instanceName)) {
      errExit(3, '实例定位或关机按钮查找失败,请检查截图与 --instance-name');
    }
    await shot(page, 'after_click_shutdown');
    await confirmDialog(page);
    await shot(page, 'after_confirm');
    await waitShutdownState(page);
    log('关机流程完成');
    return true;
  } finally { await ctx.close(); }
}

// ---------- 入口 ----------
function usage() {
  console.log(`用法:
  node shutdown_gpushare.js --login                # 首次:手动登录,保存会话
  node shutdown_gpushare.js --shutdown [选项]      # 复用登录态,网页点击关机
选项:
  --headless            无头模式(默认有头)
  --instance-name <名>  实例名称(优先于 .env)
  --console-url <URL>   控制台实例列表地址(可跳过自动探测)
`);
}

(async () => {
  const a = parseArgs(process.argv.slice(2));
  if (a.help || (!a.login && !a.shutdown)) { usage(); process.exit(0); }
  fs.mkdirSync(APP_DIR, { recursive: true });
  if (a.login) {
    const ok = await runLogin();
    process.exit(ok ? 0 : 1);
  }
  if (a.shutdown) {
    await runShutdown({ headless: a.headless, consoleUrl: a.consoleUrl, instanceName: a.instanceName });
    process.exit(0);
  }
})();
