# Fix the foundation, then outbuild Emipro and Ventor

There is real room in the Odoo 19 ↔ Magento 2 connector market. Three vendors lead it: **Emipro ($615), VentorTech ($559 plus a $56 core) and Webkul ($249 base, with core functions sold as paid add-ons)**. The remaining ~27 connectors on apps.odoo.com for Odoo 19 are low-traction releases. None of these vendors has solved the problems merchants actually hit: **orders silently skipped during paginated imports, Odoo totals that don't match Magento, duplicate or wrongly merged contacts, oversell from stock edge cases, and auth breakage since Magento 2.4.4**. VentorTech is still shipping fixes for all four of the first categories in 2025–2026, a decade after Webkul fixed the same classes of bug ([VentorTech changelog](https://apps.odoo.com/apps/modules/17.0/integration_magento2); [Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html)). Our `sdlc_magento_connector` is free and already named in a third-party "top 5" list. Today, though, it is an MVP. It has **15 critical bugs**. It has no job queue, no pagination, no incremental sync and no per-record log, and it uses an auth model that forces merchants to enable a setting Adobe recommends against. It does have two assets most rivals lack: **credit memo sync in both directions** and a **generic, bidirectional field-mapping UI** ([internal audit](../research_notes/Magento%20connector%20market%20gaps/our_module_audit.md)). The recommended path has four phases plus a parallel UI track. Phase 0 stops the module from losing or corrupting data. Phase 1 builds the reliability layer the market lacks: an error center modeled on Celigo, an `updated_at` watermark, retries that honor Retry-After, and native OAuth 1.0a. Phase 2 reaches parity with Emipro and Ventor on catalog, orders, stock and auto-workflow. Phase 3 claims capabilities no Odoo vendor advertises: tier and customer-group prices, bundle/grouped export, Adobe B2B write-back, a free Magento companion module for real-time events, and totals reconciliation without a plug line. Across all phases, the dashboard and forms follow the SDLC TRS house style (`odoo-trs-ui`): shared TRS tokens with fallbacks, theming left to the theme module, standard Odoo views first and a single custom "Magento Console" whose every figure opens its records. The positioning that follows is "the Magento connector that never silently loses an order". It can support a paid Pro tier in the $450–$550 band that already exists, while the free core stays the top of the funnel.

## Three vendors own a crowded $20–$615 market

