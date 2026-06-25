from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _needs_dbeaver_common(pr: PullRequest) -> bool:
    return pr.number >= 34181


def _dbeaver_common_tag(pr: PullRequest) -> str:
    number = pr.number
    if number <= 34181:
        return "24.1.5"
    elif number <= 35055:
        return "24.2.2"
    elif number <= 35648:
        return "24.2.4"
    elif number <= 36618:
        return "25.0.1"
    elif number <= 38847:
        return "25.3.3"
    elif number <= 39212:
        return "25.2.3"
    else:
        return "25.2.4"


def _java_era(pr: PullRequest) -> str:
    """PR number -> JDK era label for image tagging."""
    number = pr.number
    if number <= 9676:
        return "jdk8"
    elif number <= 16648:
        return "jdk11"
    elif number <= 36618:
        return "jdk17"
    else:
        return "jdk21"


def _java_package(pr: PullRequest) -> str:
    """PR number -> JDK package."""
    era = _java_era(pr)
    return {
        "jdk8": "openjdk-8-jdk",
        "jdk11": "openjdk-11-jdk",
        "jdk17": "openjdk-17-jdk",
        "jdk21": "openjdk-21-jdk",
    }[era]


def _needs_maven39(pr: PullRequest) -> bool:
    """Tycho 4.0.9+ requires Maven 3.9.0+. Ubuntu 22.04 ships 3.6.3."""
    return pr.number >= 34181


def _needs_legacy_p2(pr: PullRequest) -> bool:
    """PRs <= 20093 need local p2 repo with legacy bundles (dead dbeaver p2 repo)."""
    return pr.number <= 20093


def _ce_repo_version(pr: PullRequest) -> str:
    """PR number -> CE p2 repo version for PRs > 20093."""
    number = pr.number
    if number <= 21405:
        return "23.2.5"
    elif number <= 34181:
        return "24.1.5"
    elif number <= 35055:
        return "24.2.2"
    elif number <= 35648:
        return "24.2.4"
    elif number <= 36618:
        return "25.0.1"
    elif number <= 38847:
        return "25.3.3"
    elif number <= 39212:
        return "25.2.3"
    else:
        return "25.2.4"


def _p2_repo_url(pr: PullRequest) -> str:
    """Return p2 repo URL override for Maven command."""
    if _needs_legacy_p2(pr):
        return "file:///p2-legacy"
    ce_version = _ce_repo_version(pr)
    return f"https://repo.dbeaver.net/p2/ce/{ce_version}/"


def _test_command(pr: PullRequest) -> str:
    p2_url = _p2_repo_url(pr)
    common_flags = (
        f"-Dlocal-p2-repo.url={p2_url} "
        "-Dsurefire.useFile=false "
        "-DfailIfNoTests=false "
        "-Dstyle.color=never "
        "-DskipITs=true "
        "-Dmaven.javadoc.skip=true "
        "-Denforcer.skip=true "
        "-Dtycho.resolver.resolveOptionalDependencies=false "
        "-fae -B"
    )
    # JDK11 era needs headless to avoid loading 40+ UI modules
    if _java_era(pr) == "jdk11":
        common_flags += " -Dheadless-platform=true"
    return f"mvn clean verify {common_flags}"


def _wrap_manifest(bsn: str, version: str, packages: str) -> str:
    """Generate MANIFEST.MF content with proper 70-byte line wrapping for jar tool."""
    lines = [
        "Manifest-Version: 1.0",
        "Bundle-ManifestVersion: 2",
        f"Bundle-SymbolicName: {bsn}",
        f"Bundle-Version: {version}",
    ]
    ep_line = f"Export-Package: {packages}"
    if len(ep_line) <= 70:
        lines.append(ep_line)
    else:
        parts = [ep_line[:70]]
        rest = ep_line[70:]
        while rest:
            chunk = rest[:69]
            rest = rest[69:]
            parts.append(" " + chunk)
        lines.append("\\n".join(parts))
    return "\\n".join(lines) + "\\n"


def _bundle_repack_cmd(jar_src: str, bsn: str, version: str, packages: str, dest: str) -> str:
    """Generate shell commands to repack a JAR with OSGi MANIFEST."""
    manifest = _wrap_manifest(bsn, version, packages)
    return (
        f"mkdir -p repack/META-INF && cd repack && jar xf {jar_src} && "
        f"printf '{manifest}' > META-INF/MANIFEST.MF && "
        f"jar cfm {dest} META-INF/MANIFEST.MF . && "
        "cd /tmp && rm -rf repack"
    )


def _stub_bundle_cmd(bsn: str, version: str, pkg: str, dest: str) -> str:
    """Generate shell commands to create a stub OSGi bundle JAR."""
    manifest = _wrap_manifest(bsn, version, pkg)
    return (
        f"printf '{manifest}' > stub/META-INF/MANIFEST.MF && "
        f"jar cfm {dest} stub/META-INF/MANIFEST.MF -C stub ."
    )


