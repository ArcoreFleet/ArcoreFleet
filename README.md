# ArcoreFleet

ArcoreFleet provides an application portal, per-user access controls, infrastructure templates and an MCP gateway for a self-hosted application fleet.

This public repository is a fresh source snapshot. It contains no private repository history, customer databases, deployment inventories, credentials or automated deployment access to the operator's fleet. Network addresses and domains in templates are examples and must be configured for your environment.

## Portal development

Use Python 3.12 (Django 5.0 is not compatible with Python 3.14).

```sh
python -m venv .venv
# Activate the environment for your shell.
pip install -r portal/requirements.txt
python portal/manage.py migrate
python portal/manage.py createsuperuser
python portal/manage.py runserver
python portal/manage.py test access website hubauthority --noinput
```

## ArcoreHub provisioning

Every signed-in user has an ArcoreHub tile. Customers see only their active app assignments and can download the signed authority profile. Active administrators can register apps, assign them to customers, and revoke assignments at `/hub/provisioning/`. Changes are recorded in an audit history. Configuration publication remains admin-only.

These portal assignments do not install or authorize executable packages. The current Hub preview has not implemented production account enrollment, signed package installation or per-launch authorization. The downloaded config contains only public server trust settings and grants no app access by itself.

See `portal/README.md`, `portal/WEBSITE.md`, `fleet-mcp/README.md` and the phase directories for component setup. Configure your own identity provider, signing material and infrastructure; never commit secrets.

## License

Copyright (c) 2026 Arecore LLC.

This project is source-available under the [Elastic License 2.0 (ELv2)](LICENSE). The infrastructure templates in `phase1-*` through `phase5-*` are also available, at your option, under the Apache License 2.0 (`LICENSE-APACHE` in each of those directories).

- **Allowed:** deploy, modify and run ArcoreFleet for your own business or personal use, commercial or not, at no cost, and redistribute it under the same license with its notices intact.
- **Not allowed:** offering the software to third parties as a hosted or managed service, circumventing license key functionality, or removing licensing and copyright notices.

ELv2 is not an OSI-approved open-source license. See [LICENSING.md](LICENSING.md) for a summary; the full [LICENSE](LICENSE) governs. For a commercial license, contact Arecore LLC.
