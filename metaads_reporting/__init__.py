"""Meta Marketing API reporting and campaign management.

This package is read-only by construction: the only HTTP methods it sends
outside :mod:`metaads_reporting.write` are GET and HEAD, which on the Graph API
cannot change anything. See :mod:`metaads_reporting.client` for how that is
enforced and :mod:`metaads_reporting.graph` for where the check lives.

Everything that can change an account lives in
:mod:`metaads_reporting.write`, deliberately behind a separate import.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
