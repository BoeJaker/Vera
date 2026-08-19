# 37 · Business and Commerce

The business subsystem models products, individually tracked units, inventory,
orders, customers, services, marketplaces, sourcing, shipping, accounts,
transactions, tax, and operational work. It is an application domain built on
Vera's capabilities, fabric, integrations, media, agents, and schedulers.

## Core records

| Record | Meaning |
|---|---|
| Product | Reusable catalog identity and descriptive facts |
| Unit | One physical item with condition, location, cost, and status |
| Inventory movement | Auditable change in quantity/location |
| Listing | Channel-specific offer derived from product/unit data |
| Order | Commercial transaction and fulfilment state |
| Account/transaction | Cash or ledger movement used for reporting |
| Task/campaign/watch | Planned operational or market-monitoring work |

Avoid collapsing products and units: two copies of a product may have different
condition, acquisition cost, location, and listing status. Inventory should be
changed through movement/adjustment operations so the reason remains auditable.

## Sell-through lifecycle

1. Intake or identify a unit.
2. Resolve/create its product identity.
3. Capture condition, images, cost, and storage location.
4. Enrich and research comparable prices.
5. Draft and review a marketplace listing.
6. Publish through an authorized platform account.
7. Sync orders, book shipping, and update unit/order status.
8. Record fees, revenue, cost, profit, and tax classification.

AI enrichment and price suggestions are proposals. Human review remains
necessary for identity, condition, legal claims, price, tax, and publication.

## Integrations and side effects

Marketplace publishing, order synchronization, label booking, customer contact,
and financial records are external or durable mutations. Use idempotency keys
where supported and retain provider IDs. On retry, query current remote state
before creating a second listing, shipment, or transaction.

## Troubleshooting and reconciliation

Reconcile from stable IDs: store/account, platform listing/order, internal
product/unit/order, and shipment. Common problems are duplicate products,
stale stock after an external sale, incorrect fee/tax assumptions, image rights,
and a unit listed on multiple channels after it is unavailable.

Business rules live under `vera/business/` and `vera/commerce/`; external
accounts use [Integrations](23-integrations.md), generated media uses
[Render and media](28-render.md), and agent behavior follows
[Agents and chat](19-agents-chat.md).

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
