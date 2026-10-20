import test from 'node:test';
import assert from 'node:assert/strict';
import { createTurn, chat } from './client.mjs';

const apiKeys = { itinerary: 'synthetic-itinerary', email: 'synthetic-email' };
const turn = { message: 'Hola', request_id: 'request-1', session_id: 'session-1', operation_id: 'operation-1', plan_version: 3 };
const options = (fetchImpl) => ({ apiKeys, fetchImpl });

test('create IDs before I/O; preserve conversation and supplied operation IDs', () => {
  const first = createTurn('Hola');
  const second = createTurn('¿Cómo estás?', first);
  assert.equal(second.request_id, first.request_id);
  assert.equal(second.session_id, first.session_id);
  assert.notEqual(second.operation_id, first.operation_id);
  const retry = createTurn(turn.message, turn, turn.operation_id);
  assert.deepEqual(retry, turn);
  assert.ok(first.request_id && first.session_id && first.operation_id);
});

for (const [agent, host] of Object.entries({
  itinerary: 'https://itinerary.example.invalid',
  email: 'https://email.example.invalid',
})) {
  test(`${agent}: fixed endpoint, own key, stable payload, complete result`, async () => {
    const result = { status: 'completed', message: 'Hola', itinerary: null, email: { draft: { text: 'Texto' }, a2a_task_id: 'task-1' } };
    let calls = 0;
    const actual = await chat(agent, turn, options(async (url, init) => {
      calls += 1;
      assert.equal(url, `${host}/api/chat`);
      assert.equal(init.method, 'POST');
      assert.equal(init.redirect, 'error');
      assert.equal(init.headers['X-API-Key'], apiKeys[agent]);
      assert.deepEqual(JSON.parse(init.body), turn);
      assert.ok(init.signal instanceof AbortSignal);
      return Response.json(result);
    }));
    assert.deepEqual(actual, result);
    assert.equal(calls, 1);
    assert.equal(turn.operation_id, 'operation-1');
  });
}

test('reject other hosts, invalid input and missing keys before I/O', async () => {
  let calls = 0;
  const config = options(async () => { calls += 1; });
  for (const agent of ['https://example.com', '__proto__', 'agenda']) {
    await assert.rejects(chat(agent, turn, config), TypeError);
  }
  await assert.rejects(chat('email', { ...turn, operation_id: '' }, config), TypeError);
  await assert.rejects(chat('email', { ...turn, plan_version: 0 }, config), TypeError);
  await assert.rejects(chat('email', { ...turn, url: 'https://example.com' }, config), TypeError);
  await assert.rejects(chat('email', turn, { ...config, apiKeys: {} }), TypeError);
  assert.equal(calls, 0);
});

test('network outcome is unknown, correlated, sanitized and never retried', async () => {
  let calls = 0;
  await assert.rejects(chat('email', turn, options(async () => {
    calls += 1;
    throw new Error('do not expose synthetic-email or transport internals');
  })), (error) => {
    assert.equal(error.outcome, 'unknown');
    assert.equal(error.code, 'transport_unknown');
    assert.deepEqual(error.correlation, { request_id: 'request-1', session_id: 'session-1', operation_id: 'operation-1', plan_version: 3 });
    assert.doesNotMatch(JSON.stringify(error), /synthetic|internals/);
    return true;
  });
  assert.equal(calls, 1);
});

test('deadline also covers response body, with one POST only', async () => {
  let calls = 0;
  let cancelled = false;
  const fetchImpl = async (_url, { signal }) => {
    calls += 1;
    return new Response(new ReadableStream({
      start(controller) { signal.addEventListener('abort', () => controller.error(signal.reason), { once: true }); },
      cancel() { cancelled = true; },
    }));
  };
  // Keep the test alive while the built-in timeout's unref timer runs.
  const keepAlive = setTimeout(() => {}, 1000);
  try {
    await assert.rejects(chat('email', turn, { ...options(fetchImpl), timeoutMs: 10 }), (error) => error.outcome === 'unknown');
  } finally { clearTimeout(keepAlive); }
  assert.equal(calls, 1);
  assert.equal(typeof cancelled, 'boolean');
});

test('bound response bytes and cancel an oversized body without retry', async () => {
  let cancelled = false;
  const fetchImpl = async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array(1_048_577)); },
    cancel() { cancelled = true; },
  }));
  await assert.rejects(chat('email', turn, options(fetchImpl)), (error) => error.code === 'response_unknown' && error.outcome === 'unknown');
  assert.equal(cancelled, true);
});

test('HTTP and malformed replies do not leak contents or trigger a second POST', async () => {
  for (const response of [new Response('private-error', { status: 503 }), new Response('private-error')]) {
    let calls = 0;
    await assert.rejects(chat('email', turn, options(async () => { calls += 1; return response; })), (error) => {
      assert.equal(error.outcome, 'unknown');
      assert.doesNotMatch(error.message, /private-error/);
      return true;
    });
    assert.equal(calls, 1);
  }
});
