#!/usr/bin/env python3
import argparse
import datetime as dt
import getpass
import hashlib
import html
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path

try:
    import fitz
except ImportError as exc:
    raise SystemExit("PyMuPDF is required. Install it with: python -m pip install pymupdf") from exc


REMOTE_ROOT = "/home/root/.local/share/remarkable/xochitl"
MODEL_NAME = "Remarkable Highlight"
MODEL_FIELDS = ["Citation", "Back", "RemarkableId", "Source", "Page"]
TEXT_TAG = b"\x44\x09\x00\x00\x00\x5c"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_SOURCE_TYPES = {"epub", "pdf"}


@dataclass
class DocumentInfo:
    uuid: str
    visible_name: str
    last_modified: int
    doc_type: str
    file_type: str = ""


@dataclass
class TextFragment:
    page_index: int
    page_id: str
    order: int
    rm_offset: int
    raw_text: str
    cleaned_text: str
    start: int | None = None
    end: int | None = None


@dataclass
class Highlight:
    document_uuid: str
    document_name: str
    order: int
    page_start: int
    page_end: int
    text: str
    context_text: str = ""
    context_html: str = ""
    context_markdown: str = ""

    @property
    def stable_id(self) -> str:
        return stable_id_for_text(
            self.document_uuid, self.page_start, self.page_end, self.text
        )

    @property
    def legacy_stable_ids(self) -> list[str]:
        legacy_text = legacy_pdf_ligature_artifact_text(self.text)
        if legacy_text == self.text:
            return []
        return [
            stable_id_for_text(
                self.document_uuid, self.page_start, self.page_end, legacy_text
            )
        ]


