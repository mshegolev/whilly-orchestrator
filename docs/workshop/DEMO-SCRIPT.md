---
title: Whilly 3-minute demo screencast — shot list and cues
type: script
created: 2026-04-22
status: v1
audience: bilingual (EN summary above each section, RU detail below)
related: [INDEX.md, TUTORIAL.md, PRD-Whilly.md, ROADMAP.md]
issue: https://github.com/mshegolev/whilly-orchestrator/issues/157
roadmap_item: DR-802 (Capture demo screencast ~3 min)
---

# Whilly 3-Minute Demo — Screencast Plan

> **EN summary:** Shot-by-shot recording plan for the ~3 minute Whilly demo screencast (NFR-2 / DR-802). Output artifact is `docs/workshop/demo.gif` embedded in `README.md`. This file is the **script** — the recording itself is produced by running these cues against a clean repo checkout.
>
> **RU:** Покадровый план записи 3-минутного демо-скринкаста Whilly. Итоговый артефакт — `docs/workshop/demo.gif` во врезке `README.md`. Этот файл — **сценарий**: запись получается прогоном этих кадров на чистом чекауте репозитория.

---

## 🎯 Goal / Цель

> **EN:** Show a brand-new viewer in under three minutes that Whilly takes a real GitHub Issue, turns it into a task, runs an agent loop, and produces a commit + auto-closed Issue — without narrating the whole architecture.
>
> **RU:** За три минуты показать зрителю, что Whilly берёт реальный GitHub Issue, превращает в задачу, гоняет агентный цикл и отдаёт коммит + автоматически закрытый Issue. Без долгих объяснений архитектуры.

**Elevator line / Слоган:** *"Ralph picks a task, does it, shouts 'I'm helping!', repeats. Whilly is Ralph's smarter brother — with TRIZ, a Decision Gate, and a PRD wizard that understands the problem first."*

---

## 📋 Pre-recording checklist / Подготовка перед записью

> **EN:** Reset the environment so every take looks identical. Skipping any step risks leaking personal paths, stale tasks, or dashboard state into the recording.

```bash
# 1. Clean checkout on main
cd whilly-orchestrator
git checkout main && git pull --ff-only

# 2. Fresh virtualenv / clean install
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' --quiet

# 3. Auth is live, token scopes visible
unset GITHUB_TOKEN                      # avoid the deprecated-scope warning
gh auth status                          # must show "Logged in to github.com"
claude --version                        # Claude CLI reachable on PATH
echo "$ANTHROPIC_API_KEY" | head -c 8   # non-empty, truncated for screen safety

# 4. Remove prior run artifacts that would clutter the recording
rm -f tasks-from-github.json .whilly_state.json
rm -rf whilly_logs/ .whilly_worktrees/ .whilly_workspaces/

# 5. Terminal prep
#   - Font size: 18–20pt for GIF legibility
#   - Window size: 110 cols × 32 rows (Rich dashboard renders cleanly here)
#   - Prompt: short PS1 ("whilly $ "), no git status segments flapping
#   - Colors: dark background, light text; disable shell autosuggest plugins
```

> **RU:** Обнули окружение так, чтобы дубли смотрелись одинаково. Пропуск любого шага — риск утечки персональных путей, залипших задач и старого состояния дашборда в запись. Шрифт 18–20pt, окно 110×32 — Rich TUI на этих размерах читается без артефактов.

---

## ⏱ Timing map / Тайминг (0:00 → 3:00)

| Time / Время | Scene / Сцена | Visual / Картинка | Why this slot / Почему столько |
|---|---|---|---|
| `0:00 – 0:15` | Title card & hook | Static slide + logo | Hook — Ralph / Whilly metaphor lands in <15s |
| `0:15 – 0:45` | GitHub Issues source | `gh issue list` output | Proof it's a real repo, not staged mocks |
| `0:45 – 1:15` | Convert to tasks | `--from-github` + `jq` | Show the adapter; emphasise priority + key_files |
| `1:15 – 2:15` | Whilly loop running | Rich dashboard (TUI) | Main money shot — agent thinking, tasks flipping `done` |
| `2:15 – 2:45` | Outcome: PR + closed Issue | `gh pr view`, `gh issue view` | Proof the loop closes end-to-end |
| `2:45 – 3:00` | Call to action | Static slide: links | Tutorial + repo star + `whilly --help` |

**Total budget:** 180 seconds. Trim narration — never trim the dashboard scene.

---

## 🎬 Scene-by-scene cues / Поэпизодный план

### Scene 1 — Title card (0:00 → 0:15)

**Visual:** Static title card with the Whilly logo, slogan, and `whilly --version` output underneath.

**Narration (EN, 2 sentences max):**
> "Ralph Wiggum picks a task, does it, shouts 'I'm helping!', repeats. Meet his smarter brother — Whilly."

**Narration (RU, дубль):**
> "Ralph Wiggum берёт задачу, делает, кричит «I'm helping!», повторяет. Знакомьтесь — его умный брат, Whilly."

