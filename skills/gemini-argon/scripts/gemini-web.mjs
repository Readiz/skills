#!/usr/bin/env node
// 로그인된 OpenClaw 브라우저에서 Gemini 웹을 새 탭으로 열어 작업 모드 + Argon으로 요청을 보내고
// 완료된 마지막 응답 본문을 stdout으로 낸다. 진행 정보는 stderr에 남긴다.
// 브라우저 시작·상태는 openclaw CLI로, 탭 조작은 같은 브라우저의 CDP에 직접 붙어서 한다
// (CLI는 호출마다 다시 접속해 폴링에 쓰기에는 느리다).
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const USAGE = `사용법: gemini-web.mjs (--prompt "문장" | --prompt-file 파일) [옵션]
  --mode 작업|채팅      기본 작업
  --model argon|flash   기본 argon
  --wait-min 분         응답 대기 상한, 기본 40
  --out 파일            응답을 파일로도 저장
  --keep-tab            끝난 뒤 작업 탭을 닫지 않음
  --profile 이름        OpenClaw 브라우저 프로필, 기본 GEMINI_BROWSER_PROFILE 또는 openclaw`;

const opt = { mode: '작업', model: 'argon', waitMin: 40, keepTab: false };
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  const next = () => { if (i + 1 >= argv.length) usage(); return argv[++i]; };
  if (a === '--prompt') opt.prompt = next();
  else if (a === '--prompt-file') opt.prompt = readFileSync(next(), 'utf8');
  else if (a === '--mode') opt.mode = next();
  else if (a === '--model') opt.model = next().toLowerCase();
  else if (a === '--wait-min') opt.waitMin = Number(next());
  else if (a === '--out') opt.out = next();
  else if (a === '--keep-tab') opt.keepTab = true;
  else if (a === '--profile') opt.profile = next();
  else usage();
}
opt.prompt = (opt.prompt || '').trim();
if (!opt.prompt || !['작업', '채팅'].includes(opt.mode) || !['argon', 'flash'].includes(opt.model)
  || !(opt.waitMin > 0)) usage();
const profile = opt.profile || process.env.GEMINI_BROWSER_PROFILE || 'openclaw';
const MODEL_LABEL = opt.model === 'argon' ? 'Argon' : 'Flash';
const MODEL_RE = opt.model === 'argon' ? '/Argon(?![-\\w])/' : '/Flash(?![-\\w])/'; // Flash-Lite와 구분
const PLACEHOLDER = opt.mode === '작업' ? '작업을 설명하세요' : 'Gemini에게 물어보기';

