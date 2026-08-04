# Path: src/scratch/tests/test_llm_router.py
import unittest
from unittest.mock import patch, MagicMock
from src.services.llm_router import call_llm_with_rotation

class TestLLMRouter(unittest.TestCase):

    @patch("src.services.gemini_rotator._call_gemini_direct")
    @patch("src.services.llm_router.config")
    def test_fallback_to_gemini_when_no_openai_key(self, mock_config, mock_gemini_direct):
        mock_config.ai_provider = "auto"
        mock_config.openai_api_key = None
        mock_config.gemini_timeout = 90
        mock_gemini_direct.return_value = "Gemini Response"

        res = call_llm_with_rotation("Hello")
        self.assertEqual(res, "Gemini Response")
        mock_gemini_direct.assert_called_once()

    @patch("src.services.llm_router._call_openai_api")
    @patch("src.services.llm_router.config")
    def test_openai_called_when_provider_openai(self, mock_config, mock_openai_api):
        mock_config.ai_provider = "openai"
        mock_config.openai_api_key = "sk-proj-testkey"
        mock_config.openai_model = "gpt-4o-mini"
        mock_config.gemini_timeout = 90
        mock_openai_api.return_value = "OpenAI Response"

        res = call_llm_with_rotation("Hello")
        self.assertEqual(res, "OpenAI Response")
        mock_openai_api.assert_called_once()

if __name__ == "__main__":
    unittest.main()
