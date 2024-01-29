from collections.abc import Callable
from collections.abc import Iterator
from copy import copy
from typing import Any
from typing import cast

import tiktoken
from langchain.prompts.base import StringPromptValue
from langchain.prompts.chat import ChatPromptValue
from langchain.schema import PromptValue
from langchain.schema.language_model import LanguageModelInput
from langchain.schema.messages import AIMessage
from langchain.schema.messages import BaseMessage
from langchain.schema.messages import BaseMessageChunk
from langchain.schema.messages import HumanMessage
from langchain.schema.messages import SystemMessage
from litellm import get_max_tokens  # type: ignore
from tiktoken.core import Encoding

from danswer.configs.app_configs import LOG_LEVEL
from danswer.configs.constants import GEN_AI_API_KEY_STORAGE_KEY
from danswer.configs.constants import MessageType
from danswer.configs.model_configs import DOC_EMBEDDING_CONTEXT_SIZE
from danswer.configs.model_configs import GEN_AI_API_KEY
from danswer.db.models import ChatMessage
from danswer.dynamic_configs import get_dynamic_config_store
from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.indexing.models import InferenceChunk
from danswer.llm.interfaces import LLM
from danswer.utils.logger import setup_logger

logger = setup_logger()

_LLM_TOKENIZER: Any = None
_LLM_TOKENIZER_ENCODE: Callable[[str], Any] | None = None


def get_default_llm_tokenizer() -> Any:
    """Currently only supports the OpenAI default tokenizer: tiktoken"""
    global _LLM_TOKENIZER
    if _LLM_TOKENIZER is None:
        _LLM_TOKENIZER = tiktoken.get_encoding("cl100k_base")
    return _LLM_TOKENIZER


def get_default_llm_token_encode() -> Callable[[str], Any]:
    global _LLM_TOKENIZER_ENCODE
    if _LLM_TOKENIZER_ENCODE is None:
        tokenizer = get_default_llm_tokenizer()
        if isinstance(tokenizer, Encoding):
            return tokenizer.encode  # type: ignore

        # Currently only supports OpenAI encoder
        raise ValueError("Invalid Encoder selected")

    return _LLM_TOKENIZER_ENCODE


def tokenizer_trim_chunks(
    chunks: list[InferenceChunk], max_chunk_toks: int = DOC_EMBEDDING_CONTEXT_SIZE
) -> list[InferenceChunk]:
    tokenizer = get_default_llm_tokenizer()
    new_chunks = copy(chunks)
    for ind, chunk in enumerate(new_chunks):
        tokens = tokenizer.encode(chunk.content)
        if len(tokens) > max_chunk_toks:
            new_chunk = copy(chunk)
            new_chunk.content = tokenizer.decode(tokens[:max_chunk_toks])
            new_chunks[ind] = new_chunk
    return new_chunks


def translate_danswer_msg_to_langchain(msg: ChatMessage) -> BaseMessage:
    if msg.message_type == MessageType.SYSTEM:
        raise ValueError("System messages are not currently part of history")
    if msg.message_type == MessageType.ASSISTANT:
        return AIMessage(content=msg.message)
    if msg.message_type == MessageType.USER:
        return HumanMessage(content=msg.message)

    raise ValueError(f"New message type {msg.message_type} not handled")


def translate_history_to_basemessages(
    history: list[ChatMessage],
) -> tuple[list[BaseMessage], list[int]]:
    history_basemessages = [
        translate_danswer_msg_to_langchain(msg)
        for msg in history
        if msg.token_count != 0
    ]
    history_token_counts = [msg.token_count for msg in history if msg.token_count != 0]
    return history_basemessages, history_token_counts


def dict_based_prompt_to_langchain_prompt(
    messages: list[dict[str, str]]
) -> list[BaseMessage]:
    prompt: list[BaseMessage] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not role:
            raise ValueError(f"Message missing `role`: {message}")
        if not content:
            raise ValueError(f"Message missing `content`: {message}")
        elif role == "user":
            prompt.append(HumanMessage(content=content))
        elif role == "system":
            prompt.append(SystemMessage(content=content))
        elif role == "assistant":
            prompt.append(AIMessage(content=content))
        else:
            raise ValueError(f"Unknown role: {role}")
    return prompt


def str_prompt_to_langchain_prompt(message: str) -> list[BaseMessage]:
    return [HumanMessage(content=message)]


def convert_lm_input_to_basic_string(lm_input: LanguageModelInput) -> str:
    """Heavily inspired by:
    https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain/chat_models/base.py#L86
    """
    prompt_value = None
    if isinstance(lm_input, PromptValue):
        prompt_value = lm_input
    elif isinstance(lm_input, str):
        prompt_value = StringPromptValue(text=lm_input)
    elif isinstance(lm_input, list):
        prompt_value = ChatPromptValue(messages=lm_input)

    if prompt_value is None:
        raise ValueError(
            f"Invalid input type {type(lm_input)}. "
            "Must be a PromptValue, str, or list of BaseMessages."
        )

    return prompt_value.to_string()


def message_generator_to_string_generator(
    messages: Iterator[BaseMessageChunk],
) -> Iterator[str]:
    for message in messages:
        if not isinstance(message.content, str):
            raise RuntimeError("LLM message not in expected format.")

        yield message.content


def should_be_verbose() -> bool:
    return LOG_LEVEL == "debug"


def check_number_of_tokens(
    text: str, encode_fn: Callable[[str], list] | None = None
) -> int:
    """Gets the number of tokens in the provided text, using the provided encoding
    function. If none is provided, default to the tiktoken encoder used by GPT-3.5
    and GPT-4.
    """

    if encode_fn is None:
        encode_fn = tiktoken.get_encoding("cl100k_base").encode

    return len(encode_fn(text))


def get_gen_ai_api_key() -> str | None:
    # first check if the key has been provided by the UI
    try:
        return cast(str, get_dynamic_config_store().load(GEN_AI_API_KEY_STORAGE_KEY))
    except ConfigNotFoundError:
        pass

    # if not provided by the UI, fallback to the env variable
    return GEN_AI_API_KEY


def test_llm(llm: LLM) -> bool:
    # try for up to 2 timeouts (e.g. 10 seconds in total)
    for _ in range(2):
        try:
            llm.invoke("Do not respond")
            return True
        except Exception as e:
            logger.warning(f"GenAI API key failed for the following reason: {e}")

    return False


def get_llm_max_tokens(model_name: str) -> int | None:
    """Best effort attempt to get the max tokens for the LLM"""
    try:
        return get_max_tokens(model_name)
    except Exception:
        return None
