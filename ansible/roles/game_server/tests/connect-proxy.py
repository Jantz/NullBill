#!/usr/bin/env python3
"""A stand-in for the egress proxy in the game checks: answers CONNECT for
the names given as arguments, refuses every other with 403, and logs each
request line to /tmp/proxy.log.

    connect-proxy.py PORT NAME...
"""

import asyncio
import socket
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def handle(allowed, reader, writer):
    request = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode()
    with open("/tmp/proxy.log", "a") as log:
        log.write(request + "\n")
    method, target, _ = request.split()
    host, _, port = target.rpartition(":")
    if method != "CONNECT" or host not in allowed or port != "443":
        writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    up_reader, up_writer = await asyncio.open_connection(allowed[host], 443)
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


async def main():
    allowed = {name: socket.getaddrinfo(name, 443, socket.AF_INET)[0][4][0] for name in sys.argv[2:]}
    server = await asyncio.start_server(lambda r, w: handle(allowed, r, w), "127.0.0.1", int(sys.argv[1]))
    async with server:
        await server.serve_forever()


asyncio.run(main())
