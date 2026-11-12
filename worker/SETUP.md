# Absinthe Edge order worker: setup (about 10 minutes)

Your Kalshi key goes into Cloudflare, not into GitHub or the website. The site only knows the worker's address and asks you for the worker password.

## 1. Make a Kalshi API key for trading
kalshi.com → Account & security → API Keys → **Create key**. Save the Key ID and download the private key file.
Use a new key just for the worker, not the one My Bets uses. That way you can delete it anytime without breaking anything else.

## 2. Paste the code
Cloudflare dashboard → **Workers & Pages** → your worker (or **Create → Create Worker**, name it something like `absinthe-orders`, **Deploy**) → **Edit code**.
Delete everything in the editor, paste the whole contents of `worker.js`, then click **Deploy**.

## 3. Add the secrets
Worker → **Settings → Variables and Secrets → Add**:

| Type   | Name                 | Value |
|--------|----------------------|-------|
| Secret | `KALSHI_API_KEY_ID`  | the Key ID from step 1 |
| Secret | `KALSHI_PRIVATE_KEY` | open the key file in a text editor and paste **all** of it, including the `-----BEGIN…` and `-----END…` lines |
| Secret | `APP_PASSWORD`       | a long password you make up (20+ characters; a password manager is ideal) |
| Text   | `ALLOWED_ORIGIN`     | `https://absinthe-007.github.io` |
| Text   | `MAX_ORDER_DOLLARS`  | `25` (most one order can cost; change anytime) |
| Text   | `MAX_CONTRACTS`      | `500` |
| Text   | `MAX_SLIPPAGE_CENTS` | `2` (if the price jumps more than this above what you confirmed, the order is refused) |

Click **Deploy** again after adding them.

Optional practice run: add the Text variable `KALSHI_API_BASE` = `https://demo-api.kalshi.co`, and use a key from Kalshi's demo site (demo.kalshi.co). Orders then use play money. Delete the variable to go live.

## 4. Connect the site
Copy the worker's address (looks like `https://absinthe-orders.<you>.workers.dev`).
Open the site → **My Bets** → **⚡ One-click Kalshi orders**. Paste the address and your APP_PASSWORD → **Connect**. It should show "connected · cash $…".
Tick **Remember on this device** only on your own phone or computer.

## 5. Optional extra lock (recommended)
Cloudflare → your domain/Workers → **Security → WAF → Rate limiting rules**: limit the worker to about 20 requests a minute. This makes guessing your password useless.

## How an order works
- The site shows a ticket with the live price, contracts, the max price you'll pay, total cost and payout. Nothing happens until you press **Buy**.
- The worker re-checks the live price, your per-order cap and that it's an NFL/NBA market, then sends a **limit order, immediate-or-cancel**. It fills right away at your price or better, and any part that can't fill is canceled. Nothing is left sitting on Kalshi.
- Each ticket has its own order ID, so a double-tap can't place the same order twice.

## Turning it off
Delete the worker, or delete the API key on Kalshi. Either one stops all orders instantly.
