import {awaitCompute, request, takeover} from './api.js';
import {el, display} from './dom.js';
import {COMPUTE_STATES, evidence} from './store.js';
import {status} from './widgets.js';

const fallback = field => field.kind === 'token' ? evidence.get() : field.initial;
function input(field) {
  if (field.kind === 'bool') return el('select', {name:field.name},
    el('option',{value:'true', selected:fallback(field) === true ? '' : undefined},'是'),
    el('option',{value:'false', selected:fallback(field) === false ? '' : undefined},'否'));
  if (field.kind === 'json') return el('textarea', {name:field.name, required:''}, JSON.stringify(fallback(field), null, 2));
  const numeric = ['int','float'].includes(field.kind);
  return el('input',{name:field.name, required:'', type:numeric ? 'number' : 'text',
    step:field.kind === 'float' ? 'any' : undefined, value:fallback(field) ?? ''});
}
function parse(form, fields) {
  const data = new FormData(form);
  return Object.fromEntries(fields.map(field => {
    const raw = data.get(field.name);
    const convert = ['int','float'].includes(field.kind) ? Number :
      ['bool','json'].includes(field.kind) ? JSON.parse : String;
    return [field.name, convert(raw)];
  }));
}
function computeSummary(data) {
  const state = COMPUTE_STATES[data.status] || data.status;
  const taken = data.takeovers > 0 ? ` · 已接手 ${data.takeovers} 次（第 ${data.attempt} 轮）` : '';
  const detail = data.status === 'ready' ? ' · ' + display(data.result)
    : data.status === 'rejected' ? ' · ' + (data.detail || '') : '';
  return `任务 ${data.token} · ${state}${taken}${detail}`;
}
export function actionForm(action, refresh) {
  const notice = status('');
  const button = el('button', {type:'submit'}, action.label);
  const form = el('form', {class:'action', id:action.name},
    el('h3',{},action.label), el('p',{},action.help),
    el('div',{class:'fields'},action.fields.map(field => el('label',{},
      el('span',{},field.label), input(field)))),
    button, notice);
  form.addEventListener('submit',async event => {
    event.preventDefault();
    button.disabled = true;
    notice.className = 'status';
    try {
      const values = parse(form, action.fields);
      // 按计算号查询：只看状态，不触发任何写入。
      if (action.kind === 'lookup') {
        const data = await request('/compute/' + values[action.fields[0].name]);
        notice.textContent = computeSummary(data);
        notice.classList.add(data.status === 'rejected' ? 'error' : 'ok');
        return;
      }
      // 接手续跑：沿用原计算号，成功后等待新一轮结果。
      if (action.kind === 'takeover') {
        const data = await takeover(values[action.fields[0].name]);
        evidence.save(data.token);
        notice.textContent = '已接手，沿用计算号 ' + data.token;
        const result = await awaitCompute(data.token, current => {
          notice.textContent = computeSummary(current);
        });
        if (result.status === 'rejected') throw new Error(result.detail);
        notice.textContent = computeSummary(result);
        notice.classList.add('ok');
        await refresh();
        return;
      }
      const data = await request(action.path, values);
      if (data.token && (data.status === 'queued' || data.status === 'running')) {
        evidence.save(data.token);
        if (data.reused) {
          notice.textContent = '断线重试，沿用原计算号 ' + data.token;
        }
        const result = await awaitCompute(data.token, current => {
          notice.textContent = computeSummary(current);
        });
        if (result.status === 'rejected') throw new Error(result.detail);
        notice.textContent = computeSummary(result);
      } else if (data.token && data.status === 'ready') {
        // 幂等重试命中了已算完的原任务：直接展示原结果，不新增评估。
        evidence.save(data.token);
        notice.textContent = (data.reused ? '断线重试，沿用原计算号。' : '') + computeSummary(data);
      } else if (data.token && data.status === 'rejected') {
        evidence.save(data.token);
        throw new Error('原计算号此前已被拒绝：' + (data.detail || '未知原因'));
      } else {
        const reused = data.reused ? '（已备案，返回原单） · ' : ' · ';
        notice.textContent = action.success + reused + display(data.code ?? data.status);
      }
      notice.classList.add('ok');
      await refresh();
    } catch (error) {
      notice.textContent = error.message;
      notice.classList.add('error');
    } finally { button.disabled = false; }
  });
  return form;
}
