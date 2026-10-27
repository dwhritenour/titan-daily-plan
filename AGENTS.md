# Titan Daily Plan v1.0 — agent rules (any editor)

These rules bind every coding agent that works in this repository, whichever editor
hosts it (Cursor, VS Code with Copilot / Cline / Claude Code / Codex, or a terminal
agent). This file is the single source; `.cursor/rules/anvil.mdc`, `CLAUDE.md` and
`.github/copilot-instructions.md` only point here. Edit the rules HERE.

This repository is an **Anvil.works** application that is two-way synced with the
Anvil Cloud Editor through GitHub. Pushing to `origin master` deploys to the
Anvil *editing* branch. The `published` branch is what shop-floor users run.

## 1. Git workflow (non-negotiable)

- Work directly on `master`. Do not create feature branches unless explicitly asked.
- **Before editing anything, run `git pull origin master`.** Dave makes visual
  changes in the Anvil designer; those arrive as commits named "Edited <form>".
  Editing a stale file silently reverts his designer work.
- After completing the requested change: `git add -A`, commit with a clear,
  specific message (what changed and why, one line), then `git push origin master`.
  `SPEC.md` is gitignored and must never be committed; do not force-add it.
- **Never modify, push to, or reset the `published` branch** unless the request
  explicitly says to release. A release is:
  `git push origin master:published --force-with-lease` (never bare `--force`).
- Before any multi-file refactor, create and push a tag:
  `backup-YYYY-MM-DD-<short-reason>`.
- To undo the last pushed commit use `git revert HEAD --no-edit` then push.
  Do not rewrite history on `master`.
- If `git pull` reports conflicts in a `form_template.yaml`, STOP and report.
  Do not resolve designer-file conflicts by guessing.
- **Never discard the working tree.** No `git reset --hard`, `git checkout -- .`,
  `git restore`, `git stash` or `git clean` unless the request says so in those
  words. Cowork's finished edits live UNCOMMITTED in the working tree until you
  commit them, and `SPEC.md` is gitignored, so `git clean -x` deletes the very
  instructions you are following. (2026-10-19: a "recovery" clean wiped three
  edited files and the spec.) If the tree looks wrong, STOP, paste `git status`,
  and report.
- If a command hangs (most often `git pull`/`push` waiting on a GitHub sign-in
  that only a human can complete), stop and report it. Do not retry in a loop
  and do not "fix" it by resetting anything.

## 2. Repository layout and what you may touch

- `client_code/<form>/__init__.py` — form logic (Python, runs in browser via Skulpt).
  Forms: `ship_plan` (the board; row templates `ShipPlanLineRow`,
  `ShipPlanCandidateRow`, `ShipPlanParamsPanel` under it), `retro`, `retro_review`
  (`RetroReviewRow`, `RuleCandidateRow`), `orders_shipped`, `wo_completed`, `gates`,
  `multiday_order` (the Multi-day Order page, route `#md=<order>`).
- `client_code/<form>/form_template.html` — EVERY form in this app is a custom-HTML
  form (no designer YAML). Safe to edit when the task says so; keep the Anvil
  slot/component markup intact.
- Designer components referenced from code are hand-added in the Anvil designer and
  `hasattr(self, ...)`-guarded. Keep the guards; keep the exact component names.
- `server_code/*.py` — server modules (Python 3, pymssql to the Nexus SQL Server):
  `daily_ship_plan.py` (board, candidates, add/remove/priority/quantity, the agent
  engine `_agent_propose_impl`, publish, dry run / compare), `daily_plan_retro.py`
  (the 06:30 retro and the review page), `daily_plan_ledger.py`,
  `daily_plan_nuggets.py`, `daily_plan_shipped.py`, `daily_plan_completed.py`,
  `db_config.py`. Freely editable. This is where SQL lives.
- **Callable names are an external contract.** The `titan-ai-service` worker (a
  separate repo) calls `agent_propose_task`, the retro and the rush poll by name
  over HTTP at 06:30 / 06:40; SMS links and the kiosk use form routes (`#gates`,
  `#retrev`). Never rename a `@anvil.server.callable` or a form route.
- Version strings (`_AGENT_VERSION`, `_RETRO_VERSION`) are bumped by the SPEC, not
  by the agent.
- `theme/assets/theme.css`, `theme/parameters.yaml` — styling. Editable; see §4.
- `anvil.yaml` — app config, dependencies, server config. Do not edit unless asked.
- `.anvil_editor.yaml`, root `__init__.py` — Anvil boilerplate. Never touch
  (a SPEC that adds a form may include its one `unique_ids.forms` line).
- Line endings: files are CRLF on disk, LF in git (`core.autocrlf=true`). Do not
  convert line endings and do not commit whole-file reformatting.

## 3. Anvil client-code constraints (these have each cost us a round-trip)

