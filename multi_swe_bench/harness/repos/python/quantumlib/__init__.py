from multi_swe_bench.harness.repos.python.quantumlib.cirq_3358_to_3358 import *
from multi_swe_bench.harness.repos.python.quantumlib.cirq_5650_to_4103 import *
from multi_swe_bench.harness.repos.python.quantumlib.cirq_7362_to_7362 import *
from multi_swe_bench.harness.repos.python.quantumlib.cirq_5054_to_5005 import *

# Imported LAST on purpose: the dispatcher enumerates Instance._registry at call
# time, so every interval adapter above must already be registered.
from multi_swe_bench.harness.repos.python.quantumlib.cirq import *
