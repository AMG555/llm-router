"""Secret (SECRET_KEY) rotation tooling."""
import sqlite3

import pytest

from app.crypto import SecretBox
from app.rotate import rotate


def _make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE providers(id INTEGER PRIMARY KEY, headers TEXT);
      CREATE TABLE api_keys(id INTEGER PRIMARY KEY, api_key TEXT);
      CREATE TABLE gateway_keys(id INTEGER PRIMARY KEY, key TEXT);
    """)
    old = SecretBox("old")
    con.execute("INSERT INTO providers(id,headers) VALUES(1,?)", (old.encrypt('{"h":"v"}'),))
    con.execute("INSERT INTO api_keys(id,api_key) VALUES(1,?)", (old.encrypt("sk-secret"),))
    con.execute("INSERT INTO gateway_keys(id,key) VALUES(1,?)", (old.encrypt("sk-router-x"),))
    con.commit()
    con.close()


def test_rotation_reencrypts_all_secrets(tmp_path):
    p = tmp_path / "r.db"
    _make_db(str(p))
    counts = rotate(str(p), "old", "new")
    assert counts["api_keys"] == 1 and counts["gateway_keys"] == 1 and counts["providers"] == 1

    con = sqlite3.connect(str(p))
    new = SecretBox("new")
    assert new.decrypt(con.execute("SELECT api_key FROM api_keys").fetchone()[0]) == "sk-secret"
    assert new.decrypt(con.execute("SELECT key FROM gateway_keys").fetchone()[0]) == "sk-router-x"
    assert new.decrypt(con.execute("SELECT headers FROM providers").fetchone()[0]) == '{"h":"v"}'
    con.close()


def test_rotation_plaintext_to_encrypted(tmp_path):
    p = tmp_path / "p.db"
    con = sqlite3.connect(str(p))
    con.executescript("CREATE TABLE gateway_keys(id INTEGER PRIMARY KEY, key TEXT);")
    con.execute("INSERT INTO gateway_keys(id,key) VALUES(1,'sk-plain')")
    con.commit()
    con.close()
    rotate(str(p), "", "new")
    con = sqlite3.connect(str(p))
    assert SecretBox("new").decrypt(con.execute("SELECT key FROM gateway_keys").fetchone()[0]) == "sk-plain"
    con.close()


def test_rotation_aborts_on_wrong_old_key(tmp_path):
    p = tmp_path / "r2.db"
    _make_db(str(p))
    with pytest.raises(Exception):
        rotate(str(p), "wrong", "new")
