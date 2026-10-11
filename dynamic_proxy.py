import asyncio
import base64
import urllib.parse

class DynamicProxyBridge:
    def __init__(self, upstream_proxy=None):
        self.upstream_proxy = upstream_proxy
        self.server = None
        self.port = None
        self.is_running = False
        self.active_writers = set()

    def set_upstream(self, upstream_proxy):
        self.upstream_proxy = upstream_proxy
        print(f"🔄 Proxy Bridge Upstream updated to: {upstream_proxy or 'Direct Network'}")
        # Drop active keep-alive sockets so Chrome reconnects via the new upstream proxy
        for writer in list(self.active_writers):
            try:
                writer.close()
            except Exception:
                pass
        self.active_writers.clear()

    async def _pipe(self, reader, writer):
        try:
            while not reader.at_eof():
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_client(self, client_reader, client_writer):
        self.active_writers.add(client_writer)
        try:
            line = await client_reader.readline()
            if not line:
                client_writer.close()
                return

            req_line = line.decode('utf-8', errors='ignore').strip()
            parts = req_line.split()
            if len(parts) < 2:
                client_writer.close()
                return

            method, target = parts[0].upper(), parts[1]

            # Read remaining headers
            while True:
                h_line = await client_reader.readline()
                if not h_line or h_line == b'\r\n' or h_line == b'\n':
                    break

            if method == 'CONNECT':
                # HTTPS Tunneling
                if ':' in target:
                    host, port = target.split(':', 1)
                    port = int(port)
                else:
                    host, port = target, 443

                if self.upstream_proxy:
                    # Tunnel through Upstream Proxy
                    parsed = urllib.parse.urlparse(self.upstream_proxy if "://" in self.upstream_proxy else f"http://{self.upstream_proxy}")
                    u_host = parsed.hostname
                    u_port = parsed.port or 8080
                    u_user = urllib.parse.unquote(parsed.username) if parsed.username else None
                    u_pass = urllib.parse.unquote(parsed.password) if parsed.password else None

                    remote_reader, remote_writer = await asyncio.open_connection(u_host, u_port)
                    self.active_writers.add(remote_writer)
                    
                    connect_str = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    if u_user and u_pass:
                        cred = base64.b64encode(f"{u_user}:{u_pass}".encode()).decode()
                        connect_str += f"Proxy-Authorization: Basic {cred}\r\n"
                    connect_str += "\r\n"

                    remote_writer.write(connect_str.encode())
                    await remote_writer.drain()

                    # Read proxy response
                    resp = b""
                    while b"\r\n\r\n" not in resp:
                        chunk = await remote_reader.read(4096)
                        if not chunk:
                            break
                        resp += chunk

                    if b"200" not in resp.split(b"\r\n")[0]:
                        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        await client_writer.drain()
                        client_writer.close()
                        remote_writer.close()
                        return

                    # Success handshake back to client
                    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await client_writer.drain()
                else:
                    # Direct TCP connection
                    remote_reader, remote_writer = await asyncio.open_connection(host, port)
                    self.active_writers.add(remote_writer)
                    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await client_writer.drain()

                # Bi-directional stream forwarding
                await asyncio.gather(
                    self._pipe(client_reader, remote_writer),
                    self._pipe(remote_reader, client_writer),
                    return_exceptions=True
                )
            else:
                client_writer.write(b"HTTP/1.1 501 Not Implemented\r\n\r\n")
                await client_writer.drain()
                client_writer.close()

        except Exception as e:
            try:
                client_writer.close()
            except Exception:
                pass
        finally:
            self.active_writers.discard(client_writer)

    async def start(self):
        self.server = await asyncio.start_server(self._handle_client, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.is_running = True
        print(f"🚀 Dynamic Proxy Bridge listening on http://127.0.0.1:{self.port}")
        return f"http://127.0.0.1:{self.port}"

    async def stop(self):
        if self.server:
            for writer in list(self.active_writers):
                try:
                    writer.close()
                except Exception:
                    pass
            self.active_writers.clear()
            self.server.close()
            await self.server.wait_closed()
            self.is_running = False
            print("🛑 Dynamic Proxy Bridge stopped.")
