(() => {
  "use strict";

  // Low-latency live view for the Ufanet/UCAMS intercom camera.
  //
  // The stock HA camera player keeps ~3 of HA's own segments buffered and HA
  // disables LL-HLS for HLS inputs, so it trails the Ufanet app by several
  // seconds. This card plays the UCAMS stream itself with hls.js and a small
  // live buffer (like the app). Playlist and segments are proxied by the
  // integration (see live_playlist.py), so the UCAMS token never reaches the
  // browser and CORS does not apply.

  const CARD_TAG = "ufanet-live-camera-card";
  const HLS_JS_URL = "/ufanet_intercom/hls.light.min.js?v=1.7.3";
  // Seconds behind the newest published segment. UCAMS segments are ~4 s and
  // arrive whole, so going much below one segment makes playback stall.
  const DEFAULT_LATENCY = 4.5;
  const RESTART_DELAY_MS = 2000;

  let hlsPromise = null;

  function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsPromise) {
      hlsPromise = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = HLS_JS_URL;
        script.async = true;
        script.onload = () =>
          window.Hls ? resolve(window.Hls) : reject(new Error("hls.js did not load"));
        script.onerror = () => {
          hlsPromise = null;
          reject(new Error("hls.js failed to load"));
        };
        document.head.appendChild(script);
      });
    }
    return hlsPromise;
  }

  class UfanetLiveCameraCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._hass = null;
      this._config = null;
      this._hls = null;
      this._starting = false;
      this._restartTimer = null;
      this._generation = 0;
      this._onVisibility = () => {
        if (document.hidden) this._stop();
        else this._start();
      };
    }

    static getStubConfig(hass) {
      const entity = Object.keys(hass?.states || {}).find(
        (id) => id.startsWith("camera.") && !id.endsWith("_archive")
      );
      return { entity: entity || "camera.example" };
    }

    setConfig(config) {
      if (!config || !config.entity || !String(config.entity).startsWith("camera.")) {
        throw new Error("Set `entity` to a Ufanet live camera (camera.*)");
      }
      const latency = Number(config.latency ?? DEFAULT_LATENCY);
      if (!Number.isFinite(latency) || latency < 1 || latency > 30) {
        throw new Error("`latency` must be a number of seconds between 1 and 30");
      }
      const changed = this._config?.entity !== config.entity || this._config?.latency !== latency;
      this._config = { muted: true, controls: false, ...config, latency };
      this._render();
      if (changed && this.isConnected) this._restart(0);
    }

    set hass(hass) {
      const first = !this._hass;
      this._hass = hass;
      this._updatePoster();
      if (first && this.isConnected) this._start();
    }

    getCardSize() {
      return 5;
    }

    connectedCallback() {
      document.addEventListener("visibilitychange", this._onVisibility);
      this._start();
    }

    disconnectedCallback() {
      document.removeEventListener("visibilitychange", this._onVisibility);
      this._stop();
    }

    _render() {
      const title = this._config.title
        ? `<div class="title">${this._escape(this._config.title)}</div>`
        : "";
      this.shadowRoot.innerHTML = `
        <style>
          ha-card { overflow: hidden; }
          .title { padding: 12px 16px 8px; font-size: 16px; font-weight: 500; }
          .wrap { position: relative; background: #000; aspect-ratio: 16 / 9; }
          video { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }
          .status {
            position: absolute; left: 8px; bottom: 8px; padding: 2px 8px;
            border-radius: 4px; background: rgba(0, 0, 0, .6); color: #fff;
            font: 12px/1.6 var(--paper-font-body1_-_font-family, sans-serif);
          }
          .status:empty { display: none; }
        </style>
        <ha-card>
          ${title}
          <div class="wrap">
            <video playsinline autoplay ${this._config.muted ? "muted" : ""} ${this._config.controls ? "controls" : ""}></video>
            <div class="status"></div>
          </div>
        </ha-card>`;
      this._video = this.shadowRoot.querySelector("video");
      this._status = this.shadowRoot.querySelector(".status");
      this._updatePoster();
    }

    _escape(text) {
      return String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
    }

    _setStatus(text) {
      if (this._status) this._status.textContent = text || "";
    }

    _updatePoster() {
      if (!this._video || !this._hass || !this._config) return;
      const picture = this._hass.states[this._config.entity]?.attributes?.entity_picture;
      if (picture && !this._video.poster) this._video.poster = picture;
    }

    async _start() {
      if (this._hls || this._starting || !this._hass || !this._config || !this._video) return;
      if (document.hidden) return;
      this._starting = true;
      const generation = ++this._generation;
      this._setStatus("Connecting…");
      try {
        const [Hls, session] = await Promise.all([
          loadHls(),
          this._hass.callWS({
            type: "ufanet_intercom/live_stream",
            entity_id: this._config.entity,
          }),
        ]);
        if (generation !== this._generation) return;
        const video = this._video;
        if (Hls.isSupported()) {
          const latency = this._config.latency;
          const hls = new Hls({
            lowLatencyMode: false,
            liveSyncDuration: latency,
            liveMaxLatencyDuration: latency + 6,
            maxLiveSyncPlaybackRate: 1.15,
            backBufferLength: 8,
            maxBufferLength: 12,
            manifestLoadingTimeOut: 15000,
            levelLoadingTimeOut: 15000,
            fragLoadingTimeOut: 20000,
          });
          this._hls = hls;
          hls.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play().catch(() => {});
          });
          hls.on(Hls.Events.FRAG_BUFFERED, () => this._setStatus(""));
          hls.on(Hls.Events.ERROR, (_event, data) => {
            if (!data.fatal) return;
            this._setStatus("Reconnecting…");
            this._restart(RESTART_DELAY_MS);
          });
          hls.loadSource(session.url);
          hls.attachMedia(video);
        } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
          // No MSE: native HLS works but buffers more (higher latency).
          video.src = session.url;
          video.play().catch(() => {});
          this._setStatus("");
        } else {
          this._setStatus("This browser cannot play HLS");
        }
      } catch (err) {
        if (generation !== this._generation) return;
        this._setStatus(`Error: ${err?.message || err}`);
        this._restart(RESTART_DELAY_MS * 3);
      } finally {
        if (generation === this._generation) this._starting = false;
      }
    }

    _stop() {
      this._generation++;
      this._starting = false;
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
      if (this._hls) {
        this._hls.destroy();
        this._hls = null;
      }
      if (this._video) {
        this._video.removeAttribute("src");
        this._video.load();
      }
    }

    _restart(delayMs) {
      this._stop();
      this._restartTimer = setTimeout(() => this._start(), delayMs);
    }
  }

  if (!customElements.get(CARD_TAG)) {
    customElements.define(CARD_TAG, UfanetLiveCameraCard);
    window.customCards = window.customCards || [];
    window.customCards.push({
      type: CARD_TAG,
      name: "Ufanet live camera",
      description: "Low-latency live view of the Ufanet intercom camera (same delay as the Ufanet app).",
      preview: false,
    });
  }
})();
