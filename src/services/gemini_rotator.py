# Path: src/services/gemini_rotator.py
import time
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPICallError

from src.config import config
from src.services.supabase_client import get_gemini_keys_state, update_gemini_key_state, log_system_event

# 本地金鑰冷卻備份快取 (當資料庫斷線時的回退方案)
_local_cooling_cache: Dict[str, float] = {}
_local_use_count: Dict[str, int] = {}

def _get_key_hash(key: str) -> str:
    """
    取得金鑰的 SHA-256 雜湊識別碼，防止將明文金鑰寫入資料庫或日誌
    """
    return hashlib.sha256(key.encode('utf-8')).hexdigest()

def _sync_key_state_to_db(key_hash: str, use_count: int, cooled_until_str: Optional[str] = None):
    """
    將金鑰調用次數與冷卻狀態同步至 Supabase
    """
    updates = {
        "use_count": use_count
    }
    if cooled_until_str:
        updates["cooled_until"] = cooled_until_str

    try:
        update_gemini_key_state(key_hash, updates)
    except Exception as e:
        print(f" [金鑰輪替器] 警告: 無法同步金鑰狀態到 Supabase: {str(e)}")

def _get_available_key(retry_count: int = 0, max_retries: int = 5) -> str:
    """
    獲取目前最佳可用（未冷卻且調用次數最少）的 Gemini API 金鑰。
    若所有金鑰皆處於冷卻狀態，將等待最短冷卻時間後重新獲取。
    """
    # 直接從 config.gemini_api_keys 獲取金鑰清單，完美支援 config.json 與 Fly.io 祕密載入
    keys = config.gemini_api_keys
    if not keys:
        raise RuntimeError("安全憑證中未包含任何 Gemini API 金鑰")

    # 1. 取得資料庫中最新狀態
    db_states = {}
    try:
        states_list = get_gemini_keys_state()
        db_states = {s["key_hash"]: s for s in states_list}
    except Exception as e:
        print(f" [金鑰輪替器] 警告: 獲取資料庫金鑰狀態失敗，將採用本地快取: {str(e)}")

    now_ts = time.time()
    candidates = []

    # 2. 篩選出未冷卻的金鑰候選名單
    for key in keys:
        key_hash = _get_key_hash(key)
        
        # 取得資料庫或本地的調用次數
        db_state = db_states.get(key_hash, {})
        use_count = db_state.get("use_count")
        if use_count is None:
            use_count = _local_use_count.get(key_hash, 0)
        _local_use_count[key_hash] = use_count

        # 檢查冷卻狀態
        is_cooled = False
        cooled_until_str = db_state.get("cooled_until")
        
        # 優先檢查資料庫冷卻設定
        if cooled_until_str:
            try:
                # 處理 ISO 時間戳
                clean_time = cooled_until_str.replace("Z", "+00:00")
                cooled_until_ts = datetime.fromisoformat(clean_time).timestamp()
                if cooled_until_ts > now_ts:
                    is_cooled = True
            except ValueError:
                pass
        
        # 本地快取雙重校驗
        local_cooled_until = _local_cooling_cache.get(key_hash, 0.0)
        if local_cooled_until > now_ts:
            is_cooled = True

        if not is_cooled:
            candidates.append((key, key_hash, use_count))

    # 3. 若有可用金鑰，選取使用次數最少者，以平均分散免費用量
    if candidates:
        candidates.sort(key=lambda x: x[2])  # 依調用次數升序排序
        chosen_key, chosen_hash, current_use = candidates[0]
        
        # 增加調用次數並同步
        new_use = current_use + 1
        _local_use_count[chosen_hash] = new_use
        _sync_key_state_to_db(chosen_hash, new_use)
        return chosen_key

    # 4. 若無可用金鑰（全部皆在冷卻），尋找最短冷卻等待時間並進行睡眠等待
    if retry_count >= max_retries:
        raise RuntimeError("已達到最大金鑰輪替等待次數，所有金鑰仍處於冷卻狀態，無法執行 AI 調用")

    earliest_cool_ts = float("inf")
    for key in keys:
        key_hash = _get_key_hash(key)
        db_state = db_states.get(key_hash, {})
        
        # 計算資料庫中最快解凍的時間
        cooled_until_str = db_state.get("cooled_until")
        if cooled_until_str:
            try:
                clean_time = cooled_until_str.replace("Z", "+00:00")
                cooled_until_ts = datetime.fromisoformat(clean_time).timestamp()
                if cooled_until_ts < earliest_cool_ts:
                    earliest_cool_ts = cooled_until_ts
            except ValueError:
                pass
        
        # 計算本地最快解凍時間
        local_cooled = _local_cooling_cache.get(key_hash, 0.0)
        if 0 < local_cooled < earliest_cool_ts:
            earliest_cool_ts = local_cooled

    sleep_sec = earliest_cool_ts - now_ts
    # 限制合理等待時間，避開無限等待
    if sleep_sec > 60.0:
        sleep_sec = 60.0
    elif sleep_sec <= 0:
        sleep_sec = 5.0

    print(f" [金鑰輪替器] 所有金鑰皆在冷卻狀態，系統將等待 {sleep_sec:.1f} 秒後重新嘗試...")
    time.sleep(sleep_sec)
    
    return _get_available_key(retry_count + 1, max_retries)