def _p2_metadata_cmd(bundles: list, repo_dir: str = "/p2-legacy") -> str:
    """Generate shell commands to create p2 content.xml and artifacts.xml."""
    # content.xml
    units = ""
    for b in bundles:
        bid, bver = b["id"], b["version"]
        pkgs = b.get("packages", "").split(",")
        pkg_provides = "".join(
            f'<provided namespace="java.package" name="{p.strip()}" version="{bver}"/>'
            for p in pkgs if p.strip()
        )
        provides_count = 2 + len([p for p in pkgs if p.strip()])
        units += (
            f'<unit id="{bid}" version="{bver}" singleton="false">'
            f'<provides size="{provides_count}">'
            f'<provided namespace="org.eclipse.equinox.p2.iu" name="{bid}" version="{bver}"/>'
            f'<provided namespace="osgi.bundle" name="{bid}" version="{bver}"/>'
            f'{pkg_provides}'
            f'</provides>'
            f'<artifacts size="1"><artifact classifier="osgi.bundle" id="{bid}" version="{bver}"/></artifacts>'
            f'<touchpoint id="org.eclipse.equinox.p2.osgi" version="1.0.0"/>'
            f'</unit>\\n'
        )
    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\\n'
        '<?metadataRepository version="1.1.0"?>\\n'
        f'<repository name="Legacy" type="org.eclipse.equinox.internal.p2.metadata.repository.LocalMetadataRepository" version="1.0.0">\\n'
        f'<properties size="2"><property name="p2.timestamp" value="1700000000000"/><property name="p2.compressed" value="false"/></properties>\\n'
        f'<units size="{len(bundles)}">\\n'
        f'{units}'
        '</units>\\n</repository>\\n'
    )
    # artifacts.xml — use $$ to escape $ in Dockerfile shell for ${repoUrl} etc.
    artifacts = ""
    for b in bundles:
        bid, bver, bsize = b["id"], b["version"], b.get("size", 1000)
        artifacts += (
            f'<artifact classifier="osgi.bundle" id="{bid}" version="{bver}">'
            f'<properties size="1"><property name="download.size" value="{bsize}"/></properties>'
            f'</artifact>\\n'
        )
    # artifacts.xml — ${repoUrl}/${id}/${version} are p2 variables, NOT shell vars.
    # Since printf uses single quotes, $ is literal (no shell expansion).
    artifacts_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\\n'
        '<?artifactRepository version="1.1.0"?>\\n'
        '<repository name="Legacy" type="org.eclipse.equinox.internal.p2.artifact.repository.simple.SimpleArtifactRepository" version="1">\\n'
        '<properties size="2"><property name="p2.timestamp" value="1700000000000"/><property name="p2.compressed" value="false"/></properties>\\n'
        '<mappings size="1"><rule filter="(&amp; (classifier=osgi.bundle))" output="${repoUrl}/plugins/${id}_${version}.jar"/></mappings>\\n'
        f'<artifacts size="{len(bundles)}">\\n'
        f'{artifacts}'
        '</artifacts>\\n</repository>\\n'
    )
    return (
        f"printf '{content_xml}' > {repo_dir}/content.xml && "
        f"printf '{artifacts_xml}' > {repo_dir}/artifacts.xml"
    )


_JDK8_BUNDLES = [
    {"id": "com.github.jsqlparser", "version": "1.4.0", "size": 434206,
     "url": "https://repo1.maven.org/maven2/com/github/jsqlparser/jsqlparser/1.4/jsqlparser-1.4.jar",
     "packages": "net.sf.jsqlparser,net.sf.jsqlparser.expression,net.sf.jsqlparser.expression.operators.arithmetic,net.sf.jsqlparser.expression.operators.conditional,net.sf.jsqlparser.expression.operators.relational,net.sf.jsqlparser.parser,net.sf.jsqlparser.schema,net.sf.jsqlparser.statement,net.sf.jsqlparser.statement.alter,net.sf.jsqlparser.statement.create.index,net.sf.jsqlparser.statement.create.table,net.sf.jsqlparser.statement.create.view,net.sf.jsqlparser.statement.delete,net.sf.jsqlparser.statement.drop,net.sf.jsqlparser.statement.execute,net.sf.jsqlparser.statement.insert,net.sf.jsqlparser.statement.replace,net.sf.jsqlparser.statement.select,net.sf.jsqlparser.statement.truncate,net.sf.jsqlparser.statement.update,net.sf.jsqlparser.util,net.sf.jsqlparser.util.deparser"},
    {"id": "org.apache.commons.jexl", "version": "2.1.1", "size": 276759,
     "url": "https://repo1.maven.org/maven2/org/apache/commons/commons-jexl/2.1.1/commons-jexl-2.1.1.jar",
     "packages": "org.apache.commons.jexl2,org.apache.commons.jexl2.internal,org.apache.commons.jexl2.internal.introspection,org.apache.commons.jexl2.introspection,org.apache.commons.jexl2.parser,org.apache.commons.jexl2.scripting"},
    {"id": "net.sf.opencsv", "version": "2.3.0", "size": 21030,
     "url": "https://repo1.maven.org/maven2/net/sf/opencsv/opencsv/2.3/opencsv-2.3.jar",
     "packages": "au.com.bytecode.opencsv,au.com.bytecode.opencsv.bean"},
    {"id": "com.vividsolutions.jts", "version": "1.14.0", "size": 814149,
     "url": "https://repo1.maven.org/maven2/com/vividsolutions/jts-core/1.14.0/jts-core-1.14.0.jar",
     "packages": "com.vividsolutions.jts,com.vividsolutions.jts.algorithm,com.vividsolutions.jts.algorithm.distance,com.vividsolutions.jts.algorithm.locate,com.vividsolutions.jts.algorithm.match,com.vividsolutions.jts.geom,com.vividsolutions.jts.geom.impl,com.vividsolutions.jts.geom.prep,com.vividsolutions.jts.geom.util,com.vividsolutions.jts.geomgraph,com.vividsolutions.jts.geomgraph.index,com.vividsolutions.jts.index,com.vividsolutions.jts.index.bintree,com.vividsolutions.jts.index.chain,com.vividsolutions.jts.index.intervalrtree,com.vividsolutions.jts.index.kdtree,com.vividsolutions.jts.index.quadtree,com.vividsolutions.jts.index.strtree,com.vividsolutions.jts.index.sweepline,com.vividsolutions.jts.io,com.vividsolutions.jts.io.gml2,com.vividsolutions.jts.linearref,com.vividsolutions.jts.math,com.vividsolutions.jts.noding,com.vividsolutions.jts.noding.snapround,com.vividsolutions.jts.operation,com.vividsolutions.jts.operation.buffer,com.vividsolutions.jts.operation.buffer.validate,com.vividsolutions.jts.operation.distance,com.vividsolutions.jts.operation.linemerge,com.vividsolutions.jts.operation.overlay,com.vividsolutions.jts.operation.overlay.snap,com.vividsolutions.jts.operation.overlay.validate,com.vividsolutions.jts.operation.polygonize,com.vividsolutions.jts.operation.predicate,com.vividsolutions.jts.operation.relate,com.vividsolutions.jts.operation.union,com.vividsolutions.jts.operation.valid,com.vividsolutions.jts.planargraph,com.vividsolutions.jts.planargraph.algorithm,com.vividsolutions.jts.precision,com.vividsolutions.jts.simplify,com.vividsolutions.jts.triangulate,com.vividsolutions.jts.triangulate.quadedge,com.vividsolutions.jts.util"},
    {"id": "org.jkiss.bundle.gis", "version": "1.0.0", "size": 1017935,
     "url": "https://repo1.maven.org/maven2/org/locationtech/jts/jts-core/1.18.0/jts-core-1.18.0.jar",
     "packages": "org.locationtech.jts,org.locationtech.jts.algorithm,org.locationtech.jts.geom,org.locationtech.jts.geom.impl,org.locationtech.jts.geom.prep,org.locationtech.jts.geom.util,org.locationtech.jts.io,org.locationtech.jts.io.gml2,org.locationtech.jts.math,org.locationtech.jts.operation,org.locationtech.jts.operation.buffer,org.locationtech.jts.operation.distance,org.locationtech.jts.operation.linemerge,org.locationtech.jts.operation.overlay,org.locationtech.jts.operation.polygonize,org.locationtech.jts.operation.union,org.locationtech.jts.operation.valid,org.locationtech.jts.precision,org.locationtech.jts.simplify,org.locationtech.jts.triangulate,org.locationtech.jts.util"},
    {"id": "org.mockito.mockito-all", "version": "1.10.19", "size": 1234599,
     "url": "https://repo1.maven.org/maven2/org/mockito/mockito-all/1.10.19/mockito-all-1.10.19.jar",
     "packages": "org.mockito"},
    {"id": "org.apache.commons.cli", "version": "1.4.0", "size": 53820,
     "url": "https://repo1.maven.org/maven2/commons-cli/commons-cli/1.4/commons-cli-1.4.jar",
     "packages": "org.apache.commons.cli"},
    {"id": "org.apache.commons.logging", "version": "1.2.0", "size": 61829,
     "url": "https://repo1.maven.org/maven2/commons-logging/commons-logging/1.2/commons-logging-1.2.jar",
     "packages": "org.apache.commons.logging,org.apache.commons.logging.impl"},
]

