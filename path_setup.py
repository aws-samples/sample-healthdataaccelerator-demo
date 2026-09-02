"""
path_setup.py - Configure Python path for submodule imports.

Import this module at the top of CDK entry points before importing
any submodule packages. It validates that both submodules are initialized
and adds the openemr-on-ecs submodule to sys.path so its packages are
importable.

The parent repo's infrastructure/ package takes priority over the submodule's
openemr_ecs package because the parent directory is already on sys.path
before this module inserts the submodule path (overlay pattern).
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
_OPENEMR_SUBMODULE = os.path.join(_ROOT, "submodules", "openemr-on-ecs")
_ACCELERATOR_SUBMODULE = os.path.join(
    _ROOT, "submodules", "modern-data-architecture-accelerator"
)

# Validate that both submodule directories exist and are non-empty
for _submod_path, _name in [
    (_OPENEMR_SUBMODULE, "openemr-on-ecs"),
    (_ACCELERATOR_SUBMODULE, "modern-data-architecture-accelerator"),
]:
    if not os.path.isdir(_submod_path) or not os.listdir(_submod_path):
        raise RuntimeError(
            f"Submodule '{_name}' not initialized at: {_submod_path}\n"
            "Run: git submodule update --init --recursive\n"
            "Or clone with: git clone --recurse-submodules <repo-url>"
        )

# Add the openemr-on-ecs submodule to sys.path so its packages are importable.
# Insert after index 0 (which is '' or the script directory representing the
# parent repo root) so that the parent repo's infrastructure/ package takes
# priority over the submodule's openemr_ecs package (overlay pattern).
if _OPENEMR_SUBMODULE not in sys.path:
    sys.path.insert(1, _OPENEMR_SUBMODULE)
