## Unreal Engine

- **Events**: Event Maps are binary `.uasset` files; `cues scan` cannot read them. Search the project for
  where haptics fire: `PlayEntry`, the "Play Event (Hapbeat)" Blueprint node (`UHapbeatBlueprintLibrary`),
  `UHapbeatTriggerComponent`, `UHapbeatCollisionTriggerComponent`, `UHapbeatSequenceComponent`,
  `UHapbeatTickEmitterComponent`, `UHapbeatAnimNotify`, and the game's own cue names (arrays of names, enums,
  log lines). Write `inventory.json` by hand, or `cues init --add` for a game with its own cue names.
- **Logging fires**: `HapbeatSceneLog.h` from `hapbeat-helper scene template unreal` writes each record with
  `UE_LOG(LogTemp, Display, TEXT("HAPBEAT_SCENE %s"), ...)`. Call it where the game fires a cue. The engine
  log is `<project>/Saved/Logs/<Project>.log`; pass it with `scene session --log`.
- **Recording**: in the Editor build, `UnrealEditor-Cmd <uproject> <map> -game -UseFixedTimeStep -FPS=30
  -NoSound -HapbeatNoNetwork -unattended -windowed -ResX=1280 -ResY=720`, with a recording actor or
  subsystem that drives the scripted takes and requests a screenshot each frame (`FScreenshotRequest`)
  into the frames folder as `f<frame>.png`.
- **Silent, nothing sent**: `-NoSound -HapbeatNoNetwork`. `-HapbeatNoNetwork` works only in Editor builds,
  so do not record with a packaged game.
