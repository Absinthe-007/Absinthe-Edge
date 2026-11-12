/**
 * Absinthe Edge order worker (V3.1)
 *
 * A tiny private server on Cloudflare that places Kalshi orders for the Absinthe Edge site.
 * Your Kalshi key lives ONLY here (as an encrypted Cloudflare secret); the public site never sees it.
 *
 * Secrets (Worker > Settings > Variables and Secrets > type "Secret"):
 *   KALSHI_API_KEY_ID    the Key ID from kalshi.com > Account > API keys
 *   KALSHI_PRIVATE_KEY   the full contents of the .key/.pem file Kalshi gave you (RSA or Ed25519)
 *   APP_PASSWORD         a long password you make up; the site asks for it before any order
 * Plain-text variables (optional):
 *   ALLOWED_ORIGIN       https://absinthe-007.github.io   (who may call this worker from a browser)
 *   MAX_ORDER_DOLLARS    25    hard cap on the cost of one order
 *   MAX_CONTRACTS        500   hard cap on contracts in one order
 *   MAX_SLIPPAGE_CENTS   2     reject if the price moved more than this above what you confirmed
 *   KALSHI_API_BASE      https://api.elections.kalshi.com   (use https://demo-api.kalshi.co to test on Kalshi's demo)
 *
 * Endpoints (all need the X-App-Password header):
 *   GET  /status            -> balance, limits
 *   POST /quote  {ticker, side}                         -> live price for that side
 *   POST /order  {ticker, side, count, price, clientOrderId}
 *        Buys `count` contracts of `side` at up to `price` (dollars, e.g. 0.42), immediate-or-cancel:
 *        it fills what it can at that price or better right now and cancels the rest. Nothing is left resting.
 */

const PREFIX = "/trade-api/v2";
const TICKER_RE = /^KX(NFL|NBA)[A-Z0-9]*-[A-Z0-9]+(-[A-Z0-9.]+)*$/;

