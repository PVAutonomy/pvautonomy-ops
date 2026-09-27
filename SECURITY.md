# Security policy

PVAutonomy is an experimental Community Alpha, maintained by one person. It
makes **no security, conformity or certification claim**: the internal
security self-assessment `SEC-GATE-01` is **BLOCKED**, and **no security
assurance is given**. See [Status](README.md#status) in the README.

## Reporting a vulnerability

Please report a security problem **privately** through GitHub: open the
**Security** tab of this repository and choose **Report a vulnerability**, or
use [this link](https://github.com/PVAutonomy/pvautonomy-ops/security/advisories/new).
You need a GitHub account.

Please do not report security problems in public issues, pull requests or
discussions.

A helpful report names the affected version, the steps to reproduce the
problem, and what an attacker could achieve. Do not send real passwords, keys
or other secrets; describe them instead.

## What to expect

These are targets, not guarantees:

- an acknowledgement of your report within **14 days**;
- after that, a status update at least every **30 days** until the report is
  resolved.

When a fix is released, the report may be published as a GitHub security
advisory. Say in your report whether you want to be credited.

## Scope

In scope:

- the `pvautonomy_ops` integration in this repository, with its bundled
  firmware definitions (inverter registry, schema, production base);
- the ESPHome device configuration that the integration generates;
- the documentation in this repository.

Reports are handled for the latest release. A fix is published as a new
release; earlier releases are not updated.

Out of scope:

- Home Assistant, HACS, ESPHome and the firmware of Growatt inverters; please
  report problems there to those projects or vendors;
- the security of your own network and Home Assistant installation;
- the managed build service, the hosted proxy, build keys, the onboarding
  image and the installer add-on of earlier releases, which are not part of
  the Community Alpha.
