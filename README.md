# reMarkable Highlights to Anki

Repository: https://github.com/tdm5c/remarkable-highlights-to-anki

Deterministic local pipeline for exporting reMarkable PDF/EPUB highlights to
Markdown, JSON, and Anki.

The extraction is local and deterministic: it does not use OCR and does not use
AI/API tokens. The script reads the reMarkable over SSH and does not modify files
on the device.

## Features

- Lists recent reMarkable PDF/EPUB documents.
- Exports one Anki note per highlight.
- Uses stable IDs so reruns update existing notes instead of creating duplicates.
- Keeps one Anki deck per reMarkable document by default.
- Supports an explicit shared deck for multi-document exports.
- Adds one previous sentence and one next sentence as back-card context when
  source text is available, with the cited span highlighted as
  `<mark><strong>...</strong></mark>` on the back.
- Removes stale notes for the same reMarkable document UUID when highlights
  change.
- Starts Anki Desktop automatically when AnkiConnect is not reachable.
- Runs AnkiWeb sync after local Anki updates unless disabled.
- Includes a local web UI.

## Requirements

- Windows with Python 3.10 or newer.
- OpenSSH client available in Windows.
- A reMarkable tablet with SSH access enabled.
- Anki Desktop.
- AnkiConnect add-on installed in Anki.

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env.local` and fill in local values:

```powershell
Copy-Item .env.example .env.local
```

Typical `.env.local` values:

```text
RM_REMARKABLE_MAC=00:00:00:00:00:00
RM_SSH_PASSWORD=
ANKI_EXE=C:\Program Files\Anki\Anki.exe
```

Do not commit `.env.local`. It is ignored by git.

The script prefers `RM_REMARKABLE_MAC` because the reMarkable IP can change with
DHCP. `RM_REMARKABLE_IP` is only an optional local cache or override.

## Network Addresses

For the reMarkable, the script can rediscover the current IP address when
`RM_REMARKABLE_MAC` is configured. It checks the cached IP, local ARP data, and
the local subnet, then updates `RM_REMARKABLE_IP` in `.env.local` when it finds
the tablet.

For AnkiConnect, the default URL is `http://127.0.0.1:8765`, which is correct
when Anki runs on the same computer as the script.

For the web UI, `http://127.0.0.1:8787` works only on the computer running the
server. Phones or tablets on the same Wi-Fi must use that computer's local
network name or IP address, for example `http://your-computer-name.local:8787`.

## CLI Usage

Run the interactive CLI:

```powershell
python .\scripts\remarkable_anki.py
```

List recent PDF/EPUB documents:

```powershell
python .\scripts\remarkable_anki.py --list
```

Export one document by number, UUID prefix, or visible name:

```powershell
python .\scripts\remarkable_anki.py --document 7
python .\scripts\remarkable_anki.py --document my-book-title
python .\scripts\remarkable_anki.py --document 344dac58
```

Export all PDF/EPUB documents that contain highlights:

```powershell
python .\scripts\remarkable_anki.py --all
```

Write Markdown/JSON only without syncing to Anki:

```powershell
python .\scripts\remarkable_anki.py --document my-book-title --no-anki
```

Disable AnkiWeb sync for one run:

```powershell
python .\scripts\remarkable_anki.py --document my-book-title --no-sync-ankiweb
```

## Web UI

Start the local web UI:

```powershell
.\launch_remarkable_web.cmd
```

The launcher starts the server on port `8787` and opens:

```text
http://127.0.0.1:8787
```

To access it from another device on the same Wi-Fi, use the computer's local
network name or IP address, for example:

```text
http://your-computer-name.local:8787
```

The web UI can search, sort, select multiple documents, export in displayed
order, and optionally use one shared Anki deck for multi-document exports.

## Generated Files

Generated local data is ignored by git:

- `data/`
- `exports/`
- `tmp/`
- `.env.local`

`exports/remarkable` contains generated Markdown/JSON exports. The web launcher
cleans those export files after successful Anki sync by default. It does not
delete `data/remarkable/cache`.

## Security Notes

- Do not store reMarkable passwords in source files, README files, or notes.
- Prefer `RM_SSH_PASSWORD` in `.env.local` or a temporary PowerShell session.
- Use SSH only to read/copy data from the reMarkable.
- Do not publish copied reMarkable documents, cache directories, or Anki exports
  unless you intentionally want to share that content.
