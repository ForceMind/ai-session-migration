"""Bounded ZIP readers for authenticated recovery without plaintext ZIP staging.

The central directory is iterated, never materialized by ZipFile. GCM archives
are authenticated in full before any ZIP metadata is interpreted. A source's
stat identity is pinned between authentication, indexing and extraction.
"""
from __future__ import annotations

import base64
import codecs
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import struct
import stat
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .backup import MAGIC, Cancelled, read_key

CHUNK = 1024 * 1024
MAX_VALUE = 8 * CHUNK


def cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("任务已取消；原始资料未改变")


def descriptor_identity(fd):
    """Use one stat API for saved identities and opened-handle comparisons.

    Windows path-stat and descriptor-stat can expose different file identifiers
    or timestamp precision. Compare descriptor records to descriptor records.
    """
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode):
        raise ValueError("恢复来源不是普通文件")
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def open_regular(path, expected=None):
    path = Path(path)
    # Check before opening so a FIFO/device cannot block. O_NONBLOCK covers a
    # replacement between lstat and open on POSIX; fstat then rejects its type.
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("恢复来源不是普通文件或为符号链接")
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        opened = descriptor_identity(fd)
        if expected is not None and opened != list(expected):
            raise ValueError('备份来源在打开时改变')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def identity(path):
    with open_regular(path) as f:
        return descriptor_identity(f.fileno())


def check_identity(path, expected):
    if identity(path) != list(expected):
        raise ValueError("备份来源已改变，请重新扫描和建立索引")


def asmb_header(path):
    with open_regular(path) as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("加密备份格式不受支持")
        length = f.read(4)
        if len(length) != 4:
            raise ValueError("加密备份头不完整")
        n = struct.unpack('>I', length)[0]
        if not 1 <= n <= 16384:
            raise ValueError("加密备份头过大")
        raw = f.read(n)
        if len(raw) != n:
            raise ValueError("加密备份头不完整")
        meta = json.loads(raw)
        if meta.get('algorithm') != 'AES-256-GCM':
            raise ValueError("加密算法不受支持")
        nonce = base64.b64decode(meta['nonce'], validate=True)
        if len(nonce) != 12 or not isinstance(meta.get('keyId'), str):
            raise ValueError("加密备份头无效")
        start = f.tell()
    return meta, nonce, start, MAGIC + length + raw


def authenticate(path, key_path=None, auth_path=None, progress=lambda *_: None, cancel=None):
    path = Path(path)
    before = identity(path)
    if path.suffix.lower() == '.asmb':
        meta, nonce, start, prefix = asmb_header(path)
        if key_path is None:
            raise ValueError("需要匹配的恢复密钥")
        key, kid = read_key(Path(key_path))
        if kid != meta['keyId']:
            raise ValueError("恢复密钥不匹配")
        length = before[2] - start - 16
        if length < 0 or length >= 2**36 - 32:
            raise ValueError("加密备份长度无效")
        with open_regular(path, before) as f:
            f.seek(-16, 2)
            tag = f.read(16)
            decoder = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decoder.authenticate_additional_data(prefix)
            f.seek(start)
            remaining = length
            while remaining:
                cancelled(cancel)
                block = f.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("加密备份不完整")
                decoder.update(block)
                remaining -= len(block)
                progress('认证加密备份', int((length - remaining) * 100 / max(length, 1)))
            try:
                decoder.finalize()
            except Exception as error:
                raise ValueError("加密备份认证失败") from error
    elif path.suffix.lower() == '.enc':
        if key_path is None or auth_path is None:
            raise ValueError("旧加密备份需要恢复密钥和认证文件")
        password = bounded_bytes(key_path, 16384)
        meta = json.loads(bounded_bytes(auth_path, 65536))
        mac = hmac.new(hashlib.sha256(b'backup-auth-v1\0' + password).digest(), digestmod='sha256')
        sha = hashlib.sha256()
        with open_regular(path, before) as f:
            count = 0
            while block := f.read(CHUNK):
                cancelled(cancel)
                mac.update(block); sha.update(block); count += len(block)
                progress('认证旧加密备份', int(count * 100 / max(before[2], 1)))
        if not hmac.compare_digest(mac.hexdigest(), meta.get('hmac', '')) or sha.hexdigest() != meta.get('ciphertextSha256'):
            raise ValueError("旧加密备份认证失败")
    check_identity(path, before)
    return before


