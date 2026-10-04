// Backend location. On Vercel the UI talks to the Render API; when served by the API itself (/app/) it uses the same origin.
window.API_BASE = location.hostname.endsWith("vercel.app") ? "https://orbit-support.onrender.com" : "";
