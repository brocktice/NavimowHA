class NavimowYardPanel extends HTMLElement {
  connectedCallback() {
    const panelConfig = this._panelConfig || {};
    if (!this._iframe) {
      const iframe = document.createElement("iframe");
      iframe.src = panelConfig.url || "/navimow_yard_static/zone_editor.html?ha=1";
      iframe.style.border = "0";
      iframe.style.width = "100%";
      iframe.style.height = "100%";
      iframe.setAttribute("title", "Navimow Yard");
      iframe.addEventListener("load", () => this._sendAuthToken());
      this._iframe = iframe;
      this.replaceChildren(iframe);
    }
    this.style.display = "block";
    this.style.height = "100%";
    this._sendAuthToken();
  }

  set panel(config) {
    this._panelConfig = config?.config || {};
    if (this.isConnected) {
      this.connectedCallback();
    }
  }

  set hass(hass) {
    this._hass = hass;
    this._sendAuthToken();
  }

  _sendAuthToken() {
    const token = this._accessToken();
    if (!token || !this._iframe?.contentWindow) {
      return;
    }
    this._iframe.contentWindow.postMessage(
      { type: "navimow-yard-auth", accessToken: token },
      window.location.origin,
    );
  }

  _accessToken() {
    return (
      this._hass?.auth?.data?.access_token ||
      this._hass?.connection?.options?.auth?.accessToken ||
      this._hass?.connection?.options?.auth?.data?.access_token ||
      null
    );
  }
}

customElements.define("navimow-yard-panel", NavimowYardPanel);
