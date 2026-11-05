import unittest
from upstream_manager import parse_endpoint, PoolRoute
from secret_store import SecretStore
from cryptography.fernet import Fernet


class UpstreamURLTests(unittest.TestCase):
    def test_secure_plain_and_ipv6_routes_with_explicit_ports(self):
        self.assertEqual(parse_endpoint('stratum+ssl://pool.example:443'),('pool.example',443,True))
        self.assertEqual(parse_endpoint('stratum+tcp://pool.example',3333),('pool.example',3333,False))
        self.assertEqual(parse_endpoint('stratum+ssl://[::1]:443'),('::1',443,True))
        self.assertEqual(parse_endpoint('pool.example',3333),('pool.example',3333,False))
        route = PoolRoute.parse({'host':'stratum+ssl://pool.example:443'})
        self.assertEqual(route.port,443)
        self.assertEqual(route.export()['host'],'stratum+ssl://pool.example:443')

    def test_tls_scheme_survives_encrypted_route_persistence(self):
        route = PoolRoute.parse({'host':'stratum+ssl://pool.example:443','password':'private-pool-value'})
        routes = {'user_pool':route.export(True),'devfee_pool':None}
        store = SecretStore(key=Fernet.generate_key(),environment={})
        restored = store.open_routes(store.seal_routes(routes))
        self.assertEqual(PoolRoute.parse(restored['user_pool']),route)

    def test_ambiguous_or_credential_bearing_urls_are_rejected(self):
        for host,port in [('http://pool.example',443),('stratum+ssl://u:p@pool.example',443),
            ('stratum+ssl://pool.example/path',443),('stratum+ssl://pool.example?q=1',443),
            ('stratum+ssl://pool.example#secret',443),('stratum+ssl://pool.example:444',443),
            ('stratum+ssl://pool.example:99999',None),('stratum+ssl://pool.example',None),
            ('stratum+ssl://[broken',443),('stratum+ssl://pool.example\n',443)]:
            with self.subTest(host=host,port=port),self.assertRaises(ValueError):
                parse_endpoint(host,port)
