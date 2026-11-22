/* Duplex voice client for WS /voice/live.
 *
 * Shared by the scoped builds' portals — their chat.js files are near-twins
 * and mic code must not be forked into each.
 *
 * Sample rates are NEVER hardcoded here: they arrive in the `ready` frame,
 * because Gemini Live is 16k in / 24k out while Azure Realtime is 24k both
 * ways, and the provider is an operator-editable config row.
 *
 * Wire protocol is defined once, server-side, in voice/live/protocol.py —
 * every frame type/field name read below (`ready.audio.*`, `interrupted`,
 * the close codes checked in onclose) must match that file exactly.
 */
(function (global) {
  "use strict";

  // The only encoding _play()/the capture worklet know how to handle: raw
  // signed 16-bit PCM, hard-decoded via Int16Array below. `ready.audio.encoding`
  // IS negotiated (AudioFormat.encoding, voice/live/base.py) so a future
  // provider can declare something else -- but until this client supports
  // more than one encoding, anything other than this must be refused rather
  // than silently decoded as pcm16 (silent garbage audio, not a crash).
  var SUPPORTED_ENCODING = "pcm16";

  // Capture worklet. Two things it must get right, both learned the hard way:
  //
  // 1. RESAMPLE to the negotiated rate. `new AudioContext({sampleRate})` is a
  //    REQUEST, not a guarantee -- a browser may hand back a context running at
  //    the device's native rate (44.1k/48k) instead. Shipping those samples
  //    under the negotiated 16k label makes the caller sound ~3x too fast, and
  //    the model simply cannot understand it: no transcripts, no reply, and no
  //    error anywhere. So the worklet is told the context's REAL rate and the
  //    target, and converts between them itself.
  // 2. COALESCE. `process()` fires every 128 frames (~2.7ms at 48k), and one
  //    WebSocket message per block is ~375 msg/s of 256-byte payloads. Buffer
  //    to CHUNK_MS of target-rate audio before posting.
  var CHUNK_MS = 40;

  var WORKLET_SRC =
    'class Cap extends AudioWorkletProcessor{' +
    'constructor(o){super();' +
    'var p=(o&&o.processorOptions)||{};' +
    'this.inRate=p.inRate||sampleRate;this.outRate=p.outRate||this.inRate;' +
    'this.ratio=this.inRate/this.outRate;' +
    'this.target=Math.max(1,Math.round(this.outRate*(p.chunkMs||40)/1000));' +
    'this.buf=new Int16Array(this.target);this.n=0;this.pos=0;}' +
    'process(inputs){var ch=inputs[0]&&inputs[0][0];' +
    'if(!ch||!ch.length)return true;' +
    // Walk the output grid; `pos` carries the fractional read cursor across
    // blocks so no sample is dropped or duplicated at a block boundary.
    'while(this.pos<ch.length){' +
    'var i=Math.floor(this.pos);var frac=this.pos-i;' +
    'var a=ch[i];var b=i+1<ch.length?ch[i+1]:a;' +
    'var s=a+(b-a)*frac;' +
    'if(s>1)s=1;else if(s<-1)s=-1;' +
    'this.buf[this.n++]=s<0?s*0x8000:s*0x7fff;' +
    'if(this.n>=this.target){' +
    'this.port.postMessage(this.buf.buffer.slice(0));this.n=0;}' +
    'this.pos+=this.ratio;}' +
    'this.pos-=ch.length;' +
    'return true;}}' +
    'registerProcessor("cap",Cap);';

  function VoiceLiveClient(options) {
    this.url = options.url;
    // Optional first frame, sent the moment the socket opens: a browser
    // cannot set an Authorization header on a WebSocket, so an endpoint that
    // needs the caller's identity takes it as `{type:"auth", token:...}`
    // (voice/live/protocol.py). Unset for the anonymous portals.
    this.auth = options.auth || null;
    this.onEvent = options.onEvent || function () {};
    this.onState = options.onState || function () {};
    this.ws = null;
    this.muted = false;
    this._inCtx = null;
    this._outCtx = null;
    this._node = null;
    this._stream = null;
    this._playAt = 0;
    this._outRate = 24000;
    // Bumped by _stopCapture() (disconnect() and the onclose handler both
    // call it) and again at the top of every _startCapture() call. A
    // getUserMedia/addModule promise still in flight when either happens
    // captures the value at its own call time and compares against the
    // live counter when it resolves -- a mismatch means "superseded,"
    // never wire up (or leave live) what it was just handed. See
    // _startCapture for why this exists.
    this._captureGen = 0;
    this._ringCtx = null;
    this._ringOsc = null;
    this._micPromise = null;
  }

  VoiceLiveClient.prototype.connect = function () {
    var self = this;
    this.onState("connecting");
    // Ring from the click, not from `ready`: the caller must hear something
    // during the handshake + provider connect, which is the very gap they
    // read as "stuck".
    this._startRing();
    // MOBILE, both of these. A phone browser only honours two things inside a
    // real user gesture, and the click is the only one we get:
    //   * getUserMedia — asking later (we used to ask on the `ready` frame)
    //     can be refused outright or never prompt, so the mic silently never
    //     opens. Ask NOW, attach when `ready` tells us the rate.
    //   * AudioContext.resume() — a context created outside a gesture comes
    //     back SUSPENDED and stays that way, so the greeting plays into a
    //     muted context and the caller hears nothing.
    this._prewarmMic();
    this._prewarmPlayback();
    this.ws = new WebSocket(this.url);
    this.ws.binaryType = "arraybuffer";
    if (this.auth) {
      this.ws.onopen = function () {
        try { self.ws.send(JSON.stringify(self.auth)); } catch (e) { }
      };
    }

    this.ws.onmessage = function (evt) {
      if (typeof evt.data === "string") {
        var frame = JSON.parse(evt.data);
        if (frame.type === "ready") {
          var encoding = frame.audio && frame.audio.encoding;
          if (encoding && encoding !== SUPPORTED_ENCODING) {
            // Negotiated but unsupported (e.g. a future non-Gemini provider
            // row) -- refuse rather than hard-decoding it as Int16Array
            // pcm16 and playing back silent garbage with no error anywhere.
            self.onEvent({
              type: "error",
              message: "Unsupported audio encoding from server: " + encoding + ".",
            });
            self.disconnect();
            return;
          }
          self._outRate = frame.audio.output_sample_rate;
          self._startCapture(frame.audio.input_sample_rate);
          self.onState("listening");
        } else if (frame.type === "interrupted") {
          self._flushPlayback();
        }
        self.onEvent(frame);
      } else {
        self._play(evt.data);
      }
    };

    // Application close codes (4000-4999 -- see protocol.py) are sent by the
    // server AFTER a successful handshake, so they always reach here as a
    // real close code with a reason. A rejection that happens BEFORE the
    // handshake completes (e.g. resolve_channel() raising ahead of
    // websocket.accept() in endpoints/voice_live.py) never becomes a close
    // frame at all -- the browser sees a failed handshake, which fires
    // onerror (no useful code/reason) followed by onclose with code 1006.
    // Surfacing a message from onerror is what keeps that path from looking
    // like a silently-dead microphone.
    this.ws.onclose = function (evt) {
      self._stopRing();
      self._stopCapture();
      self.onState("closed");
      if (evt.code >= 4000) {
        self.onEvent({ type: "error", message: evt.reason || "Voice unavailable." });
      }
    };

    this.ws.onerror = function () {
      self.onEvent({ type: "error", message: "Voice connection failed." });
    };
  };

  VoiceLiveClient.prototype._prewarmMic = function () {
    // Kept as a PROMISE, not a stream: the permission prompt may still be up
    // when `ready` arrives, and _startCapture just awaits whatever this
    // resolves to. Failure is surfaced there, once, with the real reason.
    try {
      this._micPromise = navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (e) {
      this._micPromise = Promise.reject(e);
    }
  };

  VoiceLiveClient.prototype._prewarmPlayback = function () {
    if (this._outCtx) return;
    try {
      // No sampleRate option: we do not know the negotiated output rate yet
      // (it arrives in `ready`), and a context must be created inside the
      // gesture to be resumable. Buffers are created at the NEGOTIATED rate in
      // _play() and the browser resamples them into this context — which is
      // exactly what createBuffer's sampleRate argument is for.
      this._outCtx = new (global.AudioContext || global.webkitAudioContext)();
      if (this._outCtx.state === "suspended" && this._outCtx.resume) this._outCtx.resume();
      this._playAt = this._outCtx.currentTime;
    } catch (e) {
      this._outCtx = null;  // _play() falls back to creating one lazily
    }
  };

  VoiceLiveClient.prototype._startCapture = function (inputRate) {
    var self = this;
    // Mint the generation this call owns BEFORE the first await. If
    // disconnect()/onclose fires while getUserMedia's permission prompt (or
    // addModule's network fetch) is still pending, _stopCapture() bumps
    // _captureGen -- so by the time either promise settles, `gen` no
    // longer matches and we know this call was cancelled out from under
    // itself. Without this, the late .then() would unconditionally
    // overwrite the nulls _stopCapture() just set and resurrect a live mic
    // track that nothing will ever stop again (the client already
    // considers itself disconnected, so no further disconnect() call is
    // coming) -- the browser's recording indicator stays lit until reload.
    var gen = ++self._captureGen;
    var micReady = self._micPromise || navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    micReady
      .then(function (stream) {
        if (gen !== self._captureGen) {
          stream.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        self._stream = stream;
        self._inCtx = new (global.AudioContext || global.webkitAudioContext)({ sampleRate: inputRate });
        var blob = new Blob([WORKLET_SRC], { type: "application/javascript" });
        return self._inCtx.audioWorklet.addModule(URL.createObjectURL(blob)).then(function () {
          if (gen !== self._captureGen) {
            // Cancelled while the worklet module was loading -- tear down
            // what was just built instead of wiring it up onto a session
            // that's already gone.
            stream.getTracks().forEach(function (t) { t.stop(); });
            try { self._inCtx.close(); } catch (e) { }
            self._stream = null;
            self._inCtx = null;
            return;
          }
          // The rate the browser ACTUALLY gave us, which may not be the rate
          // we asked for. Everything downstream is negotiated against
          // `inputRate`, so the worklet converts real -> negotiated rather
          // than us shipping mislabelled samples.
          if (self._inCtx.state === "suspended" && self._inCtx.resume) self._inCtx.resume();
          var actualRate = self._inCtx.sampleRate;
          if (actualRate !== inputRate) {
            self.onEvent({
              type: "debug",
              message:
                "mic context is " + actualRate + "Hz, not the negotiated " +
                inputRate + "Hz — resampling in the worklet",
            });
          }
          var source = self._inCtx.createMediaStreamSource(stream);
          self._node = new AudioWorkletNode(self._inCtx, "cap", {
            processorOptions: {
              inRate: actualRate,
              outRate: inputRate,
              chunkMs: CHUNK_MS,
            },
          });
          self._node.port.onmessage = function (e) {
            if (!self.muted && self.ws && self.ws.readyState === WebSocket.OPEN) {
              self.ws.send(e.data);
            }
          };
          source.connect(self._node);
        });
      })
      .catch(function (err) {
        if (gen !== self._captureGen) return; // already cancelled -- don't report a stale failure
        // Name the real failure. This used to say "permission denied" for
        // everything, including an AudioWorklet that failed to load — which
        // sent us looking at permissions on a mobile bug that was not one.
        var name = (err && (err.name || err.message)) || "unknown error";
        var denied = /NotAllowed|Permission|SecurityError/i.test(String(name));
        self.onEvent({
          type: "error",
          message: denied
            ? "Microphone permission denied."
            : "Could not start the microphone (" + name + ").",
        });
      });
  };

  // Ringback: a real phone's "we're connecting you" cue, synthesized rather
  // than shipped as an asset (no binary in the repo, no extra request, and it
  // works offline). Gulf/EU cadence: a 425 Hz tone, 1s on / 3s off. It starts
  // on connect() and is stopped by the FIRST audio frame — i.e. the moment
  // the agent actually speaks, which is what "the call was answered" means to
  // a caller. Deliberately here in the shared client, not a portal, so every
  // front end built on it behaves the same.
  //
  // Uses its own AudioContext: _outCtx is created lazily by _play() at the
  // NEGOTIATED output rate and its scheduling clock (`_playAt`) must not be
  // perturbed by a tone we scheduled earlier.
  var RING_HZ = 425;
  var RING_ON = 1.0;
  var RING_PERIOD = 4.0;

  VoiceLiveClient.prototype._startRing = function () {
    if (this._ringCtx) return;
    // EVERYTHING here is inside one try. This runs synchronously inside the
    // click handler, before the WebSocket is created — so anything that throws
    // (mobile browsers cap live AudioContexts, and some reject scheduling on a
    // freshly-created one) took the whole call down with it and the button
    // looked dead. Ringback is a nicety; the call is not.
    try {
      var ctx = new (global.AudioContext || global.webkitAudioContext)();
      this._ringCtx = ctx;
      // Mobile hands back a SUSPENDED context. resume() only works inside a
      // user gesture, which is exactly where we are.
      if (ctx.state === "suspended" && ctx.resume) ctx.resume();
      var gain = ctx.createGain();
      gain.gain.value = 0;
      gain.connect(ctx.destination);
      var osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = RING_HZ;
      osc.connect(gain);
      // Schedule bursts up front rather than on a timer: setInterval in a
      // backgrounded tab stutters the cadence, and the ring is always stopped
      // explicitly (answered, closed, disconnected) long before this runs out.
      var t = ctx.currentTime + 0.15;
      for (var i = 0; i < 30; i++) {
        var at = t + i * RING_PERIOD;
        gain.gain.setValueAtTime(0.0001, at);
        gain.gain.exponentialRampToValueAtTime(0.12, at + 0.04);
        gain.gain.setValueAtTime(0.12, at + RING_ON - 0.04);
        gain.gain.exponentialRampToValueAtTime(0.0001, at + RING_ON);
      }
      osc.start(t);
      this._ringOsc = osc;
    } catch (e) {
      this._ringOsc = null;
      // Keep whatever context we managed to create: _stopRing closes it, and
      // leaking one is what pushes a phone over its context limit next time.
    }
  };

  VoiceLiveClient.prototype._stopRing = function () {
    if (this._ringOsc) { try { this._ringOsc.stop(); } catch (e) { } }
    if (this._ringCtx) { try { this._ringCtx.close(); } catch (e) { } }
    this._ringOsc = null;
    this._ringCtx = null;
  };

  VoiceLiveClient.prototype._play = function (buffer) {
    // First audio of the call = the agent picked up.
    this._stopRing();
    if (!this._outCtx) {
      // Fallback only — _prewarmPlayback() normally created this inside the
      // click. Reached when that threw, and on desktop where it doesn't matter.
      this._outCtx = new (global.AudioContext || global.webkitAudioContext)();
      this._playAt = this._outCtx.currentTime;
    }
    // A phone suspends contexts when the tab backgrounds or a call comes in;
    // resuming per chunk is cheap and is the difference between the rest of the
    // answer playing and silence.
    if (this._outCtx.state === "suspended" && this._outCtx.resume) this._outCtx.resume();
    var pcm = new Int16Array(buffer);
    var frames = this._outCtx.createBuffer(1, pcm.length, this._outRate);
    var channel = frames.getChannelData(0);
    for (var i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 0x8000;
    var src = this._outCtx.createBufferSource();
    src.buffer = frames;
    src.connect(this._outCtx.destination);
    var at = Math.max(this._playAt, this._outCtx.currentTime);
    src.start(at);
    this._playAt = at + frames.duration;
    // Total audio ever SCHEDULED, in ms — the server-side counterpart counts
    // the same stream as it forwards it, so (scheduled − unplayed remainder)
    // at flush time tells the server exactly how far the caller heard.
    this._scheduledMs = (this._scheduledMs || 0) + frames.duration * 1000;
    this._sources = this._sources || [];
    this._sources.push(src);
    src.onended = function () { };
  };

  VoiceLiveClient.prototype._flushPlayback = function () {
    (this._sources || []).forEach(function (s) { try { s.stop(); } catch (e) { } });
    this._sources = [];
    if (this._outCtx) {
      // Report how much audio was actually PLAYED before this flush. The
      // server streams a reply much faster than realtime, so seconds of
      // buffered speech die here — without this report the model's context
      // keeps sentences the caller never heard (it "remembers saying" them).
      // The server maps the total onto its own ledger and truncates the
      // conversation item that was playing (voice/live/base.py's
      // report_playback). Best-effort: an old server just ignores the frame.
      // `ms` is the OFFSET INTO THE RECEIVED AUDIO STREAM that playback had
      // reached — total scheduled minus the still-unplayed tail of the
      // current contiguous segment. NOT a "total seconds played" sum: audio
      // a previous flush discarded stays counted in _scheduledMs, which is
      // exactly what keeps this aligned with the server's sent-stream ledger
      // across multiple flushes (a played-sum would drift backwards by every
      // discarded tail and truncate the wrong item).
      var unplayedMs = Math.max(0, (this._playAt - this._outCtx.currentTime) * 1000);
      var playedMs = Math.max(0, (this._scheduledMs || 0) - unplayedMs);
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: "played", ms: Math.round(playedMs) }));
        } catch (e) { }
      }
      this._playAt = this._outCtx.currentTime;
    }
  };

  VoiceLiveClient.prototype.setMuted = function (muted) {
    this.muted = !!muted;
    this.onState(this.muted ? "muted" : "listening");
  };

  VoiceLiveClient.prototype._stopCapture = function () {
    // Invalidate any _startCapture() call still awaiting getUserMedia/
    // addModule -- see the comment there. Must happen before the
    // synchronous teardown below so a call already in the "resolved,
    // about to assign" gap has already lost the race the moment it checks.
    this._captureGen++;
    this._micPromise = null;
    if (this._node) { try { this._node.disconnect(); } catch (e) { } }
    if (this._stream) this._stream.getTracks().forEach(function (t) { t.stop(); });
    if (this._inCtx) { try { this._inCtx.close(); } catch (e) { } }
    this._node = null; this._stream = null; this._inCtx = null;
  };

  // Releases the playback AudioContext opened lazily by _play(). Not folded
  // into _flushPlayback() -- that also runs on every mid-session
  // `interrupted` barge-in, and tearing down the output context there would
  // kill a session that's still going. This is disconnect()-only, and safe
  // to call more than once (close() on an already-closed context is a
  // documented no-op-ish rejection we don't care about).
  VoiceLiveClient.prototype._stopPlayback = function () {
    if (this._outCtx) { try { this._outCtx.close(); } catch (e) { } }
    this._outCtx = null;
    this._playAt = 0;
  };

  VoiceLiveClient.prototype.disconnect = function () {
    this._stopRing();
    this._flushPlayback();
    this._stopCapture();
    this._stopPlayback();
    if (this.ws) { try { this.ws.close(); } catch (e) { } }
    this.ws = null;
  };

  global.VoiceLiveClient = VoiceLiveClient;
})(window);
