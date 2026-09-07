# One module owns vprusso/toqito, and it builds one shared base image for
# every PR. The interpreter and packaging toolchain are chosen from the
# checked-out tree at image-build time, so there is nothing here to route.
from multi_swe_bench.harness.repos.python.vprusso.toqito import *

# Superseded configs are deliberately NOT imported, so nothing registers and
# nothing routes to them. They remain on disk as the record of how each era's
# toolchain was established:
#
#   toqito_1077_to_1026.py, toqito_645_to_614.py, toqito_62_to_62.py
#       Per-era modules, each with its own `base-pr-{number}` image. Their
#       pins now live in TOOLCHAINS in toqito.py.
#   toqito_v1_0.py ... toqito_v1_1_2.py
#       Tag-routed configs. Their registry keys need a `tag` field the
#       dataset does not carry.