_JDK8_STUBS = [
    {"id": "org.jkiss.bundle.apache.batik", "version": "1.0.0", "size": 430, "packages": "org.apache.batik"},
    {"id": "org.jkiss.bundle.apache.poi", "version": "1.0.0", "size": 428, "packages": "org.apache.poi"},
    {"id": "org.jkiss.bundle.jfreechart", "version": "1.0.0", "size": 428, "packages": "org.jfree.chart"},
    {"id": "org.jkiss.bundle.sshj", "version": "1.0.0", "size": 422, "packages": "net.schmizz.sshj"},
    {"id": "com.github.eclipsecolortheme", "version": "1.0.0", "size": 443, "packages": "com.github.eclipsecolortheme"},
    {"id": "org.eclipse.nebula.widgets.gallery", "version": "1.0.0", "size": 440, "packages": "org.eclipse.nebula.widgets.gallery"},
]


def _legacy_p2_setup_jdk8() -> str:
    """RUN commands to create /p2-legacy with correct bundles for JDK8 era.
    Downloads real JARs from Maven Central and repacks with OSGi MANIFESTs."""
    # Download commands
    downloads = []
    for i, b in enumerate(_JDK8_BUNDLES):
        downloads.append(f'curl -fsSL -o /tmp/b{i}.jar "{b["url"]}"')

    # Repack commands
    repacks = []
    for i, b in enumerate(_JDK8_BUNDLES):
        dest = f'/p2-legacy/plugins/{b["id"]}_{b["version"]}.jar'
        # mockito-all already has OSGi MANIFEST, just copy
        if b["id"] == "org.mockito.mockito-all":
            repacks.append(f"cp /tmp/b{i}.jar {dest}")
        else:
            repacks.append(_bundle_repack_cmd(f"/tmp/b{i}.jar", b["id"], b["version"], b["packages"], dest))

    # Stub commands
    stubs = []
    for b in _JDK8_STUBS:
        dest = f'/p2-legacy/plugins/{b["id"]}_{b["version"]}.jar'
        stubs.append(_stub_bundle_cmd(b["id"], b["version"], b["packages"], dest))

    # P2 metadata
    all_bundles = _JDK8_BUNDLES + _JDK8_STUBS
    metadata = _p2_metadata_cmd(all_bundles)

    # Combine into single RUN
    cmds = (
        ["mkdir -p /p2-legacy/plugins", "cd /tmp"]
        + downloads
        + repacks
        + ["rm -f /tmp/b*.jar", "mkdir -p stub/META-INF"]
        + stubs
        + ["rm -rf /tmp/stub"]
        + [metadata]
    )
    return "    RUN " + " && \\\n        ".join(cmds) + "\n"


