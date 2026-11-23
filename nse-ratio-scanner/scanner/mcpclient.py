"""Minimal stdio JSON-RPC client for the NseKit-MCP server (no MCP SDK needed)."""
import json, os, queue, subprocess, threading, time, logging

from . import config

log = logging.getLogger("mcp")


class MCPError(RuntimeError):
    pass


class Client:
    def __init__(self, name="mcp"):
        self.name = name
        self.p = None
        self.lock = threading.Lock()
        self.id = 0

    # -- process management -------------------------------------------------
    def _start(self):
        self.p = subprocess.Popen(config.NSEKIT_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.p, self.q), daemon=True).start()
        self._req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "nse-ratio-scanner", "version": "1"}}, 120)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        log.info("%s: NseKit-MCP server started (pid %s)", self.name, self.p.pid)

    @staticmethod
    def _reader(p, q):
        for line in p.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    q.put(json.loads(line))
                except ValueError:
                    pass

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def _req(self, method, params, timeout):
        self.id += 1
        mid = self.id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if m.get("id") == mid:
                return m
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def close(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
        self.p = None

    # -- public -------------------------------------------------------------
    def call(self, tool, args=None, timeout=90, retries=1):
        """Call a tool; returns the list of JSON records the tool printed (NaN -> None)."""
        with self.lock:
            for attempt in range(retries + 1):
                try:
                    if self.p is None or self.p.poll() is not None:
                        self._start()
                    r = self._req("tools/call", {"name": tool, "arguments": args or {}}, timeout)
                    res = r.get("result") or {}
                    texts = [c.get("text") for c in res.get("content", []) if c.get("type") == "text"]
                    if "error" in r or res.get("isError"):
                        raise MCPError(f"{tool}: {(r.get('error') or {}).get('message') or (texts[:1] or ['error'])[0][:200]}")
                    out = []
                    for t in texts:
                        try:
                            out.append(json.loads(t.replace("NaN", "null")))
                        except ValueError:
                            out.append(t)
                    return out
                except (TimeoutError, BrokenPipeError, OSError) as e:
                    log.warning("%s: %s failed (%s); restarting server", self.name, tool, e)
                    self.close()
                    if attempt == retries:
                        raise MCPError(f"{tool}: {e}")
