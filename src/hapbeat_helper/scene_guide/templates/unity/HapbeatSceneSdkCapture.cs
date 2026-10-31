// HapbeatSceneSdkCapture: logs every Event Map fire of the Hapbeat SDK (HapbeatManager.Fired, Hapbeat Unity SDK
// 0.7 or later) as a recording log record {"t":"cue","source":"unity:<Event Map guid>/<entry id>"} through
// HapbeatSceneLog, so Triggers wired only in the Inspector are recorded without code changes. Set "capture": "sdk"
// (or "both") in hapbeat-scene.json; its bindings (written by `hapbeat-helper cues init`) map each source to a cue.
// The map's asset guid is part of the source because a duplicated Event Map keeps its entries' ids.
//
// Copy it next to HapbeatSceneLog.cs (Unity Editor only: the guid comes from the AssetDatabase). Call Start after
// HapbeatSceneLog.Begin and Stop before HapbeatSceneLog.End. It refuses to record unless the SDK is in No Network
// mode (-HapbeatNoNetwork or HapbeatConfig.noNetwork), so a recording never reaches Hapbeat devices.
// Not logged here: direct HapbeatManager.Play / StreamAudioClip calls (no entry; log them with HapbeatSceneLog.Cue
// where the game makes them) and the input of loop cues (the SDK reports only their start; write it with
// HapbeatSceneLog.Level from your script).
#if UNITY_EDITOR
using System;
using UnityEditor;
using UnityEngine;

namespace Hapbeat.Scene
{
    public static class HapbeatSceneSdkCapture
    {
        private static Func<int> _frame;

        /// <summary><paramref name="frame"/>: the recording frame now (the number of the PNG written for it).</summary>
        public static void Start(Func<int> frame)
        {
            var manager = HapbeatManager.Instance;
            if (manager != null && !manager.NoNetwork)
                throw new InvalidOperationException(
                    "HapbeatSceneSdkCapture: the Hapbeat SDK is not in No Network mode (run with -HapbeatNoNetwork or set HapbeatConfig.noNetwork)");
            _frame = frame ?? throw new ArgumentNullException(nameof(frame));
            HapbeatManager.Fired -= OnFired;
            HapbeatManager.Fired += OnFired;
        }

        public static void Stop()
        {
            HapbeatManager.Fired -= OnFired;
            _frame = null;
        }

        private static void OnFired(HapbeatFired fired)
        {
            if (_frame == null || string.IsNullOrEmpty(fired.entryId)) return;
            var manager = HapbeatManager.Instance;
            if (manager != null && !manager.NoNetwork)
            {
                HapbeatSceneLog.Fail("the Hapbeat SDK left No Network mode during the recording", _frame());
                Stop();
                return;
            }
            if (fired.map == null || !AssetDatabase.TryGetGUIDAndLocalFileIdentifier(fired.map, out string mapGuid, out long _))
            {
                HapbeatSceneLog.Fail("a fire of entry " + fired.entryId + " has no Event Map asset (cannot name its source)", _frame());
                return;
            }
            // gain: the multiplier the game applied on top of the entry (1 is left out).
            float? gain = Mathf.Approximately(fired.gain, 1f) ? (float?)null : fired.gain;
            HapbeatSceneLog.Source(_frame(), "unity:" + mapGuid + "/" + fired.entryId, gain: gain,
                note: string.IsNullOrEmpty(fired.displayName) ? null : fired.displayName);
        }
    }
}
#endif