_JDK11_BUNDLES = [
    {"id": "com.github.jsqlparser", "version": "4.5.0", "size": 645588,
     "url": "https://repo1.maven.org/maven2/com/github/jsqlparser/jsqlparser/4.5/jsqlparser-4.5.jar",
     "packages": "net.sf.jsqlparser,net.sf.jsqlparser.expression,net.sf.jsqlparser.expression.operators.arithmetic,net.sf.jsqlparser.expression.operators.conditional,net.sf.jsqlparser.expression.operators.relational,net.sf.jsqlparser.parser,net.sf.jsqlparser.schema,net.sf.jsqlparser.statement,net.sf.jsqlparser.statement.alter,net.sf.jsqlparser.statement.alter.sequence,net.sf.jsqlparser.statement.comment,net.sf.jsqlparser.statement.create.index,net.sf.jsqlparser.statement.create.table,net.sf.jsqlparser.statement.create.view,net.sf.jsqlparser.statement.delete,net.sf.jsqlparser.statement.drop,net.sf.jsqlparser.statement.execute,net.sf.jsqlparser.statement.grant,net.sf.jsqlparser.statement.insert,net.sf.jsqlparser.statement.merge,net.sf.jsqlparser.statement.replace,net.sf.jsqlparser.statement.select,net.sf.jsqlparser.statement.truncate,net.sf.jsqlparser.statement.update,net.sf.jsqlparser.statement.upsert,net.sf.jsqlparser.statement.values,net.sf.jsqlparser.util,net.sf.jsqlparser.util.deparser,net.sf.jsqlparser.util.validation,net.sf.jsqlparser.util.validation.feature,net.sf.jsqlparser.util.validation.metadata"},
    {"id": "org.apache.commons.jexl", "version": "3.1.0", "size": 397422,
     "url": "https://repo1.maven.org/maven2/org/apache/commons/commons-jexl3/3.1/commons-jexl3-3.1.jar",
     "packages": "org.apache.commons.jexl3,org.apache.commons.jexl3.internal,org.apache.commons.jexl3.internal.introspection,org.apache.commons.jexl3.introspection,org.apache.commons.jexl3.parser"},
    {"id": "net.sf.opencsv", "version": "2.3.0", "size": 21030,
     "url": "https://repo1.maven.org/maven2/net/sf/opencsv/opencsv/2.3/opencsv-2.3.jar",
     "packages": "au.com.bytecode.opencsv,au.com.bytecode.opencsv.bean"},
    {"id": "org.jkiss.bundle.gis", "version": "1.0.0", "size": 1017935,
     "url": "https://repo1.maven.org/maven2/org/locationtech/jts/jts-core/1.18.0/jts-core-1.18.0.jar",
     "packages": "org.locationtech.jts,org.locationtech.jts.algorithm,org.locationtech.jts.geom,org.locationtech.jts.geom.impl,org.locationtech.jts.geom.prep,org.locationtech.jts.geom.util,org.locationtech.jts.io,org.locationtech.jts.math,org.locationtech.jts.operation,org.locationtech.jts.operation.buffer,org.locationtech.jts.operation.distance,org.locationtech.jts.operation.linemerge,org.locationtech.jts.operation.overlay,org.locationtech.jts.operation.polygonize,org.locationtech.jts.operation.union,org.locationtech.jts.operation.valid,org.locationtech.jts.precision,org.locationtech.jts.simplify,org.locationtech.jts.triangulate,org.locationtech.jts.util"},
    {"id": "org.mockito.mockito-all", "version": "1.10.19", "size": 1234599,
     "url": "https://repo1.maven.org/maven2/org/mockito/mockito-all/1.10.19/mockito-all-1.10.19.jar",
     "packages": "org.mockito"},
    {"id": "org.mockito.mockito-core", "version": "4.8.1", "size": 623188,
     "url": "https://repo1.maven.org/maven2/org/mockito/mockito-core/4.8.1/mockito-core-4.8.1.jar",
     "packages": "org.mockito,org.mockito.junit,org.mockito.stubbing,org.mockito.verification,org.mockito.invocation,org.mockito.listeners,org.mockito.plugins,org.mockito.quality,org.mockito.session,org.mockito.internal,org.mockito.internal.util"},
    {"id": "net.bytebuddy.byte-buddy", "version": "1.12.19", "size": 3939869,
     "url": "https://repo1.maven.org/maven2/net/bytebuddy/byte-buddy/1.12.19/byte-buddy-1.12.19.jar",
     "packages": "net.bytebuddy,net.bytebuddy.agent.builder,net.bytebuddy.asm,net.bytebuddy.build,net.bytebuddy.description,net.bytebuddy.description.annotation,net.bytebuddy.description.enumeration,net.bytebuddy.description.field,net.bytebuddy.description.method,net.bytebuddy.description.modifier,net.bytebuddy.description.type,net.bytebuddy.dynamic,net.bytebuddy.dynamic.loading,net.bytebuddy.dynamic.scaffold,net.bytebuddy.dynamic.scaffold.inline,net.bytebuddy.dynamic.scaffold.subclass,net.bytebuddy.implementation,net.bytebuddy.implementation.auxiliary,net.bytebuddy.implementation.bind,net.bytebuddy.implementation.bind.annotation,net.bytebuddy.implementation.bytecode,net.bytebuddy.implementation.bytecode.assign,net.bytebuddy.implementation.bytecode.assign.primitive,net.bytebuddy.implementation.bytecode.assign.reference,net.bytebuddy.implementation.bytecode.collection,net.bytebuddy.implementation.bytecode.constant,net.bytebuddy.implementation.bytecode.member,net.bytebuddy.jar.asm,net.bytebuddy.matcher,net.bytebuddy.pool,net.bytebuddy.utility,net.bytebuddy.utility.dispatcher,net.bytebuddy.utility.nullability,net.bytebuddy.utility.privilege,net.bytebuddy.utility.visitor"},
    {"id": "org.objenesis", "version": "3.3.0", "size": 56868,
     "url": "https://repo1.maven.org/maven2/org/objenesis/objenesis/3.3/objenesis-3.3.jar",
     "packages": "org.objenesis,org.objenesis.instantiator,org.objenesis.strategy"},
    {"id": "org.apache.commons.cli", "version": "1.4.0", "size": 53820,
     "url": "https://repo1.maven.org/maven2/commons-cli/commons-cli/1.4/commons-cli-1.4.jar",
     "packages": "org.apache.commons.cli"},
    {"id": "org.apache.commons.logging", "version": "1.2.0", "size": 61829,
     "url": "https://repo1.maven.org/maven2/commons-logging/commons-logging/1.2/commons-logging-1.2.jar",
     "packages": "org.apache.commons.logging,org.apache.commons.logging.impl"},
]

_JDK11_STUBS = [
    {"id": "org.jkiss.bundle.apache.batik", "version": "1.0.0", "size": 430, "packages": "org.apache.batik"},
    {"id": "org.jkiss.bundle.apache.poi", "version": "1.0.0", "size": 428, "packages": "org.apache.poi"},
    {"id": "org.jkiss.bundle.jfreechart", "version": "1.0.0", "size": 428, "packages": "org.jfree.chart"},
    {"id": "org.jkiss.bundle.sshj", "version": "1.0.0", "size": 422, "packages": "net.schmizz.sshj"},
]


