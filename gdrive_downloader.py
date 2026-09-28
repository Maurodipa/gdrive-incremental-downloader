#!/usr/bin/env python3
"""
gdrive_downloader.py
====================
Agente incrementale per il download di Google Drive su disco locale.

Caratteristiche principali:
  - Stato persistente via SQLite (resumability assoluta)
  - Verifica integrità MD5 dopo ogni download
  - Audit/Sync mode per individuare file mancanti o corrotti
  - Riproduzione fedele dell'alberatura cartelle
  - Sanitizzazione nomi file cross-platform
  - Exponential backoff su errori HTTP 429/500/503
  - Gestione file nativi Google Workspace (export automatico)
  - OAuth 2.0 con token persistente

Uso rapido:
  python gdrive_downloader.py --mode download   # Download incrementale
  python gdrive_downloader.py --mode audit      # Audit / verifica integrità
"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Dipendenze esterne (vedi requirements.txt) ────────────────────────────────
try:
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except ImportError as exc:
    sys.exit(
        f"[ERRORE] Libreria mancante: {exc}\n"
        "Esegui: pip install -r requirements.txt"
    )

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURAZIONE GLOBALE
# ══════════════════════════════════════════════════════════════════════════════

# Scope OAuth 2.0 — sola lettura per massima sicurezza
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Percorso credenziali OAuth scaricate da Google Cloud Console
CREDENTIALS_FILE = "credentials.json"

# Token persistente salvato dopo il primo login
TOKEN_FILE = "token.json"

# Database SQLite dello stato
DB_FILE = "gdrive_state.db"

# Cartella radice locale dove verranno salvati i file
DEFAULT_OUTPUT_DIR = "gdrive_backup"

# Log file
LOG_FILE = "gdrive_downloader.log"

# Dimensione chunk per lo streaming del download (10 MB)
CHUNK_SIZE = 10 * 1024 * 1024

# Tentativi massimi prima di marcare un file come "errore permanente"
MAX_RETRIES = 7

# Attesa base (secondi) per exponential backoff
BACKOFF_BASE = 2

# Formato export per i file nativi Google Workspace
# chiave = mimeType Drive  →  (estensione_locale, mimeType_export)
WORKSPACE_EXPORT_MAP = {
    "application/vnd.google-apps.document": (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "application/vnd.google-apps.spreadsheet": (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "application/vnd.google-apps.presentation": (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "application/vnd.google-apps.drawing": (".pdf", "application/pdf"),
    "application/vnd.google-apps.form": (".pdf", "application/pdf"),
    "application/vnd.google-apps.script": (
        ".json",
        "application/vnd.google-apps.script+json",
    ),
    "application/vnd.google-apps.site": (".pdf", "application/pdf"),
    "application/vnd.google-apps.jam": (".pdf", "application/pdf"),
}

# Tipi ignorati (cartelle, shortcut, ecc.) — non da scaricare come file
SKIP_MIME_TYPES = {
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.unknown",
}


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configura il logger su file + console."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)-8s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)
    return logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# AUTENTICAZIONE OAUTH 2.0
# ══════════════════════════════════════════════════════════════════════════════

def get_drive_service(credentials_file: str = CREDENTIALS_FILE,
                      token_file: str = TOKEN_FILE):
    """
    Restituisce un client autenticato Google Drive v3.
    - Al primo avvio apre il browser per il consenso OAuth.
    - Nei riavvii successivi riutilizza token.json senza interazione.
    """
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"File credenziali '{credentials_file}' non trovato.\n"
                    "Leggi il README.md per le istruzioni di configurazione."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            # Avvia un server locale su porta 0 (porta casuale libera)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE SQLITE — GESTIONE STATO
# ══════════════════════════════════════════════════════════════════════════════

class StateDB:
    """
    Gestisce il registro persistente dei file tramite SQLite.

    Schema tabella 'files':
      drive_id      TEXT PRIMARY KEY  — ID univoco Google Drive
      name          TEXT              — nome originale del file
      local_path    TEXT              — percorso assoluto locale di destinazione
      mime_type     TEXT              — MIME type Drive
      md5_drive     TEXT              — checksum MD5 da Drive (NULL per Workspace)
      size_bytes    INTEGER           — dimensione in byte (NULL per Workspace)
      status        TEXT              — 'pending' | 'completed' | 'error' | 'skipped'
      error_count   INTEGER           — numero di tentativi falliti
      last_updated  TEXT              — timestamp ISO 8601 dell'ultimo aggiornamento

    Status 'skipped': file che l'API non è in grado di fornire (es. troppo grande
    per l'export Workspace). Non vengono riaccodati e sono riportati nell'audit.
    """

    STATUS_PENDING = "pending"
    STATUS_COMPLETED = "completed"
    STATUS_ERROR = "error"
    STATUS_SKIPPED = "skipped"

    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                drive_id     TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                local_path   TEXT NOT NULL,
                mime_type    TEXT,
                md5_drive    TEXT,
                size_bytes   INTEGER,
                status       TEXT NOT NULL DEFAULT 'pending',
                error_count  INTEGER NOT NULL DEFAULT 0,
                last_updated TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_status ON files(status);

            CREATE TABLE IF NOT EXISTS scan_progress (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                next_page_token TEXT,
                last_scan_ts  TEXT
            );
        """)
        self._conn.commit()

    # ── Upsert di un file ────────────────────────────────────────────────────

    def upsert_file(self, drive_id: str, name: str, local_path: str,
                    mime_type: str, md5_drive: Optional[str],
                    size_bytes: Optional[int]):
        """
        Inserisce un nuovo record oppure aggiorna i metadati se già esiste.
        Non sovrascrive 'status', 'error_count' o 'last_updated' su record
        già completati o saltati (skipped).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO files
                (drive_id, name, local_path, mime_type, md5_drive, size_bytes,
                 status, error_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
            ON CONFLICT(drive_id) DO UPDATE SET
                name         = excluded.name,
                local_path   = excluded.local_path,
                mime_type    = excluded.mime_type,
                md5_drive    = excluded.md5_drive,
                size_bytes   = excluded.size_bytes,
                last_updated = CASE
                    WHEN status IN ('completed', 'skipped') THEN last_updated
                    ELSE excluded.last_updated
                END
        """, (drive_id, name, local_path, mime_type, md5_drive, size_bytes, now))
        self._conn.commit()

    # ── Aggiornamento stato ───────────────────────────────────────────────────

    def mark_completed(self, drive_id: str):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE files SET status='completed', last_updated=? WHERE drive_id=?",
            (now, drive_id),
        )
        self._conn.commit()

    def mark_error(self, drive_id: str):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE files
               SET status='error',
                   error_count = error_count + 1,
                   last_updated = ?
               WHERE drive_id = ?""",
            (now, drive_id),
        )
        self._conn.commit()

    def mark_skipped(self, drive_id: str, reason: str = ""):
        """
        Marca un file come 'skipped': non verrà mai riaccodato.
        Usato per file che l'API non può fornire (es. exportSizeLimitExceeded).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE files SET status='skipped', last_updated=? WHERE drive_id=?",
            (now, drive_id),
        )
        self._conn.commit()

    def reset_to_pending(self, drive_id: str):
        """Rimette in coda un file (es. dopo hash mismatch o file rimosso)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE files SET status='pending', last_updated=? WHERE drive_id=?",
            (now, drive_id),
        )
        self._conn.commit()

    # ── Query ────────────────────────────────────────────────────────────────

    def get_pending_files(self) -> list:
        """Restituisce tutti i file in stato 'pending' o 'error' (esclude 'skipped' e 'completed')."""
        cur = self._conn.execute(
            "SELECT * FROM files WHERE status IN ('pending', 'error') ORDER BY drive_id"
        )
        return cur.fetchall()

    def get_all_files(self) -> list:
        cur = self._conn.execute("SELECT * FROM files ORDER BY drive_id")
        return cur.fetchall()

    def count_by_status(self) -> dict:
        cur = self._conn.execute(
            "SELECT status, COUNT(*) as n FROM files GROUP BY status"
        )
        return {row["status"]: row["n"] for row in cur.fetchall()}

    def file_exists(self, drive_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM files WHERE drive_id=?", (drive_id,)
        )
        return cur.fetchone() is not None

    def get_file(self, drive_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM files WHERE drive_id=?", (drive_id,)
        )
        return cur.fetchone()

    # ── Scan progress (page token) ────────────────────────────────────────────

    def save_page_token(self, token: Optional[str]):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO scan_progress (id, next_page_token, last_scan_ts)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                next_page_token = excluded.next_page_token,
                last_scan_ts    = excluded.last_scan_ts
        """, (token, now))
        self._conn.commit()

    def get_page_token(self) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT next_page_token FROM scan_progress WHERE id=1"
        )
        row = cur.fetchone()
        return row["next_page_token"] if row else None

    def close(self):
        self._conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY — NOMI FILE E PERCORSI
