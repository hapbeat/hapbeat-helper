## Unity

- **Events**: `hapbeat-helper cues scan --unity <project>` (the project must use Force Text serialization;
  binary assets stop the scan). Entries of Event Maps that no scene, prefab or Animator Controller references
  are listed as unreferenced and left out.
- **Logging fires**:
  - The game has its own fire point (its own cue names): call `HapbeatSceneLog.Cue(frame, cue, hand, gain)`
    there, `capture: "game"`.
  - Hapbeat SDK 0.7 or later: add `HapbeatSceneSdkCapture.cs` from the template (Editor only) and call
    `HapbeatSceneSdkCapture.Start(() => frame)` after `HapbeatSceneLog.Begin`; it logs every Event Map fire
    (Inspector-wired triggers included) as `source: "unity:<Event Map guid>/<entry id>"`, `capture: "sdk"`.
    The SDK reports a loop's start only: write each loop cue's input yourself with
    `HapbeatSceneLog.Level(frame, "<cue>:level[:left|:right]", value)` (the same 0..1 input the binding feeds
    its curve), and a `Level(..., 0)` when it stops.
- **Recording**, two ways:
  1. Edit mode (works in `-batchmode`, proven): open the scene with `EditorSceneManager.OpenScene`, step the
     game yourself (call its update with a fixed dt), render each frame with
     `RenderPipeline.SubmitRenderRequest` into a RenderTexture, `ReadPixels`, `EncodeToPNG`. The game must
     let you step it (an `Initialize` / `Simulate(dt)` style API).
  2. Play mode with `Time.captureFramerate = fps` and a PNG written at the end of each frame. Check first that
     it works in your setup; if it does not run in `-batchmode`, run it from an Editor menu.
- **Silent, nothing sent**: start Unity with `-HapbeatNoNetwork` (or set `HapbeatConfig.noNetwork`), or
  disable the SDK object during the recording; set `AudioListener.volume = 0`. `HapbeatSceneSdkCapture` refuses
  to start unless the SDK is in No Network mode.
- **Run**: close the Editor on the project, then
  `Unity.exe -batchmode -quit -projectPath <project> -executeMethod <Namespace.Class.Method> -HapbeatNoNetwork -logFile <log>`
  (leave out `-nographics` when you render frames). Judge success by your script's last log line.
- **Unity Editor.log**: with `HapbeatSceneLog.Begin(project, fps, null)` the records go to the Unity log with the
  `HAPBEAT_SCENE ` marker; pass that log with `scene session --log <Editor.log or -logFile path>`. Only lines after
  the last `start` are read, so repeated Play runs are fine.
