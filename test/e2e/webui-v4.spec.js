#!/usr/bin/env node
// Standalone Chrome DevTools Protocol smoke tests for the V4 WebUI.
// This avoids adding a Node test dependency to this uv-managed project.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const BASE_URL = process.env.WEBUI_BASE_URL || 'http://127.0.0.1:8358';
const OUT_DIR = path.resolve(process.cwd(), 'test', 'results', 'test-results');
const DEBUG_PORT = Number(process.env.CHROME_DEBUG_PORT || 9337);
const REAL_E2E = process.env.WEBUI_REAL_E2E === '1';
const REAL_WAIT_MS = Number(process.env.WEBUI_REAL_E2E_TIMEOUT_MS || 25 * 60 * 1000);

function chromeExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ].filter(Boolean);
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) throw new Error('未找到 Chrome/Edge，可设置 CHROME_PATH 后重试');
  return found;
}

function requestJson(method, url) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`${method} ${url} -> ${res.statusCode}: ${raw.slice(0, 160)}`));
          return;
        }
        try {
          resolve(JSON.parse(raw));
        } catch (err) {
          reject(new Error(`JSON 解析失败: ${err.message}; ${raw.slice(0, 160)}`));
        }
      });
    });
    req.on('error', reject);
    req.end();
  });
}

async function waitForChrome(port, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      return await requestJson('GET', `http://127.0.0.1:${port}/json/version`);
    } catch (err) {
      lastError = err;
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  throw lastError || new Error('Chrome DevTools 未就绪');
}

function connectCdp(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let seq = 0;

    ws.addEventListener('open', () => {
      resolve({
        send(method, params = {}) {
          const id = ++seq;
          ws.send(JSON.stringify({ id, method, params }));
          return new Promise((res, rej) => pending.set(id, { res, rej, method }));
        },
        close() {
          ws.close();
        },
      });
    });
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data));
      if (!message.id || !pending.has(message.id)) return;
      const item = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) item.rej(new Error(`${item.method}: ${message.error.message}`));
      else item.res(message.result || {});
    });
    ws.addEventListener('error', reject);
  });
}

async function evaluate(cdp, expression, options = {}) {
  const result = await cdp.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    ...options,
  });
  if (result.exceptionDetails) {
    throw new Error(`页面脚本异常: ${JSON.stringify(result.exceptionDetails)}`);
  }
  return result.result?.value;
}

async function waitFor(cdp, expression, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  while (Date.now() < deadline) {
    lastValue = await evaluate(cdp, expression).catch(() => undefined);
    if (lastValue) return lastValue;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`等待条件超时: ${expression}; last=${lastValue}`);
}

async function pageJson(cdp, apiPath) {
  return evaluate(cdp, `fetch(${JSON.stringify(apiPath)}).then((r) => r.json())`);
}

