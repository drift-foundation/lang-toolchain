# TCP client/server echo (Drift)

This is a minimal echo server + client example using `std.net` and virtual
threads. The client runs in a VT and relies on VT‑aware blocking I/O.

Files
- `server.drift`
- `client.drift`
- `combined.drift` (single-process example for quick sanity)

Notes
- `combined.drift` uses a VT for the client and accepts on the main thread.
- `server.drift` and `client.drift` are split examples; you can wire them up
  with a shared port.
