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
from danswer.configs.model_configs import GEN_AI_MAX_OUTPUT_TOKENS
from danswer.configs.model_configs import GEN_AI_MAX_TOKENS
from danswer.configs.model_configs import GEN_AI_MODEL_PROVIDER
from danswer.configs.model_configs import GEN_AI_MODEL_VERSION
from danswer.db.models import ChatMessage
from danswer.dynamic_configs import get_dynamic_config_store
from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.indexing.models import InferenceChunk
from danswer.llm.interfaces import LLM
from danswer.utils.logger import setup_logger

logger = setup_logger()

_LLM_TOKENIZER: Any = None
_LLM_TOKENIZER_ENCODE: Callable[[str], Any] | None = None


def get_default_llm_tokenizer() -> Encoding:
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


def tokenizer_trim_content(
    content: str, desired_length: int, tokenizer: Encoding
) -> str:
    tokens = tokenizer.encode(content)
    if len(tokens) > desired_length:
        content = tokenizer.decode(tokens[:desired_length])
    return content


def tokenizer_trim_chunks(
    chunks: list[InferenceChunk], max_chunk_toks: int = DOC_EMBEDDING_CONTEXT_SIZE
) -> list[InferenceChunk]:
    tokenizer = get_default_llm_tokenizer()
    new_chunks = copy(chunks)
    for ind, chunk in enumerate(new_chunks):
        new_content = tokenizer_trim_content(chunk.content, max_chunk_toks, tokenizer)
        if len(new_content) != len(chunk.content):
            new_chunk = copy(chunk)
            new_chunk.content = new_content
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


def test_llm(llm: LLM) -> str | None:
    # try for up to 2 timeouts (e.g. 10 seconds in total)
    error_msg = None
    for _ in range(2):
        try:
            llm.invoke("Do not respond")
            return None
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Failed to call LLM with the following error: {error_msg}")

    return error_msg


def get_llm_max_tokens(
    model_name: str | None = GEN_AI_MODEL_VERSION,
    model_provider: str = GEN_AI_MODEL_PROVIDER,
) -> int:
    """Best effort attempt to get the max tokens for the LLM"""
    if GEN_AI_MAX_TOKENS:
        # This is an override, so always return this
        return GEN_AI_MAX_TOKENS

    if not model_name:
        return 4096

    try:
        if model_provider == "openai":
            return get_max_tokens(model_name)
        return get_max_tokens("/".join([model_provider, model_name]))
    except Exception:
        return 4096


def get_max_input_tokens(
    model_name: str | None = GEN_AI_MODEL_VERSION,
    model_provider: str = GEN_AI_MODEL_PROVIDER,
    output_tokens: int = GEN_AI_MAX_OUTPUT_TOKENS,
) -> int:
    input_toks = (
        get_llm_max_tokens(model_name=model_name, model_provider=model_provider)
        - output_tokens
    )

    if input_toks <= 0:
        raise RuntimeError("No tokens for input for the LLM given settings")

    return input_toks
