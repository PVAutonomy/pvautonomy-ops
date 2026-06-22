"""Vendored third-party pure-Python dependencies for pvautonomy_ops.

Vendored here (instead of declared as a Home Assistant ``manifest.json``
requirement) when an upstream package pins a transitive constraint that
conflicts with the version Home Assistant Core already ships.

Currently vendored:

* ``pyhpke`` 0.6.4 (MIT, (c) 2022 Ajitomi Daisuke) — pure-Python RFC 9180
  HPKE. Its published metadata pins ``cryptography<47``, which cannot be
  resolved against HA Core's bundled ``cryptography==48`` (uv "No solution
  found"). The cap is defensive only: the library runs unmodified on
  cryptography 48. Vendoring drops the unsatisfiable requirement while the
  code keeps using HA Core's cryptography. Revert to a PyPI requirement once
  upstream publishes a cryptography-48-compatible release. See issue #94.
"""
