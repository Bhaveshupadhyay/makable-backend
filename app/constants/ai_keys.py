"""The AI providers users can add their own API keys for. Only these fixed hosts are ever called with a key (no
user-supplied URLs, so a key can't be used to make the server call anywhere else). Each one speaks OpenAI's chat
completions API at `base_url`."""

import re
from dataclasses import dataclass, field
from enum import StrEnum


class AiProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    MISTRAL = "mistral"
    XAI = "xai"
    OPENROUTER = "openrouter"


class AiKeyStatus(StrEnum):
    VALID = "valid"
    # The provider refused the key, on a check or while answering an edit.
    INVALID = "invalid"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    # Chat completions are at `<base_url>/chat/completions`, the model list at `<base_url>/models`.
    base_url: str
    # Where the user creates a key, for the settings page.
    keys_url: str
    # A request that fails with 401/403 for a bad key, when `/models` doesn't need one (OpenRouter's is public).
    check_path: str = "/models"
    # Query for `/models`, e.g. a page size bigger than the default.
    models_query: dict[str, str] = field(default_factory=dict)
    # Headers on top of `Authorization: Bearer <key>`; `{key}` is replaced with the key.
    extra_headers: dict[str, str] = field(default_factory=dict)


PROVIDERS: dict[AiProvider, ProviderSpec] = {
    AiProvider.OPENAI: ProviderSpec("OpenAI", "https://api.openai.com/v1", "https://platform.openai.com/api-keys"),
    # Anthropic's OpenAI-compatible endpoint takes the key as a bearer token; its `/models` wants `x-api-key`.
    AiProvider.ANTHROPIC: ProviderSpec(
        "Anthropic",
        "https://api.anthropic.com/v1",
        "https://console.anthropic.com/settings/keys",
        models_query={"limit": "1000"},
        extra_headers={"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
    ),
    AiProvider.GEMINI: ProviderSpec(
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://aistudio.google.com/apikey",
    ),
    AiProvider.DEEPSEEK: ProviderSpec("DeepSeek", "https://api.deepseek.com", "https://platform.deepseek.com/api_keys"),
    AiProvider.GROQ: ProviderSpec("Groq", "https://api.groq.com/openai/v1", "https://console.groq.com/keys"),
    AiProvider.MISTRAL: ProviderSpec("Mistral", "https://api.mistral.ai/v1", "https://console.mistral.ai/api-keys"),
    AiProvider.XAI: ProviderSpec("xAI", "https://api.x.ai/v1", "https://console.x.ai"),
    AiProvider.OPENROUTER: ProviderSpec(
        "OpenRouter", "https://openrouter.ai/api/v1", "https://openrouter.ai/settings/keys", check_path="/key"
    ),
}

# Model ids as providers write them, e.g. `gpt-4.1-mini`, `models/gemini-2.5-flash`, `meta-llama/llama-3.3-70b:free`.
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
# Models in a provider's list that can't chat (embeddings, speech, images...). Left out of what the user picks from.
NOT_CHAT_MODEL = re.compile(
    r"embed|whisper|tts|transcri|dall-e|image|audio|realtime|moderation|rerank|guard|sora|veo|imagen|aqa|babbage|davinci",
    re.IGNORECASE,
)
# Most models a provider's list is cut to (OpenRouter lists hundreds).
# Model ids of models that read images (the selected element's picture goes with AI edits to them). A model not
# matched just gets no picture.
IMAGE_MODEL = re.compile(
    r"^(gpt-4o|chatgpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|o1(?!-mini)|o3|o4|claude-(3|sonnet|opus|haiku-4|.*-4)|gemini"
    r"|gemma-3|grok-4|grok-.*vision|pixtral|llama-4|mistral-(medium|small)-(3|2505|2506|latest))"
    r"|vision|-vl\b|-vl-",
    re.IGNORECASE,
)

MAX_LISTED_MODELS = 500
# Models a key can show in the chat's model menu.
MAX_ENABLED_MODELS = 20
# The last characters of a key that are shown back, so the user can tell keys apart.
KEY_HINT_CHARS = 4
MAX_API_KEY_LENGTH = 500

# Key checks per user: bursts of this many, then one per this many seconds. They call the provider with the key, so
# this also stops the endpoint being used to test lists of stolen keys.
KEY_CHECK_BURST = 10
KEY_CHECK_EVERY_SECONDS = 30
# How long a check waits for the provider.
KEY_CHECK_TIMEOUT_SECONDS = 15
