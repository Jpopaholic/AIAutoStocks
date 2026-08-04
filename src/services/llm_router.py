# Path: src/services/llm_router.py
import time
from typing import Any, Dict, Optional
from src.config import config
from src.services.supabase_client import log_system_event

def _call_openai_api(
    prompt: str,
    system_instruction: Optional[str] = None,
    model_name: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    timeout: int = 120
) -> str:
    """
    使用官方 openai SDK 呼叫 OpenAI Chat Completions API。
    """
    api_key = config.openai_api_key
    if not api_key:
        raise ValueError("系統尚未設定 OPENAI_API_KEY")

    if not model_name or not (model_name.startswith("gpt") or model_name.startswith("o1") or model_name.startswith("o3")):
        model_name = config.openai_model or "gpt-4o-mini"

    import openai
    client = openai.OpenAI(api_key=api_key, timeout=float(timeout))

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    kwargs: Dict[str, Any] = {
        "model": model_name,
        "messages": messages,
    }

    # 處理 temperature、response_format 與 Schema 注入
    if generation_config:
        # 若有包含 Pydantic response_schema，提取欄位定義注入提示詞，確保 OpenAI 不遺漏欄位 (如 reason)
        if "response_schema" in generation_config:
            schema = generation_config["response_schema"]
            try:
                import json
                from pydantic import BaseModel
                if isinstance(schema, type) and issubclass(schema, BaseModel):
                    schema_dict = schema.model_json_schema()
                    schema_str = json.dumps(schema_dict, ensure_ascii=False, indent=2)
                    schema_hint = f"\n\n【強烈格式要求】：請務必回傳符合以下 JSON Schema 定義的 JSON 物件，並且必須包含 Schema 中的每一個必填欄位 (例如理由 reason, tactical_directive, regime, posture 等)，嚴禁遺漏任何欄位或回傳 null：\n{schema_str}"
                    
                    if messages and messages[0]["role"] == "system":
                        messages[0]["content"] += schema_hint
                    else:
                        messages.insert(0, {"role": "system", "content": schema_hint})
            except Exception:
                pass

        # 某些 OpenAI 推理模型 (如 o1, o3-mini) 不支援自訂 temperature (僅支援預設 1)
        is_reasoning_model = any(model_name.startswith(prefix) for prefix in ["o1", "o3", "o-"])
        if "temperature" in generation_config:
            if not is_reasoning_model:
                kwargs["temperature"] = float(generation_config["temperature"])
        
        if "response_mime_type" in generation_config and generation_config["response_mime_type"] == "application/json":
            kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**kwargs)
    except openai.BadRequestError as bad_req_err:
        # 若因 temperature 導致 400 BadRequest，自動安全移除 temperature 重新嘗試
        if "temperature" in str(bad_req_err).lower() and "temperature" in kwargs:
            kwargs.pop("temperature", None)
            response = client.chat.completions.create(**kwargs)
        else:
            raise bad_req_err

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI API 回傳空回應")
    
    print(f" [LLM 路由器] 成功完成 OpenAI API 調用 (模型: {model_name})")
    return content

def call_llm_with_rotation(
    prompt: str,
    system_instruction: Optional[str] = None,
    model_name: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    max_api_retries: int = 5,
    timeout: Optional[int] = None
) -> str:
    """
    統一 LLM 調用入口：支援 OpenAI 與 Gemini 雙引擎。
    - 若 AI_PROVIDER == "openai" 或 (AI_PROVIDER == "auto" 且已設定 OPENAI_API_KEY)，優先調用 OpenAI。
    - 若 OpenAI 發送異常或未設定，自動平滑 Fallback 至 Gemini 金鑰輪替器。
    """
    from src.services.gemini_rotator import _call_gemini_direct

    if timeout is None:
        timeout = getattr(config, "gemini_timeout", 120)

    provider = config.ai_provider  # 'auto', 'openai', 'gemini'
    has_openai_key = bool(config.openai_api_key)

    # 判斷是否使用 OpenAI
    should_use_openai = (provider == "openai") or (provider == "auto" and has_openai_key)

    if should_use_openai:
        try:
            return _call_openai_api(
                prompt=prompt,
                system_instruction=system_instruction,
                model_name=model_name,
                generation_config=generation_config,
                timeout=timeout
            )
        except Exception as oa_err:
            err_msg = f"OpenAI API 調用失敗: {str(oa_err)}"
            print(f" [LLM 路由器] 警告: {err_msg}")
            
            # 若為強制使用 OpenAI 模式，直接拋出異常；若為 auto 模式，則降級回退切換至 Gemini
            if provider == "openai":
                log_system_event("ERROR", err_msg)
                raise RuntimeError(err_msg)
            
            print(" [LLM 路由器] 自動降級平滑切換至 Gemini 金鑰輪替器...")
            log_system_event("WARN", f"{err_msg}，系統自動切換至 Gemini 備援節點。")

    # 執行 Gemini 金鑰輪替調用
    return _call_gemini_direct(
        prompt=prompt,
        system_instruction=system_instruction,
        model_name=model_name,
        generation_config=generation_config,
        max_api_retries=max_api_retries,
        timeout=timeout
    )
