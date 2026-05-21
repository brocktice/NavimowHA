class NavimowYardPanel extends HTMLElement {
  connectedCallback() {
    const panelConfig = this._panelConfig || {};
    const iframe = document.createElement("iframe");
    iframe.src = panelConfig.url || "/navimow_yard_static/zone_editor.html?ha=1";
    iframe.style.border = "0";
    iframe.style.width = "100%";
    iframe.style.height = "100%";
    iframe.setAttribute("title", "Navimow Yard");
    this.replaceChildren(iframe);
    this.style.display = "block";
    this.style.height = "100%";
  }

  set panel(config) {
    this._panelConfig = config?.config || {};
    if (this.isConnected) {
      this.connectedCallback();
    }
  }
}

customElements.define("navimow-yard-panel", NavimowYardPanel);
