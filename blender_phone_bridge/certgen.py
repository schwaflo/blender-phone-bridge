# SPDX-License-Identifier: MIT
"""Self-signed TLS certificate generator in pure Python.

Phone browsers only give access to motion sensors / camera / WebXR in a
secure context (HTTPS) and an HTTPS page may only talk to ``wss://``.
Blender ships no crypto library, so RSA key generation, DER encoding and
PKCS#1 v1.5 signing are implemented here with the standard library.
"""

import base64
import datetime
import hashlib
import ipaddress
import os
import secrets

# --------------------------------------------------------------------------
# RSA
# --------------------------------------------------------------------------
_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % d for d in range(2, int(p ** 0.5) + 1))]


def _is_probable_prime(n, rounds=40):
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits, e):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if c % e != 1 and _is_probable_prime(c):
            return c


def generate_rsa_key(bits=2048, e=65537):
    while True:
        p = _gen_prime(bits // 2, e)
        q = _gen_prime(bits // 2, e)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        d = pow(e, -1, phi)
        return {"n": n, "e": e, "d": d, "p": p, "q": q,
                "dp": d % (p - 1), "dq": d % (q - 1), "qi": pow(q, -1, p)}


# --------------------------------------------------------------------------
# DER helpers
# --------------------------------------------------------------------------
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, content):
    return bytes([tag]) + _len(len(content)) + content


def _int(v):
    b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big")  # leading 0 keeps it positive
    return _tlv(0x02, b)


def _seq(*items):
    return _tlv(0x30, b"".join(items))


def _set(*items):
    return _tlv(0x31, b"".join(items))


def _oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.insert(0, 0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(enc)
    return _tlv(0x06, out)


_NULL = b"\x05\x00"


def _bitstring(b):
    return _tlv(0x03, b"\x00" + b)


def _octets(b):
    return _tlv(0x04, b)


def _utf8(s):
    return _tlv(0x0C, s.encode("utf-8"))


def _time(dt):
    if dt.year < 2050:
        return _tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, dt.strftime("%Y%m%d%H%M%SZ").encode())


def _explicit(n, content):
    return _tlv(0xA0 + n, content)


def _pem(label, der):
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


# --------------------------------------------------------------------------
# Certificate
# --------------------------------------------------------------------------
_SHA256_RSA = _seq(_oid("1.2.840.113549.1.1.11"), _NULL)


def _sign(key, data):
    digest = hashlib.sha256(data).digest()
    digest_info = _seq(_seq(_oid("2.16.840.1.101.3.4.2.1"), _NULL), _octets(digest))
    k = (key["n"].bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    m = int.from_bytes(em, "big")
    # CRT for speed
    m1 = pow(m, key["dp"], key["p"])
    m2 = pow(m, key["dq"], key["q"])
    h = (key["qi"] * (m1 - m2)) % key["p"]
    s = m2 + h * key["q"]
    return s.to_bytes(k, "big")


def make_self_signed_cert(common_name, ip_addresses=(), dns_names=(), days=397, bits=2048):
    """Return (cert_pem, key_pem) for a fresh self-signed server certificate."""
    key = generate_rsa_key(bits)

    name = _seq(_set(_seq(_oid("2.5.4.3"), _utf8(common_name))),
                _set(_seq(_oid("2.5.4.10"), _utf8("Blender Phone Bridge"))))
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    validity = _seq(_time(now - datetime.timedelta(days=1)), _time(now + datetime.timedelta(days=days)))
    spki = _seq(_seq(_oid("1.2.840.113549.1.1.1"), _NULL),
                _bitstring(_seq(_int(key["n"]), _int(key["e"]))))

    general_names = b""
    for dns in dns_names:
        general_names += _tlv(0x82, dns.encode("ascii"))
    for ip in ip_addresses:
        general_names += _tlv(0x87, ipaddress.ip_address(ip).packed)

    extensions = [
        # subjectAltName
        _seq(_oid("2.5.29.17"), _octets(_seq(general_names))),
        # basicConstraints CA:FALSE (critical)
        _seq(_oid("2.5.29.19"), b"\x01\x01\xff", _octets(_seq())),
        # keyUsage digitalSignature, keyEncipherment (critical)
        _seq(_oid("2.5.29.15"), b"\x01\x01\xff", _octets(_tlv(0x03, b"\x05\xa0"))),
        # extendedKeyUsage serverAuth
        _seq(_oid("2.5.29.37"), _octets(_seq(_oid("1.3.6.1.5.5.7.3.1")))),
    ]

    serial = secrets.randbits(63) | 1
    tbs = _seq(
        _explicit(0, _int(2)),
        _int(serial),
        _SHA256_RSA,
        name,
        validity,
        name,
        spki,
        _explicit(3, _seq(*extensions)),
    )
    cert = _seq(tbs, _SHA256_RSA, _bitstring(_sign(key, tbs)))

    key_der = _seq(_int(0), _int(key["n"]), _int(key["e"]), _int(key["d"]), _int(key["p"]),
                   _int(key["q"]), _int(key["dp"]), _int(key["dq"]), _int(key["qi"]))
    return _pem("CERTIFICATE", cert), _pem("RSA PRIVATE KEY", key_der)


def ensure_certificate(directory, ip):
    """Create (or reuse) cert.pem/key.pem in *directory* valid for *ip*.

    Returns (cert_path, key_path, fingerprint_sha256_hex).
    """
    os.makedirs(directory, exist_ok=True)
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    meta_path = os.path.join(directory, "cert_ip.txt")

    valid = False
    if os.path.exists(cert_path) and os.path.exists(key_path) and os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            parts = f.read().split()
        if len(parts) == 2 and parts[0] == ip:
            try:
                expires = datetime.datetime.fromisoformat(parts[1])
                valid = expires - datetime.datetime.now(datetime.timezone.utc) > datetime.timedelta(days=7)
            except ValueError:
                valid = False

    if not valid:
        days = 397
        cert_pem, key_pem = make_self_signed_cert(
            "Blender Phone Bridge", ip_addresses=[ip, "127.0.0.1"], dns_names=["localhost"], days=days)
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(key_pem)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        with open(cert_path, "w", encoding="utf-8") as f:
            f.write(cert_pem)
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write("%s %s" % (ip, expires.isoformat()))

    with open(cert_path, "r", encoding="utf-8") as f:
        pem = f.read()
    der = base64.b64decode("".join(l for l in pem.splitlines() if not l.startswith("-----")))
    return cert_path, key_path, hashlib.sha256(der).hexdigest()
