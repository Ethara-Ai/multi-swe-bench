from multi_swe_bench.harness.repos.typescript.babel.babel import *
from multi_swe_bench.harness.repos.typescript.babel.babel_npm_mocha import *
from multi_swe_bench.harness.repos.typescript.babel.babel_classic_mocha import *
from multi_swe_bench.harness.repos.typescript.babel.babel_classic_jest import *
from multi_swe_bench.harness.repos.typescript.babel.babel_berry_jest import *
from multi_swe_bench.harness.repos.typescript.babel.babel_yarn3_jest import *
from multi_swe_bench.harness.repos.typescript.babel.babel_yarn4_jest import *
from multi_swe_bench.harness.repos.typescript.babel.babel_14065_to_12725 import *

from multi_swe_bench.harness.repos.typescript.babel.babel_15027_to_14240 import *

# Dispatcher last: it imports an era module above.
from multi_swe_bench.harness.repos.typescript.babel.babel_dispatcher import *

# babel_shards is the active config for babel/babel. It registers the same
# ("babel", "babel") key as babel_dispatcher and is imported last, so it wins
# the registry entry. The dispatcher and the era modules above are unchanged
# and keep their own registrations.
from multi_swe_bench.harness.repos.typescript.babel.babel_12707_to_9498 import *
