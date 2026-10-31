// HapbeatSceneLog: writes a Hapbeat Scene recording log (hapbeat-contracts specs/scene-recording.md section 5),
// which `hapbeat-helper scene session` turns into the session.json that Hapbeat Studio's Scene tab reads.
//
// Copy this file into your Unity project (any folder; it does not depend on the Hapbeat SDK) and call it from
// your recording script: Begin -> (Take, the action, TakeEnd) per take -> End. Call Cue / Level from the place
// where the game fires a haptic (or use HapbeatSceneSdkCapture.cs with Hapbeat SDK 0.7+, which logs every
// Event Map fire by itself). `frame` is the recording frame (frame i is game time i / fps), the same number as
// the f<frame>.png you render.
//
// The recording must be silent and send nothing: run Unity with -HapbeatNoNetwork (or set HapbeatConfig.noNetwork,
// or disable the Hapbeat SDK) and mute the audio (AudioListener.volume = 0, or -batchmode).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using UnityEngine;

namespace Hapbeat.Scene
{
    public static class HapbeatSceneLog
    {
        /// <summary>The line marker when the log goes to the Unity log (section 5.1).</summary>
        public const string Marker = "HAPBEAT_SCENE ";
        /// <summary>The log file hapbeat-scene.json points at by default, relative to the project root.</summary>
        public const string DefaultPath = "Saved/HapticRecord/events.jsonl";

        private static StreamWriter _file;
        private static bool _unityLog;

        public static bool IsRecording => _file != null || _unityLog;

        /// <summary>
        /// Starts a recording. <paramref name="path"/>: the JSON Lines file to (over)write, relative to the project
        /// root or absolute; null writes marker lines to the Unity log instead (Debug.Log).
        /// </summary>
        public static void Begin(string project, float fps, string path = DefaultPath)
        {
            Close();
            if (path != null)
            {
                var full = Path.IsPathRooted(path) ? path : Path.Combine(Application.dataPath, "..", path);
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(full)));
                _file = new StreamWriter(full, false, new UTF8Encoding(false)) { NewLine = "\n", AutoFlush = true };
            }
            else
            {
                _unityLog = true;
            }
            Write(new Json("start").Int("schema_version", 1).Str("project", project).Num("fps", fps));
        }

        /// <summary>A take of the script starts. expect: cues that must fire in it; allow: cues that may.</summary>
        public static void Take(int frame, string label, IEnumerable<string> expect = null, IEnumerable<string> allow = null)
        {
            var j = new Json("take").Int("frame", frame).Str("label", label);
            if (expect != null) j.List("expect", expect);
            if (allow != null) j.List("allow", allow);
            Write(j);
        }

        public static void TakeEnd(int frame) => Write(new Json("take_end").Int("frame", frame));

        /// <summary>
        /// A cue fired: "cue" or "cue:variant". hand: "left" / "right" / "both" (the side that acted, for
        /// at: "hand" routes). gain: the multiplier the game applied. dist: distance to the player in cm.
        /// </summary>
        public static void Cue(int frame, string cue, string hand = null, float? gain = null, float? dist = null, string note = null)
            => Write(Extras(new Json("cue").Int("frame", frame).Str("cue", cue), hand, gain, dist, note));

        /// <summary>A fire named by the SDK entry ("unity:&lt;entry id&gt;"); hapbeat-scene.json bindings map it to a cue.</summary>
        public static void Source(int frame, string source, string hand = null, float? gain = null, float? dist = null, string note = null)
            => Write(Extras(new Json("cue").Int("frame", frame).Str("source", source), hand, gain, dist, note));

        /// <summary>A loop cue's input from this frame on, e.g. ("rub:level:left", 0.4). Write it when it changes.</summary>
        public static void Level(int frame, string column, float value)
            => Write(new Json("level").Int("frame", frame).Obj("values", new Json(null).Num(column, value)));

        /// <summary>A loop's input named by its SDK entry source; bindings give the cue (and the hand when null).</summary>
        public static void LevelSource(int frame, string source, float value, string hand = null)
        {
            var j = new Json("level").Int("frame", frame).Str("source", source).Num("value", value);
            if (hand != null) j.Str("hand", hand);
            Write(j);
        }

        /// <summary>The script failed (an unintended cue, an action that never happened). The recording is rejected.</summary>
        public static void Fail(string reason, int frame = -1)
        {
            var j = new Json("fail");
            if (frame >= 0) j.Int("frame", frame);
            Write(j.Str("reason", reason));
        }

        /// <summary>The recording finished: <paramref name="frames"/> frames (0 .. frames - 1) were rendered.</summary>
        public static void End(int frames)
        {
            Write(new Json("end").Int("frames", frames));
            Close();
        }

        private static Json Extras(Json j, string hand, float? gain, float? dist, string note)
        {
            if (hand != null) j.Str("hand", hand);
            if (gain.HasValue) j.Num("gain", gain.Value);
            if (dist.HasValue) j.Num("dist", dist.Value);
            if (!string.IsNullOrEmpty(note)) j.Str("note", note);
            return j;
        }

        private static void Write(Json j)
        {
            if (_file != null) _file.WriteLine(j.ToString());
            else if (_unityLog) Debug.Log(Marker + j);
            else Debug.LogWarning("HapbeatSceneLog: call Begin first");
        }

        private static void Close()
        {
            _file?.Dispose();
            _file = null;
            _unityLog = false;
        }

        /// <summary>A one-line JSON object builder (no dependency on a JSON library).</summary>
        private sealed class Json
        {
            private readonly StringBuilder _b = new StringBuilder("{");
            private bool _first = true;

            public Json(string type) { if (type != null) Str("t", type); }

            private Json Key(string key)
            {
                if (!_first) _b.Append(',');
                _first = false;
                Quote(key);
                _b.Append(':');
                return this;
            }

            public Json Str(string key, string value) { Key(key).Quote(value); return this; }
            public Json Int(string key, int value) { Key(key)._b.Append(value.ToString(CultureInfo.InvariantCulture)); return this; }
            public Json Num(string key, float value)
            {
                Key(key)._b.Append(float.IsFinite(value) ? value.ToString("0.######", CultureInfo.InvariantCulture) : "0");
                return this;
            }
            public Json Obj(string key, Json value) { Key(key)._b.Append(value); return this; }
            public Json List(string key, IEnumerable<string> values)
            {
                Key(key)._b.Append('[');
                var first = true;
                foreach (var v in values)
                {
                    if (!first) _b.Append(',');
                    first = false;
                    Quote(v);
                }
                _b.Append(']');
                return this;
            }

            private Json Quote(string s)
            {
                _b.Append('"');
                foreach (var c in s ?? "")
                {
                    switch (c)
                    {
                        case '"': _b.Append("\\\""); break;
                        case '\\': _b.Append("\\\\"); break;
                        case '\n': _b.Append("\\n"); break;
                        case '\r': _b.Append("\\r"); break;
                        case '\t': _b.Append("\\t"); break;
                        default:
                            if (c < ' ') _b.Append("\\u").Append(((int)c).ToString("x4"));
                            else _b.Append(c);
                            break;
                    }
                }
                _b.Append('"');
                return this;
            }

            public override string ToString() => _b + "}";
        }
    }
}
