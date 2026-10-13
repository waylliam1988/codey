// One physical POST. Python owns admission, retries, framing and cancellation.
import readline from 'node:readline';
import {once} from 'node:events';
import {channel} from 'node:diagnostics_channel';

const lines = readline.createInterface({input: process.stdin});
const [line] = await once(lines, 'line');
lines.close();
const request = JSON.parse(line);
const controller = new AbortController();
const deadline = setTimeout(() => controller.abort(), request.timeout * 1000);
let idle;
let submitted = false;
let connectionFailed = false;
// Undici publishes this immediately before the first request byte is written.
channel('undici:client:sendHeaders').subscribe(() => {
  submitted = true;
  process.stdout.write(JSON.stringify({type: 'submitted'}) + '\n');
});
channel('undici:client:connectError').subscribe(() => { connectionFailed = true; });
const activity = () => {
  clearTimeout(idle);
  if (request.idleTimeout > 0) idle = setTimeout(() => controller.abort(), request.idleTimeout * 1000);
};
async function emit(value) {
  if (!process.stdout.write(JSON.stringify(value) + '\n')) await once(process.stdout, 'drain');
}
try {
  const response = await fetch(request.url, {
    method: 'POST', headers: request.headers, body: Buffer.from(request.body, 'base64'),
    redirect: 'manual', signal: controller.signal,
  });
  const headers = Object.fromEntries(response.headers);
  // Fetch decodes compressed bodies; their wire length no longer frames the
  // bytes delivered to Python. EOF still proves a complete rejection body.
  if (headers['content-encoding']) {
    delete headers['content-length'];
    delete headers['content-encoding'];
  }
  await emit({type: 'headers', status: response.status, reason: response.statusText, headers});
  activity();
  for await (const chunk of response.body) {
    activity();
    // Keep each pipe frame bounded even if fetch combines large network chunks.
    for (let offset = 0; offset < chunk.length; offset += 65536) {
      await emit({type: 'data', data: Buffer.from(chunk.subarray(offset, offset + 65536)).toString('base64')});
    }
  }
  await emit({type: 'end'});
} catch (error) {
  await emit({type: 'error', requestSubmitted: submitted, connectionFailed, code: error.cause?.code ?? error.code,
    message: error.name + ': ' + error.message + (error.cause?.message ? ': ' + error.cause.message : '')});
} finally {
  clearTimeout(deadline);
  clearTimeout(idle);
}
