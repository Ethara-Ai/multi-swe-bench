import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PermifyImageBase(Image):
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
        return "golang:1.21-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class PermifyImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return PermifyImageBase(self.pr, self.config)

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

go mod download || true
go test -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("Permify", "permify")
class Permify(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PermifyImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_pattern = re.compile(r"--- PASS: (\S+)")
        failed_pattern = re.compile(r"--- FAIL: (\S+)")
        skipped_pattern = re.compile(r"--- SKIP: (\S+)")

        for line in log.splitlines():
            line = line.strip()

            m = passed_pattern.search(line)
            if m:
                if m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))
                continue

            m = failed_pattern.search(line)
            if m:
                passed_tests.discard(m.group(1))
                failed_tests.add(m.group(1))
                continue

            m = skipped_pattern.search(line)
            if m:
                if m.group(1) not in passed_tests and m.group(1) not in failed_tests:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- Bundle-level number_interval routing keys (all -> Permify) ---
# Each delivered bundle's number_interval registered so Instance.create()
# resolves f"Permify/{number_interval}" to the single-era Permify class
# (see harness/instance.py create(): non-empty number_interval keys the lookup).
_BUNDLE_NIS_PERMIFY = [
    "104-105-106-107-109-111-112-113-114-115-116-117",
    "1064-1065-1066-1071-1072-1073-1074",
    "1080-1081",
    "1099-1103-1106-1107-1153-1154-1155-1156-1157-1158-1160-1161-1162-1163-1164-1165-1166-1167-1168-1169-1170-1171-1172-1173-1174-1175-1176-1177-1178-1179-1180",
    "1266-1273-1274-1275",
    "1276-1277-1296-1307-1308-1309",
    "1319-1320",
    "1331-1333-1335-1336",
    "1442-1460-1461-1463-1464",
    "1462-1467-1468-1469-1470-1472-1475-1476-1477-1478-1479-1480-1481-1482-1483",
    "1474-1484-1486-1487-1488-1489-1490-1491-1508",
    "152-153-155",
    "1697-1698-1699-1700-1701-1702-1703-1704-1705-1707",
    "1706-1708-1709-1710",
    "1750-1751-1752-1753-1754",
    "1816-1817-1819-1820-1821-1822-1823-1824-1825-1828-1831-1832-1833-1834-1835-1836-1837-1838-1839-1840-1841-1842-1843-1845-1846-1848-1849-1850-1851-1852-1853",
    "2111-2112-2113-2115-2116",
    "2512-2621-2622-2623-2624-2625-2626-2627-2628-2629-2630-2631-2632-2633-2634-2635-2636-2637-2638-2639-2640-2641-2643-2644-2645-2646-2647-2648-2649-2650-2651-2652-2654-2655-2658-2659-2660-2661-2662-2664-2665-2666",
    "2540-2541-2542-2543-2544-2545-2546-2547-2548-2549-2550-2551-2552-2553-2554-2555-2556-2557-2558-2560",
    "2561-2562-2563-2564-2565-2566-2567-2568-2569-2570-2571-2572-2575-2576",
    "2657-2667-2668-2669-2670-2671-2673-2674-2675-2676-2677-2679-2680-2682-2683-2684-2685-2688-2689-2690-2691-2692",
    "2708-2718-2719-2720-2721-2722-2723-2724-2725-2726-2727-2728-2729-2730-2737-2738-2739",
    "2740-2741",
    "2789-2798-2811-2815-2824-2825-2826-2827-2828",
    "310-311-312",
    "348-350-353-368-369-370-371-372-377-378-379-381-382-387-388-389-390-394-396-398-399",
    "435-442-443-444-450-451-457-468-470-471-472-473-474-475-476-481-482-484",
    "479-485-487-488-489",
    "491-493-509-510-511",
    "512-513-516-517-518-519-520-521-522-523-524-525",
    "556-561-562-563-564-565-566-567-568-569-570-571-572-573",
    "669-670-671",
    "672-673-674-675-676-677-678-679-680",
    "717-739-741-742-743-744-745-746-747-748-749-750-751-752-753-754-755-756-757-759-760-761-762-763-765-766-767-769-771-772-773",
    "776-777-778-779-780-781-782-783-784-785-786-799-800",
    "812-854-855-856-857-858-859-860-864-865-866-867-868-869-870-871-874-876-877-879-881-882-883-885-902",
    "848-849-850-851-852-853",
    "887-890-894-896-897-901-947-952-953-954-956-957",
    "958-959-960-962-963-964-965-966-983-985-986-987",
    "1020-1023-1025-1026",
    "1027-1028-1034-1038-1045-1076-1083-1087-1088-1089-1091-1093-1096-1097-1098-1110-1112-1114-1115-1116-1117-1118-1119-1120-1122-1123-1124-1126-1127-1128-1129-1130-1131-1132-1134-1136-1137-1138-1139-1140-1141-1142-1143-1144-1145-1146-1147-1148-1149-1150-1151-1152",
    "124-125-126-127-128-129-130-131-132-133-134-135-136-137-138-139-140-141-142-143-144-145-147-148-149",
    "1281-1282-1287-1288-1294-1295-1297-1298-1301-1302-1303-1305-1306",
    "1513-1745-1746-1867-1868-1926-1927-1928-1937-1938-1939-1940-1941-1942-1945-1946-1950-1951-1952-1953-1954-1955-1956-1957-1958-1959-1960-1961-1962-1963-1964-1965-1966-1967-1968-1969-1970-1971-1972-1973-1974-1975-1976-1977-1978-1979-1980",
    "156-157-158-159-160-161-162-163-164-167-169-170-171-172-177-180-181-182-183-184-185-186-187-190-191-192-193-194-195-196-197-198-199-200-201-202-203-204-205-206-207-208-210-211-212-213-214-215-216-218-219-220-221-222-223-224-225-226-227-228-229-230-234-235-236-237-238-239-240-241-242-243-244-245-246-247-248-250-251-252-253-256-257-258-259-260-261-262-263-264-271-272-273-274-276-278-279-281-282-283-284-285-286-287-288-289-290-292-293-294-295-296-297-298-299-300-301-302-303-304-305-306-307",
    "1601-1602-1603-1604-1605-1607-1608-1609",
    "1620-1621-1622-1623-1624-1625-1627-1629-1630-1632-1634-1635-1636-1637-1639-1640-1641-1642-1643",
    "1644-1645-1646-1647-1648-1649-1650-1651-1652-1653-1654-1655-1656-1657-1658-1659-1660-1661-1662-1663-1664-1665-1666-1667",
    "1668-1669-1670-1671-1672-1673-1674-1675-1676-1677-1678-1679-1680-1682-1683-1687-1688-1689-1690-1692-1694-1695-1696",
    "1911-1912-1913-1914-1915-1916-1917-1918-1919-1920-1921-1924-1925",
    "1981-1982-1983-1984-1985-1986-1987-1988",
    "2004-2005-2008-2009-2010-2011-2013-2014-2015-2016-2017-2018-2019-2021-2022-2023-2024-2025-2026-2027-2028-2029-2030-2031",
    "2268-2269-2270-2271-2272-2273-2274-2275-2276-2277-2278-2279-2280-2281-2282-2283-2284-2285-2286-2287-2289-2290-2291-2292",
    "2293-2294-2295-2296-2298-2299-2301-2302-2303-2304-2305-2306-2308-2309-2310-2311-2312-2313-2314-2315-2316-2317-2318-2319-2320-2321",
    "2322-2323-2324-2325-2326-2327-2328-2329-2330-2331-2332-2333-2334-2335-2336-2337-2338-2339-2340-2341-2342-2343-2344-2345-2346-2347-2348-2349-2350-2351-2352-2353-2354-2355-2356-2357-2358-2359-2360-2361-2363-2364-2365-2366-2367-2368-2369-2371-2372-2373-2374-2375-2376-2377-2378-2379-2380-2381-2382-2383-2384-2385-2386-2387-2388-2389-2390-2392-2393-2394-2395-2396-2397-2398-2399-2400-2401-2402",
    "2391-2403-2404-2405-2407-2408-2409-2410-2411-2412-2413-2414-2415-2416-2417-2418-2419-2420-2421-2422-2423-2424-2425-2426-2427-2428-2429-2430-2431-2432-2433-2434-2436-2437-2438-2439-2440-2441-2442-2443-2444-2445-2446-2447-2448-2449-2450-2451-2452-2453",
    "2454-2455-2456-2457-2458-2459-2460-2461-2462-2465-2466-2467-2468-2469-2470-2471-2472-2473-2474-2475-2476-2477-2478-2479-2480-2481-2482-2483-2484-2485-2486-2487-2488-2489-2490-2491-2492-2493-2494-2495-2497-2498-2499-2500-2502-2504-2505-2506-2507-2508-2509-2510-2511-2513-2515-2516-2517-2518-2519-2520-2521-2522-2523-2524-2525-2526-2527-2528-2529-2530-2531-2532-2533-2534-2535-2536-2537-2538-2539",
    "2578-2579-2580-2581",
    "2604-2605-2606-2607-2609-2610-2611-2612-2614-2615-2616-2617-2618-2619",
    "2763-2782-2783-2784-2807-2808-2809-2810-2812-2813-2814-2817-2818-2819-2829-2830-2831-2832-2833-2837-2838",
    "2836-2842-2845-2847-2861-2864-2865-2866-2867-2868-2869-2871",
    "2883-2898-2899-2907-2910-2912-2913-2914-2915-2919-2920-2922-2923-2924-2925-2927-2928-2929-2933-2934-2939-2940-2941-2942-2944-2945",
    "314-315-316-317-318-319-321-322-323-324-325-326-328-329-331-332-334-335-337-338-339-340-341-343-344-345-347-349-351-358-359",
    "400-401-402-403-405-406-407-408-409-411-413-414",
    "415-416-418-423-428-429-430-433-445-449-452-453-454-455-458-459-460-461-462-464-465",
    "424-581-585-595-596-597-598-599-600-601-602-604-607-608-609-610-611-612-613-614-615-616-622-623-624-625-626-627-628-629-630-633-634-635-636-638-641-642-643-644-645-646-647-648-650-653-654-655-656-657-658-659-660-661-662-663-664-665-666-667-668",
    "495-498-499-501-502-503-505-526-528-529-531-532-533-534-535-536-537-538-539-540-542-543-544-545-546-547-549-550-551-552-553-554",
    "683-684-685-693-694-702-710-711-712-713-714-715-718-728-729-731-732-735-737-738-740",
    "841-842-843-845-846",
    "86-87-89-91-92-93-94-95-96-100-101-102",
    "909-910-911-912-913-914-915-919-920-921-922",
    "923-925-926-927-928-930-931-932",
    "937-949-951",
]
for _ni in _BUNDLE_NIS_PERMIFY:
    Instance.register("Permify", _ni)(Permify)
