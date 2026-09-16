from __future__ import annotations

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.python.dribdat.dribdat import (
    ImageBase,
    ImageDefault,
    Dribdat,
)

NUMBER_INTERVAL = "dribdat_373_to_373"


_PREPARE_EXTRA = """
# `-r prod.txt` WITHOUT --no-deps makes pip re-resolve all 104 pins against the
# baked environment; it backtracks and dies with a misleading
#     No matching distribution found for petl==1.7.14
# even though petl 1.7.14 installs fine on its own (verified in a clean 3.9).
# A poetry-exported lock file already lists every transitive dependency, so
# --no-deps is both the correct way to install one and the thing that avoids
# the phantom conflict. pip then skips every pin the base already satisfies at
# the same version (misaka==2.1.1 among them) and installs only what is new.
#
# Several pins bump C extensions (lxml, cffi, psycopg2-binary, bcrypt). Those
# publish manylinux wheels, but a compiler removes the entire class of risk for
# about a minute of build time rather than betting on wheel availability.
apt-get update -qq
apt-get install -y --no-install-recommends build-essential > /dev/null
rm -rf /var/lib/apt/lists/*

for req in requirements/prod.txt requirements/dev.txt; do
    [ -f "$req" ] || continue
    for attempt in 1 2 3; do
        if pip install --no-cache-dir --no-deps -r "$req"; then
            echo "prepare: installed $req"
            break
        fi
        echo "prepare: $req attempt $attempt failed, retrying"
        sleep $((attempt * 10))
        [ "$attempt" = "3" ] && exit 1
    done
done

# The traceback only ever names the FIRST missing import, so gate on the app
# actually importing. A miss fails the BUILD, where it is attributable, instead
# of surfacing three acts later as 0/0.
python -c "import dribdat.app" || {
    echo "prepare: dribdat.app still not importable after installing manifests"
    exit 1
}
echo "prepare: dribdat.app imports cleanly"
"""


class Dribdat373ImageDefault(ImageDefault):
    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def files(self) -> list[File]:
        out = []
        for f in super().files():
            if f.name == "prepare.sh":
                out.append(File(f.dir, f.name, f.content.rstrip() + "\n" + _PREPARE_EXTRA))
            else:
                out.append(f)
        return out


@Instance.register("dribdat", NUMBER_INTERVAL)
class Dribdat373(Dribdat):
    def dependency(self) -> Image:
        return Dribdat373ImageDefault(self.pr, self._config)