def stable_id_for_text(document_uuid: str, page_start: int, page_end: int, text: str) -> str:
    normalized = normalize_spaces(text).casefold()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"rm:{document_uuid}:{page_start}-{page_end}:{digest}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args()
    load_env_file(PROJECT_ROOT / ".env")
    load_env_file(PROJECT_ROOT / ".env.local")
    load_env_file(Path(".env"))
    load_env_file(Path(".env.local"))
    explicit_host = args.host
    args.host = args.host or os.environ.get("RM_REMARKABLE_IP")
    args.mac = args.mac or os.environ.get("RM_REMARKABLE_MAC")
    args.anki_exe = (
        args.anki_exe
        or os.environ.get("ANKI_EXE")
        or r"C:\Program Files\Anki\Anki.exe"
    )
    output_root = project_path(args.output_root)
    export_root = project_path(args.export_root)
    output_root.mkdir(parents=True, exist_ok=True)
    export_root.mkdir(parents=True, exist_ok=True)

    if args.local_dir:
        result = export_local_document(Path(args.local_dir), export_root, args)
        print_summary([result], args.no_anki)
        return 0

    if args.mac and not args.no_discover:
        discovered = find_remarkable_ip(
            mac=args.mac,
            cached_ip=args.host,
            cache_path=PROJECT_ROOT / ".env.local",
            timeout=args.scan_timeout,
        )
        if discovered:
            args.host = discovered
        elif explicit_host:
            print(
                "Could not find the reMarkable by MAC. "
                f"Using explicit host {explicit_host}."
            )
            args.host = explicit_host
        else:
            print(
                "Could not find the reMarkable by MAC. "
                "Wake the reMarkable, check RM_REMARKABLE_MAC, or pass --host "
                "explicitly if you want to bypass discovery."
            )
            return 1

    if not args.host:
        print(
            "No reMarkable host is configured. Set RM_REMARKABLE_MAC in "
            ".env.local for automatic discovery, or pass --host explicitly."
        )
        return 1

    ssh = RemarkableSSH(
        host=args.host,
        user=args.user,
        port=args.port,
        password_env=args.password_env,
        askpass_script=Path(__file__).with_name("ssh_askpass_env.cmd"),
        prompt_password=not args.no_password_prompt,
        retries=args.ssh_retries,
        retry_delay=args.ssh_retry_delay,
    )

    docs = ssh.list_documents()
    docs = [doc for doc in docs if is_supported_remote_document(doc)]
    docs.sort(key=lambda doc: doc.last_modified, reverse=True)

    if args.list:
        print_documents(docs[: args.limit])
        return 0

    selected_docs = select_documents(docs, args.document, args.all, args.limit)
    if not selected_docs:
        print("No document selected.")
        return 1

    results = []
    for doc in selected_docs:
        local_dir = copy_document(ssh, doc, output_root)
        results.append(export_local_document(local_dir, export_root, args))

    print_summary(results, args.no_anki)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export reMarkable highlights to Markdown/JSON and Anki."
    )
    parser.add_argument("--host", help="Explicit reMarkable IP address.")
    parser.add_argument("--mac", help="reMarkable MAC address. Defaults to RM_REMARKABLE_MAC.")
    parser.add_argument("--no-discover", action="store_true", help="Do not discover the IP from the MAC address.")
    parser.add_argument("--scan-timeout", type=float, default=20.0)
    parser.add_argument("--ssh-retries", type=int, default=6)
    parser.add_argument("--ssh-retry-delay", type=float, default=5.0)
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--password-env", default="RM_SSH_PASSWORD")
    parser.add_argument("--no-password-prompt", action="store_true")
    parser.add_argument("--document", help="Document number, UUID prefix, or visible name.")
    parser.add_argument("--all", action="store_true", help="Export all documents with highlights.")
    parser.add_argument("--list", action="store_true", help="List recent documents and exit.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output-root", default="data/remarkable/cache")
    parser.add_argument("--export-root", default="exports/remarkable")
    parser.add_argument("--local-dir", help="Export from an already copied local document directory.")
    parser.add_argument("--no-anki", action="store_true", help="Write files only; do not sync to Anki.")
    parser.add_argument(
        "--clean-exports-after-success",
        action="store_true",
        help="Delete generated Markdown/JSON exports after a successful Anki export.",
    )
    parser.add_argument(
        "--no-sync-ankiweb",
        action="store_true",
        help="Do not run AnkiWeb sync after updating local Anki.",
    )
    parser.add_argument("--anki-url", default="http://127.0.0.1:8765")
    parser.add_argument("--anki-exe", help="Path to Anki.exe. Defaults to ANKI_EXE or Program Files.")
    parser.add_argument(
        "--no-start-anki",
        action="store_true",
        help="Do not start Anki automatically if AnkiConnect is unavailable.",
    )
    parser.add_argument("--anki-start-timeout", type=float, default=90.0)
    parser.add_argument(
        "--deck-prefix",
        default="",
        help="Optional parent deck prefix. Empty by default, so decks use the document name.",
    )
    parser.add_argument(
        "--single-deck",
        action="store_true",
        help="Put all exported documents into one Anki deck instead of one deck per document.",
    )
    parser.add_argument(
        "--single-deck-name",
        default="Deck commun",
        help="Anki deck name used with --single-deck.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def project_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def upsert_env_file(path: Path, key: str, value: str) -> None:
    lines = []
    found = False
    if path.exists():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    updated = []
    for line in lines:
        if "=" in line and line.split("=", 1)[0].strip().lstrip("\ufeff") == key:
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def normalize_mac(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.casefold())


def arp_lookup(mac: str) -> str | None:
    target = normalize_mac(mac)
    commands = [["arp", "-a"]]
    if platform.system().casefold() == "linux":
        commands.insert(0, ["ip", "neigh", "show"])
    for command in commands:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in result.stdout.splitlines():
            if target not in normalize_mac(line):
                continue
            ip_match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
            if ip_match:
                return ip_match.group(1)
    return None


def local_subnet() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
    finally:
        sock.close()
    return ".".join(local_ip.split(".")[:3])


def ping_host(ip: str, timeout_ms: int = 700) -> None:
    system = platform.system().casefold()
    if system == "windows":
        command = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    elif system == "darwin":
        command = ["ping", "-c", "1", "-W", str(timeout_ms), ip]
    else:
        command = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def tcp_probe(ip: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def wake_probe(ip: str, port: int = 22) -> None:
    ping_host(ip)
    tcp_probe(ip, port, timeout=0.8)


def find_remarkable_ip(
    mac: str,
    cached_ip: str | None,
    cache_path: Path,
    timeout: float,
) -> str | None:
    mac = mac.strip()
    if cached_ip:
        print(f"Checking cached reMarkable IP {cached_ip} for MAC {mac}...")
        wake_probe(cached_ip)
        if arp_lookup(mac) == cached_ip:
            return cached_ip

    ip = arp_lookup(mac)
    if ip:
        upsert_env_file(cache_path, "RM_REMARKABLE_IP", ip)
        return ip

    subnet = local_subnet()
    print(f"Scanning {subnet}.0/24 to find reMarkable MAC {mac}...")
    deadline = time.time() + timeout
    addresses = [f"{subnet}.{index}" for index in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        futures = [executor.submit(ping_host, ip) for ip in addresses]
        concurrent.futures.wait(futures, timeout=max(1.0, timeout / 2))
    while time.time() < deadline:
        ip = arp_lookup(mac)
        if ip:
            upsert_env_file(cache_path, "RM_REMARKABLE_IP", ip)
            return ip
        time.sleep(0.5)
    return None


class RemarkableSSH:
    def __init__(
        self,
        host: str,
        user: str,
        port: int,
        password_env: str,
        askpass_script: Path,
        prompt_password: bool,
        retries: int,
        retry_delay: float,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.password_env = password_env
        self.askpass_script = askpass_script
        self.retries = max(1, retries)
        self.retry_delay = max(0.5, retry_delay)
        self.password = os.environ.get(password_env)
        if not self.password and prompt_password and sys.stdin.isatty():
            entered = getpass.getpass(
                f"Password for {user}@{host} (leave blank to use SSH keys): "
            )
            self.password = entered or None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.password:
            env[self.password_env] = self.password
            env["SSH_ASKPASS"] = str(self.askpass_script.resolve())
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = "codex"
        return env

    def ssh_args(self) -> list[str]:
        return [
            windows_openssh("ssh.exe"),
            "-p",
            str(self.port),
            "-o",
            "PreferredAuthentications=publickey,password",
            "-o",
            "PubkeyAuthentication=yes",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "BatchMode=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=NUL",
            "-o",
            "LogLevel=ERROR",
            self.target,
        ]

    def scp_args(self) -> list[str]:
        return [
            windows_openssh("scp.exe"),
            "-P",
            str(self.port),
            "-o",
            "PreferredAuthentications=publickey,password",
            "-o",
            "PubkeyAuthentication=yes",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "BatchMode=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=NUL",
            "-o",
            "LogLevel=ERROR",
        ]

    def run(self, remote_command: str) -> str:
        completed = self.run_with_retries(self.ssh_args() + [remote_command], "ssh")
        if completed.returncode != 0:
            raise SystemExit(self.format_ssh_error("ssh", completed))
        return completed.stdout.decode("utf-8", errors="replace")

    def list_documents(self) -> list[DocumentInfo]:
        command = (
            f"for f in {REMOTE_ROOT}/*.metadata; do "
            "[ -f \"$f\" ] || continue; "
            "b=${f##*/}; uuid=${b%.metadata}; "
            "printf '\\n===RM_METADATA_START:%s===\\n' \"$uuid\"; "
            "cat \"$f\"; "
            "printf '\\n===RM_METADATA_END===\\n'; "
            f"c={REMOTE_ROOT}/\"$uuid\".content; "
            "if [ -f \"$c\" ]; then "
            "printf '\\n===RM_CONTENT_START:%s===\\n' \"$uuid\"; "
            "cat \"$c\"; "
            "printf '\\n===RM_CONTENT_END===\\n'; "
            "fi; "
            "done"
        )
        output = self.run(command)
        return parse_document_stream(output)

    def copy_glob(self, uuid: str, local_dir: Path) -> None:
        local_dir.parent.mkdir(parents=True, exist_ok=True)
        if local_dir.exists():
            shutil.rmtree(local_dir)
        local_dir.mkdir(parents=True)
        remote_spec = f"{self.target}:{REMOTE_ROOT}/{uuid}*"
        completed = self.run_with_retries(
            self.scp_args() + ["-r", remote_spec, str(local_dir)], "scp"
        )
        if completed.returncode != 0:
            raise SystemExit(self.format_ssh_error("scp", completed))

    def run_with_retries(self, command: list[str], label: str) -> subprocess.CompletedProcess:
        last_completed = None
        for attempt in range(1, self.retries + 1):
            last_completed = subprocess.run(
                command,
                env=self.env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if last_completed.returncode == 0:
                return last_completed
            if not self.should_retry_ssh(last_completed) or attempt == self.retries:
                return last_completed
            print(
                f"{label} not ready for {self.host} "
                f"(attempt {attempt}/{self.retries}). Wake the reMarkable if it is asleep..."
            )
            wake_probe(self.host, self.port)
            time.sleep(self.retry_delay)
        return last_completed

    def format_ssh_error(self, action: str, completed: subprocess.CompletedProcess) -> str:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        details = stderr or stdout or f"{action} exited with code {completed.returncode}"
        if completed.returncode == 255 and not self.password:
            return (
                f"{action} failed: {details}\n\n"
                "No SSH password was available. Put it in .env.local as "
                f"{self.password_env}=... or set it for this PowerShell session."
            )
        if completed.returncode == 255:
            return (
                f"{action} failed: {details}\n\n"
                "If the reMarkable is asleep, wake it manually, wait a few seconds, "
                "then retry or click Actualiser in the web interface."
            )
        return f"{action} failed: {details}"

    def should_retry_ssh(self, completed: subprocess.CompletedProcess) -> bool:
        if completed.returncode != 255:
            return False
        details = (
            completed.stderr.decode("utf-8", errors="replace")
            + completed.stdout.decode("utf-8", errors="replace")
        ).casefold()
        if "permission denied" in details:
            return False
        return True


def windows_openssh(executable: str) -> str:
    path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / executable
    if path.exists():
        return str(path)
    return executable


def is_supported_remote_document(doc: DocumentInfo) -> bool:
    return doc.doc_type == "DocumentType" and doc.file_type in SUPPORTED_SOURCE_TYPES


def parse_document_stream(output: str) -> list[DocumentInfo]:
    contents = parse_json_blocks(output, "CONTENT")
    docs = []
    for uuid, metadata in parse_json_blocks(output, "METADATA").items():
        content = contents.get(uuid, {})
        docs.append(
            DocumentInfo(
                uuid=uuid,
                visible_name=metadata.get("visibleName") or uuid,
                last_modified=parse_int(metadata.get("lastModified")),
                doc_type=metadata.get("type") or "",
                file_type=(content.get("fileType") or "").casefold(),
            )
        )
    return docs


def parse_json_blocks(output: str, label: str) -> dict[str, dict]:
    pattern = re.compile(
        rf"===RM_{label}_START:([0-9a-fA-F-]+)===\n(.*?)\n===RM_{label}_END===",
        re.S,
    )
    blocks = {}
    for match in pattern.finditer(output):
        uuid = match.group(1)
        try:
            blocks[uuid] = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue
    return blocks


def print_documents(docs: list[DocumentInfo]) -> None:
    for index, doc in enumerate(docs, start=1):
        modified = format_timestamp_ms(doc.last_modified)
        source_type = doc.file_type.upper() if doc.file_type else "UNKNOWN"
        print(f"{index:>2}. {modified}  {source_type:<4}  {doc.visible_name}  [{doc.uuid}]")


def select_documents(
    docs: list[DocumentInfo], selector: str | None, all_docs: bool, limit: int
) -> list[DocumentInfo]:
    if all_docs or (selector and selector.strip().casefold() == "tout"):
        return docs
    if selector:
        return [resolve_document_selector(docs, selector)]

    recent = docs[:limit]
    print_documents(recent)
    choice = input("Document PDF/EPUB a exporter (numero, nom, UUID, ou TOUT): ").strip()
    if choice.casefold() == "tout":
        return docs
    return [resolve_document_selector(recent, choice)]


def resolve_document_selector(docs: list[DocumentInfo], selector: str) -> DocumentInfo:
    value = selector.strip()
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(docs):
            return docs[index - 1]
    value_cf = value.casefold()
    matches = [
        doc
        for doc in docs
        if doc.uuid.startswith(value_cf)
        or doc.visible_name.casefold() == value_cf
        or value_cf in doc.visible_name.casefold()
    ]
    if not matches:
        raise SystemExit(f"No document matches: {selector}")
    if len(matches) > 1:
        print("Multiple documents match:")
        print_documents(matches[:20])
        raise SystemExit("Use a more precise name or UUID prefix.")
    return matches[0]


def copy_document(ssh: RemarkableSSH, doc: DocumentInfo, output_root: Path) -> Path:
    slug = safe_filename(f"{doc.visible_name}_{doc.uuid[:8]}")
    local_dir = output_root / slug
    print(f"Copying {doc.visible_name} [{doc.uuid}] read-only...")
    ssh.copy_glob(doc.uuid, local_dir)
    return local_dir


def export_local_document(local_dir: Path, export_root: Path, args: argparse.Namespace) -> dict:
    bundle = load_local_bundle(local_dir)
    highlights = extract_highlights(bundle)
    export_paths = write_exports(bundle, highlights, export_root)
    anki_stats = None
    if not args.no_anki and highlights:
        anki = AnkiConnect(
            args.anki_url,
            anki_exe=Path(args.anki_exe),
            auto_start=not args.no_start_anki,
            start_timeout=args.anki_start_timeout,
        )
        anki_stats = anki.sync_highlights(
            bundle["visible_name"],
            bundle["uuid"],
            highlights,
            deck_prefix=args.deck_prefix,
            deck_name_override=getattr(args, "single_deck_name", "Deck commun")
            if getattr(args, "single_deck", False)
            else None,
        )
        if not args.no_sync_ankiweb:
            anki_stats["web_sync"] = anki.sync_ankiweb()
    cleaned_exports = []
    if should_cleanup_exports_after_success(args, highlights, anki_stats):
        cleaned_exports = cleanup_export_files(export_paths)
    return {
        "document": bundle["visible_name"],
        "uuid": bundle["uuid"],
        "highlight_count": len(highlights),
        "export_paths": export_paths,
        "cleaned_exports": cleaned_exports,
        "anki": anki_stats,
    }


def should_cleanup_exports_after_success(
    args: argparse.Namespace, highlights: list[Highlight], anki_stats: dict | None
) -> bool:
    if not getattr(args, "clean_exports_after_success", False):
        return False
    if getattr(args, "no_anki", False):
        return False
    return not highlights or anki_stats is not None


def cleanup_export_files(export_paths: dict[str, str]) -> list[str]:
    removed = []
    for path in export_paths.values():
        if not path:
            continue
        candidate = Path(path)
        if not candidate.is_file():
            continue
        try:
            candidate.unlink()
        except OSError as exc:
            print(f"Could not clean export {candidate}: {exc}")
            continue
        removed.append(str(candidate))
    return removed


def load_local_bundle(local_dir: Path) -> dict:
    metadata_files = list(local_dir.glob("*.metadata"))
    if not metadata_files:
        raise SystemExit(f"No .metadata file found in {local_dir}")
    metadata_path = metadata_files[0]
    uuid = metadata_path.stem
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    content_path = local_dir / f"{uuid}.content"
    if not content_path.exists():
        raise SystemExit(f"No .content file found for {uuid}")
    content = json.loads(content_path.read_text(encoding="utf-8"))
    source_path = find_source_file(local_dir, uuid, content)
    if not source_path:
        raise SystemExit(f"No PDF/EPUB source file found for {uuid}")
    return {
        "local_dir": local_dir,
        "uuid": uuid,
        "visible_name": metadata.get("visibleName") or uuid,
        "metadata": metadata,
        "content": content,
        "source_path": source_path,
        "annotation_dir": local_dir / uuid,
    }


def find_source_file(local_dir: Path, uuid: str, content: dict) -> Path | None:
    candidates = []
    # reMarkable EPUB annotations are page-based against the rendered sidecar PDF,
    # so prefer it when present even if content.fileType is "epub".
    candidates.extend(local_dir.glob(f"{uuid}.pdf"))
    file_type = (content.get("fileType") or "").lower()
    if file_type:
        candidates.append(local_dir / f"{uuid}.{file_type}")
    candidates.extend(local_dir.glob(f"{uuid}.epub"))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def extract_highlights(bundle: dict) -> list[Highlight]:
    source_path: Path = bundle["source_path"]
    content: dict = bundle["content"]
    annotation_dir: Path = bundle["annotation_dir"]
    page_ids = content_page_ids(content)
    page_texts = extract_source_texts(source_path)
    fragments = extract_fragments(annotation_dir, page_ids)
    align_fragments(fragments, page_texts)
    if any(normalize_spaces(page_text) for page_text in page_texts):
        fragments = [
            fragment
            for fragment in fragments
            if fragment.start is not None and fragment.end is not None
        ]
    return group_fragments(
        fragments=fragments,
        page_texts=page_texts,
        document_uuid=bundle["uuid"],
        document_name=bundle["visible_name"],
    )


def content_page_ids(content: dict) -> list[str]:
    pages = content.get("pages") or []
    if pages:
        return [page for page in pages if isinstance(page, str)]

    c_pages = content.get("cPages") or {}
    page_entries = c_pages.get("pages") or []
    if not isinstance(page_entries, list):
        return []

    indexed_pages = []
    for fallback_order, page in enumerate(page_entries):
        if not isinstance(page, dict) or not isinstance(page.get("id"), str):
            continue
        order = nested_value(page, "redir", "value")
        if not isinstance(order, int):
            order = fallback_order
        indexed_pages.append((order, fallback_order, page["id"]))
    indexed_pages.sort()
    return [page_id for _order, _fallback_order, page_id in indexed_pages]


def nested_value(data: dict, *keys: str):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def extract_source_texts(source_path: Path) -> list[str]:
    doc = fitz.open(source_path)
    texts = []
    for page in doc:
        texts.append(clean_extracted_text(page.get_text()))
    return texts


def extract_fragments(annotation_dir: Path, page_ids: list[str]) -> list[TextFragment]:
    fragments: list[TextFragment] = []
    if not annotation_dir.exists():
        return fragments
    page_index_by_id = {page_id: index for index, page_id in enumerate(page_ids)}
    for rm_path in sorted(annotation_dir.glob("*.rm")):
        page_id = rm_path.stem
        if page_id not in page_index_by_id:
            continue
        page_index = page_index_by_id[page_id]
        for order, (offset, raw_text) in enumerate(parse_rm_text_blocks(rm_path), start=1):
            cleaned = clean_rm_text(raw_text)
            if not is_highlight_text(cleaned):
                continue
            fragments.append(
                TextFragment(
                    page_index=page_index,
                    page_id=page_id,
                    order=order,
                    rm_offset=offset,
                    raw_text=raw_text,
                    cleaned_text=cleaned,
                )
            )
    fragments.sort(key=lambda frag: (frag.page_index, frag.order, frag.rm_offset))
    return fragments


def parse_rm_text_blocks(rm_path: Path) -> list[tuple[int, str]]:
    data = rm_path.read_bytes()
    blocks: list[tuple[int, str]] = []
    pos = 0
    while True:
        tag = data.find(TEXT_TAG, pos)
        if tag < 0:
            break
        pos = tag + 1
        if tag + len(TEXT_TAG) + 5 >= len(data):
            continue
        length_pos = tag + len(TEXT_TAG)
        byte_len = int.from_bytes(data[length_pos : length_pos + 4], "little")
        if byte_len <= 0 or byte_len > len(data):
            continue
        cursor = length_pos + 4
        _, cursor = read_leb128(data, cursor)
        if cursor >= len(data):
            continue
        cursor += 1
        raw = data[cursor : cursor + byte_len].rstrip(b"\x00")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        blocks.append((tag, text))
    if blocks:
        return blocks
    return fallback_text_blocks(data)


def read_leb128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    cursor = offset
    while cursor < len(data):
        byte = data[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            break
        shift += 7
    return value, cursor


def fallback_text_blocks(data: bytes) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for match in re.finditer(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf\xc2-\xf4]{5,}", data):
        raw = match.group(0)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        blocks.append((match.start(), text))
    return blocks


def clean_rm_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = clean_extracted_text(text)
    text = normalize_spaces(text)
    while text.endswith("l!") or text.endswith("lA"):
        text = text[:-2].rstrip()
    return text


def clean_extracted_text(text: str) -> str:
    text = repair_mojibake(text)
    return repair_pdf_ligature_artifacts(text)


def repair_pdf_ligature_artifacts(text: str) -> str:
    replacements = {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "st",
        "\ufb06": "st",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)

    # Some reMarkable EPUB sidecar PDFs expose broken font glyph maps through
    # PDF text extraction. Repair only word-like positions.
    text = re.sub(r"(?<=\d)>(?=[A-Za-z])", " fi", text)
    text = re.sub(r"(?<![A-Za-z0-9])>(?=[A-Za-z])", "fi", text)
    text = re.sub(r"(?<=[A-Za-z])>(?=[A-Za-z])", "fi", text)
    text = re.sub(r"(?<=[A-Za-z])\?(?=[A-Za-z])", "ff", text)
    text = re.sub(r"(?<=[a-z])W(?=[a-z])", "ffl", text)
    text = re.sub(r"\bdi\s+culties\b", "difficulties", text)
    text = re.sub(r"\bdi\s+culty\b", "difficulty", text)
    text = re.sub(r"\bdi\s+cult\b", "difficult", text)
    text = re.sub(r"\bo\s+ces\b", "offices", text)
    text = re.sub(r"\bo\s+ce\b", "office", text)
    text = re.sub(r"\ba\s+rmations\b", "affirmations", text)
    text = re.sub(r"\ba\s+rmation\b", "affirmation", text)
    text = re.sub(r"\ba\s+rmed\b", "affirmed", text)
    text = re.sub(r"\ba\s+rm\b", "affirm", text)
    text = re.sub(r"\bstu\?(?=\W|$)", "stuff", text)
    text = re.sub(r"\bsta\?(?=\W|$)", "staff", text)
    text = re.sub(r"\bo\?(?=\W|$)", "off", text)
    text = re.sub(r"\btra\s+c\b", "traffic", text)
    text = re.sub(r"\bragamu\s+n\b", "ragamuffin", text)
    text = re.sub(r"\bFowing\b", "flowing", text)
    text = re.sub(r"\bFiers\b", "fliers", text)
    text = re.sub(r"\byour\s+Fight\b", "your flight", text)
    return text


def legacy_pdf_ligature_artifact_text(text: str) -> str:
    text = re.sub(r"\bdifficulties\b", "di culties", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdifficulty\b", "di culty", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdifficult\b", "di cult", text, flags=re.IGNORECASE)
    text = re.sub(r"\boffices\b", "o ces", text, flags=re.IGNORECASE)
    text = re.sub(r"\boffice\b", "o ce", text, flags=re.IGNORECASE)
    text = re.sub(r"\baffirmations\b", "a rmations", text, flags=re.IGNORECASE)
    text = re.sub(r"\baffirmation\b", "a rmation", text, flags=re.IGNORECASE)
    text = re.sub(r"\baffirmed\b", "a rmed", text, flags=re.IGNORECASE)
    text = re.sub(r"\baffirm\b", "a rm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bstuff\b", "stu?", text, flags=re.IGNORECASE)
    text = re.sub(r"\bstaff\b", "sta?", text, flags=re.IGNORECASE)
    text = re.sub(r"\boff\b", "o?", text, flags=re.IGNORECASE)
    text = re.sub(r"\btraffic\b", "tra c", text, flags=re.IGNORECASE)
    text = re.sub(r"\bragamuffin\b", "ragamu n", text, flags=re.IGNORECASE)
    text = re.sub(r"\bflowing\b", "Fowing", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfliers\b", "Fiers", text, flags=re.IGNORECASE)
    text = re.sub(r"\byour\s+flight\b", "your Fight", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[a-z])ffl(?=[a-z])", "W", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<![A-Za-z0-9])fi(?=[A-Za-z])", ">", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[A-Za-z])fi(?=[A-Za-z])", ">", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s+fi(?=[A-Za-z])", ">", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[A-Za-z])ff(?=[A-Za-z])", "?", text, flags=re.IGNORECASE)
    return text


def repair_mojibake(text: str) -> str:
    if not any(marker in text for marker in ("Ã", "Â", "Æ", "Ä", "á»", "áº")):
        return text
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except UnicodeError:
        return text
    if mojibake_score(repaired) < mojibake_score(text):
        return repaired
    return text


def mojibake_score(text: str) -> int:
    markers = ("Ã", "Â", "Æ", "Ä", "á»", "áº", "â€", "ï¼")
    return sum(text.count(marker) for marker in markers)


def is_highlight_text(text: str) -> bool:
    if len(text) < 3:
        return False
    if text.startswith("reMarkable .lines file"):
        return False
    if text.startswith("Layer "):
        return False
    return any(char.isalpha() for char in text)


def align_fragments(fragments: list[TextFragment], page_texts: list[str]) -> None:
    cursors: dict[int, int] = {}
    for index, fragment in enumerate(fragments):
        if fragment.page_index >= len(page_texts):
            continue
        page_text = page_texts[fragment.page_index]
        start_hint = cursors.get(fragment.page_index, 0)
        next_text = next_fragment_text(fragments, index)
        span = find_best_fragment_span(page_text, fragment.cleaned_text, start_hint, next_text)
        if span is None:
            span = find_fragment_span(page_text, fragment.cleaned_text, 0)
        if span is None:
            continue
        fragment.start, fragment.end = span
        cursors[fragment.page_index] = max(cursors.get(fragment.page_index, 0), fragment.end)


def next_fragment_text(fragments: list[TextFragment], index: int) -> str | None:
    fragment = fragments[index]
    for next_fragment in fragments[index + 1 :]:
        if next_fragment.page_index != fragment.page_index:
            return None
        if next_fragment.cleaned_text:
            return next_fragment.cleaned_text
    return None


def find_best_fragment_span(
    source: str, fragment: str, start_hint: int, next_text: str | None
) -> tuple[int, int] | None:
    candidates = find_fragment_span_candidates(source, fragment, start_hint)
    if next_text and len(candidates) > 1:
        for candidate in candidates:
            next_span = find_fragment_span(source, next_text, candidate[1])
            if next_span and source[candidate[1] : next_span[0]].strip() == "":
                return candidate
    if candidates:
        return candidates[0]
    return find_fragment_span(source, fragment, start_hint)


def find_fragment_span_candidates(
    source: str, fragment: str, start_hint: int, limit: int = 24
) -> list[tuple[int, int]]:
    fragment = normalize_spaces(fragment)
    if not fragment:
        return []

    candidates: list[tuple[int, int]] = []
    seen = set()

    direct_start = source.find(fragment, start_hint)
    while direct_start >= 0 and len(candidates) < limit:
        span = (direct_start, direct_start + len(fragment))
        if span not in seen:
            candidates.append(span)
            seen.add(span)
        direct_start = source.find(fragment, direct_start + 1)

    compact_source, compact_map = compact_with_map(source)
    compact_fragment, _ = compact_with_map(fragment)
    if compact_fragment:
        compact_hint = compact_index_for_source_offset(compact_map, start_hint)
        compact_start = compact_source.find(compact_fragment, compact_hint)
        while compact_start >= 0 and len(candidates) < limit:
            span = (
                compact_map[compact_start],
                compact_map[compact_start + len(compact_fragment) - 1] + 1,
            )
            if span not in seen:
                candidates.append(span)
                seen.add(span)
            compact_start = compact_source.find(compact_fragment, compact_start + 1)

    candidates.sort()
    return candidates


def compact_index_for_source_offset(compact_map: list[int], source_offset: int) -> int:
    for index, mapped_offset in enumerate(compact_map):
        if mapped_offset >= source_offset:
            return index
    return len(compact_map)


def find_fragment_span(source: str, fragment: str, start_hint: int) -> tuple[int, int] | None:
    fragment = normalize_spaces(fragment)
    if not fragment:
        return None

    direct = source.find(fragment, start_hint)
    if direct >= 0:
        return direct, direct + len(fragment)

    compact_source, compact_map = compact_with_map(source)
    compact_fragment, _ = compact_with_map(fragment)
    if not compact_fragment:
        return None

    compact_hint = compact_index_for_source_offset(compact_map, start_hint)

    compact_start = compact_source.find(compact_fragment, compact_hint)
    if compact_start < 0:
        compact_start = compact_source.find(compact_fragment)
    if compact_start >= 0:
        norm_start = compact_map[compact_start]
        norm_end = compact_map[compact_start + len(compact_fragment) - 1] + 1
        return norm_start, norm_end

    return fuzzy_span(compact_source, compact_map, compact_fragment, compact_hint)


def fuzzy_span(
    compact_source: str, compact_map: list[int], compact_fragment: str, compact_hint: int
) -> tuple[int, int] | None:
    if len(compact_fragment) < 20:
        return None
    head = compact_fragment[: min(24, len(compact_fragment) // 2)]
    tail = compact_fragment[-min(24, len(compact_fragment) // 2) :]
    head_start = compact_source.find(head, compact_hint)
    if head_start < 0:
        head_start = compact_source.find(head)
    if head_start < 0:
        return None
    tail_start = compact_source.find(tail, head_start + len(head))
    if tail_start < 0:
        common = longest_common_prefix_at(compact_source, compact_fragment, head_start)
        if common >= min(60, max(20, len(compact_fragment) // 3)):
            compact_end = head_start + common
            compact_end = trim_partial_compact_word(compact_map, head_start, compact_end)
            compact_end = min(compact_end, len(compact_map))
            if compact_end <= head_start:
                return None
            return compact_map[head_start], compact_map[compact_end - 1] + 1
        return None
    compact_end = tail_start + len(tail)
    return compact_map[head_start], compact_map[compact_end - 1] + 1


def trim_partial_compact_word(
    compact_map: list[int], compact_start: int, compact_end: int
) -> int:
    if compact_end <= compact_start or compact_end >= len(compact_map):
        return compact_end
    if compact_map[compact_end] != compact_map[compact_end - 1] + 1:
        return compact_end

    word_start = compact_end - 1
    while (
        word_start > compact_start
        and compact_map[word_start] == compact_map[word_start - 1] + 1
    ):
        word_start -= 1
    if word_start == compact_start:
        return compact_end
    return word_start


def longest_common_prefix_at(source: str, fragment: str, source_start: int) -> int:
    count = 0
    while (
        source_start + count < len(source)
        and count < len(fragment)
        and source[source_start + count] == fragment[count]
    ):
        count += 1
    return count


def compact_with_map(text: str) -> tuple[str, list[int]]:
    chars = []
    index_map = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(char.casefold())
        index_map.append(index)
    return "".join(chars), index_map


def group_fragments(
    fragments: list[TextFragment],
    page_texts: list[str],
    document_uuid: str,
    document_name: str,
) -> list[Highlight]:
    highlights: list[Highlight] = []
    current: dict | None = None

    for fragment in fragments:
        if fragment.start is None or fragment.end is None:
            text = fragment.cleaned_text
        else:
            text = page_texts[fragment.page_index][fragment.start : fragment.end]

        if current and can_merge(current, fragment, page_texts):
            if fragment.start is not None and fragment.end is not None:
                if current["page_end"] == fragment.page_index:
                    current["end"] = max(current["end"], fragment.end)
                    current["text"] = page_texts[fragment.page_index][current["start"] : current["end"]]
                else:
                    current["text"] = normalize_spaces(current["text"] + " " + text)
                    current["page_end"] = fragment.page_index
                    current["end"] = fragment.end
            else:
                current["text"] = normalize_spaces(current["text"] + " " + text)
            continue

        if current:
            append_highlight(highlights, current, page_texts, document_uuid, document_name)

        current = {
            "page_start": fragment.page_index,
            "page_end": fragment.page_index,
            "start": fragment.start,
            "end": fragment.end,
            "text": text,
        }

    if current:
        append_highlight(highlights, current, page_texts, document_uuid, document_name)
    return highlights


def append_highlight(
    highlights: list[Highlight],
    data: dict,
    page_texts: list[str],
    document_uuid: str,
    document_name: str,
) -> None:
    highlight = dict_to_highlight(
        data, page_texts, document_uuid, document_name, len(highlights) + 1
    )
    if normalize_spaces(highlight.text):
        highlights.append(highlight)


def can_merge(current: dict, fragment: TextFragment, page_texts: list[str]) -> bool:
    if current["end"] is None or fragment.start is None or fragment.end is None:
        return False
    if current["page_end"] == fragment.page_index:
        if fragment.start <= current["end"]:
            return True
        gap = page_texts[fragment.page_index][current["end"] : fragment.start]
        return gap.strip() == ""
    if current["page_end"] + 1 == fragment.page_index:
        previous_tail = page_texts[current["page_end"]][current["end"] :].strip()
        next_head = page_texts[fragment.page_index][: fragment.start].strip()
        return previous_tail == "" and next_head == ""
    return False


def dict_to_highlight(
    data: dict,
    page_texts: list[str],
    document_uuid: str,
    document_name: str,
    order: int,
) -> Highlight:
    context = build_context(data, page_texts)
    return Highlight(
        document_uuid=document_uuid,
        document_name=document_name,
        order=order,
        page_start=data["page_start"] + 1,
        page_end=data["page_end"] + 1,
        text=normalize_spaces(data["text"]),
        context_text=context["text"],
        context_html=context["html"],
        context_markdown=context["markdown"],
    )


def build_context(data: dict, page_texts: list[str]) -> dict[str, str]:
    highlight_text = normalize_spaces(data["text"])
    fallback_html = highlighted_html(highlight_text)
    fallback_markdown = f"**{highlight_text}**"
    if (
        data["start"] is None
        or data["end"] is None
        or data["page_start"] >= len(page_texts)
    ):
        return {"text": highlight_text, "html": fallback_html, "markdown": fallback_markdown}

    if data["page_start"] != data["page_end"]:
        return build_cross_page_context(data, page_texts, highlight_text, fallback_html, fallback_markdown)

    source = page_texts[data["page_start"]]
    start = max(0, min(data["start"], len(source)))
    end = max(start, min(data["end"], len(source)))
    spans = sentence_spans(source)
    first_sentence = sentence_index_for_offset(spans, start)
    last_sentence = sentence_index_for_offset(spans, max(start, end - 1))

    current_start = spans[first_sentence][0]
    current_end = spans[last_sentence][1]
    parts_before = [span_text(source, previous_sentence_span(spans, first_sentence))]
    parts_after = [span_text(source, next_sentence_span(spans, last_sentence))]
    before = join_context_parts(parts_before + [source[current_start:start]])
    highlighted = source[start:end]
    after = join_context_parts([source[end:current_end]] + parts_after)
    highlighted = normalize_spaces(highlighted)
    text = join_context_parts([before, highlighted, after])
    html_context = join_context_parts(
        [html.escape(before), highlighted_html(highlighted), html.escape(after)]
    )
    markdown_context = join_context_parts([before, f"**{highlighted}**", after])
    return {
        "text": normalize_spaces(text),
        "html": normalize_spaces(html_context),
        "markdown": normalize_spaces(markdown_context),
    }


def build_cross_page_context(
    data: dict,
    page_texts: list[str],
    highlight_text: str,
    fallback_html: str,
    fallback_markdown: str,
) -> dict[str, str]:
    if data["page_end"] >= len(page_texts):
        return {"text": highlight_text, "html": fallback_html, "markdown": fallback_markdown}

    first_page = page_texts[data["page_start"]]
    last_page = page_texts[data["page_end"]]
    start = max(0, min(data["start"], len(first_page)))
    end = max(0, min(data["end"], len(last_page)))

    first_spans = sentence_spans(first_page)
    first_sentence = sentence_index_for_offset(first_spans, start)
    first_current_start = first_spans[first_sentence][0]
    before = join_context_parts(
        [
            span_text(first_page, previous_sentence_span(first_spans, first_sentence)),
            first_page[first_current_start:start],
        ]
    )

    last_spans = sentence_spans(last_page)
    last_sentence = sentence_index_for_offset(last_spans, max(0, end - 1))
    last_current_end = last_spans[last_sentence][1]
    after = join_context_parts(
        [
            last_page[end:last_current_end],
            span_text(last_page, next_sentence_span(last_spans, last_sentence)),
        ]
    )

    html_context = join_context_parts(
        [
            html.escape(before),
            highlighted_html(highlight_text),
            html.escape(after),
        ]
    )
    markdown_context = join_context_parts([before, f"**{highlight_text}**", after])
    return {
        "text": join_context_parts([before, highlight_text, after]),
        "html": html_context,
        "markdown": markdown_context,
    }


def highlighted_html(text: str) -> str:
    return f"<mark><strong>{html.escape(text)}</strong></mark>"


def sentence_spans(text: str) -> list[tuple[int, int, bool]]:
    spans: list[tuple[int, int, bool]] = []
    segment_start = 0
    for line_match in re.finditer(r"[^\r\n]*(?:\r?\n|$)", text):
        line_start = line_match.start()
        line_end = line_match.end()
        line = line_match.group(0)
        if line_start == line_end:
            continue
        if is_heading_line(line):
            append_sentence_segments(text, segment_start, line_start, spans)
            spans.append((line_start, line_end, True))
            segment_start = line_end
    append_sentence_segments(text, segment_start, len(text), spans)
    if not spans:
        spans.append((0, len(text), False))
    return spans


def append_sentence_segments(
    text: str, start: int, end: int, spans: list[tuple[int, int, bool]]
) -> None:
    if start >= end or not text[start:end].strip():
        return
    segment_start = start
    for match in re.finditer(r"[.!?。！？]+(?:[\"'”’»)\]]+)?\s+", text[start:end]):
        segment_end = start + match.end()
        if text[segment_start:segment_end].strip():
            spans.append((segment_start, segment_end, False))
        segment_start = segment_end
    if text[segment_start:end].strip():
        spans.append((segment_start, end, False))


def is_heading_line(line: str) -> bool:
    stripped = normalize_spaces(line)
    if not stripped:
        return False
    if stripped.startswith("Lời khuyên"):
        return True
    if stripped.endswith((".", "!", "?", "。", "！", "？")):
        return False
    return ":" in stripped and len(stripped) <= 120


def sentence_index_for_offset(spans: list[tuple[int, int, bool]], offset: int) -> int:
    for index, (span_start, span_end, _is_heading) in enumerate(spans):
        if span_start <= offset < span_end:
            return index
    if not spans:
        return 0
    return len(spans) - 1


def previous_sentence_span(
    spans: list[tuple[int, int, bool]], start_index: int
) -> tuple[int, int, bool] | None:
    for index in range(start_index - 1, -1, -1):
        if not spans[index][2]:
            return spans[index]
    return None


def next_sentence_span(
    spans: list[tuple[int, int, bool]], start_index: int
) -> tuple[int, int, bool] | None:
    for index in range(start_index + 1, len(spans)):
        if not spans[index][2]:
            return spans[index]
    return None


def span_text(text: str, span: tuple[int, int, bool] | None) -> str:
    if span is None:
        return ""
    return text[span[0] : span[1]]


def join_context_parts(parts: list[str]) -> str:
    text = " ".join(normalize_spaces(part) for part in parts if part and part.strip())
    return re.sub(r"\s+([,.;:!?。！？])", r"\1", text)


def write_exports(bundle: dict, highlights: list[Highlight], export_root: Path) -> dict[str, str]:
    slug = safe_filename(bundle["visible_name"])
    json_path = export_root / f"{slug}.json"
    md_path = export_root / f"{slug}.md"
    payload = {
        "document": {
            "uuid": bundle["uuid"],
            "visibleName": bundle["visible_name"],
            "source": str(bundle["source_path"]),
            "exportedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        "highlightCount": len(highlights),
        "highlights": [
            {
                "id": highlight.stable_id,
                "order": highlight.order,
                "pageStart": highlight.page_start,
                "pageEnd": highlight.page_end,
                "citation": highlight.text,
                "contextText": highlight.context_text,
                "contextHtml": highlight.context_html,
            }
            for highlight in highlights
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(bundle, highlights), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def render_markdown(bundle: dict, highlights: list[Highlight]) -> str:
    lines = [
        f"# {bundle['visible_name']}",
        "",
        f"- UUID: `{bundle['uuid']}`",
        f"- Highlights: {len(highlights)}",
        "",
    ]
    for highlight in highlights:
        page = (
            f"page {highlight.page_start}"
            if highlight.page_start == highlight.page_end
            else f"pages {highlight.page_start}-{highlight.page_end}"
        )
        lines.extend(
            [
                f"## {highlight.order}. {page}",
                "",
                highlight.text,
                "",
                "Contexte:",
                "",
                highlight.context_markdown or f"**{highlight.text}**",
                "",
                f"`{highlight.stable_id}`",
                "",
            ]
        )
    return "\n".join(lines)


class AnkiConnect:
    def __init__(
        self,
        url: str,
        anki_exe: Path,
        auto_start: bool,
        start_timeout: float,
    ) -> None:
        self.url = url
        self.anki_exe = anki_exe
        self.auto_start = auto_start
        self.start_timeout = start_timeout
        self._start_attempted = False

    def invoke(self, action: str, params: dict | None = None):
        result = self.request(action, params)
        if result.get("unreachable"):
            if not self.auto_start:
                raise SystemExit(f"AnkiConnect is not reachable at {self.url}: {result['error']}")
            self.ensure_anki_ready(start_if_needed=True)
            result = self.request(action, params)
        if self.is_collection_unavailable(result.get("error")) and self.auto_start:
            self.ensure_anki_ready(start_if_needed=False)
            result = self.request(action, params)
        if result.get("unreachable"):
            raise SystemExit(f"AnkiConnect is not reachable at {self.url}: {result['error']}")
        if result.get("error"):
            raise SystemExit(f"AnkiConnect error for {action}: {result['error']}")
        return result.get("result")

    def request(self, action: str, params: dict | None = None) -> dict:
        payload = json.dumps({"action": action, "version": 6, "params": params or {}}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"result": None, "error": str(exc), "unreachable": True}

    def ensure_anki_ready(self, start_if_needed: bool) -> None:
        if start_if_needed and not self._start_attempted:
            self.start_anki()
            self._start_attempted = True
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            result = self.request("deckNames")
            if not result.get("unreachable") and not self.is_collection_unavailable(result.get("error")):
                return
            time.sleep(2)
        raise SystemExit(
            "Anki did not become available in time. Open Anki, unlock/select a profile, "
            "then run the script again."
        )

    def start_anki(self) -> None:
        if not self.anki_exe.exists():
            raise SystemExit(f"Anki executable not found: {self.anki_exe}")
        print(f"Starting Anki: {self.anki_exe}")
        if platform.system().casefold() == "windows":
            try:
                os.startfile(str(self.anki_exe))
                return
            except OSError:
                pass
        subprocess.Popen(
            [str(self.anki_exe)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.anki_exe.parent),
            close_fds=True,
        )

    @staticmethod
    def is_collection_unavailable(error: str | None) -> bool:
        return bool(error and "collection is not available" in error.casefold())

    def sync_highlights(
        self,
        document_name: str,
        document_uuid: str,
        highlights: list[Highlight],
        deck_prefix: str,
        deck_name_override: str | None = None,
    ) -> dict:
        self.ensure_model()
        highlights = [highlight for highlight in highlights if normalize_spaces(highlight.text)]
        deck_name = make_deck_name(deck_prefix, deck_name_override or document_name)
        self.invoke("createDeck", {"deck": deck_name})
        current_ids = {highlight.stable_id for highlight in highlights}
        lookup_ids = sorted(
            {
                lookup_id
                for highlight in highlights
                for lookup_id in [highlight.stable_id, *highlight.legacy_stable_ids]
            }
        )
        existing_notes = self.notes_by_remarkable_id(lookup_ids)
        note_specs = []
        update_actions = []
        added_notes = []
        notes_to_move = []

        for highlight in highlights:
            note_id = existing_notes.get(highlight.stable_id)
            if note_id is None:
                for legacy_id in highlight.legacy_stable_ids:
                    note_id = existing_notes.get(legacy_id)
                    if note_id is not None:
                        break
            fields = self.highlight_fields(document_name, highlight)
            spec = {"highlight_id": highlight.stable_id, "note_id": note_id}
            note_specs.append(spec)
            if note_id:
                update_actions.append(
                    {
                        "action": "updateNoteFields",
                        "params": {"note": {"id": note_id, "fields": fields}},
                    }
                )
                notes_to_move.append(int(note_id))
            else:
                added_notes.append(
                    {
                        "deckName": deck_name,
                        "modelName": MODEL_NAME,
                        "fields": fields,
                        "options": {"allowDuplicate": True},
                        "tags": ["remarkable", f"rm_{document_uuid[:8]}"],
                    }
                )

        if update_actions:
            self.invoke_multi(update_actions, chunk_size=10)
        if notes_to_move:
            self.move_notes_cards(notes_to_move, deck_name)

        added_ids = []
        if added_notes:
            for note_chunk in self.chunks(added_notes, chunk_size=10):
                added_ids.extend(self.invoke("addNotes", {"notes": note_chunk}))
            pending_ids = iter(added_ids)
            for spec in note_specs:
                if spec["note_id"] is None:
                    spec["note_id"] = next(pending_ids, None)

        ordered_note_ids = [int(spec["note_id"]) for spec in note_specs if spec["note_id"]]
        deleted = self.delete_stale_notes(document_uuid, current_ids, set(ordered_note_ids))
        reordered = self.reorder_new_cards(ordered_note_ids)
        added = sum(1 for note_id in added_ids if note_id)
        updated = len(update_actions)
        return {
            "added": added,
            "updated": updated,
            "deleted": deleted,
            "reordered": reordered,
            "deck": deck_name,
        }

    def invoke_multi(self, actions: list[dict], chunk_size: int = 25) -> list:
        results = []
        for start in range(0, len(actions), chunk_size):
            chunk = actions[start : start + chunk_size]
            results.extend(self.invoke("multi", {"actions": chunk}))
        return results

    @staticmethod
    def chunks(items: list, chunk_size: int = 25):
        for start in range(0, len(items), chunk_size):
            yield items[start : start + chunk_size]

    @staticmethod
    def highlight_fields(document_name: str, highlight: Highlight) -> dict[str, str]:
        return {
            "Citation": html.escape(highlight.text).replace("\n", "<br>"),
            "Back": highlight.context_html or highlighted_html(highlight.text),
            "RemarkableId": highlight.stable_id,
            "Source": document_name,
            "Page": str(highlight.page_start)
            if highlight.page_start == highlight.page_end
            else f"{highlight.page_start}-{highlight.page_end}",
        }

    def notes_by_remarkable_id(self, remarkable_ids: list[str]) -> dict[str, int]:
        if not remarkable_ids:
            return {}
        actions = [
            {"action": "findNotes", "params": {"query": f'RemarkableId:"{remarkable_id}"'}}
            for remarkable_id in remarkable_ids
        ]
        notes = {}
        for remarkable_id, matches in zip(remarkable_ids, self.invoke_multi(actions)):
            if matches:
                notes[remarkable_id] = int(matches[0])
        return notes

    def ensure_model(self) -> None:
        model_names = self.invoke("modelNames")
        if MODEL_NAME in model_names:
            return
        self.invoke(
            "createModel",
            {
                "modelName": MODEL_NAME,
                "inOrderFields": MODEL_FIELDS,
                "css": ".card { font-family: sans-serif; font-size: 22px; text-align: left; }",
                "cardTemplates": [
                    {
                        "Name": "Card 1",
                        "Front": "{{Citation}}",
                        "Back": "{{Back}}",
                    }
                ],
            },
        )

    def find_note_by_remarkable_id(self, remarkable_id: str) -> int | None:
        query = f'RemarkableId:"{remarkable_id}"'
        matches = self.invoke("findNotes", {"query": query})
        if not matches:
            return None
        return int(matches[0])

    def move_note_cards(self, note_id: int, deck_name: str) -> None:
        cards = self.invoke("findCards", {"query": f"nid:{note_id}"})
        if cards:
            self.invoke("changeDeck", {"cards": cards, "deck": deck_name})

    def move_notes_cards(self, note_ids: list[int], deck_name: str) -> None:
        actions = [
            {"action": "findCards", "params": {"query": f"nid:{note_id}"}}
            for note_id in note_ids
        ]
        cards = []
        for result in self.invoke_multi(actions):
            cards.extend(int(card_id) for card_id in result)
        if cards:
            self.invoke("changeDeck", {"cards": cards, "deck": deck_name})

    def reorder_new_cards(self, note_ids: list[int]) -> int:
        actions = [
            {"action": "findCards", "params": {"query": f"nid:{note_id} is:new"}}
            for note_id in note_ids
        ]
        new_cards = []
        for result in self.invoke_multi(actions):
            new_cards.extend(int(card_id) for card_id in result)
        if not new_cards:
            return 0

        first_info = self.invoke("cardsInfo", {"cards": [new_cards[0]]})
        base_due = int(first_info[0].get("due", 0)) if first_info else 0
        actions = [
            {
                "action": "setSpecificValueOfCard",
                "params": {
                    "card": card_id,
                    "keys": ["due"],
                    "newValues": [base_due + offset],
                    "warning_check": True,
                },
            }
            for offset, card_id in enumerate(new_cards)
        ]
        self.invoke_multi(actions)
        return len(new_cards)

    def delete_stale_notes(
        self,
        document_uuid: str,
        current_ids: set[str],
        current_note_ids: set[int] | None = None,
    ) -> int:
        matches = self.invoke("findNotes", {"query": f"RemarkableId:rm:{document_uuid}:*"})
        if not matches:
            return 0
        if current_note_ids:
            matches = [note_id for note_id in matches if int(note_id) not in current_note_ids]
        if not matches:
            return 0

        stale = []
        for note_ids in self.chunks(matches, chunk_size=10):
            for info in self.invoke("notesInfo", {"notes": note_ids}):
                remarkable_id = info.get("fields", {}).get("RemarkableId", {}).get("value", "")
                if remarkable_id and remarkable_id not in current_ids:
                    stale.append(info["noteId"])
        if stale:
            self.invoke("deleteNotes", {"notes": stale})
        return len(stale)

    def sync_ankiweb(self) -> str:
        self.invoke("sync")
        return "ok"


def make_deck_name(prefix: str, document_name: str) -> str:
    clean_name = document_name.replace("\n", " ").strip() or "Untitled"
    if prefix:
        return f"{prefix}::{clean_name}"
    return clean_name


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "untitled"


def parse_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def format_timestamp_ms(value: int) -> str:
    if value <= 0:
        return "unknown"
    try:
        moment = dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return str(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def print_summary(results: list[dict], no_anki: bool) -> None:
    print("")
    print("Export complete.")
    for result in results:
        print(f"- {result['document']}: {result['highlight_count']} highlights")
        cleaned_exports = result.get("cleaned_exports") or []
        if cleaned_exports:
            print(f"  Exports cleaned: {len(cleaned_exports)} files")
        else:
            print(f"  Markdown: {result['export_paths']['markdown']}")
            print(f"  JSON: {result['export_paths']['json']}")
        if result["anki"]:
            print(
                f"  Anki: {result['anki']['added']} added, "
                f"{result['anki']['updated']} updated, "
                f"{result['anki'].get('deleted', 0)} deleted"
            )
            if result["anki"].get("reordered"):
                print(f"  Anki order: {result['anki']['reordered']} new cards repositioned")
            if result["anki"].get("web_sync"):
                print("  AnkiWeb sync: ok")
    if no_anki:
        print("Anki sync skipped (--no-anki).")


if __name__ == "__main__":
    raise SystemExit(main())