# ══════════════════════════════════════════════════════════════════════════════

# Caratteri vietati su Windows (è il superset più restrittivo)
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Nomi riservati Windows (case-insensitive)
_RESERVED_NAMES = re.compile(
    r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', re.IGNORECASE
)


def sanitize_name(name: str, replacement: str = "_") -> str:
    """
    Rende un nome file/cartella valido su Windows, macOS e Linux.
    - Sostituisce caratteri non validi con `replacement`
    - Rimuove spazi e punti iniziali/finali
    - Gestisce i nomi riservati Windows aggiungendo un prefisso '_'
    - Tronca a 255 caratteri (limite comune dei filesystem)
    """
    sanitized = _INVALID_CHARS.sub(replacement, name)
    sanitized = sanitized.strip(". ")
    if not sanitized:
        sanitized = replacement
    if _RESERVED_NAMES.match(sanitized):
        sanitized = replacement + sanitized
    return sanitized[:255]


def build_local_path(output_dir: str,
                     ancestors: list[str],
                     file_name: str,
                     extra_ext: str = "") -> Path:
    """
    Costruisce il percorso locale partendo dalla cartella di output,
    applicando la sanitizzazione a ogni componente del percorso.

    ancestors: lista di nomi cartelle in ordine dalla radice alla foglia.
    extra_ext:  estensione aggiuntiva per i file Workspace (es. '.docx').
    """
    parts = [sanitize_name(p) for p in ancestors]
    safe_name = sanitize_name(file_name) + extra_ext
    return Path(output_dir).joinpath(*parts, safe_name)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY — HASHING