function usage() { process.stderr.write(`${USAGE}\n`); process.exit(2); }
const log = (msg) => process.stderr.write(`[gemini-web] ${msg}\n`);
class Fail extends Error { constructor(msg, code = 1) { super(msg); this.code = code; } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function openclaw(args, ms = 30000) {
  return execFileSync('openclaw', ['browser', '--browser-profile', profile, '--timeout', String(ms), ...args],
    { encoding: 'utf8', timeout: ms + 15000, stdio: ['ignore', 'pipe', 'pipe'] });
}

// --- 최소 CDP 클라이언트 ---
let ws, seq = 0;
const pending = new Map();
function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { pending.delete(id); reject(new Error(`CDP 응답 없음: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, t });
  });
}
async function js(expr) {
  const r = await send('Runtime.evaluate', { expression: `(()=>{${expr}})()`, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(`페이지 스크립트 오류: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
  return r.result.value;
}
async function waitFor(cond, ms, what, every = 1000) {
  const end = Date.now() + ms;
  for (;;) {
    try { if (await js(`return !!(${cond})`)) return; } catch (e) { if (Date.now() >= end) throw e; }
    if (Date.now() >= end) throw new Fail(`대기 시간 초과: ${what}`);
    await sleep(every);
  }
}

const EDITOR = 'document.querySelector("[contenteditable=true]")';
const MODE_BTN = '[...document.querySelectorAll("button[aria-label]")].find(x=>x.getAttribute("aria-label").includes("모드 선택"))';
const STOP_BTN = 'document.querySelector("button[aria-label=\\"대답 생성 중지\\"]")';
const SEND_BTN = 'document.querySelector("button[aria-label=\\"메시지 보내기\\"]")';

const started = Date.now();
let base, targetId, done = false;
try {
  const status = JSON.parse(openclaw(['status', '--json']));
  if (!status.running || !status.cdpReady) { log('브라우저를 시작합니다.'); openclaw(['start'], 60000); }
  const st = JSON.parse(openclaw(['status', '--json']));
  base = `http://127.0.0.1:${st.cdpPort}`;

  const target = await (await fetch(`${base}/json/new?${encodeURIComponent('https://gemini.google.com/app')}`, { method: 'PUT' })).json();
  targetId = target.id;
  ws = new WebSocket(target.webSocketDebuggerUrl);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    const p = m.id && pending.get(m.id);
    if (!p) return;
    clearTimeout(p.t); pending.delete(m.id);
    m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
  };
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Fail('CDP 연결 실패')); });
  await send('Page.bringToFront');

  await waitFor(`document.readyState==="complete"&&${EDITOR}`, 45000, '입력창');
  await sleep(1500);
  if (!await js('return !!document.querySelector("[aria-label^=\\"Google 계정\\"]")'))
    throw new Fail('Gemini에 로그인돼 있지 않습니다. 이 브라우저 프로필에서 로그인이 필요합니다.');

  // 모드 전환: 입력창 placeholder로 확인한다.
  await js(`const b=[...document.querySelectorAll("button.gem-segmented-button-action")].find(x=>x.innerText.trim()===${JSON.stringify(opt.mode)});if(b)b.click();return !!b;`);
  await waitFor(`${EDITOR}.dataset.placeholder===${JSON.stringify(PLACEHOLDER)}`, 15000, `${opt.mode} 모드`);

  // 모델 선택: 버튼 라벨이 "현재 <모델> 모드"가 될 때까지.
  await waitFor(MODE_BTN, 15000, '모델 선택 버튼');
  const isModel = `${MODE_BTN}.getAttribute("aria-label").includes("현재 ${MODEL_LABEL} 모드")`;
  if (!await js(`return ${isModel}`)) {
    await js(`${MODE_BTN}.click();return 1;`);
    const item = `[...document.querySelectorAll("[role=menuitemradio]")].find(x=>${MODEL_RE}.test(x.innerText))`;
    await waitFor(item, 10000, `${MODEL_LABEL} 메뉴 항목`);
    await js(`${item}.click();return 1;`);
    await waitFor(isModel, 10000, `${MODEL_LABEL} 선택`);
  }
  const modelLabel = await js(`return ${MODE_BTN}.getAttribute("aria-label")`);

  // 여러 줄 프롬프트를 Enter 없이 넣고 실제로 들어갔는지 확인한다.
  const len = await js(`const el=${EDITOR};el.focus();document.execCommand("selectAll",false);document.execCommand("insertText",false,${JSON.stringify(opt.prompt)});return el.innerText.trim().length;`);
  if (len < Math.min(opt.prompt.replace(/\s/g, '').length, 20)) throw new Fail(`프롬프트 입력 확인 실패 (입력 길이 ${len})`);

  const before = await js('return document.querySelectorAll("model-response").length');
  await waitFor(`${SEND_BTN}&&!${SEND_BTN}.disabled`, 10000, '보내기 버튼');
  await js(`${SEND_BTN}.click();return 1;`);
  await waitFor(`${STOP_BTN}||document.querySelectorAll("model-response").length>${before}`, 60000, '제출 확인');
  const url = await js('return location.href');
  log(`제출: ${modelLabel} / ${opt.mode} / ${url}`);

  // 완료: 중지 버튼이 사라지고 몇 초 뒤에도 다시 나타나지 않을 때.
  const deadline = started + opt.waitMin * 60000;
  for (;;) {
    if (Date.now() >= deadline) {
      opt.keepTab = true;
      throw new Fail(`대기 시간 ${opt.waitMin}분 초과. 작업은 계속될 수 있습니다: ${url}`, 3);
    }
    await sleep(3000);
    if (await js(`return !!${STOP_BTN}`)) continue;
    await sleep(4000);
    if (!await js(`return !!${STOP_BTN}`)) break;
  }

  const res = await js(`const r=[...document.querySelectorAll("model-response")].at(-1);if(!r)return null;const mc=r.querySelectorAll("message-content");return (mc.length?mc[mc.length-1]:r).innerText;`);
  if (!res || !res.trim()) throw new Fail(`응답 본문을 찾지 못했습니다: ${url}`);
  const finalUrl = await js('return location.href');
  if (opt.out) writeFileSync(opt.out, res);
  process.stdout.write(res.endsWith('\n') ? res : `${res}\n`);
  log(`완료: ${Math.round((Date.now() - started) / 1000)}초 / ${finalUrl}`);
  done = true;
} catch (e) {
  opt.keepTab = true;
  log(`실패${targetId ? ` (탭 유지: ${targetId})` : ''}: ${e.message}`);
  process.exitCode = e.code || 1;
} finally {
  try { ws?.close(); } catch {}
  if (done && !opt.keepTab && base && targetId) await fetch(`${base}/json/close/${targetId}`).catch(() => log('탭을 닫지 못했습니다.'));
}
