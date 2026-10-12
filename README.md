# SuppAI Vendor API

Backend for the SuppAI vendor flow: vendors apply, admins accept them, vendors list products, admins approve listings into the Shop, and vendors pack and ship orders. It follows *SuppAI Vendors — Version 1 (29 Sep 2026)* and uses the same stack as the doctor and labs backends: **FastAPI + MongoDB, with files in GridFS**.

```
Vendor applies ─► Admin reviews ─► Admin accepts (login + invite mail)
      │                                      │
      ▼                                      ▼
 SA-VEN-1001                   Vendor sets password, signs in
                                             │
                               Request new listing ─► Admin: approve / changes / reject
                                                             │
                                                             ▼
                               Shop (listed only) ─► Member pays (Razorpay)
                                                             │
                               Vendor: Placed → Packed → Shipped → Delivered (or Cancelled)
                                                             │
                               Return window ends ─► payout on settlement cycle, minus commission
```

## Run it

```bash
cp .env.example .env            # set the secrets
docker compose up --build       # Mongo (single-node replica set) + API on :8000
docker compose exec api python -m scripts.create_admin admin@suppai.in "Ops"
```

Open http://localhost:8000/docs for the full interactive API.

Without Docker:

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Tests use an in-memory Mongo mock, so you don't need a database to run them:

```bash
pytest          # 17 end-to-end tests: onboarding, catalogue, shop, orders, payouts
```

## Accounts

| Who | How they sign in | Token |
|---|---|---|
| Vendor | `POST /auth/vendor/login` (username or email) | `role=vendor`, `vendor_id`; every vendor API reads `vendor_id` from the token only |
| Admin | `POST /auth/admin/login`; create with `scripts.create_admin` | `role=admin` |
| Member | Issued by the main SuppAI app | HS256 with `MEMBER_JWT_SECRET`, `role=member`, `sub=<member id>` |

## Endpoints

**Vendor application (public, `/join/vendor`)** — `POST /vendors/applications` starts a draft and returns a `draft_token`. Send it as `X-Draft-Token` on these calls:
- `PUT /vendors/applications/{id}/steps/{company|signatory|statutory|bank|commercial|declarations}` saves steps 1–5 and 7.
- `POST /vendors/applications/{id}/documents` uploads step 6, one file per document type (multipart: `doc_type`, `file`).
- `POST /vendors/applications/{id}/submit` re-checks everything, then issues `SA-VEN-1001…` and sets the status to **submitted**.

**Admin → Onboarding → Vendors**
- `GET /admin/vendors/applications?status=` · `GET …/{id}` (every answer, plus problems) · `GET …/{id}/documents/{doc}/link` (5-minute signed link)
- `POST …/{id}/review` · `…/hold` · `…/reject` (with a note) · `…/accept` (optional `commission_pct`)
- `POST /admin/vendors/{vendor_id}/reissue-login` is the doc's **New password** button. `…/send-invite` is **Send invite email**.
- `PATCH /admin/vendors/{vendor_id}` changes commission or suspends the vendor. `GET /admin/audit-logs` shows the audit trail.

**Vendor portal `/vendor`**
- `POST /auth/vendor/set-password` handles the first login from the invite link (24 hours, single use). `forgot-password` and `change-password` are also available.
- `POST /vendor/uploads/images` takes a photo, resizes it to 200/600/1200 px WebP and strips EXIF. Use the returned id in `images`.
- `GET|POST /vendor/products` · `PATCH /vendor/products/{id}` (edit or resubmit) · `PATCH …/{id}/stock` (stock, in/out of stock, discount, applied immediately) · `DELETE …/{id}` (remove) · `DELETE …/{id}/pending-edit`
- `GET /vendor/orders` · `PATCH /vendor/orders/{id}` · `GET /vendor/reports/summary` · `GET /vendor/payouts` · `GET /vendor/profile`

**Admin → Vendor catalogue**
- `GET /admin/catalogue?status=pending` lists requests (`request_type` is `new` or `edit`). `GET /admin/catalogue/{id}` shows photos, pricing, margin, history and, for edits, a before/after diff.
- `POST /admin/catalogue/{id}/approve` · `/changes` (with a note) · `/reject` (with a reason)
- `GET|PUT /admin/hsn-rates/{hsn}` manages the GST table used to fill GST from the HSN code.

**Shop and members**
- `GET /shop/products?category=&q=&brand=&min_price=&max_price=&in_stock_only=&sort=` · `GET /shop/products/{id}` · `GET /shop/categories`
- `POST /shop/checkout` reserves stock, splits the cart into one order per vendor and creates one Razorpay order.
- `POST /shop/checkout/{id}/verify` takes the signature from the browser checkout. `POST /payments/razorpay/webhook` handles `payment.captured` and `order.paid`. Both are idempotent.
- `GET /shop/orders` · `GET /shop/orders/{id}` return the order tracker, which moves with the vendor's status.