def _legacy_p2_setup_jdk11() -> str:
    """RUN commands to create /p2-legacy with correct bundles for JDK11 era."""
    downloads = []
    for i, b in enumerate(_JDK11_BUNDLES):
        downloads.append(f'curl -fsSL -o /tmp/b{i}.jar "{b["url"]}"')

    repacks = []
    for i, b in enumerate(_JDK11_BUNDLES):
        dest = f'/p2-legacy/plugins/{b["id"]}_{b["version"]}.jar'
        if b["id"] in ("org.mockito.mockito-all", "com.github.jsqlparser", "net.bytebuddy.byte-buddy", "org.objenesis"):
            repacks.append(f"cp /tmp/b{i}.jar {dest}")
        elif b["id"] == "org.mockito.mockito-core":
            repacks.append(_bundle_repack_cmd(f"/tmp/b{i}.jar", b["id"], b["version"], b["packages"], dest))
        else:
            repacks.append(_bundle_repack_cmd(f"/tmp/b{i}.jar", b["id"], b["version"], b["packages"], dest))

    stubs = []
    for b in _JDK11_STUBS:
        dest = f'/p2-legacy/plugins/{b["id"]}_{b["version"]}.jar'
        stubs.append(_stub_bundle_cmd(b["id"], b["version"], b["packages"], dest))

    all_bundles = _JDK11_BUNDLES + _JDK11_STUBS
    metadata = _p2_metadata_cmd(all_bundles)

    cmds = (
        ["mkdir -p /p2-legacy/plugins", "cd /tmp"]
        + downloads
        + repacks
        + ["rm -f /tmp/b*.jar", "mkdir -p stub/META-INF"]
        + stubs
        + ["rm -rf /tmp/stub"]
        + [metadata]
    )
    return "    RUN " + " && \\\n        ".join(cmds) + "\n"


def _fix_dead_repos_cmd() -> str:
    """Replace dead p2 repo URLs with local p2-legacy."""
    return (
        "sed -i 's|http://dbeaver.jkiss.org/eclipse-repo|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|https://dbeaver.jkiss.org/eclipse-repo|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|https://dbeaver.io/eclipse-repo|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|http://dbeaver.io/eclipse-repo|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|https://p2.dev.dbeaver.com/eclipse-repo|file:///p2-legacy|g' pom.xml && "
        # Also replace eclipse-color-theme dead repo URL
        "sed -i 's|http://eclipse-color-theme.github.com/update|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|https://eclipse-color-theme.github.io/update|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|http://download.eclipse.org/nebula/releases/latest|file:///p2-legacy|g' pom.xml && "
        "sed -i 's|https://download.eclipse.org/nebula/releases/latest|file:///p2-legacy|g' pom.xml || true"
    )


def _remove_modules_cmd() -> str:
    """Remove non-test modules from root pom.xml that we don't need."""
    return (
        "sed -i '/<module>features<\\/module>/d' pom.xml && "
        "sed -i '/<module>plugins-dev<\\/module>/d' pom.xml && "
        "sed -i '/<module>product/d' pom.xml && "
        "sed -i '/<module>..\\/..*<\\/module>/d' pom.xml || true"
    )


def _remove_modules_jdk8_cmd() -> str:
    return (
        "sed -i '/<module>org.jkiss.dbeaver.ext.ui.svg<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.net.ssh.sshj<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.data.office<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.data.office.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ui.charts<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ui.dashboard<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.ui.colortheme<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.ui.tipoftheday<\\/module>/d' plugins/pom.xml || true"
    )


def _remove_modules_jdk11_cmd() -> str:
    """Remove modules with missing deps for JDK11 era. Much more aggressive —
    remove all GIS-dependent, UI, and ext modules that aren't needed by test.platform."""
    return (
        # Remove gef3-dependent UI modules
        "sed -i '/<module>org.jkiss.dbeaver.erd.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.ui.locks<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.ui.svg<\\/module>/d' plugins/pom.xml && "
        # Remove GIS-dependent modules
        "sed -i '/<module>org.jkiss.dbeaver.data.gis<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.data.gis.view<\\/module>/d' plugins/pom.xml && "
        # Remove ext modules that pull in dead deps
        "sed -i '/<module>org.jkiss.dbeaver.ext.exasol<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.exasol.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.h2gis<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.hana<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.mysql<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.mysql.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.oracle<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.oracle.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql.debug.core<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql.debug.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.greenplum<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.sqlite<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.sqlite.ui<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.import_config<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.net.ssh.sshj<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.data.office<\\/module>/d' plugins/pom.xml && "
        "sed -i '/<module>org.jkiss.dbeaver.ext.oceanbase<\\/module>/d' plugins/pom.xml 2>/dev/null; "
        "sed -i '/<module>org.jkiss.dbeaver.ext.oceanbase.ui<\\/module>/d' plugins/pom.xml 2>/dev/null && "
        # Remove test modules that depend on removed plugins
        "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql.test<\\/module>/d' test/pom.xml 2>/dev/null; "
        "sed -i '/<module>org.jkiss.dbeaver.ext.greenplum.test<\\/module>/d' test/pom.xml 2>/dev/null; "
        "sed -i '/<module>org.jkiss.dbeaver.ext.oracle.test<\\/module>/d' test/pom.xml 2>/dev/null; "
        "sed -i '/<module>org.jkiss.dbeaver.ext.test<\\/module>/d' test/pom.xml 2>/dev/null; "
        # Fix test.platform MANIFEST — remove deps on removed modules
        "sed -i '/org.jkiss.dbeaver.ext.postgresql/d' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        "sed -i '/org.jkiss.dbeaver.ext.oracle/d' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        "sed -i '/org.jkiss.dbeaver.ext.snowflake/d' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        "sed -i '/org.jkiss.dbeaver.ext.hana/d' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        "sed -i '/org.jkiss.dbeaver.ext.mssql/d' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        "sed -i -z 's/,\\n\\([A-Z]\\)/\\n\\1/g' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        "sed -i -z 's/,\\n$/\\n/g' test/org.jkiss.dbeaver.test.platform/META-INF/MANIFEST.MF 2>/dev/null; "
        # Add test module to default modules (desktop profile disabled with -Dheadless-platform)
        "sed -i 's|<module>plugins</module>|<module>plugins</module>\\n        <module>test</module>|' pom.xml 2>/dev/null || true"
    )


def _fix_manifest_cmd() -> str:
    """Remove bouncycastle from MANIFEST files (optional dep, not in any p2 repo)."""
    return (
        "perl -i -0pe 's/,\\n org.jkiss.bundle.bouncycastle[^\\n]*//s' "
        "plugins/org.jkiss.dbeaver.core/META-INF/MANIFEST.MF 2>/dev/null || true && "
        "perl -i -0pe 's/,\\n org.jkiss.bundle.bouncycastle[^\\n]*//s' "
        "plugins/org.jkiss.dbeaver.model/META-INF/MANIFEST.MF 2>/dev/null || true"
    )


