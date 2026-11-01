"""vault.py: round trips, wrong person, tampering, the signed-out public-key envelope, keyed hashes, key wrapping."""

import base64

import pytest

from rectifi import userctx, vault


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")


def ctx_for(uid, **kw):
    key = kw.pop("key", vault.new_data_key())
    pub, priv = vault.new_keypair()
    return userctx.UserContext(uid=uid, key=key, public_key=pub, ingest_salt=vault.new_ingest_salt(), strict=True,
                               private_key=priv, **kw)


def test_round_trip_text_json_bytes():
    a = ctx_for(1)
    with userctx.bound(a):
        sealed = vault.seal("SWIGGY BLR ₹250")
        assert sealed.startswith("rx1.") and "SWIGGY" not in sealed
        assert vault.open_value(sealed) == "SWIGGY BLR ₹250"
        assert vault.open_json(vault.seal_json({"a": [1, 2]})) == {"a": [1, 2]}
        blob = vault.seal_bytes(b"\x00png\xff")
        assert blob.startswith(b"rx1.") and vault.open_bytes(blob) == b"\x00png\xff"
        assert vault.seal(None) is None
        assert vault.seal("") != ""                      # an empty string is sealed too, nothing leaks by its absence


def test_sealing_twice_gives_different_text():
    with userctx.bound(ctx_for(1)):
        assert vault.seal("same") != vault.seal("same")


def test_wrong_uid_fails_even_with_the_same_key():
    key = vault.new_data_key()
    a, b = ctx_for(1, key=key), ctx_for(2, key=key)
    sealed = vault.seal("secret", a)
    with pytest.raises(vault.VaultError):
        vault.open_value(sealed, b)


def test_wrong_key_and_tampering_fail():
    a, b = ctx_for(1), ctx_for(1)
    sealed = vault.seal("secret", a)
    with pytest.raises(vault.VaultError):
        vault.open_value(sealed, b)
    raw = bytearray(base64.urlsafe_b64decode(sealed[4:] + "=="))
    raw[-1] ^= 1
    forged = "rx1." + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    with pytest.raises(vault.VaultError):
        vault.open_value(forged, a)
    with pytest.raises(vault.VaultError):
        vault.open_value("rx1.!!!not-base64!!!", a)
    with pytest.raises(vault.VaultError):
        vault.open_value("plain text", a)


def test_a_forged_envelope_is_left_as_text_by_the_row_cell_opener():
    a = ctx_for(1)
    assert vault.open_cell("rx1.AAAAAAAAAAAAAAAAAAAAAAAAAAAA", a) == "rx1.AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert vault.open_cell("hello", a) == "hello" and vault.open_cell(None, a) is None and vault.open_cell(5, a) == 5


def test_signed_out_seal_uses_the_public_key_and_only_the_private_key_opens_it():
    full = ctx_for(1)
    signed_out = userctx.UserContext(uid=1, key=None, public_key=full.public_key, ingest_salt=full.ingest_salt, strict=False)
    sealed = vault.seal("UPI/DR/412/SWIGGY", signed_out)
    assert sealed.startswith("rs1.") and "SWIGGY" not in sealed
    # the signed-out server cannot read it back
    assert vault.open_cell(sealed, signed_out) is None
    with pytest.raises(vault.VaultLocked):
        vault.open_value(sealed, userctx.UserContext(uid=1, key=None, strict=True))
    # signed in with the private key it opens
    assert vault.open_value(sealed, full) == "UPI/DR/412/SWIGGY"
    # the data key alone is not enough; the private key is what opens it
    only_key = userctx.UserContext(uid=1, key=full.key, strict=True)
    with pytest.raises(vault.VaultLocked):
        vault.open_value(sealed, only_key)
    # another person's private key fails
    other = ctx_for(1)
    with pytest.raises(vault.VaultError):
        vault.open_value(sealed, other)


def test_no_key_and_no_public_key_cannot_seal():
    with pytest.raises(vault.VaultLocked):
        vault.seal("x", userctx.UserContext(uid=1, strict=True))


def test_strict_flag_decides_between_error_and_none():
    a = ctx_for(1)
    sealed = vault.seal("x", a)
    locked_strict = userctx.UserContext(uid=1, key=None, strict=True)
    locked_soft = userctx.UserContext(uid=1, key=None, strict=False)
    with pytest.raises(vault.VaultLocked):
        vault.open_cell(sealed, locked_strict)
    assert vault.open_cell(sealed, locked_soft) is None


def test_blind_is_stable_per_person_and_differs_between_people():
    a, b = ctx_for(1), ctx_for(2)
    x1, x2 = vault.blind("123456789012", a), vault.blind("123456789012", a)
    assert x1 == x2 and x1.startswith("b1:") and "123456789012" not in x1
    assert vault.blind("123456789012", b) != x1
    assert vault.blind("other", a) != x1
    with pytest.raises(vault.VaultLocked):
        vault.blind("x", userctx.UserContext(uid=1, key=None))


def test_blind_ingest_works_signed_out_and_is_per_person():
    a = ctx_for(1)
    signed_out = userctx.UserContext(uid=1, key=None, ingest_salt=a.ingest_salt, strict=False)
    assert vault.blind_ingest("ref", a) == vault.blind_ingest("ref", signed_out)
    assert vault.blind_ingest("ref", ctx_for(2)) != vault.blind_ingest("ref", a)
    with pytest.raises(vault.VaultLocked):
        vault.blind_ingest("ref", userctx.UserContext(uid=1, key=None))


def test_laptop_mode_changes_nothing(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    assert vault.seal("plain") == "plain" and vault.blind("x") == "x" and vault.blind_ingest("x") == "x"
    assert vault.seal_bytes(b"abc") == b"abc"


def test_password_keys_and_wrapping():
    salt = b"s" * 16
    v1, k1 = vault.derive_password_keys("correct horse", salt)
    v2, k2 = vault.derive_password_keys("correct horse", salt)
    v3, _ = vault.derive_password_keys("wrong horse", salt)
    assert (v1, k1) == (v2, k2) and v1 != k1 and v3 != v1
    stored = vault.verifier_hash(v1)
    assert vault.verifier_matches(v2, stored) and not vault.verifier_matches(v3, stored)
    dek = vault.new_data_key()
    wrapped = vault.wrap_key(k1, dek, "dek|1")
    assert vault.unwrap_key(k1, wrapped, "dek|1") == dek
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(k1, wrapped, "dek|2")          # a wrapped key cannot be moved to another purpose
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(vault.new_data_key(), wrapped, "dek|1")


def test_recovery_code_has_125_bits_and_unwraps_a_second_copy_of_the_key():
    code = vault.new_recovery_code()
    groups = code.split("-")
    assert len(groups) == 5 and all(len(g) == 5 for g in groups)
    assert vault.new_recovery_code() != code
    salt = b"r" * 16
    dek = vault.new_data_key()
    wrapped = vault.wrap_key(vault.recovery_key(code, salt), dek, "dek|1")
    # typing it in lower case, with or without dashes, still works
    assert vault.unwrap_key(vault.recovery_key(code.lower().replace("-", " "), salt), wrapped, "dek|1") == dek
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(vault.recovery_key(vault.new_recovery_code(), salt), wrapped, "dek|1")