**Cue:** Fade card → cut to terminal at `0:15`. No commands typed on screen yet.

---

### Scene 2 — "Real Issues in a real repo" (0:15 → 0:45)

**Visual:** Split is optional; full terminal is fine. Typing rate ~6 cps (realistic but not painful).

**Keystrokes:**
```bash
gh issue list --label workshop --label whilly:ready
```

**Expected on screen:** 5–7 open workshop issues (use the curated `whilly:ready` label so the list is stable between takes).

**Narration (EN):**
> "Here's our sandbox repo. These are real open issues — not fixtures. Each one has a label `whilly:ready` saying the orchestrator may pick it up."

**Narration (RU):**
> "Это sandbox-репозиторий. Реальные открытые issues, не фикстуры. Метка `whilly:ready` разрешает оркестратору брать их в работу."

**Cue:** Leave output on screen for ~2 seconds after typing finishes before moving to Scene 3.

---

### Scene 3 — "Issues become structured tasks" (0:45 → 1:15)

**Visual:** Terminal.

**Keystrokes:**
```bash
python3 -m whilly --from-github workshop,whilly:ready
jq '.tasks[0] | {id, priority, key_files, github_issue}' tasks-from-github.json
```

**Expected on screen:** First the converter progress lines, then a pretty-printed JSON object showing `id`, `priority`, `key_files`, `github_issue` — these four fields tell the whole story of how Whilly reasons about work.

**Narration (EN):**
> "One command extracts the issues, infers priority, pulls the files the issue touches, and keeps the link back to GitHub. That last field is what lets Whilly close the issue when it's done."

**Narration (RU):**
> "Одна команда — и issues вынуты, приоритет выведен, затронутые файлы размечены, ссылка на GitHub сохранена. Именно эта ссылка позволит Whilly закрыть issue после выполнения."

**Cue:** Keep Scene 3 strictly ≤ 30s — the conversion is interesting but not the climax.

---

### Scene 4 — "Agent loop actually running" (1:15 → 2:15) — **money shot**

**Visual:** Rich dashboard (TUI) in foreground. This is the scene viewers screenshot.

**Keystrokes:**
```bash
WHILLY_CLOSE_EXTERNAL_TASKS=true \
WHILLY_GITHUB_AUTO_CLOSE=true \
WHILLY_GITHUB_ADD_COMMENTS=true \
python3 -m whilly tasks-from-github.json
```

**What to show during this 60s window:**