def call_gemini_with_rotation(
    prompt: str,
    system_instruction: Optional[str] = None,
    model_name: str = "gemini-1.5-flash",
    generation_config: Optional[Dict[str, Any]] = None,
    max_api_retries: int = 5
) -> str:
    """
    包裝 google-generativeai 接口，自動執行金鑰輪替、429 速率限制冷卻及重試機制
    :param prompt: 使用者輸入 Prompt
    :param system_instruction: 系統指令 (System Prompt)
    :param model_name: 採用的模型名稱，預設為 'gemini-1.5-flash'
    :param generation_config: 輸出參數設定 (如 Response Schema)
    :param max_api_retries: 最大 API 重試次數
    :returns: Gemini 的生成文字
    """
    last_error = None
    
    for attempt in range(1, max_api_retries + 1):
        # 1. 取得當前最佳可用金鑰
        try:
            api_key = _get_available_key()
            key_hash = _get_key_hash(api_key)
        except Exception as err:
            raise RuntimeError(f"無法獲取可用金鑰以調用 Gemini: {str(err)}")

        # 2. 設定此線程/呼叫之金鑰並調用 SDK
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_instruction
            )
            
            response = model.generate_content(
                prompt,
                generation_config=generation_config
            )
            
            return response.text
        except ResourceExhausted as e:
            # 3. 處理 429 速率限制：將目前金鑰設定為冷卻狀態，並同步至資料庫
            last_error = e
            cooldown_seconds = 60
            cool_until_dt = datetime.utcnow() + timedelta(seconds=cooldown_seconds)
            cool_until_str = cool_until_dt.isoformat() + "Z"
            
            # 設定本地與資料庫狀態
            _local_cooling_cache[key_hash] = cool_until_dt.timestamp()
            _sync_key_state_to_db(key_hash, _local_use_count.get(key_hash, 1), cool_until_str)
            
            log_system_event(
                "WARN",
                f"Gemini API 金鑰輪替與冷卻狀態 異動: 金鑰 [{key_hash[:8]}...] 觸發 429 Rate Limit。標記冷卻 {cooldown_seconds} 秒。"
            )
            print(f" [金鑰輪替器] 金鑰 [{key_hash[:8]}] 觸發 429，開始進行金鑰輪替...")
            
        except GoogleAPICallError as e:
            # 處理其他 Google API 呼叫異常
            last_error = e
            print(f" [金鑰輪替器] 金鑰 [{key_hash[:8]}] 調用失敗: {str(e)}，嘗試其他金鑰。")
            time.sleep(2)
        except Exception as e:
            # 處理非預期異常
            last_error = e
            print(f" [金鑰輪替器] 未知錯誤 (金鑰 [{key_hash[:8]}]): {str(e)}")
            time.sleep(2)

    log_system_event("ERROR", f"已嘗試多組金鑰輪替後仍調用失敗。最後錯誤: {str(last_error)}")
    raise RuntimeError(f"經過重試後，調用 Gemini API 仍失敗。最後錯誤: {str(last_error)}")
