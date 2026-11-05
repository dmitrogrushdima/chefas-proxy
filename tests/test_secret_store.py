import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from secret_store import SecretStore, SecretConfigurationError, load_environment
from dashboard_control import DashboardControl
from dashboard_server import DashboardStream
from stratum_session import Relay


def routes(password='pool-secret-value'):
    return {'user_pool':{'host':'127.0.0.1','port':4444,'worker':'wallet','password':password},'devfee_pool':None}


class SecretStoreTests(unittest.TestCase):
    def test_ciphertext_roundtrip_key_and_context_authentication(self):
        key=Fernet.generate_key()
        store=SecretStore(key=key,environment={})
        saved=store.seal_routes(routes())
        self.assertNotIn('pool-secret-value',json.dumps(saved))
        self.assertNotIn(key.decode(),json.dumps(saved))
        self.assertEqual(store.open_routes(saved),routes())
        with self.assertRaises(SecretConfigurationError):
            SecretStore(key=Fernet.generate_key(),environment={}).open_routes(saved)
        with self.assertRaises(SecretConfigurationError):
            SecretStore(environment={}).open_routes(saved)
        changed=copy.deepcopy(saved)
        changed['user_pool']['host']='other.invalid'
        with self.assertRaises(SecretConfigurationError): store.open_routes(changed)
        changed=copy.deepcopy(saved)
        token=changed['user_pool']['password_secret']['ciphertext']
        changed['user_pool']['password_secret']['ciphertext']=token[:50]+('A' if token[50]!='A' else 'B')+token[51:]
        with self.assertRaises(SecretConfigurationError): store.open_routes(changed)

    def test_env_references_and_no_plaintext_fallback(self):
        store=SecretStore(environment={'KONTUR_USER_PASSWORD':'env-secret-value'})
        saved=store.seal_routes(routes('env-secret-value'))
        self.assertNotIn('env-secret-value',json.dumps(saved))
        self.assertEqual(store.open_routes(saved),routes('env-secret-value'))
        with self.assertRaises(SecretConfigurationError): SecretStore(environment={}).open_routes(saved)
        with self.assertRaises(SecretConfigurationError): SecretStore(environment={}).seal_routes(routes())
        for value in ('','x'):
            self.assertEqual(SecretStore(environment={}).open_routes(SecretStore(environment={}).seal_routes(routes(value))),routes(value))

    def test_dotenv_never_overrides_server_env_or_expands_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'KONTUR_ADMIN_TOKEN':'server-value'},clear=True):
            path=Path(directory)/'.env'
            path.write_text('KONTUR_ADMIN_TOKEN=file-value\nKONTUR_USER_PASSWORD=literal-${HOME}\n',encoding='utf-8-sig')
            load_environment(path)
            self.assertEqual(os.environ['KONTUR_ADMIN_TOKEN'],'server-value')
            self.assertEqual(os.environ['KONTUR_USER_PASSWORD'],'literal-${HOME}')

    def test_legacy_control_migrates_atomically_without_plaintext_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'control.json'
            path.write_text(json.dumps({'blacklist':[],'observers':{},'routes':routes(),'admin_token':'old-admin-secret'}),encoding='utf-8')
            relay=Relay('127.0.0.1',5555)
            stream=DashboardStream(relay.live_snapshot)
            store=SecretStore(key=Fernet.generate_key(),environment={})
            DashboardControl(relay,stream,state_path=path,secret_store=store)
            data=path.read_text()
            self.assertNotIn('pool-secret-value',data)
            self.assertNotIn('old-admin-secret',data)
            self.assertEqual(relay.user_password,'pool-secret-value')
            self.assertEqual(len(list(Path(directory).iterdir())),1)
