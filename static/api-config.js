// Replace this value when the temporary Cloudflare tunnel changes.
const staticverLocalHost = ["127.0.0.1", "localhost"].includes(window.location.hostname);
window.STATICVER_API_BASE = staticverLocalHost ? "" : "https://neither-informed-tier-sustainability.trycloudflare.com";
