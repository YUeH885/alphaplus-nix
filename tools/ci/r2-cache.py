#!/usr/bin/env python3
"""Publish non-upstream Nix closure paths to Cloudflare R2 and prune stale objects."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


# Nix's base32 alphabet omits e, o, t, u to avoid ambiguous or offensive words.
NIX32_ALPHABET = "0123456789abcdfghijklmnpqrsvwxyz"
UPSTREAM_SIG_PREFIX = "cache.nixos.org-1:"
CACHE_PRIORITY = 41
DELETE_BATCH_SIZE = 1000


class CacheError(RuntimeError):
    """Raised when exporting, publishing, or pruning the binary cache fails."""


@dataclass(frozen=True)
class PathMeta:
    store_path: str
    store_dir: str
    store_hash: str
    nar_hash: str
    nar_size: int
    references: tuple[str, ...]
    deriver: str | None
    ca: str | None
    signatures: tuple[str, ...]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def to_nix32(digest: bytes) -> str:
    """Encode raw hash bytes using Nix's reversed-bit nix32 format."""
    nix32_len = (len(digest) * 8 - 1) // 5 + 1
    out: list[str] = []
    for n in range(nix32_len - 1, -1, -1):
        b = n * 5
        i = b // 8
        j = b % 8
        c = digest[i] >> j
        if i + 1 < len(digest):
            c |= digest[i + 1] << (8 - j)
        out.append(NIX32_ALPHABET[c & 0x1F])
    return "".join(out)


def sha256_hash_to_nix32(value: str) -> str:
    """Convert an SRI ('sha256-...') or typed nix32 ('sha256:...') hash to nix32."""
    if value.startswith("sha256-"):
        raw = base64.b64decode(value[len("sha256-") :])
        if len(raw) != 32:
            raise CacheError(f"Unexpected SHA-256 byte length ({len(raw)}) in {value!r}")
        return to_nix32(raw)
    if value.startswith("sha256:"):
        rest = value[len("sha256:") :]
        if len(rest) == 52 and all(ch in NIX32_ALPHABET for ch in rest):
            return rest
        if len(rest) == 64:
            return to_nix32(bytes.fromhex(rest))
    raise CacheError(f"Unsupported SHA-256 hash format: {value!r}")


def store_prefix_args(store_uri: str | None) -> list[str]:
    return ["--store", store_uri] if store_uri else []


