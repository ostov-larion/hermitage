"""Один бинарный файл вместо тысяч файлов миниатюр.

Раньше каждая миниатюра лежала отдельным JPEG в
`$XDG_CACHE_HOME/masonry-gallery/thumbs-v2/<бакет>/<sha1>.jpg`. На каждый показ
плитки это стоило `stat` + `open` + чтение: четыре-пять системных вызовов и
поиск в каталоге, который на 100 000 файлов сам начинает тормозить.

Здесь все миниатюры складываются в один файл-контейнер с индексом в памяти:

    ┌────────┬──────────────────────────────┬──────────────────────────────┐
    │ маг.   │ запись                       │ запись                       │
    │ 8 байт │ sha1(20) │ len(4) │ данные   │ sha1(20) │ len(4) │ данные   │
    └────────┴──────────────────────────────┴──────────────────────────────┘

* **Чтение** — `mmap` открытого файла + срез по индексу: ни одного системного
  вызова на миниатюру, страницы отдаёт ядро из page cache.
* **Запись** — дописывание в конец одним `write` (заголовок и данные вместе,
  так что оборванных записей не бывает). Перезапись ключа оставляет «мёртвый»
  хвост, поэтому при переполнении файл пересобирается: живые записи
  переносятся в новый файл и `os.replace` подменяет старый атомарно.
* **Индекс** строится сканированием заголовков при открытии — это чтение по
  24 байта на запись, без декодирования; на 100 000 миниатюр ~0,1 с.

Формат намеренно простой: его можно посмотреть `xxd`, а сломать — только
записав мусор в кэш, после чего он просто пересоберётся.
"""
from __future__ import annotations

import mmap
import os
import struct
import threading

MAGIC = b"HRMTPK01"
HEADER = struct.Struct("<20sI")  # ключ (sha1) + длина данных

#: Порог «мусора» для пересборки файла: 25 % и не меньше 4 МБ.
DEAD_RATIO = 0.25
DEAD_MIN = 4 << 20
#: Жёсткий предел размера контейнера по умолчанию (512 МБ).
MAX_BYTES = 512 << 20


class ThumbPack:
    """Контейнер миниатюр: `get(ключ) -> bytes | None`, `put(ключ, данные)`."""

    def __init__(self, path: str, max_bytes: int = MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.compactions = 0
        self._lock = threading.Lock()
        self._index: dict[bytes, tuple[int, int]] = {}
        self._dead = 0
        self._size = 0
        self._mmap: mmap.mmap | None = None
        self._reader = None
        self._writer = None
        self._closed = False
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._open()

    # ------------------------------------------------------------- открытие
    def _open(self) -> None:
        self._reader = open(self.path, "a+b")
        try:
            self._size = os.fstat(self._reader.fileno()).st_size
        except OSError:
            self._size = 0
        if self._size < len(MAGIC):
            self._reader.seek(0)
            self._reader.truncate()
            self._reader.write(MAGIC)
            self._reader.flush()
            self._size = len(MAGIC)
        self._writer = open(self.path, "ab", buffering=0)
        self._index = {}
        self._dead = 0
        self._scan()

    def _scan(self) -> None:
        """Строит индекс по заголовкам записей; обрезает оборванный хвост."""
        reader = self._reader
        reader.seek(0)
        if reader.read(len(MAGIC)) != MAGIC:
            reader.seek(0)
            reader.truncate()
            reader.write(MAGIC)
            reader.flush()
            self._size = len(MAGIC)
            return
        index = self._index
        pos = len(MAGIC)
        size = self._size
        while pos + HEADER.size <= size:
            reader.seek(pos)
            raw = reader.read(HEADER.size)
            if len(raw) < HEADER.size:
                break
            digest, length = HEADER.unpack(raw)
            start = pos + HEADER.size
            end = start + length
            if length <= 0 or end > size:
                break
            index[digest] = (start, length)
            pos = end
        if pos != size:  # недописанная запись (например, жёсткое выключение)
            reader.seek(pos)
            reader.truncate()
            reader.flush()
            self._size = pos

    # ------------------------------------------------------------- чтение
    def _ensure_map(self) -> None:
        if self._mmap is not None:
            return
        if self._size <= 0:
            return
        try:
            self._mmap = mmap.mmap(self._reader.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            self._mmap = None

    def get(self, digest: bytes) -> bytes | None:
        entry = self._index.get(digest)
        if entry is None:
            self.misses += 1
            return None
        start, length = entry
        with self._lock:
            self._ensure_map()
            mapped = self._mmap
            if mapped is not None and start + length <= len(mapped):
                self.hits += 1
                return mapped[start:start + length]
            # файл вырос после mmap — отдаём кусок обычным чтением и
            # пересоздаём отображение при следующем обращении
            if mapped is not None:
                try:
                    mapped.close()
                except (BufferError, ValueError):
                    pass
                self._mmap = None
            try:
                self._reader.seek(start)
                data = self._reader.read(length)
            except OSError:
                self.misses += 1
                return None
        if len(data) != length:
            self.misses += 1
            return None
        self.hits += 1
        return data

    # ------------------------------------------------------------- запись
    def put(self, digest: bytes, data: bytes) -> None:
        if self._closed or not data:
            return
        record = HEADER.pack(digest, len(data)) + data
        with self._lock:
            previous = self._index.get(digest)
            try:
                self._writer.write(record)
            except OSError:
                return
            start = self._size + HEADER.size
            self._size += len(record)
            self._index[digest] = (start, len(data))
            self.writes += 1
            if previous is not None:
                self._dead += previous[1] + HEADER.size
            over_budget = self._size + self._dead > self.max_bytes
            dirty = self._dead > DEAD_MIN and self._dead > self._size * DEAD_RATIO
        if over_budget or dirty:
            self.compact()

    # --------------------------------------------------------- обслуживание
    def compact(self) -> None:
        """Переносит живые записи в новый файл (мусор не копируется)."""
        with self._lock:
            if self._closed or not self._index:
                return
            tmp = self.path + ".tmp"
            index: dict[bytes, tuple[int, int]] = {}
            try:
                with open(tmp, "wb") as out:
                    out.write(MAGIC)
                    pos = len(MAGIC)
                    for digest, (start, length) in self._index.items():
                        self._reader.seek(start)
                        data = self._reader.read(length)
                        if len(data) != length:
                            continue
                        out.write(HEADER.pack(digest, length))
                        out.write(data)
                        index[digest] = (pos + HEADER.size, length)
                        pos += HEADER.size + length
                self._close_files()
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            self._size = pos
            self._dead = 0
            self._index = index
            self.compactions += 1
            self._open_handles()

    def _open_handles(self) -> None:
        self._reader = open(self.path, "a+b")
        self._writer = open(self.path, "ab", buffering=0)
        self._mmap = None

    def _close_files(self) -> None:
        if self._mmap is not None:
            try:
                self._mmap.close()
            except (BufferError, ValueError):
                pass
            self._mmap = None
        for handle in (self._writer, self._reader):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._writer = None
        self._reader = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_files()

    # ------------------------------------------------------------ сведения
    def __len__(self) -> int:
        return len(self._index)

    @property
    def size_bytes(self) -> int:
        return self._size

    @property
    def dead_bytes(self) -> int:
        return self._dead
