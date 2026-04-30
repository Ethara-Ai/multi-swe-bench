import importlib, warnings

_submodules = [
    "c", "cpp", "golang", "java", "javascript", "python", "rust",
    "typescript", "ruby", "php", "swift", "kotlin", "scala", "csharp", "html",
]

for _mod in _submodules:
    try:
        _m = importlib.import_module(f"multi_swe_bench.harness.repos.{_mod}")
        _names = getattr(_m, "__all__", [k for k in vars(_m) if not k.startswith("_")])
        globals().update({k: getattr(_m, k) for k in _names})
    except Exception as _e:
        warnings.warn(f"Could not import repos.{_mod}: {_e}")
