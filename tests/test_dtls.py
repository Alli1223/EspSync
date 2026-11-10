"""DTLS 1.2 PSK client: crypto primitives, record layer, and interop with Mbed TLS.

The interop tests drive our sans-IO client against Mbed TLS's server (via
python-mbedtls, only installable on CPython <= 3.12) entirely in memory.  The
Hue bridge's DTLS stack is Mbed TLS too.
"""

import asyncio
import socket

import pytest

from espsync.lights.dtls import (
    ALERT,
    APPLICATION_DATA,
    HANDSHAKE,
    DtlsAlert,
    DtlsError,
    DtlsPskClient,
    DtlsPskSession,
    _Cipher,
    parse_records,
    prf,
    psk_premaster,
)

PSK = bytes.fromhex("0123456789abcdef0123456789abcdef")
IDENTITY = "espsync-test-identity"


def test_prf_matches_published_tls12_sha256_vector():
    out = prf(
        bytes.fromhex("9bbe436ba940f017b17652849a71db35"),
        b"test label",
        bytes.fromhex("a0ba9f936cda311827a6f796ffd5198c"),
        100,
    )
    assert out.hex() == (
        "e3f229ba727be17b8d122620557cd453c2aab21d07c3d495329b52d4e61edb5a6b301791e90d35c9c9a46b4e14baf9af0fa022f7"
        "077def17abfd3797c0564bab4fbc91666e9def9b97fce34f796789baa48082d122ee42c5a72e5a5110fff70187347b66"
    )


def test_psk_premaster_layout():
    assert psk_premaster(b"\xaa\xbb") == b"\x00\x02\x00\x00\x00\x02\xaa\xbb"


def test_gcm_record_round_trip_and_tamper_detection():
    from cryptography.exceptions import InvalidTag

    from espsync.lights.dtls import DTLS_1_2, RECORD_HEADER, Record

    tx = _Cipher(bytes(range(16)), b"\x01\x02\x03\x04")
    body = tx.seal(APPLICATION_DATA, 1, 7, b"hello")
    assert len(body) == 8 + 5 + 16 and body[:8] == b"\x00\x01" + (7).to_bytes(6, "big")
    datagram = RECORD_HEADER.pack(APPLICATION_DATA, DTLS_1_2, 1, (7).to_bytes(6, "big"), len(body)) + body
    (rec,) = parse_records(datagram)
    assert (rec.type, rec.epoch, rec.seq) == (APPLICATION_DATA, 1, 7)
    assert tx.open(rec) == b"hello"
    with pytest.raises(InvalidTag):  # the sequence number is authenticated
        tx.open(Record(rec.type, rec.version, rec.epoch, 8, rec.fragment))


def test_parse_records_splits_and_drops_truncated():
    s = DtlsPskSession(b"id", PSK)
    (hello,) = s.start()
    two = hello + hello
    assert len(parse_records(two)) == 2
    assert len(parse_records(two[:-1])) == 1


def test_retransmit_uses_new_sequence_numbers():
    s = DtlsPskSession(b"id", PSK)
    (first,) = s.start()
    (again,) = s.retransmit()
    a, b = parse_records(first)[0], parse_records(again)[0]
    assert a.fragment == b.fragment and b.seq == a.seq + 1


def test_encrypt_before_handshake_raises():
    with pytest.raises(DtlsError):
        DtlsPskSession(b"id", PSK).encrypt(b"x")


# ------------------------------------------------------------ Mbed TLS interop
def _mbed():
    return pytest.importorskip("mbedtls.tls", reason="python-mbedtls not installed (CPython <= 3.12 only)")


def _tls_error():
    from mbedtls.exceptions import TLSError

    return TLSError


def _handshake(client: DtlsPskSession):
    """Run a full handshake against an in-memory Mbed TLS DTLS server.

    Returns (server buffer, HelloVerifyRequest seen, server exception or None).
    """
    tls = _mbed()
    conf = tls.DTLSConfiguration(
        pre_shared_key_store={IDENTITY: PSK},
        ciphers=("TLS-PSK-WITH-AES-128-GCM-SHA256",),
        validate_certificates=False,
        lowest_supported_version=tls.DTLSVersion.DTLSv1_2,
    )
    ctx = tls.ServerContext(conf)

    def new_server():
        srv = ctx.wrap_buffers()
        srv.setcookieparam(b"127.0.0.1")
        return srv

    def take(srv) -> bytes:
        out = srv.peek_outgoing(65536) or b""
        srv.consume_outgoing(len(out))
        return out

    srv = new_server()
    inbox = list(client.start())
    hello_verify = False
    for _ in range(200):
        if srv._handshake_state is tls.HandshakeStep.HANDSHAKE_OVER and client.established:
            return srv, hello_verify, None
        try:
            srv.do_handshake()
        except tls.HelloVerifyRequest:
            hello_verify = True
            inbox += client.receive(take(srv))
            srv = new_server()  # what accept() does: new connection, same cookie key
        except tls.WantWriteError:
            inbox += client.receive(take(srv))
        except tls.WantReadError:
            out = take(srv)
            if out:
                inbox += client.receive(out)
            if client.closed or not inbox:
                break
            srv.receive_from_network(inbox.pop(0))
        except _tls_error() as exc:
            out = take(srv)
            if out:
                client.receive(out)
            return srv, hello_verify, exc
    return srv, hello_verify, None


def test_mbedtls_handshake_data_and_close_notify():
    client = DtlsPskSession(IDENTITY.encode(), PSK)
    srv, hello_verify, err = _handshake(client)
    assert err is None and client.established and hello_verify
    for i in range(3):
        srv.receive_from_network(client.encrypt(b"HueStream-%d" % i))
        assert srv.read(100) == b"HueStream-%d" % i
    note = client.close_notify()
    assert parse_records(note)[0].type == ALERT
    srv.receive_from_network(note)
    with pytest.raises(_tls_error(), match="close"):
        srv.read(100)


def test_mbedtls_rejects_wrong_key_with_alert():
    client = DtlsPskSession(IDENTITY.encode(), bytes(16))
    _srv, _hv, err = _handshake(client)
    assert err is not None and not client.established
    assert isinstance(client.error, DtlsAlert) and client.error.description == 20  # bad_record_mac


def test_mbedtls_rejects_unknown_identity_with_alert():
    client = DtlsPskSession(b"nobody", PSK)
    _srv, _hv, err = _handshake(client)
    assert err is not None and isinstance(client.error, DtlsAlert)
    assert client.error.description == 115  # unknown_psk_identity


def test_wrong_server_finished_is_rejected():
    """A server that doesn't know the PSK can't produce a valid Finished."""
    s = DtlsPskSession(IDENTITY.encode(), PSK)
    s._master = b"\x00" * 48
    s._sent_finished = s._rx_epoch1 = True
    assert s._on_message(20, 0, bytes(12)) == []
    assert s.closed and not s.established and "Finished" in str(s.error)


def test_async_client_handshake_timeout_on_silent_port():
    """Nothing answers: connect() gives up (retransmitting meanwhile)."""

    async def main():
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        port = sink.getsockname()[1]
        client = DtlsPskClient("127.0.0.1", port, b"id", PSK)
        try:
            with pytest.raises(TimeoutError):
                await client.connect(timeout=1.2)
            sink.settimeout(0.5)
            got = []
            while True:
                try:
                    got.append(sink.recv(2048))
                except TimeoutError:
                    break
            assert len(got) >= 2  # initial ClientHello + at least one retransmission
            assert all(parse_records(d)[0].type == HANDSHAKE for d in got)
        finally:
            sink.close()

    asyncio.run(main())
