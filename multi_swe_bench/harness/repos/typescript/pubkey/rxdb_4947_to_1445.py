import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:20"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the shared era base to a
        # single PR's base.sha and breaking every other PR in the era with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see ImageDefault below).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && \\
    rm -rf /var/lib/apt/lists/*
RUN npm install -g cross-env

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
npm install --legacy-peer-deps --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
npm run transpile || true
cross-env DEFAULT_STORAGE=lokijs npx mocha --config ./config/.mocharc.js ./test_tmp/unit.test.js 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch
npm install --legacy-peer-deps --ignore-scripts || true
npm run transpile || true
cross-env DEFAULT_STORAGE=lokijs npx mocha --config ./config/.mocharc.js ./test_tmp/unit.test.js 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --legacy-peer-deps --ignore-scripts || true
npm run transpile || true
cross-env DEFAULT_STORAGE=lokijs npx mocha --config ./config/.mocharc.js ./test_tmp/unit.test.js 2>&1 || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable from git.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("pubkey", "rxdb_4947_to_1445")
class Rxdb4947To1445(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        re_pass = re.compile(r"^\s*[✓✔]\s+(.*)")
        re_fail = re.compile(r"^\s+(\d+)\)\s+(.*)")
        re_skip = re.compile(r"^\s*[-–]\s+(.*)")

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).rstrip()
            if not clean:
                continue

            fail_match = re_fail.match(clean)
            if fail_match:
                failed_tests.add(fail_match.group(2).strip())
                continue

            pass_match = re_pass.match(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            skip_match = re_skip.match(clean)
            if skip_match:
                skipped_tests.add(skip_match.group(1).strip())

        # A test that appears in both passed and failed should count as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# The raw dataset leaves `number_interval` empty. At build/delivery the record's
# `number_interval` is set to "-".join(prs_in_bundle); the loader then resolves
# `pubkey/<dash-joined-bundle>`. Register Rxdb4947To1445 (this era, lead PR in range)
# under every bundle key so those records route to the correct era class. The
# original `pubkey/rxdb_4947_to_1445` era key registration above is kept (harmless).
_BUNDLE_NIS_RXDB_ERA1 = [
    "1455-1555-1556-1558-1559-1560-1561-1562-1563-1564-1565-1566-1567-1568-1569-1570-1571-1572-1573-1574-1575-1576",
    "1489-1490-1492-1493-1494-1495-1496-1498-1499-1500-1501-1502-1503-1504-1505-1506-1507-1508-1510-1511-1512-1513-1514-1515-1516-1517-1518-1519-1520-1521-1522-1523-1524-1525-1526-1527-1528-1529-1530-1532-1534-1535-1537-1538-1539-1540-1541-1542-1543-1544-1545-1546-1547-1548-1549-1550-1551-1552-1553-1554",
    "1577-1578-1579-1580-1581-1582-1584-1585-1586-1587-1588-1589-1590-1591-1593-1594-1595-1596-1597-1598-1599",
    "1600-1601-1602-1603-1604-1605-1606-1607-1608-1609-1611-1613-1614-1615-1616-1617-1618-1619-1620-1621-1622-1623-1625-1626-1627",
    "1628-1629-1630-1632-1633-1634-1635-1638-1639-1641-1642-1643-1644-1646-1647-1648-1649-1650-1651-1652-1653-1654-1656-1657-1658-1659-1660-1661-1662-1664-1665-1666-1667-1668-1669-1670-1671-1672-1673-1674-1675-1676-1677-1679-1680-1681-1682-1683-1684-1685-1686-1687-1688-1689-1691",
    "1678-1828-1829-1831-1834-1835-1836-1837-1838-1840-1841-1842-1843-1844-1845-1846-1847-1848-1849-1850-1851-1853-1855-1856-1859-1860-1861-1863-1864-1865-1869-1870-1871-1872-1873-1874-1875-1876-1878-1880-1882-1883-1884-1885-1887-1888-1890-1891-1892-1893-1894-1896-1897-1898-1899-1900-1901-1902-1903-1910-1911-1918-1920-1926-1927-1928-1946-1947-1948-1949",
    "1690-1692-1693-1694-1696-1697-1698-1699-1700-1701-1702-1703-1704-1706-1707-1708-1709-1710-1712-1713-1714-1715-1716-1717-1718-1720-1721-1722-1723-1724-1725-1726-1727-1728-1729-1730-1731-1733-1734-1735-1736-1737-1738-1739-1740-1741-1742-1743-1744-1745-1746-1747-1748-1749-1750-1751-1752-1753",
    "1695-2080-2081-2082-2083-2085-2087-2088-2089-2090-2091-2092-2094-2095-2096-2097-2099-2100-2101-2102-2103-2104-2105-2106-2107-2108-2109-2110-2111-2112-2113-2114-2115-2116-2117-2118-2119-2120-2121-2122-2123-2124-2126-2127-2128-2129-2131-2133-2134-2135-2136-2137-2138-2139-2140-2141-2142-2143-2144-2145-2146-2147-2148-2149-2150-2151-2153-2154-2155-2156-2157-2158-2159-2160-2161-2162-2163-2164-2165-2166-2167-2168-2169-2170-2172-2174-2175-2176-2177-2178-2180-2181-2182-2183-2184-2185-2186-2187",
    "1915-1950-1951-1952-1953-1954-1955-1957-1958-1959-1960-1961-1962-1963-1964-1965-1966-1967-1968-1969-1970-1971-1973-1974-1975-1976-1979-1980-1981-1982-1984-1985-1986-1987-1988-1989-1990-1991-1992-1993-1994-1995-1996-1997-1998-1999-2000-2001-2002-2003-2004-2005-2006-2007-2008-2010-2011-2012-2013-2014-2015-2016-2017-2018-2019-2020-2021-2022-2023-2024-2025-2027-2028-2029-2030-2031-2032-2033-2034-2035-2036-2037-2038-2039-2040-2041-2042-2043-2044-2045-2046-2047-2049-2050-2051-2052-2053-2054-2055-2056-2057-2058-2059-2061-2062-2063-2064-2065-2066-2067-2068-2069-2072-2073-2074-2075-2076-2077-2078-2079",
    "2179-2188-2189-2190-2191-2192-2193-2195-2196-2197-2200-2201-2202-2203-2204-2205-2206-2207-2208-2209-2210-2211-2212-2213-2214-2215-2216-2217-2218-2219-2220-2222-2223-2224-2225-2226-2228-2230-2231-2233-2235-2236-2238",
    "2300-2302-2303-2305-2306-2307-2308-2310-2311-2312-2313-2316",
    "2319-2380-2381-2382-2383-2384-2385-2387-2388",
    "2598-3088-3141-3274-3366-3367-3451-3452-3453-3454",
    "2621-2622-2625-2626-2627-2628-2629",
    "2630-2631-2632-2634-2635-2637-2638-2639-2640-2641-2642-2643-2644-2645-2646-2647-2648-2649-2650-2652-2653-2655-2659",
    "2657-2658-2661-2662-2663-2664-2665-2666-2667-2668-2669-2671-2672-2673-2674-2678",
    "2675-2677-2679-2680-2681-2683-2684-2686-2687-2688-2689-2690-2692-2693-2696-2697-2698-2699-2700-2703-2704",
    "2702-2707",
    "2706-2708-2709-2711-2712-2714-2715-2719-2720-2721-2724-2725-2726-2727-2728-2729-2730-2731-2732-2733-2734-2735-2736-2737-2738-2741-2742-2743-2744-2745-2749",
    "2860-2861-2862-2863-2864-2865-2866-2867-2868",
    "3052-3053-3054",
    "3056-3057-3058-3059-3060-3061-3062-3063-3064-3065-3066-3067-3068-3069-3070-3071-3072-3073-3074-3075-3076-3077-3078-3079-3080-3082-3083-3085",
    "3086-3090-3093-3094-3095-3097-3099-3100-3101-3102-3104-3105-3106-3109-3110-3112-3113-3114-3115-3116-3117-3118-3119-3120-3121-3122-3123-3124-3125-3126-3127-3128-3129-3133-3135-3136-3137-3138-3139-3140-3142-3143-3145-3146-3148-3150-3151-3154-3155",
    "3087-3134-3156-3157-3160-3162-3164-3165-3166-3167-3170-3171-3172-3173-3174-3176-3177-3178-3179-3180-3181-3182-3183-3185-3186-3187-3188-3189-3191-3192-3193-3194-3195-3196-3197-3198-3199-3200-3201-3203-3205-3206-3207-3208-3209-3210-3211-3212-3214-3217-3218-3219-3220-3221-3222-3223-3224-3225-3226-3227-3229-3230-3232-3233-3234-3235-3236-3237-3238-3239-3240-3241-3243-3244-3245-3249-3250-3251-3252-3253-3254-3255-3256-3257-3259",
    "3228-3258-3261-3262-3263-3264-3265-3266-3267-3268-3269-3270-3273-3275-3276-3277-3278",
    "3288-3289-3290-3291-3292-3293-3294-3295-3296-3297-3298-3299-3300-3301-3302-3303-3304-3305-3306-3307-3309-3312-3313-3315-3316-3317",
    "3359-3464-3468-3469-3470-3474-3475-3478-3479-3480-3481-3482-3483-3484-3485",
    "3488-3511-3512-3513-3514-3515-3516-3519-3523-3524-3528-3529-3531-3532-3533-3534-3535-3536-3538-3539",
    "3489-3491-3492",
    "3490-3493-3494-3496",
    "3506-3507",
    "3606-3607-3608-3609-3612-3613-3614-3615-3616-3617-3618",
    "3620-3621-3623-3624-3625-3627",
    "3628-3629-3632-3635-3636-3637-3638-3639-3640-3641-3642-3643-3644-3646-3647-3648-3649-3653-3656-3657",
    "3662-3663-3668-3669-3672-3673",
    "3781-3782-3783",
    "3789-3790",
    "3795-3796-3797-3798-3799-3801-3802-3803-3804-3805-3808-3809-3810",
    "3868-3870-3871",
    "3879-3880-3881-3882",
    "3884-3885-3886-3887-3888-3889-3890-3891-3893-3894-3896-3897",
    "3977-3980-3981-3982-3983",
    "3986-3987",
    "3988-3989-3991-3992-3993",
    "3995-3996-3997",
    "3999-4000-4001-4002-4003",
    "4004-4006",
    "4009-4010-4012-4013-4014",
    "4057-4058",
    "4059-4061-4062-4063-4064-4065-4066-4068-4069-4070-4071-4072-4074-4075-4076-4077-4078-4080-4081-4082-4083-4084",
    "4099-4100",
    "4108-4114-4115-4116",
    "4109-4118-4119-4120-4121-4122-4124",
    "4126-4129-4130-4134-4135-4137-4138-4139-4140-4141",
    "4136-4147-4148-4149-4150-4152-4154-4155-4156",
    "4142-4144-4145-4146",
    "4162-4163-4164-4165-4166",
    "4170-4171",
    "4178-4182-4183-4184-4185-4187-4188-4192-4194-4195-4196-4198-4199-4200-4201",
    "4203-4205-4206-4207",
    "4275-4944-4946-4952-4953-4954-4955-4956-4957-4958-4959-4961-4962-4963-4964-4965-4966-4967-4968-4969-4970-4971-4972-4973-4974-4975-4976-4977-4978-4979-4980-4981-4982-4983-4984-4987-4989-4990-4993",
    "4412-4413-4414-4415-4416-4417-4418-4419-4420-4421-4422-4423-4424-4425-4426-4427-4428-4429",
    "4455-4456-4459-4460-4462-4463-4464",
    "4465-4466-4467-4468-4469-4470-4471-4472-4473-4475",
    "4476-4477",
    "4478-4479-4480-4481-4482-4484",
    "4483-4485-4486-4487-4488-4489-4490-4491-4492-4493-4494-4496",
    "4503-4504-4508-4509-4510-4511-4512-4513-4514-4515-4516-4517-4518-4519-4520-4521-4522-4523-4524-4525-4526-4527",
    "4505-4506-4507",
    "4529-4534-4535-4537-4538-4539-4540",
    "4530-4531-4532",
    "4541-4542-4543-4545-4550-4551",
    "4553-4555-4556-4557-4558-4559",
    "4579-4589-4590-4591-4593-4594-4595-4596-4597-4598-4599-4601",
    "4581-4604",
    "4606-4607-4609",
    "4614-4615-4616-4617-4618-4619-4620-4621-4622-4623-4624-4625-4626-4627-4628-4629-4631-4633-4635-4636-4637-4638-4640-4643",
    "4634-4639-4641-4645",
    "4649-4650-4651-4652-4653-4654-4655-4656-4659-4660-4661-4662-4663-4664-4665-4666",
    "4657-4667-4668-4669-4670-4671-4672-4673-4674-4675-4676-4677-4678-4679-4680-4681-4682-4683-4684-4685-4688-4689-4690-4691",
    "4686-4692-4694-4695-4696",
    "4693-4697-4701-4702-4703-4704-4705-4706-4707-4708-4709-4710-4711-4712-4713-4714-4715-4717-4718-4719-4720-4721-4723-4724-4725-4726-4727-4728-4729-4730-4731-4732-4733-4734-4735-4736-4737",
    "4745-4747",
    "4756-4757",
    "4758-4759-4760-4761-4762-4763-4764-4765-4766-4767-4768-4769-4770-4771-4772-4776-4777-4778-4779-4780-4782-4783-4784-4785-4786-4787-4788-4789",
    "4790-4791-4792-4795-4796-4797-4798-4799-4800-4801-4802-4805-4806-4807-4808-4809-4810-4812-4813-4814",
    "4811-4815-4816-4817-4818-4819-4820-4822-4825",
]
for _ni in _BUNDLE_NIS_RXDB_ERA1:
    Instance.register("pubkey", _ni)(Rxdb4947To1445)
