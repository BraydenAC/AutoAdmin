"""Resolving Minecraft, Java, Fabric, and NeoForge versions.

All of these read large upstream listings in code and return only the few
fields the agent needs.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .downloads import get_json, get_text

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
ADOPTIUM_ASSET = ("https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
                  "?architecture=x64&image_type=jre&os=linux&vendor=eclipse")
FABRIC_META = "https://meta.fabricmc.net/v2"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"

_manifest: tuple[float, dict] | None = None
MANIFEST_TTL = 600


class VersionError(Exception):
    pass


@dataclass(frozen=True)
class MinecraftVersion:
    id: str
    type: str          # release | snapshot | old_beta | old_alpha
    java_major: int
    server_url: str
    server_sha1: str


async def _manifest_json() -> dict:
    global _manifest
    if _manifest and time.time() - _manifest[0] < MANIFEST_TTL:
        return _manifest[1]
    data = await get_json(MOJANG_MANIFEST)
    _manifest = (time.time(), data)
    return data


async def resolve_minecraft(version: str) -> MinecraftVersion:
    """`version` may be an id like '1.21.1', or 'latest' / 'latest-snapshot'."""
    m = await _manifest_json()
    if version in ("latest", "latest-release"):
        version = m["latest"]["release"]
    elif version == "latest-snapshot":
        version = m["latest"]["snapshot"]
    entry = next((v for v in m["versions"] if v["id"] == version), None)
    if entry is None:
        close = [v["id"] for v in m["versions"] if v["type"] == "release"
                 and v["id"].startswith(version.rsplit(".", 1)[0])][:5]
        hint = f" Similar releases: {', '.join(close)}." if close else ""
        raise VersionError(f"Minecraft version '{version}' does not exist.{hint}")
    detail = await get_json(entry["url"])
    server = detail.get("downloads", {}).get("server")
    if not server:
        raise VersionError(f"Minecraft {version} has no dedicated server download.")
    java = detail.get("javaVersion", {}).get("majorVersion", 8)
    return MinecraftVersion(entry["id"], entry["type"], java, server["url"], server["sha1"])


@dataclass(frozen=True)
class JavaRuntime:
    major: int
    release: str
    url: str
    sha256: str


async def resolve_java(major: int) -> JavaRuntime:
    assets = await get_json(ADOPTIUM_ASSET.format(major=major))
    if not assets:
        raise VersionError(f"No Temurin JRE {major} build for linux x64.")
    a = assets[0]
    pkg = a["binary"]["package"]
    return JavaRuntime(major, a["release_name"], pkg["link"], pkg["checksum"])


# -- Fabric ------------------------------------------------------------------

@dataclass(frozen=True)
class FabricBuild:
    loader: str
    installer: str
    url: str


async def resolve_fabric(mc: str, loader: str | None = None) -> FabricBuild:
    loaders = await get_json(f"{FABRIC_META}/versions/loader/{mc}")
    if not loaders:
        raise VersionError(f"Fabric does not support Minecraft {mc}.")
    available = [x["loader"]["version"] for x in loaders]
    if loader is not None and loader not in available:
        raise VersionError(f"Fabric loader {loader} is not available for Minecraft {mc}.")
    loader = loader or next((x["loader"]["version"] for x in loaders if x["loader"]["stable"]),
                            available[0])
    installers = await get_json(f"{FABRIC_META}/versions/installer")
    installer = next(x["version"] for x in installers if x["stable"])
    url = f"{FABRIC_META}/versions/loader/{mc}/{loader}/{installer}/server/jar"
    return FabricBuild(loader, installer, url)


# -- NeoForge ----------------------------------------------------------------

def neoforge_prefix(mc: str) -> str:
    """Map a Minecraft version to the NeoForge version prefix.

    1.21.1 -> '21.1.'   1.21 -> '21.0.'   26.3 -> '26.3.0.'   26.1.2 -> '26.1.2.'
    """
    parts = mc.split(".")
    if parts[0] == "1":
        if len(parts) not in (2, 3):
            raise VersionError(f"Unrecognised Minecraft version '{mc}'.")
        return f"{parts[1]}.{parts[2] if len(parts) == 3 else 0}."
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise VersionError(f"Unrecognised Minecraft version '{mc}'.")
    return ".".join(parts) + "."


def _num_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0]))


def pick_neoforge(versions: list[str], mc: str) -> str:
    prefix = neoforge_prefix(mc)
    matching = [v for v in versions if v.startswith(prefix)]
    if not matching:
        raise VersionError(f"NeoForge has no build for Minecraft {mc}.")
    stable = [v for v in matching if "-" not in v]
    return max(stable or matching, key=_num_key)


@dataclass(frozen=True)
class NeoForgeBuild:
    version: str
    installer_url: str
    installer_sha1: str


async def resolve_neoforge(mc: str, version: str | None = None) -> NeoForgeBuild:
    xml = await get_text(f"{NEOFORGE_MAVEN}/maven-metadata.xml")
    available = re.findall(r"<version>([^<]+)</version>", xml)
    if version is not None and version not in available:
        raise VersionError(f"NeoForge {version} does not exist.")
    version = version or pick_neoforge(available, mc)
    url = f"{NEOFORGE_MAVEN}/{version}/neoforge-{version}-installer.jar"
    sha1 = (await get_text(url + ".sha1")).strip().split()[0]
    return NeoForgeBuild(version, url, sha1)
