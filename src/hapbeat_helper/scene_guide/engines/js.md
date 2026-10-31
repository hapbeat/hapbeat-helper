## JavaScript / web (hapbeat-js-sdk)

- **Events**: the game's `eventmap.json` (if any) and its `play(` / `streamClip(` calls. Write
  `inventory.json` by hand, or `cues init --add` for a game with its own cue names.
- **Logging fires**: `hapbeat-helper scene template js` gives `hapbeat-scene-log.mjs`: a recording
  Transport that sends nothing and logs each `play()` as a `cue` record, plus the log writer. Give the SDK
  this Transport for the recording run instead of the Node or browser transport.
- **Recording**: advance the game with a fixed dt yourself and capture each frame (e.g. a canvas
  `toDataURL` per frame from a headless browser), or record a constant-fps video and set `video` instead of
  `frames` in `hapbeat-scene.json`. Keep the page muted.