- Anvil **Labels do not emit a `click` event.** Use a Link or Button for anything
  clickable; `set_event_handler('click', ...)` on a Label silently does nothing.
- Label, CheckBox and FlowPanel have **no `width` property.** Column geometry
  comes from DataGrid columns, never from component widths or CSS roles.
- `role` only accepts names defined in `theme/parameters.yaml`. Do not invent roles.
- `datetime.strptime` suspends in client code — never call it inside a `sorted()`
  key function or any non-suspendable context.
- Server calls: `anvil.server.call('fn', ...)`; server functions are decorated
  `@anvil.server.callable`. Keep heavy work (SQL, PDF) on the server.
- Anvil servers run UTC. Any user-facing timestamp must go through the existing
  `_now_eastern()` helper pattern.
- Search-box style TextBoxes use `placeholder`, not `text`, for the hint.
- A client form (package under `client_code/`) and a server module must never share
  a name: on the server the form package shadows the module (`Cannot import Form
  templates on the server`). Suffix the form (`routing_desk_form`), never the module.
- FlowPanel ignores `container:width` / per-item widths. Fixed column geometry in a
  custom-HTML row comes from a theme role whose CSS makes the panel a grid (see
  `rd-grid` in theme.css), or from a DataGrid.
- Top-level children of a page's root ColumnPanel need `container:full_width_row="true"`
  or Anvil caps them at 970px and centres them.

## 4. House UI pattern (locked — do not deviate)

- This app predates the DataGrid rule: its forms are custom-HTML forms with
  RepeatingPanels and hand-coded row templates. Keep that pattern here; do not
  rebuild a form as a designer DataGrid unless a SPEC says so.
- Word + colour, never colour alone: status chips carry the word (`PRIORITY`,
  `NEXT`, `IF TIME`, `PROPOSED`); glyphs (`⚡`, `⚖`, `⛔`) sit in front of the word.
- Colours are set in code or theme.css with exact hex values from the existing
  palettes in `ShipPlanLineRow` (`STATUS_COLORS`, `TIER_COLORS`). Reuse them.
- No runtime component construction beyond what the form already does (dialog
  panels built in `_add_due_in`, `_priority_dialog` and `_add_order` are the
  pattern for new dialogs: `ColumnPanel` + `Label` + `TextBox`, shown with `alert`).
- Code-referenced component names are load-bearing; keep exact names. Do not
  rename components, forms, or server functions unless the task says so.

## 5. Data and SQL

- The database is **Nexus** (SQL Server, T-SQL). Server modules query it with
  parameterized pymssql calls. Never build SQL by string formatting user input.
- Schema changes (DDL) are **not** done from this repo. If a change needs a new
  column/table/proc, stop and report: "needs migration" with the DDL you would
  propose. Dave applies migrations through the numbered `migrations/` process.
- `work_orders` table: EasyMorph reloads the e21-mirrored columns every 15 minutes;
  only app-owned columns (`plan_date`, `plan_tier`, `plan_position`, `plan_stamped_at`)
  are written from this app, and only through `sp_stamp_plan_work_orders`.
- The plan's own tables are `pc_*` (`pc_ship_plan`, `pc_ship_plan_line`,
  `pc_plan_line_events`, `pc_plan_line_hold`, `pc_plan_variance`, `pc_agent_proposal*`,
  `pc_plan_parameters`, `pc_flag_definitions`) and the views `vw_pc_*`. The flag
  view `vw_pc_order_flags` is patched only by numbered migrations.
- `pc_plan_line_events.event_type` is CHECK-constrained; a new event type needs a
  migration. Use an existing type (e.g. `Recalc` with a note) when the SPEC says so.
- Board notes (`priority_note`, 200 chars) that start with `SPLIT n of N` or
  `SHELF DECISION:` are machine-read (drift rule, publish cleanup, filters). Keep
  those prefixes exactly.

## 6. How to work a request

1. `git pull origin master`.
2. If a `SPEC.md` is referenced, read it fully before touching code. Implement
   it verbatim — no renaming, reformatting, or "improvements" beyond the spec.
   Read exactly the file the request names (`SPEC.md`, `SPEC_pause.md`, ...); if
   that file does not exist, STOP and say so — do not open a different spec.
   If a reference in the spec does not resolve (missing component, function,
   column), **report it; do not work around it.**
3. Make the change. Run `python -m py_compile` on every edited `.py` file.
   On Windows, if `python` is the Microsoft Store stub, use `py -3 -m py_compile`
   or the full interpreter path; never skip the check and "verify by reading".
   When a SPEC asks for a CRLF check and `file` is not on the PATH, use
   PowerShell: `(Get-Content -Raw <path>) -match "\r\n"` must print `True`
   (Git for Windows also ships `file.exe` under its `usr\bin`).
4. `git add -A`, commit, `git push origin master`.
5. Reply with: files changed, the commit hash, anything unresolved, and the
   manual smoke test Dave should run in the Anvil editor (green Run button).