export default {
  async fetch(req, env) {
    const origin = env.ALLOWED_ORIGIN || "https://absinthe-007.github.io";
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-App-Password",
      "Access-Control-Max-Age": "600",
      "Vary": "Origin",
    };
    const json = (obj, status = 200) =>
      new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...cors } });

    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const reqOrigin = req.headers.get("Origin");
    if (reqOrigin && reqOrigin !== origin) return json({ error: "This worker only answers the Absinthe Edge site." }, 403);

    if (!env.APP_PASSWORD || env.APP_PASSWORD.length < 12) return json({ error: "Set an APP_PASSWORD secret (12+ characters) on the worker first." }, 500);
    if (!(await sameSecret(req.headers.get("X-App-Password") || "", env.APP_PASSWORD))) {
      await sleep(800); // slows down password guessing
      return json({ error: "Wrong password." }, 401);
    }
    if (!env.KALSHI_API_KEY_ID || !env.KALSHI_PRIVATE_KEY) return json({ error: "Add KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY secrets to the worker." }, 500);

    const limits = {
      maxOrder: num(env.MAX_ORDER_DOLLARS, 25),
      maxContracts: Math.floor(num(env.MAX_CONTRACTS, 500)),
      slippage: num(env.MAX_SLIPPAGE_CENTS, 2) / 100,
    };
    const base = (env.KALSHI_API_BASE || "https://api.elections.kalshi.com").replace(/\/+$/, "");
    const url = new URL(req.url);

    try {
      if (req.method === "GET" && url.pathname === "/status") {
        const bal = await kalshi(env, base, "GET", "/portfolio/balance");
        if (!bal.ok) return json({ error: kerr(bal) }, 502);
        return json({ ok: true, balance: cents(bal.body.balance), portfolio: cents(bal.body.portfolio_value), limits, demo: base.includes("demo") });
      }

      if (req.method === "POST" && url.pathname === "/quote") {
        const { ticker, side } = await body(req);
        const bad = checkTicket(ticker, side);
        if (bad) return json({ error: bad }, 400);
        const q = await quote(base, ticker, side);
        return q.error ? json(q, 400) : json(q);
      }

      if (req.method === "POST" && url.pathname === "/order") {
        const b = await body(req);
        const ticker = b.ticker, side = String(b.side || "").toLowerCase();
        const count = Number(b.count), price = Number(b.price);
        const bad = checkTicket(ticker, side);
        if (bad) return json({ error: bad }, 400);
        if (!Number.isInteger(count) || count < 1) return json({ error: "Contracts must be a whole number, 1 or more." }, 400);
        if (count > limits.maxContracts) return json({ error: `Max ${limits.maxContracts} contracts per order (MAX_CONTRACTS).` }, 400);
        if (!(price >= 0.01 && price <= 0.99)) return json({ error: "Price must be between 1¢ and 99¢." }, 400);
        const priceC = Math.round(price * 100);
        const cost = (priceC * count) / 100;
        if (cost > limits.maxOrder + 1e-9) return json({ error: `That's $${cost.toFixed(2)}; the per-order cap is $${limits.maxOrder.toFixed(2)} (MAX_ORDER_DOLLARS).` }, 400);
        if (!/^[A-Za-z0-9_-]{8,64}$/.test(String(b.clientOrderId || ""))) return json({ error: "Missing order id; reload the page and try again." }, 400);

        // re-check the live market right before sending
        const q = await quote(base, ticker, side);
        if (q.error) return json(q, 400);
        if (q.ask == null) return json({ error: "Nobody is selling that side right now, so there's nothing to buy." }, 409);
        if (q.ask > price + limits.slippage + 1e-9)
          return json({ error: `Price moved: ${side.toUpperCase()} is now ${Math.round(q.ask * 100)}¢, above the ${priceC}¢ you confirmed.`, ask: q.ask }, 409);

        const order = {
          ticker, side, action: "buy", count, type: "limit",
          [side === "yes" ? "yes_price" : "no_price"]: priceC,
          time_in_force: "immediate_or_cancel",
          client_order_id: String(b.clientOrderId),
        };
        const r = await kalshi(env, base, "POST", "/portfolio/orders", order);
        if (!r.ok) return json({ error: kerr(r) }, r.status === 409 ? 409 : 502);
        const o = r.body.order || {};
        const filled = o.fill_count ?? 0;
        const paid = o.taker_fill_cost != null ? (o.taker_fill_cost + (o.maker_fill_cost || 0)) / 100 : null;
        const fees = o.taker_fees != null ? ((o.taker_fees || 0) + (o.maker_fees || 0)) / 100 : null;
        return json({ ok: true, orderId: o.order_id, status: o.status, filled, asked: count, paid, fees,
                      avg: filled && paid != null ? paid / filled : null, title: q.title });
      }

      return json({ error: "Not found" }, 404);
    } catch (e) {
      return json({ error: "Worker error: " + (e && e.message ? e.message : String(e)) }, 500);
    }
  },
};

// ------------------------------------------------------------------ helpers
const sleep = ms => new Promise(r => setTimeout(r, ms));
const num = (v, d) => (v == null || v === "" || isNaN(Number(v)) ? d : Number(v));
const cents = v => (v == null ? null : Number(v) / 100);
async function body(req) {
  try { return await req.json(); } catch { return {}; }
}
function checkTicket(ticker, side) {
  if (!TICKER_RE.test(String(ticker || ""))) return "Only Kalshi NFL and NBA markets can be traded from here.";
  if (side !== "yes" && side !== "no") return "Side must be YES or NO.";
  return null;
}
async function sameSecret(a, b) {
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([crypto.subtle.digest("SHA-256", enc.encode(a)), crypto.subtle.digest("SHA-256", enc.encode(b))]);
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let d = 0;
  for (let i = 0; i < u.length; i++) d |= u[i] ^ v[i];
  return d === 0;
}
function kerr(r) {
  const e = (r.body && (r.body.error || r.body)) || {};
  const msg = e.message || e.details || e.code || (typeof r.body === "string" ? r.body : "");
  if (r.status === 401) return "Kalshi rejected the API key (check KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY, and that the key can trade).";
  return `Kalshi said ${r.status}${msg ? ": " + String(msg).slice(0, 200) : ""}`;
}