def _build_fixup_cmd(pr: PullRequest) -> str:
    """Combine all era-specific fixup commands into one block."""
    era = _java_era(pr)
    parts = []

    # Fix dead repos — all eras <= 20093
    if pr.number <= 20093:
        parts.append(_fix_dead_repos_cmd())

    # Remove common non-test modules (features, product, plugins-dev)
    parts.append(_remove_modules_cmd())

    # Era-specific module removal
    if era == "jdk8":
        parts.append(_remove_modules_jdk8_cmd())
        parts.append(_fix_manifest_cmd())
    elif era == "jdk11":
        parts.append(_remove_modules_jdk11_cmd())
        parts.append(_fix_manifest_cmd())
    else:
        # JDK17/21 era: remove gef3 modules if needed (PRs 18135, 20093)
        if pr.number <= 20093:
            parts.append(
                "sed -i '/<module>org.jkiss.dbeaver.erd.ui<\\/module>/d' plugins/pom.xml && "
                "sed -i '/<module>org.jkiss.dbeaver.ext.ui.locks<\\/module>/d' plugins/pom.xml && "
                "sed -i '/<module>org.jkiss.dbeaver.ext.ui.svg<\\/module>/d' plugins/pom.xml && "
                "sed -i '/<module>org.jkiss.dbeaver.ext.oracle.ui<\\/module>/d' plugins/pom.xml && "
                "sed -i '/<module>org.jkiss.dbeaver.ext.exasol.ui<\\/module>/d' plugins/pom.xml && "
                "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql.ui<\\/module>/d' plugins/pom.xml && "
                "sed -i '/<module>org.jkiss.dbeaver.ext.postgresql.debug.ui<\\/module>/d' plugins/pom.xml || true"
            )

    # For PRs >=34181: add org.jkiss.utils from dbeaver-common to reactor
    if _needs_dbeaver_common(pr):
        parts.append(
            "sed -i 's|<module>plugins</module>|"
            "<module>../dbeaver-common/modules/org.jkiss.utils</module>\\n"
            "        <module>plugins</module>|' pom.xml"
        )

    # Remove eclipse-color-theme dead repo (perl approach for older pom format)
    if pr.number <= 20093:
        parts.append(
            "perl -i -0pe 's/<repository>\\s*<id>eclipse-color-theme<\\/id>.*?<\\/repository>//s' pom.xml || true"
        )

    return "\n".join(parts)


class DbeaverImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"base-{_java_era(self.pr)}"

    def workdir(self) -> str:
        return f"base-{_java_era(self.pr)}"

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

        jdk_package = _java_package(self.pr)

        if _needs_maven39(self.pr):
            maven_install = (
                "RUN apt-get update && apt-get install -y git {jdk} curl && "
                "curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz "
                "| tar -xz -C /opt && ln -s /opt/apache-maven-3.9.9/bin/mvn /usr/local/bin/mvn"
            ).format(jdk=jdk_package)
        else:
            maven_install = f"RUN apt-get update && apt-get install -y git {jdk_package} maven curl"

        era = _java_era(self.pr)
        java_version_map = {"jdk8": "8", "jdk11": "11", "jdk17": "17", "jdk21": "21"}
        java_ver = java_version_map[era]
        set_java = (
            f'RUN ARCH=$(dpkg --print-architecture) && '
            f'JAVA_HOME=/usr/lib/jvm/java-{java_ver}-openjdk-$ARCH && '
            f'update-alternatives --set java $JAVA_HOME/bin/java 2>/dev/null || true && '
            f'update-alternatives --set javac $JAVA_HOME/bin/javac 2>/dev/null || true && '
            f'ln -sfn $JAVA_HOME /usr/lib/jvm/default-java'
        )

        p2_setup = ""
        if era == "jdk8":
            p2_setup = _legacy_p2_setup_jdk8()
        elif era == "jdk11":
            p2_setup = _legacy_p2_setup_jdk11()

        # The leading `# syntax=` directive makes DockerfileEnhancer return this
        # Dockerfile verbatim (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`).
        # That is REQUIRED for the shared per-era base: without it the enhancer's
        # _standardize_repo_fetch rewrites the `git clone` below into a
        # ${BASE_COMMIT}-dependent hardening pass. The base has no BASE_COMMIT
        # (it is reused by every PR of the era and must keep full history so any
        # PR's base.sha stays reachable), so that injected pass would both fail
        # to build and wrongly prune history. Hardening happens per-PR in
        # DbeaverImageDefault instead.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH=/usr/lib/jvm/default-java/bin:$PATH
WORKDIR /home/
{maven_install}
{set_java}

{p2_setup}

{code}

{self.clear_env}

"""


class DbeaverImageDefault(Image):
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
        return DbeaverImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _test_command(self.pr)
        fixup_cmd = _build_fixup_cmd(self.pr)
        # Do NOT use -o (offline) for legacy p2 — Tycho 1.x has a bug resolving
        # ${repoUrl} in artifacts.xml for file: repos when running offline
        run_test_cmd = test_cmd

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

""",
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
{fixup_cmd}

{test_cmd} || true

