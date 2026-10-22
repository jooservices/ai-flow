"""Encrypted control-plane backups and restore primitives.

The bundle keeps the database dump, stored secrets, control-repository settings and knowledge snapshot as
independently checked components. A one-time 256-bit key is kept outside the bundle and supplied by the owner.
"""
from __future__ import annotations

import hashlib
import hmac
import httpx
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from cryptography.exceptions import InvalidTag

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg import sql

MAGIC = b"AIFLOW4BK1"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
BACKUP_RE = re.compile(r"^aiflow4-(\d{8}T\d{6}Z)-[a-f0-9]{8}\.aiflow4$")
KNOWLEDGE_TABLES = ("managed_resources", "managed_resource_history", "managed_resource_proposals", "memory",
                    "project_reports")


class BackupError(RuntimeError):
    """A backup cannot safely be created, read, or restored."""


def _safe_json(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_key(path: Path, key: bytes, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        if path.exists() and not replace:
            raise FileExistsError(path)
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def create_key_file(path: str | Path) -> bytes:
    """Create the once-downloadable key; refuse to replace a key or follow a final symlink."""
    target = Path(path)
    key = secrets.token_bytes(KEY_BYTES)
    _write_key(target, key)
    return key


def read_key(path: str | Path) -> bytes:
    try:
        key = Path(path).read_bytes()
    except OSError as exc:
        raise BackupError("the backup key file could not be read") from exc
    if len(key) != KEY_BYTES:
        raise BackupError("the backup key is invalid")
    return key


def install_key(path: str | Path, key: bytes) -> None:
    """Put the owner's supplied key on a restored machine with private permissions."""
    if Path(path).exists():
        if not hmac.compare_digest(read_key(path), key):
            raise BackupError("the installed backup key does not match the restore key")
        return
    _write_key(Path(path), key)


def encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise BackupError("the backup key is invalid")
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as src, destination.open("wb") as dst:
        os.chmod(destination, 0o600)
        dst.write(header)
        while chunk := src.read(1024 * 1024):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)
        dst.flush()
        os.fsync(dst.fileno())


def decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise BackupError("the backup key is invalid")
    size = source.stat().st_size
    header_size = len(MAGIC) + NONCE_BYTES
    if size < header_size + TAG_BYTES:
        raise BackupError("the backup file is incomplete")
    with source.open("rb") as src:
        header = src.read(header_size)
        if not header.startswith(MAGIC):
            raise BackupError("the backup format is not recognized")
        nonce = header[len(MAGIC):]
        src.seek(size - TAG_BYTES)
        tag = src.read(TAG_BYTES)
        src.seek(header_size)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = size - header_size - TAG_BYTES
        try:
            with destination.open("wb") as dst:
                os.chmod(destination, 0o600)
                while remaining:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise BackupError("the backup file is incomplete")
                    remaining -= len(chunk)
                    dst.write(decryptor.update(chunk))
                dst.write(decryptor.finalize())
                dst.flush()
                os.fsync(dst.fileno())
        except InvalidTag as exc:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise BackupError("backup decryption failed: the key is incorrect or the backup is damaged") from exc
        except Exception:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise


def _add_tree(tar: tarfile.TarFile, source: Path, prefix: str, excluded: set[Path] | None = None) -> None:
    excluded = excluded or set()
    if not source.is_dir():
        return
    for item in sorted(source.rglob("*")):
        try:
            relative = item.relative_to(source)
        except ValueError:
            continue
        if any(part == ".git" for part in relative.parts) or item in excluded or item.is_symlink():
            continue
        arcname = (PurePosixPath(prefix) / PurePosixPath(relative.as_posix())).as_posix()
        if item.is_dir():
            info = tarfile.TarInfo(arcname.rstrip("/") + "/")
            info.type, info.mode = tarfile.DIRTYPE, 0o700
            tar.addfile(info)
        elif item.is_file():
            info = tar.gettarinfo(str(item), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode &= 0o700
            with item.open("rb") as stream:
                tar.addfile(info, stream)


def _secret_roots(settings) -> list[tuple[str, Path]]:
    roots = [("control", Path(settings.control_secrets_dir)), ("console", Path(settings.console_secrets_dir)),
             ("jira", Path(settings.jira_secret_dir)), ("github", Path(settings.github_app_dir))]
    return roots


def _knowledge_rows(database_url: str) -> dict[str, list[dict]]:
    result = {}
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        for table in KNOWLEDGE_TABLES:
            result[table] = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()]
    return result


def _knowledge_bytes(database_url: str) -> bytes:
    return json.dumps(_knowledge_rows(database_url), ensure_ascii=False, sort_keys=True, default=_safe_json,
                      separators=(",", ":")).encode("utf-8")


def _run_db_command(command: list[str], label: str, timeout: int = 3600,
                    env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
                       env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"{label} failed") from exc


def _pgpass_field(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise BackupError("database credentials cannot be passed to PostgreSQL tools")
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _database_cli_connection(database_url: str):
    """Yield a password-free libpq connection string and a private PGPASSFILE environment when needed."""
    try:
        fields = conninfo_to_dict(database_url)
        password = fields.pop("password", None)
        safe_conninfo = make_conninfo(**fields)
    except (psycopg.Error, TypeError, ValueError) as exc:
        raise BackupError("database connection settings are invalid") from exc
    if not password:
        yield safe_conninfo, None
        return

    host = fields.get("host") or "localhost"
    port = fields.get("port") or "*"
    database = fields.get("dbname") or "*"
    user = fields.get("user") or "*"
    passfile_fd, passfile_name = tempfile.mkstemp(prefix="aiflow4-pgpass-")
    os.close(passfile_fd)
    passfile = Path(passfile_name)
    try:
        os.chmod(passfile, 0o600)
        passfile.write_text(":".join(_pgpass_field(str(value)) for value in
                                     (host, port, database, user, password)) + "\n", encoding="utf-8")
        env = os.environ.copy()
        env.pop("PGPASSWORD", None)
        env["PGPASSFILE"] = str(passfile)
        yield safe_conninfo, env
    finally:
        passfile.unlink(missing_ok=True)


def create_archive(database_url: str, repo_root: str | Path, settings, target: Path,
                   created_at: datetime | None = None) -> dict:
    """Create the plaintext component tarball. The caller immediately encrypts and removes it."""
    created_at = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="aiflow4-backup-") as tmp:
        tmpdir = Path(tmp)
        database = tmpdir / "database.dump"
        with _database_cli_connection(database_url) as (safe_conninfo, env):
            _run_db_command(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(database),
                             "--dbname", safe_conninfo], "database backup", env=env)
        repo_archive = tmpdir / "control-repository.tar.gz"
        with tarfile.open(repo_archive, "w:gz") as tar:
            _add_tree(tar, Path(repo_root), "repository")
        secret_archive = tmpdir / "stored-secrets.tar.gz"
        excluded = {Path(settings.backup_key_file).absolute()}
        with tarfile.open(secret_archive, "w:gz") as tar:
            for label, root in _secret_roots(settings):
                _add_tree(tar, root, label, excluded)
        knowledge = tmpdir / "knowledge.json"
        knowledge.write_bytes(_knowledge_bytes(database_url))
        components = {"database": database, "stored_secrets": secret_archive,
                      "control_repository": repo_archive, "knowledge": knowledge}
        manifest = {
            "format": 1,
            "created_at": created_at.isoformat(timespec="seconds"),
            "components": {name: {"file": file.name, "sha256": _file_hash(file),
                                  "bytes": file.stat().st_size} for name, file in components.items()},
        }
        with tarfile.open(target, "w:gz") as tar:
            for file in components.values():
                tar.add(file, arcname=file.name, recursive=False)
            raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(raw)
            info.mode = 0o600
            import io
            tar.addfile(info, io.BytesIO(raw))
        os.chmod(target, 0o600)
        return manifest


