from gsuid_core.sv import Plugins

Plugins(
    name="ChisaEating",
    force_prefix=[],
    allow_empty_prefix=True,
)

from . import chisaeating_config  # noqa: F401,E402
from . import chisaeating_help  # noqa: F401,E402
from . import chisaeating_main  # noqa: F401,E402
from . import chisaeating_assets  # noqa: F401,E402
