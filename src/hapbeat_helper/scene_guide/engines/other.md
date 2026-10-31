## Other engines (Godot, Python, custom)

- **Events**: find where the game calls the Hapbeat SDK (`play`, `stream`), write `inventory.json` by hand or
  use `cues init --add`.
- **Logging fires**: write the JSON Lines records of scene-recording.md section 5 to
  `Saved/HapticRecord/events.jsonl` (one JSON object per line), or to the engine log with the
  `HAPBEAT_SCENE ` marker. Wrap the SDK's play call so the recording run logs instead of sending.
- **Recording**: run at a fixed time step with audio off and render one PNG per frame, or record a
  constant-fps video (`video` in `hapbeat-scene.json`).
