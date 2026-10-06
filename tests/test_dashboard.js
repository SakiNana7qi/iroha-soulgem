// Browser behavior checks with a fake DOM/network; no service or hardware.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function browser(status = 200, result = {ok: true}) {
  const elements = new Map();
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const context = {
    document: {getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, {
          style: {}, value: '', textContent: '', open: false,
          addEventListener() {},
          showModal() {this.open = true;},
          close() {this.open = false; queueMicrotask(() => this.onclose());},
        });
      }
      return elements.get(id);
    }},
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      return {ok: status >= 200 && status < 300, status, json: async () => result};
    },
    EventSource: class {}, console,
    setTimeout(callback) {timers.set(++timerId, callback); return timerId;},
    clearTimeout(id) {timers.delete(id);},
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return {
    context, requests, timers,
    element: id => context.document.getElementById(id),
    submit(password) {
      context.document.getElementById('fan-password-input').value = password;
      context.document.getElementById('fan-password-form').onsubmit({preventDefault() {}});
    },
  };
}

async function run() {
  {
    const b = browser();
    const operation = b.context.sendFanControl('/api/fanctl/mode', {mode: 'manual'});
    assert.equal(b.requests.length, 0);
    assert.equal(b.element('fan-password-dialog').open, true);
    b.submit('test-password');
    assert.equal(await operation, true);
    assert.deepEqual(b.requests[0], {url: '/api/fanctl/mode', body: {mode: 'manual', password: 'test-password'}});
    assert.equal(b.element('fan-password-input').value, '');
    assert.equal(await b.context.sendFanControl('/api/fanctl/pwm', {zones: {0: 25}}), true);
    assert.equal(b.element('fan-password-dialog').open, false);
    assert.equal(b.requests[1].body.password, 'test-password');
  }
  {
    const b = browser();
    const operation = b.context.sendFanControl('/api/fanctl/mode', {mode: 'full'});
    b.element('fan-password-input').value = 'cancelled-password';
    b.element('fan-password-dialog').close();
    assert.equal(await operation, false);
    assert.equal(b.requests.length, 0);
    assert.equal(b.element('fan-password-input').value, '');
    assert.ok(b.element('fanctl-action').textContent.includes('已取消'));
  }
  for (const status of [401, 503]) {
    const b = browser(status, {detail: 'rejected'});
    const operation = b.context.sendFanControl('/api/fanctl/mode', {mode: 'full'});
    b.submit('wrong-password');
    assert.equal(await operation, false);
    assert.equal(b.context._fanPassword, null);
    assert.ok(b.element('fanctl-action').textContent.includes(status === 401 ? '密码错误' : '尚未配置'));
    const retry = b.context.sendFanControl('/api/fanctl/mode', {mode: 'curve'});
    assert.equal(b.element('fan-password-dialog').open, true);
    b.element('fan-password-dialog').close();
    assert.equal(await retry, false);
  }
  {
    const b = browser(400, {error: 'Unknown zone'});
    b.context._fanPassword = 'test-password';
    assert.equal(await b.context.sendFanControl('/api/fanctl/pwm', {zones: {99: 20}}), false);
    assert.ok(b.element('fanctl-action').textContent.includes('Unknown zone'));
  }
  {
    const b = browser();
    b.context.fetch = async () => {throw new Error('Network unavailable');};
    b.context._fanPassword = 'test-password';
    assert.equal(await b.context.sendFanControl('/api/fanctl/mode', {mode: 'full'}), false);
    assert.equal(b.context._fanControlBusy, false);
    assert.ok(b.element('fanctl-action').textContent.includes('请求失败'));
  }
  {
    const b = browser();
    const commands = b.context.renderCommands([{name: '<img src=x onerror=bad()>', output: '<script>stealPassword()</script>'}]);
    assert.ok(!commands.includes('<script>'));
    assert.ok(!commands.includes('<img'));
    assert.ok(commands.includes('&lt;script&gt;'));
    const sensors = b.context.renderSensors({method: 'test', sensors: [{name: '<svg onload=bad()>', value: '<img src=x>', status: '<script>bad</script>'}]});
    assert.ok(!sensors.includes('<svg'));
    assert.ok(!sensors.includes('<img'));
    assert.ok(!sensors.includes('<script>'));
    const stopped = b.context.renderFanctl({running: false, error: '<script>bad</script>'});
    assert.ok(stopped.includes('Fan control stopped'));
    assert.ok(!stopped.includes('<script>'));
  }
  {
    const b = browser();
    let pending;
    b.context.sendFanControl = (url, body) => {pending = {url, body};};
    b.context.setFCPWM(0, '25');
    b.context.setFCPWM(1, '35');
    assert.equal(b.timers.size, 1);
    b.timers.values().next().value();
    assert.equal(pending.url, '/api/fanctl/pwm');
    assert.equal(pending.body.zones[0], 25);
    assert.equal(pending.body.zones[1], 35);
  }
  console.log('Dashboard checks passed: password prompts, reuse, cancellation, errors, escaping, and multi-zone updates.');
}

run().catch(error => {console.error(error); process.exitCode = 1;});
