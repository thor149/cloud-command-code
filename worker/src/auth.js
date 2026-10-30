const encoder = new TextEncoder();
const hex = bytes => Array.from(new Uint8Array(bytes), x => x.toString(16).padStart(2, '0')).join('');

export async function sign(secret, method, path, timestamp, body = new Uint8Array()) {
  const bytes = typeof body === 'string' ? encoder.encode(body) : body;
  const digest = hex(await crypto.subtle.digest('SHA-256', bytes));
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return 'sha256=' + hex(await crypto.subtle.sign('HMAC', key, encoder.encode(`${method.toUpperCase()}\n${path}\n${timestamp}\n${digest}`)));
}

export function constantTimeEqual(a, b) {
  // Fixed-length digest comparison; malformed/length-mismatched input is public information.
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let different = 0;
  for (let i = 0; i < a.length; i++) different |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return different === 0;
}

export async function verify(request, secret, body, nowSeconds) {
  if (!secret) return false;
  const timestamp = request.headers.get('X-Ccbot-Timestamp');
  const signature = request.headers.get('X-Ccbot-Signature');
  if (!/^\d{1,12}$/.test(timestamp ?? '') || Math.abs(nowSeconds - Number(timestamp)) > 300) return false;
  if (!/^sha256=[0-9a-f]{64}$/.test(signature ?? '')) return false;
  const digest = hex(await crypto.subtle.digest('SHA-256', body));
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
  const supplied = Uint8Array.from(signature.slice(7).match(/../g), byte => parseInt(byte, 16));
  // Native HMAC verification performs the MAC comparison, without a JS early-exit compare.
  return crypto.subtle.verify('HMAC', key, supplied,
    encoder.encode(`${request.method}\n${new URL(request.url).pathname}\n${timestamp}\n${digest}`));
}
