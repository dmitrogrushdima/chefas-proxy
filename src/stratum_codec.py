"""Bounded UTF-8 JSON object framing for Stratum streams.

Each byte is scanned once. Only a balanced object reaches json.loads; malformed
frames are fatal and never resynchronized across uncertain job boundaries.
"""
import codecs
from collections import deque
from decimal import Decimal, InvalidOperation
import json

MINER_METHODS = frozenset(('mining.configure','mining.subscribe','mining.authorize','mining.submit',
    'mining.extranonce.subscribe','mining.suggest_difficulty','mining.suggest_target',
    'mining.get_transactions','mining.capabilities','mining.multi_version','mining.ping'))


class ProtocolError(Exception):
    pass


def _integer(value):
    if len(value) > 80:
        raise ValueError('integer too long')
    return int(value)


def _decimal(value):
    if len(value) > 64:
        raise ValueError('decimal too long')
    number = Decimal(value)
    if not number.is_finite() or abs(number.as_tuple().exponent) > 512 or abs(number.adjusted()) > 256:
        raise ValueError('decimal out of range')
    return number


def _object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class StratumDecoder:
    def __init__(self, max_bytes=4096, role=None, max_depth=32):
        self.max_bytes, self.role, self.max_depth = max_bytes, role, max_depth
        self.buffer = bytearray()
        self.stack = bytearray()
        self.string = self.escape = False
        self.unicode_left = 0
        self.padding = 0
        self.utf8 = codecs.getincrementaldecoder('utf-8')('strict')
        self.failed = False

    def feed(self, data):
        if self.failed:
            raise ProtocolError('decoder closed')
        packets = []
        try:
            # Validate partial multibyte sequences without waiting for a closing brace.
            self.utf8.decode(data, final=False)
            for byte in data:
                if not self.buffer:
                    if byte in b' \t\r\n':
                        self.padding += 1
                        if self.padding > self.max_bytes:
                            raise ValueError('padding limit')
                        continue
                    if byte != 123:
                        raise ValueError('JSON object prefix required')
                    self.padding = 0
                self.buffer.append(byte)
                if len(self.buffer) > self.max_bytes:
                    raise ValueError('message limit')
                if self.string:
                    if self.unicode_left:
                        if byte not in b'0123456789abcdefABCDEF':
                            raise ValueError('invalid Unicode escape')
                        self.unicode_left -= 1
                    elif self.escape:
                        if byte not in b'"\\/bfnrtu':
                            raise ValueError('invalid escape')
                        self.unicode_left = 4 if byte == 117 else 0
                        self.escape = False
                    elif byte == 92:
                        self.escape = True
                    elif byte == 34:
                        self.string = False
                    elif byte < 32:
                        raise ValueError('control byte in string')
                    continue
                if byte == 34:
                    self.string = True
                elif byte in (123,91):
                    self.stack.append(byte)
                    if len(self.stack) > self.max_depth:
                        raise ValueError('nesting limit')
                elif byte in (125,93):
                    if not self.stack or self.stack.pop() != (123 if byte == 125 else 91):
                        raise ValueError('unbalanced object')
                    if not self.stack:
                        packet = json.loads(self.buffer.decode('utf-8'), parse_int=_integer, parse_float=_decimal,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)), object_pairs_hook=_object)
                        self.validate(packet)
                        packets.append(packet)
                        self.buffer.clear()
                elif byte > 127 or byte < 32 and byte not in b'\t\r\n':
                    raise ValueError('invalid byte outside string')
            return packets
        except (ValueError, UnicodeError, TypeError, RecursionError, InvalidOperation):
            self.failed = True
            self.buffer.clear()
            self.stack.clear()
            raise ProtocolError('invalid or oversized Stratum frame') from None

    def validate(self, packet):
        if not isinstance(packet, dict):
            raise ValueError('object required')
        values = [packet]
        while values:
            value = values.pop()
            if isinstance(value, str):
                value.encode('utf-8', 'strict')
            elif isinstance(value, dict):
                values.extend(value.keys())
                values.extend(value.values())
            elif isinstance(value, list):
                values.extend(value)
        if self.role == 'miner':
            if not isinstance(packet.get('method'),str) or packet['method'] not in MINER_METHODS or not isinstance(packet.get('params', []), list):
                raise ValueError('unsupported miner method')
            ident = packet.get('id')
            if type(ident) not in (int,str) or isinstance(ident,str) and len(ident) > 128 or isinstance(ident,int) and ident.bit_length() > 256:
                raise ValueError('invalid request ID')
        elif self.role == 'pool':
            if 'method' in packet:
                if not isinstance(packet['method'],str) or not isinstance(packet.get('params'),list):
                    raise ValueError('invalid pool notification')
            elif 'id' not in packet or not ('result' in packet or 'error' in packet):
                raise ValueError('invalid pool response')

    def eof(self):
        try:
            self.utf8.decode(b'',final=True)
            if self.buffer:
                raise ValueError('partial frame')
        except (ValueError, UnicodeError):
            self.failed = True
            self.buffer.clear()
            self.stack.clear()
            raise ProtocolError('incomplete Stratum frame') from None


class PacketReader:
    def __init__(self, reader, max_bytes=4096, role=None):
        self.reader = reader
        self.decoder = StratumDecoder(max_bytes, role)
        self.pending = deque()

    async def read_packet(self):
        while not self.pending:
            data = await self.reader.read(1024)
            if not data:
                self.decoder.eof()
                return None
            self.pending.extend(self.decoder.feed(data))
        return self.pending.popleft()