async function fetchCount(cdp, needle, predicate = 'true') {
  return evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes(${JSON.stringify(needle)}) && (${predicate})).length`);
}

async function confirmVisibleDialog(cdp) {
  await waitFor(cdp, `!!document.querySelector('#confirm-overlay [data-a="confirm"]')`, 8000);
  await evaluate(cdp, `document.querySelector('#confirm-overlay [data-a="confirm"]')?.click(); true`);
}

async function goto(cdp, url) {
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, 'document.readyState === "complete" || document.readyState === "interactive"');
}

async function maybeLogin(cdp) {
  const needsLogin = await evaluate(cdp, `!!document.querySelector('#auth-overlay:not(.hidden)')`);
  if (!needsLogin) return;
  const password = process.env.WEBUI_E2E_PASSWORD || process.env.WEBUI_PASSWORD;
  assert.ok(password, '当前 WebUI 需要登录，请设置 WEBUI_E2E_PASSWORD');
  await evaluate(cdp, `
    (() => {
      const input = document.querySelector('#auth-password');
      input.value = ${JSON.stringify(password)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('#auth-box').requestSubmit();
      return true;
    })()
  `);
  await waitFor(cdp, `!document.querySelector('#auth-overlay:not(.hidden)')`, 8000);
}

async function screenshot(cdp, name) {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const result = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
  });
  fs.writeFileSync(path.join(OUT_DIR, name), Buffer.from(result.data, 'base64'));
}

async function removeDirBestEffort(dir) {
  for (let i = 0; i < 5; i += 1) {
    try {
      fs.rmSync(dir, { recursive: true, force: true, maxRetries: 2, retryDelay: 120 });
      return;
    } catch (err) {
      if (i === 4) {
        console.warn(`临时 Chrome 目录清理失败: ${dir}: ${err.message}`);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
}

async function run() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mimi3-webui-cdp-'));
  const chrome = spawn(chromeExecutable(), [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    `--user-data-dir=${profileDir}`,
    `--remote-debugging-port=${DEBUG_PORT}`,
    'about:blank',
  ], { stdio: 'ignore' });

  try {
    await waitForChrome(DEBUG_PORT);
    const pageTarget = await requestJson('PUT', `http://127.0.0.1:${DEBUG_PORT}/json/new?about:blank`);
    const cdp = await connectCdp(pageTarget.webSocketDebuggerUrl);
    try {
      await cdp.send('Page.enable');
      await cdp.send('Runtime.enable');
      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width: 1440,
        height: 1000,
        deviceScaleFactor: 1,
        mobile: false,
      });
      await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
        source: `
          (() => {
            window.__mimi3Fetches = [];
            window.__mimi3FetchDetails = [];
            const originalFetch = window.fetch.bind(window);
            window.fetch = (...args) => {
              const target = args[0] && args[0].url ? args[0].url : args[0];
              const method = String((args[1] && args[1].method) || 'GET').toUpperCase();
              window.__mimi3Fetches.push(String(target));
              return originalFetch(...args).then((response) => {
                window.__mimi3FetchDetails.push({
                  url: String(target),
                  method,
                  status: response.status,
                  ok: response.ok,
                });
                return response;
              }, (error) => {
                window.__mimi3FetchDetails.push({
                  url: String(target),
                  method,
                  status: 0,
                  ok: false,
                  error: String(error && error.message || error),
                });
                throw error;
              });
            };
          })();
        `,
      });

      await goto(cdp, `${BASE_URL}/#scheduler`);
      await maybeLogin(cdp);
      await waitFor(cdp, `window.__mimi3Fetches?.some((url) => url.includes('/api/scheduler/status'))`);
      await waitFor(cdp, `!document.querySelector('#page-scheduler')?.classList.contains('hidden')`);
      await waitFor(cdp, `document.querySelector('#scheduler-actions')?.innerText.includes('启动调度循环')`);
      await waitFor(cdp, `['/api/status','/api/plan','/api/config','/api/history','/api/scheduler/status'].every((path) => window.__mimi3Fetches?.some((url) => url.includes(path)))`);

      const schedulerText = await evaluate(cdp, `document.querySelector('#page-scheduler')?.innerText || ''`);
      assert.match(schedulerText, /调度队列/);
      assert.match(schedulerText, /启动调度循环/);
      assert.match(schedulerText, /执行待部署队列/);
      await screenshot(cdp, 'webui-v4-scheduler-desktop.png');

      await evaluate(cdp, `document.querySelector('.nav-tab[data-page="history"]')?.click(); true`);
      await waitFor(cdp, `!document.querySelector('#page-history')?.classList.contains('hidden')`);
      await waitFor(cdp, `!!document.querySelector('#history-limit-form input[name="history_limit"]')`);
      const historyUiReady = await evaluate(cdp, `Boolean(document.querySelector('#page-history')?.innerText.includes('显示条数') && document.querySelector('#page-history .history-hero'))`);
      assert.ok(historyUiReady, '部署历史页缺少 V4 条数控制区');
      const historyBefore = await evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/history?limit=3')).length`);
      await evaluate(cdp, `
        (() => {
          const input = document.querySelector('#history-limit-input');
          input.value = '3';
          input.dispatchEvent(new Event('input', { bubbles: true }));
          document.querySelector('#history-limit-form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          return true;
        })()
      `);
      await waitFor(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/history?limit=3')).length > ${historyBefore}`, 8000);
      const historyShowing = await evaluate(cdp, `document.querySelector('#history-showing')?.innerText || ''`);
      assert.match(historyShowing, /上限 3 条/);

      const planBefore = await evaluate(cdp, `(window.__mimi3Fetches || []).filter((url) => url.includes('/api/plan')).length`);
      await evaluate(cdp, `document.querySelector('[data-scheduler-action="refresh-plan"]')?.click(); true`);
      await waitFor(cdp, `(window.__mimi3Fetches || []).filter((url) => url.includes('/api/plan')).length > ${planBefore}`);

      await waitFor(cdp, `['handleDeploy','handleEnable','handleDisable'].every((name) => typeof window[name] === 'function')`);
      const credsApiHidden = await evaluate(cdp, `typeof window.handleCreds === 'undefined' && !document.querySelector('[data-a="creds"]')`);
      assert.equal(credsApiHidden, true);
      const missingUid = '__missing_e2e__';

      const deployBefore = await evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/deploy/')).length`);
      await evaluate(cdp, `(() => { window.__missingDeployPromise = handleDeploy(${JSON.stringify(missingUid)}); return true; })()`);
      await waitFor(cdp, `!!document.querySelector('#confirm-overlay [data-a="confirm"]')`);
      await evaluate(cdp, `document.querySelector('#confirm-overlay [data-a="confirm"]')?.click(); true`);
      await waitFor(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/deploy/${missingUid}') && item.method === 'POST' && item.status === 404).length > ${deployBefore}`, 8000);
      await evaluate(cdp, `window.__missingDeployPromise.then(() => true)`);

      const enableBefore = await evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/account/${missingUid}/enable')).length`);
      await evaluate(cdp, `handleEnable(${JSON.stringify(missingUid)}).then(() => true)`);
      await waitFor(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/account/${missingUid}/enable') && item.method === 'POST' && item.status === 404).length > ${enableBefore}`, 8000);

      const disableBefore = await evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/account/${missingUid}/disable')).length`);
      await evaluate(cdp, `(() => { window.__missingDisablePromise = handleDisable(${JSON.stringify(missingUid)}); return true; })()`);
      await waitFor(cdp, `!!document.querySelector('#confirm-overlay [data-a="confirm"]')`);
      await evaluate(cdp, `document.querySelector('#confirm-overlay [data-a="confirm"]')?.click(); true`);
      await waitFor(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/account/${missingUid}/disable') && item.method === 'POST' && item.status === 404).length > ${disableBefore}`, 8000);
      await evaluate(cdp, `window.__missingDisablePromise.then(() => true)`);

      const accountErrorVisible = await evaluate(cdp, `Array.from(document.querySelectorAll('.toast-error')).some((toast) => toast.innerText.length > 0)`);
      assert.equal(accountErrorVisible, true);

      await evaluate(cdp, `document.querySelector('.nav-tab[data-page="detail"]')?.click(); true`);
      await waitFor(cdp, `!document.querySelector('#page-detail')?.classList.contains('hidden')`);
      const statusPayload = await pageJson(cdp, '/api/status');
      if ((statusPayload.snapshot || []).length) {
        assert.ok(Object.hasOwn(statusPayload.snapshot[0], 'workbench_state'));
        assert.ok(Object.hasOwn(statusPayload.snapshot[0], 'connector_display'));
      }
      const workbenchText = await evaluate(cdp, `document.querySelector('#account-table-section')?.innerText || ''`);
      assert.match(workbenchText, /运行中/);
      assert.match(workbenchText, /冷却中/);
      assert.equal(workbenchText.includes('限流重试中'), false);
      assert.match(workbenchText, /空闲中/);
      assert.match(workbenchText, /token 失效/);
      assert.match(workbenchText, /批量导入账号/);
      assert.equal(workbenchText.includes('补号'), false);
      assert.equal(await evaluate(cdp, `!!document.querySelector('[data-a="creds"]')`), false);

      const importBefore = await fetchCount(cdp, '/api/accounts/import');
      await evaluate(cdp, `document.querySelector('#btn-import-accounts')?.click(); true`);
      await waitFor(cdp, `!!document.querySelector('#account-import-overlay:not(.hidden) #account-import-text')`);
      await evaluate(cdp, `
        (() => {
          const textarea = document.querySelector('#account-import-text');
          textarea.value = 'cUserId=__e2e_cookie__; serviceToken=secret-e2e-token; passInfo=ok';
          textarea.dispatchEvent(new Event('input', { bubbles: true }));
          document.querySelector('#account-import-dialog')?.requestSubmit();
          return true;
        })()
      `);
      await waitFor(
        cdp,
        `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/accounts/import') && item.method === 'POST' && item.status === 200).length > ${importBefore}`,
        8000,
      );
      await waitFor(cdp, `document.querySelector('#account-import-result')?.innerText.includes('缺少字段')`, 8000);
      const importResultText = await evaluate(cdp, `document.querySelector('#account-import-result')?.innerText || ''`);
      assert.match(importResultText, /xiaomichatbot_ph/);
      assert.equal(importResultText.includes('secret-e2e-token'), false);
      await evaluate(cdp, `document.querySelector('#account-import-overlay [data-a="close-import"]')?.click(); true`);

      if (REAL_E2E) {
        console.log('WebUI V4 REAL E2E: start real business checks');
        const realPlan = await pageJson(cdp, '/api/plan');
        const dueUid = realPlan?.due_deploys?.[0]?.uid;
        assert.ok(dueUid, '真实 E2E 需要至少一个待部署账号');

        console.log(`WebUI V4 REAL E2E: deploy due queue uid=${dueUid}`);
        const deployDueBefore = await fetchCount(cdp, '/api/scheduler/deploy-due');
        await evaluate(cdp, `document.querySelector('[data-scheduler-action="deploy-due"]')?.click(); true`);
        await confirmVisibleDialog(cdp);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/scheduler/deploy-due') && item.method === 'POST' && item.status === 200).length > ${deployDueBefore}`,
          REAL_WAIT_MS,
        );
        await waitFor(cdp, `document.querySelector('#scheduler-last-operation')?.innerText.length > 0`, 10000);

        let realStatus = await pageJson(cdp, '/api/status');
        let manualRow = (realStatus.snapshot || []).find((row) =>
          row.uid !== dueUid && row.deploy_state === 'idle' && row.eligible
        );
        assert.ok(manualRow?.uid, '真实 E2E 需要一个额外 idle 账号用于单账号部署');

        console.log(`WebUI V4 REAL E2E: manual deploy uid=${manualRow.uid}`);
        const manualDeployBefore = await fetchCount(cdp, `/api/deploy/${manualRow.uid}`);
        await evaluate(cdp, `(() => { window.__realManualDeployPromise = handleDeploy(${JSON.stringify(manualRow.uid)}); return true; })()`);
        await confirmVisibleDialog(cdp);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/deploy/${manualRow.uid}') && item.method === 'POST').length > ${manualDeployBefore}`,
          REAL_WAIT_MS,
        );
        await evaluate(cdp, `window.__realManualDeployPromise.then(() => true)`);

        realStatus = await pageJson(cdp, '/api/status');
        let stateRow = (realStatus.snapshot || []).find((row) =>
          row.uid !== dueUid && row.uid !== manualRow.uid && row.deploy_state === 'idle' && row.eligible
        );
        if (!stateRow) {
          stateRow = (realStatus.snapshot || []).find((row) =>
            row.uid !== dueUid && row.uid !== manualRow.uid && row.deploy_state !== 'active'
          );
        }
        assert.ok(stateRow?.uid, '真实 E2E 需要一个非 active 账号用于启用/禁用/重载往返');

        console.log(`WebUI V4 REAL E2E: disable/enable uid=${stateRow.uid}`);
        const realDisableBefore = await fetchCount(cdp, `/api/account/${stateRow.uid}/disable`);
        await evaluate(cdp, `(() => { window.__realDisablePromise = handleDisable(${JSON.stringify(stateRow.uid)}); return true; })()`);
        await confirmVisibleDialog(cdp);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/account/${stateRow.uid}/disable') && item.method === 'POST' && item.status === 200).length > ${realDisableBefore}`,
          30000,
        );
        await evaluate(cdp, `window.__realDisablePromise.then(() => true)`);
        realStatus = await pageJson(cdp, '/api/status');
        assert.equal((realStatus.snapshot || []).find((row) => row.uid === stateRow.uid)?.deploy_state, 'disabled');

        const realEnableBefore = await fetchCount(cdp, `/api/account/${stateRow.uid}/enable`);
        await evaluate(cdp, `handleEnable(${JSON.stringify(stateRow.uid)}).then(() => true)`);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/account/${stateRow.uid}/enable') && item.method === 'POST' && item.status === 200).length > ${realEnableBefore}`,
          30000,
        );
        realStatus = await pageJson(cdp, '/api/status');
        assert.notEqual((realStatus.snapshot || []).find((row) => row.uid === stateRow.uid)?.deploy_state, 'disabled');

        console.log('WebUI V4 REAL E2E: scheduler start/stop/tick');
        const startBefore = await fetchCount(cdp, '/api/scheduler/start');
        await evaluate(cdp, `document.querySelector('[data-scheduler-action="start"]')?.click(); true`);
        await confirmVisibleDialog(cdp);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/scheduler/start') && item.method === 'POST' && item.status === 200).length > ${startBefore}`,
          30000,
        );
        await waitFor(cdp, `fetch('/api/scheduler/status').then((r) => r.json()).then((s) => s.running === true)`, 30000);

        const stopBefore = await fetchCount(cdp, '/api/scheduler/stop');
        await evaluate(cdp, `document.querySelector('[data-scheduler-action="stop"]')?.click(); true`);
        await confirmVisibleDialog(cdp);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/scheduler/stop') && item.method === 'POST' && item.status === 200).length > ${stopBefore}`,
          60000,
        );
        await waitFor(cdp, `fetch('/api/scheduler/status').then((r) => r.json()).then((s) => s.running === false || s.mode === 'stopping')`, 60000);

        const tickBefore = await fetchCount(cdp, '/api/scheduler/tick');
        await evaluate(cdp, `document.querySelector('[data-scheduler-action="tick"]')?.click(); true`);
        await confirmVisibleDialog(cdp);
        await waitFor(
          cdp,
          `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/scheduler/tick') && item.method === 'POST' && item.status === 200).length > ${tickBefore}`,
          REAL_WAIT_MS,
        );

        await screenshot(cdp, 'webui-v4-real-business.png');
        console.log('WebUI V4 REAL E2E: real business checks finished');
      }

      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width: 375,
        height: 900,
        deviceScaleFactor: 2,
        mobile: true,
      });
      await goto(cdp, `${BASE_URL}/#scheduler`);
      await maybeLogin(cdp);
      await waitFor(cdp, `!document.querySelector('#page-scheduler')?.classList.contains('hidden')`);
      const mobileOverflow = await evaluate(cdp, `document.documentElement.scrollWidth - window.innerWidth`);
      assert.ok(mobileOverflow <= 1, `移动端出现横向溢出: ${mobileOverflow}px`);
      await screenshot(cdp, 'webui-v4-scheduler-mobile.png');

      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width: 1440,
        height: 1000,
        deviceScaleFactor: 1,
        mobile: false,
      });
      await evaluate(cdp, `document.querySelector('.nav-tab[data-page="config"]')?.click(); true`);
      await maybeLogin(cdp);
      await waitFor(cdp, `!document.querySelector('#page-config')?.classList.contains('hidden')`);
      await waitFor(cdp, `!!document.querySelector('#page-config input[name="public_hostname"]')`);
      await waitFor(cdp, `!!document.querySelector('#page-config input[name="history_limit"]')`);
      await waitFor(cdp, `window.__mimi3Fetches?.some((url) => url.includes('/api/prompt-templates'))`);
      await evaluate(cdp, `
        (() => {
          const input = document.querySelector('#page-config input[name="public_hostname"]');
          input.focus();
          input.value = 'unsaved.example.com';
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        })()
      `);
      const statusBefore = await evaluate(cdp, `(window.__mimi3Fetches || []).filter((url) => url.includes('/api/scheduler/status')).length`);
      await waitFor(cdp, `(window.__mimi3Fetches || []).filter((url) => url.includes('/api/scheduler/status')).length > ${statusBefore}`, 8000);
      const preserved = await evaluate(cdp, `document.querySelector('#page-config input[name="public_hostname"]')?.value`);
      assert.equal(preserved, 'unsaved.example.com');

      await waitFor(cdp, `!!document.querySelector('#page-config textarea[name="text"]:not([disabled])')`);
      await evaluate(cdp, `
        (() => {
          const textarea = document.querySelector('#page-config textarea[name="text"]');
          textarea.focus();
          textarea.value = textarea.value + '\\n# unsaved-e2e-marker';
          textarea.dispatchEvent(new Event('input', { bubbles: true }));
          textarea.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        })()
      `);
      const promptStatusBefore = await evaluate(cdp, `(window.__mimi3Fetches || []).filter((url) => url.includes('/api/scheduler/status')).length`);
      await waitFor(cdp, `(window.__mimi3Fetches || []).filter((url) => url.includes('/api/scheduler/status')).length > ${promptStatusBefore}`, 8000);
      const promptPreserved = await evaluate(cdp, `document.querySelector('#page-config textarea[name="text"]')?.value.includes('# unsaved-e2e-marker')`);
      assert.equal(promptPreserved, true);

      const reloadBefore = await evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/config/reload')).length`);
      await evaluate(cdp, `document.querySelector('#btn-config-page-reload')?.click(); true`);
      await waitFor(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/config/reload') && item.method === 'POST' && item.status === 200).length > ${reloadBefore}`, 8000);

      const invalidBefore = await evaluate(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/config/update')).length`);
      await evaluate(cdp, `
        (() => {
          const input = document.querySelector('#page-config input[name="public_hostname"]');
          input.value = 'not-a-host';
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          document.querySelector('#project-config-form')?.requestSubmit();
          return true;
        })()
      `);
      await waitFor(cdp, `(window.__mimi3FetchDetails || []).filter((item) => item.url.includes('/api/config/update') && item.method === 'POST' && item.status === 400).length > ${invalidBefore}`, 8000);
      const visibleError = await evaluate(cdp, `!!document.querySelector('.toast-error') && document.querySelector('.toast-error')?.innerText.length > 0`);
      assert.equal(visibleError, true);
      await screenshot(cdp, 'webui-v4-config-preserve.png');

      const authRequired = await evaluate(cdp, `fetch('/api/auth/status').then((r) => r.json()).then((r) => r.required)`);
      const logoutButtonState = await evaluate(cdp, `
        (() => {
          const button = document.querySelector('#btn-logout');
          return {
            exists: !!button,
            visible: !!button && getComputedStyle(button).display !== 'none' && !button.hidden,
          };
        })()
      `);
      assert.equal(logoutButtonState.exists, true);
      assert.equal(logoutButtonState.visible, authRequired);
      console.log('WebUI V4 E2E PASS');
    } finally {
      cdp.close();
    }
  } finally {
    chrome.kill();
    await new Promise((resolve) => {
      if (chrome.exitCode !== null) {
        resolve();
        return;
      }
      chrome.once('exit', resolve);
      setTimeout(resolve, 1500);
    });
    await removeDirBestEffort(profileDir);
  }
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
