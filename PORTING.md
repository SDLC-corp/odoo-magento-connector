# Version porting runbook

The `19.0` branch is the functional reference. Port one Odoo major version at a time and
keep every runtime isolated. Do not point two Odoo versions at the same database and do
not upgrade a port database in place to another Odoo major version.

## Isolation matrix

| Odoo | Git branch | Database | HTTP port | Suggested config |
|---|---|---|---:|---|
| 15 | `15.0` | `magento_port_15` | 8515 | `odoo-magento-15.conf` |
| 16 | `16.0` | `magento_port_16` | 8516 | `odoo-magento-16.conf` |
| 17 | `17.0` | `magento_port_17` | 8517 | `odoo-magento-17.conf` |
| 18 | `18.0` | `magento_port_18` | 8518 | `odoo-magento-18.conf` |
| 19 | `19.0` | `magento_port_19` | 8519 | `odoo-magento-19.conf` |
| 20 | `20.0` | `magento_port_20` | 8520 | `odoo-magento-20.conf` |

Each config must use the matching Odoo source/addons paths, an exact database filter such
as `^magento_port_17$`, and `list_db = False` during browser verification. Use a separate
Python virtual environment per Odoo major version.

## Port order

1. Freeze and tag the verified `19.0` baseline.
2. Port backwards in API-distance order: 18, 17, 16, then 15.
3. Port 20 independently from the frozen 19 baseline; do not derive it from a backwards port.
4. Keep the technical module name `sdlc_magento_connector` and marketplace name
   `Magento 2 Connector` identical on every branch.
5. Change the manifest version prefix, migration directory and marketplace Odoo badge for
   the target version. Preserve feature wording only where the target branch implements it.

## Required gate for every branch

- Parse every XML file and byte-compile every Python file.
- Install from scratch in that version's empty database.
- Run all tests tagged `magento` with `--stop-after-init`.
- Upgrade the module in a second, disposable database containing representative records.
- Start the isolated HTTP service and verify login, Console, Instances, Orders, Products,
  Job Queue, Error Center and permissions for User, Manager and Administrator roles.
- Exercise a mocked Magento REST flow and, separately, a staging Magento instance for the
  supported authentication modes and enabled features.
- Capture screenshots from the target version itself; never reuse screenshots from another
  Odoo major version.
- Run `git diff --check` and confirm the package contains no secrets, generated logs,
  filestore data or database dumps.

## Release evidence

For each branch, retain the exact test command, database name, Odoo commit, connector
commit, test summary and UI checklist result in the release notes. Delete or archive the
disposable database only after the evidence and screenshots have been reviewed.
