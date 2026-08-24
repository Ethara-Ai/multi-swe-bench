"""Pytest configuration for the multi_swe_bench framework self-tests.

Two jobs:

1. Put the repo root on ``sys.path`` so ``import multi_swe_bench`` works without an
   editable install.
2. Install a non-persistent in-memory shim so importing the harness package
   succeeds despite ``harness/repos/**/__init__.py`` referencing a few absent
   submodules (e.g. ``cpp.shadps4_emu``), which otherwise breaks the package-wide
   ``import *`` chain that ``harness/__init__.py`` triggers. The shim is appended
   to ``sys.meta_path`` so it only fires for repo modules the real finder cannot
   locate; existing repo modules load normally. Nothing is written to disk.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _install_missing_repo_shim() -> None:
    prefix = "multi_swe_bench.harness.repos."

    class _EmptyLoader(importlib.abc.Loader):
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            # Trade-off: unknown attributes on stubbed repo modules resolve
            # to None so package-wide `import *` cascades keep working on
            # partial checkouts. Tests that depend on a specific stubbed
            # symbol will silently receive None; import the target module
            # directly if you need a loud failure instead.
            module.__getattr__ = lambda _name: None
            return None

    class _MissingRepoFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.startswith(prefix):
                return None
            # Reached only when no real finder located the module: stub it empty
            # so the package's ``import *`` chain continues.
            return importlib.util.spec_from_loader(fullname, _EmptyLoader())

    if not any(type(f).__name__ == "_MissingRepoFinder" for f in sys.meta_path):
        sys.meta_path.append(_MissingRepoFinder())


_install_missing_repo_shim()
