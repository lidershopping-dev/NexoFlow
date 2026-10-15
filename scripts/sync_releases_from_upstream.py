#!/usr/bin/env python3
"""Espelha releases publicos do NexoFlow original com verificacao SHA-256.

Executar somente no GitHub Actions do fork lidershopping-dev/NexoFlow.
Nunca altera a branch main, nem substitui tags existentes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

SOURCE = "felipetelesbarbosa/NexoFlow"
DEST = "lidershopping-dev/NexoFlow"
API = "https://api.github.com/repos/"
ASSET_PREFIX = f"https://github.com/{SOURCE}/releases/download/"
TOKEN = os.environ["GH_TOKEN"]


def github_api(path, *, missing_ok=False):
    request = Request(
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "NexoFlow-release-sync",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise


def command(*args, check=True):
    result = subprocess.run(
        [str(item) for item in args], text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        raise RuntimeError(
            f"Comando falhou ({result.returncode}): {args[0]}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def valid_tag(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,100}", tag):
        raise ValueError(f"Tag nao suportada: {tag!r}")
    if ".." in tag or "@{" in tag or tag.endswith((".", "/", ".lock")):
        raise ValueError(f"Tag invalida: {tag!r}")
    return tag


def download_verified(asset, destination):
    name = asset["name"]
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"Nome de arquivo invalido: {name!r}")
    url = asset["browser_download_url"]
    if not url.startswith(ASSET_PREFIX):
        raise ValueError(f"URL de asset inesperada: {name!r}")
    digest = asset.get("digest") or ""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError(f"SHA-256 oficial ausente para {name}")
    target = destination / name
    sha = hashlib.sha256()
    length = 0
    with urlopen(Request(url, headers={"User-Agent": "NexoFlow-release-sync"}), timeout=120) as src:
        with target.open("wb") as output:
            while block := src.read(1024 * 1024):
                output.write(block)
                sha.update(block)
                length += len(block)
    if sha.hexdigest() != digest[7:] or length != asset["size"]:
        target.unlink(missing_ok=True)
        raise ValueError(f"Falha de integridade SHA-256/tamanho: {name}")
    print(f"Verificado: {name} ({length} bytes)", flush=True)
    return target


def ensure_tag(tag):
    command("git", "remote", "add", "upstream", f"https://github.com/{SOURCE}.git", check=False)
    command("git", "fetch", "--no-tags", "upstream", f"refs/tags/{tag}")
    commit_sha = command("git", "rev-parse", "FETCH_HEAD^{commit}")
    remote_ref = command("git", "ls-remote", "--refs", "--tags", "origin", f"refs/tags/{tag}")
    if remote_ref:
        existing_sha = remote_ref.split()[0]
        if existing_sha != commit_sha:
            raise ValueError(f"A tag {tag} ja existe no fork em outro objeto; nao sera substituida.")
    else:
        command("git", "push", "origin", f"{commit_sha}:refs/tags/{tag}")
        print(f"Tag {tag} criada no fork", flush=True)


def signature(release):
    return sorted((x["name"], x.get("digest"), x["size"]) for x in release["assets"])


def sync_release(source):
    tag = valid_tag(source["tag_name"])
    existing = github_api(f"{DEST}/releases/tags/{quote(tag, safe='')}", missing_ok=True)
    unchanged = (
        existing is not None
        and existing.get("name") == source.get("name")
        and (existing.get("body") or "").strip() == (source.get("body") or "").strip()
        and existing.get("prerelease") == source.get("prerelease")
        and signature(existing) == signature(source)
    )
    if unchanged:
        print(f"Release {tag}: identico, nenhuma alteracao", flush=True)
        return

    with tempfile.TemporaryDirectory(prefix="nexoflow-release-") as directory:
        base = Path(directory)
        src_assets = source["assets"]
        dest_assets = {asset["name"]: asset for asset in existing["assets"]} if existing else {}
        files = [
            download_verified(asset, base)
            for asset in src_assets
            if not existing or (
                asset["name"] not in dest_assets
                or dest_assets[asset["name"]].get("digest") != asset.get("digest")
                or dest_assets[asset["name"]].get("size") != asset.get("size")
            )
        ]
        notes_file = base / "RELEASE_NOTES.md"
        notes_file.write_text(source.get("body") or "", encoding="utf-8")
        ensure_tag(tag)
        title = source.get("name") or tag
        if not existing:
            args = [
                "gh", "release", "create", tag, *files,
                "--repo", DEST, "--title", title,
                "--notes-file", notes_file, "--verify-tag",
            ]
            if source["prerelease"]:
                args.append("--prerelease")
            print(command(*args), flush=True)
        else:
            if files:
                print(command(
                    "gh", "release", "upload", tag, *files,
                    "--repo", DEST, "--clobber"
                ), flush=True)
            args = [
                "gh", "release", "edit", tag, "--repo", DEST,
                "--title", title, "--notes-file", notes_file,
            ]
            if existing.get("prerelease") != source.get("prerelease"):
                args.append(f"--prerelease={str(bool(source['prerelease'])).lower()}")
            print(command(*args), flush=True)
        print(f"Release {tag} sincronizado: https://github.com/{DEST}/releases/tag/{tag}", flush=True)


def main():
    if os.environ.get("GITHUB_REPOSITORY") != DEST:
        raise RuntimeError("Este script so pode rodar no fork autorizado.")
    page = 1
    while True:
        releases = github_api(f"{SOURCE}/releases?per_page=100&page={page}")
        for release in reversed(releases):
            if not release["draft"]:
                sync_release(release)
        if len(releases) < 100:
            break
        page += 1


if __name__ == "__main__":
    main()