1. Startup banner (~5s — sleep in `_show_startup_banner`, do **not** cut around it, it's canon).
2. Dashboard renders: task list on the left, live log / cost / iteration counter on the right.
3. At least one task flips `pending → in_progress → done` on camera.
4. `whilly_logs/whilly_events.jsonl` can be briefly tailed in a split pane if your recorder supports it — otherwise skip, dashboard is enough.

**Narration (EN, spread over the 60 seconds, not a wall of text):**
> *(at 1:15)* "Now the loop. Whilly picks the highest-priority ready task…"
> *(at 1:35)* "…hands it to a Claude agent in an isolated tmux session…"
> *(at 1:55)* "…watches for the `COMPLETE` promise, and moves on."

**Narration (RU):**
> *(1:15)* "Цикл поехал. Whilly берёт задачу с наивысшим приоритетом…"
> *(1:35)* "…отдаёт Claude-агенту в изолированную tmux-сессию…"
> *(1:55)* "…ждёт обещания `COMPLETE` и переходит к следующей."

**Cue:** If the first task takes longer than 45s in real time, **speed up playback 2×** in post — never re-record to a different task set. The recorder decides pacing; the script decides what's captured.

---

### Scene 5 — "Loop closes the loop" (2:15 → 2:45)

**Visual:** Terminal (exit dashboard with `q` or let it finish naturally).

**Keystrokes:**
```bash
git log --oneline -3
gh pr list --state open --limit 3
gh issue list --label whilly:ready --state closed --limit 3
```

**Expected on screen:**
- Git log shows 1–2 fresh commits authored by the agent.
- A PR is open against the sandbox repo.
- At least one issue has moved to `closed` with reason `completed`.

**Narration (EN):**
> "Commit landed, PR opened, issue closed — and a comment on that issue links back to the commit. End to end, no hand-holding."

**Narration (RU):**
> "Коммит есть, PR открыт, issue закрыт, в комментарии к issue ссылка на коммит. От начала до конца — без ручных шагов."

---

### Scene 6 — Call to action (2:45 → 3:00)

**Visual:** Static slide with three links stacked:
- `github.com/mshegolev/whilly-orchestrator` (+ ⭐)
- `docs/workshop/TUTORIAL.md` — 90-minute hands-on
- `pip install whilly-orchestrator`

**Narration (EN):**
> "Full 90-minute walkthrough in `TUTORIAL.md`. Install with pip. Star the repo if Whilly was helpful — pun intended."

**Narration (RU):**
> "Полный 90-минутный туториал — в `TUTORIAL.md`. Ставится через pip. Лайкните репо, если Whilly «I'm helping!» зашёл."

**Cue:** End slide stays on screen for the last 3 seconds with no narration — gives the GIF a natural loop point.

---

## 🎙 Recording tips / Советы по записи

> **EN:**
> 1. **Type commands, don't paste.** Viewers trust commands they see typed; autopaste looks like a demo video, not a screencast.
> 2. **Record in 1080p or better, export GIF at 800×500.** Rich TUI degrades fast below that.
> 3. **Never record secrets.** `ANTHROPIC_API_KEY` must be set in shell rc, **not** typed live. Redact any full token that slips into the terminal history.
> 4. **One continuous take if possible.** If editing, keep cuts at scene boundaries only — mid-scene cuts break the "it just works" feel.
> 5. **Disable OS notifications, Slack, email.** A Slack popup in frame forces a re-take.
> 6. **Mic tip:** Record voiceover separately against the muted screencast. Timing tighter, retake cheaper.

> **RU:**
> 1. Команды **печатать**, не вставлять. Зрители верят тому, что видят как набор.
> 2. Записывать в 1080p+, GIF выгружать 800×500 — Rich TUI быстро разваливается на меньших размерах.
> 3. **Никаких секретов на экране.** `ANTHROPIC_API_KEY` — только из rc-файла. Случайно попавший токен — заредактировать.
> 4. Оптимально — один дубль. Если резать, то только по границам сцен.
> 5. Отключить уведомления ОС, Slack, почту. Всплывашка = пересъёмка.
> 6. Голос писать отдельно поверх немого скринкаста — так проще монтировать.

---

## 🎞 Post-production / Пост-обработка

> **EN:** The ROADMAP acceptance (DR-802) asks for `docs/workshop/demo.gif` embedded in `README.md`. Recommended pipeline:

```bash
# Record with asciinema (terminal-only scenes)
asciinema rec -t "Whilly 3-min demo" demo.cast

# Convert to GIF
agg --theme monokai --font-size 18 demo.cast docs/workshop/demo.gif

# Or: record screen (QuickTime / OBS), then convert
ffmpeg -i demo.mov -vf "fps=10,scale=800:-1:flags=lanczos" -loop 0 docs/workshop/demo.gif
```

**Checklist before publishing:**
- [ ] Runtime is ≥ 2:45 and ≤ 3:15 (hard bounds — longer hurts README load, shorter cuts the money shot).
- [ ] File size ≤ 8 MB (GitHub README inlines comfortably below 10 MB, 8 MB leaves margin).
- [ ] First frame is the title card (GitHub's auto-thumbnail uses frame 1).
- [ ] Last 3 seconds are the CTA slide (natural loop point).
- [ ] No token, email, or personal path visible on any frame.
- [ ] `README.md` embeds with: `![Whilly demo](docs/workshop/demo.gif)`.
- [ ] `CHANGELOG.md` has a line referencing the asset under the next release.

> **RU:** Целевой артефакт DR-802 — `docs/workshop/demo.gif` в `README.md`. Пайплайн: `asciinema rec → agg`, либо QuickTime/OBS → `ffmpeg`. Чек-лист перед публикацией: длительность 2:45–3:15, размер ≤ 8 MB, первый кадр — титры, последние 3 секунды — CTA-слайд, никаких токенов/путей на экране, embed в README одной строкой, запись в CHANGELOG.

---

## 🧪 Dry-run before recording / Прогон перед записью

> **EN:** Run the whole sequence on throwaway data to catch surprises. If any step fails, fix it in the repo first — don't work around it in the edit.

```bash
./workshop_demo.sh                       # basic path (Scene 2 → Scene 4)
./workshop_demo_with_integrations.sh     # with auto-close (Scene 5 proof)
```

Both live at the repo root and are intentionally kept in sync with this script. If you change scene keystrokes here, update the shell scripts too so the dry-run matches.

> **RU:** Сначала прогнать `workshop_demo.sh` (базовый путь) и `workshop_demo_with_integrations.sh` (с автозакрытием issue). Если что-то падает — чинить в репозитории, не «заклеивать» монтажом. Эти скрипты держим в одном ритме со сценарием; при изменении команд в плане — правим и их.

---

## 📎 References / Ссылки

- [`WORKSHOP.md`](../../WORKSHOP.md) — narrative overview of the self-writing orchestrator demo.
- [`workshop_demo.sh`](../../workshop_demo.sh), [`workshop_demo_with_integrations.sh`](../../workshop_demo_with_integrations.sh) — executable mirrors of Scenes 2–5.
- [`TUTORIAL.md`](TUTORIAL.md) — the 90-minute long-form hands-on the demo points viewers to.
- [`ROADMAP.md`](ROADMAP.md) — DR-802 (screencast capture) consumes this plan.
- [`PRD-Whilly.md`](PRD-Whilly.md) — NFR-2 (cost predictability / Decision Gate), which this demo implicitly showcases via the `--from-github` path.
- GitHub Issue tracking this doc: [#157](https://github.com/mshegolev/whilly-orchestrator/issues/157).
