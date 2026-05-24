"""
openai_service.py -- DEPRECATED. Renamed to llm_service.py.
This shim keeps old imports working during the transition.
"""
from services.llm_service import LLMService as OpenAIService, get_llm_service as get_openai_service  # noqa: F401

# --- END OF FILE: openai_service.py ---