def bounded_bytes(path, maximum=MAX_VALUE):
    with open_regular(path) as f:
        data = f.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("元数据超过读取上限")
    return data


class ArchiveReader(io.RawIOBase):
    def __init__(self, path, expected, key_path=None):
        check_identity(path, expected)
        self.path = Path(path); self.expected = expected
        self.file = open_regular(self.path, expected); self.position = 0
        self.mode = 'plain'; self.start = 0; self.length = expected[2]
        try:
            if self.path.suffix.lower() == '.asmb':
                meta, self.nonce, self.start, _ = asmb_header(path)
                self.key, kid = read_key(Path(key_path))
                if kid != meta['keyId']:
                    raise ValueError("恢复密钥不匹配")
                self.length -= self.start + 16; self.mode = 'gcm'
            elif self.path.suffix.lower() == '.enc':
                password = bounded_bytes(key_path, 16384)
                if self.file.read(8) != b'Salted__':
                    raise ValueError("旧加密备份头无效")
                salt_len = 8 if self.length % 16 == 0 else 16
                salt = self.file.read(salt_len)
                material = hashlib.pbkdf2_hmac('sha256', password.splitlines()[0], salt, 200000, 48)
                self.key, self.iv = material[:32], material[32:]; self.start = self.file.tell()
                cipher_len = self.length - self.start
                if cipher_len < 16 or cipher_len % 16:
                    raise ValueError("旧加密备份长度无效")
                self.file.seek(self.start + cipher_len - 32 if cipher_len >= 32 else self.start)
                previous = self.file.read(16) if cipher_len >= 32 else self.iv
                last = self.file.read(16)
                d = Cipher(algorithms.AES(self.key), modes.CBC(previous)).decryptor()
                tail = d.update(last) + d.finalize(); pad = tail[-1]
                if not 1 <= pad <= 16 or tail[-pad:] != bytes([pad]) * pad:
                    raise ValueError("旧加密备份填充无效")
                self.length = cipher_len - pad; self.mode = 'cbc'
        except BaseException:
            self.file.close()
            raise

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        p = offset if whence == 0 else self.position + offset if whence == 1 else self.length + offset
        if p < 0: raise ValueError("negative seek")
        self.position = p
        return p
    def read(self, size=-1):
        available = max(0, self.length - self.position)
        size = available if size is None or size < 0 else min(size, available)
        if size > MAX_VALUE:
            raise ValueError("请分块读取备份资料")
        if not size: return b''
        if self.mode == 'plain':
            self.file.seek(self.position); result = self.file.read(size)
        else:
            block, skip = divmod(self.position, 16)
            if self.mode == 'gcm':
                counter = self.nonce + (2 + block).to_bytes(4, 'big')
                d = Cipher(algorithms.AES(self.key), modes.CTR(counter)).decryptor()
                self.file.seek(self.start + block * 16)
                result = (d.update(self.file.read(size + skip)) + d.finalize())[skip:]
            else:
                if block:
                    self.file.seek(self.start + (block - 1) * 16); iv = self.file.read(16)
                else:
                    self.file.seek(self.start); iv = self.iv
                d = Cipher(algorithms.AES(self.key), modes.CBC(iv)).decryptor()
                result = (d.update(self.file.read(((size + skip + 15) // 16) * 16)) + d.finalize())[skip:skip + size]
        self.position += len(result)
        return result
    def close(self):
        if not self.closed:
            self.file.close()
        super().close()


def _read_exact(reader, n):
    data = reader.read(n)
    if len(data) != n: raise ValueError("ZIP 目录不完整")
    return data


def central_entries(reader):
    reader.seek(0, 2); length = reader.tell()
    reader.seek(max(0, length - 65557)); tail = reader.read(min(length, 65557))
    at = tail.rfind(b'PK\x05\x06')
    if at < 0 or len(tail) - at < 22: raise ValueError("ZIP 目录缺失")
    eocd = tail[at:at + 22]
    if at + 22 + int.from_bytes(eocd[20:22], 'little') != len(tail): raise ValueError("ZIP 尾部无效")
    if eocd[4:8] != b'\0' * 4: raise ValueError("分盘 ZIP 不受支持")
    count = int.from_bytes(eocd[10:12], 'little'); offset = int.from_bytes(eocd[16:20], 'little')
    if count == 65535 or offset == 0xffffffff:
        absolute = length - len(tail) + at
        reader.seek(absolute - 20); loc = _read_exact(reader, 20)
        if loc[:4] != b'PK\x06\x07': raise ValueError("ZIP64 目录缺失")
        reader.seek(int.from_bytes(loc[8:16], 'little')); end = _read_exact(reader, 56)
        if end[:4] != b'PK\x06\x06': raise ValueError("ZIP64 目录无效")
        count = int.from_bytes(end[32:40], 'little'); offset = int.from_bytes(end[48:56], 'little')
    if offset >= length or count > length // 46: raise ValueError("ZIP 目录范围无效")
    reader.seek(offset)
    for _ in range(count):
        header = _read_exact(reader, 46)
        if header[:4] != b'PK\x01\x02': raise ValueError("ZIP 中央目录无效")
        n, e, c = struct.unpack_from('<HHH', header, 28)
        raw = _read_exact(reader, n); extra = _read_exact(reader, e); _read_exact(reader, c)
        next_pos = reader.tell(); flags = int.from_bytes(header[8:10], 'little')
        name = raw.decode('utf-8' if flags & 2048 else 'cp437')
        fs, cs, start = (int.from_bytes(header[a:a + 4], 'little') for a in (24, 20, 42))
        index = 0
        while index + 4 <= len(extra):
            kind, size = struct.unpack_from('<HH', extra, index); value = extra[index + 4:index + 4 + size]; index += 4 + size
            if kind == 1:
                pos = 0
                for field in ('fs', 'cs', 'start'):
                    current = {'fs': fs, 'cs': cs, 'start': start}[field]
                    if current == 0xffffffff:
                        if pos + 8 > len(value): raise ValueError("ZIP64 字段无效")
                        val = int.from_bytes(value[pos:pos + 8], 'little'); pos += 8
                        if field == 'fs': fs = val
                        elif field == 'cs': cs = val
                        else: start = val
        if start + 30 > length or cs > length: raise ValueError("ZIP 对象范围无效")
        yield {'filename': name, 'compress_type': int.from_bytes(header[10:12], 'little'), 'flag_bits': flags,
               'CRC': int.from_bytes(header[16:20], 'little'), 'compress_size': cs, 'file_size': fs,
               'header_offset': start, 'external_attr': int.from_bytes(header[38:42], 'little')}
        reader.seek(next_pos)


def zip_member(reader, fields):
    raw_filename = fields['filename']
    info = zipfile.ZipInfo(raw_filename)
    # ZipInfo normalizes the host separator on Windows. The central directory
    # contains POSIX source names, including literal backslashes from macOS.
    info.filename = raw_filename
    info.orig_filename = raw_filename
    for k, v in fields.items():
        if k != 'filename': setattr(info, k, v)
    if info.flag_bits & 1: raise ValueError("内层加密 ZIP 不受支持")
    reader.seek(info.header_offset); header = _read_exact(reader, 30)
    if header[:4] != b'PK\x03\x04': raise ValueError("ZIP 对象头无效")
    n, e = struct.unpack_from('<HH', header, 26)
    raw = _read_exact(reader, n); _read_exact(reader, e)
    name = raw.decode('utf-8' if info.flag_bits & 2048 else 'cp437')
    if name != raw_filename or int.from_bytes(header[8:10], 'little') != info.compress_type:
        raise ValueError("ZIP 对象头与目录不一致")
    if reader.tell() + info.compress_size > reader.length: raise ValueError("ZIP 对象越界")
    return zipfile.ZipExtFile(reader, 'r', info, close_fileobj=False)


class VerifiedStream:
    """Hash as the consumer reads. Context exit drains and verifies the stream."""
    def __init__(self, stream, size, sha256=None):
        self.stream = stream; self.size = size; self.sha256 = sha256
        self.count = 0; self.sha = hashlib.sha256(); self.verified = False
    def read(self, size=-1):
        # Never allow one call to allocate a multi-gigabyte payload.
        size = CHUNK if size is None or size < 0 else min(size, MAX_VALUE)
        data = self.stream.read(size)
        self.count += len(data); self.sha.update(data)
        if self.count > self.size: raise ValueError("恢复对象长度超出清单")
        if not data: self.verify()
        return data
    def verify(self):
        if self.count != self.size or self.sha256 and self.sha.hexdigest() != self.sha256:
            raise ValueError("恢复对象内容校验失败")
        self.verified = True
    def finish(self):
        while not self.verified:
            if not self.read(CHUNK): break
    def readable(self): return True


class JsonCursor:
    def __init__(self, stream):
        self.stream = stream; self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.text = ''; self.eof = False; self.parser = json.JSONDecoder()
    def fill(self):
        if self.eof: return False
        data = self.stream.read(64 * 1024)
        self.text += self.decoder.decode(data, final=not data); self.eof = not data
        if len(self.text) > MAX_VALUE: raise ValueError("单项元数据过大")
        return bool(data)
    def strip(self):
        self.text = self.text.lstrip()
        while not self.text and not self.eof:
            self.fill(); self.text = self.text.lstrip()
    def token(self, token):
        self.strip()
        if not self.text.startswith(token): raise ValueError("清单 JSON 结构无效")
        self.text = self.text[len(token):]
    def value(self):
        while True:
            self.strip()
            try:
                value, end = self.parser.raw_decode(self.text)
                # A number at the buffer edge may be incomplete.
                if end == len(self.text) and not self.eof:
                    self.fill(); continue
                self.text = self.text[end:]; return value
            except json.JSONDecodeError:
                if not self.fill(): raise ValueError("清单 JSON 不完整")


def json_fields(stream):
    """Yield (top-level key, value, array_item), buffering one bounded value."""
    c = JsonCursor(stream); c.token('{'); c.strip()
    if c.text.startswith('}'): c.token('}'); return
    while True:
        key = c.value()
        if not isinstance(key, str): raise ValueError("清单字段名无效")
        c.token(':'); c.strip()
        if c.text.startswith('['):
            c.token('['); c.strip()
            if not c.text.startswith(']'):
                while True:
                    yield key, c.value(), True
                    c.strip()
                    if c.text.startswith(']'): break
                    c.token(',')
            c.token(']')
        else:
            yield key, c.value(), False
        c.strip()
        if c.text.startswith('}'): c.token('}'); break
        c.token(',')
    c.strip()
    if c.text: raise ValueError("清单 JSON 尾部无效")


def json_lines(stream):
    pending = b''
    while block := stream.read(64 * 1024):
        pending += block
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            if len(line) > MAX_VALUE: raise ValueError("单项元数据过大")
            if line.strip(): yield json.loads(line)
        if len(pending) > MAX_VALUE: raise ValueError("单项元数据过大")
    if pending.strip(): yield json.loads(pending)
