# Whilly Status Reports

Periodic, human-readable snapshots of where the project stands. Stable URL
for stakeholders, no need to scrape `git log` or `CHANGELOG`.

## Layout

| File | Purpose |
|---|---|
| `STATUS-YYYY-MM-DD.md` | Russian status post (canonical — primary audience) |
| `STATUS-YYYY-MM-DD-EN.md` | English mirror (PyPI / external readers) |
| `PRESENTATION.md` | Slide deck (Markdown — imports cleanly into Gamma / Marp / Slidev) |
| `whilly-architecture.mmd` | Mermaid source for the architecture diagram |
| `whilly-architecture.png` | Rendered PNG (commit if regenerated) |
| `whilly-archimate.xml` | ArchiMate 3.1 Open Exchange model — imports into [Archi](https://www.archimatetool.com/) |

## Render the architecture diagram

### Option A — local Mermaid CLI (preferred for CI)

```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i docs/status/whilly-architecture.mmd \
     -o docs/status/whilly-architecture.png \
     -t default -b transparent --width 1600
```

### Option B — online (zero install)

1. Open https://mermaid.live
2. Paste the contents of `whilly-architecture.mmd`
3. `Actions → PNG` (or SVG). Save into this directory.

## Open the ArchiMate model

1. Install [Archi](https://www.archimatetool.com/) (free, cross-platform).
2. `File → Import → Open Exchange XML File…`
3. Pick `docs/status/whilly-archimate.xml`.
4. Three pre-defined views appear in the model tree:
   - **Whilly — Application Layer** — components + services + data
   - **Whilly — Technology Layer** — external CLIs, runtime, OS
   - **Whilly — Pipeline (vNext)** — Issue → PR business flow

Drag elements onto the canvases to populate them — Open Exchange ships
elements + relationships but not coordinates, so the first time you'll
spend ~5 minutes laying things out. After that the layout is saved in
the `.archimate` workspace file.

## Build a presentation in Gamma

1. https://gamma.app → `Create new` → `Import` → `Paste in text`.
2. Paste the entire contents of `PRESENTATION.md`.
3. Pick a theme — Gamma keeps `---` slide breaks and `# / ##` titles.

## NotebookLM (audio overview / Q&A)

The status report is a clean source for [NotebookLM](https://notebooklm.google):

1. Create a new notebook.
2. Add `STATUS-YYYY-MM-DD.md` (and optionally `README.md`, `CHANGELOG.md`).
3. `Generate Audio Overview` for a 6–10-min two-host podcast version, or
   ask Gemini questions ("what changed since last release?",
   "which PRs introduced the Jira lifecycle?") — every answer is grounded
   in the uploaded sources with citations.

NotebookLM does **not** generate slide decks — for that, use Gamma.

## Cadence

- Every Monday: new `STATUS-<that Monday's date>.md`.
- BREAKING releases: bullet line in the next status under "What's shipped".
- Don't edit old status files — append a note + link to the new one.
