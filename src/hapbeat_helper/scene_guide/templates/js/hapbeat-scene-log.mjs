// hapbeat-scene-log.mjs: a Hapbeat Scene recording log (hapbeat-contracts specs/scene-recording.md section 5)
// for games that use hapbeat-js-sdk. `hapbeat-helper scene session --log events.jsonl` turns it into the
// session.json that Hapbeat Studio's Scene tab reads.
//
//   const log = new SceneLog('my-game', 30);
//   const transport = new RecordingTransport(log, () => frame, (eventId) => cueFor(eventId));
//   // give `transport` to the SDK instead of the Node / browser transport: nothing is sent to devices
//   log.take(frame, 'take 1: hit', ['hit']); ... log.takeEnd(frame); ... log.end(frames);
//   fs.writeFileSync('Saved/HapticRecord/events.jsonl', log.text());
//
// Keep the page / process muted while recording.

export class SceneLog {
  constructor(project, fps) {
    this.lines = [];
    this.write({ t: 'start', schema_version: 1, project, fps });
  }

  write(record) {
    this.lines.push(JSON.stringify(record));
  }

  /** A take of the script starts. expect: cues that must fire in it; allow: cues that may. */
  take(frame, label, expect = [], allow = []) {
    this.write({ t: 'take', frame, label, expect, allow });
  }

  takeEnd(frame) {
    this.write({ t: 'take_end', frame });
  }

  /** A cue fired: 'cue' or 'cue:variant'. extra: { hand, gain, dist, note } (all optional). */
  cue(frame, cue, extra = {}) {
    this.write({ t: 'cue', frame, cue, ...extra });
  }

  /** A loop cue's input from this frame on, e.g. (frame, 'rub:level:left', 0.4). */
  level(frame, column, value) {
    this.write({ t: 'level', frame, values: { [column]: value } });
  }

  fail(reason, frame) {
    this.write(frame === undefined ? { t: 'fail', reason } : { t: 'fail', frame, reason });
  }

  end(frames) {
    this.write({ t: 'end', frames });
  }

  /** The JSON Lines file content. */
  text() {
    return this.lines.join('\n') + '\n';
  }
}

/**
 * A Transport (hapbeat-js-sdk src/types.ts) that sends nothing and logs each play() as a cue record.
 * frameOf(): the current recording frame. cueOf(eventId): the cue name for an event id ('cue' or
 * 'cue:variant'), or null to log nothing for it.
 */
export class RecordingTransport {
  constructor(log, frameOf, cueOf = (eventId) => eventId) {
    this.log = log;
    this.frameOf = frameOf;
    this.cueOf = cueOf;
  }

  async connect() {}

  play(eventId, gain, target, _targetTimeUs, _pan) {
    const cue = this.cueOf(eventId, target);
    if (cue) this.log.cue(this.frameOf(), cue, gain === 1 ? {} : { gain });
  }

  stop() {}
  stopAll() {}
  ping() {}
  async discover() {
    return [];
  }
  close() {}

  // EndpointStreamSink (src/stream.ts): clip streams are not sent while recording (no endpoints, every
  // session stays deferred). Log a stream's start from the game code with log.cue(...) where it streams.
  streamEndpoints() {
    return [];
  }
  streamBeginEndpoint() {
    return false;
  }
  streamDataEndpoint() {}
  streamEndEndpoint() {}
}
