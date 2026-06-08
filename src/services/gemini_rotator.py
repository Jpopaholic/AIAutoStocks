# Path: src/services/gemini_rotator.py
import time
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPICallError

from src.config import config
from src.services.supabase_client import get_gemini_keys_state, update_gemini_key_state, log_system_event

class DailyRateLimitExceeded(Exception):
    """每日調用次數達上限 (RPD)"""
    pass

# 本地金鑰冷卻備份快取 (當資料庫斷線時的回退方案)
_local_cooling_cache: Dict[str, float] = {}
_local_use_count: Dict[str, int] = {}

# 追蹤每個金鑰雜湊在過去 24 小時與 60 秒內的調用紀錄
# 儲存格式: key_hash -> [{"timestamp": float, "tokens": int}]
_key_request_history: Dict[str, List[Dict[str, Any]]] = {}

# 預設的免費版金鑰頻率限制上限
RPM_LIMIT = 15
TPM_LIMIT = 1000000
RPD_LIMIT = 1500

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
    獲取目前最佳可用（未冷卻且未達 RPD 限制，且調用次數最少）的 Gemini API 金鑰。
    若所有金鑰皆處於冷卻狀態，將等待最短冷卻時間後重新獲取。
    """
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

    # 2. 篩選出未冷卻且未超限的金鑰候選名單
    for key in keys:
        key_hash = _get_key_hash(key)
        
        # 2.1 檢查記憶體內的 RPD 限制
        history = _key_request_history.get(key_hash, [])
        one_day_ago = now_ts - 86400.0
        history = [r for r in history if r["timestamp"] > one_day_ago]
        _key_request_history[key_hash] = history
        
        if len(history) >= RPD_LIMIT:
            # 達 RPD 上限，跳過此金鑰
            continue

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

    # 4. 若無可用金鑰，檢查是否因為全部達到 RPD 限制
    rpd_exhausted_count = 0
    for key in keys:
        kh = _get_key_hash(key)
        if len(_key_request_history.get(kh, [])) >= RPD_LIMIT:
            rpd_exhausted_count += 1

    if rpd_exhausted_count == len(keys):
        raise DailyRateLimitExceeded("所有可用 Gemini API 金鑰皆已達到每日限制上限 (RPD)")

    # 5. 若非 RPD 超限，代表全部皆在冷卻，尋找最短冷卻等待時間並進行睡眠等待
    if retry_count >= max_retries:
        raise RuntimeError("已達到最大金鑰輪替等待次數，所有金鑰仍處於冷卻狀態，無法執行 AI 調用")

    earliest_cool_ts = float("inf")
    for key in keys:
        key_hash = _get_key_hash(key)
        db_state = db_states.get(key_hash, {})
        
        cooled_until_str = db_state.get("cooled_until")
        if cooled_until_str:
            try:
                clean_time = cooled_until_str.replace("Z", "+00:00")
                cooled_until_ts = datetime.fromisoformat(clean_time).timestamp()
                if cooled_until_ts < earliest_cool_ts:
                    earliest_cool_ts = cooled_until_ts
            except ValueError:
                pass
        
        local_cooled = _local_cooling_cache.get(key_hash, 0.0)
        if 0 < local_cooled < earliest_cool_ts:
            earliest_cool_ts = local_cooled

    sleep_sec = earliest_cool_ts - now_ts
    if sleep_sec > 60.0:
        sleep_sec = 60.0
    elif sleep_sec <= 0:
        sleep_sec = 5.0

    print(f" [金鑰輪替器] 所有金鑰皆在冷卻狀態，系統將等待 {sleep_sec:.1f} 秒後重新嘗試...")
    time.sleep(sleep_sec)
    
    return _get_available_key(retry_count + 1, max_retries)

def _pace_key_rate_limits(key_hash: str, estimated_tokens: int) -> None:
    """
    主動檢查並調節 (Pacing) 指定金鑰的調用頻率，避免觸發 RPM/TPM/RPD 上限。
    若將超限，則自動暫停 (sleep) 到限制消退後再繼續。
    """
    global _key_request_history
    
    if key_hash not in _key_request_history:
        _key_request_history[key_hash] = []
        
    while True:
        now = time.time()
        one_min_ago = now - 60.0
        
        history = _key_request_history[key_hash]
        # 篩選過去 60 秒內的請求
        min_history = [r for r in history if r["timestamp"] > one_min_ago]
        _key_request_history[key_hash] = [r for r in history if r["timestamp"] > now - 86400.0]
        
        rpm_count = len(min_history)
        tpm_count = sum(r["tokens"] for r in min_history)
        
        # 1. 檢查是否超額 RPM
        if rpm_count >= RPM_LIMIT:
            oldest_req = min_history[0]
            wait_time = oldest_req["timestamp"] + 60.0 - now
            if wait_time > 0:
                print(f" [金鑰輪替器] 警告: 金鑰 [{key_hash[:8]}...] 即將達到 RPM 上限 ({rpm_count}/{RPM_LIMIT})，自動暫停 {wait_time:.2f} 秒等待限制消退...")
                time.sleep(wait_time + 0.1)
                continue
                
        # 2. 檢查是否超額 TPM
        if tpm_count + estimated_tokens >= TPM_LIMIT:
            target_time = min_history[0]["timestamp"]
            accumulated_tpm = tpm_count
            for r in min_history:
                accumulated_tpm -= r["tokens"]
                if accumulated_tpm + estimated_tokens < TPM_LIMIT:
                    target_time = r["timestamp"]
                    break
            wait_time = target_time + 60.0 - now
            if wait_time > 0:
                print(f" [金鑰輪替器] 警告: 金鑰 [{key_hash[:8]}...] 即將達到 TPM 上限 (目前累計: {tpm_count} tokens)，自動暫停 {wait_time:.2f} 秒等待限制消退...")
                time.sleep(wait_time + 0.1)
                continue
                
        break

def _record_key_request(key_hash: str, tokens: int) -> None:
    """
    向金鑰歷史中記錄一筆成功調用的請求
    """
    global _key_request_history
    if key_hash not in _key_request_history:
        _key_request_history[key_hash] = []
    _key_request_history[key_hash].append({
        "timestamp": time.time(),
        "tokens": tokens
    })

def call_gemini_with_rotation(
    prompt: str,
    system_instruction: Optional[str] = None,
    model_name: str = "gemini-1.5-flash",
    generation_config: Optional[Dict[str, Any]] = None,
    max_api_retries: int = 5
) -> str:
    """
    包裝 google-generativeai 接口，自動執行金鑰輪替、主動速率限制 Pacing 及冷卻與重試機制
    """
    last_error = None
    
    # 估算本次請求的 Token 數 (安全估計：字符數 * 0.75)
    estimated_tokens = int((len(prompt) + len(system_instruction or "")) * 0.75)
    
    for attempt in range(1, max_api_retries + 1):
        # 1. 取得當前最佳可用金鑰
        try:
            api_key = _get_available_key()
            key_hash = _get_key_hash(api_key)
        except DailyRateLimitExceeded as rpd_err:
            log_system_event("ERROR", f"所有金鑰皆已耗盡每日額度 (RPD): {str(rpd_err)}")
            raise
        except Exception as err:
            raise RuntimeError(f"無法獲取可用金鑰以調用 Gemini: {str(err)}")

        # 2. 主動頻率限制暫停 (Pacing)
        _pace_key_rate_limits(key_hash, estimated_tokens)

        # 3. 設定此線程/呼叫之金鑰並調用 SDK
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
            
            # 解析實際消耗的 Tokens 並記錄
            actual_tokens = estimated_tokens
            try:
                if response.usage_metadata:
                    actual_tokens = response.usage_metadata.total_token_count
            except Exception:
                pass
                
            _record_key_request(key_hash, actual_tokens)
            return response.text
            
        except ResourceExhausted as e:
            # 處理 429 速率限制
            last_error = e
            cooldown_seconds = 60
            cool_until_dt = datetime.utcnow() + timedelta(seconds=cooldown_seconds)
            cool_until_str = cool_until_dt.isoformat() + "Z"
            
            _local_cooling_cache[key_hash] = cool_until_dt.timestamp()
            _sync_key_state_to_db(key_hash, _local_use_count.get(key_hash, 1), cool_until_str)
            
            log_system_event(
                "WARN",
                f"Gemini API 觸發 429 Rate Limit。標記金鑰 [{key_hash[:8]}...] 冷卻 {cooldown_seconds} 秒。"
            )
            print(f" [金鑰輪替器] 金鑰 [{key_hash[:8]}] 觸發 429，開始進行金鑰輪替...")
            
        except GoogleAPICallError as e:
            last_error = e
            print(f" [金鑰輪替器] 金鑰 [{key_hash[:8]}] 調用失敗: {str(e)}，嘗試其他金鑰。")
            time.sleep(2)
        except Exception as e:
            last_error = e
            print(f" [金鑰輪替器] 未知錯誤 (金鑰 [{key_hash[:8]}]): {str(e)}")
            time.sleep(2)

    log_system_event("ERROR", f"已嘗試多組金鑰輪替後仍調用失敗。最後錯誤: {str(last_error)}")
    raise RuntimeError(f"經過重試後，調用 Gemini API 仍失敗。最後錯誤: {str(last_error)}")

