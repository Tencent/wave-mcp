// wave-view service worker.
// Bump SW_VERSION whenever fetch-interception logic changes; view.html
// (EXPECTED_SW_VERSION) refuses to proceed until a matching version
// controls the page, so users never need a hard refresh.
const SW_VERSION = 1;

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "GET_VERSION") {
    const reply = { type: "SW_VERSION", version: SW_VERSION };
    if (event.ports && event.ports[0]) {
      event.ports[0].postMessage(reply);
    } else if (event.source) {
      event.source.postMessage(reply);
    }
  }
});

self.addEventListener("fetch", function (event) {
  if (event.request.cache === "only-if-cached" && event.request.mode !== "same-origin") {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then(function (response) {
        const newHeaders = new Headers(response.headers);
        newHeaders.set("Cross-Origin-Embedder-Policy", "require-corp");
        newHeaders.set("Cross-Origin-Opener-Policy", "same-origin");

        // Reverse proxies (nginx/openresty gateways) rewrite the `Server`
        // header, which Surfer uses to detect a surver backend. Restore
        // it for our same-origin /surver/* proxy path.
        try {
          const url = new URL(event.request.url);
          if (url.origin === self.location.origin &&
              url.pathname.startsWith("/surver/")) {
            newHeaders.set("server", "Surfer");
          }
        } catch (e) { /* ignore */ }

        return new Response(response.body, {
          status: response.status,
          statusText: response.statusText,
          headers: newHeaders,
        });
      })
      .catch(function (e) {
        console.error(e);
        // Return a proper error response instead of undefined
        return new Response("Service Worker fetch error: " + e.message, {
          status: 502,
          statusText: "Bad Gateway",
          headers: { "Content-Type": "text/plain" }
        });
      })
  );
});
