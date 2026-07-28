"""Policy for the sibling tools evalctl integrates with.

Single home for the versions and contracts evalctl requires of optional
integrations. These values were previously literals scattered across the
compatibility gate, three error hints, the capabilities payload, and the robot
docs; they drifted apart, and evalctl advertised a spoolctl minimum that no
longer matched the one it enforced.

Nothing here describes evalctl's own contract surface. That lives in
static_contract.py, which imports from this module rather than the reverse.
"""

from __future__ import annotations

MINIMUM_SPOOLCTL_VERSION = "0.4.11"
MINIMUM_SPOOLCTL_CONTRACT = 2

SPOOLCTL_UPGRADE_HINT = (
    f"install spoolctl >= {MINIMUM_SPOOLCTL_VERSION} "
    f"(contract >= {MINIMUM_SPOOLCTL_CONTRACT}) or drop --queue spoolctl"
)