# ══════════════════════════════════════════════════════════════════════════════

def md5_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calcola l'MD5 di un file locale in modo streaming (bassa memoria)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY — EXPONENTIAL BACKOFF
# ══════════════════════════════════════════════════════════════════════════════

def with_backoff(func, *args, logger: logging.Logger, max_retries: int = MAX_RETRIES,
                 base: float = BACKOFF_BASE, **kwargs):
    """
    Esegue `func(*args, **kwargs)` con exponential backoff su:
      - HttpError con status 429, 500, 502, 503, 504
      - Eccezioni di rete generiche (ConnectionError, TimeoutError, OSError)
      - httplib2.ServerNotFoundError  ← caduta DNS / connessione persa
      - socket.gaierror               ← risoluzione DNS fallita

    Solleva l'ultima eccezione se esaurisce i tentativi.
    """
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except HttpError as exc:
            if exc.resp.status in (429, 500, 502, 503, 504):
                wait = base ** attempt
                logger.warning(
                    "HTTP %s — tentativo %d/%d, attesa %.0fs…",
                    exc.resp.status, attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise  # Errori non recuperabili (401, 403, 404…)
        except (
            ConnectionError,
            TimeoutError,
            OSError,
            socket.gaierror,
            httplib2.error.ServerNotFoundError,
        ) as exc:
            wait = base ** attempt
            logger.warning(
                "Errore di rete (%s) — tentativo %d/%d, attesa %.0fs…",
                exc, attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"Esauriti {max_retries} tentativi.")


# ══════════════════════════════════════════════════════════════════════════════
# RISOLUZIONE PERCORSI DRIVE → PERCORSI LOCALI
# ══════════════════════════════════════════════════════════════════════════════

class PathResolver:
    """
    Risolve l'alberatura di Google Drive costruendo per ogni file il percorso
    locale corrispondente.

    OTTIMIZZAZIONE CHIAVE — cache del percorso completo per folder ID:
      - La prima volta che si incontra una cartella, si risale tutta la gerarchia
        fino alla radice con chiamate API e si memorizza il percorso completo.
      - Ogni file successivo nella stessa cartella (o in una cartella già vista)
        riceve il percorso direttamente dalla cache → 0 chiamate API aggiuntive.
      - Durante la risalita vengono cachati anche tutti i percorsi intermedi,
        così cartelle "sorelle" a qualsiasi livello beneficiano subito della cache.

    Risultato: da O(depth × n_files) a O(unique_folders × depth) chiamate API.
    """

    def __init__(self, service, logger: logging.Logger):
        self.service = service
        self.logger = logger
        # cache: folder_id → lista completa di nomi dalla radice alla cartella
        # Es: "folder_xyz" → ["Documenti", "Lavoro", "2025"]
        self._path_cache: dict[str, list[str]] = {}

    def resolve_ancestors(self, parents: list[str]) -> list[str]:
        """
        Restituisce la lista ordinata di nomi cartelle dalla radice alla foglia.

        Algoritmo:
          1. Controlla se il parent diretto è già in cache → ritorno immediato.
          2. Altrimenti risale iterativamente la gerarchia fino alla radice
             o a un nodo già cachato.
          3. Costruisce il percorso completo e popola la cache per ogni
             nodo intermedio incontrato durante la risalita.
        """
        if not parents:
            return []

        parent_id = parents[0]

        # ── Fast path: cache hit esatto ──────────────────────────────────────
        if parent_id in self._path_cache:
            return self._path_cache[parent_id]

        # ── Risalita iterativa dalla foglia alla radice ───────────────────────
        # chain_ids[i], chain_names[i] = cartella incontrata nell'ordine leaf→root
        chain_ids: list[str] = []
        chain_names: list[str] = []
        seen: set[str] = set()
        current_id: Optional[str] = parent_id
        cached_prefix: list[str] = []  # percorso già noto se troviamo un nodo cachato

        while current_id:
            # Cache hit su un antenato intermedio → usiamo il suo percorso come prefisso
            if current_id in self._path_cache:
                cached_prefix = self._path_cache[current_id]
                break

            if current_id in seen:
                break  # ciclo in Drive (raro ma possibile)
            seen.add(current_id)

            try:
                meta = with_backoff(
                    self.service.files().get(
                        fileId=current_id,
                        fields="name,parents,mimeType",
                    ).execute,
                    logger=self.logger,
                )
            except (HttpError, RuntimeError):
                break

            if meta.get("mimeType") != "application/vnd.google-apps.folder":
                break

            chain_ids.append(current_id)
            chain_names.append(meta.get("name", current_id))

            grandparents = meta.get("parents", [])
            current_id = grandparents[0] if grandparents else None

        # ── Ricostruzione percorsi e popolamento cache ────────────────────────
        # chain_ids/chain_names vanno da leaf→root; invertiamo per avere root→leaf
        chain_ids.reverse()
        chain_names.reverse()

        # Per ogni nodo nella catena, calcola e cacha il percorso completo
        # Es. chain = ["Documenti", "Lavoro", "2025"] con prefix = []
        #   → "Documenti_id"          : ["Documenti"]
        #   → "Lavoro_id"             : ["Documenti", "Lavoro"]
        #   → "2025_id" (= parent_id) : ["Documenti", "Lavoro", "2025"]
        for i, (fid, fname) in enumerate(zip(chain_ids, chain_names)):
            self._path_cache[fid] = cached_prefix + chain_names[: i + 1]

        return self._path_cache.get(parent_id, cached_prefix)


# ══════════════════════════════════════════════════════════════════════════════
# SCANNER — POPOLA IL DATABASE CON L'ELENCO COMPLETO DEI FILE
# ══════════════════════════════════════════════════════════════════════════════

def scan_drive(service, db: StateDB, output_dir: str,
               logger: logging.Logger, resolver: PathResolver):
    """
    Percorre l'intero Drive con paginazione e popola il database SQLite.
    Riprende dall'ultimo pageToken salvato in caso di interruzione.
    """
    page_token = db.get_page_token()

    # ── Stato del DB dalla sessione precedente ────────────────────────────────
    prev_counts = db.count_by_status()
    prev_total = sum(prev_counts.values())
    if prev_total > 0:
        logger.info(
            "DB esistente: %d file già registrati "
            "(completati=%d  in-attesa=%d  errori=%d)",
            prev_total,
            prev_counts.get("completed", 0),
            prev_counts.get("pending", 0),
            prev_counts.get("error", 0),
        )

    if page_token:
        logger.info("Ripresa scansione dal pageToken salvato (nuovi file da questa sessione: 0)…")
    else:
        logger.info("Inizio scansione completa di Google Drive…")

    fields = (
        "nextPageToken,"
        "files(id,name,mimeType,md5Checksum,size,parents,trashed)"
    )

    # Conta i file nuovi trovati in questa sessione
    session_found = 0

    while True:
        params = {
            "pageSize": 1000,
            "fields": fields,
            "q": "trashed=false",
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
        }
        if page_token:
            params["pageToken"] = page_token

        response = with_backoff(
            service.files().list(**params).execute,
            logger=logger,
        )

        files = response.get("files", [])
        session_found += len(files)

        for item in files:
            mime_type = item.get("mimeType", "")

            # Salta tipi non scaricabili come file
            if mime_type in SKIP_MIME_TYPES:
                continue

            drive_id = item["id"]
            name = item.get("name", drive_id)
            md5_drive = item.get("md5Checksum")
            size_bytes = int(item["size"]) if item.get("size") else None

            parents = item.get("parents", [])
            ancestors = resolver.resolve_ancestors(parents)

            # Determina l'eventuale estensione aggiuntiva per file Workspace
            extra_ext = ""
            if mime_type in WORKSPACE_EXPORT_MAP:
                extra_ext = WORKSPACE_EXPORT_MAP[mime_type][0]

            local_path = build_local_path(output_dir, ancestors, name, extra_ext)

            db.upsert_file(
                drive_id=drive_id,
                name=name,
                local_path=str(local_path),
                mime_type=mime_type,
                md5_drive=md5_drive,
                size_bytes=size_bytes,
            )

        page_token = response.get("nextPageToken")
        db.save_page_token(page_token)

        # Mostra totale cumulativo = sessioni precedenti + questa sessione
        cumulative = prev_total + session_found
        logger.info(
            "  Scansionati %d nuovi in questa sessione  (totale DB: %d)…",
            session_found, cumulative,
        )

        if not page_token:
            break

    # Scansione completata: cancella il page_token salvato
    db.save_page_token(None)
    final_total = sum(db.count_by_status().values())
    logger.info(
        "Scansione completata. Nuovi questa sessione: %d  |  Totale nel DB: %d",
        session_found, final_total,
    )


# ══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD SINGOLO FILE
# ══════════════════════════════════════════════════════════════════════════════

def download_file(service, row: sqlite3.Row,
                  logger: logging.Logger) -> bool:
    """
    Scarica un singolo file (binario o Workspace) e verifica l'integrità.

    Ritorna True se il download è riuscito, False in caso di errore.
    """
    drive_id = row["drive_id"]
    name = row["name"]
    local_path = Path(row["local_path"])
    mime_type = row["mime_type"]
    md5_drive = row["md5_drive"]

    # ── Crea directory di destinazione ───────────────────────────────────────
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # ── File temporaneo per download sicuro ──────────────────────────────────
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")

    def _safe_unlink(path: Path):
        """Elimina un file ignorando errori di lock su Windows."""
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            logger.warning("  Impossibile eliminare il file temporaneo (lock): %s", path)

    try:
        if mime_type in WORKSPACE_EXPORT_MAP:
            success = _download_workspace(service, drive_id, name,
                                          mime_type, tmp_path, logger)
        else:
            success = _download_binary(service, drive_id, name,
                                       tmp_path, md5_drive, logger)

        if success:
            # Sposta il file temporaneo nella destinazione finale (atomico)
            if local_path.exists():
                local_path.unlink()
            tmp_path.rename(local_path)
            return "ok"
        else:
            _safe_unlink(tmp_path)
            return "error"

    except ExportTooLargeError as exc:
        # File Workspace troppo grande anche per il fallback PDF → skip permanente
        logger.warning("  SKIPPED (troppo grande per l'export): %s", name)
        _safe_unlink(tmp_path)
        return "skipped"

    except Exception as exc:
        logger.error("Eccezione durante il download di '%s': %s", name, exc, exc_info=False)
        _safe_unlink(tmp_path)
        return "error"


def _download_binary(service, drive_id: str, name: str,
                     tmp_path: Path, md5_drive: Optional[str],
                     logger: logging.Logger) -> bool:
    """
    Scarica un file binario con streaming e verifica MD5.
    """
    logger.debug("  Download binario: %s", name)

    request = service.files().get_media(
        fileId=drive_id,
        supportsAllDrives=True,
    )

    with open(tmp_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
        done = False
        while not done:
            status, done = with_backoff(
                downloader.next_chunk,
                logger=logger,
            )
            if status:
                pct = int(status.progress() * 100)
                logger.debug("    %s … %d%%", name, pct)

    # ── Verifica MD5 ─────────────────────────────────────────────────────────
    if md5_drive:
        md5_local = md5_of_file(tmp_path)
        if md5_local.lower() != md5_drive.lower():
            logger.warning(
                "  HASH MISMATCH '%s' — drive=%s  locale=%s — file eliminato, riaccodato.",
                name, md5_drive, md5_local,
            )
            return False
        logger.debug("  MD5 OK: %s", name)
    else:
        logger.debug("  MD5 non disponibile per '%s' (file senza checksum).", name)

    return True


class ExportTooLargeError(Exception):
    """Sollevata quando Drive rifiuta l'export per dimensione eccessiva."""


def _download_workspace(service, drive_id: str, name: str,
                        mime_type: str, tmp_path: Path,
                        logger: logging.Logger) -> bool:
    """
    Esporta un file nativo Google Workspace nel formato Office corrispondente.
    Per questi file non è disponibile l'MD5; il successo si basa su HTTP 200.

    Strategia per exportSizeLimitExceeded (file > ~10MB):
      1. Prova il formato primario (es. .docx).
      2. Se fallisce per dimensione, ritenta in PDF.
      3. Se anche il PDF fallisce → solleva ExportTooLargeError (file 'skipped').
    """
    export_ext, export_mime = WORKSPACE_EXPORT_MAP[mime_type]
    logger.debug("  Export Workspace: %s → %s", name, export_ext)

    try:
        with_backoff(
            _do_workspace_export,
            service, drive_id, export_mime, tmp_path,
            logger=logger,
        )
        return True
    except HttpError as exc:
        is_size_limit = (
            exc.resp.status == 403
            and "exportSizeLimitExceeded" in str(exc.error_details)
        )
        if not is_size_limit:
            raise  # altro 403 (permessi, ecc.) → ri-solleva normalmente

        # ── Fallback PDF ──────────────────────────────────────────────────
        if export_mime != "application/pdf":
            logger.warning(
                "  Export %s troppo grande, provo PDF come fallback: %s",
                export_ext, name,
            )
            pdf_tmp = tmp_path.with_suffix(".pdf.tmp")
            try:
                with_backoff(
                    _do_workspace_export,
                    service, drive_id, "application/pdf", pdf_tmp,
                    logger=logger,
                )
                pdf_tmp.replace(tmp_path)
                logger.info("  ✓ Fallback PDF riuscito: %s", name)
                return True
            except HttpError:
                pdf_tmp.unlink(missing_ok=True)
                logger.warning("  Anche il fallback PDF è fallito: %s", name)

        raise ExportTooLargeError(name)


def _do_workspace_export(service, drive_id: str, export_mime: str,
                         tmp_path: Path):
    """Helper separato per consentire il retry tramite with_backoff."""
    request = service.files().export_media(
        fileId=drive_id, mimeType=export_mime
    )
    fh = io.FileIO(str(tmp_path), mode="wb")
    try:
        downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    finally:
        fh.close()  # garantisce la chiusura anche in caso di eccezione (evita PermissionError su Windows)


# ══════════════════════════════════════════════════════════════════════════════
# MODALITÀ DOWNLOAD INCREMENTALE
# ══════════════════════════════════════════════════════════════════════════════

def run_download(service, db: StateDB, output_dir: str,
                 logger: logging.Logger, resolver: PathResolver):
    """
    Fase 1 — Scansione (popola/aggiorna il database)
    Fase 2 — Download incrementale (salta i file già completati)
    """
    # ── Fase 1: scansione ────────────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("FASE 1 — Scansione alberatura Google Drive")
    logger.info("═" * 60)
    scan_drive(service, db, output_dir, logger, resolver)

    # ── Riepilogo pre-download ────────────────────────────────────────────────
    counts = db.count_by_status()
    logger.info(
        "Stato DB: completati=%d  in-attesa=%d  errori=%d",
        counts.get("completed", 0),
        counts.get("pending", 0),
        counts.get("error", 0),
    )

    # ── Fase 2: download ─────────────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("FASE 2 — Download incrementale")
    logger.info("═" * 60)

    pending = db.get_pending_files()
    total = len(pending)
    logger.info("File da scaricare: %d", total)

    ok = 0
    errors = 0
    skipped = 0

    for idx, row in enumerate(pending, start=1):
        drive_id = row["drive_id"]
        name = row["name"]
        logger.info("[%d/%d] %s", idx, total, name)

        result = download_file(service, row, logger)

        if result == "ok":
            db.mark_completed(drive_id)
            ok += 1
        elif result == "skipped":
            db.mark_skipped(drive_id)
            skipped += 1
        else:
            db.mark_error(drive_id)
            errors += 1
            logger.warning("  ✗ Errore: %s (riaccodato)", name)

    logger.info("═" * 60)
    logger.info(
        "Download completato — OK: %d  Errori: %d  Saltati: %d  Totale: %d",
        ok, errors, skipped, total,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MODALITÀ AUDIT / VERIFICA INTEGRITÀ
# ══════════════════════════════════════════════════════════════════════════════

def run_audit(service, db: StateDB, output_dir: str,
              logger: logging.Logger, resolver: PathResolver,
              report_path: str = "audit_report.txt"):
    """
    Modalità Audit:
    1. Recupera l'elenco completo dei file da Drive (re-scan).
    2. Confronta con il database locale.
    3. Verifica che i file completati esistano su disco e abbiano l'MD5 corretto.
    4. Genera audit_report.txt con tutti i problemi rilevati.
    """
    logger.info("═" * 60)
    logger.info("MODALITÀ AUDIT — Verifica integrità completa")
    logger.info("═" * 60)

    # ── 1. Re-scan per individuare eventuali file nuovi/mancanti ─────────────
    logger.info("Re-scan Drive in corso…")
    scan_drive(service, db, output_dir, logger, resolver)

    # ── 2. Verifica ogni file nel database ───────────────────────────────────
    all_files = db.get_all_files()
    total = len(all_files)
    logger.info("File nel database: %d", total)

    missing = []         # file 'completed' ma mancanti su disco
    corrupted = []       # file 'completed' con MD5 sbagliato
    pending_list = []    # file ancora in 'pending' o 'error'

    for idx, row in enumerate(all_files, start=1):
        drive_id = row["drive_id"]
        name = row["name"]
        local_path = Path(row["local_path"])
        md5_drive = row["md5_drive"]
        status = row["status"]

        if idx % 500 == 0:
            logger.info("  Verificati %d / %d file…", idx, total)

        if status == "completed":
            # Verifica che il file esista fisicamente
            if not local_path.exists():
                logger.warning("MANCANTE: %s → %s", name, local_path)
                db.reset_to_pending(drive_id)
                missing.append((drive_id, name, str(local_path)))
                continue

            # Verifica MD5 (solo per file binari — non Workspace)
            if md5_drive:
                md5_local = md5_of_file(local_path)
                if md5_local.lower() != md5_drive.lower():
                    logger.warning(
                        "CORROTTO: %s  drive=%s  locale=%s",
                        name, md5_drive, md5_local,
                    )
                    # Elimina il file corrotto e rimetti in coda
                    local_path.unlink()
                    db.reset_to_pending(drive_id)
                    corrupted.append((drive_id, name, str(local_path)))

        elif status in ("pending", "error"):
            pending_list.append((drive_id, name, status, row["error_count"]))

    # ── 3. Scrittura report ───────────────────────────────────────────────────
    _write_audit_report(
        report_path=report_path,
        total=total,
        counts=db.count_by_status(),
        missing=missing,
        corrupted=corrupted,
        pending_list=pending_list,
        logger=logger,
    )

    logger.info("Report salvato in: %s", report_path)
    logger.info(
        "Riepilogo — Mancanti: %d  Corrotti: %d  In attesa: %d",
        len(missing), len(corrupted), len(pending_list),
    )


def _write_audit_report(report_path: str, total: int, counts: dict,
                        missing: list, corrupted: list,
                        pending_list: list, logger: logging.Logger):
    """Scrive il file di report in formato testo leggibile."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(report_path, "w", encoding="utf-8") as rpt:
        rpt.write("=" * 70 + "\n")
        rpt.write("AUDIT REPORT — Google Drive Backup\n")
        rpt.write(f"Generato il: {now}\n")
        rpt.write("=" * 70 + "\n\n")

        rpt.write("RIEPILOGO\n")
        rpt.write("-" * 40 + "\n")
        rpt.write(f"  Totale file nel database : {total}\n")
        for status, n in sorted(counts.items()):
            rpt.write(f"  {status:<25}: {n}\n")
        rpt.write(f"\n  File mancanti su disco   : {len(missing)}\n")
        rpt.write(f"  File corrotti (hash fail) : {len(corrupted)}\n")
        rpt.write(f"  File non ancora scaricati : {len(pending_list)}\n\n")

        if missing:
            rpt.write("FILE MANCANTI SU DISCO (rimessi in coda)\n")
            rpt.write("-" * 40 + "\n")
            for drive_id, name, path in missing:
                rpt.write(f"  [{drive_id}] {name}\n    → {path}\n")
            rpt.write("\n")

        if corrupted:
            rpt.write("FILE CORROTTI / HASH MISMATCH (rimessi in coda)\n")
            rpt.write("-" * 40 + "\n")
            for drive_id, name, path in corrupted:
                rpt.write(f"  [{drive_id}] {name}\n    → {path}\n")
            rpt.write("\n")

        if pending_list:
            rpt.write("FILE NON ANCORA SCARICATI\n")
            rpt.write("-" * 40 + "\n")
            for drive_id, name, status, err_count in pending_list:
                rpt.write(
                    f"  [{drive_id}] {name}  (stato={status}, errori={err_count})\n"
                )
            rpt.write("\n")

        if not missing and not corrupted and not pending_list:
            rpt.write("✔  BACKUP COMPLETO E INTEGRO — nessun problema rilevato.\n\n")

        rpt.write("=" * 70 + "\n")

    logger.info("Report scritto: %s", report_path)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gdrive_downloader.py",
        description="Agente per il backup incrementale e verificabile di Google Drive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python gdrive_downloader.py --mode download
  python gdrive_downloader.py --mode audit
  python gdrive_downloader.py --mode download --output /mnt/backup/drive --verbose
""",
    )
    parser.add_argument(
        "--mode",
        choices=["download", "audit"],
        required=True,
        help="'download' = download incrementale; 'audit' = verifica integrità",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Cartella locale di destinazione (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--db",
        default=DB_FILE,
        help=f"Percorso del database SQLite (default: {DB_FILE})",
    )
    parser.add_argument(
        "--credentials",
        default=CREDENTIALS_FILE,
        help=f"Percorso del file credentials.json (default: {CREDENTIALS_FILE})",
    )
    parser.add_argument(
        "--token",
        default=TOKEN_FILE,
        help=f"Percorso del file token.json (default: {TOKEN_FILE})",
    )
    parser.add_argument(
        "--report",
        default="audit_report.txt",
        help="Percorso del report di audit (default: audit_report.txt)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Abilita log dettagliato (DEBUG)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logger = setup_logging(args.verbose)

    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║   Google Drive Incremental Downloader    ║")
    logger.info("╚══════════════════════════════════════════╝")
    logger.info("Modalità : %s", args.mode.upper())
    logger.info("Output   : %s", args.output)
    logger.info("Database : %s", args.db)

    # ── Autenticazione ────────────────────────────────────────────────────────
    logger.info("Autenticazione Google Drive…")
    service = get_drive_service(
        credentials_file=args.credentials,
        token_file=args.token,
    )
    logger.info("Autenticazione completata.")

    # ── Database ──────────────────────────────────────────────────────────────
    db = StateDB(args.db)

    # ── Path Resolver ─────────────────────────────────────────────────────────
    resolver = PathResolver(service, logger)

    try:
        if args.mode == "download":
            run_download(service, db, args.output, logger, resolver)
        elif args.mode == "audit":
            run_audit(service, db, args.output, logger, resolver,
                      report_path=args.report)
    except KeyboardInterrupt:
        logger.info("\nInterrotto dall'utente. Il progresso è salvato nel DB.")
    finally:
        db.close()

    logger.info("Fine.")


if __name__ == "__main__":
    main()