# Cache is warmed in ~/.m2 and target/; revert the fixup's tracked pom edits so
# the tree is clean before the image-level hardening pass detaches onto
# ${{BASE_COMMIT}}. The runtime scripts (run/test-run/fix-run) re-apply fixup.
git reset --hard || true
""".format(pr=self.pr, test_cmd=test_cmd, fixup_cmd=fixup_cmd),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
{fixup_cmd}
{run_test_cmd}
""".format(pr=self.pr, run_test_cmd=run_test_cmd, fixup_cmd=fixup_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
{fixup_cmd}
{run_test_cmd}

""".format(pr=self.pr, run_test_cmd=run_test_cmd, fixup_cmd=fixup_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch
{fixup_cmd}
{run_test_cmd}

""".format(pr=self.pr, run_test_cmd=run_test_cmd, fixup_cmd=fixup_cmd),
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
        clone_common = ""
        if _needs_dbeaver_common(self.pr):
            common_tag = _dbeaver_common_tag(self.pr)
            clone_common = (
                f"RUN git clone --depth 1 --branch {common_tag} "
                f"https://github.com/dbeaver/dbeaver-common.git /home/dbeaver-common"
            )

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        # Per-PR hardening. This layer chains to the shared base *Image*, so
        # DockerfileEnhancer returns dockerfile() verbatim and supplies neither
        # ARG BASE_COMMIT nor the hardening pass — we bake both in here.
        #
        # BASE_COMMIT is the value the hardening block detaches onto. It is set
        # to this PR's base.sha so Image._HARDENING_BLOCK prunes every other
        # ref/remote/commit, leaving only base.sha reachable. This is what stops
        # a solver from `git log`/`git show`/`git diff`-ing the fix commit or any
        # future commit out of the checked-out repo (reward hacking).
        #
        # `hardening` is substituted as a plain variable value (NOT spliced into
        # f-string brace syntax), so the ${BASE_COMMIT}/%(refname) tokens inside
        # it stay byte-identical to image.py. Literal Dockerfile braces below are
        # doubled ({{ }}) so they survive f-string interpolation.
        hardening = Image._HARDENING_BLOCK
        base_sha = self.pr.base.sha
        repo = self.pr.repo
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{proxy_setup}

WORKDIR /home/{repo}

{clone_common}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

WORKDIR /home/{repo}

{hardening}

CMD ["/bin/bash"]
"""


@Instance.register("dbeaver", "dbeaver")
class Dbeaver(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DbeaverImageDefault(self.pr, self._config)

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

    @staticmethod
    def _classify_test(test_name, tests_run, failures, errors, skipped,
                       passed_tests, failed_tests, skipped_tests):
        if tests_run > 0 and failures == 0 and errors == 0 and skipped != tests_run:
            passed_tests.add(test_name)
        elif failures > 0 or errors > 0:
            failed_tests.add(test_name)
        elif skipped == tests_run:
            skipped_tests.add(test_name)

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # Modern surefire: "Tests run: N ... Time elapsed: X.XX s -- in org.pkg.Class"
        pattern_modern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )
        # Old surefire (Tycho 1.x): "Running org.pkg.Class" then "Tests run: N ... Time elapsed: X.XX sec"
        pattern_running = re.compile(r"Running\s+(\S+)")
        pattern_old = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ sec"
        )

        current_test_class = None
        lines = test_log.splitlines()
        for line in lines:
            # Try modern format first
            match = pattern_modern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5)
                self._classify_test(test_name, tests_run, failures, errors, skipped,
                                    passed_tests, failed_tests, skipped_tests)
                continue

            # Track "Running <class>" lines for old format
            running_match = pattern_running.match(line)
            if running_match:
                current_test_class = running_match.group(1)
                continue

            # Old format: "Tests run: ... Time elapsed: X.XX sec" (no "in <class>")
            old_match = pattern_old.search(line)
            if old_match and current_test_class:
                tests_run = int(old_match.group(1))
                failures = int(old_match.group(2))
                errors = int(old_match.group(3))
                skipped = int(old_match.group(4))
                self._classify_test(current_test_class, tests_run, failures, errors, skipped,
                                    passed_tests, failed_tests, skipped_tests)
                current_test_class = None

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval routing (per-bundle, hyphen-joined exact PR numbers).
#
# Each bundle's number_interval is the hyphen-joined list of its real PR
# numbers (e.g. "146-147-150-155-157"), NOT a contiguous range. Every bundle
# uses the single Dbeaver config above, so Instance.create's exact-key lookup
#   name = f"{org}/{number_interval}"
# needs each bundle string registered. Rows with number_interval="" keep
# routing to dbeaver/dbeaver and are unaffected.
#
# Derived from prs_in_bundle in dbeaver__dbeaver_lht_final.jsonl; keys MUST
# match the dataset exactly. Regenerate if the bundling changes.
# ---------------------------------------------------------------------------
_DBEAVER_BUNDLES = [
    [1154, 1159],
    [1626, 1637],
    [1890, 1892, 1893],
    [1914, 1973],
    [2360, 2367, 2368, 2369, 2370, 2404, 2418],
    [2432, 2476, 2489, 2506],
    [2559, 2580, 2581, 2582],
    [2684, 2685, 2699, 2708, 2713, 2727, 2729, 2731, 2734, 2735, 2745, 2746],
    [3002, 3007, 3032, 3033, 3041, 3067, 3068, 3069, 3071, 3072, 3073, 3074, 3077, 3083],
    [3430, 3433],
    [3834, 3850, 3855],
    [4023, 4865, 4875, 4891, 4895, 4908, 4909, 4910, 4922],
    [4499, 4513],
    [4546, 4563, 4596, 4602],
    [4788, 4797, 4841, 4852],
    [4917, 4939, 4946, 4958, 4959, 4960, 4970, 4974, 5000, 5012, 5034],
    [5075, 5080, 5082, 5083, 5084, 5094, 5098, 5102, 5103, 5104, 5119, 5123, 5125, 5160, 5181, 5183, 5187, 5189],
    [5194, 5199, 5200, 5201, 5231, 5234, 5242, 5245, 5270],
    [5303, 5308, 5309, 5320, 5347, 5348, 5355, 5360, 5361, 5362, 5372, 5390, 5394, 5395],
    [5432, 5434, 5437, 5438, 5439, 5449, 5473, 5475, 5476, 5478, 5481, 5487, 5496, 5497, 5510, 5537],
    [5559, 5676, 5692, 5695, 5696, 5697, 5698, 5710, 5724, 5731, 5733, 5748, 5752, 5762],
    [5876, 5882, 5898, 5907, 5913, 5933, 5935, 5941],
    [5945, 5947, 5979, 5995, 5997, 6004, 6006, 6009, 6039, 6058, 6071],
    [6079, 6119, 6143],
    [6133, 6174, 6175, 6184, 6187, 6202, 6217, 6223],
    [6590, 6636, 6660, 6665],
    [6714, 6765, 6780],
    [6857, 6863],
    [7719, 7728],
    [9100, 10201, 10202, 10217, 10218, 10220, 10223, 10225, 10226, 10233, 10248, 10251, 10253, 10258, 10262, 10264, 10288, 10301, 10305, 10321, 10323, 10324, 10337],
    [9676, 9683, 9684, 9694, 9695, 9700, 9722, 9730, 9739, 9750, 9752, 9768, 9772, 9774, 9781, 9782, 9785, 9786, 9788, 9790, 9791, 9792, 9798],
    [13021, 13363, 13364, 13366, 13376, 13378, 13381, 13383, 13400, 13405, 13411, 13412, 13417, 13421, 13437, 13439, 13442, 13448, 13451, 13455, 13460, 13462, 13463, 13473, 13478, 13480, 13485, 13488, 13494, 13499, 13502, 13508, 13509],
    [13288, 13308, 13449, 13511, 13535, 13539, 13540, 13541, 13554, 13560, 13567, 13572, 13573, 13574, 13579, 13588, 13590, 13597, 13600, 13602, 13607, 13610, 13615, 13621, 13624, 13625, 13628, 13630, 13631, 13632, 13633, 13634, 13645, 13650, 13653, 13664, 13665, 13670, 13672, 13685, 13713],
    [14159, 14213, 14320, 14421, 14422, 14435, 14446, 14453, 14458, 14463, 14468, 14472, 14484, 14487, 14501, 14502, 14503, 14506, 14509, 14514],
    [14515, 14516, 14524, 14532, 14536, 14541, 14547, 14548, 14562, 14570, 14571, 14577, 14581, 14584, 14587, 14592, 14597, 14598, 14600, 14602, 14604, 14605, 14607, 14609, 14610, 14621, 14622, 14630, 14631, 14632, 14633, 14636, 14640, 14641, 14649, 14652, 14653, 14655, 14656, 14657, 14661, 14665, 14666, 14668],
    [16648, 16874, 17007, 17090, 17137, 17142, 17145, 17150, 17155, 17157, 17168, 17170, 17174, 17180, 17181, 17183, 17184, 17185, 17189, 17191, 17193, 17207, 17212, 17214, 17216, 17218, 17221, 17223, 17225, 17226, 17227, 17230, 17233, 17236, 17237, 17239, 17247, 17258, 17261, 17264],
    [18135, 18369, 18636, 18717, 18757, 18766, 18801, 18803, 18807, 18822, 18832, 18834, 18842, 18845, 18846, 18848, 18859, 18862, 18864, 18866, 18868, 18877, 18880, 18883, 18884, 18887, 18891, 18902, 18904, 18905, 18910, 18915, 18920, 18923, 18927],
    [20093, 20240, 20253, 20302, 20319, 20334, 20356, 20357, 20358, 20360, 20368, 20385, 20387, 20389, 20395, 20396, 20400, 20401, 20402, 20403, 20407, 20411, 20412, 20417, 20418, 20420, 20422, 20427, 20433, 20441, 20443, 20453, 20454, 20455, 20456, 20468, 20475, 20476, 20482],
    [21405, 21700, 21725, 21729, 21731, 21737, 21751, 21752, 21758, 21763, 21767, 21771, 21775, 21778, 21781, 21782, 21787, 21788, 21790, 21791, 21792, 21795, 21799, 21800, 21801, 21802, 21803, 21807, 21811, 21814, 21815, 21819, 21835, 21836, 21837, 21838, 21839, 21840, 21844, 21845, 21851, 21852, 21853, 21856, 21857, 21858, 21859, 21861, 21862, 21866, 21867, 21868, 21890],
    [34181, 34182, 34278, 34336, 34453, 34670, 34860, 34977, 34983, 35007, 35016, 35021, 35036, 35061, 35079, 35081, 35083, 35084, 35086, 35091, 35093, 35096, 35099, 35101, 35110, 35113, 35116, 35119, 35130, 35131, 35136, 35140, 35141, 35162, 35163, 35164, 35168, 35170, 35185, 35186, 35189, 35190, 35192, 35199, 35200],
    [35055, 35126, 35316, 35318, 35365, 35413, 35450, 35592, 35615, 35643, 35653, 35658, 35659, 35660, 35665, 35672, 35691, 35692, 35696, 35699, 35707, 35710, 35711, 35715, 35721, 35723, 35731, 35735, 35736, 35739, 35740, 35741, 35742, 35743, 35746, 35751, 35753, 35765],
    [35648, 35766, 35927, 35947, 35948, 35969, 35971, 35974, 35985, 35987, 36010, 36016, 36024, 36027, 36031, 36039, 36040, 36046, 36047, 36049, 36050, 36051, 36055, 36056, 36057, 36065, 36067, 36074, 36081, 36093, 36100],
    [36618, 37698, 37792, 37851, 37889, 37899, 37901, 37910, 37913, 37914, 37915, 37916, 37924, 37931, 37932, 37933, 37946, 37962, 37963, 37965, 37966, 37967, 37978, 37980, 37985, 37987, 37993, 37994, 38001, 38003, 38005, 38022, 38025, 38052],
    [38847, 39585, 39705, 39904, 39934, 39974, 39985, 39991, 39999, 40008, 40011, 40015, 40020, 40024, 40033, 40037, 40041, 40042, 40043, 40045, 40053, 40054, 40055, 40056, 40061, 40063, 40066, 40071, 40074, 40086, 40090, 40091, 40094, 40098],
    [39212, 39236, 39273, 39285, 39297, 39311, 39312, 39313, 39314, 39317, 39318, 39319, 39320, 39323, 39331, 39334, 39336, 39348, 39349, 39351, 39352, 39353, 39354, 39356, 39357, 39359, 39360, 39363, 39365, 39366, 39374, 39376, 39380, 39384, 39389, 39392, 39395],
    [39261, 39347, 39393, 39396, 39398, 39399, 39400, 39401, 39403, 39411, 39413, 39414, 39415, 39418, 39421, 39429, 39431, 39434, 39437, 39441, 39449, 39451, 39456, 39459, 39463, 39466, 39476, 39477, 39478, 39479, 39480, 39483, 39485, 39486, 39491, 39493, 39498, 39500, 39510],
]

for _bundle in _DBEAVER_BUNDLES:
    Instance.register("dbeaver", "-".join(str(_n) for _n in _bundle))(Dbeaver)
