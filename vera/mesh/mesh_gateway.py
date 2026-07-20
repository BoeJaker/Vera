#!/usr/bin/env python3
"""
mesh_gateway.py — LAN → Vera forwarder for firewalled edge devices (ESP32)
==========================================================================

Run this on a machine that (a) sits on the SAME Wi-Fi/LAN as your ESP32 nodes and
(b) can already reach Vera — e.g. your PC or a Raspberry Pi that has the Twingate
client (or any VPN / route) to the Vera server. It re-originates the device's HTTP
to Vera, so a node that can't punch through the firewall to the server can still
reach it *through this box*.

    python mesh_gateway.py --target http://vera-host:8000
    python mesh_gateway.py --target http://100.x.x.x:8000 --port 8088

If Vera is served over HTTPS with a self-signed / private-CA cert (the usual case
on a LAN or Twingate host), plain urllib will reject it with CERTIFICATE_VERIFY_
FAILED. Point it at your CA bundle, or skip verification for a trusted LAN target:

    python mesh_gateway.py --target https://vera-host:8443 --cafile vera-ca.pem
    python mesh_gateway.py --target https://100.x.x.x:8443 --insecure

A nice side effect: the ESP32s speak plain HTTP to this gateway and never have to
do TLS themselves — the gateway terminates it and re-originates HTTPS to Vera.

Then set each node's **Server URL** (in the Mesh panel, or serial provisioning) to:

    http://<THIS-MACHINE-LAN-IP>:8088

Why this works with Twingate: Twingate routes an *authorized client machine's*
traffic to the protected resource. This gateway runs ON that authorized machine,
so the outbound leg to Vera is the machine's own (allowed) connection; the inbound
leg from the ESP32 is plain LAN. The ESP32 never needs Twingate (it can't run it).

Stdlib only — no pip installs. Handles the mesh long-poll (GET /mesh/poll?wait=25)
by simply blocking on the upstream until it answers. Each request is threaded, so
many nodes poll concurrently.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = ""
SSL_CTX: ssl.SSLContext | None = None     # TLS context for the upstream (HTTPS Vera)
# Headers we must not blindly copy between the two connections.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


class _Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mesh-gateway/1.0"

    def _forward(self):
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        url = TARGET + self.path
        req = urllib.request.Request(url, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in _HOP:
                req.add_header(k, v)
        # 40s covers the mesh long-poll (wait=25) plus slack. SSL_CTX is None for
        # http:// targets (ignored) and a real context for https:// ones.
        try:
            with urllib.request.urlopen(req, timeout=40, context=SSL_CTX) as r:
                data = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        except urllib.error.HTTPError as e:           # upstream replied with 4xx/5xx
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except Exception as e:                         # upstream unreachable / timeout
            msg = f"gateway: upstream error: {e}".encode()
            try:
                self.send_response(502)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _forward

    def log_message(self, fmt, *args):                 # quiet by default
        if VERBOSE:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


VERBOSE = False


def _lan_ip() -> str:
    """Best-effort: the LAN IP a node would use to reach this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    global TARGET, VERBOSE, SSL_CTX
    ap = argparse.ArgumentParser(description="Forward ESP32 mesh traffic to a Vera server this machine can reach.")
    ap.add_argument("--target", required=True,
                    help="Vera base URL reachable FROM THIS MACHINE, e.g. http://vera-host:8000 or https://vera-host:8443")
    ap.add_argument("--port", type=int, default=8088, help="port to listen on (default 8088)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default all interfaces)")
    ap.add_argument("--cafile", default="", help="CA bundle (PEM) to trust for an HTTPS target with a private/self-signed cert")
    ap.add_argument("--insecure", "-k", action="store_true",
                    help="skip TLS verification for the HTTPS target (use only for a trusted LAN/VPN Vera)")
    ap.add_argument("--verbose", action="store_true", help="log every forwarded request")
    a = ap.parse_args()
    TARGET = a.target.rstrip("/")
    VERBOSE = a.verbose

    tls = "http"
    if TARGET.lower().startswith("https:"):
        if a.insecure:
            SSL_CTX = ssl.create_default_context()
            SSL_CTX.check_hostname = False
            SSL_CTX.verify_mode = ssl.CERT_NONE
            tls = "https (verification OFF)"
        elif a.cafile:
            SSL_CTX = ssl.create_default_context(cafile=a.cafile)
            tls = f"https (trusting {a.cafile})"
        else:
            SSL_CTX = ssl.create_default_context()     # system trust store
            tls = "https (system CAs)"

    # Quick reachability sanity check (non-fatal).
    try:
        urllib.request.urlopen(TARGET + "/mesh/nodes", timeout=8, context=SSL_CTX).read(1)
        reach = "reachable OK"
    except ssl.SSLCertVerificationError as e:
        reach = (f"TLS cert NOT trusted ({e.reason}). Vera is HTTPS with a private/self-signed "
                 f"cert - re-run with --cafile <vera-ca.pem>, or --insecure for a trusted LAN target.")
    except Exception as e:
        reach = f"NOT reachable yet ({e}) - check Twingate/VPN + the --target URL"

    ip = _lan_ip()
    # ASCII only — the Windows console (cp1252) can't encode box/emoji glyphs and
    # would crash the process on print() before it ever starts serving.
    print("-- Vera mesh gateway ----------------------------------------")
    print(f"  forwarding   http://{a.host}:{a.port}   ->   {TARGET}  [{tls}]")
    print(f"  upstream     {reach}")
    print(f"  set each ESP32 'Server URL' to:   http://{ip}:{a.port}")
    print("  (Ctrl-C to stop)")
    print("-------------------------------------------------------------")
    try:
        ThreadingHTTPServer((a.host, a.port), _Gateway).serve_forever()
    except KeyboardInterrupt:
        print("\nmesh_gateway stopped.")


if __name__ == "__main__":
    main()