**Payouts and jobs**
- `GET /admin/payouts` · `POST /admin/payouts/{id}/mark-paid` (UTR) · `GET /admin/refunds` · `POST /admin/jobs/run`

## Status rules

**Application:** `draft → submitted → reviewing / on_hold → accepted | rejected`. On hold can go back to reviewing. Accepted and rejected are final.

**Product**

| What happens | Status | Shop |
|---|---|---|
| Vendor submits | awaiting_review | Hidden |
| Admin asks for changes | changes_requested | Hidden |
| Vendor resubmits | awaiting_review | Hidden |
| Admin approves | listed | Shown and buyable |
| Vendor marks out of stock, or stock hits 0 | listed | Shown, cannot be bought |
| Admin rejects | rejected | Hidden |
| Vendor removes | removed | Hidden |
| Vendor edits price, photos or description of a listed product | listed + `pending_edit` | **Old version stays live** until the admin approves the edit |

**Order:** `payment_pending → placed → packed → shipped → delivered`. A vendor can cancel while the order is placed or packed.

| Vendor sets | Member sees | Backend also does |
|---|---|---|
| (checkout) | Awaiting payment | Reserves stock; releases it if unpaid after 30 min (`expired`) |
| Placed (on payment) | Order placed | Emails the vendor |
| Packed | Packed | — |
| Shipped | Shipped | Requires courier and tracking number; saves a shipment |
| Delivered | Delivered | Starts the return window; queues the payout |
| Cancelled | Cancelled | Refunds this vendor's part through Razorpay; puts the stock back |

**Payout:** once an order is delivered and its return window has passed, it goes into the vendor's next payout on their settlement cycle (weekly, fortnightly or monthly): `net = gross − commission%`. An admin pays it by bank transfer and records the UTR.

## Where this differs from the spec, and why

- **No temporary password is ever stored or shown.** Accepting a vendor creates the login with no password and mails a 24-hour, single-use set-password link. The old password generator (word + number + symbol) is replaced by "New password", which reissues that link and invalidates the current password.
- **Edits to a live product don't take it off the Shop.** The edit is staged and reviewed while the old version keeps selling.
- **Stock is reserved at checkout, not at "Placed".** Two members can't pay for the last unit. A payment that arrives after the 30-minute window is refunded automatically.
- **Aadhaar:** only the last 4 digits are stored.
- **MongoDB instead of Postgres:** search uses a Mongo text index (name, brand, category, tags). Order items are embedded in `orders`, and documents and photos live in GridFS. Public photos are served with `immutable` cache headers, so you can put a CDN in front of `/files/images/*`. Private documents are only reachable through 5-minute signed links.

## Before going live

1. **Fill the GST table.** Copy `data/hsn_rates.example.json` to `data/hsn_rates.json`, enter the current rates (confirm them with your CA), then run `python -m scripts.seed_hsn`. Until a code is in the table, the vendor's GST % is used.
2. **Set `DEFAULT_COMMISSION_PCT`.** 15 is a placeholder. The commission can also be set per vendor when accepting.
3. Set real secrets, Razorpay keys and the webhook secret. Point the Razorpay webhook to `/payments/razorpay/webhook` with the `payment.captured` and `order.paid` events.
4. Configure SMTP. Until then, every mail is only written to the `outbox` collection.
5. Set `EXPOSE_INVITE_LINKS=false` in production, so the invite link goes only to the vendor's email.
6. Keep `RUN_BACKGROUND_JOBS=true` on exactly one API instance, or run `python -m scripts.run_jobs` from cron instead.

## Layout

```
app/
  main.py            app, routers, background jobs
  config.py db.py    settings, Mongo connection, indexes, transactions
  security.py deps.py  hashing, JWT, signed links, role guards
  storage.py         GridFS store, image resizing
  schemas/           application steps, products
  services/          applications, products, orders, payments (Razorpay), payouts, audit + email
  routers/           applications, admin_vendors, auth, vendor_products, admin_catalogue, shop, orders, reports, files
scripts/             create_admin, seed_hsn, run_jobs
tests/               end-to-end API tests
```

Collections: `vendor_applications`, `vendors`, `vendor_users`, `admins`, `products`, `product_images`, `listing_events`, `checkouts`, `orders`, `shipments`, `refunds`, `payouts`, `hsn_rates`, `audit_logs`, `outbox`, `webhook_events`, `counters`, plus `files.*` for GridFS.
