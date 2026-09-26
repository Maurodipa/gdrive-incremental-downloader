# Google Drive Incremental Downloader

> Agente Python per il backup **incrementale**, **sicuro** e **verificabile** dell'intero contenuto di Google Drive su disco locale.

---

## Indice

1. [Configurazione Google Cloud Console](#1-configurazione-google-cloud-console)
2. [Installazione](#2-installazione)
3. [Modalità Download Incrementale](#3-modalità-download-incrementale)
4. [Modalità Audit / Verifica](#4-modalità-audit--verifica)
5. [Struttura dei File Generati](#5-struttura-dei-file-generati)
6. [Architettura e Funzionamento](#6-architettura-e-funzionamento)
7. [Riferimento Argomenti CLI](#7-riferimento-argomenti-cli)
8. [Risoluzione dei Problemi](#8-risoluzione-dei-problemi)

---

## 1. Configurazione Google Cloud Console

> [!IMPORTANT]
> Questo passaggio è necessario **una volta sola**. Sono richiesti circa 10 minuti.

### 1.1 Crea un progetto (se non ne hai già uno)

1. Vai su [console.cloud.google.com](https://console.cloud.google.com).
2. Clicca sul menu a tendina del progetto (in alto a sinistra) → **Nuovo progetto**.
3. Assegna un nome (es. `gdrive-backup`) e clicca **Crea**.

### 1.2 Abilita le API di Google Drive

1. Nel pannello laterale: **API e servizi** → **Libreria**.
2. Cerca **Google Drive API** e clicca **Abilita**.

### 1.3 Configura la schermata di consenso OAuth

1. **API e servizi** → **Schermata consenso OAuth**.
2. Tipo utente: **Esterno** → Crea.
3. Compila i campi obbligatori (Nome app, email supporto, email sviluppatore).
4. Aggiungi lo scope: `.../auth/drive.readonly` (sola lettura).
5. Aggiungi il tuo indirizzo Gmail nella sezione **Utenti di test**.
6. Salva e continua fino alla fine.

### 1.4 Crea le credenziali OAuth 2.0

1. **API e servizi** → **Credenziali** → **Crea credenziali** → **ID client OAuth**.
2. Tipo applicazione: **App desktop**.
3. Assegna un nome (es. `gdrive-backup-client`).
4. Clicca **Crea** → poi **Scarica JSON**.
5. Rinomina il file scaricato in **`credentials.json`** e copialo nella stessa cartella dello script.

> [!CAUTION]
> Non condividere mai `credentials.json` o `token.json`. Aggiungili al `.gitignore` se usi Git.

---

## 2. Installazione

### 2.1 Prerequisiti

- Python 3.10 o superiore
- `pip` aggiornato

### 2.2 Ambiente virtuale (consigliato)

```bash
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 2.3 Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2.4 Struttura minima prima del primo avvio

```
Download GDrive Files/
├── gdrive_downloader.py   ← lo script
├── requirements.txt
└── credentials.json       ← scaricato da Google Cloud Console
```

---

## 3. Modalità Download Incrementale

### Avvio standard

```bash
python gdrive_downloader.py --mode download
```

Al **primo avvio** si aprirà automaticamente il browser per il consenso OAuth. Dopo aver autorizzato l'accesso, il token viene salvato in `token.json` e i riavvii successivi non richiedono alcuna interazione.

### Specificare una cartella di destinazione personalizzata

```bash
python gdrive_downloader.py --mode download --output D:\Backup\MyDrive
```

### Con log dettagliato (DEBUG)

```bash
python gdrive_downloader.py --mode download --verbose
```

### Come funziona (step-by-step)

1. **Scansione** — L'agente elenca tutti i file su Drive usando la paginazione e popola il database `gdrive_state.db`. Se la scansione viene interrotta, riprende dall'ultimo `pageToken` salvato.
2. **Download** — Per ogni file in stato `pending` o `error` nel database:
   - I file **binari** vengono scaricati in streaming con verifica MD5.
   - I file **Google Workspace** (Docs/Sheets/Slides) vengono esportati in formato Office (`.docx`, `.xlsx`, `.pptx`).
3. **Verifica** — Dopo ogni download, l'MD5 locale viene confrontato con quello di Drive. In caso di mismatch, il file viene eliminato e rimesso in coda.
4. **Riprendibilità** — Puoi interrompere con `Ctrl+C` in qualsiasi momento. Al riavvio, i file già completati verranno saltati automaticamente.

### Esempio di output console

```
2026-09-26 14:00:01 [INFO    ] ╔══════════════════════════════════════════╗
2026-09-26 14:00:01 [INFO    ] ║   Google Drive Incremental Downloader    ║
2026-09-26 14:00:01 [INFO    ] ╚══════════════════════════════════════════╝
2026-09-26 14:00:01 [INFO    ] Modalità : DOWNLOAD
2026-09-26 14:00:03 [INFO    ] FASE 1 — Scansione alberatura Google Drive
2026-09-26 14:00:05 [INFO    ]   Scansionati 1000 file fin ora…
2026-09-26 14:02:11 [INFO    ] Scansione completata. Totale file trovati: 48320
2026-09-26 14:02:11 [INFO    ] FASE 2 — Download incrementale
2026-09-26 14:02:11 [INFO    ] File da scaricare: 48320
2026-09-26 14:02:12 [INFO    ] [1/48320] Relazione Q1 2025.docx
2026-09-26 14:02:14 [INFO    ] [2/48320] budget_2025.xlsx
...
```

---

## 4. Modalità Audit / Verifica

La modalità audit esegue una verifica completa dell'integrità del backup:

```bash
python gdrive_downloader.py --mode audit
```

Con report personalizzato:

```bash
python gdrive_downloader.py --mode audit --report audit_2026-09-26.txt
```

### Cosa fa l'audit

| Controllo | Azione |
|---|---|
| File in DB `completed` ma assente su disco | Rimette in coda (`pending`) |
| File in DB `completed` con MD5 errato | Elimina il file corrotto, rimette in coda |
| File nuovo su Drive non ancora nel DB | Aggiunge al DB come `pending` |
| File ancora in `pending` / `error` | Li elenca nel report |

### Esempio di `audit_report.txt`

```
======================================================================
AUDIT REPORT — Google Drive Backup
Generato il: 2026-09-26 16:30:00
======================================================================

RIEPILOGO
----------------------------------------
  Totale file nel database : 48320
  completed                : 48315
  error                    :     3
  pending                  :     2

  File mancanti su disco   : 1
  File corrotti (hash fail) : 2
  File non ancora scaricati : 5

FILE MANCANTI SU DISCO (rimessi in coda)
----------------------------------------
  [1a2b3c] Contratto_Fornitore.pdf
    → D:\Backup\MyDrive\Documenti\Legale\Contratto_Fornitore.pdf

FILE CORROTTI / HASH MISMATCH (rimessi in coda)
----------------------------------------
  [4d5e6f] Dataset_Finale.csv
    → D:\Backup\MyDrive\Progetti\Dataset_Finale.csv
...

✔  Dopo il prossimo download, tutti i problemi segnalati verranno risolti.
======================================================================
```

> [!TIP]
> Dopo un audit che ha rilevato problemi, lancia nuovamente `--mode download` per riscarica automaticamente i soli file problematici.

---

## 5. Struttura dei File Generati

```
Download GDrive Files/
├── gdrive_downloader.py     ← script principale
├── requirements.txt
├── credentials.json         ← credenziali OAuth (NON condividere)
├── token.json               ← token OAuth auto-generato (NON condividere)
├── gdrive_state.db          ← database SQLite dello stato
├── gdrive_downloader.log    ← log completo dell'esecuzione
├── audit_report.txt         ← report di verifica (generato da --mode audit)
└── gdrive_backup/           ← cartella di backup (default)
    ├── Documenti/
    │   ├── Legale/
    │   │   └── Contratto.pdf
    │   └── Relazione_2025.docx
    ├── Foto/
    │   └── Vacanze/
    │       └── img_001.jpg
    └── ...
```

---

## 6. Architettura e Funzionamento

### Database SQLite — Schema

```sql
-- Registro principale di tutti i file
CREATE TABLE files (
    drive_id     TEXT PRIMARY KEY,  -- ID univoco Google Drive
    name         TEXT NOT NULL,     -- Nome file originale
    local_path   TEXT NOT NULL,     -- Percorso locale assoluto
    mime_type    TEXT,              -- MIME type
    md5_drive    TEXT,              -- MD5 da Drive (NULL per Workspace)
    size_bytes   INTEGER,           -- Dimensione in byte
    status       TEXT DEFAULT 'pending',  -- pending | completed | error
    error_count  INTEGER DEFAULT 0, -- Contatore tentativi falliti
    last_updated TEXT               -- Timestamp ISO 8601
);

-- Salvataggio del pageToken per la ripresa della scansione
CREATE TABLE scan_progress (
    id             INTEGER PRIMARY KEY,
    next_page_token TEXT,
    last_scan_ts   TEXT
);
```

### Flusso di download

```
┌─────────────┐    scansiona     ┌──────────┐   download+MD5   ┌───────────┐
│ Google Drive │ ─────────────▶  │  SQLite  │ ───────────────▶ │   Disco   │
│     API      │  pageToken       │    DB    │                  │  Locale   │
└─────────────┘  persistente     └──────────┘                  └───────────┘
                                      │
                         status: pending → completed / error
```

### Exponential Backoff

| Tentativo | Attesa |
|:---------:|:------:|
| 1         | 2s     |
| 2         | 4s     |
| 3         | 8s     |
| 4         | 16s    |
| 5         | 32s    |
| 6         | 64s    |
| 7         | 128s   |

Attivato su errori HTTP: `429` (rate limit), `500`, `502`, `503`, `504`.

### Export file Google Workspace

| Tipo Drive | Formato Esportato |
|---|---|
| Google Docs | `.docx` (Word) |
| Google Sheets | `.xlsx` (Excel) |
| Google Slides | `.pptx` (PowerPoint) |
| Google Drawings | `.pdf` |
| Google Forms | `.pdf` |
| Google Sites | `.pdf` |
| Apps Script | `.json` |

---

## 7. Riferimento Argomenti CLI

| Argomento | Valori | Default | Descrizione |
|---|---|---|---|
| `--mode` | `download` \| `audit` | *(obbligatorio)* | Modalità operativa |
| `--output` | percorso | `gdrive_backup` | Cartella di destinazione del backup |
| `--db` | percorso | `gdrive_state.db` | File database SQLite |
| `--credentials` | percorso | `credentials.json` | Credenziali OAuth |
| `--token` | percorso | `token.json` | Token OAuth persistente |
| `--report` | percorso | `audit_report.txt` | Report audit |
| `--verbose` / `-v` | flag | `False` | Log DEBUG dettagliato |

---

## 8. Risoluzione dei Problemi

### `FileNotFoundError: credentials.json non trovato`

Assicurati di aver scaricato le credenziali da Google Cloud Console e di aver rinominato il file in `credentials.json` nella stessa cartella dello script.

### `HttpError 403: The user does not have sufficient permissions`

- Verifica di aver aggiunto il tuo account come **Utente di test** nella schermata di consenso OAuth.
- Assicurati che lo scope `drive.readonly` sia abilitato.

### `HttpError 403: Daily Limit for Unauthenticated Use Exceeded`

Il progetto ha superato la quota API giornaliera. Attendi 24h o richiedi un aumento di quota nella Google Cloud Console sotto **API e servizi** → **Quote**.

### Il download si interrompe spesso per timeout

Prova a ridurre `CHUNK_SIZE` nello script (es. da 10 MB a 4 MB):

```python
CHUNK_SIZE = 4 * 1024 * 1024
```

### Voglio resettare completamente il database

```bash
# Attenzione: perderai tutto il progresso!
del gdrive_state.db   # Windows
rm gdrive_state.db    # macOS / Linux
```

### Come verificare lo stato del database senza avviare lo script

```bash
python -c "
import sqlite3
conn = sqlite3.connect('gdrive_state.db')
for row in conn.execute('SELECT status, COUNT(*) FROM files GROUP BY status'):
    print(f'{row[0]}: {row[1]}')
conn.close()
"
```

---

> [!NOTE]
> Il log completo di ogni sessione è salvato in `gdrive_downloader.log`. In caso di problemi, allegalo per facilitare la diagnosi.
