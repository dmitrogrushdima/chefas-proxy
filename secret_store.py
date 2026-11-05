"""Pool passwords live in memory; authenticated ciphertext or ENV references at rest."""
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from pilot_logging import remember_secret


class SecretConfigurationError(ValueError):
    pass


def load_environment(path=None):
    # Explicit path only: never search parent directories for an unrelated .env.
    if path:
        load_dotenv(Path(path), override=False, interpolate=False, encoding="utf-8-sig")
    for key in ('KONTUR_ADMIN_TOKEN','KONTUR_SECRETS_KEY','KONTUR_USER_PASSWORD','KONTUR_DEVFEE_PASSWORD'):
        remember_secret(os.environ.get(key))


class SecretStore:
    ENV_NAMES = {'user_pool': 'KONTUR_USER_PASSWORD', 'devfee_pool': 'KONTUR_DEVFEE_PASSWORD'}

    def __init__(self, key=None, environment=None):
        self.environment = os.environ if environment is None else environment
        key = self.environment.get('KONTUR_SECRETS_KEY') if key is None else key
        self.cipher = None
        if key:
            try:
                self.cipher = Fernet(key.encode() if isinstance(key,str) else key)
            except (ValueError, TypeError):
                raise SecretConfigurationError('Invalid KONTUR_SECRETS_KEY') from None
            remember_secret(key.decode() if isinstance(key,bytes) else key)

    def seal_routes(self, routes):
        if routes is None:
            return None
        result = {}
        for role, env_name in self.ENV_NAMES.items():
            route = routes[role]
            if route is None:
                result[role] = None
                continue
            public = {key: route[key] for key in ('host','port','worker')}
            password = route['password']
            remember_secret(password)
            if self.cipher:
                payload = json.dumps({'context': dict(role=role, **public), 'password': password}).encode()
                public['password_secret'] = {'scheme': 'fernet-v1', 'ciphertext': self.cipher.encrypt(payload).decode()}
            elif password == self.environment.get(env_name):
                public['password_secret'] = {'scheme':'env-v1','name':env_name}
            elif password in ('', 'x'):
                public['password_secret'] = {'scheme':'empty-v1' if password == '' else 'default-v1'}
            else:
                raise SecretConfigurationError('Set KONTUR_SECRETS_KEY or matching pool password ENV before saving secrets')
            result[role] = public
        return result

    def open_routes(self, routes):
        if routes is None:
            return None
        result = {}
        for role, env_name in self.ENV_NAMES.items():
            route = routes[role]
            if route is None:
                result[role] = None
                continue
            public = {key: route[key] for key in ('host','port','worker')}
            if 'password' in route:
                # Legacy plaintext is consumed only to migrate immediately,
                # never written to an unencrypted backup.
                password = route['password']
            else:
                secret = route.get('password_secret', {})
                scheme = secret.get('scheme')
                if scheme == 'fernet-v1':
                    if self.cipher is None:
                        raise SecretConfigurationError('KONTUR_SECRETS_KEY required to open saved secrets')
                    try:
                        decoded = json.loads(self.cipher.decrypt(secret['ciphertext'].encode()))
                        if decoded['context'] != dict(role=role, **public):
                            raise ValueError('wrong context')
                        password = decoded['password']
                    except (InvalidToken, ValueError, KeyError, TypeError, AttributeError):
                        raise SecretConfigurationError('Saved secret authentication failed; check KONTUR_SECRETS_KEY') from None
                elif scheme == 'env-v1' and secret.get('name') == env_name:
                    password = self.environment.get(env_name)
                    if password is None:
                        raise SecretConfigurationError('Required pool password ENV is missing')
                elif scheme in ('empty-v1', 'default-v1'):
                    password = '' if scheme == 'empty-v1' else 'x'
                else:
                    raise SecretConfigurationError('Unsupported saved secret format')
            if not isinstance(password, str) or len(password) > 1024:
                raise SecretConfigurationError('Invalid saved password')
            remember_secret(password)
            result[role] = dict(public, password=password)
        return result
