# SDLC_OdooConnector: real-time push from Magento 2 to Odoo

A free, optional companion module for the **SDLC Magento 2 Connector for Odoo**
(`sdlc_magento_connector`). It tells Odoo, within about a minute, that a record changed in
Magento, so Odoo does not have to wait for its next scheduled poll.

Magento Open Source has no outgoing webhooks, and Adobe's webhooks and I/O Events are for
Adobe Commerce only. This module works on both Open Source and Adobe Commerce (PaaS).

- License: MIT
- Magento 2.4.6 to 2.4.9, PHP 8.3, 8.4 or 8.5
- No third-party dependency. Odoo's polling stays on as the safety net.

## How it works

```
 save (checkout, admin, REST, import)            every minute (Magento cron)
 ┌──────────────┐  after commit   ┌──────────────────────────┐   signed POST    ┌────────────────────┐
 │ observer /   │ ──────────────▶ │ sdlc_odoo_event_outbox   │ ───────────────▶ │ Odoo               │
 │ MSI plugin   │  1 small row    │ pending → sent / retry → │  {"events":[…]}  │ /magento/webhook/… │
 └──────────────┘                 │ failed                   │                  │ queues a re-fetch  │
                                  └──────────────────────────┘                  └────────────────────┘
```

1. **Observers record, nothing else.** Each one adds one small row to the
   `sdlc_odoo_event_outbox` table: entity type, Magento id, SKU or order number, event type
   and time.
   - The row is written by a *commit callback*, after the order, product or customer
     transaction has committed. It never holds checkout locks, and a rolled-back save
     records nothing.
   - Errors are caught and logged, so an observer can never break or slow a save.
   - While a row is still waiting, repeated saves of the same record update it instead
     of adding rows. Twenty saves during checkout produce one row.
2. **A cron job sends the outbox.** The job `sdlc_odoo_connector_send_outbox` runs every
   minute in its own cron group, `sdlc_odoo_connector`.
   - It claims up to *Events per Request* rows and coalesces rows of the same record, then
     POSTs them to Odoo.
   - It repeats until the outbox is empty or 45 seconds have passed.
   - On failure (Odoo down, HTTP 401 or 5xx, timeout) the rows go to `retry`, backing off
     1, 2, 4, 8 … 60 minutes. They become `failed` after *Give Up After* attempts.
   - A daily job deletes delivered rows after *Keep Delivered Events* days and failed rows
     after 30 days.
3. **Odoo re-reads the record.**
   - Odoo checks the signature, stores the event ids to reject replays, and queues one job
     per record. For example, fifty events for one order make one job.
   - The job fetches the record through the REST API with the instance's normal
     credentials. The pushed payload is never trusted for data.

### Events

| Magento event | Sent as | Odoo re-fetches |
|---|---|---|
| `sales_order_save_after` | `order` (created / updated / status_changed) | the order |
| `sales_order_invoice_save_after` | `invoice` + `order_id` | the parent order (with its invoices) |
| `sales_order_shipment_save_after` | `shipment` + `order_id` | the parent order (with its shipments) |
| `sales_order_creditmemo_save_after` | `creditmemo` + `order_id` + `return_to_stock_items` | the parent order (with its credit memos) |
| `catalog_product_save_after` / `catalog_product_delete_after` | `product` + `sku` | the product (deletions are only logged) |
| `customer_save_after` | `customer` (id only) | the customer |
| `cataloginventory_stock_item_save_after`, MSI `SourceItemsSaveInterface::execute` | `stock` + `sku` | the SKU's stock item |

`return_to_stock_items` lists the order item ids that a credit memo sent back to stock.
Magento does not keep this flag on the stored credit memo, so Odoo cannot read it through
the REST API. The module collects it from three places:

- the items' `back_to_stock` flag, from the admin credit memo form;
- *Automatically Return Credit Memo Item to Stock*;
- the `arguments.extension_attributes.return_to_stock_items` of `POST /V1/order/{id}/refund`
  and `POST /V1/invoice/{id}/refund`.

Odoo's credit memo import uses it to decide whether a refund restocks.

### Request format

```
POST https://odoo.example.com/magento/webhook/<instance key>
Content-Type: application/json
X-SDLC-Signature: sha256=<hex HMAC-SHA256 of the raw body, key = shared secret>
X-SDLC-Timestamp: 1760340000
X-SDLC-Event-Id: <delivery id>

{"delivery_id":"<delivery id>","timestamp":1760340000,"source":"magento",
 "events":[{"id":"<event uuid>","entity":"order","event":"created",
            "occurred_at":"2026-10-13T07:20:00Z","entity_id":"42","increment_id":"000000042"}]}
```

Odoo answers `200 {"status":"ok","accepted":n,"duplicates":n,"ignored":n,"jobs":n}`. It
answers `401` for a bad signature or a timestamp more than 5 minutes off, and `404` for an
unknown or disabled instance. Responses never contain data.

## Install

The module is in the Odoo connector's repository, under
`magento_extension/SDLC/OdooConnector`.

**Option A: copy into `app/code`**