class S3CompatibleStore:
    """Small path-style S3 client using SigV4 and the control process AWS environment credentials."""

    def __init__(self, endpoint: str, bucket: str, region: str = "us-east-1") -> None:
        parsed = urlsplit(endpoint.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BackupError("the S3 endpoint is invalid")
        self.endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.bucket = bucket.strip()
        self.region = region.strip() or os.environ.get("AWS_REGION") or "us-east-1"
        self.access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = os.environ.get("AWS_SESSION_TOKEN", "")
        if not self.bucket or not self.access_key or not self.secret_key:
            raise BackupError("S3 destination needs a bucket and AWS credentials in the control environment")

    def _signed(self, method: str, path: str, query: dict[str, str], payload_hash: str,
                extra: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(self.endpoint)
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        endpoint_path = parsed.path.rstrip("/")
        canonical_uri = endpoint_path + "/" + "/".join(quote(part, safe="-_.~") for part in path.strip("/").split("/"))
        canonical_query = urlencode(sorted(query.items()), quote_via=quote, safe="-_.~")
        headers = {"host": parsed.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        headers.update({k.lower(): v.strip() for k, v in (extra or {}).items()})
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical = "\n".join((method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash))
        scope = f"{day}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(("AWS4-HMAC-SHA256", amz_date, scope,
                                     hashlib.sha256(canonical.encode()).hexdigest()))

        def sign(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()

        signing_key = sign(sign(sign(sign(("AWS4" + self.secret_key).encode(), day), self.region), "s3"), "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                                     f"SignedHeaders={signed_headers}, Signature={signature}")
        for name in ("host",):
            headers.pop(name, None)  # httpx supplies Host from the URL.
        url = parsed.scheme + "://" + parsed.netloc + canonical_uri + ("?" + canonical_query if canonical_query else "")
        return url, {name: value for name, value in headers.items() if name != "host"}

    def _object_path(self, key: str) -> str:
        return f"{self.bucket}/{key.lstrip('/')}"

    def put(self, key: str, file: Path) -> None:
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        url, headers = self._signed("PUT", self._object_path(key), {}, digest.hexdigest(),
                                    {"content-type": "application/octet-stream"})
        try:
            with file.open("rb") as stream:
                response = httpx.put(url, content=stream, headers=headers, timeout=300)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackupError("could not write backup to S3-compatible storage") from exc

    def get(self, key: str, file: Path) -> None:
        url, headers = self._signed("GET", self._object_path(key), {}, hashlib.sha256(b"").hexdigest())
        try:
            with httpx.stream("GET", url, headers=headers, timeout=300) as response:
                response.raise_for_status()
                with file.open("wb") as stream:
                    os.chmod(file, 0o600)
                    for chunk in response.iter_bytes(1024 * 1024):
                        stream.write(chunk)
        except httpx.HTTPError as exc:
            raise BackupError("could not read backup from S3-compatible storage") from exc

    def delete(self, key: str) -> None:
        url, headers = self._signed("DELETE", self._object_path(key), {}, hashlib.sha256(b"").hexdigest())
        try:
            response = httpx.delete(url, headers=headers, timeout=30)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackupError("could not remove an expired S3 backup") from exc

    def list(self, prefix: str) -> list[str]:
        query = {"list-type": "2", "prefix": prefix.strip("/") + ("/" if prefix.strip("/") else "")}
        continuation = ""
        found: list[str] = []
        try:
            while True:
                current = dict(query)
                if continuation:
                    current["continuation-token"] = continuation
                url, headers = self._signed("GET", self.bucket, current, hashlib.sha256(b"").hexdigest())
                response = httpx.get(url, headers=headers, timeout=60)
                response.raise_for_status()
                root = ET.fromstring(response.content)
                found.extend(item.text or "" for item in root.findall(".//{*}Contents/{*}Key"))
                if root.findtext(".//{*}IsTruncated", "false").lower() != "true":
                    return found
                continuation = root.findtext(".//{*}NextContinuationToken", "")
                if not continuation:
                    return found
        except (httpx.HTTPError, ET.ParseError) as exc:
            raise BackupError("could not list S3-compatible backups") from exc


def destination_for(config: dict) -> tuple[str, str]:
    kind = config.get("destination_kind", "local")
    if kind in {"local", "mounted"}:
        return kind, str(config.get("destination_path") or "/srv/aiflow/backups")
    if kind == "s3":
        return kind, str(config.get("s3_prefix") or "")
    raise BackupError("backup destination is invalid")


def _s3_store(config: dict) -> S3CompatibleStore:
    return S3CompatibleStore(str(config.get("s3_endpoint") or ""), str(config.get("s3_bucket") or ""),
                             str(config.get("s3_region") or "us-east-1"))


def list_backups(config: dict) -> list[dict]:
    kind, target = destination_for(config)
    if kind in {"local", "mounted"}:
        path = Path(target)
        try:
            keys = [item.name for item in path.iterdir() if item.is_file() and BACKUP_RE.fullmatch(item.name)] if path.is_dir() else []
        except OSError as exc:
            raise BackupError("could not list the configured backup folder") from exc
        return [{"name": name, "source": str(path / name), "created_at": _backup_time(name)} for name in sorted(keys, reverse=True)]
    store = _s3_store(config)
    base = target.strip("/")
    keys = store.list(base)
    output = []
    for key in keys:
        name = PurePosixPath(key).name
        if BACKUP_RE.fullmatch(name):
            output.append({"name": name, "source": f"s3://{config['s3_bucket']}/{quote(key, safe='/!-_.~')}", "created_at": _backup_time(name)})
    return sorted(output, key=lambda row: row["name"], reverse=True)


def _backup_time(name: str) -> str:
    match = BACKUP_RE.fullmatch(name)
    if not match:
        return ""
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()


def _retention(config: dict, now: datetime | None = None) -> list[str]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=int(config.get("retention_days", 30)))
    kind, target = destination_for(config)
    removed = []
    if kind in {"local", "mounted"}:
        root = Path(target)
        if not root.is_dir():
            return removed
        for item in root.iterdir():
            if not item.is_file() or not BACKUP_RE.fullmatch(item.name):
                continue
            if datetime.fromisoformat(_backup_time(item.name)) < cutoff:
                item.unlink()
                removed.append(item.name)
        return removed
    store = _s3_store(config)
    base = target.strip("/")
    for backup in list_backups(config):
        if datetime.fromisoformat(backup["created_at"]) < cutoff:
            key = f"{base}/{backup['name']}" if base else backup["name"]
            store.delete(key)
            removed.append(backup["name"])
    return removed


def create_backup(database_url: str, repo_root: str | Path, settings, config: dict,
                  key: bytes, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    name = f"aiflow4-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}.aiflow4"
    with tempfile.TemporaryDirectory(prefix="aiflow4-backup-encrypted-") as tmp:
        plain, encrypted = Path(tmp) / "bundle.tar.gz", Path(tmp) / name
        manifest = create_archive(database_url, repo_root, settings, plain, now)
        encrypt_file(plain, encrypted, key)
        kind, target = destination_for(config)
        if kind in {"local", "mounted"}:
            root = Path(target)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            output = root / name
            staged = root / f".{name}.{secrets.token_hex(4)}.tmp"
            shutil.copyfile(encrypted, staged)
            os.chmod(staged, 0o600)
            os.replace(staged, output)
        else:
            key_name = f"{target.strip('/') + '/' if target.strip('/') else ''}{name}"
            _s3_store(config).put(key_name, encrypted)
    removed = _retention(config, now)
    return {"name": name, "created_at": now.isoformat(timespec="seconds"), "components": list(manifest["components"]),
            "removed": removed}


def _download_source(source: str, key: bytes, s3_endpoint: str = "", s3_region: str = "us-east-1") -> Path:
    parsed = urlsplit(source)
    if parsed.scheme == "s3":
        if not parsed.netloc or not parsed.path.strip("/"):
            raise BackupError("the S3 backup source is invalid")
        store = S3CompatibleStore(s3_endpoint, parsed.netloc, s3_region)
        with tempfile.NamedTemporaryFile(prefix="aiflow4-restore-", suffix=".aiflow4", delete=False) as file:
            path = Path(file.name)
        try:
            store.get(unquote(parsed.path.strip("/")), path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path
    if parsed.scheme or not source:
        raise BackupError("backup source must be a local file or s3:// URI")
    path = Path(source).expanduser()
    if not path.is_file() or not BACKUP_RE.fullmatch(path.name):
        raise BackupError("the selected backup file could not be found")
    return path


def _safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError("the backup contains an unsafe file path")
    return path


def _read_bundle(path: Path, key: bytes, workspace: Path) -> tuple[dict, dict[str, Path]]:
    plain = workspace / "bundle.tar.gz"
    decrypt_file(path, plain, key)
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(plain, "r:gz") as tar:
            for member in tar.getmembers():
                safe = _safe_member(member)
                if not member.isfile() or len(safe.parts) != 1 or safe.name == "manifest.json":
                    if safe.name == "manifest.json" and member.isfile() and len(safe.parts) == 1:
                        stream = tar.extractfile(member)
                        if stream:
                            members[safe.name] = stream.read()
                    continue
                stream = tar.extractfile(member)
                if stream:
                    members[safe.name] = stream.read()
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise BackupError("the decrypted backup bundle is damaged") from exc
    try:
        manifest = json.loads(members.pop("manifest.json"))
    except (KeyError, ValueError, TypeError) as exc:
        raise BackupError("the backup manifest is invalid") from exc
    if manifest.get("format") != 1 or set(manifest.get("components", {})) != {
            "database", "stored_secrets", "control_repository", "knowledge"}:
        raise BackupError("the backup manifest is incomplete")
    files = {}
    expected = {"database": "database.dump", "stored_secrets": "stored-secrets.tar.gz",
                "control_repository": "control-repository.tar.gz", "knowledge": "knowledge.json"}
    for component, name in expected.items():
        data = members.get(name)
        details = manifest["components"][component]
        if data is None or details.get("file") != name or details.get("sha256") != hashlib.sha256(data).hexdigest():
            raise BackupError(f"the {component.replace('_', ' ')} component failed its integrity check")
        target = workspace / name
        target.write_bytes(data)
        files[component] = target
    return manifest, files


def _extract_archive(archive: Path, target_roots: dict[str, Path], *, control_repo: bool = False) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            path = _safe_member(member)
            label = path.parts[0]
            if control_repo:
                root = target_roots.get("repository")
                relative = PurePosixPath(*path.parts[1:])
            else:
                root = target_roots.get(label)
                relative = PurePosixPath(*path.parts[1:])
            if root is None or not relative.parts:
                continue
            if not member.isdir() and not member.isfile():
                continue
            destination = root.joinpath(*relative.parts)
            if not destination.resolve().is_relative_to(root.resolve()):
                raise BackupError("the backup contains an unsafe file path")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(destination, 0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stream = tar.extractfile(member)
                if stream is None:
                    continue
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(destination, flags, 0o600)
                with os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(stream, output)
                os.chmod(destination, 0o600 if not control_repo else member.mode & 0o700)


def _restore_control_repo(archive: Path, root: Path) -> None:
    if root.exists():
        for item in root.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
    root.mkdir(parents=True, exist_ok=True)
    _extract_archive(archive, {"repository": root}, control_repo=True)


def _archive_env(archive: Path, name: str) -> dict[str, str]:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = next((item for item in tar.getmembers() if item.name == name and item.isfile()), None)
            if member is None:
                return {}
            stream = tar.extractfile(member)
            raw = stream.read().decode("utf-8") if stream else ""
    except (OSError, tarfile.TarError, UnicodeDecodeError) as exc:
        raise BackupError("stored secret files could not be read from the backup") from exc
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


def _restore_database_password(database_url: str, secrets_archive: Path) -> str:
    """Make a fresh control database's login match the restored secret files without exposing its password."""
    postgres = _archive_env(secrets_archive, "control/postgres.env")
    app = _archive_env(secrets_archive, "control/aiflow4.env")
    password = postgres.get("POSTGRES_PASSWORD", "")
    user = postgres.get("POSTGRES_USER", "aiflow")
    if password and user:
        try:
            with psycopg.connect(database_url) as connection:
                connection.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(sql.Identifier(user)), (password,))
        except psycopg.Error as exc:
            raise BackupError("the restored database password could not be applied") from exc
    # The database hostname/user are usually unchanged, but retain the source's exact URL when it was stored.
    return app.get("AIFLOW4_DATABASE_URL") or database_url


def _validate_knowledge(database_url: str, knowledge_file: Path) -> bool:
    expected = json.loads(knowledge_file.read_text(encoding="utf-8"))
    current = json.loads(_knowledge_bytes(database_url))
    return expected == current


def restore_archive(source: str, key: bytes, database_url: str, settings, *, s3_endpoint: str = "",
                    s3_region: str = "us-east-1", restore_roots: dict[str, Path] | None = None,
                    install_backup_key: bool = True, scratch_mode: bool = False) -> dict:
    """Restore all four named components and return an outcome for each one."""
    backup = _download_source(source, key, s3_endpoint, s3_region)
    remove_source = urlsplit(source).scheme == "s3"
    outcomes: dict[str, dict] = {}
    restored_database_url = database_url
    try:
        with tempfile.TemporaryDirectory(prefix="aiflow4-restore-unpack-") as temp:
            workspace = Path(temp)
            manifest, files = _read_bundle(backup, key, workspace)
            try:
                with _database_cli_connection(database_url) as (safe_conninfo, env):
                    _run_db_command(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
                                     "--exit-on-error", "--dbname", safe_conninfo, str(files["database"])],
                                    "database restore", env=env)
                outcomes["database"] = {"status": "restored", "detail": "database dump applied"}
            except BackupError as exc:
                outcomes["database"] = {"status": "failed", "detail": str(exc)}
            try:
                roots = restore_roots or {name: Path(path) for name, path in {
                    "control": settings.control_secrets_dir, "console": settings.console_secrets_dir,
                    "jira": settings.jira_secret_dir, "github": settings.github_app_dir}.items()}
                if outcomes.get("database", {}).get("status") == "restored" and not scratch_mode:
                    restored_database_url = _restore_database_password(database_url, files["stored_secrets"])
                _extract_archive(files["stored_secrets"], roots)
                if install_backup_key:
                    install_key(settings.backup_key_file, key)
                outcomes["stored_secrets"] = {"status": "restored", "detail": "stored secret files applied"}
            except Exception as exc:  # noqa: BLE001
                outcomes["stored_secrets"] = {"status": "failed", "detail": str(exc) if isinstance(exc, BackupError) else "stored secret files could not be applied"}
            try:
                repo_root = Path(restore_roots["repository"]) if restore_roots and "repository" in restore_roots else Path(settings.control_repo)
                _restore_control_repo(files["control_repository"], repo_root)
                outcomes["control_repository"] = {"status": "restored", "detail": "control repository settings applied"}
            except Exception:
                outcomes["control_repository"] = {"status": "failed", "detail": "control repository settings could not be applied"}
            try:
                if outcomes.get("database", {}).get("status") != "restored" or not _validate_knowledge(restored_database_url, files["knowledge"]):
                    raise BackupError("knowledge data does not match the backup snapshot")
                outcomes["knowledge"] = {"status": "restored", "detail": "knowledge snapshot verified in the restored database"}
            except Exception as exc:  # noqa: BLE001
                outcomes["knowledge"] = {"status": "failed", "detail": str(exc) if isinstance(exc, BackupError) else "knowledge snapshot could not be verified"}
            return {"backup": Path(urlsplit(source).path).name or source, "created_at": manifest.get("created_at"),
                    "components": outcomes,
                    "ok": all(item["status"] == "restored" for item in outcomes.values())}
    finally:
        if remove_source:
            backup.unlink(missing_ok=True)


def source_for_command(backup: dict) -> str:
    return str(backup.get("source") or "")

