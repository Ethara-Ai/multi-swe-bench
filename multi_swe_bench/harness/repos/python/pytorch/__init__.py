# Interval configs are selected by the record's "number_interval" field:
# Instance.create resolves f"{org}/{number_interval}" directly (harness/instance.py:42).
#
# The five ignite_* modules immediately below are the per-era set covering
# 1239..99999 on ONE shared base image (mswebench/pytorch_m_ignite:base-ignite-py39).
# Each is self-contained: its dependency constants are fixed for its range, so no
# module branches on a PR number.
#
# ORDER IS LOAD-BEARING: the enrichment pass that fills "number_interval" walks the
# registry in INSERTION order and takes the first interval whose range contains the
# PR number. Registration happens at import time, so these must stay ABOVE the older
# per-era ignite imports, whose ranges overlap them.
from multi_swe_bench.harness.repos.python.pytorch.ignite_1756_to_1239 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_2178_to_1757 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_2895_to_2179 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_3239_to_2896 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_99999_to_3240 import *

from multi_swe_bench.harness.repos.python.pytorch.ignite_3373_to_3240 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_3240_to_2896 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_2369_to_2179 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_2019_to_1756 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_1756_to_1557 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_1556_to_1239 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_1238_to_1105 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_1104_to_564 import *
from multi_swe_bench.harness.repos.python.pytorch.ignite_563_to_43 import *
from multi_swe_bench.harness.repos.python.pytorch.vision_8227_to_6883 import *
from multi_swe_bench.harness.repos.python.pytorch.vision_6830_to_6521 import *
from multi_swe_bench.harness.repos.python.pytorch.vision import *
from multi_swe_bench.harness.repos.python.pytorch.rl_443_to_295 import *
from multi_swe_bench.harness.repos.python.pytorch.rl_295_to_148 import *
