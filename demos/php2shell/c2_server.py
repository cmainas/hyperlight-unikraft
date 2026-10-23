#!/usr/bin/env python3
"""A stand-in command-and-control listener for the demo.

Run it in a split terminal (`just c2`) to watch exfiltration arrive when
the payload runs on bare metal.  When the payload runs inside hluk the
guest cannot reach it, so nothing shows up here.
"""
import socketserver

HOST, PORT = "127.0.0.1", 9000


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        data = self.rfile.read()
        print(f"[C2] {self.client_address[0]} sent {len(data)} bytes:")
        print(data.decode("utf-8", "replace"))


if __name__ == "__main__":
    print(f"[C2] listening on {HOST}:{PORT} (Ctrl-C to stop)")
    with socketserver.TCPServer((HOST, PORT), Handler) as srv:
        srv.serve_forever()
