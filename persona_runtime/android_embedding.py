"""Use the Android CPU ONNX session behind the shared embedding contract."""

from collections import namedtuple

from .android_platform import host
from .character_embedding import CharacterEmbeddingProvider
from java import jarray, jint


Encoding = namedtuple("Encoding", "ids offsets")


def prepare_packaged_model(paths):
    from .installation import prepare_model

    bridge = host()
    return prepare_model(paths, copy_asset=lambda name, path: bridge.installAsset(f"bge/{name}", str(path)))


class AndroidTokenizer:
    def __init__(self, bridge):
        self.bridge = bridge

    def encode(self, text, add_special_tokens=True):
        packed = list(self.bridge.bgeTokens(text, add_special_tokens))
        return Encoding(packed[::3], list(zip(packed[1::3], packed[2::3], strict=True)))


class AndroidEmbeddingProvider(CharacterEmbeddingProvider):
    def _load_backend(self, identity):
        bridge = host()
        bridge.openBge(str(self.directory / 'model_optimized.onnx'), str(self.directory / 'tokenizer.json'))
        return bridge, AndroidTokenizer(bridge)

    def _embed_batch(self, model, texts, tokenizer):
        return (model.embedBge(jarray(jint)(tokenizer.encode(text).ids)) for text in texts)
