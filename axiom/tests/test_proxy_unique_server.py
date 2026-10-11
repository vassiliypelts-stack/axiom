"""Offline: the pool hands out a server nobody sits on, not just a free domain name."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from db import database
from channels import proxy_pool as pp

SECRET = 'dd104462821249bd7ac519130220c25d09'
# two names for the same pair of servers, and one name with a server of its own
DNS = {
    'a.example': ['10.0.0.1', '10.0.0.2'],
    'b.example': ['10.0.0.1', '10.0.0.2'],
    'c.example': ['10.0.0.3'],
}


def link(server, host=None):
    tail = f'&host={host}' if host else ''
    return f'tg://proxy?server={server}&port=443&secret={SECRET}{tail}'


class UniqueServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db'),
            patch.object(pp, '_server_ips', lambda s: DNS.get(s, [s] if s[:1].isdigit() else [])),
            patch.object(pp, '_tcp_alive', lambda ip, port, timeout=2.0: ip not in self.down),
        ]
        for p in self.patches:
            p.start()
        self.down = set()
        self.phones = iter(range(10 ** 6))
        database.init_db()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import gc
        gc.collect()
        self.tmp.cleanup()

    def pool(self, *servers):
        with database.get_conn() as conn:
            for ping, s in enumerate(servers):
                conn.execute("INSERT INTO proxies(kind,server,port,secret,source,status,ping_ms) "
                             "VALUES ('mtproto',?,443,?,'test','alive',?)", (s, SECRET, ping))

    def account(self, proxy='', session='s'):
        with database.get_conn() as conn:
            return conn.execute("INSERT INTO accounts(label,phone,status,proxy,tg_session) "
                                "VALUES (?,?,?,?,?)",
                                ('x', f'+7{next(self.phones):06d}', 'warming', proxy, session)).lastrowid

    def test_other_name_of_a_busy_server_is_not_free(self):
        self.pool('b.example')
        self.account(link('10.0.0.1', 'a.example'))
        self.assertEqual(pp.pick_free(1), [link('10.0.0.2', 'b.example')])

    def test_busy_name_still_gives_its_free_server(self):
        self.pool('a.example')
        self.account(link('10.0.0.1', 'a.example'))
        self.assertEqual(pp.pick_free(1), [link('10.0.0.2', 'a.example')])

    def test_unpinned_account_holds_every_server_of_its_name(self):
        self.pool('b.example', 'c.example')
        self.account(link('a.example'))
        self.assertEqual(pp.pick_free(1), [link('10.0.0.3', 'c.example')])

    def test_dead_server_is_skipped(self):
        self.pool('a.example')
        self.down = {'10.0.0.1'}
        self.assertEqual(pp.pick_free(1), [link('10.0.0.2', 'a.example')])

    def test_batch_gets_different_servers(self):
        self.pool('a.example', 'b.example', 'c.example')
        got = pp.pick_free(3)
        self.assertEqual({pp._hostport(lk)[0] for lk in got}, {'10.0.0.1', '10.0.0.2', '10.0.0.3'})

    def test_falls_back_to_a_shared_server_when_none_is_free(self):
        self.pool('b.example')
        self.account(link('10.0.0.1', 'a.example'))
        self.account(link('10.0.0.2', 'c.example'))
        self.assertEqual(pp.pick_free(1), [link('b.example')])

    def test_own_proxy_is_not_counted_when_replacing(self):
        self.pool('c.example')
        me = self.account(link('10.0.0.3', 'c.example'))
        self.assertEqual(pp.pick_free(1, skip_id=me), [link('10.0.0.3', 'c.example')])

    def test_assign_puts_new_accounts_on_different_servers(self):
        self.pool('a.example', 'b.example')
        self.account(link('10.0.0.1', 'c.example'))
        new = [self.account(), self.account()]
        self.assertEqual(pp.assign(ids=new), 2)
        with database.get_conn() as conn:
            rows = conn.execute("SELECT proxy FROM accounts WHERE id IN (?,?)", new).fetchall()
        # only one server is free, so the second account falls back to a shared one
        self.assertEqual(sorted(r['proxy'] for r in rows),
                         sorted([link('10.0.0.2', 'a.example'), link('b.example')]))


if __name__ == '__main__':
    unittest.main()
