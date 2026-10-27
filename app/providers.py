PRESETS = {
    "openai":     {"label": "OpenAI",          "adapter": "openai",    "base_url": "https://api.openai.com/v1"},
    "anthropic":  {"label": "Anthropic",       "adapter": "anthropic", "base_url": "https://api.anthropic.com/v1"},
    "gemini":     {"label": "Google Gemini",   "adapter": "gemini",    "base_url": "https://generativelanguage.googleapis.com/v1beta"},
    "groq":       {"label": "Groq",            "adapter": "openai",    "base_url": "https://api.groq.com/openai/v1"},
    "openrouter": {"label": "OpenRouter",      "adapter": "openai",    "base_url": "https://openrouter.ai/api/v1"},
    "deepseek":   {"label": "DeepSeek",        "adapter": "openai",    "base_url": "https://api.deepseek.com/v1"},
    "mistral":    {"label": "Mistral",         "adapter": "openai",    "base_url": "https://api.mistral.ai/v1"},
    "together":   {"label": "Together AI",     "adapter": "openai",    "base_url": "https://api.together.xyz/v1"},
    "cerebras":   {"label": "Cerebras",        "adapter": "openai",    "base_url": "https://api.cerebras.ai/v1"},
    "fireworks":  {"label": "Fireworks",       "adapter": "openai",    "base_url": "https://api.fireworks.ai/inference/v1"},
    "xai":        {"label": "xAI Grok",        "adapter": "openai",    "base_url": "https://api.x.ai/v1"},
    "nvidia":     {"label": "NVIDIA NIM",      "adapter": "openai",    "base_url": "https://integrate.api.nvidia.com/v1"},
    "sambanova":  {"label": "SambaNova",       "adapter": "openai",    "base_url": "https://api.sambanova.ai/v1"},
    "hyperbolic": {"label": "Hyperbolic",      "adapter": "openai",    "base_url": "https://api.hyperbolic.xyz/v1"},
    "perplexity": {"label": "Perplexity",      "adapter": "openai",    "base_url": "https://api.perplexity.ai"},
    "moonshot":   {"label": "Moonshot Kimi",   "adapter": "openai",    "base_url": "https://api.moonshot.ai/v1"},
    "zhipu":      {"label": "Z.ai GLM",        "adapter": "openai",    "base_url": "https://api.z.ai/api/paas/v4"},
    "ollama":     {"label": "Ollama (local)",  "adapter": "openai",    "base_url": "http://localhost:11434/v1"},
    "lmstudio":   {"label": "LM Studio",       "adapter": "openai",    "base_url": "http://localhost:1234/v1"},
    "custom":     {"label": "Custom",          "adapter": "openai",    "base_url": ""},
}


def preset_headers(name: str) -> dict:
    if name == "openrouter":
        return {"HTTP-Referer": "https://localhost", "X-Title": "LLM Router"}
    return {}