```bash
mkdir -p app/code/SDLC
cp -r /path/to/odoo-magento-connector/magento_extension/SDLC/OdooConnector app/code/SDLC/
bin/magento module:enable SDLC_OdooConnector
bin/magento setup:upgrade
bin/magento setup:di:compile        # production mode only
bin/magento cache:flush
```

**Option B: Composer path repository**

```bash
composer config repositories.sdlc-odoo path /path/to/odoo-magento-connector/magento_extension/SDLC/OdooConnector
composer require sdlc/module-odoo-connector:@dev
bin/magento module:enable SDLC_OdooConnector
bin/magento setup:upgrade
bin/magento setup:di:compile        # production mode only
bin/magento cache:flush
```

The module installs **disabled**: nothing is recorded or sent until you configure it.

When you upgrade the module with `setup:upgrade --keep-generated`, also delete
`generated/code/SDLC` first. A stale generated proxy breaks every `bin/magento` command.

> All XML files are plain, validated XML. If you edit them, never put `--` inside a comment:
> it makes the XML invalid, and Magento then fails on every request.

> Multi-Source Inventory: the module adds a plugin on
> `Magento\InventoryApi\Api\SourceItemsSaveInterface`. If you removed the `Magento_Inventory*`
> modules, delete that `<type>` block from `etc/di.xml`. Legacy stock items are still
> covered by the `cataloginventory_stock_item_save_after` observer.

## Configure

1. **In Odoo:** open *Magento > Configuration > Instances*, then your instance, then the
   **Real-time** tab.
   - Turn on *Real-time Push* and save.
   - If Magento reaches Odoo under another address than `web.base.url`, set *Public Odoo
     URL*.
   - Copy the **Webhook URL** and the **Shared Secret**. The secret is visible to Magento
     administrators only. *Regenerate Secret* and *Regenerate URL* revoke the old ones.
2. **In Magento:** open *Stores > Configuration > Services > Odoo Connector*.
   - Set **Enabled** to Yes, then paste the URL and the secret. The secret is stored
     encrypted.
   - Choose the events to send.
   - Fill *Odoo Database* only if one Odoo server hosts several databases without a
     database filter. It is sent as `X-Odoo-Database`.
   - The same settings are available from the command line:

     ```bash
     bin/magento config:set sdlc_odoo_connector/general/enabled 1
     bin/magento config:set sdlc_odoo_connector/general/endpoint_url https://odoo.example.com/magento/webhook/<key>
     bin/magento config:set sdlc_odoo_connector/general/secret '<shared secret>'
     bin/magento cache:clean config
     ```
3. **Check that the Magento cron runs** (`* * * * * bin/magento cron:run`).
4. **Test.**
   - Change a product name in Magento, then run `bin/magento sdlc:odoo:outbox:flush`
     instead of waiting for the cron.
   - The event appears under *Latest Events* in Odoo's Real-time tab, and its job updates
     the product.

### Commands

| Command | Purpose |
|---|---|
| `bin/magento sdlc:odoo:outbox:status` | Rows per status, plus the latest retry and failed rows with their HTTP status and error |
| `bin/magento sdlc:odoo:outbox:flush [--batches=20]` | Send due rows now, using the same code as the cron |
| `bin/magento sdlc:odoo:outbox:retry` | Make failed and backing-off rows due now, after fixing the URL or secret |
| `bin/magento cron:run --group=sdlc_odoo_connector` | Run only this module's cron group |

## Security notes

- **Authenticity:** every request carries an HMAC-SHA256 signature of the exact body.
  - The body repeats the timestamp and the delivery id, so the signature covers them.
  - Odoo compares signatures in constant time and rejects requests more than 5 minutes
    old or in the future.
  - Odoo stores every event id for a few days and ignores a second delivery of it, so a
    captured request cannot be replayed.
- **No data in transit:** events carry ids, SKUs and order numbers only. They never
  include names, e-mails, addresses, prices or payment data. Odoo re-reads everything
  through the authenticated REST API, so a forged or altered event can at worst trigger
  a harmless re-sync.
- **Secret storage:** in Magento the secret is encrypted with the deployment key. In Odoo
  it is readable only by the *Magento / Administrator* group.
- **Transport:** use `https://` and keep *Verify Odoo's SSL Certificate* set to Yes.
  Redirects are not followed, and only the http and https protocols are allowed.
- **Unknown URL:** a wrong or revoked key gets a 404, so the URL does not reveal whether
  an instance exists. Rejected requests are logged in Odoo at most once a minute per
  instance.
- **Fail safe:** if Odoo is unreachable, checkout is unaffected. Rows wait in the outbox
  and are retried. Rows that are given up are still caught by Odoo's scheduled
  `updated_at` polling.

## Uninstall

To keep the module installed but silent, set `sdlc_odoo_connector/general/enabled` to `0`.
Nothing is recorded or sent while it is disabled.

To remove it, run the commands below. Then delete the module folder, or `composer remove`
it, and drop the `sdlc_odoo_event_outbox` table if it is still there.

```bash
bin/magento module:disable SDLC_OdooConnector
bin/magento setup:upgrade
```
