#!/usr/bin/env python3
import argparse
import contextlib
import io
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import uuid as uuidlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
WEB_ROOT = PROJECT_ROOT / "web" / "remarkable"
DEFAULT_SINGLE_DECK_NAME = "Deck commun"
DEFAULT_CLEAN_EXPORTS_AFTER_SUCCESS = True

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import remarkable_anki as core  # noqa: E402


class WebError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class RemarkableWebService:
    def __init__(self) -> None:
        self.cache_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.cached_docs: list[core.DocumentInfo] = []
        self.cached_at = 0.0
        self.cached_host = ""
        self.job_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.active_job_id: str | None = None
        self.clean_exports_after_success_default = DEFAULT_CLEAN_EXPORTS_AFTER_SUCCESS

    def load_environment(self) -> None:
        core.load_env_file(PROJECT_ROOT / ".env")
        core.load_env_file(PROJECT_ROOT / ".env.local")

    def resolve_host(self) -> str:
        self.load_environment()
        cached_ip = os.environ.get("RM_REMARKABLE_IP")
        mac = os.environ.get("RM_REMARKABLE_MAC")
        if mac:
            discovered = core.find_remarkable_ip(
                mac=mac,
                cached_ip=cached_ip,
                cache_path=PROJECT_ROOT / ".env.local",
                timeout=float(os.environ.get("RM_SCAN_TIMEOUT", "20")),
            )
            if discovered:
                return discovered
            raise WebError(
                "reMarkable introuvable. Reveille la tablette ou verifie RM_REMARKABLE_MAC.",
                status=503,
            )
        if cached_ip:
            return cached_ip
        raise WebError(
            "Aucun reMarkable configure. Ajoute RM_REMARKABLE_MAC dans .env.local.",
            status=503,
        )

    def make_ssh(self) -> core.RemarkableSSH:
        host = self.resolve_host()
        return core.RemarkableSSH(
            host=host,
            user=os.environ.get("RM_SSH_USER", "root"),
            port=int(os.environ.get("RM_SSH_PORT", "22")),
            password_env="RM_SSH_PASSWORD",
            askpass_script=SCRIPT_DIR / "ssh_askpass_env.cmd",
            prompt_password=False,
            retries=int(os.environ.get("RM_SSH_RETRIES", "6")),
            retry_delay=float(os.environ.get("RM_SSH_RETRY_DELAY", "5")),
        )

    def list_documents(self, force: bool = False) -> tuple[list[core.DocumentInfo], str]:
        now = time.time()
        with self.cache_lock:
            if not force and self.cached_docs and now - self.cached_at < 60:
                return list(self.cached_docs), self.cached_host

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            ssh = self.make_ssh()
            docs = ssh.list_documents()

        docs = [doc for doc in docs if core.is_supported_remote_document(doc)]
        docs.sort(key=lambda doc: doc.last_modified, reverse=True)

        with self.cache_lock:
            self.cached_docs = docs
            self.cached_at = time.time()
            self.cached_host = ssh.host
        return list(docs), ssh.host

    def filtered_documents(
        self, limit: str, search: str, force: bool, sort_order: str = "modified"
    ) -> dict:
        docs, host = self.list_documents(force=force)
        query = search.strip().casefold()
        if query:
            docs = [
                doc
                for doc in docs
                if query in doc.visible_name.casefold()
                or query in doc.uuid.casefold()
                or query in doc.file_type.casefold()
            ]

        sort_order = sort_order.casefold()
        if sort_order == "name_asc":
            docs.sort(key=lambda doc: doc.visible_name.casefold())
        elif sort_order == "name_desc":
            docs.sort(key=lambda doc: doc.visible_name.casefold(), reverse=True)
        elif sort_order == "type_pdf":
            docs.sort(
                key=lambda doc: (
                    0 if doc.file_type.casefold() == "pdf" else 1,
                    doc.visible_name.casefold(),
                )
            )
        elif sort_order == "type_epub":
            docs.sort(
                key=lambda doc: (
                    0 if doc.file_type.casefold() == "epub" else 1,
                    doc.visible_name.casefold(),
                )
            )
        elif sort_order != "modified":
            raise WebError("Tri invalide.", status=400)

        total = len(docs)
        if limit.casefold() != "all":
            try:
                docs = docs[: int(limit)]
            except ValueError as exc:
                raise WebError("Limite invalide.", status=400) from exc

        return {
            "host": host,
            "total": total,
            "documents": [document_payload(doc) for doc in docs],
            "fetchedAt": int(time.time() * 1000),
        }

    def export_documents(
        self,
        uuids: list[str],
        sync_ankiweb: bool,
        single_deck: bool = False,
        single_deck_name: str = DEFAULT_SINGLE_DECK_NAME,
    ) -> dict:
        if not uuids:
            raise WebError("Aucun document selectionne.", status=400)
        if not self.export_lock.acquire(blocking=False):
            raise WebError("Un export est deja en cours.", status=409)
        try:
            return self._export_documents(uuids, sync_ankiweb, single_deck, single_deck_name)
        finally:
            self.export_lock.release()

    def _export_documents(
        self,
        uuids: list[str],
        sync_ankiweb: bool,
        single_deck: bool = False,
        single_deck_name: str = DEFAULT_SINGLE_DECK_NAME,
    ) -> dict:
        docs, _host = self.list_documents(force=False)
        docs_by_uuid = {doc.uuid: doc for doc in docs}
        selected_docs = []
        for uuid in uuids:
            doc = docs_by_uuid.get(uuid)
            if not doc:
                raise WebError(f"Document introuvable: {uuid}", status=404)
            selected_docs.append(doc)
        use_single_deck = single_deck and len(selected_docs) > 1
        single_deck_name = clean_single_deck_name(single_deck_name)

        args = argparse.Namespace(
            no_anki=False,
            no_sync_ankiweb=not sync_ankiweb,
            anki_url=os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765"),
            anki_exe=os.environ.get("ANKI_EXE") or r"C:\Program Files\Anki\Anki.exe",
            no_start_anki=False,
            anki_start_timeout=float(os.environ.get("ANKI_START_TIMEOUT", "90")),
            deck_prefix=os.environ.get("ANKI_DECK_PREFIX", ""),
            single_deck=use_single_deck,
            single_deck_name=single_deck_name,
        )
        output_root = core.project_path("data/remarkable/cache")
        export_root = core.project_path("exports/remarkable")
        output_root.mkdir(parents=True, exist_ok=True)
        export_root.mkdir(parents=True, exist_ok=True)

        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                ssh = self.make_ssh()
                results = []
                for doc in selected_docs:
                    local_dir = core.copy_document(ssh, doc, output_root)
                    results.append(core.export_local_document(local_dir, export_root, args))
        except SystemExit as exc:
            message = str(exc) if str(exc) else "Export interrompu."
            raise WebError(message, status=500) from exc

        return {
            "message": "export terminé",
            "results": [export_result_payload(result) for result in results],
            "log": buffer.getvalue().strip(),
        }

    def create_export_job(
        self,
        uuids: list[str],
        sync_ankiweb: bool,
        single_deck: bool = False,
        single_deck_name: str = DEFAULT_SINGLE_DECK_NAME,
        clean_exports_after_success: bool | None = None,
    ) -> dict:
        if not uuids:
            raise WebError("Aucun document selectionne.", status=400)
        single_deck_name = clean_single_deck_name(single_deck_name)
        if clean_exports_after_success is None:
            clean_exports_after_success = self.clean_exports_after_success_default
        with self.job_lock:
            if self.active_job_id:
                raise WebError("Un export est deja en cours.", status=409)
            job_id = uuidlib.uuid4().hex
            job = {
                "id": job_id,
                "status": "running",
                "message": "Export en cours",
                "progress": {
                    "current": 0,
                    "total": len(uuids),
                    "phase": "preparation",
                    "currentDocument": "",
                    "documents": [],
                },
                "log": [],
                "startedAt": int(time.time() * 1000),
                "finishedAt": None,
                "options": {
                    "singleDeck": bool(single_deck and len(uuids) > 1),
                    "singleDeckName": single_deck_name,
                    "cleanExportsAfterSuccess": bool(clean_exports_after_success),
                },
                "result": None,
                "error": None,
            }
            self.jobs[job_id] = job
            self.active_job_id = job_id

        thread = threading.Thread(
            target=self.run_export_job,
            args=(
                job_id,
                uuids,
                sync_ankiweb,
                single_deck,
                single_deck_name,
                bool(clean_exports_after_success),
            ),
            daemon=True,
        )
        thread.start()
        return self.get_export_job(job_id)

    def run_export_job(
        self,
        job_id: str,
        uuids: list[str],
        sync_ankiweb: bool,
        single_deck: bool,
        single_deck_name: str,
        clean_exports_after_success: bool,
    ) -> None:
        try:
            result = self.export_documents_with_progress(
                job_id,
                uuids,
                sync_ankiweb,
                single_deck,
                single_deck_name,
                clean_exports_after_success,
            )
            self.finish_job(job_id, status="done", result=result, error=None)
        except WebError as exc:
            self.append_job_log(job_id, f"Erreur: {exc}")
            self.finish_job(job_id, status="error", result=None, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self.append_job_log(job_id, traceback.format_exc(limit=8))
            self.finish_job(job_id, status="error", result=None, error=str(exc))
        finally:
            with self.job_lock:
                if self.active_job_id == job_id:
                    self.active_job_id = None

    def export_documents_with_progress(
        self,
        job_id: str,
        uuids: list[str],
        sync_ankiweb: bool,
        single_deck: bool = False,
        single_deck_name: str = DEFAULT_SINGLE_DECK_NAME,
        clean_exports_after_success: bool = DEFAULT_CLEAN_EXPORTS_AFTER_SUCCESS,
    ) -> dict:
        if not uuids:
            raise WebError("Aucun document selectionne.", status=400)
        if not self.export_lock.acquire(blocking=False):
            raise WebError("Un export est deja en cours.", status=409)
        try:
            return self._export_documents_with_progress(
                job_id,
                uuids,
                sync_ankiweb,
                single_deck,
                single_deck_name,
                clean_exports_after_success,
            )
        finally:
            self.export_lock.release()

    def _export_documents_with_progress(
        self,
        job_id: str,
        uuids: list[str],
        sync_ankiweb: bool,
        single_deck: bool = False,
        single_deck_name: str = DEFAULT_SINGLE_DECK_NAME,
        clean_exports_after_success: bool = DEFAULT_CLEAN_EXPORTS_AFTER_SUCCESS,
    ) -> dict:
        docs, _host = self.list_documents(force=False)
        docs_by_uuid = {doc.uuid: doc for doc in docs}
        selected_docs = []
        for uuid in uuids:
            doc = docs_by_uuid.get(uuid)
            if not doc:
                raise WebError(f"Document introuvable: {uuid}", status=404)
            selected_docs.append(doc)
        use_single_deck = single_deck and len(selected_docs) > 1
        single_deck_name = clean_single_deck_name(single_deck_name)

        document_states = [
            {"uuid": doc.uuid, "name": doc.visible_name, "status": "waiting", "message": "En attente"}
            for doc in selected_docs
        ]
        self.update_job_progress(
            job_id,
            current=0,
            total=len(selected_docs),
            phase="preparation",
            current_document="",
            documents=document_states,
        )

        args = argparse.Namespace(
            no_anki=False,
            no_sync_ankiweb=not sync_ankiweb,
            anki_url=os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765"),
            anki_exe=os.environ.get("ANKI_EXE") or r"C:\Program Files\Anki\Anki.exe",
            no_start_anki=False,
            anki_start_timeout=float(os.environ.get("ANKI_START_TIMEOUT", "90")),
            deck_prefix=os.environ.get("ANKI_DECK_PREFIX", ""),
            single_deck=use_single_deck,
            single_deck_name=single_deck_name,
            clean_exports_after_success=clean_exports_after_success,
        )
        output_root = core.project_path("data/remarkable/cache")
        export_root = core.project_path("exports/remarkable")
        output_root.mkdir(parents=True, exist_ok=True)
        export_root.mkdir(parents=True, exist_ok=True)

        results = []
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                self.append_job_log(job_id, "Connexion reMarkable")
                ssh = self.make_ssh()
                for index, doc in enumerate(selected_docs, start=1):
                    self.append_job_log(
                        job_id,
                        f"[{index}/{len(selected_docs)}] Copie: {doc.visible_name}",
                    )
                    self.set_document_state(
                        job_id, document_states, doc.uuid, "running", "Copie depuis reMarkable"
                    )
                    self.update_job_progress(
                        job_id,
                        current=index,
                        total=len(selected_docs),
                        phase="copie",
                        current_document=doc.visible_name,
                        documents=document_states,
                    )
                    local_dir = core.copy_document(ssh, doc, output_root)
                    self.flush_output_log(job_id, buffer)

                    self.append_job_log(
                        job_id,
                        f"[{index}/{len(selected_docs)}] Extraction: {doc.visible_name}",
                    )
                    self.set_document_state(
                        job_id, document_states, doc.uuid, "running", "Extraction highlights"
                    )
                    self.update_job_progress(
                        job_id,
                        current=index,
                        total=len(selected_docs),
                        phase="extraction",
                        current_document=doc.visible_name,
                        documents=document_states,
                    )
                    result = self.export_local_document_with_progress(
                        local_dir, export_root, args, job_id, doc, index, len(selected_docs), document_states
                    )
                    self.flush_output_log(job_id, buffer)
                    results.append(result)
                    self.set_document_state(
                        job_id,
                        document_states,
                        doc.uuid,
                        "done",
                        f"{result['highlight_count']} highlights",
                    )
                    self.update_job_progress(
                        job_id,
                        current=index,
                        total=len(selected_docs),
                        phase="termine",
                        current_document=doc.visible_name,
                        documents=document_states,
                    )
        except SystemExit as exc:
            self.flush_output_log(job_id, buffer)
            message = str(exc) if str(exc) else "Export interrompu."
            self.mark_running_document_error(job_id, document_states, message)
            raise WebError(message, status=500) from exc
        except Exception as exc:
            self.flush_output_log(job_id, buffer)
            message = str(exc) if str(exc) else exc.__class__.__name__
            self.mark_running_document_error(job_id, document_states, message)
            raise

        return {
            "message": "export termine",
            "results": [export_result_payload(result) for result in results],
            "log": buffer.getvalue().strip(),
        }

    def export_local_document_with_progress(
        self,
        local_dir: Path,
        export_root: Path,
        args: argparse.Namespace,
        job_id: str,
        doc: core.DocumentInfo,
        index: int,
        total: int,
        document_states: list[dict],
    ) -> dict:
        bundle = core.load_local_bundle(local_dir)
        highlights = core.extract_highlights(bundle)
        self.append_job_log(
            job_id,
            f"Highlights: {bundle['visible_name']} ({len(highlights)})",
        )
        export_paths = core.write_exports(bundle, highlights, export_root)
        anki_stats = None
        if not args.no_anki and highlights:
            self.append_job_log(
                job_id,
                f"Anki: {bundle['visible_name']} ({len(highlights)} highlights)",
            )
            self.set_document_state(job_id, document_states, doc.uuid, "running", "Mise a jour Anki")
            self.update_job_progress(
                job_id,
                current=index,
                total=total,
                phase="anki",
                current_document=doc.visible_name,
                documents=document_states,
            )
            anki = core.AnkiConnect(
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
                deck_name_override=args.single_deck_name if getattr(args, "single_deck", False) else None,
            )
            if not args.no_sync_ankiweb:
                self.append_job_log(job_id, f"AnkiWeb: {bundle['visible_name']}")
                self.set_document_state(job_id, document_states, doc.uuid, "running", "Sync AnkiWeb")
                self.update_job_progress(
                    job_id,
                    current=index,
                    total=total,
                    phase="ankiweb",
                    current_document=doc.visible_name,
                    documents=document_states,
                )
                anki_stats["web_sync"] = anki.sync_ankiweb()
        cleaned_exports = []
        if core.should_cleanup_exports_after_success(args, highlights, anki_stats):
            cleaned_exports = core.cleanup_export_files(export_paths)
            self.append_job_log(
                job_id,
                f"Exports nettoyes: {len(cleaned_exports)} fichier(s)",
            )
        return {
            "document": bundle["visible_name"],
            "uuid": bundle["uuid"],
            "highlight_count": len(highlights),
            "export_paths": export_paths,
            "cleaned_exports": cleaned_exports,
            "anki": anki_stats,
        }

    def set_document_state(
        self, job_id: str, documents: list[dict], uuid: str, status: str, message: str
    ) -> None:
        for document in documents:
            if document["uuid"] == uuid:
                document["status"] = status
                document["message"] = message
                break

    def append_job_log(self, job_id: str, message: str) -> None:
        lines = [line.strip() for line in str(message).splitlines() if line.strip()]
        if not lines:
            return
        timestamp = time.strftime("%H:%M:%S")
        with self.job_lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            log = job.setdefault("log", [])
            for line in lines:
                log.append(f"{timestamp} {line}")
            del log[:-200]

    def flush_output_log(self, job_id: str, buffer: io.StringIO) -> None:
        text = buffer.getvalue().strip()
        if text:
            self.append_job_log(job_id, text)
        buffer.seek(0)
        buffer.truncate(0)

    def mark_running_document_error(
        self, job_id: str, documents: list[dict], message: str
    ) -> None:
        for document in documents:
            if document.get("status") == "running":
                document["status"] = "error"
                document["message"] = message
                break
        with self.job_lock:
            job = self.jobs.get(job_id)
            if not job or not job.get("progress"):
                return
            job["progress"]["documents"] = [dict(document) for document in documents]

    def update_job_progress(
        self,
        job_id: str,
        current: int,
        total: int,
        phase: str,
        current_document: str,
        documents: list[dict],
    ) -> None:
        with self.job_lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["progress"] = {
                "current": current,
                "total": total,
                "phase": phase,
                "currentDocument": current_document,
                "documents": [dict(document) for document in documents],
            }

    def finish_job(
        self, job_id: str, status: str, result: dict | None, error: str | None
    ) -> None:
        with self.job_lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["status"] = status
            job["message"] = "export termine" if status == "done" else "Erreur export"
            job["finishedAt"] = int(time.time() * 1000)
            job["result"] = result
            job["error"] = error

    def get_export_job(self, job_id: str) -> dict:
        with self.job_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise WebError("Tache export introuvable.", status=404)
            return json.loads(json.dumps(job, ensure_ascii=False))


def document_payload(doc: core.DocumentInfo) -> dict:
    return {
        "uuid": doc.uuid,
        "name": doc.visible_name,
        "type": doc.file_type.upper(),
        "modified": doc.last_modified,
        "modifiedText": core.format_timestamp_ms(doc.last_modified),
    }


def export_result_payload(result: dict) -> dict:
    anki = result.get("anki") or {}
    return {
        "document": result["document"],
        "uuid": result["uuid"],
        "highlightCount": result["highlight_count"],
        "cleanedExports": len(result.get("cleaned_exports") or []),
        "anki": {
            "added": anki.get("added", 0),
            "updated": anki.get("updated", 0),
            "deleted": anki.get("deleted", 0),
            "reordered": anki.get("reordered", 0),
            "webSync": bool(anki.get("web_sync")),
            "deck": anki.get("deck", ""),
        }
        if anki
        else None,
    }


def clean_single_deck_name(value) -> str:
    if not isinstance(value, str):
        return DEFAULT_SINGLE_DECK_NAME
    name = " ".join(value.split())
    return name or DEFAULT_SINGLE_DECK_NAME


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


SERVICE = RemarkableWebService()


class Handler(BaseHTTPRequestHandler):
    server_version = "RemarkableAnkiWeb/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/documents":
            self.handle_documents(parsed)
            return
        if parsed.path.startswith("/api/jobs/"):
            self.handle_job(parsed)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/export":
            self.handle_export()
            return
        self.send_json({"error": "Route inconnue."}, status=404)

    def handle_documents(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        limit = params.get("limit", ["20"])[0]
        search = params.get("search", [""])[0]
        sort_order = params.get("sort", ["modified"])[0]
        force = params.get("force", ["0"])[0] == "1"
        try:
            payload = SERVICE.filtered_documents(
                limit=limit, search=search, force=force, sort_order=sort_order
            )
            self.send_json(payload)
        except WebError as exc:
            self.send_json({"error": str(exc)}, status=exc.status)
        except SystemExit as exc:
            self.send_json({"error": str(exc) or "Commande interrompue."}, status=500)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, status=500)

    def handle_export(self) -> None:
        try:
            body = self.read_json()
            uuids = body.get("uuids") or []
            sync_ankiweb = bool(body.get("syncAnkiweb", True))
            single_deck = bool(body.get("singleDeck", False))
            single_deck_name = clean_single_deck_name(body.get("singleDeckName", ""))
            clean_exports = body.get(
                "cleanExportsAfterSuccess",
                SERVICE.clean_exports_after_success_default,
            )
            if not isinstance(uuids, list) or not all(isinstance(uuid, str) for uuid in uuids):
                raise WebError("Selection invalide.", status=400)
            self.send_json(
                SERVICE.create_export_job(
                    uuids,
                    sync_ankiweb,
                    single_deck,
                    single_deck_name,
                    bool(clean_exports),
                ),
                status=202,
            )
        except WebError as exc:
            self.send_json({"error": str(exc)}, status=exc.status)
        except SystemExit as exc:
            self.send_json({"error": str(exc) or "Commande interrompue."}, status=500)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, status=500)

    def handle_job(self, parsed: urllib.parse.ParseResult) -> None:
        job_id = parsed.path.rsplit("/", 1)[-1]
        try:
            self.send_json(SERVICE.get_export_job(job_id))
        except WebError as exc:
            self.send_json({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, status=500)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WebError("JSON invalide.", status=400) from exc

    def serve_static(self, request_path: str) -> None:
        path = urllib.parse.unquote(request_path)
        if path == "/":
            path = "/index.html"
        target = (WEB_ROOT / path.lstrip("/")).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self.send_error(403)
            return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local web UI for reMarkable -> Anki exports.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--clean-exports-after-success",
        dest="clean_exports_after_success",
        action="store_true",
        default=None,
        help="Default the web UI/API to delete Markdown/JSON exports after successful Anki export.",
    )
    parser.add_argument(
        "--keep-exports",
        dest="clean_exports_after_success",
        action="store_false",
        help="Default the web UI/API to keep generated Markdown/JSON exports.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.clean_exports_after_success is None:
        SERVICE.clean_exports_after_success_default = parse_bool_env(
            "RM_CLEAN_EXPORTS_AFTER_SUCCESS",
            DEFAULT_CLEAN_EXPORTS_AFTER_SUCCESS,
        )
    else:
        SERVICE.clean_exports_after_success_default = bool(args.clean_exports_after_success)
    if not (WEB_ROOT / "index.html").exists():
        raise SystemExit(f"Web assets not found: {WEB_ROOT}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"reMarkable Anki web interface: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
