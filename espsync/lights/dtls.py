"""Minimal DTLS 1.2 client for one cipher suite: TLS_PSK_WITH_AES_128_GCM_SHA256.

This is what the Philips Hue Entertainment API speaks (UDP 2100).  It is written
on top of ``cryptography`` (AES-GCM) and the standard library (HMAC-SHA256 PRF)
instead of a DTLS binding because:

* python-mbedtls only ships wheels up to CPython 3.12 and bundles Mbed TLS 2.28,
  which is end of life;
* pyOpenSSL has DTLS but no PSK callbacks;
* ``cryptography`` is already installed (aioesphomeapi depends on it) and has
  wheels for every platform and Python version we support.

Scope: a client that sends application data with the lowest possible latency.
It implements RFC 6347 (DTLS 1.2) with RFC 4279 (PSK key exchange) and
RFC 5288 (AES-GCM): cookie exchange (HelloVerifyRequest), handshake message
reassembly, flight retransmission, verification of the server's Finished (so a
bridge that doesn't know the key is rejected), alerts and close_notify.  No
renegotiation, session resumption, certificates or other cipher suites.

The protocol core (`DtlsPskSession`) does no I/O, so it can be tested with any
transport; `DtlsPskClient` runs it on an asyncio datagram endpoint.  Sending is
a single non-blocking ``sendto`` with nothing queued.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

DTLS_1_2 = 0xFEFD
DTLS_1_0 = 0xFEFF  # allowed in the record version of the first server flight
TLS_PSK_WITH_AES_128_GCM_SHA256 = 0x00A8

# record content types
CHANGE_CIPHER_SPEC = 20
ALERT = 21
HANDSHAKE = 22
APPLICATION_DATA = 23

# handshake message types
CLIENT_HELLO = 1
SERVER_HELLO = 2
HELLO_VERIFY_REQUEST = 3
CERTIFICATE = 11
SERVER_KEY_EXCHANGE = 12
CERTIFICATE_REQUEST = 13
SERVER_HELLO_DONE = 14
CLIENT_KEY_EXCHANGE = 16
FINISHED = 20

ALERT_WARNING = 1
ALERT_FATAL = 2
CLOSE_NOTIFY = 0

ALERT_NAMES = {
    0: "close_notify",
    10: "unexpected_message",
    20: "bad_record_mac",
    40: "handshake_failure",
    47: "illegal_parameter",
    50: "decode_error",
    51: "decrypt_error",
    70: "protocol_version",
    71: "insufficient_security",
    80: "internal_error",
    90: "user_canceled",
    115: "unknown_psk_identity",
}

RECORD_HEADER = struct.Struct("!BHH6sH")  # type, version, epoch, seq (48 bit), length
HANDSHAKE_HEADER_LEN = 12
GCM_EXPLICIT_NONCE = 8
GCM_TAG = 16


class DtlsError(Exception):
    """Handshake failed or the peer closed the connection."""


class DtlsAlert(DtlsError):
    def __init__(self, level: int, description: int) -> None:
        self.level = level
        self.description = description
        name = ALERT_NAMES.get(description, str(description))
        super().__init__(f"{'fatal' if level == ALERT_FATAL else 'warning'} alert from peer: {name}")


# --------------------------------------------------------------------- crypto
def p_sha256(secret: bytes, seed: bytes, length: int) -> bytes:
    out = b""
    a = seed
    while len(out) < length:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:length]


def prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    """TLS 1.2 PRF with SHA-256 (RFC 5246 section 5)."""
    return p_sha256(secret, label + seed, length)


def psk_premaster(psk: bytes) -> bytes:
    """RFC 4279 section 2: other_secret is N zero bytes for plain PSK."""
    n = len(psk)
    return struct.pack("!H", n) + bytes(n) + struct.pack("!H", n) + psk


def _seq_bytes(seq: int) -> bytes:
    return seq.to_bytes(6, "big")


@dataclass
class Record:
    type: int
    version: int
    epoch: int
    seq: int
    fragment: bytes


def parse_records(datagram: bytes) -> list[Record]:
    """Split a datagram into DTLS records (several may share one datagram)."""
    records = []
    pos = 0
    while pos + RECORD_HEADER.size <= len(datagram):
        ctype, version, epoch, seq, length = RECORD_HEADER.unpack_from(datagram, pos)
        pos += RECORD_HEADER.size
        if pos + length > len(datagram):
            break  # truncated: drop the rest (RFC 6347 4.1.2.7)
        records.append(Record(ctype, version, epoch, int.from_bytes(seq, "big"), datagram[pos : pos + length]))
        pos += length
    return records


class _Cipher:
    """AES-128-GCM record protection for one direction (RFC 5288)."""

    def __init__(self, key: bytes, salt: bytes) -> None:
        self.aead = AESGCM(key)
        self.salt = salt

    def seal(self, ctype: int, epoch: int, seq: int, plaintext: bytes) -> bytes:
        explicit = struct.pack("!H", epoch) + _seq_bytes(seq)
        aad = explicit + struct.pack("!BHH", ctype, DTLS_1_2, len(plaintext))
        return explicit + self.aead.encrypt(self.salt + explicit, plaintext, aad)

    def open(self, rec: Record) -> bytes:
        frag = rec.fragment
        if len(frag) < GCM_EXPLICIT_NONCE + GCM_TAG:
            raise InvalidTag
        explicit = frag[:GCM_EXPLICIT_NONCE]
        seq_num = struct.pack("!H", rec.epoch) + _seq_bytes(rec.seq)
        plain_len = len(frag) - GCM_EXPLICIT_NONCE - GCM_TAG
        aad = seq_num + struct.pack("!BHH", rec.type, rec.version, plain_len)
        return self.aead.decrypt(self.salt + explicit, frag[GCM_EXPLICIT_NONCE:], aad)


# ------------------------------------------------------------- protocol core
class DtlsPskSession:
    """Sans-IO DTLS 1.2 PSK client.

    Usage: ``start()`` returns the first flight (a list of datagrams).  Feed every
    received datagram to ``receive()``; it returns datagrams to send back.  After
    ``established`` becomes true, ``encrypt(data)`` returns one datagram of
    application data.  ``retransmit()`` returns the last flight again (call it on
    a timer while the handshake is incomplete).
    """

    def __init__(self, identity: bytes, psk: bytes, *, random: bytes | None = None) -> None:
        if not psk:
            raise ValueError("empty pre-shared key")
        self.identity = identity
        self.psk = psk
        self.client_random = random if random is not None else os.urandom(32)
        self.server_random = b""
        self.cookie = b""
        self.established = False
        self.closed = False
        self.error: DtlsError | None = None
        self.server_identity_hint = b""

        self._epoch = 0  # write epoch
        self._seqs = [0, 0]  # next record sequence number per epoch
        self._msg_seq = 0  # next handshake message_seq
        self._transcript = bytearray()
        self._hello = b""
        # Last flight as (content type, epoch, plaintext); re-framed with new
        # record sequence numbers on every retransmission (RFC 6347 4.2.4).
        self._flight: list[tuple[int, int, bytes]] = []
        self._next_rx_msg_seq = 0  # next server handshake message_seq we expect
        self._rx_buffers: dict[int, dict] = {}
        self._master = b""
        self._tx: _Cipher | None = None
        self._rx: _Cipher | None = None
        self._rx_epoch1 = False  # server sent ChangeCipherSpec
        self._have_server_hello = False
        self._sent_finished = False
        self._rx_replay: set[int] = set()

    # -- record / handshake framing ------------------------------------------
    def _record(self, ctype: int, payload: bytes, epoch: int | None = None) -> bytes:
        epoch = self._epoch if epoch is None else epoch
        seq = self._seqs[epoch]
        if seq >= (1 << 48) - 1:
            raise DtlsError("record sequence number exhausted")
        self._seqs[epoch] = seq + 1
        if epoch == 0:
            body = payload
        else:
            assert self._tx is not None
            body = self._tx.seal(ctype, epoch, seq, payload)
        return RECORD_HEADER.pack(ctype, DTLS_1_2, epoch, _seq_bytes(seq), len(body)) + body

    def _send_flight(self, flight: list[tuple[int, int, bytes]]) -> list[bytes]:
        self._flight = flight
        return [self._record(ctype, payload, epoch) for ctype, epoch, payload in flight]

    def _handshake(self, msg_type: int, body: bytes, *, hash_it: bool = True) -> bytes:
        n = len(body)
        msg = (
            struct.pack("!B", msg_type)
            + n.to_bytes(3, "big")
            + struct.pack("!H", self._msg_seq)
            + (0).to_bytes(3, "big")
            + n.to_bytes(3, "big")
            + body
        )
        self._msg_seq += 1
        if hash_it:
            self._transcript += msg
        return msg

    def _client_hello(self) -> bytes:
        body = (
            struct.pack("!H", DTLS_1_2)
            + self.client_random
            + b"\x00"  # session id
            + struct.pack("!B", len(self.cookie))
            + self.cookie
            + struct.pack("!HH", 2, TLS_PSK_WITH_AES_128_GCM_SHA256)
            + b"\x01\x00"  # compression: null
        )
        # The ClientHello without a cookie (and the HelloVerifyRequest) are not
        # part of the Finished hash (RFC 6347 4.2.6); the one with a cookie is.
        # We only know which one counts once the server answers, so hash later.
        return self._handshake(CLIENT_HELLO, body, hash_it=False)

    # -- public API ------------------------------------------------------------
    def start(self) -> list[bytes]:
        self._hello = self._client_hello()
        return self._send_flight([(HANDSHAKE, 0, self._hello)])

    def retransmit(self) -> list[bytes]:
        """The last flight again, with fresh record sequence numbers."""
        if self.established or self.closed or not self._flight:
            return []
        return [self._record(ctype, payload, epoch) for ctype, epoch, payload in self._flight]

    def receive(self, datagram: bytes) -> list[bytes]:
        if self.closed:
            return []
        out: list[bytes] = []
        for rec in parse_records(datagram):
            if rec.epoch == 0:
                plain = rec.fragment
            elif rec.epoch == 1 and self._rx is not None and self._rx_epoch1:
                if rec.seq in self._rx_replay:
                    continue
                try:
                    plain = self._rx.open(rec)
                except InvalidTag:
                    continue  # forged or corrupted: silently drop (RFC 6347 4.1.2.1)
                self._rx_replay.add(rec.seq)
                if len(self._rx_replay) > 4096:
                    self._rx_replay = {s for s in self._rx_replay if s > rec.seq - 1024}
            else:
                continue
            if rec.type == HANDSHAKE:
                out += self._on_handshake_record(plain)
            elif rec.type == CHANGE_CIPHER_SPEC:
                if self._sent_finished and plain == b"\x01":
                    self._rx_epoch1 = True
            elif rec.type == ALERT and len(plain) >= 2:
                self._on_alert(plain[0], plain[1])
            # application data from the server is ignored (Hue never sends any)
            if self.closed:
                break
        return out

    def encrypt(self, data: bytes) -> bytes:
        if not self.established or self.closed:
            raise DtlsError("connection not established")
        return self._record(APPLICATION_DATA, data)

    def close_notify(self) -> bytes | None:
        if self.closed:
            return None
        self.closed = True
        if not self.established:
            return None
        return self._record(ALERT, bytes((ALERT_WARNING, CLOSE_NOTIFY)))

    # -- handshake processing --------------------------------------------------
    def _on_alert(self, level: int, desc: int) -> None:
        if desc == CLOSE_NOTIFY or level == ALERT_FATAL:
            self.closed = True
            self.error = DtlsAlert(level, desc)

    def _on_handshake_record(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        pos = 0
        while pos + HANDSHAKE_HEADER_LEN <= len(data):
            msg_type = data[pos]
            length = int.from_bytes(data[pos + 1 : pos + 4], "big")
            msg_seq = struct.unpack_from("!H", data, pos + 4)[0]
            frag_off = int.from_bytes(data[pos + 6 : pos + 9], "big")
            frag_len = int.from_bytes(data[pos + 9 : pos + 12], "big")
            frag = data[pos + HANDSHAKE_HEADER_LEN : pos + HANDSHAKE_HEADER_LEN + frag_len]
            pos += HANDSHAKE_HEADER_LEN + frag_len
            if len(frag) != frag_len or frag_off + frag_len > length:
                break
            body = self._reassemble(msg_type, length, msg_seq, frag_off, frag)
            if body is None:
                continue
            out += self._on_message(msg_type, msg_seq, body)
            if self.closed:
                break
        return out

    def _reassemble(self, msg_type: int, length: int, msg_seq: int, off: int, frag: bytes) -> bytes | None:
        if msg_type == HELLO_VERIFY_REQUEST:
            # Sent before any state; message_seq mirrors the ClientHello's.
            return frag if off == 0 and len(frag) == length else None
        if msg_seq < self._next_rx_msg_seq:
            return None  # duplicate (the server retransmitted its flight)
        if off == 0 and len(frag) == length:
            return frag
        buf = self._rx_buffers.setdefault(msg_seq, {"type": msg_type, "data": bytearray(length), "have": set()})
        buf["data"][off : off + len(frag)] = frag
        buf["have"].update(range(off, off + len(frag)))
        if len(buf["have"]) == length:
            del self._rx_buffers[msg_seq]
            return bytes(buf["data"])
        return None

    def _fail(self, msg: str) -> None:
        self.closed = True
        self.error = DtlsError(msg)

    def _on_message(self, msg_type: int, msg_seq: int, body: bytes) -> list[bytes]:
        if msg_type == HELLO_VERIFY_REQUEST:
            if self._have_server_hello or len(body) < 3:
                return []
            cookie = body[3 : 3 + body[2]]
            self.cookie = cookie
            self._hello = self._client_hello()
            return self._send_flight([(HANDSHAKE, 0, self._hello)])

        if msg_type == SERVER_HELLO and not self._have_server_hello:
            # A server that sent HelloVerifyRequest numbers its ServerHello 1,
            # a stateless one may restart at 0: follow whatever it uses.
            self._next_rx_msg_seq = msg_seq
        if msg_seq != self._next_rx_msg_seq:
            return []  # out of order or duplicate; wait for the retransmission
        self._next_rx_msg_seq += 1
        n = len(body)
        header = (
            struct.pack("!B", msg_type) + n.to_bytes(3, "big") + struct.pack("!H", msg_seq)
            + (0).to_bytes(3, "big") + n.to_bytes(3, "big")
        )

        if msg_type == SERVER_HELLO:
            self._transcript += self._hello  # the ClientHello the server accepted
            self._transcript += header + body
            self._on_server_hello(body)
        elif msg_type == SERVER_KEY_EXCHANGE:
            self._transcript += header + body
            if len(body) >= 2:
                hint_len = struct.unpack_from("!H", body, 0)[0]
                self.server_identity_hint = body[2 : 2 + hint_len]
        elif msg_type in (CERTIFICATE, CERTIFICATE_REQUEST):
            self._fail("server asked for certificates (not a PSK server)")
        elif msg_type == SERVER_HELLO_DONE:
            self._transcript += header + body
            if not self._have_server_hello:
                self._fail("ServerHelloDone before ServerHello")
                return []
            return self._client_finish()
        elif msg_type == FINISHED:
            if not self._sent_finished or not self._rx_epoch1:
                self._fail("unexpected Finished")
                return []
            expected = prf(self._master, b"server finished", hashlib.sha256(self._transcript).digest(), 12)
            if not hmac.compare_digest(expected, body):
                self._fail("server Finished did not verify (wrong client key?)")
                return []
            self._transcript += header + body
            self.established = True
            self._flight = []
        return []

    def _on_server_hello(self, body: bytes) -> None:
        if len(body) < 38:
            self._fail("short ServerHello")
            return
        version = struct.unpack_from("!H", body, 0)[0]
        if version != DTLS_1_2:
            self._fail(f"server chose an unsupported version 0x{version:04x}")
            return
        self.server_random = body[2:34]
        sid_len = body[34]
        pos = 35 + sid_len
        if len(body) < pos + 3:
            self._fail("short ServerHello")
            return
        suite = struct.unpack_from("!H", body, pos)[0]
        if suite != TLS_PSK_WITH_AES_128_GCM_SHA256:
            self._fail(f"server chose cipher suite 0x{suite:04x}")
            return
        if body[pos + 2] != 0:
            self._fail("server chose compression")
            return
        self._have_server_hello = True

    def _client_finish(self) -> list[bytes]:
        pms = psk_premaster(self.psk)
        self._master = prf(pms, b"master secret", self.client_random + self.server_random, 48)
        kb = prf(self._master, b"key expansion", self.server_random + self.client_random, 40)
        client_key, server_key, client_iv, server_iv = kb[0:16], kb[16:32], kb[32:36], kb[36:40]

        cke = self._handshake(CLIENT_KEY_EXCHANGE, struct.pack("!H", len(self.identity)) + self.identity)
        self._tx = _Cipher(client_key, client_iv)
        self._rx = _Cipher(server_key, server_iv)
        verify = prf(self._master, b"client finished", hashlib.sha256(self._transcript).digest(), 12)
        fin = self._handshake(FINISHED, verify)
        self._epoch = 1
        self._sent_finished = True
        return self._send_flight([(HANDSHAKE, 0, cke), (CHANGE_CIPHER_SPEC, 0, b"\x01"), (HANDSHAKE, 1, fin)])


# ------------------------------------------------------------ asyncio client
class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, client: DtlsPskClient) -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self.client._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        # ICMP port unreachable etc.  Harmless while streaming (UDP), but during
        # the handshake it means nothing listens on the port.
        self.client._on_socket_error(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.client._on_lost(exc)


class DtlsPskClient:
    """DTLS-PSK client on an asyncio datagram endpoint.

    ``await connect()`` performs the handshake (retransmitting with exponential
    back-off) and raises DtlsError/TimeoutError/OSError on failure.  ``send()``
    encrypts and transmits one datagram immediately.  ``on_close`` is called
    (once) if the peer closes the connection or sends a fatal alert.
    """

    def __init__(self, host: str, port: int, identity: bytes, psk: bytes) -> None:
        self.host = host
        self.port = port
        self.session = DtlsPskSession(identity, psk)
        self.on_close: Callable[[Exception | None], None] | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._handshake_done: asyncio.Future | None = None
        self._socket_error: Exception | None = None
        self._closed_reported = False

    @property
    def connected(self) -> bool:
        return self.session.established and not self.session.closed and self._transport is not None

    async def connect(self, timeout: float = 8.0) -> None:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(self.host, self.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        addr = infos[0][4][:2]
        self._handshake_done = loop.create_future()
        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), remote_addr=addr)
        self._transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:  # DSCP EF: low-latency traffic class, ignored where unsupported
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
            except OSError:
                pass
        try:
            await asyncio.wait_for(self._handshake(), timeout)
        except BaseException:
            self.abort()
            raise

    async def _handshake(self) -> None:
        assert self._handshake_done is not None
        self._send_all(self.session.start())
        wait = 0.5
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(self._handshake_done), wait)
                return
            except TimeoutError:
                if self._socket_error is not None:
                    raise ConnectionError(f"{self.host}:{self.port} refused the connection") from self._socket_error
                self._send_all(self.session.retransmit())
                wait = min(wait * 2, 4.0)

    def _send_all(self, datagrams: list[bytes]) -> None:
        if self._transport is None:
            return
        for d in datagrams:
            self._transport.sendto(d)

    def _on_datagram(self, data: bytes) -> None:
        s = self.session
        was_established = s.established
        try:
            replies = s.receive(data)
        except Exception as exc:  # noqa: BLE001 - malformed input must not kill the loop
            log.debug("DTLS: dropping malformed datagram: %s", exc)
            return
        self._send_all(replies)
        done = self._handshake_done
        if done is not None and not done.done():
            if s.error is not None:
                done.set_exception(s.error)
            elif s.established:
                done.set_result(None)
        elif was_established and s.closed:
            self._report_close(s.error)

    def _on_socket_error(self, exc: Exception) -> None:
        self._socket_error = exc

    def _on_lost(self, exc: Exception | None) -> None:
        self._transport = None
        if self.session.established:
            self._report_close(exc)

    def _report_close(self, exc: Exception | None) -> None:
        if self._closed_reported:
            return
        self._closed_reported = True
        if self.on_close is not None:
            self.on_close(exc)

    def send(self, data: bytes) -> None:
        if self._transport is None:
            raise DtlsError("not connected")
        self._transport.sendto(self.session.encrypt(data))

    def close(self) -> None:
        """Send close_notify and release the socket."""
        self._closed_reported = True
        if self._transport is not None:
            note = self.session.close_notify()
            if note is not None:
                try:
                    self._transport.sendto(note)
                except OSError:
                    pass
            self._transport.close()
            self._transport = None

    def abort(self) -> None:
        self._closed_reported = True
        self.session.closed = True
        if self._transport is not None:
            self._transport.close()
            self._transport = None
