#!/usr/bin/env python3
"""Join a Minecraft server as a player who is not signed in to Mojang, the
way a game with a made-up account would: ask the server its protocol, then
handshake, login start and the encryption response. The server then asks
Mojang whether this player has joined, and Mojang says no.

    login.py PORT NAME
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import uuid


def varint(value: int) -> bytes:
    out = b""
    while True:
        byte, value = value & 0x7F, value >> 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


def recv(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise EOFError("the server closed the connection")
        data += part
    return data


def read_varint(sock: socket.socket) -> int:
    value = 0
    for shift in range(0, 35, 7):
        byte = recv(sock, 1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
    raise ValueError("number too long")


def take(packet: bytes, at: int) -> tuple[bytes, int]:
    """A length-prefixed field of packet at offset at, and the offset after it."""
    size = 0
    for shift in range(0, 35, 7):
        byte = packet[at]
        at += 1
        size |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
    return packet[at : at + size], at + size


def string(text: str) -> bytes:
    return varint(len(text)) + text.encode()


def packet(sock: socket.socket, body: bytes) -> None:
    sock.sendall(varint(len(body)) + body)


def handshake(port: int, protocol: int, state: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), 10)
    packet(sock, b"\x00" + varint(protocol) + string("127.0.0.1") + struct.pack(">H", port) + bytes([state]))
    return sock


def protocol(port: int) -> int:
    with handshake(port, 0xFFFFFFFF, 1) as sock:
        packet(sock, b"\x00")
        read_varint(sock)
        read_varint(sock)
        return json.loads(recv(sock, read_varint(sock)))["version"]["protocol"]


def main(port: int, name: str) -> None:
    with handshake(port, protocol(port), 2) as sock:
        packet(sock, b"\x00" + string(name) + uuid.uuid4().bytes)
        answer = recv(sock, read_varint(sock))
        if answer[0] != 1:
            sys.exit(f"the server did not ask for encryption: packet {answer[0]}")
        _, at = take(answer, 1)  # the server's id, empty
        public_key, at = take(answer, at)
        token, at = take(answer, at)
        with tempfile.NamedTemporaryFile(suffix=".der") as key:
            key.write(public_key)
            key.flush()

            def encrypted(data: bytes) -> bytes:
                argv = ["openssl", "pkeyutl", "-encrypt", "-pubin", "-keyform", "DER", "-inkey", key.name]
                out = subprocess.run(argv, input=data, capture_output=True, check=True).stdout
                return varint(len(out)) + out

            packet(sock, b"\x01" + encrypted(os.urandom(16)) + encrypted(token))
        # The server answers encrypted, or only closes; either way it has
        # asked Mojang by then.
        sock.settimeout(30)
        try:
            sock.recv(4096)
        except OSError:
            pass


main(int(sys.argv[1]), sys.argv[2])