// live price for one side, from the public market endpoint
async function quote(base, ticker, side) {
  const r = await fetch(`${base}${PREFIX}/markets/${encodeURIComponent(ticker)}`, { headers: { Accept: "application/json" } });
  if (!r.ok) return { error: `Couldn't load ${ticker} (${r.status}).` };
  const m = (await r.json()).market || {};
  const st = String(m.status || "").toLowerCase();
  if (st && !["active", "open", "initialized"].includes(st)) return { error: `That market is ${st}, so it can't be traded.` };
  const px = k => {
    const d = m[k + "_dollars"];
    if (d != null && d !== "") return Number(d);
    return m[k] != null ? Number(m[k]) / 100 : null;
  };
  let ask = side === "yes" ? px("yes_ask") : px("no_ask");
  if (side === "no" && ask == null) { const yb = px("yes_bid"); ask = yb != null ? 1 - yb : null; }
  if (!(ask > 0 && ask < 1)) ask = null;
  const bid = side === "yes" ? px("yes_bid") : px("no_bid");
  return { ticker, side, ask, bid: bid > 0 ? bid : null, title: m.title || m.yes_sub_title || ticker,
           sub: side === "yes" ? m.yes_sub_title : m.no_sub_title, closes: m.close_time || null, status: st };
}

// ------------------------------------------------------------------ Kalshi signing
let KEY = null; // cached imported key { alg, key }

async function kalshi(env, base, method, path, payload) {
  const full = PREFIX + path;
  const ts = String(Date.now());
  const sig = await sign(env.KALSHI_PRIVATE_KEY, ts + method + full.split("?")[0]);
  const headers = {
    "KALSHI-ACCESS-KEY": env.KALSHI_API_KEY_ID.trim(),
    "KALSHI-ACCESS-TIMESTAMP": ts,
    "KALSHI-ACCESS-SIGNATURE": sig,
    Accept: "application/json",
  };
  if (payload) headers["Content-Type"] = "application/json";
  const r = await fetch(base + full, { method, headers, body: payload ? JSON.stringify(payload) : undefined });
  let b = null;
  const t = await r.text();
  try { b = JSON.parse(t); } catch { b = t; }
  return { ok: r.ok, status: r.status, body: b };
}

async function sign(pem, msg) {
  if (!KEY) KEY = await importKey(pem);
  const data = new TextEncoder().encode(msg);
  const sig = KEY.alg === "rsa"
    ? await crypto.subtle.sign({ name: "RSA-PSS", saltLength: 32 }, KEY.key, data)
    : await crypto.subtle.sign({ name: KEY.name }, KEY.key, data);
  return b64(new Uint8Array(sig));
}

async function importKey(pem) {
  const txt = String(pem).trim();
  const isPkcs1 = /BEGIN RSA PRIVATE KEY/.test(txt);
  let der = unb64(txt.replace(/-----[^-]+-----/g, "").replace(/\s+/g, ""));
  if (isPkcs1) der = pkcs1to8(der);
  // PKCS#8 can hold RSA or Ed25519: try RSA, then Ed25519
  try {
    const key = await crypto.subtle.importKey("pkcs8", der, { name: "RSA-PSS", hash: "SHA-256" }, false, ["sign"]);
    return { alg: "rsa", key };
  } catch (e) {
    if (isPkcs1) throw new Error("Couldn't read the RSA private key.");
  }
  for (const name of ["Ed25519", "NODE-ED25519"]) {
    try {
      const algo = name === "Ed25519" ? { name } : { name, namedCurve: name };
      const key = await crypto.subtle.importKey("pkcs8", der, algo, false, ["sign"]);
      return { alg: "ed", name, key };
    } catch (e) { /* try next */ }
  }
  throw new Error("Couldn't read KALSHI_PRIVATE_KEY (paste the whole file, including the BEGIN/END lines).");
}

// wrap a PKCS#1 RSAPrivateKey in a PKCS#8 PrivateKeyInfo (WebCrypto only imports PKCS#8)
function pkcs1to8(pk1) {
  const len = n => {
    if (n < 0x80) return [n];
    const b = [];
    while (n > 0) { b.unshift(n & 0xff); n >>= 8; }
    return [0x80 | b.length, ...b];
  };
  const tlv = (tag, bytes) => [tag, ...len(bytes.length), ...bytes];
  const algId = [0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00]; // rsaEncryption, NULL
  const inner = [0x02, 0x01, 0x00, ...algId, ...tlv(0x04, Array.from(pk1))];
  return new Uint8Array(tlv(0x30, inner));
}
function unb64(s) {
  const bin = atob(s);
  const u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
}
function b64(u) {
  let s = "";
  for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
  return btoa(s);
}
