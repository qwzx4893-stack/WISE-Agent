# Proxy

> H3 proxy utilities.

### `fetchWithEvent(event, url, init?)`

Make a fetch request with the event's context and headers.

### `getProxyRequestHeaders(event)`

Get the request headers object without headers known to cause issues when proxying.

### `proxy(event, target, opts)`

Make a proxy request to a target URL and send the response back to the client.

### `proxyRequest(event, target, opts)`

Proxy the incoming request to a target URL.
