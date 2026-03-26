import os
from multi_swe_bench.harness.instance import Instance
from .base import FirecrawlFirecrawlInstance

# Dynamic registration of firecrawl/firecrawl instances
Instance.register("firecrawl", "firecrawl")(FirecrawlFirecrawlInstance)