def run_nix(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nix", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _basename_in_store(value: str | None) -> str | None:
    if not value or value == "unknown-deriver":
        return None
    return os.path.basename(value.rstrip("/"))


def _format_ca(ca_value: object) -> str | None:
    if ca_value is None:
        return None
    if isinstance(ca_value, str):
        return ca_value
    if isinstance(ca_value, dict):
        method = ca_value.get("method")
        digest = ca_value.get("hash")
        if method == "nar" and isinstance(digest, str):
            return f"fixed:r:sha256:{sha256_hash_to_nix32(digest)}"
        if method == "flat" and isinstance(digest, str):
            return f"fixed:sha256:{sha256_hash_to_nix32(digest)}"
    raise CacheError(f"Unsupported CA metadata format: {ca_value!r}")


def query_path_info(
    paths: list[str],
    *,
    recursive: bool,
    store_uri: str | None = None,
) -> dict[str, PathMeta]:
    if not paths:
        return {}

    base_args = ["path-info", *store_prefix_args(store_uri)]
    if recursive:
        base_args.append("-r")

    proc = run_nix([*base_args, "--json", "--json-format", "1", *paths])
    if proc.returncode != 0 and "--json-format" in proc.stderr:
        # Older Nix releases only understand plain --json.
        proc = run_nix([*base_args, "--json", *paths])

    if proc.returncode != 0:
        raise CacheError(
            f"nix path-info failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )

    parsed = json.loads(proc.stdout)
    store_dir = "/nix/store"
    entries: dict[str, dict] = {}

    if isinstance(parsed, dict) and "info" in parsed and "storeDir" in parsed:
        store_dir = str(parsed["storeDir"]).rstrip("/")
        raw_info = parsed["info"]
        if not isinstance(raw_info, dict):
            raise CacheError("Unexpected nix path-info v2 payload")
        for key, item in raw_info.items():
            full = key if key.startswith("/") else f"{store_dir}/{key}"
            entries[full] = item
    elif isinstance(parsed, dict):
        for key, item in parsed.items():
            full = key if key.startswith("/") else f"{store_dir}/{key}"
            entries[full] = item
    elif isinstance(parsed, list):
        for item in parsed:
            full = item["path"]
            entries[full] = item
    else:
        raise CacheError(f"Unexpected nix path-info JSON type: {type(parsed).__name__}")

    result: dict[str, PathMeta] = {}
    for full_path, item in entries.items():
        base = os.path.basename(full_path.rstrip("/"))
        store_hash = base.split("-", 1)[0]
        if len(store_hash) != 32:
            raise CacheError(f"Invalid store path hash in {full_path!r}")
        refs = tuple(sorted(_basename_in_store(r) for r in (item.get("references") or []) if r))
        deriver = _basename_in_store(item.get("deriver"))
        result[full_path] = PathMeta(
            store_path=full_path,
            store_dir=store_dir,
            store_hash=store_hash,
            nar_hash=str(item["narHash"]),
            nar_size=int(item["narSize"]),
            references=refs,
            deriver=deriver,
            ca=_format_ca(item.get("ca")),
            signatures=tuple(str(s) for s in (item.get("signatures") or [])),
        )
    return result


def sign_paths(paths: list[str], signing_key_file: Path, store_uri: str | None) -> None:
    if not paths:
        return
    proc = run_nix(
        ["store", "sign", *store_prefix_args(store_uri), "--key-file", str(signing_key_file), "--stdin"],
        input_text="\n".join(paths) + "\n",
    )
    if proc.returncode != 0:
        raise CacheError(f"nix store sign failed (exit {proc.returncode}): {proc.stderr.strip()}")


def _compress_nar_one(meta: PathMeta, nar_dir: Path, store_uri: str | None) -> tuple[str, int, str]:
    fd, tmp_name = tempfile.mkstemp(prefix=".nar-", suffix=".nar.zst", dir=nar_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)

    dump_proc = subprocess.Popen(
        ["nix", "store", "dump-path", *store_prefix_args(store_uri), meta.store_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert dump_proc.stdout is not None
    zstd_proc = subprocess.Popen(
        ["zstd", "-3", "-c"],
        stdin=dump_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Allow dump_proc to receive SIGPIPE if zstd exits early.
    dump_proc.stdout.close()
    assert zstd_proc.stdout is not None

    hasher = hashlib.sha256()
    file_size = 0
    try:
        with tmp_path.open("wb") as out_f:
            while True:
                chunk = zstd_proc.stdout.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
                out_f.write(chunk)
                file_size += len(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        zstd_proc.kill()
        dump_proc.kill()
        raise
    finally:
        zstd_proc.stdout.close()

    zstd_err = (zstd_proc.stderr.read() if zstd_proc.stderr else b"").decode(errors="replace")
    dump_err = (dump_proc.stderr.read() if dump_proc.stderr else b"").decode(errors="replace")
    zstd_rc = zstd_proc.wait()
    dump_rc = dump_proc.wait()

    if dump_rc != 0 or zstd_rc != 0:
        tmp_path.unlink(missing_ok=True)
        raise CacheError(
            f"Failed dumping/compressing {meta.store_path} "
            f"(dump={dump_rc}: {dump_err.strip()}; zstd={zstd_rc}: {zstd_err.strip()})"
        )

    file_hash_nix32 = to_nix32(hasher.digest())
    nar_rel = f"nar/{file_hash_nix32}.nar.zst"
    nar_dest = nar_dir / f"{file_hash_nix32}.nar.zst"
    # Multiple store paths can theoretically share identical NAR bytes; keep existing file cleanly.
    if nar_dest.exists():
        tmp_path.unlink(missing_ok=True)
    else:
        os.chmod(tmp_path, 0o644)
        tmp_path.replace(nar_dest)
    return file_hash_nix32, file_size, nar_rel


def export_paths(
    paths: list[str],
    stage_dir: Path,
    signing_key_file: Path,
    key_name: str,
    *,
    store_uri: str | None = None,
) -> dict[str, str]:
    """Sign and export store paths into stage_dir without requiring references in stage_dir."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    nar_dir = stage_dir / "nar"
    nar_dir.mkdir(parents=True, exist_ok=True)

    (stage_dir / "nix-cache-info").write_text(
        f"StoreDir: /nix/store\nWantMassQuery: 1\nPriority: {CACHE_PRIORITY}\n",
        encoding="utf-8",
    )

    if not paths:
        return {}

    sign_paths(paths, signing_key_file, store_uri)
    metas = query_path_info(paths, recursive=False, store_uri=store_uri)
    missing_meta = [p for p in paths if p not in metas]
    if missing_meta:
        raise CacheError(f"nix path-info omitted signed paths: {missing_meta}")

    def export_one(path: str) -> tuple[str, str]:
        meta = metas[path]
        sig_prefix = f"{key_name}:"
        sigs = [s for s in meta.signatures if s.startswith(sig_prefix)]
        if not sigs:
            raise CacheError(f"No signature matching key '{key_name}' found on {path}")

        file_hash_nix32, file_size, nar_rel = _compress_nar_one(meta, nar_dir, store_uri)
        nar_hash_nix32 = sha256_hash_to_nix32(meta.nar_hash)

        lines = [
            f"StorePath: {meta.store_path}",
            f"URL: {nar_rel}",
            "Compression: zstd",
            f"FileHash: sha256:{file_hash_nix32}",
            f"FileSize: {file_size}",
            f"NarHash: sha256:{nar_hash_nix32}",
            f"NarSize: {meta.nar_size}",
        ]
        if meta.references:
            lines.append(f"References: {' '.join(meta.references)}")
        if meta.deriver:
            lines.append(f"Deriver: {meta.deriver}")
        for sig in sigs:
            lines.append(f"Sig: {sig}")
        if meta.ca:
            lines.append(f"CA: {meta.ca}")

        narinfo_rel = f"{meta.store_hash}.narinfo"
        (stage_dir / narinfo_rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return narinfo_rel, nar_rel

    workers = min(8, os.cpu_count() or 4, len(paths))
    exported: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for narinfo_rel, nar_rel in pool.map(export_one, paths):
            exported[narinfo_rel] = nar_rel
    return exported


def _aws_env() -> dict[str, str]:
    env = os.environ.copy()
    # Cloudflare R2 rejects AWS CLI v2.23+ default trailing/flexible checksums.
    env.setdefault("AWS_DEFAULT_REGION", "auto")
    env["AWS_REQUEST_CHECKSUM_CALCULATION"] = "when_required"
    env["AWS_RESPONSE_CHECKSUM_VALIDATION"] = "when_required"
    return env


def _run_aws(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["aws", *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def list_r2_keys(endpoint: str, bucket: str) -> set[str]:
    env = _aws_env()
    keys: set[str] = set()
    token: str | None = None
    while True:
        args = [
            "s3api",
            "list-objects-v2",
            "--endpoint-url",
            endpoint,
            "--bucket",
            bucket,
        ]
        if token:
            args.extend(["--continuation-token", token])
        proc = _run_aws(args, env=env)
        if proc.returncode != 0:
            raise CacheError(f"list-objects-v2 failed: {proc.stderr.strip()}")
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        for item in payload.get("Contents") or []:
            keys.add(str(item["Key"]))
        if not payload.get("IsTruncated"):
            break
        token = payload.get("NextContinuationToken")
        if not token:
            break
    return keys


def parse_narinfo(text: str) -> tuple[str | None, list[str]]:
    url: str | None = None
    sigs: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("URL:"):
            url = line[len("URL:") :].strip()
        elif line.startswith("Sig:"):
            sigs.append(line[len("Sig:") :].strip())
    return url, sigs


def fetch_narinfo(endpoint: str, bucket: str, public_url: str | None, key: str) -> str | None:
    if public_url:
        req = urllib.request.Request(
            f"{public_url.rstrip('/')}/{key}",
            headers={"User-Agent": "alphaplus-r2-cache/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return None
            log(f"Warning: HTTP {exc.code} fetching {key} from public URL, falling back to S3 API")
        except urllib.error.URLError as exc:
            log(f"Warning: HTTP error fetching {key} ({exc.reason}), falling back to S3 API")

    env = _aws_env()
    proc = _run_aws(["s3", "cp", "--endpoint-url", endpoint, f"s3://{bucket}/{key}", "-"], env=env)
    if proc.returncode != 0:
        return None
    return proc.stdout


def put_object(
    endpoint: str,
    bucket: str,
    key: str,
    local_path: Path,
    *,
    content_type: str,
    cache_control: str,
) -> None:
    env = _aws_env()
    proc = _run_aws(
        [
            "s3api",
            "put-object",
            "--endpoint-url",
            endpoint,
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(local_path),
            "--content-type",
            content_type,
            "--cache-control",
            cache_control,
        ],
        env=env,
    )
    if proc.returncode != 0:
        raise CacheError(f"put-object failed for {key}: {proc.stderr.strip()}")


def delete_keys_batched(endpoint: str, bucket: str, keys: list[str]) -> None:
    if not keys:
        return
    env = _aws_env()
    for start in range(0, len(keys), DELETE_BATCH_SIZE):
        batch = keys[start : start + DELETE_BATCH_SIZE]
        payload = {"Objects": [{"Key": k} for k in batch], "Quiet": True}
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tf:
            json.dump(payload, tf)
            tmp_path = Path(tf.name)
        try:
            proc = _run_aws(
                [
                    "s3api",
                    "delete-objects",
                    "--endpoint-url",
                    endpoint,
                    "--bucket",
                    bucket,
                    "--delete",
                    f"file://{tmp_path}",
                ],
                env=env,
            )
            if proc.returncode != 0:
                raise CacheError(f"delete-objects failed: {proc.stderr.strip()}")
            if proc.stdout.strip():
                resp = json.loads(proc.stdout)
                errors = resp.get("Errors") or []
                if errors:
                    raise CacheError(f"delete-objects returned errors: {errors[:5]}")
        finally:
            tmp_path.unlink(missing_ok=True)


def plan_prune(existing_keys: set[str], manifest_key_lists: list[list[str]]) -> tuple[list[str], list[str]]:
    """Compute ordered stale keys to delete (narinfo first, then remaining objects)."""
    active: set[str] = {"nix-cache-info"}
    for key_list in manifest_key_lists:
        active.update(key_list)
    if not any(k.endswith(".narinfo") for k in active):
        raise CacheError("Refusing to prune: active manifest set contains no .narinfo entries")

    stale = sorted(existing_keys - active)
    stale_narinfo = [k for k in stale if k.endswith(".narinfo")]
    stale_other = [k for k in stale if not k.endswith(".narinfo")]
    return stale_narinfo, stale_other


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CacheError(f"Missing required environment variable: {name}")
    return value


def read_root_paths(paths_file: str | None, extra_paths: list[str]) -> list[str]:
    roots: list[str] = []
    if paths_file:
        for line in Path(paths_file).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                roots.append(stripped)
    roots.extend(p.strip() for p in extra_paths if p.strip())
    deduped = list(dict.fromkeys(roots))
    if not deduped:
        raise CacheError("No store paths provided to publish")
    return deduped


def filter_non_upstream(closure: dict[str, PathMeta]) -> list[PathMeta]:
    selected: list[PathMeta] = []
    for path in sorted(closure):
        meta = closure[path]
        if any(sig.startswith(UPSTREAM_SIG_PREFIX) for sig in meta.signatures):
            continue
        selected.append(meta)
    return selected


def cmd_publish(args: argparse.Namespace) -> int:
    endpoint = require_env("R2_ENDPOINT")
    bucket = require_env("R2_BUCKET")
    signing_key = require_env("NIX_SIGNING_KEY")
    public_key = require_env("NIX_PUBLIC_KEY")
    public_url = os.environ.get("R2_PUBLIC_URL", "").strip() or None

    if ":" not in public_key:
        raise CacheError(f"Invalid NIX_PUBLIC_KEY format: {public_key!r}")
    key_name = public_key.split(":", 1)[0]

    roots = read_root_paths(args.paths_file, args.paths)
    closure = query_path_info(roots, recursive=True)
    targets = filter_non_upstream(closure)
    log(f"Closure has {len(closure)} paths; {len(targets)} non-upstream paths selected")

    if not targets:
        raise CacheError("No non-upstream store paths found in closure; refusing to emit empty manifest")

    existing_keys = list_r2_keys(endpoint, bucket)
    log(f"R2 bucket '{bucket}' currently contains {len(existing_keys)} objects")

    candidates = [m for m in targets if f"{m.store_hash}.narinfo" in existing_keys]
    active_keys: set[str] = set()
    to_export: list[PathMeta] = []

    def check_cached(meta: PathMeta) -> tuple[PathMeta, str | None]:
        narinfo_key = f"{meta.store_hash}.narinfo"
        text = fetch_narinfo(endpoint, bucket, public_url, narinfo_key)
        if not text:
            return meta, None
        nar_url, sigs = parse_narinfo(text)
        if not nar_url or nar_url not in existing_keys:
            return meta, None
        if not any(s.startswith(f"{key_name}:") for s in sigs):
            return meta, None
        return meta, nar_url

    if candidates:
        with ThreadPoolExecutor(max_workers=min(16, len(candidates))) as pool:
            for meta, cached_nar in pool.map(check_cached, candidates):
                if cached_nar:
                    active_keys.add(f"{meta.store_hash}.narinfo")
                    active_keys.add(cached_nar)
                else:
                    to_export.append(meta)

    candidate_hashes = {m.store_hash for m in candidates}
    for meta in targets:
        if meta.store_hash not in candidate_hashes:
            to_export.append(meta)

    log(f"Cache hit: {len(targets) - len(to_export)}; exporting and uploading: {len(to_export)}")

    with tempfile.TemporaryDirectory(prefix="r2-cache-stage-") as tmp_dir_str:
        stage_dir = Path(tmp_dir_str)
        key_fd, key_tmp = tempfile.mkstemp(prefix="nix-sign-", dir=stage_dir)
        key_path = Path(key_tmp)
        try:
            os.fchmod(key_fd, 0o600)
            with os.fdopen(key_fd, "w", encoding="utf-8") as kf:
                kf.write(signing_key.rstrip("\n") + "\n")

            exported = export_paths(
                [m.store_path for m in to_export],
                stage_dir,
                key_path,
                key_name,
            )
        finally:
            key_path.unlink(missing_ok=True)

        unique_nars = sorted({nar_rel for nar_rel in exported.values() if nar_rel not in existing_keys})
        if unique_nars:
            log(f"Uploading {len(unique_nars)} NAR object(s)...")
            with ThreadPoolExecutor(max_workers=min(8, len(unique_nars))) as pool:
                list(
                    pool.map(
                        lambda rel: put_object(
                            endpoint,
                            bucket,
                            rel,
                            stage_dir / rel,
                            content_type="application/x-nix-nar",
                            cache_control="public, max-age=31536000, immutable",
                        ),
                        unique_nars,
                    )
                )

        narinfo_keys = sorted(exported)
        if narinfo_keys:
            log(f"Uploading {len(narinfo_keys)} .narinfo object(s)...")
            with ThreadPoolExecutor(max_workers=min(16, len(narinfo_keys))) as pool:
                list(
                    pool.map(
                        lambda rel: put_object(
                            endpoint,
                            bucket,
                            rel,
                            stage_dir / rel,
                            content_type="text/x-nix-narinfo",
                            cache_control="public, max-age=300",
                        ),
                        narinfo_keys,
                    )
                )

        # Always ensure nix-cache-info matches Priority/WantMassQuery settings.
        put_object(
            endpoint,
            bucket,
            "nix-cache-info",
            stage_dir / "nix-cache-info",
            content_type="text/plain; charset=utf-8",
            cache_control="public, max-age=300",
        )

        for narinfo_rel, nar_rel in exported.items():
            active_keys.add(narinfo_rel)
            active_keys.add(nar_rel)

    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"keys": sorted(active_keys)}, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"Wrote manifest with {len(active_keys)} keys to {manifest_path}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    endpoint = require_env("R2_ENDPOINT")
    bucket = require_env("R2_BUCKET")

    manifest_key_lists: list[list[str]] = []
    for path_str in args.manifests:
        p = Path(path_str)
        if not p.is_file():
            raise CacheError(f"Manifest file not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        keys = data.get("keys")
        if not isinstance(keys, list):
            raise CacheError(f"Invalid manifest payload in {p}")
        manifest_key_lists.append([str(k) for k in keys])

    existing_keys = list_r2_keys(endpoint, bucket)
    stale_narinfo, stale_other = plan_prune(existing_keys, manifest_key_lists)
    total_stale = len(stale_narinfo) + len(stale_other)
    if total_stale == 0:
        log("No stale objects to prune")
        return 0

    log(f"Pruning {len(stale_narinfo)} stale .narinfo and {len(stale_other)} stale NAR/other objects...")
    # Delete .narinfo metadata before NAR payloads so readers never observe dangling metadata.
    delete_keys_batched(endpoint, bucket, stale_narinfo)
    delete_keys_batched(endpoint, bucket, stale_other)
    log(f"Pruned {total_stale} stale objects")
    return 0


def cmd_export_local(args: argparse.Namespace) -> int:
    dest = Path(args.dest)
    exported = export_paths(
        args.paths,
        dest,
        Path(args.key_file),
        args.key_name,
        store_uri=args.store,
    )
    print(json.dumps(exported, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pub = sub.add_parser("publish", help="Publish non-upstream closure paths to R2")
    p_pub.add_argument("--manifest", required=True, help="Output JSON manifest path")
    p_pub.add_argument("--paths-file", help="File containing newline-separated root store paths")
    p_pub.add_argument("paths", nargs="*", help="Additional root store paths")
    p_pub.set_defaults(func=cmd_publish)

    p_prune = sub.add_parser("prune", help="Delete R2 objects absent from active manifests")
    p_prune.add_argument("manifests", nargs="+", help="Active manifest JSON files")
    p_prune.set_defaults(func=cmd_prune)

    p_exp = sub.add_parser("export-local", help="Export signed cache files to a local directory")
    p_exp.add_argument("--dest", required=True, help="Destination binary cache directory")
    p_exp.add_argument("--key-file", required=True, help="Secret signing key file")
    p_exp.add_argument("--key-name", required=True, help="Signing key name")
    p_exp.add_argument("--store", help="Optional Nix store URI")
    p_exp.add_argument("paths", nargs="+", help="Store paths to export")
    p_exp.set_defaults(func=cmd_export_local)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CacheError as exc:
        log(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
