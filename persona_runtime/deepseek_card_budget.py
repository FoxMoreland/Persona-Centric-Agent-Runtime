"""Local input budgeting for Community's released Flash card request."""

from functools import lru_cache
from hashlib import sha256
from pathlib import Path


# https://api-docs.deepseek.com/api/list-models/
FLASH_CONTEXT_TOKENS = 1_048_576
_TOKENIZER_PATH = Path(__file__).with_name('deepseek_tokenizer_assets') / 'tokenizer.json'
# https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/tree/314113f8a7dadb388215396ba5fbee2c610a04d6
_TOKENIZER_SHA256 = 'c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b'


def input_limit(max_output_tokens):
    return (FLASH_CONTEXT_TOKENS - max_output_tokens) * 4 // 5


@lru_cache(maxsize=1)
def _tokenizer():
    try:
        from tokenizers import Tokenizer

        raw = _TOKENIZER_PATH.read_bytes()
        if sha256(raw).hexdigest() != _TOKENIZER_SHA256:
            raise ValueError('deepseek_flash_tokenizer_digest')
        return Tokenizer.from_buffer(raw)
    except (ImportError, OSError, ValueError) as error:
        raise ValueError('deepseek_flash_tokenizer_unavailable') from error


def input_tokens(messages):
    system, user = messages
    # The public V4.1 encoding for two text messages in non-thinking mode.
    prompt = (f'<｜begin▁of▁sentence｜><｜System｜>{system["content"]}'
              f'<｜User｜>{user["content"]}<｜Assistant｜></think>')
    return len(_tokenizer().encode(prompt, add_special_tokens=False).ids)