A search of apps.odoo.com for "magento" filtered to Odoo 19.0 returns **43 apps, roughly 30 of them real Magento connectors** ([apps.odoo.com 19.0 search](https://apps.odoo.com/apps/modules/browse?search=magento&series=19.0)).

**The feature-complete tier: Emipro and VentorTech.**
- **Emipro's** `odoo_magento2_ept` costs **$615.10**, has about 19,800 lines of code, and is sold separately for each Odoo version from 9 to 20 ([Emipro 19.0](https://apps.odoo.com/apps/modules/19.0/odoo_magento2_ept)).
  - It imports and exports simple and configurable products, and pushes stock with or without MSI.
  - It exports invoices, shipments with tracking, and Odoo refunds as Magento credit notes.
  - It runs queues and webhooks for order create/cancel, and adds per-website dashboards, "net profit insights" and digest emails.
  - Its FAQ states that **creating products from Odoo in Magento is not supported**.
- **VentorTech's** `integration_magento2` costs **$559.08** plus a **$56.02 `integration` core**. On its own store it is also sold as a subscription at **€499/yr** (dropping to €299.40 by year three) or €499 one-time, with an optional **€449.10 installation service** ([Ventor 19.0](https://apps.odoo.com/apps/modules/19.0/integration_magento2); [Ventor store](https://ecosystem.ventor.tech/product/odoo-magento-2-connector-pro/)). It has the strongest architecture in the market:
  - a background job queue whose smart retry honors `retry-after`, plus bulk requeue
  - per-field sync direction with optional Python transform scripts
  - a 7-step auto-workflow keyed to Magento order status
  - MSI per-source mapping and BoM kit stock
  - B2B VAT handling and per-store-view translations
  - auto-cleaned logs, and pre-go-live validation of duplicate SKUs
  - Its own listing admits that **returns, refunds and credit memos are "Coming soon"**.

**Webkul** sells `magento2x_odoo_bridge` for **$249**, or from **$199 on its own store**. The base product connects one Odoo to one Magento store. **Credit memos, multi-store, multi-language, MSI, customer pricelists and multi-image** are each a paid add-on. Installation is +$100, support +$99.50 to $139.30, and the Adobe Commerce edition +$199. Real-time order sync needs a separate patch ([Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html); [Webkul guide](https://webkul.com/blog/odoo-multichannel-magento2x-connector/amp/)).

**Odoo IN**, Odoo's India subsidiary, launched `ecommerce_magento` at **$195.33 for Odoo 19 only**. It is branded "directly built and supported by Odoo". It is the only competitor besides us positioned exclusively on 19, and the only one that advertises **OAuth 1.0a**. It does not support refunds, webhooks, category push or dashboards, and it turns variants into standalone products ([Odoo IN 19.0](https://apps.odoo.com/apps/modules/19.0/ecommerce_magento)).

That branding matters because Odoo SA itself ships no Magento connector. Odoo 19's official connectors are Amazon, Shopee, Lazada, TikTok Shop and Gelato ([Odoo 19 Sales docs](https://www.odoo.com/documentation/19.0/applications/sales/sales.html)).

**The open-source route is effectively dead.**
- OCA `connector-magento` has had no working module since 12.0. Branches 13.0–20.0 hold only scaffolding ([OCA repo](https://github.com/OCA/connector-magento)).
- It was built for the legacy SOAP/XML-RPC API.
- An 18.0 port opened in July 2026 drew the reviewer comment "nothing works, even installing the module" ([OCA PR #340](https://github.com/OCA/connector-magento/pull/340)).
- Cybrosys and Ksolves return 404s for current versions ([Cybrosys 19.0](https://apps.odoo.com/apps/modules/19.0/odoo_magento_2_4_5); [Ksolves 18.0](https://apps.odoo.com/apps/modules/18.0/ks_magento_connector)).

**iPaaS tools solve a different problem.** Alumio, Celigo, SyncSpider and Zapier can connect the two systems, but they bring subscription pricing and no Odoo-native accounting depth ([Alumio](https://www.alumio.com/connect/magento-to-odoo); [Zapier](https://zapier.com/apps/magento-v2/integrations/odoo)). Their value to us is as a benchmark for operations, discussed below.

**The long tail is mostly marketing ideas rather than serious threats.** Several small vendors carry one design idea each that is worth taking:
- **Miko** ($416.69) separates **"Failed" (transient, exponential backoff)** jobs from **"Needs attention" (not retried)** ones, and stores payloads so retries replay without refetching ([Miko](https://apps.odoo.com/apps/modules/19.0/miko_magento)).
- **ECOSIRE** ($482.13) offers a **dry-run import preview**, read-only modeling of Adobe B2B companies and shared catalogs, Retry-After handling, and **29 multi-company record rules** ([ECOSIRE](https://apps.odoo.com/apps/modules/19.0/magento_store_management)).
- **Jupical** ($229.16) imports Magento credit memos and has per-entity 1-way/2-way/Off toggles ([Jupical](https://apps.odoo.com/apps/modules/19.0/jupical_magento)).
- **PIT Solutions** ($226.32) has role-based access but **requires a companion Magento extension** and does not support MSI ([PIT](https://apps.odoo.com/apps/modules/19.0/pits_odoo_magento_connector)).

### Competitor comparison matrix (Odoo 19, as of October 2026)

The table below compares our module with the main competitors. A dash means the feature was not advertised. "Ours today" reflects the code audit, not the listing.

| Capability | **Ours today** | Emipro | VentorTech | Webkul | Odoo IN | Notable others |
|---|---|---|---|---|---|---|
| Price (one-time) | Free | $615.10 (+$54.90 engine) | $559.08 + $56.02 core; or €499/yr | $249 app; $199 + paid add-ons | $195.33 | Miko $417, ECOSIRE $482, Jupical $229, PIT $226 |
| Odoo 19 build | Yes (19 only) | Yes (9–20, per-version purchase) | Yes (16–19, per-version) | Yes (8–19) | Yes (19 only) | Most 2026 v1.0 releases |
| Auth | Static Bearer; requires insecure 2.4.4+ flag | Not stated | Bearer integration token | Not stated | **OAuth 1.0a** or admin token | Techloyce: admin user/pass |
| Configurable products | **Crashes sync** | Import/export | Both ways | Both ways | Variants become standalone | Jupical: variants |
| Bundle / grouped | Crash | — | Orders: decomposed or kit | — | — | None advertise export |
| Create products Odoo→Magento | Yes (simple) | **No** | Yes (exported disabled) | Yes | No | PIT, Jupical yes |
| Stock push / MSI | **No on-hand push; no MSI** | Yes, with/without MSI | Real-time, per-source MSI, kits | Real-time; MSI paid add-on | Push after order sync | PIT: no MSI |
| Order fidelity (tax/discount/shipping) | **Lines only; draft; never confirmed** | Yes | Yes, plus coupons | Yes | Yes, taxes matched | Miko: unmapped tax halts |
| Auto-workflow | No | Financial-status workflow | 7 steps per status | Status mapping | Paid orders auto-invoiced | — |
| Invoice export | Yes | Yes | Via workflow | Yes | Auto for paid | Jupical both ways |
| Shipments + tracking | Export without tracking; import broken | Yes | Yes, partial, both ways | Yes | Two-way | Miko: double-ship guard |
| **Credit memos** | **Both directions** | Odoo→Magento | **Not supported** | Paid add-on | No | Jupical Magento→Odoo only |
| Real-time | Product push inside `write()` | Webhooks (create/cancel) | Webhooks via Mageplaza extension | Needs patch | No (10-min polling) | — |
| Job queue + retry | **None** | Queues, manual processing | Queue, Retry-After, bulk requeue | Crons | Crons | ECOSIRE, Miko: backoff |
| Per-record log | **Counts only, errors always 0** | Logs (disk-bloat complaint) | Every action, auto-cleanup, email alerts | Sync feeds/history | Debug API logs, chatter, email | Miko: job dashboard |
| Multi-instance / website | Not instance-safe | Yes | Yes, plus translations | Paid add-on | Multi-store, multi-company | ECOSIRE: record rules |
| Dashboard | OWL, no drill-down | Per website, net profit, digest | Revenue, top products, countries | Charts | None | Jupical, ECOSIRE OWL |
| Field mapping UI | **Generic JSON-path, both ways** | SKU matching | Per-field, Python transforms, live test | Manual mapping views | — | — |
| Pricelists / special / tier | No | Not mentioned | Pricelist + special price export; no tier | Paid add-on | Not on import | **No vendor advertises tier prices** |
| Adobe B2B | No | No | VAT/company detection only | No | No | ECOSIRE read-only |
| Magento extension needed | No | Unconfirmed | Only for real-time | Yes (MOB module) | No | PIT: mandatory |

Sources: [Emipro](https://apps.odoo.com/apps/modules/19.0/odoo_magento2_ept), [Ventor](https://apps.odoo.com/apps/modules/19.0/integration_magento2), [Webkul app](https://apps.odoo.com/apps/modules/19.0/magento2x_odoo_bridge), [Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html), [Odoo IN](https://apps.odoo.com/apps/modules/19.0/ecommerce_magento), [Miko](https://apps.odoo.com/apps/modules/19.0/miko_magento), [ECOSIRE](https://apps.odoo.com/apps/modules/19.0/magento_store_management), [Jupical](https://apps.odoo.com/apps/modules/19.0/jupical_magento), [PIT](https://apps.odoo.com/apps/modules/19.0/pits_odoo_magento_connector), [internal audit](../research_notes/Magento%20connector%20market%20gaps/our_module_audit.md).

The matrix shows two kinds of market gap. **Table-stakes gaps** are features the leaders already have and we lack:
- configurable products
- stock and MSI
- order fidelity
- a queue and per-record logs

**White space** is features nobody, or only one fringe vendor, offers:
- tier and customer-group prices
- bundle/grouped export
- full credit memos in both directions
- Adobe B2B write-back
- real-time sync without a third-party Magento extension

## Missed orders, wrong totals and duplicate contacts have recurred for a decade

Public reviews are sparse and curated. **Emipro shows only about 7 of its 36 comments**, and Ventor's are uniformly 5-star. The most honest evidence of what breaks is therefore in vendor changelogs and FAQs ([Emipro 14.0](https://apps.odoo.com/apps/modules/14.0/odoo_magento2_ept); [Ventor](https://apps.odoo.com/apps/modules/17.0/integration_magento2)). Ranked by recurrence and business impact, the pain points form a clear hierarchy.

**1. Silent order loss is the most severe.**
- In **August 2026** VentorTech fixed "a critical issue where orders could be skipped or missed when a single import run had to fetch more than one page of orders". It patched "a remaining case" ten days later and capped manual imports at 50 pages to prevent timeouts ([Ventor changelog](https://apps.odoo.com/apps/modules/17.0/integration_magento2)).
- The WMS Mintsoft saw Magento orders "stopped importing with no import errors" until the page size dropped from 1000 to 300 ([Mintsoft](https://help-mintsoft.theaccessgroup.com/en/articles/11679224-magento-orders-stopped-importing)).
- Cut-off filters that use creation date mean that "any order created before your cut-off date will never be imported—even if it's updated later" ([Ventor FAQ](https://ecosystem.ventor.tech/faq/e-commerce-connectors/common-questions/how-do-you-filter-orders-to-be-downloaded-how-is-the-order-cut-off-date-used/)).
- Our module fetches without pagination or a watermark, so it sits squarely inside this failure class ([internal audit](../research_notes/Magento%20connector%20market%20gaps/our_module_audit.md)).

**2. Tax and total mismatches are structural, not occasional.**
- Ventor publishes a dedicated FAQ on why "the total amount in the sales order differs". It names rounding stage, precision, tax-included vs tax-excluded pricing, and fiscal positions. Its default remedy is to inject a **"price difference line"** ([Ventor FAQ](https://ecosystem.ventor.tech/faq/e-commerce-connectors/common-questions/the-total-amount-in-the-sales-order-differs-from-the-calculated-amount-in-odoo-why-it-happens-and-how-to-fix-it-2/)). That line papers over the gap instead of explaining it to the accountant.
- Emipro asks users to mirror Magento's tax configuration in Odoo ([Emipro docs](http://docs.emiprotechnologies.com/odoo-magento2-connector/v14/import-export-operations/import-sale-order/import-sale-order-from-magento.html)).
- Webkul refuses to sync taxes from Odoo to Magento at all ([Webkul UVdesk](https://webkul.uvdesk.com/en/blog/import-export-magento-odoo)).
- Our module imports no shipping, discount or tax lines, so every order total diverges ([internal audit](../research_notes/Magento%20connector%20market%20gaps/our_module_audit.md)).

**3. Duplicate or wrongly merged contacts are the most frequent bug class.**
- Ventor shipped contact-matching fixes in at least five releases in 2025–2026. One of them fixed guest orders without an email being "incorrectly linked to an existing contact with the same name", which is a privacy failure ([Ventor changelog](https://apps.odoo.com/apps/modules/17.0/integration_magento2)).
- Webkul fixed duplicate addresses three times in 2016–2017 ([Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html)).
- **Our module matches partners by email and then by name, and drops the shipping address.** That reproduces the exact bug Ventor patched in March 2026.

**4. Stock errors cause oversell.**
- Ventor fixed stock-sync bugs in at least four releases (2024–2026): zero stock, BoM kits, manufacturing triggers, and multi-company access.
- OCA logged backorderable products being pushed as out of stock ([OCA issues](https://github.com/OCA/connector-magento/issues)).
- MSI makes this harder. Salable quantity equals source quantity minus reservations, so pushing source quantities does not equal what the storefront shows ([Magento MSI wiki](https://github.com/magento-engcom/msi/wiki/Overview)).

**5. Auth breakage after Magento 2.4.4 is a known upgrade trap.** Since 2.4.4, integration tokens are rejected as standalone Bearer tokens unless `oauth/consumer/enable_integration_as_bearer=1`. Adobe "does not recommend" that flag because the tokens never expire ([Adobe token auth](https://developer.adobe.com/commerce/webapi/get-started/authentication/gs-authentication-token)). Merchants hit 401s after upgrades ([Magento community](https://community.magento.com/t5/Magento-2-x-Technical-Issues/Integration-to-Magento-failing-despite-API-access-granted/td-p/521133)). **Both Ventor and our module rely on that flag.**

**6. Commercial pain is just as visible:**
- **per-version repurchase**
- **90-day support windows** on Odoo Apps purchases
- discontinued older versions (Emipro v14 is "no longer available for sale")
- **one Webkul customer filing "almost 100 tickets"**
- reviewers switching away from "a different widely used Odoo-Magento connector"

Sources: [Ventor 17.0](https://apps.odoo.com/apps/modules/17.0/integration_magento2); [Emipro 14.0](https://apps.odoo.com/apps/modules/14.0/odoo_magento2_ept); [Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html).

**7. Observability is bolted on late.**
- Ventor's Consistency Wizard is hidden behind debug mode.
- Webkul tells users to "apply the not synced filter" to find missing orders.
- A February 2026 Emipro reviewer reported that logs filled **about 90% of disk** ([Emipro 19.0](https://apps.odoo.com/apps/modules/19.0/odoo_magento2_ept); [Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html)).

The lesson is that the market sells support because the software is not trustworthy on its own. A connector that makes failures visible, bounded and self-healing attacks the real cost centre.

There is one caveat. No vendor publishes ticket counts by category, so this ranking is inferred from how often issue classes recur in changelogs and how severe the vendors say they are. It is not based on measured frequency.

## Our module has two rare strengths and fifteen critical bugs

The October 2026 audit describes `sdlc_magento_connector` as **MVP-level**:
- ~6,000 lines of Python
- 13 new and 6 inherited models
- 24 menus
- no tests
- no i18n

It works for one instance, simple products and draft quotations ([internal audit](../research_notes/Magento%20connector%20market%20gaps/our_module_audit.md)).

**Strengths.** Two are genuinely rare in the market:
- **Credit memos sync in both directions** (import as `out_refund`, export on post). Ventor lacks this, Webkul charges for it, Emipro supports only Odoo→Magento, and Jupical supports only Magento→Odoo.
- **A generic JSON-path field-mapping UI** for products, customers and categories in both directions, with "Load New Fields" read from the live API. Only Ventor's per-field mapping with Python transforms is comparable.

Product export from Odoo to Magento, which Emipro does not offer, is a third.

**The defects are serious enough that marketing the module harder before fixing them would hurt the brand.**

Defects that **lose or corrupt data**:
- Static crons are no-ops.
- Any configurable, bundle or grouped product **crashes the entire product sync**.
- Disabled Magento products create a duplicate Odoo product on every run.
- **Confirming an imported order breaks all later order syncs**, because confirmed lines get unlinked.
- Late payments are never registered.
- Shipment import always fails on the Odoo 19 rename from `product_uom` to `product_uom_id`.
- Configurable order items are duplicated (parent and child).
- Guest-cart export **silently rewrites Magento stock configuration**.

Defects in **architecture**:
- Every call is synchronous inside the user or cron transaction, with a 60-second timeout and no retry for 429/5xx errors.
- One failure rolls back the whole run.
- Upsert keys are not scoped by instance, and there are no unique `(instance_id, magento_id)` constraints.
- There is no `company_id` anywhere.
- Product edits in Odoo are **blocked when Magento is down**, because the push runs inside `write()`.
- Sync reports overstate success: manual runs always show errors = 0.

Defects in **security**:
- `base.group_user` has full CRUD on every model, so **any internal user can read the access token and the shared default customer password**.
- There are no record rules.
- PII is logged at INFO level.
- SSL verification is silently disabled for localhost.

**UX and code hygiene** add friction:
- The instance form hides required configuration.
- There are no smart buttons or chatter on custom models.
- 745 lines of dead `magento.order` code remain.
- **Shopify copy-paste leftovers run SQL on module load.**

Every critical bug maps to a market pain point:
- No pagination maps to missed orders.
- No tax or shipping lines map to total mismatches.
- Name matching maps to merged contacts.
- No stock push maps to oversell.
- The Bearer flag maps to post-upgrade 401s.

The gap to the leaders is therefore the same gap the whole market has with its customers. Closing it in the right order produces a better product than Emipro or Ventor, not merely a comparable one.

## Magento's API rules set the reliability architecture

Several platform facts constrain the design and should be built in rather than discovered in production.

**Pagination and incremental sync**
- Paginated REST is effectively capped at **300 items per page**.
- List inputs are capped at **20 items by default** when input limiting is on. A stock update with more than 20 array items failed on 2.4.3 ([Adobe KB](https://experienceleague.adobe.com/en/docs/commerce-knowledge-base/kb/troubleshooting/known-issues-patches-attached/web-api-resources-limit); [PyMagento #4](https://github.com/Bixoto/PyMagento/issues/4)).
- The safe pattern is an **`updated_at` high-watermark with an overlap window**, sorted by `updated_at` and then `entity_id`, using a page size of 100–300. This search syntax is standard Magento but was not re-verified against Adobe documentation in this research.

**Bulk writes**
- For mass writes, the **Async/Bulk REST API** returns a `bulk_uuid` immediately.
- Its status endpoints separate **code 2, "failed but retriable"**, from **code 3, "needs a change before retry"**. That maps directly onto a transient vs permanent error queue ([Adobe Bulk](https://developer.adobe.com/commerce/webapi/rest/use-rest/bulk-endpoints/); [Adobe operation status](https://developer.adobe.com/commerce/webapi/rest/use-rest/operation-status-endpoints/)).
- Store codes in the route determine scope: an update without a store code touches only the default store. Translations and website prices therefore need per-store-code calls.

**Authentication**
- Use an Integration with **OAuth 1.0a signing** on Magento Open Source and Adobe Commerce PaaS, rather than the Bearer flag.
- Admin tokens are unusable for unattended sync: they expire after 4 hours and require 2FA.
- **Adobe Commerce as a Cloud Service (SaaS)** uses IMS OAuth 2 server-to-server credentials. It has tenant-based URLs with no `/rest`, a different bulk URL layout, and a reduced endpoint set ([Adobe token auth](https://developer.adobe.com/commerce/webapi/get-started/authentication/gs-authentication-token); [ACCS auth](https://developer.adobe.com/commerce/services/cloud/guides/rest/authentication); [ACCS REST](https://developer.adobe.com/commerce/services/cloud/guides/rest)).
- No Odoo vendor's listing claims SaaS support.

**Real-time events**
- **Adobe Commerce Webhooks** (synchronous) and **I/O Events** (asynchronous) exist only on Adobe Commerce PaaS and SaaS. **Magento Open Source supports neither** ([Adobe Webhooks](https://developer.adobe.com/commerce/extensibility/webhooks/); [I/O Events](https://developer.adobe.com/commerce/extensibility/events/)).
- Real-time sync on Open Source therefore needs a small companion module that uses observers, a queue and a signed POST to Odoo. Polling must remain as the reconciliation fallback, which also closes the "missed event" gap.
- Ventor outsources this step to Mageplaza's paid extension, and Webkul to its own patch.

**Inventory**
- With MSI, push **`POST /V1/inventory/source-items`** per `(sku, source_code)` in chunks within the array limit.
- Never unassign sources, and never write salable quantity ([Adobe source items](https://developer.adobe.com/commerce/webapi/rest/inventory/manage-source-items/)).

**Order documents**
- Use the one-call endpoints: `/order/{id}/invoice`, `/order/{id}/ship` with `tracks[]`, and `/order/{id}/refund` ([Adobe invoice](https://developer.adobe.com/commerce/webapi/rest/tutorials/orders/order-create-invoice/); [Adobe shipment](https://developer.adobe.com/commerce/webapi/rest/tutorials/orders/order-create-shipment/)).
- These endpoints are not idempotent, so the connector must check whether a document already exists before posting it.

**B2B**
- The Adobe Commerce B2B REST API covers **shared catalogs** and **negotiable quotes** ([Adobe B2B](https://developer.adobe.com/commerce/webapi/rest/b2b/integrations); [negotiable quotes](https://developer.adobe.com/commerce/webapi/rest/b2b/negotiable-manage)).

**Target versions**
- Target **2.4.6–2.4.9**. 2.4.6 standard support ended in August 2026, and 2.4.9 runs to May 2029 ([Adobe lifecycle](https://experienceleague.adobe.com/en/docs/commerce-operations/release/planning/lifecycle-policy)).
- 429-aware backoff matters on Adobe Commerce Cloud, where Fastly returns rate-limit errors ([Adobe KB ka-27428](https://experienceleague.adobe.com/en/docs/experience-cloud-kcs/kbarticles/ka-27428)).

## Celigo and Business Central show what "well beyond" looks like

The operational benchmark comes from outside the Odoo ecosystem.

**Celigo's error management.** Celigo auto-resolves errors by a **trace key**, the record's unique ID, when the same record later succeeds. It auto-retries errors classified as intermittent up to four times. It offers bulk retry and resolve, and reports user-resolved vs auto-resolved counts ([Celigo resolve](https://docs.celigo.com/hc/en-us/articles/16182564553371-Resolve-errors-automatically-or-manually); [Celigo retries](https://docs.celigo.com/hc/en-us/articles/10972500798107)).

**Business Central's Shopify connector.** Microsoft's Dynamics 365 Business Central connector adds several things no Odoo–Magento listing advertises ([BC orders](https://learn.microsoft.com/en-us/dynamics365/business-central/shopify/synchronize-orders); [BC items](https://learn.microsoft.com/en-us/dynamics365/business-central/shopify/synchronize-items); [BC setup](https://learn.microsoft.com/en-us/dynamics365/business-central/shopify/get-started)):
- **Conflict views** for orders "updated in Shopify after they were processed", with an explicit table of what each edit type does.
- An explicit **system-of-record setting per entity**, plus a separate "Can Update" guard.
- **SKU matching strategies**: SKU field separator, barcode fallback, and bulk "Try Find Mappings".
- **Returns processing** to a credit memo or return order, with return-location priority and G/L accounts for refund differences.
- B2B company and location mapping.
- Onboarding that normalizes the pasted URL and handles token rotation.

Inside Odoo, the closest approximations are scattered:
- Ventor's queue and live mapping tests
- Miko's distinction between failed and needs-attention jobs
- ECOSIRE's dry-run preview

No single connector combines them. Building them natively into Odoo is the strongest available differentiator. It addresses the top three pain points directly, it is hard for a $195 entrant to copy, and it turns support load into product value.

## Prioritized phased roadmap

Effort: **S** ≈ under 1 developer-week, **M** ≈ 1–3 weeks, **L** ≈ more than 3 weeks. These are planning estimates for one experienced Odoo developer and have not been validated against a backlog. Evidence links point to the sources above. "Audit" means the [internal audit](../research_notes/Magento%20connector%20market%20gaps/our_module_audit.md).

### Phase 0: stop losing and leaking data (target ~3–5 weeks)

Nothing in later phases matters while the module can crash, duplicate, or expose credentials. Phase 0 also deletes dead code, so the later refactor works on a smaller surface.

| # | Item | User problem solved (evidence) | Competitors | Effort | Depends on |
|---|---|---|---|---|---|
| 0.1 | **Security model:** "Magento User/Manager" groups and ACLs; restrict tokens and default password to managers via field `groups`; record rules per instance/company; remove `sudo` from dashboard; stop INFO-level PII logging; never silently disable SSL | Any internal user can read the token and password (audit). Multi-company leakage is a known bug (Ventor v2.3.0 access fix) | PIT has role-based access; ECOSIRE has 29 record rules; most are silent | M | — |
| 0.2 | **Per-record isolation:** wrap each record in a savepoint with try/except; skip unsupported product types with a logged reason; commit in batches | One configurable product crashes the whole sync; one failure rolls back the run (audit, bugs 2 and 15) | All leaders isolate records | S | — |
| 0.3 | **Fix data-corruption bugs:** archived-product duplicates (`active_test=False`); never unlink lines of confirmed orders (update only in draft, otherwise flag a conflict); keep the invoice link so late payments register; `product_uom_id` in shipment import; filter configurable parent and child items | Duplicate products, broken order sync after confirm, missing payments, failed shipments (audit, bugs 3–7) | Fixed in all leaders | M | 0.2 |
| 0.4 | **Partner matching:** Magento `customer_id` → normalized email → **never name**; explicit guest handling; keep billing and shipping as child addresses, deduplicated | Merged strangers and dropped ship-to (audit, bug 13); Ventor's 2026 privacy fix | Ventor fixed it in 5+ releases; Webkul in 3 | S | — |
| 0.5 | **Decouple pushes from transactions:** move the push out of `write()` to post-commit or a queued call; stop mapping saves from pushing the whole catalog; remove the guest-cart stock-config rewrite | Odoo product edits fail when Magento is down; saving a mapping hangs the UI; Magento stock config silently changed (audit, bugs 10–12) | Ventor queues all pushes | M | 1.1 for the full fix (interim: `postcommit` hook) |
| 0.6 | **Make crons real:** fix static no-op crons; handle timezone and monthly-minute settings; status update via the comments endpoint instead of `PUT /V1/orders/{id}` | Scheduled sync silently does nothing (audit, bugs 1, 9, 14) | — | S | — |
| 0.7 | **Instance-scoped keys:** unique `(instance_id, magento_id)` constraints on every binding; add `company_id`; migration script to deduplicate | Double import and duplicate keys (OCA #165, #137); wrong upserts across instances (audit) | Leaders all support multi-instance | M | 0.2 |
| 0.8 | **Honest reporting and hygiene:** report real error counts; remove Shopify leftovers that run SQL on load, dead `magento.order*` code, and the unused wizard; add `@api.model_create_multi`; add a first regression test suite | "Errors=0" erodes trust; SQL on load is a security and upgrade risk (audit) | — | M | 0.2 |

### Phase 1: the reliability foundation (target ~6–9 weeks)

This phase is the core of the "never silently loses an order" promise and the main differentiator against Odoo IN, Webkul and Emipro.

| # | Item | User problem solved (evidence) | Competitors | Effort | Depends on |
|---|---|---|---|---|---|
| 1.1 | **Job queue** (own lightweight `magento.job` model with cron workers, channels, locks and identity keys; avoid a hard OCA dependency, as Ventor did for 19) | Timeouts, blocked UI, double processing (audit; OCA #165; [Ventor 19 migration](https://ecosystem.ventor.tech/faq/e-commerce-connectors/common-questions/migrating-ventortech-e-commerce-connectors-to-odoo-19-0/)) | Ventor, Emipro, Miko have queues; Webkul and Odoo IN are cron only | L | Phase 0 |
| 1.2 | **Error center** (one log row per instance + entity + Magento ID; trace-key auto-resolve; transient vs permanent classification; stored payload for replay; bulk retry and resolve; chatter link to the Odoo record) | Silent failures; hunting with "not synced" filters ([Webkul](https://store.webkul.com/Odoo-Bridge-For-Magento2.html); [Celigo](https://docs.celigo.com/hc/en-us/articles/16182564553371-Resolve-errors-automatically-or-manually)) | Ventor logs every action; Miko splits failed vs needs attention; **no Odoo vendor auto-resolves by trace key** | L | 1.1 |
| 1.3 | **Retry and backoff:** exponential backoff on 429/5xx honoring `Retry-After`; adaptive concurrency; surface real auth errors instead of the store-prefix fallback chain | Fastly 429s on Cloud ([Adobe KB](https://experienceleague.adobe.com/en/docs/experience-cloud-kcs/kbarticles/ka-27428)); hidden 401s (audit) | Ventor, ECOSIRE | S | 1.1 |
| 1.4 | **Incremental, gap-free sync:** `updated_at` watermark with overlap; sort by `updated_at` then `entity_id`; page size ≤ 300 (configurable); idempotent upserts; never use a creation-date cut-off | Skipped orders across pages ([Ventor Aug 2026](https://apps.odoo.com/apps/modules/17.0/integration_magento2); [Mintsoft](https://help-mintsoft.theaccessgroup.com/en/articles/11679224-magento-orders-stopped-importing)) | Ventor fixed this only in 2026; Techsnas has per-entity cut-off dates | M | 0.7, 1.1 |
| 1.5 | **OAuth 1.0a signing** (HMAC-SHA256) as the default, with Bearer as a legacy fallback; setup diagnostics that name missing ACL resources | 2.4.4+ 401s and an insecure flag ([Adobe](https://developer.adobe.com/commerce/webapi/get-started/authentication/gs-authentication-token)) | **Only Odoo IN**; Ventor and ours need the flag | M | — |
| 1.6 | **Reconciliation job:** compare Magento vs Odoo order counts and totals per day and website; flag gaps; one-click re-import of missing orders | Missed orders found only through customer complaints (pain-point synthesis) | None advertise it | M | 1.2, 1.4 |
| 1.7 | **Idempotent document posting:** check existing invoice, shipment or credit memo before POST; store the Magento document IDs | Double invoices and double shipping ([Miko](https://apps.odoo.com/apps/modules/19.0/miko_magento)); non-idempotent endpoints | Miko only | S | 0.7 |
| 1.8 | **Bounded logs and alerts:** retention policy with auto-cleanup; Odoo activity, email or Discuss alert on permanent failures and auth loss | Logs filling 90% of disk ([Emipro review](https://apps.odoo.com/apps/modules/19.0/odoo_magento2_ept)) | Ventor has auto-cleanup and email | S | 1.2 |
| 1.9 | **Bulk/Async transport** for mass stock, price and product writes, polling `detailed-status` and mapping codes 2 and 3 into the error center | Timeouts on large catalogs; >20-item array failures ([Adobe Bulk](https://developer.adobe.com/commerce/webapi/rest/use-rest/bulk-endpoints/)) | None advertise it | M | 1.1, 1.2 |

### Phase 2: parity with Emipro and VentorTech (target ~10–14 weeks)

Phase 2 removes every reason a buyer would rule us out on a feature checklist.

| # | Item | User problem solved (evidence) | Competitors | Effort | Depends on |
|---|---|---|---|---|---|
| 2.1 | **Configurable products ↔ `product.template` + variants** (attribute and option mapping, creating options before children, URL-key collision check) | Variant mapping is a decade-long bug class ([Ventor](https://apps.odoo.com/apps/modules/17.0/integration_magento2); [Webkul](https://store.webkul.com/Odoo-Bridge-For-Magento2.html)) | Emipro, Ventor, Webkul yes; Odoo IN flattens variants | L | 0.2, 1.1 |
| 2.2 | **Bundle and grouped import** as kits (BoM) or decomposed lines; bundle order lines with correct parent/child totals | Bundle orders failing on total mismatch (Ventor v2.1.6; OCA #282) | Ventor (orders only) | M | 2.1 |
| 2.3 | **Categories linked to `product.category`/public categories; customers linked to `res.partner`** with all addresses, customer groups and VAT/company detection | Our data sits in shadow models (audit); Ventor reviewers praise "contact recognition" | Ventor, Emipro | M | 0.4 |
| 2.4 | **Order fidelity:** shipping, discount, coupon and FPT lines; per-line tax via `applied_taxes`; tax-class → `account.tax` mapping; payment method → journal; carrier mapping; configurable rounding mode; **residual difference shown as a visible flag, not a hidden plug line** | Total mismatches ([Ventor FAQ](https://ecosystem.ventor.tech/faq/e-commerce-connectors/common-questions/the-total-amount-in-the-sales-order-differs-from-the-calculated-amount-in-odoo-why-it-happens-and-how-to-fix-it-2/); OCA #170) | All leaders import totals; Ventor uses a plug line | L | 2.3 |
| 2.5 | **Auto-workflow per Magento status and payment method:** confirm, invoice, post, register payment, validate delivery | Our orders stay draft forever and Delivered is always 0 (audit) | Ventor 7-step; Emipro financial-status workflow | M | 2.4, 1.1 |
| 2.6 | **Stock push** from on-hand, free or forecast quantity, per warehouse; **MSI `source-items`** mapping with a legacy fallback; kit (BoM) availability; backorder respect; event-driven push plus nightly full reconcile | Oversell ([Ventor](https://apps.odoo.com/apps/modules/17.0/integration_magento2); OCA #268); we push no stock (audit) | Ventor, Emipro; Webkul MSI is a paid add-on | L | 1.9 |
| 2.7 | **Shipments with tracking**, both ways, including partial and MSI `source_code`; cancel and hold sync | Ours exports without tracking and import fails (audit) | Emipro, Ventor, Webkul, Odoo IN | M | 1.7 |
| 2.8 | **Multi-website and store view:** per-store-view translations, prices to website store codes, all images (media gallery) | Update without store code touches only the default store ([Adobe](https://developer.adobe.com/commerce/webapi/rest/use-rest/bulk-endpoints/)); first image only (audit) | Ventor translations; Webkul multi-store is paid | M | 2.1 |
| 2.9 | **Pricelist and special-price export** per website or customer group | Prices diverge between channels | Ventor yes; Webkul paid; Emipro unclear | M | 2.8 |
| 2.10 | **Onboarding wizard:** URL normalization, OAuth test, discovery of websites and store views, auto-proposed payment, carrier and tax mappings, pre-go-live SKU and barcode validation, known-issues checklist | "Nothing works at first" ([Webkul reviews](https://store.webkul.com/Odoo-Bridge-For-Magento2.html)); hidden required config (audit) | Ventor setup wizard; Emipro onboarding panel | M | 1.5, U3 |

### Phase 3: market-first differentiators (ongoing, prioritized)

Phase 3 items are ordered by how closely they track evidenced pain and how few competitors offer them.

| # | Item | User problem solved (evidence) | Competitors | Effort | Depends on |
|---|---|---|---|---|---|
| 3.1 | **Totals reconciliation engine:** per-order explanation of any Odoo vs Magento difference (rounding, tax mode, FPT, discount tax compensation), with suggested fixes, instead of a plug line | The most structural accounting pain ([Ventor FAQ](https://ecosystem.ventor.tech/faq/e-commerce-connectors/common-questions/the-total-amount-in-the-sales-order-differs-from-the-calculated-amount-in-odoo-why-it-happens-and-how-to-fix-it-2/)) | **None**; Ventor plugs, Emipro says "mirror the config" | L | 2.4, 1.6 |
| 3.2 | **Full returns lifecycle:** keep credit memos in both directions and add return-location priority, restock vs non-restock, refund-difference accounts, online vs offline refund choice | Credit memos are the clearest functional gap (Ventor "Coming soon") | **No vendor has both directions plus RMA**; BC has the return-location model | M | 1.7, 2.6 |
| 3.3 | **Free signed Magento companion module** (Open Source and PaaS observers → queue → HMAC POST to an Odoo controller) plus optional **Adobe I/O Events** ingress; polling stays as fallback | Real-time sync requires a paid third-party extension or a patch ([Ventor](https://apps.odoo.com/apps/modules/19.0/integration_magento2); [Webkul](https://webkul.com/blog/odoo-multichannel-magento2x-connector/amp/)); Open Source has no native webhooks | No vendor ships a free, first-party, optional one | L | 1.1, 1.4 |
| 3.4 | **Tier and customer-group prices** (`/V1/products/tier-prices`) mapped from pricelist rules with minimum quantity | B2B and wholesale merchants; no vendor advertises it (market synthesis) | **None** | M | 2.9 |
| 3.5 | **Bundle and grouped product export** from Odoo kits and product packs | Merchants manage kits in Odoo but must rebuild them in Magento | **None** advertise export | M | 2.2 |
| 3.6 | **Adobe Commerce B2B write-back:** company ↔ partner hierarchy, shared catalog ↔ pricelist, negotiable quote ↔ quotation (two-way price negotiation), payment terms | B2B data stranded ([Adobe B2B](https://developer.adobe.com/commerce/webapi/rest/b2b/integrations)) | ECOSIRE read-only; BC has the B2B model | L | 3.4, 2.3 |
| 3.7 | **Conflict detection:** orders edited in Magento after Odoo processing go to a "Conflicts" view, with defined behaviour per edit type | Silent divergence; ignored rapid status changes (Ventor v1.13.2) | **None** in Odoo; BC benchmark | M | 1.4, 1.2 |
| 3.8 | **Dry-run preview and per-entity system of record:** preview creates and updates before applying; 1-way/2-way/Off plus a "can update" guard per entity; field mapping with sandboxed transform expressions and live test (extends our existing mapping UI) | Accidental overwrite; risky first sync | ECOSIRE dry-run; Jupical toggles; Ventor transforms; none combine them | M | 1.1 |
| 3.9 | **Adobe Commerce as a Cloud Service support** (IMS OAuth 2, tenant URLs, SaaS endpoint gaps) | Adobe's SaaS line; no Odoo vendor claims it ([ACCS](https://developer.adobe.com/commerce/services/cloud/guides/rest)) | **None** found | M | 1.5, 1.9 |
| 3.10 | **Operations insights:** daily digest, net-profit view (fees, refunds), settings export/import between instances | Retention and upsell; Emipro sells digests and net profit | Emipro, Ventor (settings export) | S–M | U2 |

### UI track: the TRS house style (`odoo-trs-ui`), applied to the connector (runs in parallel from Phase 0)

All UI work follows the SDLC house conventions in the `odoo-trs-ui` skill, taken from TRS Retail (`retail_theme`, `retail_dashboard`, `retail_core`; reference code `custom_addons/Inventory-dev`, branch `dev`). The aim is for the connector to look like part of the same product family as TRS, not a separately branded app. The connector already uses the same plumbing (registry actions, `orm.call`), so the work is templates, styles and charts ([TRS reference](../research_notes/Magento%20connector%20market%20gaps/trs_ui_reference.md)).

**House rules the connector must follow**
- **Theming happens in the theme module, at the SCSS variable layer.** The connector is a *feature* module:
  - It ships no backend-wide theme and never restyles Odoo `o_` classes.
  - Stock lists, forms and dialogs get the TRS look when `retail_theme` (or a shared SDLC theme module) is installed. That module injects `variables.scss` `('before', 'web/static/src/scss/primary_variables.scss', …)` into `web._assets_primary_variables`.
  - The connector does **not** depend on it, so it still installs cleanly into customers' Odoo.
  - It never copies `retail_theme`'s hook that hides other apps.
- **Shared TRS tokens with fallbacks.** Components use the TRS tokens with fallback values: `var(--trs-crimson, #c8103a)`, `var(--trs-line, #e3d8cc)`, `var(--trs-ink-dim, #6b5f55)`, and so on.
  - They match exactly when the theme is present and still look like TRS without it.
  - **No Magento-orange accent and no separate dark mode**, which drops the current `--md-*` orange-gradient system.
- **Status colours are separate from the brand colour:** `--ok #067647`, `--warn #b54708`, `--crit #b42318` (a different red from brand crimson).
- **Type:**
  - Display serif (`Rockwell, "Roboto Slab"`) for titles and record names; the UI sans for everything else.
  - Body 13.5/1.5. Micro-labels 11px, uppercase, letter-spacing .06–.09em. Figures 26px, weight 680.
  - `tabular-nums` on every compared number: KPI values, totals and list numerics.
- **State styling:**
  - Use inset `box-shadow`, never borders, so boxes never change size.
  - Use `rgba` overlays rather than hard-coded tints; the selected overlay (.18) is stronger than hover (.12).
  - Signal state on more than one channel: tint, line and weight.
  - Optical padding.
  - A missing logo falls back to a coloured dot.
  - Selected chips use the ink colour, not the brand colour.
- **Elevation:** warm shadows `0 1px 2px rgba(60,40,20,.04), 0 10px 24px -14px rgba(60,40,20,.18)`. Radii are 8px for controls, 10–12px for cards and 999px for pills.
- **Accessibility built in from the start:**
  - `:focus-visible` crimson outline on every clickable element.
  - `prefers-reduced-motion` turns off every transition.
  - Toggle chips get `aria-pressed`; chip groups get `role="group"` and `aria-label`.
  - Clickable rows get `tabindex` and an Enter-key handler.
- **Naming:** a module prefix with BEM, `mg-` (e.g. `mg-rail__cell--crit`, `mg-chip--active`). Never collide with `o_` classes.
- **Images and artwork:** stored as data records, not asset bundles. For example, an optional `logo` image on `magento.instance`/website records, served by `/web/image`, so admins can change it without a deploy.
- **Every figure opens its records.** Each KPI and table cell opens the matching records through `action.doAction`, carrying the filters currently on screen. Clickable cells get `cursor: pointer`, a hover tint and `:focus-visible`.
- **Use standard Odoo views first.** Build a custom OWL component only where a standard view cannot express the screen. That applies to the Console alone.

**What each screen does decides its layout** (from the house table):

| Connector screen | What the user does there | Layout | Built with |
|---|---|---|---|
| Magento Console (dashboard) | Watch state, spot trouble | **KPI rail plus panels, every figure opens its records** | Custom OWL (the only one) |
| Instance setup and connection | Move a record through stages | **Form with statusbar** (Draft → Connected → Live), grouped sections, notebook tabs, smart buttons, `<chatter/>` | Standard form |
| Error Center / sync jobs | Validate staged input | **Per-row errors; one bad row never sinks the batch.** Bulk Retry/Resolve, payload viewer, badge decorations | Standard list + form |
| Order and totals reconciliation (1.6, 3.1) | Reconcile figures | **Dense table**, tabular nums, section subtotals per instance and currency | Standard list (grouped, `sum=`) |
| Product bindings and catalog | Browse a catalogue | **Kanban, image-led**, chips for type, website and sync state | Standard kanban |
| Onboarding (2.10) | Validate staged input | **Stepper** with a per-step checklist; failures shown on the step, not as a traceback | Standard wizard form |

**The Magento Console follows the TRS Store Console structure, top to bottom:**
1. Masthead: display-serif title, per-instance connection dot and last sync, Refresh.
2. Permission-aware grouped nav pills (`get_nav_links` / `_user_can_open` pattern).
3. Instance and website filter chips (logo or coloured dot, plus count; toggle with `aria-pressed`).
4. Error, loading and warning notices.
5. A one-row **KPI rail**: orders imported, failed jobs (crit), unreconciled orders (warn), stock pushes pending, conflicts. Warning and critical cells are clickable.
6. A two-column panel grid:
   - a "Needs action" list with pills
   - a Chart.js revenue/orders trend (lazy `loadBundle("web.chartjs_lib")`, colours read from CSS tokens), converted to company currency
   - top products, categories and countries as bar lists
7. A per-block empty state, and a one-column layout below 900px.

It loads **once, with a Refresh button, and no polling**.

| # | Item | User problem solved (evidence) | Competitors | Effort | Depends on |
|---|---|---|---|---|---|
| U1 | **House-style foundation:** `mg-` component SCSS that uses TRS tokens with fallbacks and separate status tokens; tabular-nums; focus-visible and reduced-motion; remove the `--md-*` orange system and the action-manager inline-style hack; no `o_` overrides and no global SCSS in the connector | The current dashboard is a separately branded, orange screen bolted onto stock Odoo (TRS reference, audit) | — | S | — |
| U2 | **Magento Console** built to the structure above. Fix the en-IN locale and Asia/Kolkata timezone, the always-empty Top Countries panel, the unconverted multi-currency sums and the deprecated `read_group`; remove the 30s polling | No click-through to records; wrong or empty KPIs (audit) | Ventor, Emipro, Jupical, ECOSIRE have dashboards; **none advertise clicking from every KPI into an error center** | M | U1; 1.2 for the error KPIs |
| U3 | **Instance form:** statusbar, smart buttons (orders, products, customers, failed jobs, conflicts), connection-test badge, notebook exposing all hidden required config, `<chatter/>` | Hidden config; no status shown (audit) | Standard in Ventor and Emipro | S | 0.1 |
| U4 | **Error Center:** standard list grouped by entity and error class, badge decorations (status tokens), bulk Retry/Resolve, payload viewer, links to the Odoo and Magento record | Diagnostics hidden behind debug mode (Ventor Consistency Wizard) | Ventor partial; Miko job dashboard | M | 1.2 |
| U5 | **Standard-view hygiene:** search views and filters for all models, optional columns, `sum=` footers, `nocontent` help, product bindings as image kanban, reconciliation as grouped dense list; menus consolidated from 24 into Console / Operations / Catalog / Configuration / Logs; SVG `web_icon` on the root menu | Only 2 search views; menu sprawl; no app icon (audit) | — | S | — |
| U6 | **Onboarding stepper** for wizard 2.10, with empty-state guidance on the Console until the first sync succeeds | Setup friction, an "intensive journey" ([Webkul reviews](https://store.webkul.com/Odoo-Bridge-For-Magento2.html)) | Emipro onboarding panel; Ventor wizard | M | U1, 1.5 |
| U7 | **Accessibility and navigation pass:** ARIA roles, keyboard handlers, `aria-pressed`, nav pills only for actions the user can open. Optional: reuse `retail_theme`'s `nav_active.js` service to mark the current Magento menu section | Our dashboard has no accessibility support (TRS reference) | None advertise it | S | U2, 0.1 |

The sequencing logic holds even if the estimates slip:
- Phase 0 and U1/U3/U5 can ship within a month as a "stability release".
- Phase 1 together with U2/U4 is the release that earns the positioning claim.
- Phase 2 makes the product comparable to the leaders feature by feature.
- Phase 3 should be pulled forward selectively. **3.2 (returns) and 3.5/3.4 (bundle export, tier prices)** build on code we already partly have and address gaps every competitor leaves open.

## Positioning on trust and a fair upgrade path, not price

**Pricing.** The market has settled at two price levels:
- **About $500–$625 one-time** (Emipro, Ventor, ECOSIRE), or **€499/yr** for Ventor's subscription.
- **About $195–$250** for narrower products (Odoo IN, Webkul base, Globalteckz, Jupical).

Sources: [Emipro](https://apps.odoo.com/apps/modules/19.0/odoo_magento2_ept); [Ventor store](https://ecosystem.ventor.tech/product/odoo-magento-2-connector-pro/); [Webkul store](https://store.webkul.com/Odoo-Bridge-For-Magento2.html); [Odoo IN](https://apps.odoo.com/apps/modules/19.0/ecommerce_magento).

The premium is paid for support, not software. Webkul's real price climbs well above $249 once installation, support and add-ons are counted. Undercutting on price alone would put us against Odoo IN's "built by Odoo" brand at $195, which is a poor fight.

**Recommendation: open core with a different promise.**
- Keep the **free Community edition** on apps.odoo.com. It already has 42 counts and a place in a "top 5" list ([Webkul blog](https://webkul.com/blog/best-odoo-magento-2-connectors/amp/)). It should cover Phase 0 and the core of Phase 1: per-record isolation, OAuth, incremental import and a basic log. A free connector that does not lose orders is itself a marketing asset.
- Offer a **Pro edition in the $450–$550 band**, or about €399/yr. It would contain:
  - the error center with auto-retry and auto-resolve
  - reconciliation, and the totals engine
  - MSI and bulk transport
  - auto-workflow
  - returns
  - tier, B2B and ACCS support
  - the free companion module for real-time sync

**Commercial levers.** The cheapest differentiators to copy are commercial, and they answer evidenced complaints about per-version repurchase, 90-day support and stranded old versions ([Ventor](https://apps.odoo.com/apps/modules/17.0/integration_magento2); [Emipro 14.0](https://apps.odoo.com/apps/modules/14.0/odoo_magento2_ept)):
- **Include the next Odoo major-version port** in an active subscription.
- **Publish a public changelog and status page.**
- **Never charge for core entities**: credit memos, multi-store and MSI. This is a direct contrast with Webkul's add-on model.
- **Ship the Magento companion module free and optional**, unlike Ventor's reliance on a paid Mageplaza extension.
- **A paid onboarding package** is still sensible; Ventor charges €449.10. With good self-serve onboarding, it becomes optional rather than necessary.

**The case for competing on breadth.** The strongest counter-argument is that buyers choose on breadth of features, and that Emipro and Ventor have a ten-year head start. That is partly true. Phase 2 cannot be skipped, and until it ships the Pro tier should not be priced at parity. But the changelog evidence shows the leaders still fixing missed orders, merged contacts and stock edge cases in 2026. They have breadth without trustworthiness, and reliability built as a first-class product surface is where a later entrant can win.

**What remains uncertain:**
- Real install counts: apps.odoo.com does not label its numbers.
- Whether Emipro's webhooks need a Magento module.
- The actual demand for Adobe B2B and SaaS support among Odoo merchants. This should be validated with 5–10 prospect interviews before committing L-sized effort to items 3.6 and 3.9.

## Conclusion

The research changes the framing of "going beyond the competition". The leaders do not mainly win on features. They win on vendor support that compensates for software that loses orders, mis-totals invoices and merges customers, failures that recur in their own changelogs from 2016 to 2026. Our module currently has every one of those failure modes plus a security hole. But it is small enough to re-found correctly, and it already holds the two capabilities hardest to bolt on later: two-way credit memos and a generic mapping engine. A connector whose dashboard opens on "0 missed orders, 0 unexplained total differences, 3 items need action", with every number one click from its cause, is a product category the Odoo–Magento market does not yet have.

The practical implication is to spend the next quarter almost entirely on Phase 0, Phase 1 and the scoped UI track before adding features. Reliability is the moat, because it requires architectural decisions competitors cannot retrofit cheaply: instance-scoped bindings, watermarks, trace-key error records, idempotent document posting and OAuth signing. Features are the follow-on, and the white-space items (tier prices, bundle export, returns, B2B write-back, free real-time sync) should be chosen by prospect demand rather than built all at once.
