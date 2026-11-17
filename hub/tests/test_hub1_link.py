from app import hub1_link as hl


def test_jpake_both_sides_agree_with_the_right_code():
    c, s = hl.Jpake("914955", "client"), hl.Jpake("914955", "server")
    r1c, r1s = c.round_one(), s.round_one()
    c.read_round_one(r1s)
    s.read_round_one(r1c)
    r2c, r2s = c.round_two(), s.round_two()
    assert c.read_round_two(r2s) == s.read_round_two(r2c)


def test_jpake_wrong_code_gives_different_keys():
    c, s = hl.Jpake("914955", "client"), hl.Jpake("111111", "server")
    r1c, r1s = c.round_one(), s.round_one()
    c.read_round_one(r1s)
    s.read_round_one(r1c)
    r2c, r2s = c.round_two(), s.round_two()
    assert c.read_round_two(r2s) != s.read_round_two(r2c)


def test_cipher_and_frame_roundtrip():
    a, b = hl.Cipher(bytes(16), bytes(8)), hl.Cipher(bytes(16), bytes(8))
    msg = {"jsonrpc": "2.0", "id": 0, "method": "/api/getDevList", "params": {}}
    assert hl.unpack(b.decrypt(a.encrypt(hl.pack(msg)))) == msg
