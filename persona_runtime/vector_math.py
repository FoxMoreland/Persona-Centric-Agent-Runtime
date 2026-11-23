"""Float32 vector operations shared by the desktop and Android recall paths."""

from array import array
import math
import sys


DIMENSION = 512
_ROW_BYTES = DIMENSION * 4


def _dot(a, b):
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


class VectorMath:
    def __init__(self, numpy=None):
        self.np = numpy

    def normalize(self, values, count):
        if self.np is not None:
            np = self.np
            vectors = np.asarray(values, dtype=np.float32)
            if vectors.shape != (count, DIMENSION) or not np.isfinite(vectors).all():
                raise ValueError("invalid embedding shape or value")
            norms = np.linalg.norm(vectors, axis=1)
            if not np.isfinite(norms).all() or (norms <= 0).any():
                raise ValueError("invalid embedding norm")
            return vectors / norms[:, None]

        rows = [array('f', row) for row in values]
        if len(rows) != count or any(len(row) != DIMENSION for row in rows):
            raise ValueError("invalid embedding shape")
        normalized = []
        for row in rows:
            norm = math.sqrt(_dot(row, row))
            if not math.isfinite(norm) or norm <= 0:
                raise ValueError("invalid embedding norm")
            normalized.append(array('f', (value / norm for value in row)))
        return normalized

    def decode(self, blob):
        if len(blob) % _ROW_BYTES:
            raise ValueError("invalid vector byte count")
        if self.np is not None:
            return self.np.frombuffer(blob, dtype='<f4').reshape((-1, DIMENSION))
        values = array('f')
        values.frombytes(blob)
        if sys.byteorder != 'little':
            values.byteswap()
        return [values[start:start + DIMENSION] for start in range(0, len(values), DIMENSION)]

    def encode(self, rows):
        if self.np is not None:
            return self.np.asarray(rows, dtype='<f4').reshape((-1, DIMENSION)).tobytes()
        values = array('f', (value for row in rows for value in row))
        if sys.byteorder != 'little':
            values.byteswap()
        return values.tobytes()

    def are_unit(self, rows, *, atol, rtol=0):
        if self.np is not None:
            return bool(self.np.isfinite(rows).all()
                        and self.np.allclose(self.np.linalg.norm(rows, axis=1), 1,
                                             atol=atol, rtol=rtol))
        return all(len(row) == DIMENSION and abs(math.sqrt(_dot(row, row)) - 1) <= atol + rtol
                   for row in rows)

    def join(self, batches):
        if self.np is not None:
            return self.np.concatenate(batches)
        return [row for batch in batches for row in batch]

    def max_similarity(self, left, right):
        if self.np is not None:
            return float((self.np.asarray(left, dtype=self.np.float32) @ right.T).max())
        return max(_dot(a, b) for a in left for b in right)

    def best_queries(self, query, documents):
        if self.np is not None:
            scores = query @ self.np.asarray(documents, dtype=self.np.float32).reshape((-1, DIMENSION)).T
            for column in scores.T:
                best = int(column.argmax())
                yield best, float(column[best])
        else:
            for document in documents:
                yield max(enumerate(_dot(part, document) for part in query), key=lambda pair: pair[1])
