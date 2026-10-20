import { randomUUID } from 'node:crypto';

const endpoints = Object.freeze({
  itinerary: 'https://itinerary.example.invalid/api/chat',
  email: 'https://email.example.invalid/api/chat',
});
const fields = new Set(['message', 'request_id', 'session_id', 'operation_id', 'plan_version']);

// Call once per logical turn; retain this object before starting any I/O.
export function createTurn(message, conversation = {}, operationId = randomUUID()) {
  return {
    message,
    request_id: conversation.request_id ?? randomUUID(),
    session_id: conversation.session_id ?? randomUUID(),
    operation_id: operationId,
    ...(conversation.plan_version == null ? {} : { plan_version: conversation.plan_version }),
  };
}

class AgentCallError extends Error {
  constructor(code, turn, httpStatus) {
    super('Agent outcome unknown. Check the saved request state before any further action. Do not automatically repeat this POST.');
    this.name = 'AgentCallError';
    this.code = code;
    this.outcome = 'unknown';
    this.httpStatus = httpStatus;
    this.correlation = Object.freeze(Object.fromEntries(
      ['request_id', 'session_id', 'operation_id', 'plan_version']
        .filter((field) => turn[field] != null).map((field) => [field, turn[field]]),
    ));
  }
}

async function readResult(response) {
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error('HTTP error');
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Missing response body');
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 1_048_576) {
        await reader.cancel();
        throw new Error('Response exceeds 1 MiB');
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('Invalid response');
  return result;
}

// Import only from a trusted server handler, never from a browser module.
export async function chat(agent, turn, {
  apiKeys = {
    itinerary: process.env.COLOMBIA_ITINERARY_API_KEY,
    email: process.env.COLOMBIA_EMAIL_API_KEY,
  },
  fetchImpl = globalThis.fetch,
  timeoutMs = 65_000,
} = {}) {
  if (!Object.hasOwn(endpoints, agent)) throw new TypeError('Unsupported agent');
  if (!turn || Object.keys(turn).some((field) => !fields.has(field))) throw new TypeError('Unsupported request field');
  for (const field of ['message', 'request_id', 'session_id', 'operation_id']) {
    if (typeof turn[field] !== 'string' || !turn[field].trim() || turn[field].length > (field === 'message' ? 4000 : 100)) {
      throw new TypeError(`Invalid ${field}`);
    }
  }
  if (turn.plan_version != null && (!Number.isSafeInteger(turn.plan_version) || turn.plan_version < 1)) throw new TypeError('Invalid plan_version');
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 90_000) throw new TypeError('Invalid timeout');
  const key = apiKeys?.[agent];
  if (typeof key !== 'string' || !key.trim() || /[\r\n]/.test(key)) throw new TypeError('Missing or invalid server API key');
  const payload = JSON.stringify(turn);
  let response;
  try {
    response = await fetchImpl(endpoints[agent], {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(timeoutMs),
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: payload,
    });
    return await readResult(response);
  } catch {
    throw new AgentCallError(response ? 'response_unknown' : 'transport_unknown', turn, response?.status);
  }
}
