# Hapbeat Scene: a cue table and a recording for a game (guide for AI agents)

Printed by `hapbeat-helper scene guide` (add `--engine unity|unreal|js|other` for the engine notes).
Specs: hapbeat-contracts `specs/cue-table.md` (the cue table, section 10 for the first draft) and
`specs/scene-recording.md` (the recording log, `hapbeat-scene.json`, the checks).

## What you make

1. **A cue table** (`<name>-cues.json`): one entry per game event (cue) with its haptics and sound.
   You make the first draft from the game's haptic events; a human then tunes it in Hapbeat Studio.
2. **A recording script** in the game that fires every cue once per take, at a fixed time step, silently,
   sending nothing, and writes a log of what fired plus one PNG per frame.
3. **`hapbeat-scene.json`** at the project root (version-controlled): fps, where the frames and the log are,
   where the cue table and WAVs are, and the bindings from SDK entries to cues.

Then `hapbeat-helper scene session --build` checks the recording against the table and writes
`Saved/HapticViewer/`, which Hapbeat Studio's Scene tab opens.

## Rules (always)

- Never play sound on the PC and never send to Hapbeat devices: the SDK runs in No Network mode
  (or is disabled) and the game's audio is muted for every run you start.
- Do not change how the game plays outside the recording path. The recording script drives the game's
  existing code; the only additions are the log calls (and a scripted input source for VR).
- Do not decide what a human must decide. `cues init` reports undecided items (no target position, no WAV,
  renamed duplicates): list them for the human, do not fill them in. Never write ratings or `approved`.
- Do not claim more than the tools did. The first draft copies the SDK entries; sounds stay undecided.

## Steps

### 1. Prepare

- `hapbeat-helper` with the `scene` / `cues` commands (`hapbeat-helper scene --help`).
- ffmpeg with libx264 on PATH (or pass `--ffmpeg PATH` to `scene session --build`).

### 2. Find the game's haptic events

- **Unity**: `hapbeat-helper cues scan --unity <project>` writes `Saved/HapticScan/inventory.json` and
  `wiring.md` (which scene / prefab object and trigger fires each Event Map entry). Read `wiring.md`; it is
  the main input for the recording script.
- **Other engines, or a game with its own cue names in code**: find where the game fires haptics (see the
  engine notes) and either write `inventory.json` yourself (schema: hapbeat-contracts
  `schemas/cue-inventory.schema.json`, rules: cue-table.md section 10) or add empty cues with
  `hapbeat-helper cues init --add a,b,c --loop d --out <table>`.

Names: a cue is `^[a-z][a-z0-9_]*$`; strength or situation variants are `<cue>:<variant>`
(`punch:soft`, `punch:hard`). To rename, set `cue` on the inventory entries before `cues init`. The same
event at the left and the right wrist becomes one cue with `at: "hand"` when both entries give the same
name and the same clip, gain and loop.

### 3. Make the first draft

```
hapbeat-helper cues init --inventory Saved/HapticScan/inventory.json --out <path>/<name>-cues.json
hapbeat-helper cues validate <path>/<name>-cues.json --project-root .
```

- `cues init` copies the WAVs into the clip folder, writes the table, and creates or updates
  `hapbeat-scene.json` (its `bindings`). `--into <table>` adds only what is missing to an existing table and
  never changes decided values. `--dry-run` shows what would change.
- Read its report and keep the undecided items for your final message.

### 4. Write the recording script

- **Fixed time step**: frame i is game time i / fps (30 unless the project says otherwise). Render each frame
  to `<frames>/f<frame>.png` (the folder holds nothing else; `scene build` deletes those PNGs afterwards).
- **Silent, nothing sent**: see the rules above and the engine notes.
- **Log every fire** with the template (`hapbeat-helper scene template <engine> --out <dir>`):
  `Begin` at frame 0, `Cue(frame, "cue[:variant]", hand, gain)` where the game fires (or the SDK fire
  notification, logged as `source`), `Level(frame, "<cue>:level[:left|:right]", value)` for loop inputs,
  `End(frames)` at the end. Write the cue name the game decided, at the frame it decided it.
- **Takes**: one take per cue (and per variant and per hand). `Take(frame, label, expect: [cue])`, perform
  the action, let it settle, `TakeEnd(frame)`. A take that fires anything outside `expect` / `allow`, or
  misses an `expect` cue, fails the check, so the script cannot pass by accident.
- **Loop cues**: start the loop, sweep the input from 0 to its maximum and back to 0 over a few seconds,
  writing `Level` whenever it changes.
- **Repeatable**: fix random seeds; drive VR rigs with scripted poses (no headset), seen from a PC camera at
  the player's eye.
- **On failure**: `Fail(reason)` and stop; never skip a take silently.

### 5. Fill in `hapbeat-scene.json`

`cues init` writes a minimal one. Check `fps`, `frames`, `log` and `lib.paths`; optionally add `lib.title`,
`lib.families` (colour groups), `lib.ticks`, `lib.moment_rules`, `lib.record_command`,
`lib.import_command` (scene-recording.md sections 3.1 and 6). `capture` says who writes the fires:
`game` (the game's code calls `Cue`), `sdk` (the SDK notification), or `both` when they never log the
same fire twice.

### 6. Record and check

Run the recording (engine notes), then from the project root:

```
hapbeat-helper scene session --require-all --build
```

It reads the log, checks it against the cue table (scene-recording.md section 7), writes
`Saved/HapticRecord/session.json` and builds `Saved/HapticViewer/`. Fix the script or the table until it
passes (`--json` gives the errors as data). `hapbeat-helper scene check --session <file>` checks an existing
session.json the same way.

| Error | Usual cause |
|---|---|
| `unknown cue` / `unknown variant` | The game fires a name the table does not have: add it (`cues init --into`) or fix the name |
| `unmapped source` | An SDK entry fired that `bindings` does not know: re-run `cues init --into` with a fresh inventory |
| take: `unexpected` / `missing` | The action fired another cue, or did not fire: fix the script's pose or timing |
| loop cue without a level column | The loop fired but no `Level` was logged for it |
| no `end` | The script stopped early (see the engine log) |

### 7. Hand over

Tell the human: open Hapbeat Studio, Scene tab, open the project folder; tune each cue there; then run the
project's import step. List the undecided items from step 3.

## Asking an AI agent (example prompt for developers)

> Read `hapbeat-helper scene guide --engine unity` and follow it for this project: make a first-draft cue
> table from the Hapbeat Event Maps, write a recording script that fires every cue (one take each, silent,
> No Network), record it, and make `hapbeat-helper scene session --require-all --build` pass. Tell me which
> cues are left for me to decide.
